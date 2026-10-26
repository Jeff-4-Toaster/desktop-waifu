"""Offline plan by default; explicitly prepare an optional Fixed WebView2 runtime.

The initial CAB has no Microsoft-published SHA256. HTTPS plus valid Microsoft
Authenticode signatures bootstrap a separately reviewed computed digest; this
tool never edits its lock or labels a computed digest as an upstream hash.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
LOCK = Path(__file__).with_name('fixed-webview-lock.json')
GIB = 1024 ** 3
MAX_TREE = 2 * GIB
MAX_CAB = GIB
MAX_FILES = 10000
SEGMENT = 32 * 1024 ** 2


class PreparationError(Exception):
    pass


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_lock():
    value = json.loads(LOCK.read_text(encoding='utf-8-sig'))
    assert value['schema_version'] == 1 and value['architecture'] == 'x64'
    assert re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', value['version'])
    assert value['filename'] == 'Microsoft.WebView2.FixedVersionRuntime.' + value['version'] + '.x64.cab'
    prefix = 'https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/'
    assert value['url'].startswith(prefix) and value['url'].endswith('/' + value['filename'])
    assert type(value['size']) is int and 0 < value['size'] < MAX_CAB
    for key in ('official_sha256', 'reviewed_sha256'):
        assert value[key] is None or re.fullmatch(r'[a-f0-9]{64}', value[key])
    assert value['license_url'] == 'https://developer.microsoft.com/microsoft-edge/api/eula/webview2?locale=en-us&fixed=true'
    assert value['license_file'] == 'tools/licenses/WebView2-Fixed-LICENSE.en-US.html'
    plain_path(ROOT / value['license_file'])
    assert sha(ROOT / value['license_file']) == value['license_sha256']
    for item in value['product_notice_files']:
        assert item['source'].startswith('tools/licenses/')
        plain_path(ROOT / item['source'])
        assert member_name(item['filename']) == Path(item['filename']).name
        assert sha(ROOT / item['source']) == item['sha256']
    return value


def plain_path(path, boundary=ROOT):
    path, boundary = Path(path), Path(boundary).resolve()
    if not path.resolve().is_relative_to(boundary):
        raise PreparationError('Path escapes its allowed workspace directory')
    current = path
    while current != boundary:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise PreparationError('Reparse points are not accepted')
        if current.parent == current:
            raise PreparationError('Invalid path boundary')
        current = current.parent


def member_name(raw):
    name = raw.replace('\\', '/')
    if not name or name.startswith('/') or ':' in name:
        raise PreparationError('CAB contains an absolute or empty filename')
    parts = name.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part != part.rstrip(' .')
                or re.search(r'[<>"|?*\x00-\x1f\x7f]', part)
                or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise PreparationError('CAB contains an unsafe filename')
    return name


def cab_members(path):
    """Read CFHEADER/CFFOLDER/CFFILE metadata before invoking native expand."""
    size = Path(path).stat().st_size
    if not 36 <= size <= MAX_CAB:
        raise PreparationError('CAB size exceeds bounds')
    with Path(path).open('rb') as stream:
        header = stream.read(36)
        signature, r1, cabinet_size, r2, offset, r3, minor, major, folders, files, flags, set_id, cabinet = struct.unpack('<4sIIIIIBBHHHHH', header)
        if signature != b'MSCF' or (r1, r2, r3) != (0, 0, 0) or not 36 <= cabinet_size <= size:
            raise PreparationError('CAB header or declared size is invalid')
        if major != 1 or minor != 3 or flags & ~4 or cabinet != 0:
            raise PreparationError('Multicab or unsupported CAB headers are refused')
        if not 1 <= folders <= 1024 or not 1 <= files <= MAX_FILES:
            raise PreparationError('CAB file/folder count exceeds bounds')
        folder_reserved, data_reserved, reserved_bytes = 0, 0, b''
        overlay = None
        if flags & 4:
            reserve = stream.read(4)
            if len(reserve) != 4:
                raise PreparationError('Truncated CAB reserve header')
            header_reserved, folder_reserved, data_reserved = struct.unpack('<HBB', reserve)
            reserved_bytes = stream.read(header_reserved)
            if len(reserved_bytes) != header_reserved or stream.tell() > cabinet_size:
                raise PreparationError('Truncated CAB header reserve')
        if cabinet_size != size:
            # Authenticode CABs declare their appended PKCS#7 envelope in the
            # 20-byte signing reserve. No arbitrary trailing data is accepted.
            if len(reserved_bytes) != 20 or folder_reserved or data_reserved:
                raise PreparationError('CAB trailing bytes lack a recognized signing reserve')
            marker, signature_offset, signature_size, unused1, unused2 = struct.unpack('<IIIII', reserved_bytes)
            if (marker != 0x00100000 or unused1 or unused2 or signature_offset != cabinet_size
                    or not 32 <= signature_size <= 4 * 1024 * 1024 or signature_offset + signature_size != size):
                raise PreparationError('CAB signature overlay bounds are invalid')
            position = stream.tell()
            stream.seek(signature_offset)
            envelope = stream.read(signature_size)
            stream.seek(position)
            if len(envelope) != signature_size or envelope[0] != 0x30:
                raise PreparationError('CAB signature is not a bounded DER sequence')
            first = envelope[1]
            if first < 128:
                count, length, prefix_size = 0, first, 2
            else:
                count = first & 127
                if not 1 <= count <= 4 or envelope[2] == 0:
                    raise PreparationError('CAB signature DER length is unsupported')
                length = int.from_bytes(envelope[2:2 + count], 'big')
                prefix_size = 2 + count
                if length < 128:
                    raise PreparationError('CAB signature DER length is not canonical')
            der_size = prefix_size + length
            signed_data_oid = b'\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02'
            if (not prefix_size < der_size <= signature_size or signature_size - der_size > 7
                    or any(envelope[der_size:]) or envelope[prefix_size:prefix_size + 11] != signed_data_oid):
                raise PreparationError('CAB signature envelope or padding is invalid')
            overlay = {'offset': signature_offset, 'size': signature_size, 'der_bytes': der_size,
                       'kind': 'declared_pkcs7_signed_data_requires_windows_authenticode'}
        folder_data = []
        for _ in range(folders):
            if stream.tell() + 8 + folder_reserved > cabinet_size:
                raise PreparationError('CAB folder table exceeds the cabinet boundary')
            raw = stream.read(8)
            if len(raw) != 8 or len(stream.read(folder_reserved)) != folder_reserved:
                raise PreparationError('Truncated CAB folder table')
            start, blocks, compression = struct.unpack('<IHH', raw)
            if not blocks or compression & 15 not in (0, 1, 3) or start >= cabinet_size:
                raise PreparationError('Unsupported CAB compression or folder offset')
            folder_data.append(start)
        if offset < stream.tell() or offset >= cabinet_size:
            raise PreparationError('CAB file table offset is invalid')
        stream.seek(offset)
        result, spellings, ranges = [], {}, {}
        total = 0
        for _ in range(files):
            if stream.tell() + 16 > cabinet_size:
                raise PreparationError('CAB file table exceeds the cabinet boundary')
            raw = stream.read(16)
            if len(raw) != 16:
                raise PreparationError('Truncated CAB file entry')
            length, start, folder, date, clock, attributes = struct.unpack('<IIHHHH', raw)
            if folder >= folders or attributes & ~0xE7 or length > GIB or start + length > MAX_TREE:
                raise PreparationError('Spanning/reparse/oversized CAB file is refused')
            encoded = bytearray()
            while len(encoded) <= 4096:
                if stream.tell() >= cabinet_size:
                    raise PreparationError('CAB filename exceeds the cabinet boundary')
                byte = stream.read(1)
                if not byte:
                    raise PreparationError('Truncated CAB filename')
                if byte == b'\0':
                    break
                encoded.extend(byte)
            else:
                raise PreparationError('CAB filename exceeds bounds')
            try:
                name = member_name(encoded.decode('utf-8' if attributes & 0x80 else 'cp1252'))
            except UnicodeError:
                raise PreparationError('CAB filename encoding is invalid') from None
            pieces = name.split('/')
            for index in range(1, len(pieces) + 1):
                prefix = '/'.join(pieces[:index])
                key = prefix.casefold()
                if key in spellings and spellings[key] != prefix:
                    raise PreparationError('CAB has case-aliased paths')
                spellings[key] = prefix
            total += length
            if total > MAX_TREE:
                raise PreparationError('CAB expanded total exceeds bounds')
            result.append({'name': name, 'size': length})
            ranges.setdefault(folder, []).append((start, start + length))
        names = sorted(item['name'].casefold() for item in result)
        if len(names) != len(set(names)) or any(b.startswith(a + '/') for a, b in zip(names, names[1:])):
            raise PreparationError('CAB duplicate or file/directory path alias')
        if any(start < stream.tell() for start in folder_data):
            raise PreparationError('CAB data overlaps its metadata')
        for entries in ranges.values():
            ordered = sorted(entries)
            if any(b[0] < a[1] for a, b in zip(ordered, ordered[1:])):
                raise PreparationError('CAB file offsets overlap')
        candidates = [item['name'] for item in result if Path(item['name']).name.casefold() == 'msedgewebview2.exe']
        if len(candidates) != 1:
            raise PreparationError('CAB must contain exactly one WebView2 executable')
        prefix = candidates[0].rsplit('/', 1)[0] + '/' if '/' in candidates[0] else ''
        if any(not item['name'].startswith(prefix) for item in result):
            raise PreparationError('CAB contains entries outside its runtime root')
        return {'files': result, 'runtime_prefix': prefix, 'expanded_bytes': total,
                'cabinet_bytes': cabinet_size, 'signature_overlay': overlay}


@contextmanager
def exclusive(path):
    try:
        with path.open('x', encoding='ascii') as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        raise PreparationError('Preparation lock already exists; confirm its owner exited before manual recovery') from None
    try:
        yield
    finally:
        path.unlink()


class MicrosoftRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://msedge.sf.dl.delivery.mp.microsoft.com/'):
            raise PreparationError('Download redirect left the pinned Microsoft HTTPS host')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_cab(lock, destination):
    expected_digest = lock['official_sha256'] or lock['reviewed_sha256']
    if destination.exists():
        if destination.stat().st_size != lock['size']:
            raise PreparationError('Existing CAB size differs; original file was preserved')
        digest = sha(destination)
        if expected_digest and digest != expected_digest:
            raise PreparationError('Existing CAB differs from separately reviewed digest')
        return digest
    partial = destination.with_name(destination.name + '.part')
    plain_path(partial)
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > lock['size']:
        raise PreparationError('Partial CAB exceeds expected size')
    opener = urllib.request.build_opener(MicrosoftRedirects())
    while offset < lock['size']:
        end = min(lock['size'] - 1, offset + SEGMENT - 1)
        request = urllib.request.Request(lock['url'], headers={'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity', 'User-Agent': 'DesktopWaifu-FixedWebView-M4/1.0'})
        for attempt in range(3):
            try:
                with opener.open(request, timeout=45) as response:
                    if response.status != 206 or response.headers.get('Content-Range') != f'bytes {offset}-{end}/{lock["size"]}':
                        raise PreparationError('Microsoft server did not return the exact requested CAB range')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) != end - offset + 1):
                        raise PreparationError('CAB response Content-Length differs from its exact range')
                    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        raise PreparationError('CAB response used unexpected content encoding')
                    data = response.read(end - offset + 2)
                if len(data) != end - offset + 1:
                    raise PreparationError('CAB range body length differs')
                break
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise PreparationError('CAB request returned HTTP ' + str(code) + '; contiguous partial data was retained') from None
                time.sleep(attempt + 1)
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                if attempt == 2:
                    raise PreparationError('CAB connection failed; contiguous partial data was retained') from None
                time.sleep(attempt + 1)
        with partial.open('ab') as stream:
            stream.write(data)
        offset += len(data)
        print(f'CAB {offset}/{lock["size"]} bytes', file=sys.stderr, flush=True)
    digest = sha(partial)
    if expected_digest and digest != expected_digest:
        raise PreparationError('Downloaded CAB differs from separately reviewed SHA256; partial preserved')
    os.rename(partial, destination)
    return digest


def execute(command, timeout):
    environment = {key: value for key, value in os.environ.items() if key.upper() != 'PSMODULEPATH'}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output, overflow = bytearray(), threading.Event()
    def drain():
        while block := process.stdout.read(65536):
            if len(output) + len(block) > 1024 * 1024:
                overflow.set();process.kill();break
            output.extend(block)
        process.stdout.close()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=5)
        raise PreparationError('Owned extraction/signature process exceeded its deadline') from None
    finally:
        reader.join(timeout=5)
    if overflow.is_set() or reader.is_alive() or process.returncode != 0:
        raise PreparationError('Owned extraction/signature process failed or exceeded output bounds')


def verify_extracted(directory, members):
    actual = {}
    for path in directory.rglob('*'):
        plain_path(path, directory)
        if path.is_file():
            actual[path.relative_to(directory).as_posix()] = path.stat().st_size
    expected = {item['name']: item['size'] for item in members['files']}
    if actual != expected:
        raise PreparationError('Expanded CAB tree has missing, extra, renamed or wrong-size files')


def authenticode(files, stage, label):
    manifest, script, output = stage / (label + '-signature-input.json'), stage / (label + '-check-signatures.ps1'), stage / (label + '-signatures.json')
    manifest.write_text(json.dumps(files, ensure_ascii=False), encoding='utf-8')
    script.write_text(r'''param([string]$Manifest,[string]$Output)
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
$results=@(foreach($item in (Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json)) {
 $signature=Get-AuthenticodeSignature -LiteralPath $item.path
 [pscustomobject]@{name=$item.name;status=[string]$signature.Status;subject=$signature.SignerCertificate.Subject;thumbprint=$signature.SignerCertificate.Thumbprint;product_version=(Get-Item -LiteralPath $item.path).VersionInfo.ProductVersion}
})
[IO.File]::WriteAllText($Output,(ConvertTo-Json -InputObject $results -Depth 5),[Text.UTF8Encoding]::new($false))
''', encoding='utf-8-sig')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    execute([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script), '-Manifest', str(manifest), '-Output', str(output)], 240)
    result = json.loads(output.read_text(encoding='utf-8-sig'))
    if not isinstance(result, list) or len(result) != len(files):
        raise PreparationError('Signature enumeration returned an invalid result')
    for item in result:
        if item.get('status') != 'Valid' or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', item.get('subject') or ''):
            raise PreparationError('A runtime executable/DLL lacks a valid Microsoft signature: ' + item['name'])
    return result


def signatures(payload, stage, version):
    files = [{'name': path.relative_to(payload).as_posix(), 'path': str(path)}
             for path in payload.rglob('*') if path.suffix.lower() in ('.exe', '.dll')]
    result = authenticode(files, stage, 'runtime')
    executable = next((item for item in result if item['name'] == 'msedgewebview2.exe'), None)
    if not executable or not str(executable.get('product_version', '')).startswith(version):
        raise PreparationError('Signed WebView2 executable product version differs from the pin')
    with (payload / 'msedgewebview2.exe').open('rb') as stream:
        dos = stream.read(64)
        if len(dos) < 64 or dos[:2] != b'MZ':
            raise PreparationError('Runtime executable lacks a PE header')
        offset = struct.unpack_from('<I', dos, 60)[0]
        if offset > 16 * 1024 * 1024:
            raise PreparationError('Runtime PE offset exceeds bounds')
        stream.seek(offset)
        if stream.read(6) != b'PE\0\0\x64\x86':
            raise PreparationError('Runtime executable is not x64')
    return result


def prepare(lock, cab, target):
    if os.name != 'nt':
        raise PreparationError('Extraction/signature preparation requires Windows; CAB parser remains portable')
    if target.exists():
        raise PreparationError('Prepared runtime directory already exists; it was not overwritten')
    members = cab_members(cab)
    stage = ROOT / 'build/companion-test' / ('fixed-webview2-prepare-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    expanded, payload = stage / 'expanded', stage / 'runtime/webview2'
    stage.mkdir(parents=True)
    cab_signature = authenticode([{'name': cab.name, 'path': str(cab)}], stage, 'cab')[0]
    expanded.mkdir();payload.mkdir(parents=True)
    expand = Path(os.environ['SystemRoot']) / 'System32/expand.exe'
    execute([str(expand), str(cab), '-F:*', str(expanded)], 180)
    verify_extracted(expanded, members)
    for item in members['files']:
        source = expanded / item['name']
        destination = payload / item['name'][len(members['runtime_prefix']):]
        plain_path(source, stage);plain_path(destination, stage)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
    signed = signatures(payload, stage, lock['version'])
    files = {path.relative_to(payload).as_posix(): sha(path) for path in sorted(payload.rglob('*')) if path.is_file()}
    original_license = ROOT / lock['license_file']
    if sha(original_license) != lock['license_sha256']:
        raise PreparationError('Original official Fixed Runtime license differs from its local pin')
    license_copy = stage / 'licenses/Microsoft-WebView2-Fixed-LICENSE.en-US.html'
    license_copy.parent.mkdir(parents=True)
    shutil.copyfile(original_license, license_copy)
    license_files = [{'path': license_copy.relative_to(stage).as_posix(), 'sha256': lock['license_sha256'],
                      'source_url': lock['license_url'], 'document_type': 'original Microsoft Fixed Runtime EULA'}]
    for item in lock['product_notice_files']:
        source = ROOT / item['source']
        if sha(source) != item['sha256']:
            raise PreparationError('Required product notice differs from its reviewed pin')
        destination = stage / 'licenses' / item['filename']
        shutil.copyfile(source, destination)
        license_files.append({'path': destination.relative_to(stage).as_posix(), 'sha256': item['sha256'],
                              'document_type': 'reviewed product notice or exact required-notice excerpt'})
    for name in files:
        if re.search(r'(?i)(?:license|licence|eula)', Path(name).name):
            source = payload / name
            if source.stat().st_size <= 4 * 1024 * 1024:
                destination = stage / 'licenses/cab' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                license_files.append({'runtime_path': name, 'path': destination.relative_to(stage).as_posix(),
                                      'sha256': files[name], 'copied_from': 'original Microsoft CAB'})
    digest = sha(cab)
    report = {'schema_version': 1, 'feature': 'fixed-webview2-m4-20261024', 'version': lock['version'], 'architecture': 'x64',
        'generated_at': datetime.now().astimezone().isoformat(), 'source_url': lock['url'], 'cab_size': cab.stat().st_size,
        'computed_cab_sha256': digest, 'official_sha256': lock['official_sha256'],
        'separately_reviewed_sha256_matched': digest == lock['reviewed_sha256'] if lock['reviewed_sha256'] else False,
        'integrity_basis': 'First HTTPS acquisition plus Microsoft Authenticode; computed hash is not an upstream-published hash.',
        'runtime_relative_path': 'runtime/webview2', 'files': files, 'expanded_bytes': members['expanded_bytes'],
        'cabinet_bytes': members['cabinet_bytes'], 'cab_signature_overlay': members['signature_overlay'],
        'cab_microsoft_signature': cab_signature, 'cab_signature_verified_before_expand': True,
        'microsoft_signatures': signed, 'license_files': license_files, 'license_url': lock['license_url'],
        'license_review_required': not lock['license_reviewed'],
        'license_review_scope': lock.get('license_review_scope'),
        'eligible_for_release': bool(lock['reviewed_sha256'] == digest and lock['license_reviewed']),
        'scope': {'system_installed': False, 'acl_changed': False, 'profile_accessed': False, 'runtime_started': False}}
    (stage / 'fixed-webview-manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    target.parent.mkdir(parents=True, exist_ok=True)
    plain_path(stage, ROOT / 'build/companion-test');plain_path(target, ROOT / '.tools')
    os.rename(stage, target)
    return report


def self_test():
    """Synthetic CAB metadata only; no network, expand, signatures or runtime."""
    checks = {}
    def cabinet(names):
        entries, offset = bytearray(), 0
        for name in names:
            entries.extend(struct.pack('<IIHHHH', 1, offset, 0, 0, 0, 0x80))
            entries.extend(name.encode('utf-8') + b'\0')
            offset += 1
        data_start = 44 + len(entries)
        size = data_start + 8 + len(names)
        header = struct.pack('<4sIIIIIBBHHHHH', b'MSCF', 0, size, 0, 44, 0, 3, 1, 1, len(names), 0, 0, 0)
        return bytearray(header + struct.pack('<IHH', data_start, 1, 0) + entries
                         + struct.pack('<IHH', 0, len(names), len(names)) + b'X' * len(names))
    root = ROOT / 'build/companion-test'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fixed-webview-parser-', dir=root) as temporary:
        directory = Path(temporary).resolve()
        assert directory.is_relative_to(root.resolve())
        path = directory / 'fixture.cab'
        valid = cabinet(['Runtime/msedgewebview2.exe', 'Runtime/msedge.dll'])
        path.write_bytes(valid)
        parsed = cab_members(path)
        assert parsed['runtime_prefix'] == 'Runtime/' and parsed['expanded_bytes'] == 2
        checks['valid_metadata_and_normalized_runtime_root'] = True
        bad_names = {
            'parent_traversal': ['Runtime/msedgewebview2.exe', 'Runtime/../outside'],
            'absolute_drive': ['Runtime/msedgewebview2.exe', 'C:/outside'],
            'unc': ['Runtime/msedgewebview2.exe', '\\\\server\\share\\outside'],
            'reserved_device': ['Runtime/msedgewebview2.exe', 'Runtime/NUL.txt'],
            'trailing_dot': ['Runtime/msedgewebview2.exe', 'Runtime/name.'],
            'control': ['Runtime/msedgewebview2.exe', 'Runtime/name\x1f'],
            'case_file_alias': ['Runtime/msedgewebview2.exe', 'Runtime/MSedgewebview2.exe'],
            'case_directory_alias': ['Runtime/msedgewebview2.exe', 'RUNTIME/a.txt'],
            'file_directory_alias': ['Runtime/msedgewebview2.exe', 'Runtime/module', 'Runtime/module/a'],
            'outside_runtime_root': ['Runtime/msedgewebview2.exe', 'Outside/a.txt'],
            'missing_runtime_executable': ['Runtime/other.exe'],
            'two_runtime_executables': ['Runtime/msedgewebview2.exe', 'Runtime/Other/msedgewebview2.exe'],
            'oversized_filename': ['Runtime/msedgewebview2.exe', 'Runtime/' + 'a' * 4097],
        }
        for label, names in bad_names.items():
            path.write_bytes(cabinet(names))
            try:
                cab_members(path)
                raise AssertionError('Accepted unsafe CAB: ' + label)
            except PreparationError:
                checks[label + '_rejected'] = True
        mutations = {
            'multicab': (30, '<H', 1), 'cabinet_index': (34, '<H', 1),
            'wrong_cab_size': (8, '<I', 36), 'file_table_outside': (16, '<I', len(valid) + 1),
            'spanned_file': (52, '<H', 0xfffe), 'reparse_attribute': (58, '<H', 0x400),
            'oversized_file': (44, '<I', GIB + 1),
        }
        for label, (offset, format, value) in mutations.items():
            changed = bytearray(valid)
            struct.pack_into(format, changed, offset, value)
            path.write_bytes(changed)
            try:
                cab_members(path)
                raise AssertionError('Accepted invalid CAB header: ' + label)
            except PreparationError:
                checks[label + '_rejected'] = True
        # A signing reserve is structural evidence only. Real preparation must
        # still validate the whole CAB through Windows before invoking expand.
        oid = b'\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02'
        der_body = oid + b'\xa0\x14' + b'\0' * 20
        der = b'\x30' + bytes([len(der_body)]) + der_body
        envelope = der + b'\0' * ((-len(der)) % 8)
        cabinet_size = len(valid) + 24
        reserve = struct.pack('<HBBIIIII', 20, 0, 0, 0x00100000, cabinet_size, len(envelope), 0, 0)
        signed = bytearray(valid[:36] + reserve + valid[36:] + envelope)
        struct.pack_into('<I', signed, 8, cabinet_size)
        struct.pack_into('<I', signed, 16, 68)
        struct.pack_into('<H', signed, 30, 4)
        struct.pack_into('<I', signed, 60, struct.unpack_from('<I', valid, 36)[0] + 24)
        path.write_bytes(signed)
        observed = cab_members(path)
        assert observed['cabinet_bytes'] == cabinet_size and observed['signature_overlay']['size'] == len(envelope)
        checks['declared_signed_overlay_structure_recognized_but_not_trusted'] = True
        overlay_bad = {'unknown_trailing_garbage': bytearray(valid + b'junk')}
        for label, at, value in (
                ('unknown_signature_marker', 40, 0), ('wrong_signature_offset', 44, cabinet_size + 1),
                ('overlay_as_cabinet_data', 60, cabinet_size)):
            changed = bytearray(signed)
            struct.pack_into('<I', changed, at, value)
            overlay_bad[label] = changed
        changed = bytearray(signed)
        changed[-1] = 1
        overlay_bad['nonzero_overlay_padding'] = changed
        for label, changed in overlay_bad.items():
            path.write_bytes(changed)
            try:
                cab_members(path)
                raise AssertionError('Accepted invalid signature overlay: ' + label)
            except PreparationError:
                checks[label + '_rejected'] = True
        expanded = directory / 'expanded'
        (expanded / 'Runtime').mkdir(parents=True)
        for item in parsed['files']:
            (expanded / item['name']).write_bytes(b'X')
        verify_extracted(expanded, parsed)
        (expanded / 'extra.txt').write_text('extra')
        try:
            verify_extracted(expanded, parsed)
            raise AssertionError('Accepted extra extracted file')
        except PreparationError:
            checks['extracted_tree_extra_file_rejected'] = True
        try:
            MicrosoftRedirects().redirect_request(None, None, 302, '', {}, 'http://example.invalid/cab')
            raise AssertionError('Accepted non-Microsoft/downgraded redirect')
        except PreparationError:
            checks['untrusted_redirect_rejected_without_request'] = True
    result = {'passed': True, 'checks': checks, 'case_count': len(checks),
              'scope': {'fixture_only': True, 'network_used': False, 'runtime_started': False,
                        'system_installed': False, 'acl_changed': False, 'profile_accessed': False},
              'source_hashes': {'tools/prepare-fixed-webview.py': sha(Path(__file__)),
                                'tools/fixed-webview-lock.json': sha(LOCK)}}
    print(json.dumps(result, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Explicitly fetch CAB and prepare a local development runtime')
    parser.add_argument('--report', type=Path, help='Write a new JSON report, never overwrite an existing report')
    parser.add_argument('--self-test', action='store_true', help='Only synthetic offline CAB parser fixtures')
    args = parser.parse_args()
    if args.self_test:
        if args.download or args.report:
            parser.error('--self-test cannot be combined with --download or --report')
        return self_test()
    try:
        lock = load_lock()
        cache = ROOT / '.tools/downloads/fixed-webview2'
        cab = cache / lock['filename']
        target = ROOT / '.tools' / ('fixed-webview2-' + lock['version'])
        for path in (cache, cab, target):
            plain_path(path)
        if args.report:
            plain_path(args.report.resolve())
            if args.report.exists():
                raise PreparationError('Report already exists')
        partial = cab.with_name(cab.name + '.part')
        plain_path(partial)
        partial_size = partial.stat().st_size if partial.exists() else 0
        free = shutil.disk_usage(ROOT).free
        needed = max(0, lock['size'] - partial_size) + MAX_TREE + 2 * GIB
        result = {'mode': 'offline_plan', 'lock': lock, 'target': str(target), 'cab_present': cab.is_file(),
                  'prepared_directory_present': target.exists(), 'partial_bytes': partial_size,
                  'free_disk_bytes': free, 'required_free_disk_bytes': needed,
                  'download_requested': args.download, 'network_used': False,
                  'scope': {'profile_accessed': False, 'system_installed': False, 'acl_changed': False}}
        if args.download:
            if free < needed:
                raise PreparationError('Insufficient free space for remaining CAB, bounded expansion and 2 GiB reserve')
            cache.mkdir(parents=True, exist_ok=True)
            with exclusive(cache / (lock['filename'] + '.prepare.lock')):
                already = cab.exists()
                digest = download_cab(lock, cab)
                result.update(mode='prepared', network_used=not already, computed_cab_sha256=digest,
                              preparation=prepare(lock, cab, target))
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open('x', encoding='utf-8') as stream:
                stream.write(encoded)
        print(encoded)
        return 0
    except (PreparationError, OSError, ValueError, AssertionError, KeyError):
        error = sys.exception()
        message = str(error) if isinstance(error, PreparationError) else 'Fixed runtime preparation failed; no existing runtime was overwritten'
        print(json.dumps({'passed': False, 'error': message}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
