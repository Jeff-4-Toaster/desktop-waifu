"""Small, privacy-safe contract for the optional bundled CPU speech helper."""
import json
from pathlib import Path
import subprocess
from engine import hidden


def parse_probe(value):
    if not isinstance(value, dict) or value.get('backend') != 'local-melo':
        raise ValueError('Unknown local speech helper')
    for key in ('available', 'model_loaded', 'playback'):
        if type(value.get(key)) is not bool:
            raise ValueError('Invalid local speech probe')
    if value['model_loaded'] or value['playback']:
        raise ValueError('Speech probe must not synthesize or play audio')
    available = value['available']
    if available and (value.get('runtime_version') != '2.0.0' or
                      value.get('model') != 'melo-zh-en-female-int8' or
                      value.get('language') != 'zh-CN' or
                      value.get('provider') != 'cpu' or
                      value.get('supports_traditional_text') is not True or
                      value.get('model_present') is not True):
        raise ValueError('Inconsistent local speech identity')
    return {'available': available, 'backend': 'local-melo',
            'language': 'zh-CN', 'supports_traditional_text': True,
            'model_loaded': False, 'playback': False}


def probe(executable):
    path = Path(executable)
    if not path.is_file():
        return {'available': False, 'backend': 'local-melo', 'language': 'zh-CN',
                'supports_traditional_text': True, 'model_loaded': False, 'playback': False}
    result = subprocess.run([str(path), '--probe'], cwd=path.parent,
                            capture_output=True, timeout=10, **hidden())
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError('Local speech probe failed')
    return parse_probe(json.loads(result.stdout.decode('utf-8-sig')))
