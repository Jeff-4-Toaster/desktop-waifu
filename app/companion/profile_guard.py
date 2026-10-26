"""Windows profile lifetime leases shared by desktop/service and offline transfer.

Use the companion data directory, not the profile root. The lock file is permanent:
deleting it would allow an older open handle and a new file to be locked separately.
"""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import urllib.request
import urllib.error


class ProfileBusyError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # The loopback token belongs only to the original status endpoint. Never
        # forward it even to a second loopback port or a same-origin redirect.
        return None


if os.name == 'nt':
    _kernel = c.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    _kernel.CreateFileW.restype = w.HANDLE
    _kernel.CloseHandle.argtypes = [w.HANDLE]
    _kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    _kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    _invalid = c.c_void_p(-1).value


class ProfileLease:
    """Hold a shared runtime lease or exclusive offline read/write lease."""
    def __init__(self, data_dir, exclusive=False):
        self.handle = None
        self.data_dir = Path(data_dir).resolve()
        if os.name != 'nt':
            raise RuntimeError('Profile transfer requires Windows profile leases.')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        access, share = (0xC0000000, 0) if exclusive else (0x80000000, 1)
        handle = _kernel.CreateFileW(str(self.data_dir / 'profile.lock'), access, share, None, 4, 0x80, None)
        if handle == _invalid:
            code = c.get_last_error()
            if code in (32, 33):
                raise ProfileBusyError('資料正在使用中或正在備份／還原。請先正常退出桌寵與伴隨服務，再重試。')
            raise OSError(code, 'Cannot acquire profile lifetime lease')
        self.handle = handle

    def close(self):
        if self.handle is not None:
            _kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


class _ProcessEntry(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD), ('heap', c.c_size_t),
                ('module', w.DWORD), ('threads', w.DWORD), ('parent', w.DWORD),
                ('priority', w.LONG), ('flags', w.DWORD), ('exe', w.WCHAR * 260)]


def _legacy_desktops():
    """Conservatively identify unguarded native instances, without command lines.

    Guarded windows advertise a protocol property. Unknown DesktopWaifu processes
    (including older versions, and a new version still starting) fail closed. This
    is a migration check; old executables cannot participate in the lifetime lock.
    """
    user = c.WinDLL('user32', use_last_error=True)
    callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    user.GetPropW.argtypes = [w.HWND, w.LPCWSTR]
    user.GetPropW.restype = w.HANDLE
    guarded = set()

    @callback
    def visit(hwnd, _):
        name = c.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        if name.value.startswith('DesktopWaifu.Cinderella.') and user.GetPropW(hwnd, 'DesktopWaifu.ProfileLease.v1'):
            pid = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, c.byref(pid))
            guarded.add(pid.value)
        return True

    if not user.EnumWindows(visit, 0):
        raise RuntimeError('Cannot inspect desktop instances; exit desktop pets before transfer.')
    _kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(_ProcessEntry)]
    _kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(_ProcessEntry)]
    snapshot = _kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == _invalid:
        raise OSError(c.get_last_error(), 'Cannot inspect desktop processes')
    legacy = []
    try:
        entry = _ProcessEntry()
        entry.size = c.sizeof(entry)
        more = _kernel.Process32FirstW(snapshot, c.byref(entry))
        if not more:
            raise OSError(c.get_last_error(), 'Cannot enumerate desktop processes')
        while more:
            if entry.exe.casefold() == 'desktopwaifu.exe' and entry.pid not in guarded:
                legacy.append(entry.pid)
            more = _kernel.Process32NextW(snapshot, c.byref(entry))
    finally:
        _kernel.CloseHandle(snapshot)
    return legacy


def ensure_profile_inactive(data_dir):
    """Additional legacy checks, called while holding the exclusive lease.

    Authenticated status proves activity. For older services without leases, an
    unavailable HTTP endpoint with a live/unknown PID cannot prove safe shutdown.
    A missing PID process or refused old two-line endpoint can prove staleness.
    No secret or command line is logged.
    """
    data_dir = Path(data_dir).resolve()
    if _legacy_desktops():
        raise ProfileBusyError('偵測到未支援資料鎖的桌寵或仍在啟動的桌寵。請先正常退出其他舊版桌寵，再重試。')
    try:
        connection = (data_dir / 'connection.txt').read_text(encoding='utf-8').splitlines()
        port = int(connection[0])
        token = connection[1]
        if not 1024 <= port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            return
    except (OSError, ValueError, IndexError, UnicodeError):
        return
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + '/api/status', headers={'X-Waifu-Token': token})
    try:
        # Explicit loopback access must not use proxies or follow redirects.
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=1.5) as response:
            status = json.loads(response.read(1024 * 1024))
        if isinstance(status, dict):
            raise ProfileBusyError('伴隨服務仍在使用這份資料。請先正常退出桌寵與伴隨服務，再重試。')
    except (OSError, ValueError) as error:
        pid = None
        try:
            value = int(connection[2])
            if 0 < value < 2 ** 32:
                pid = value
        except (ValueError, IndexError):
            pass
        if pid is not None:
            _kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            _kernel.OpenProcess.restype = w.HANDLE
            _kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
            process = _kernel.OpenProcess(0x100000, False, pid)
            if process:
                try:
                    if _kernel.WaitForSingleObject(process, 0) == 0:
                        return
                finally:
                    _kernel.CloseHandle(process)
            elif c.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID absent
                return
            # Access denied and PID reuse are conservatively treated as active.
        else:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, ConnectionRefusedError):
                return
        raise ProfileBusyError('無法確認舊版伴隨服務已退出。請先正常退出服務；若已退出，請檢查舊連線資訊後再重試。') from None
