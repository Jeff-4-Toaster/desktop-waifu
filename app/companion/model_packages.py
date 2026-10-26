"""M1 experimental model inventory. Offline and independent of private profiles.

This catalog does not extend the production model selector. Resource estimates
and a small benchmark never certify a model/device combination for daily use.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

CATALOG = Path(__file__).with_name('model-packages.json')
GIB = 1024 ** 3
STATUS_LABELS = {'missing_model': '尚未安裝', 'invalid_model': '檔案不符',
                 'insufficient': '資源不足', 'unknown': '無法確認', 'trial': '建議試用'}


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def positive_integer(value):
    return type(value) is int and 0 < value < 2 ** 63


def backend_fingerprint(executable):
    """Use the whole executable/DLL bundle as the evidence identity.

    sha256 is the compatibility alias for bundle_sha256, never EXE-only.
    The JSON encoding matches the isolated benchmark's bundle calculation.
    """
    executable = Path(executable)
    files = {path.name: sha256(path) for path in sorted(executable.parent.iterdir())
             if path.is_file() and path.suffix.lower() in ('.exe', '.dll')}
    if executable.name not in files:
        raise FileNotFoundError('Backend executable missing')
    bundle = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {'sha256': bundle, 'bundle_sha256': bundle,
            'executable_sha256': files[executable.name], 'bundle_files': files}


def load_catalog(path=None):
    path = Path(path) if path is not None else CATALOG
    if path.stat().st_size > 131072:
        raise ValueError('Model catalog is too large')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported model catalog')
    packages = data.get('packages')
    if not isinstance(packages, dict) or not 1 <= len(packages) <= 32:
        raise ValueError('Invalid model packages')
    for key, item in packages.items():
        if not isinstance(key, str) or not re.fullmatch(r'[0-9]{1,3}B', key) or not isinstance(item, dict):
            raise ValueError('Invalid model identity')
        name = item.get('name')
        if not isinstance(name, str) or name != f'Qwen3-{key}-Q4_K_M.gguf':
            raise ValueError('Model filename must be a known basename')
        if not positive_integer(item.get('size')) or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))):
            raise ValueError('Invalid model size or hash')
        revision = item.get('revision')
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise ValueError('Model revision must be pinned')
        expected_url = f'https://huggingface.co/Qwen/Qwen3-{key}-GGUF/resolve/{revision}/{name}'
        if item.get('url') != expected_url or urlsplit(item['url']).hostname != 'huggingface.co':
            raise ValueError('Model URL must use its official pinned revision')
        if item.get('license') != 'Apache-2.0' or item.get('architecture') != 'qwen3' or item.get('quantization') != 'Q4_K_M':
            raise ValueError('Unsupported license or model architecture')
        if item.get('backend') != {'build': 'b11669', 'runtime': 'cuda-12.4', 'architecture': 'qwen3',
                                   'template': 'gguf-jinja-enable-thinking-false'}:
            raise ValueError('Unsupported experimental backend')
        for field in ('trial_minimum', 'recommended'):
            bounds = item.get(field)
            if not isinstance(bounds, dict) or not bounds or any(not positive_integer(v) for v in bounds.values()):
                raise ValueError('Invalid resource estimates')
        if not {'ram_total_bytes', 'ram_available_bytes', 'vram_reserve_bytes'} <= item['trial_minimum'].keys():
            raise ValueError('Missing resource guard')
        contexts = item.get('trial_context_tokens')
        if not isinstance(contexts, list) or not contexts or any(type(v) is not int or v not in (4096, 8192, 16384) for v in contexts):
            raise ValueError('Invalid experimental contexts')
        if type(item.get('trial_max_reply_tokens')) is not int or not 1 <= item['trial_max_reply_tokens'] <= 512:
            raise ValueError('Invalid experimental reply limit')
        if item.get('release_enabled') is not False:
            raise ValueError('Experimental inventory cannot enable a production model')
    return packages


def inspect_model(package, path, verify_hashes=False):
    """A size-only observation is explicitly unverified."""
    path = Path(path)
    result = {'name': package['name'], 'expected_size': package['size'],
              'expected_sha256': package['sha256'], 'sha256': None, 'hash_checked': False}
    try:
        if not path.is_file():
            return {**result, 'status': 'missing'}
        size = path.stat().st_size
        result['size'] = size
        if size != package['size']:
            return {**result, 'status': 'mismatch'}
        if not verify_hashes:
            return {**result, 'status': 'unverified'}
        digest = sha256(path)
        return {**result, 'status': 'verified' if digest == package['sha256'] else 'mismatch',
                'sha256': digest, 'hash_checked': True}
    except OSError:
        return {**result, 'status': 'unreadable'}


def verify_model(package, path):
    return inspect_model(package, path, verify_hashes=True)


def model_file(directory, package):
    """Resolve under the explicit model directory, refusing escaping symlinks."""
    directory = Path(directory).resolve()
    path = directory / package['name']
    if not path.resolve().is_relative_to(directory):
        raise ValueError('Model path escapes the selected directory')
    return path


def assess_device(package, observations, installation=None, evidence=None):
    observations = observations if isinstance(observations, dict) else {}
    reasons, unknown, cautions = [], [], []
    def payload(name):
        probe = observations.get(name, {})
        return probe.get('data', {}) if isinstance(probe, dict) and probe.get('ok') is True and isinstance(probe.get('data'), dict) else {}
    system, backend = payload('system'), payload('backend')
    minimum = package['trial_minimum']
    arch = system.get('native_arch')
    if arch is None:
        unknown.append('未取得原生處理器架構')
    elif arch not in ('AMD64', 'x64', 'X64', 'x86_64'):
        reasons.append('此實驗後端限 Windows x64')
    avx2 = system.get('avx2')
    if avx2 is False:
        reasons.append('未具備本批測試要求的 AVX2')
    elif avx2 is not True:
        unknown.append('AVX2 未確認')
    for field in ('ram_total_bytes', 'ram_available_bytes'):
        value = system.get(field)
        if type(value) is not int or not 0 <= value < 2 ** 63:
            unknown.append(field + ' 未確認')
        elif value < minimum[field]:
            reasons.append(field + ' 低於本批保守試跑門檻')
    total_ram, available_ram = system.get('ram_total_bytes'), system.get('ram_available_bytes')
    if positive_integer(total_ram) and positive_integer(available_ram) and available_ram > total_ram:
        unknown.append('RAM 探測值不一致')
    cuda = backend.get('cuda_available')
    if cuda is False:
        reasons.append('本批 CUDA 後端未發現可用裝置')
    elif cuda is not True:
        unknown.append('CUDA 裝置未確認')
    devices = backend.get('devices')
    known = []
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, dict) or not str(device.get('name', '')).startswith('CUDA'):
            continue
        total, free = device.get('total_memory_bytes'), device.get('free_memory_bytes')
        if positive_integer(total) and type(free) is int and 0 <= free <= total:
            known.append(device)
    required_vram = package['size'] + minimum['vram_reserve_bytes']
    if known:
        # The benchmark is single-device. Never add unrelated GPUs' free memory.
        best = max(known, key=lambda d: d['free_memory_bytes'])
        if best['free_memory_bytes'] < required_vram:
            reasons.append('單張 GPU 當下可用顯存不足權重加試跑預留；未評估 CPU offload')
        if best['total_memory_bytes'] < package['recommended'].get('vram_total_bytes', 0):
            cautions.append('顯存低於建議設備級別；僅允許隔離短上下文試跑')
    else:
        unknown.append('單張 CUDA 裝置可用顯存未確認')
    if positive_integer(total_ram) and total_ram < package['recommended'].get('ram_total_bytes', 0):
        cautions.append('RAM 低於建議設備級別')
    context = observations.get('context_tokens', 8192)
    if type(context) is not int or context not in package['trial_context_tokens']:
        reasons.append('該上下文未列入此套件的試跑範圍')
    reply = observations.get('reply_tokens', 256)
    if type(reply) is not int:
        unknown.append('回覆預算未確認')
    elif not 1 <= reply <= package['trial_max_reply_tokens']:
        reasons.append('回覆預算超出此套件的試跑範圍')
    elif type(context) is int and reply + 256 >= context:
        reasons.append('上下文不足以保留回覆與安全餘裕')
    identity = observations.get('backend_identity', {})
    if not isinstance(identity, dict) or identity.get('build') != package['backend']['build']:
        unknown.append('後端 build 未核對')
    matched = (isinstance(evidence, dict) and evidence.get('status') == 'passed'
               and isinstance(identity, dict) and bool(re.fullmatch(r'[a-f0-9]{64}', str(identity.get('sha256', ''))))
               and bool(re.fullmatch(r'[a-f0-9]{64}', str(observations.get('device_fingerprint', ''))))
               and all(evidence.get(k) == v for k, v in {
                   'model_sha256': package['sha256'], 'backend_sha256': identity.get('sha256'),
                   'device_fingerprint': observations.get('device_fingerprint'),
                   'context_tokens': context, 'max_tokens': observations.get('reply_tokens')}.items())
               and type(evidence.get('context_tokens')) is int and type(evidence.get('max_tokens')) is int
               and evidence['max_tokens'] > 0)
    installed = installation.get('status') if isinstance(installation, dict) else None
    status = ('missing_model' if installed == 'missing' else
              'invalid_model' if installed in ('mismatch', 'unreadable') else
              'insufficient' if reasons else 'unknown' if unknown or installed not in ('verified', 'unverified') else 'trial')
    return {'status': status, 'label': STATUS_LABELS[status], 'reasons': reasons, 'unknown': unknown,
            'cautions': cautions, 'required_free_vram_bytes_estimate': required_vram,
            'model_integrity': installed, 'benchmark_evidence_matches': bool(matched),
            'release_enabled': False, 'limitation': '容量門檻僅供試跑；小型 benchmark 不代表日常品質、長文或新機驗收。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True, help='明確指定可攜執行包；只執行既有 system/backend 探針')
    parser.add_argument('--models', type=Path, required=True, help='明確指定模型目錄')
    parser.add_argument('--context', type=int, choices=(4096, 8192, 16384), default=8192)
    parser.add_argument('--verify-hashes', action='store_true')
    parser.add_argument('--report', type=Path, help='另存 JSON，不覆寫既有檔案')
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error('報告已存在，請選擇新檔名')
    # Deferred import leaves pure catalog functions portable and easy to test.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from environment_check import run_child_probe
    package = args.package.resolve()
    with ThreadPoolExecutor(max_workers=2) as pool:
        observations = dict(zip(('system', 'backend'), pool.map(lambda name: run_child_probe(name, package), ('system', 'backend'))))
    backend = package / 'backend/llama/llama-server.exe'
    try:
        from environment_probes import _execute
        code, output = _execute(backend, ['--version'])
        build = 'b11669' if code == 0 and re.search(r'(?<!\d)11669(?!\d)', output) else None
        observations['backend_identity'] = {'build': build, **backend_fingerprint(backend)}
    except (OSError, ValueError, RuntimeError, TimeoutError):
        observations['backend_identity'] = {}
    observations['context_tokens'] = args.context
    inventory = {}
    for key, item in load_catalog().items():
        installation = inspect_model(item, model_file(args.models, item), args.verify_hashes)
        inventory[key] = {'file': installation, 'assessment': assess_device(item, observations, installation)}
    report = {'schema_version': 1, 'feature': 'model-packages-m1-20261024',
              'generated_at': datetime.now().astimezone().isoformat(), 'observations': observations,
              'packages': inventory, 'scope': {'profile_accessed': False, 'model_loaded': False,
              'downloaded': False, 'settings_changed': False, 'full_hash_check': args.verify_hashes}}
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as stream:
            stream.write(encoded)
    print(encoded)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
