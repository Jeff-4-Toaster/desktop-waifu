"""Read-only, offline package/environment checks; never opens a user profile."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

FEATURE = 'environment-check-20261024'
PROBE_NAMES = ('system', 'graphics', 'webview', 'voices', 'backend')
REQUIRED_FILES = (
    'DesktopWaifu.exe', 'cinderella_renderer.dll', 'SDL2.dll', 'model.inp',
    'CompanionChat.exe', 'SpeechHelper.exe', 'python/python.exe',
    'python/pythonw.exe', 'python/python313.dll', 'python/python313.zip',
    'companion/service.py', 'companion/web/index.html',
)
BACKEND_FILES = ('backend/llama/llama-server.exe', 'backend/llama/llama-server-impl.dll',
                 'backend/llama/llama.dll', 'backend/llama/ggml.dll',
                 'backend/llama/ggml-base.dll', 'backend/llama/ggml-cuda.dll',
                 'backend/llama/cudart64_12.dll')
ASR_FILES = ('backend/whisper/AsrHelper.exe', 'backend/whisper/whisper.dll',
             'backend/whisper/ggml.dll', 'backend/whisper/ggml-base.dll',
             'backend/whisper/ggml-cpu.dll')
MODEL_KEYS = ('8B', '4B', 'asr')
STATUS_LABELS = {'ok': '可用', 'warning': '注意', 'blocked': '缺少條件',
                 'unknown': '未能確認', 'info': '說明'}


def short(value, limit=180):
    return ''.join(c for c in str(value) if c >= ' ' and c != '\x7f')[:limit]


def relative_file(package, name):
    """Never inspect manifest paths outside the selected package."""
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('invalid package path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('invalid package path')
    candidate = package.joinpath(*path.parts)
    if not candidate.resolve().is_relative_to(package.resolve()):
        raise ValueError('package path escapes root')
    return candidate


def read_json(path, limit=2 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError('metadata too large')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run_child_probe(name, package):
    interpreter = package / 'python/python.exe'
    if not interpreter.is_file():
        return {'ok': False, 'error': 'failed', 'detail': 'python_missing'}
    try:
        completed = subprocess.run(
            [str(interpreter), '-B', '-X', 'utf8', str(Path(__file__).resolve()),
             '--package', str(package), '--probe', name],
            cwd=package, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'timeout', 'detail': 'probe_timeout'}
    except OSError:
        return {'ok': False, 'error': 'failed', 'detail': 'probe_start_failed'}
    if len(completed.stdout) > 65536:
        return {'ok': False, 'error': 'invalid', 'detail': 'probe_output_too_large'}
    try:
        result = json.loads(completed.stdout.decode('utf-8-sig'))
        if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                or (result['ok'] and not isinstance(result.get('data'), dict))):
            raise ValueError('invalid probe envelope')
        if completed.returncode and result['ok']:
            raise ValueError('unsuccessful probe')
        return result
    except (ValueError, UnicodeError):
        return {'ok': False, 'error': 'crashed' if completed.returncode else 'invalid',
                'detail': 'probe_did_not_return_valid_json'}


def collect(package, probe_runner=None):
    runner = probe_runner or run_child_probe
    def one(name):
        try:
            return name, runner(name, package)
        except Exception:
            return name, {'ok': False, 'error': 'failed', 'detail': 'probe_runner_failed'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(one, PROBE_NAMES))


def evaluate(package, probes, verify_hashes=False):
    package = Path(package).resolve()
    checks = []
    def add(identifier, status, title, detail, action='', data=None):
        checks.append({'id': identifier, 'status': status, 'title': title,
                       'detail': detail, 'action': action, 'data': data or {}})
    def data(name):
        value = probes.get(name, {})
        if not isinstance(value, dict) or value.get('ok') is not True or not isinstance(value.get('data'), dict):
            reason = value.get('error', 'invalid') if isinstance(value, dict) else 'invalid'
            # Only a known category is exposed; raw child stderr/paths are not a report.
            reason = reason if reason in ('timeout', 'crashed', 'failed', 'invalid') else 'failed'
            add(name, 'unknown', {'system': 'Windows 與處理器', 'graphics': 'OpenGL 繪圖',
                                 'webview': '聊天執行環境', 'voices': '繁中朗讀語音',
                                 'backend': '本地推論後端'}[name],
                '探測未完成（' + reason + '），不能據此判定功能可用。',
                '確認檔案完整並在一般桌面工作階段重試。', {'reason': reason})
            return None
        return value['data']

    missing = [name for name in REQUIRED_FILES if not relative_file(package, name).is_file()]
    add('package', 'blocked' if missing else 'ok', '桌寵與聊天檔案',
        '缺少：' + '、'.join(missing) if missing else '必要程式與聊天入口存在。',
        '重新解壓完整執行包；不要只搬 EXE。' if missing else '', {'missing': missing})
    for identifier, title, names in (('backend_files', '推論檔案', BACKEND_FILES),
                                     ('asr_files', '語音辨識檔案', ASR_FILES)):
        absent = [name for name in names if not relative_file(package, name).is_file()]
        add(identifier, 'warning' if absent else 'ok', title,
            '缺少：' + '、'.join(absent) if absent else '必要檔案存在；不代表已完成模型運算。',
            '重新解壓完整執行包。' if absent else '', {'missing': absent})

    system = data('system')
    if system is not None:
        arch = short(system.get('native_arch', 'unknown'), 24).upper()
        build = system.get('windows_build')
        good_build = type(build) is int and build > 0
        architecture_ok = arch in ('AMD64', 'X64', 'X86_64')
        status = ('unknown' if arch.startswith('UNKNOWN') or not arch or not good_build else
                  'blocked' if not architecture_ok else 'warning' if build < 22000 else 'ok')
        add('system', status, 'Windows 與處理器',
            f'原生架構 {arch}，Windows build {build if good_build else "未知"}。',
            '本包以 Windows 11 x64 為驗收目標；其他平台需另行驗證。',
            {'native_arch': arch, 'windows_build': build if good_build else None})
        features = {key: system.get(key) if type(system.get(key)) is bool else None for key in ('avx', 'avx2')}
        feature_label = lambda value: '有' if value is True else '無' if value is False else '未知'
        add('cpu_features', 'info', 'CPU 指令集',
            'AVX：' + feature_label(features['avx']) + '；AVX2：' + feature_label(features['avx2']) + '。後端實際枚舉另行檢查。', data=features)
        memory = {key: system.get(key) if type(system.get(key)) is int and system[key] >= 0 else None
                  for key in ('ram_total_bytes', 'ram_available_bytes')}
        valid_memory = all(value is not None for value in memory.values())
        detail = ('實體 RAM %.1f GiB，目前可用 %.1f GiB；此數值會隨其他程式改變。' %
                  (memory['ram_total_bytes'] / 2**30, memory['ram_available_bytes'] / 2**30)
                  if valid_memory else '無法取得完整 RAM 資訊。')
        add('memory', 'info' if valid_memory else 'unknown', '記憶體', detail,
            '本檢查不載入模型，不以 RAM 數字保證速度或可用上下文。', memory)

    graphics = data('graphics')
    if graphics is not None:
        version = short(graphics.get('version', ''), 100)
        match = re.match(r'\s*(\d+)\.(\d+)', version)
        supported = bool(match and tuple(map(int, match.groups())) >= (3, 3))
        available = graphics.get('available')
        core = graphics.get('core_profile')
        status = ('ok' if available is True and supported and core is True else
                  'blocked' if available is False or core is False or (match and not supported) else 'unknown')
        add('graphics', status, 'OpenGL 繪圖',
            ('已建立隱藏 OpenGL 3.3 core 測試視窗：' + version if status == 'ok'
             else '無法確認或建立桌寵要求的 OpenGL 3.3 core 繪圖環境。'),
            '更新顯示卡官方驅動並於一般桌面重試；未自動安裝任何項目。' if status != 'ok' else '',
            {'version': version, 'core_profile': core if type(core) is bool else None, 'vendor': short(graphics.get('vendor', '')),
             'renderer': short(graphics.get('renderer', ''))})
    webview = data('webview')
    if webview is not None:
        from environment_probes import parse_webview_payload
        try:
            selected = parse_webview_payload(webview)
        except (ValueError, TypeError):
            add('webview', 'unknown', '聊天執行環境', '執行環境來源無法確認，探針版本或回傳格式不相容。',
                '重新解壓同一批完整執行包後再檢查；未啟動聊天或建立 WebView 資料。')
        else:
            available, version, source = selected['available'], selected['version'], selected['source']
            preview = bool(re.search(r'\b(beta|dev|canary)\b', version, re.IGNORECASE))
            status = 'warning' if available and preview else 'ok' if available else 'blocked'
            label = '包內固定 WebView2' if source == 'fixed' else '系統 WebView2'
            reasons = {'invalid_bundle':'包內固定執行環境不完整或路徑不安全。',
                       'policy_conflict':'系統政策會覆寫所選執行環境，已停止使用固定 Runtime。',
                       'version_unavailable':'未能發現可用的聊天執行環境。'}
            add('webview', status, '聊天執行環境',
                ('偵測到預覽版候選：' + version if preview else label + '：' + version) if available else reasons[selected['error']],
                '正式使用請準備 WebView2 Runtime，不以 Edge 預覽版作正式相容保證。' if preview else
                '重新解壓完整包或由系統管理者確認政策；工具不改登錄、權限或自動安裝。' if not available else
                '版本探測不取代聊天實際啟動、新機與離線完整版驗收。', selected)
    voices = data('voices')
    if voices is not None:
        raw = voices.get('voices')
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            add('voices', 'unknown', '繁中朗讀語音', '語音列舉格式無法辨識。')
        else:
            found = []
            for item in raw[:128]:
                languages = str(item.get('language', '')).split(';')
                if any(re.fullmatch(r'0*404', value.strip(), re.IGNORECASE) for value in languages):
                    found.append(short(item.get('name', '繁中語音'), 100))
            local = voices.get('local', {})
            local_available = (isinstance(local, dict) and local.get('available') is True
                               and local.get('backend') == 'local-melo'
                               and local.get('model_loaded') is False and local.get('playback') is False)
            labels = (['Windows：' + '、'.join(found)] if found else []) + (['隨包中文語音 MeloTTS（支援繁體文字）'] if local_available else [])
            failed = voices.get('windows_probe_error') is True or voices.get('local_probe_error') is True
            add('voices', 'ok' if labels else 'unknown' if failed else 'warning', '繁中朗讀語音',
                '可發現：' + '；'.join(labels) + ('；部分語音探測未完成。' if failed else '') if labels else
                '語音探測未完成，無法確認是否可用；文字功能仍可用。' if failed else '沒有可用的繁中朗讀語音；文字功能仍可用。',
                '只列舉語音及檢查本地 Runtime；未載入權重、合成或播放。MeloTTS 不代表臺灣口音驗收。' if labels else
                '重新解壓完整包，或準備 Windows 繁中語音後再做喇叭實測。',
                {'zh_tw_voices': found, 'local_available': local_available})
    backend = data('backend')
    if backend is not None:
        available = backend.get('cuda_available')
        devices = backend.get('devices', [])
        # Probe helpers may return additional diagnostics; expose hardware labels only.
        safe_devices, labels = [], []
        for item in devices[:16] if isinstance(devices, list) else []:
            if not isinstance(item, dict) or not re.fullmatch(r'CUDA\d+', str(item.get('name', ''))):
                continue
            label = short(item.get('description') or item['name'], 140)
            total, free = item.get('total_memory_bytes'), item.get('free_memory_bytes')
            memory_valid = type(total) is int and type(free) is int and 0 <= free <= total < 2**64
            safe_devices.append({'name': short(item['name'], 30), 'description': label,
                                 'total_memory_bytes': total if memory_valid else None,
                                 'free_memory_bytes': free if memory_valid else None})
            labels.append(label + ('（顯存總量 %.1f GiB／當下可用 %.1f GiB）' % (total / 2**30, free / 2**30) if memory_valid else ''))
        status = 'ok' if available is True and labels else 'warning' if available is False else 'unknown'
        add('backend', status, '本地推論後端',
            '隨包後端可枚舉 CUDA 裝置：' + '、'.join(labels) if status == 'ok' else
            '隨包後端未確認可用 CUDA 裝置；本專案尚未驗收 CPU 推論的互動效能。',
            '裝置枚舉不代表模型可載入或顯存足夠；需另做實際對話測量。' if status == 'ok' else
            '檢查 NVIDIA 官方驅動與後端相容性；不要將 CPU 插件存在當作已通過替代方案。',
            {'cuda_available': available if type(available) is bool else None, 'devices': safe_devices})

    try:
        models = read_json(relative_file(package, 'companion/model-manifest.json'), 131072)
        if not isinstance(models, dict):
            raise ValueError('invalid model manifest')
        for key in MODEL_KEYS:
            item = models[key]
            if (not isinstance(item, dict) or type(item.get('size')) is not int or item['size'] < 1
                    or not re.fullmatch(r'[A-Za-z0-9_.-]+', item.get('name', ''))
                    or not re.fullmatch(r'[0-9a-fA-F]{64}', item.get('sha256', ''))):
                raise ValueError('invalid model entry')
            path = relative_file(package, 'models/' + item['name'])
            exists = path.is_file()
            size_ok = exists and path.stat().st_size == item['size']
            digest_ok = sha(path).lower() == item['sha256'].lower() if verify_hashes and size_ok else None
            optional = key == '4B'
            status = ('info' if optional and not exists else 'warning' if not size_ok
                      else 'blocked' if digest_ok is False else 'ok')
            add('model_' + key, status, {'8B': '日常 8B 模型', '4B': '選配 4B 模型', 'asr': '語音辨識模型'}[key],
                '選配模型未安裝。' if optional and not exists else
                '模型不存在或檔案尺寸不符。' if not size_ok else
                '模型 SHA256 不符。' if digest_ok is False else
                '尺寸與 SHA256 相符。' if digest_ok is True else '檔案尺寸相符；尚未驗證完整 SHA256。',
                '依既有模型準備流程處理；本工具不下載或改變模型偏好。' if not size_ok else '',
                {'installed': exists, 'size_matches': bool(size_ok), 'sha256_matches': digest_ok})
    except (OSError, ValueError, KeyError, TypeError):
        add('models', 'unknown', '模型清單', '模型清單不存在或格式無效，無法判定檔案完整性。',
            '重新解壓完整執行包，不使用未知清單自動下載。')

    if verify_hashes:
        try:
            files = read_json(relative_file(package, 'version.json'))['files']
            if not isinstance(files, dict) or not files or len(files) > 4096:
                raise ValueError('invalid package manifest')
            mismatch = []
            for name, expected in files.items():
                path = relative_file(package, name)
                if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
                    raise ValueError('invalid package hash')
                if not path.is_file() or sha(path).lower() != expected.lower():
                    mismatch.append(name)
            add('integrity', 'blocked' if mismatch else 'ok', '完整檔案核對',
                f'按包內 manifest 核對 {len(files)} 個檔案；不符 {len(mismatch)} 個。',
                '重新解壓完整執行包。' if mismatch else '雜湊核對本機清單，不是發行者身分驗證。',
                {'checked': len(files), 'mismatches': mismatch[:30]})
        except (OSError, ValueError, KeyError, TypeError):
            add('integrity', 'unknown', '完整檔案核對', '包內 manifest 無效或檔案無法讀取，核對未完成。')
    else:
        add('integrity', 'info', '完整檔案核對', '快速檢查不讀取數 GB 的全部權重；可加 --verify-hashes 完整核對。')
    add('manual_audio', 'info', '實體設備', '麥克風、喇叭、權限、多螢幕與真實第二台電腦仍需使用者實測。')
    statuses = {item['status'] for item in checks}
    status = ('blocked' if 'blocked' in statuses else 'unknown' if 'unknown' in statuses
              else 'attention' if 'warning' in statuses else 'ready')
    return {'schema_version': 1, 'feature': FEATURE, 'checked_at': datetime.now().astimezone().isoformat(),
            'status': status, 'checks': checks,
            'scope': {'offline': True, 'profile_accessed': False, 'model_loaded': False,
                      'microphone_opened': False, 'audio_played': False,
                      'downloads_or_installations': False, 'full_hash_check': bool(verify_hashes)},
            'limitation': '本機自檢只回報可偵測條件，不保證模型效能、完整功能或任意新機免安裝。'}


def render_text(report, compact=False):
    headings = {'ready': '未發現已知阻礙', 'attention': '有項目需要注意',
                'blocked': '部分功能目前缺少條件', 'unknown': '有項目未能確認'}
    lines = ['灰姑娘 · 執行環境自檢', headings[report['status']], '']
    for check in report['checks']:
        lines.append(f'[{STATUS_LABELS[check["status"]]}] {check["title"]}：{check["detail"]}')
        if check['action'] and (not compact or check['status'] not in ('ok', 'info')):
            lines.append('  ' + check['action'])
    lines.extend(['', report['limitation'], '本次未開啟私人資料、載入 AI 模型、錄音或播放聲音；未下載／安裝。'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--probe', choices=PROBE_NAMES, help=argparse.SUPPRESS)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--verify-hashes', action='store_true', help='另讀全部 manifest 檔案與模型 SHA256；需較長時間。')
    parser.add_argument('--output', type=Path, help='將 JSON 寫到指定新檔，不覆寫現有檔案。')
    args = parser.parse_args(argv)
    package = args.package.resolve()
    if args.probe:
        try:
            from environment_probes import run_probe
            result = {'ok': True, 'data': run_probe(args.probe, package)}
        except Exception:
            result = {'ok': False, 'error': 'failed', 'detail': 'windows_probe_failed'}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 3
    try:
        report = evaluate(package, collect(package), args.verify_hashes)
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        if args.interactive:
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            user.MessageBoxW(None, render_text(report, compact=True), '灰姑娘 · 執行環境自檢',
                             0x40 if report['status'] == 'ready' else 0x30)
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
        return {'ready': 0, 'attention': 0, 'blocked': 2, 'unknown': 3}[report['status']]
    except Exception:
        if args.interactive and sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None, '環境檢查未完成；請確認完整解壓執行包，並使用一般桌面工作階段重試。',
                                            '灰姑娘 · 執行環境自檢', 0x30)
        elif sys.stderr is not None:
            print('Environment check could not finish; no profile was opened.', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
