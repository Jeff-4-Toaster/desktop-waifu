"""Read-only Windows/package probes, called in disposable child processes.

No probe reads the daily profile, loads a model, opens an inference port,
records a microphone, plays audio, or downloads/installs dependencies. A
successful probe returns observations, not a promise of end-to-end readiness.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from local_tts import parse_probe as parse_local_tts_probe

PROBES = ('system', 'graphics', 'webview', 'voices', 'backend')
_PROCESS_OUTPUT_LIMIT = 1024 * 1024


def _function(dll, name, arguments, result):
    function = getattr(dll, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _execute(executable: Path, arguments: list[str], timeout: float = 8.0) -> tuple[int, str]:
    if not executable.is_file():
        raise FileNotFoundError(f'Package component missing: {executable.name}')
    # Ambient llama arguments can select RPC devices or model URLs. Enumeration
    # is deliberately independent of those settings. Other Windows runtime
    # variables, including driver selection, remain as they are on this machine.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('LLAMA_', 'GGML_RPC_'))}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    error_mode = _function(kernel, 'SetErrorMode', [wintypes.UINT], wintypes.UINT)
    previous_mode = error_mode(0x0001 | 0x0002 | 0x8000)
    try:
        process = subprocess.Popen(
            [str(executable), *arguments], cwd=executable.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        error_mode(previous_mode)
    output = bytearray()
    overflow = threading.Event()

    def drain():
        try:
            while block := process.stdout.read(65536):
                if len(output) + len(block) > _PROCESS_OUTPUT_LIMIT:
                    overflow.set()
                    process.kill()
                    break
                output.extend(block)
        finally:
            process.stdout.close()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise TimeoutError(f'{executable.name} timed out') from None
    finally:
        reader.join(timeout=5)
    if reader.is_alive():
        raise RuntimeError(f'{executable.name} did not close its diagnostic output')
    if overflow.is_set():
        raise RuntimeError(f'{executable.name} exceeded the diagnostic output limit')
    return process.returncode, bytes(output).decode('utf-8', errors='replace')


def _system(package: Path) -> dict:
    class SystemInfo(ctypes.Structure):
        _fields_ = [('architecture', wintypes.WORD), ('reserved', wintypes.WORD),
                    ('page_size', wintypes.DWORD), ('minimum_address', ctypes.c_void_p),
                    ('maximum_address', ctypes.c_void_p), ('processor_mask', ctypes.c_size_t),
                    ('processor_count', wintypes.DWORD), ('processor_type', wintypes.DWORD),
                    ('allocation_granularity', wintypes.DWORD), ('processor_level', wintypes.WORD),
                    ('processor_revision', wintypes.WORD)]

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('memory_load', wintypes.DWORD),
                    ('total_physical', ctypes.c_ulonglong), ('available_physical', ctypes.c_ulonglong),
                    ('total_pagefile', ctypes.c_ulonglong), ('available_pagefile', ctypes.c_ulonglong),
                    ('total_virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong),
                    ('available_extended_virtual', ctypes.c_ulonglong)]

    class VersionInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('major', wintypes.DWORD),
                    ('minor', wintypes.DWORD), ('build', wintypes.DWORD),
                    ('platform', wintypes.DWORD), ('service_pack', wintypes.WCHAR * 128)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    info = SystemInfo()
    _function(kernel, 'GetNativeSystemInfo', [ctypes.POINTER(SystemInfo)], None)(ctypes.byref(info))
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not _function(kernel, 'GlobalMemoryStatusEx', [ctypes.POINTER(MemoryStatus)], wintypes.BOOL)(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Unlike GetVersionEx, RtlGetVersion reports the actual version independently
    # of the Python executable's supportedOS application-manifest entries.
    version = VersionInfo()
    version.size = ctypes.sizeof(version)
    ntdll = ctypes.WinDLL('ntdll')
    status = _function(ntdll, 'RtlGetVersion', [ctypes.POINTER(VersionInfo)], ctypes.c_long)(ctypes.byref(version))
    if status < 0:
        raise RuntimeError(f'Windows version enumeration failed: {status & 0xffffffff}')
    feature = _function(kernel, 'IsProcessorFeaturePresent', [wintypes.DWORD], wintypes.BOOL)
    # Official PF constants. Newer flags are only documented on Windows 10
    # build 19041+. On older systems a zero is unknown, not proof of absence.
    modern = version.major >= 10 and version.build >= 19041
    features = {name: bool(feature(code)) if modern or code < 36 else None
                for name, code in (('sse2', 10), ('sse3', 13), ('ssse3', 36),
                                   ('sse4_1', 37), ('sse4_2', 38), ('xsave', 17),
                                   ('avx', 39), ('avx2', 40))}
    return {
        'native_arch': {0: 'x86', 9: 'AMD64', 12: 'ARM64'}.get(info.architecture, f'unknown:{info.architecture}'),
        'process_bits': ctypes.sizeof(ctypes.c_void_p) * 8,
        'windows_major': version.major, 'windows_minor': version.minor,
        'windows_build': version.build,
        'ram_total_bytes': int(memory.total_physical),
        'ram_available_bytes': int(memory.available_physical),
        'logical_processors': int(info.processor_count),
        'avx': features['avx'], 'avx2': features['avx2'],
        'cpu_features': features,
        'cpu_feature_detection': 'IsProcessorFeaturePresent',
    }


def _graphics(package: Path) -> dict:
    library = package / 'SDL2.dll'
    if not library.is_file():
        raise FileNotFoundError('Package component missing: SDL2.dll')
    # Only the package's SDL is used. The renderer SDK and character model are
    # never loaded. SDL/OpenGL work happens inside this disposable process.
    sdl = ctypes.CDLL(str(library), winmode=0x00000100 | 0x00001000)
    _function(sdl, 'SDL_SetMainReady', [], None)()
    init = _function(sdl, 'SDL_Init', [wintypes.DWORD], ctypes.c_int)
    error = _function(sdl, 'SDL_GetError', [], ctypes.c_char_p)
    attribute = _function(sdl, 'SDL_GL_SetAttribute', [ctypes.c_int, ctypes.c_int], ctypes.c_int)
    get_attribute = _function(sdl, 'SDL_GL_GetAttribute', [ctypes.c_int, ctypes.POINTER(ctypes.c_int)], ctypes.c_int)
    create_window = _function(sdl, 'SDL_CreateWindow', [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.DWORD], ctypes.c_void_p)
    create_context = _function(sdl, 'SDL_GL_CreateContext', [ctypes.c_void_p], ctypes.c_void_p)
    make_current = _function(sdl, 'SDL_GL_MakeCurrent', [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
    delete_context = _function(sdl, 'SDL_GL_DeleteContext', [ctypes.c_void_p], None)
    destroy_window = _function(sdl, 'SDL_DestroyWindow', [ctypes.c_void_p], None)
    quit_sdl = _function(sdl, 'SDL_Quit', [], None)
    get_proc = _function(sdl, 'SDL_GL_GetProcAddress', [ctypes.c_char_p], ctypes.c_void_p)

    def reason() -> str:
        return (error() or b'No SDL diagnostic').decode('utf-8', errors='replace')[:1024]

    def unavailable(detail: str) -> dict:
        return {'available': False, 'version': '', 'vendor': '', 'renderer': '', 'reason': detail}

    window = context = None
    try:
        if init(0x20) != 0:  # SDL_INIT_VIDEO
            raise RuntimeError('SDL video initialization unavailable: ' + reason())
        for setting, value in ((17, 3), (18, 3), (21, 1)):  # OpenGL 3.3, core profile
            if attribute(setting, value) != 0:
                return unavailable('OpenGL context attribute rejected: ' + reason())
        window = create_window(b'DesktopWaifu environment probe', 0x1FFF0000, 0x1FFF0000,
                               16, 16, 0x2 | 0x8)  # OPENGL | HIDDEN
        if not window:
            raise RuntimeError('Hidden OpenGL window unavailable: ' + reason())
        context = create_context(window)
        if not context:
            return unavailable('OpenGL 3.3 core context creation failed: ' + reason())
        if make_current(window, context) != 0:
            raise RuntimeError('OpenGL context could not become current: ' + reason())
        address = get_proc(b'glGetString')
        if not address:
            raise RuntimeError('OpenGL context did not expose glGetString')
        get_string = ctypes.WINFUNCTYPE(ctypes.c_char_p, wintypes.UINT)(address)
        observed = {}
        for name, value in (('version', 0x1F02), ('vendor', 0x1F00), ('renderer', 0x1F01)):
            raw = get_string(value)
            if not raw:
                raise RuntimeError(f'OpenGL did not return {name}')
            observed[name] = raw.decode('utf-8', errors='replace')[:1024]
        major, minor, profile = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if any(get_attribute(setting, ctypes.byref(value)) != 0
               for setting, value in ((17, major), (18, minor), (21, profile))):
            raise RuntimeError('OpenGL context attributes could not be verified: ' + reason())
        return {'available': (major.value, minor.value) >= (3, 3) and bool(profile.value & 1),
                **observed, 'major': major.value, 'minor': minor.value, 'core_profile': bool(profile.value & 1)}
    finally:
        if context:
            make_current(window, None)
            delete_context(context)
        if window:
            destroy_window(window)
        quit_sdl()


def parse_webview_payload(data: dict) -> dict:
    fields = ('available', 'version', 'hresult', 'source', 'fixed_present', 'error',
              'fixed_relative', 'env_overrides_ignored')
    if not isinstance(data, dict) or not set(fields) <= data.keys():
        raise ValueError('WebView runtime probe contract is incomplete')
    if any(type(data[key]) is not bool for key in ('available', 'fixed_present', 'env_overrides_ignored')):
        raise ValueError('Invalid WebView flags')
    if type(data['hresult']) is not int or not 0 <= data['hresult'] < 2**32:
        raise ValueError('Invalid WebView result')
    source, error, version = data['source'], data['error'], data['version']
    if source not in ('fixed', 'installed', 'none') or error not in ('none', 'invalid_bundle', 'policy_conflict', 'version_unavailable'):
        raise ValueError('Invalid WebView source or error category')
    if not isinstance(version, str) or len(version) > 128:
        raise ValueError('Invalid WebView version')
    fixed = source == 'fixed'
    if data['fixed_present'] != fixed or data['fixed_relative'] != ('runtime/webview2' if fixed else ''):
        raise ValueError('Inconsistent WebView bundle selection')
    if data['available']:
        pattern = r'[0-9]+(?:\.[0-9]+){3}' + ('' if fixed else r'(?: (?:beta|dev|canary))?')
        if source == 'none' or error != 'none' or data['hresult'] != 0 or not re.fullmatch(pattern, version, re.I):
            raise ValueError('Inconsistent available WebView runtime')
    elif version or error == 'none' or data['hresult'] == 0 or source == 'installed':
        raise ValueError('Inconsistent unavailable WebView runtime')
    return {key: data[key] for key in fields}


def _webview(package: Path) -> dict:
    code, output = _execute(package / 'CompanionChat.exe', ['--probe-runtime'])
    if code != 0:
        raise RuntimeError(f'WebView runtime probe exited with code {code}')
    return parse_webview_payload(json.loads(output))


def _voices(package: Path) -> dict:
    voices = []
    windows_error = False
    try:
        code, output = _execute(package / 'SpeechHelper.exe', ['--list'])
        data = json.loads(output)
        if code != 0 or not isinstance(data, list) or len(data) > 512:
            raise ValueError('Invalid Windows speech enumeration')
        for voice in data:
            if not isinstance(voice, dict) or not isinstance(voice.get('name'), str) or not isinstance(voice.get('language'), str):
                raise ValueError('Invalid Windows voice')
            # Registry token IDs never enter the public report.
            voices.append({'name': voice['name'][:512], 'language': voice['language'][:256]})
    except (OSError, RuntimeError, ValueError, TimeoutError):
        voices = [];windows_error = True
    local = {'available': False, 'backend': 'local-melo', 'model_loaded': False, 'playback': False}
    helper = package / 'backend/tts/LocalTtsHelper.exe'
    local_error = False
    if helper.exists():
        try:
            code, output = _execute(helper, ['--probe'])
            if code != 0:raise ValueError('Local helper failed')
            local = parse_local_tts_probe(json.loads(output))
        except (OSError, RuntimeError, ValueError, TimeoutError):
            local_error = True
    return {'voices': voices, 'windows_probe_error': windows_error,
            'local': local, 'local_probe_error': local_error}


def _backend(package: Path) -> dict:
    code, output = _execute(package / 'backend' / 'llama' / 'llama-server.exe', ['--list-devices'], timeout=10)
    if code != 0:
        raise RuntimeError(f'llama.cpp device enumeration exited with code {code}')
    # Parse the supplied b11669 CLI's list format. An unrecognized listing is
    # unknown rather than an invented CPU/CUDA success.
    cleaned = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    if 'Available devices:' not in cleaned:
        raise ValueError('llama.cpp did not return a recognized device listing')
    devices = []
    for line in cleaned.split('Available devices:', 1)[1].splitlines():
        match = re.fullmatch(r'\s*([^\s:]+):\s*(.+?)\s*\((\d+) MiB, (\d+) MiB free\)\s*', line)
        if match:
            name, description, total, free = match.groups()
            devices.append({'name': name[:128], 'description': description[:512],
                            'total_memory_bytes': int(total) * 1024 * 1024,
                            'free_memory_bytes': int(free) * 1024 * 1024})
        elif line.strip():
            # Future backend formats remain visible without claiming that their
            # memory or acceleration capabilities were parsed successfully.
            devices.append({'detail': line.strip()[:1024]})
    detail = cleaned.replace(str(package), '<package>')
    detail = ''.join(value for value in detail if value >= ' ' or value in '\n\t')[:16384]
    cuda = any(item.get('name', '').startswith('CUDA') for item in devices)
    cuda_available = True if cuda else None if any('detail' in item for item in devices) else False
    return {'cuda_available': cuda_available,
            'devices': devices, 'detail': detail}


def run_probe(name: str, package: Path) -> dict:
    if name not in PROBES:
        raise ValueError('Unknown environment probe')
    if os.name != 'nt':
        raise OSError('Environment probes require Windows')
    return globals()['_' + name](Path(package).resolve())
