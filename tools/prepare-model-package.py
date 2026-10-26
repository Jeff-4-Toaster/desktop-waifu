"""Inspect a pinned experimental model offline; download only with --download.

Never opens a private profile or installs dependencies. Completed model files
are immutable here: matching files are reused and mismatches require manual
resolution. Partial files can resume, but are published only after full SHA256.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'app/companion'))
from model_packages import load_catalog, model_file, verify_model

GIB = 1024 ** 3
SEGMENT_BYTES = 32 * 1024 ** 2
REQUEST_TIMEOUT = 45
ATTEMPTS = 4


class PreparationError(Exception):
    """Only controlled, URL-free messages cross the command-line boundary."""


def validate_range(status, headers, start, end, total):
    expected = end - start + 1
    match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)',
                         headers.get('Content-Range', ''))
    if status != 206 or not match or tuple(map(int, match.groups())) != (start, end, total):
        raise PreparationError('下載伺服器未精確回應指定範圍，未附加資料')
    length = headers.get('Content-Length')
    if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) != expected):
        raise PreparationError('下載範圍的長度標頭不符，未附加資料')
    if headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise PreparationError('下載伺服器使用了不支援的內容編碼')


def nearest_existing(path):
    path = Path(path)
    while not path.exists():
        if path.parent == path:
            raise PreparationError('無法確認目的磁碟的可用空間')
        path = path.parent
    return path


def describe_plan(package, models, workers=1):
    final = model_file(models, package)
    part = final.with_name(final.name + '.part')
    if final.is_symlink() or part.is_symlink():
        raise PreparationError('模型或暫存檔不得使用符號連結')
    installation = verify_model(package, final)
    if installation['status'] not in ('missing', 'verified'):
        raise PreparationError('既有模型不符或無法讀取；保留原檔，請另行處理')
    try:
        partial_size = part.stat().st_size if part.exists() else 0
        if part.exists() and not part.is_file():
            raise PreparationError('暫存路徑不是一般檔案')
        free = shutil.disk_usage(nearest_existing(models)).free
    except OSError:
        raise PreparationError('無法讀取暫存檔或目的磁碟空間') from None
    if partial_size > package['size']:
        raise PreparationError('既有暫存檔超出預期大小；保留原檔，請另行處理')
    remaining = 0 if installation['status'] == 'verified' else package['size'] - partial_size
    required = remaining + 2 * GIB
    return {'profile': package['id'] if 'id' in package else package['name'].split('-')[1],
            'metadata': {key: package[key] for key in ('name', 'revision', 'size', 'sha256', 'license', 'url')},
            'models_directory': str(models), 'existing_file': installation,
            'partial_bytes': partial_size, 'remaining_download_bytes': remaining,
            'free_disk_bytes': free, 'required_free_disk_bytes': required,
            'space_gate_passed': free >= required,
            'recommended_three_weight_bytes': package['size'] * 3,
            'segment_bytes': SEGMENT_BYTES,
            'parallel_workers': workers,
            'maximum_buffered_payload_bytes': workers * SEGMENT_BYTES,
            'maximum_segment_requests': math.ceil(remaining / SEGMENT_BYTES) * ATTEMPTS,
            'request_timeout_seconds': REQUEST_TIMEOUT,
            'note': '下載僅寫入所選模型目錄；3 倍容量為規畫建議，實際門檻為剩餘下載量加 2 GiB。'}


@contextmanager
def exclusive_download(part):
    lock = part.with_name(part.name + '.lock')
    try:
        stream = lock.open('x', encoding='ascii')
    except FileExistsError:
        raise PreparationError('下載鎖已存在；請確認其他下載已退出，再人工處理鎖檔') from None
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def read_segment(package, start, end, opener=urllib.request.urlopen):
    request = urllib.request.Request(package['url'], headers={
        'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity',
        'User-Agent': 'DesktopWaifu-model-package-M1/1.0'})
    for attempt in range(ATTEMPTS):
        try:
            with opener(request, timeout=REQUEST_TIMEOUT) as response:
                validate_range(response.status, response.headers, start, end, package['size'])
                data = response.read(end - start + 2)
            if len(data) != end - start + 1:
                raise PreparationError('下載範圍內容不完整，未附加資料')
            return data
        except PreparationError:
            raise
        except urllib.error.HTTPError as error:
            # Do not reveal redirects, signed CDN URLs, response bodies or headers.
            error.close()
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == ATTEMPTS - 1:
                raise PreparationError(f'下載 HTTP 狀態 {error.code}；保留已驗收的暫存範圍') from None
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException):
            if attempt == ATTEMPTS - 1:
                raise PreparationError('下載連線失敗或逾時；保留已驗收的暫存範圍') from None
        time.sleep(attempt + 1)


def publish_without_overwrite(part, final):
    # Windows rename refuses an existing destination. POSIX rename may replace
    # one, so its atomic exclusive hard-link equivalent is used on that platform.
    try:
        if os.name == 'nt':
            os.rename(part, final)
        else:
            os.link(part, final)
            part.unlink()
    except FileExistsError:
        raise PreparationError('目的模型已出現；保留暫存檔，未覆寫既有模型') from None


def read_segments(package, offset, workers, opener):
    """Fetch a bounded window, yielding only complete contiguous ranges."""
    if workers == 1:
        while offset < package['size']:
            end = min(package['size'] - 1, offset + SEGMENT_BYTES - 1)
            yield offset, read_segment(package, offset, end, opener)
            offset = end + 1
        return
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = deque()
    next_start = offset
    def submit():
        nonlocal next_start
        start = next_start
        end = min(package['size'] - 1, start + SEGMENT_BYTES - 1)
        pending.append((start, executor.submit(read_segment, package, start, end, opener)))
        next_start = end + 1
    try:
        while next_start < package['size'] and len(pending) < workers:
            submit()
        while pending:
            start, future = pending.popleft()
            data = future.result()
            yield start, data
            # Release the consumed payload before starting a replacement fetch.
            del data, future
            if next_start < package['size']:
                submit()
    finally:
        for _, future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def download(package, models, opener=urllib.request.urlopen, workers=1):
    if type(workers) is not int or not 1 <= workers <= 4:
        raise PreparationError('下載工作數必須介於 1 至 4')
    final = model_file(models, package)
    part = final.with_name(final.name + '.part')
    plan = describe_plan(package, models, workers)
    if plan['existing_file']['status'] == 'verified':
        return {'status': 'reused', 'downloaded_bytes': 0, 'verification': plan['existing_file']}
    if not plan['space_gate_passed']:
        raise PreparationError('可用空間不足剩餘下載量加 2 GiB，未開始下載')
    models.mkdir(parents=True, exist_ok=True)
    with exclusive_download(part):
        # Recheck after acquiring the lock in case another invocation completed.
        plan = describe_plan(package, models, workers)
        if plan['existing_file']['status'] == 'verified':
            return {'status': 'reused', 'downloaded_bytes': 0, 'verification': plan['existing_file']}
        if not plan['space_gate_passed']:
            raise PreparationError('可用磁碟空間已變動，未開始下載')
        offset = started = plan['partial_bytes']
        last_progress = offset
        segments = read_segments(package, offset, workers, opener)
        try:
            for start, data in segments:
                if start != offset:
                    raise PreparationError('下載範圍不連續，未附加資料')
                with part.open('ab') as stream:
                    stream.write(data)
                offset += len(data)
                del data
                if offset - last_progress >= 128 * 1024 ** 2 or offset == package['size']:
                    print(f"{package['name']}: {offset}/{package['size']} bytes", file=sys.stderr, flush=True)
                    last_progress = offset
        finally:
            segments.close()
        verified = verify_model(package, part)
        if verified['status'] != 'verified':
            raise PreparationError('完整 SHA256 或大小不符；保留暫存檔，未發布模型')
        publish_without_overwrite(part, final)
        return {'status': 'downloaded' if offset > started else 'published_verified_partial',
                'downloaded_bytes': offset - started, 'verification': verified}


def self_test():
    """Small deterministic tests; no network calls and no real model files."""
    import hashlib
    import threading
    global SEGMENT_BYTES
    checks = 0
    for status, value, length, valid in [
        (206, 'bytes 4-7/12', '4', True), (200, 'bytes 4-7/12', '4', False),
        (206, 'bytes 0-7/12', '4', False), (206, 'bytes 4-8/12', '4', False),
        (206, 'bytes 4-7/13', '4', False), (206, 'bytes 4-7/*', '4', False),
        (206, 'bytes 4-7/12', '5', False), (206, 'bytes 4-7/12', None, True)]:
        headers = {'Content-Range': value}
        if length is not None:
            headers['Content-Length'] = length
        try:
            validate_range(status, headers, 4, 7, 12)
            assert valid
        except PreparationError:
            assert not valid
        checks += 1
    class Response(io.BytesIO):
        status = 206
        headers = {'Content-Range': 'bytes 4-7/8', 'Content-Length': '4'}
    payload = b'abcdefgh'
    package = {'name': 'Qwen3-14B-Q4_K_M.gguf', 'revision': '0' * 40,
               'size': 8, 'sha256': hashlib.sha256(payload).hexdigest(),
               'license': 'Apache-2.0', 'url': 'https://example.invalid/pinned'}
    fixture_root = ROOT / 'build/companion-test'
    fixture_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='model-download-fixture-', dir=fixture_root) as temporary:
        models = Path(temporary)
        final = models / package['name']
        part = final.with_name(final.name + '.part')
        part.write_bytes(payload[:4])
        def opener(request, timeout):
            assert request.headers['Range'] == 'bytes=4-7'
            assert timeout == REQUEST_TIMEOUT
            return Response(payload[4:])
        result = download(package, models, opener)
        assert result['downloaded_bytes'] == 4 and final.read_bytes() == payload and not part.exists()
        checks += 1
        def forbidden(*args, **kwargs):
            raise AssertionError('network must not be used for verified existing files')
        assert download(package, models, forbidden)['status'] == 'reused'
        checks += 1
        part.write_bytes(b'other')
        try:
            publish_without_overwrite(part, final)
            raise AssertionError('existing final was replaced')
        except PreparationError:
            assert final.read_bytes() == payload and part.read_bytes() == b'other'
        checks += 1
        final.write_bytes(b'wrong')
        try:
            download(package, models, forbidden)
            raise AssertionError('mismatched existing file was accepted')
        except PreparationError:
            assert final.read_bytes() == b'wrong'
        checks += 1
        final.unlink()
        part.write_bytes(b'abcdefgZ')
        try:
            download(package, models, forbidden)
            raise AssertionError('unverified completed partial was published')
        except PreparationError:
            assert not final.exists() and part.exists()
        checks += 1
        part.write_bytes(payload[:4])
        class WrongRange(Response):
            headers = {'Content-Range': 'bytes 0-7/8', 'Content-Length': '8'}
        try:
            download(package, models, lambda *args, **kwargs: WrongRange(payload))
            raise AssertionError('incorrect resume range was accepted')
        except PreparationError:
            assert not final.exists() and part.read_bytes() == payload[:4]
        checks += 1
        try:
            download(package, models, lambda *args, **kwargs: Response(b'efg'))
            raise AssertionError('incomplete range was appended')
        except PreparationError:
            assert not final.exists() and part.read_bytes() == payload[:4]
        checks += 1
        part.unlink()
        original_segment_bytes = SEGMENT_BYTES
        SEGMENT_BYTES = 2
        try:
            later_segment = threading.Event()
            finished = []
            def parallel_opener(request, timeout):
                match = re.fullmatch(r'bytes=([0-9]+)-([0-9]+)', request.headers['Range'])
                start, end = map(int, match.groups())
                if start == 0:
                    assert later_segment.wait(5), 'parallel requests were not started'
                response = Response(payload[start:end + 1])
                response.headers = {'Content-Range': f'bytes {start}-{end}/8', 'Content-Length': '2'}
                finished.append(start)
                if start == 6:
                    later_segment.set()
                return response
            result = download(package, models, parallel_opener, workers=4)
            assert result['downloaded_bytes'] == 8 and final.read_bytes() == payload
            assert finished.index(6) < finished.index(0) and not part.exists()
            checks += 1
            final.unlink()
            def failure_opener(request, timeout):
                if request.headers['Range'] == 'bytes=2-3':
                    raise PreparationError('fixture segment failure')
                return parallel_opener(request, timeout)
            try:
                download(package, models, failure_opener, workers=4)
                raise AssertionError('failed parallel segment was accepted')
            except PreparationError:
                assert not final.exists() and part.read_bytes() == payload[:2]
                assert not part.with_name(part.name + '.lock').exists()
            checks += 1
            def resume_opener(request, timeout):
                match = re.fullmatch(r'bytes=([0-9]+)-([0-9]+)', request.headers['Range'])
                start, end = map(int, match.groups())
                assert start >= 2
                response = Response(payload[start:end + 1])
                response.headers = {'Content-Range': f'bytes {start}-{end}/8', 'Content-Length': '2'}
                return response
            assert download(package, models, resume_opener, workers=4)['downloaded_bytes'] == 6
            assert final.read_bytes() == payload and not part.exists()
            checks += 1
        finally:
            SEGMENT_BYTES = original_segment_bytes
    print(json.dumps({'self_test': 'passed', 'checks': checks, 'network_used': False}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('4B', '8B', '14B'))
    parser.add_argument('--models', type=Path, default=ROOT / 'models/companion')
    parser.add_argument('--download', action='store_true', help='明確開始選配模型下載；預設僅離線檢查')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=1,
                        help='分段下載並行數；預設 1，最多緩衝工作數乘 32 MiB')
    parser.add_argument('--self-test', action='store_true', help='僅執行小型離線 fixture')
    args = parser.parse_args()
    if args.self_test:
        if args.download or args.profile or args.workers != 1:
            parser.error('--self-test 不與 --download、--profile 或 --workers 併用')
        return self_test()
    if not args.profile:
        parser.error('請明確指定 --profile 4B、8B 或 14B')
    try:
        package = load_catalog()[args.profile]
        models = args.models.resolve()
        report = {'feature': 'prepare-model-package-m1-20261024',
                  'plan': describe_plan(package, models, args.workers),
                  'download_requested': args.download,
                  'scope': {'profile_accessed': False, 'dependencies_installed': False,
                            'settings_changed': False, 'network_used': False}}
        if args.download:
            report['result'] = download(package, models, workers=args.workers)
            report['scope']['network_used'] = report['result']['downloaded_bytes'] > 0
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (PreparationError, OSError, ValueError, KeyError):
        error = sys.exception()
        message = str(error) if isinstance(error, PreparationError) else '無法讀取套件或模型路徑；未更改既有模型'
        print(json.dumps({'status': 'failed', 'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
