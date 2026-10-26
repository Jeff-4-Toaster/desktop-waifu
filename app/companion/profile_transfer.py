"""Bounded, local-only profile snapshots. No model, token, browser or voice data.

Online callers must hold Store.lock; offline callers must hold ProfileLease.
Restore acquires its own exclusive lease and never starts the application.
"""
import datetime
from contextlib import closing
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
import zlib

FORMAT = 'DesktopWaifu.profile-backup'
FORMAT_VERSION = 1
APP_SCHEMA = 1
MAX_DB = 256 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_CAPTURE = 64 * 1024 * 1024
MAX_INI = 128 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_FILES = 2048
DB_NAME = 'companion/companion.sqlite3'
INI_NAMES = ('settings.ini', 'companion/window.ini')
TABLES = ('settings', 'messages', 'memories', 'reminders', 'events')
CONSISTENCY = ('SQLite online backup creates one consistent database snapshot; '
               'saved INI and optional captures are sampled during export. '
               'Unsaved UI settings and changes after the snapshot are not included.')
_SQL = {
    'settings': 'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    'messages': "CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,epoch INTEGER NOT NULL,created REAL NOT NULL, finish_reason TEXT NOT NULL DEFAULT '')",
    'memories': 'CREATE TABLE memories(id INTEGER PRIMARY KEY,text TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)',
    'reminders': "CREATE TABLE reminders(id INTEGER PRIMARY KEY,title TEXT NOT NULL,due REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',notified REAL,created REAL NOT NULL)",
    'events': 'CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,created REAL NOT NULL)',
}


class TransferError(ValueError):
    """Invalid or unsupported archive/profile; contains no private values."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@lru_cache(maxsize=1)
def _expected_columns():
    with closing(sqlite3.connect(':memory:')) as db:
        for sql in _SQL.values():
            db.execute(sql)
        return {name: db.execute('PRAGMA table_xinfo(' + name + ')').fetchall() for name in TABLES}


def _safe(path):
    """Check lexical ancestors before resolving, including Windows junctions."""
    path = Path(os.path.abspath(path))
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise TransferError('資料路徑含連結或重新解析點，已停止')
    return path


def _regular(path):
    path = _safe(path)
    if not path.is_file():
        raise TransferError('必要資料檔不存在或不是一般檔案')
    return path


def _name(name):
    if not isinstance(name, str) or len(name) > 220 or not name or '\\' in name:
        raise TransferError('備份路徑格式無效')
    parts = name.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part[-1:] in (' ', '.') or
                any(ord(ch) < 32 or ch in ':<>"|?*' for ch in part) or
                re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise TransferError('備份包含不安全路徑')
    if name in (*INI_NAMES, DB_NAME):
        return MAX_DB if name == DB_NAME else MAX_INI
    if len(parts) == 2 and parts[0] == 'captures' and parts[1].lower().endswith('.png'):
        return MAX_CAPTURE
    raise TransferError('備份包含白名單以外的檔案')


def _schema(db):
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    if db.execute('PRAGMA user_version').fetchone()[0] != 0:
        raise TransferError('不支援這個資料庫版本')
    records = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    expected = {'table': set(TABLES), 'index': {'sqlite_autoindex_settings_1'}}
    seen = {'table': set(), 'index': set()}
    fingerprint = {}
    for kind, name, table, sql in records:
        if kind not in expected or name not in expected[kind]:
            raise TransferError('資料庫含未知結構、檢視或觸發器')
        seen[kind].add(name)
        if kind == 'table':
            normalize = lambda value: re.sub(r'\s+', '', value).casefold()
            if normalize(sql or '') != normalize(_SQL[name]):
                raise TransferError('資料庫欄位或結構版本不受支援')
            columns = db.execute('PRAGMA table_xinfo(' + name + ')').fetchall()
            if columns != _expected_columns()[name]:
                raise TransferError('資料庫欄位定義或預設值不受支援')
            fingerprint[name] = {'sql': normalize(sql), 'columns': [list(row) for row in columns]}
            if db.execute('PRAGMA foreign_key_list(' + name + ')').fetchall():
                raise TransferError('資料庫含未知關聯')
    if seen != expected:
        raise TransferError('資料庫缺少必要資料表')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise TransferError('資料庫完整性檢查失敗')
    # Settings remain opaque except for their required JSON encoding.
    for row in db.execute('SELECT value FROM settings'):
        try:
            json.loads(row[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError, RecursionError):
            raise TransferError('資料庫設定格式無效') from None
    return hashlib.sha256(_json(fingerprint)).hexdigest(), {
        name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] for name in TABLES}


def _db_summary(path):
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return _schema(db)
    except sqlite3.Error:
        raise TransferError('無法讀取備份資料庫') from None


def _copy_file(source, target, limit):
    _regular(source)
    if source.stat().st_size > limit:
        raise TransferError('資料檔超過備份容量限制')
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with source.open('rb') as original, target.open('xb') as dest:
        while block := original.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise TransferError('資料檔超過備份容量限制')
            dest.write(block)


def _png(path):
    with path.open('rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n':
            raise TransferError('擷取檔不是 PNG 格式')


def _captures(profile):
    directory = _safe(profile / 'captures')
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise TransferError('擷取路徑不是資料夾')
    result = []
    for path in directory.iterdir():
        _safe(path)
        if path.suffix.lower() == '.png':
            _regular(path)
            _name('captures/' + path.name)
            result.append(path)
    if len(result) > MAX_FILES - 3:
        raise TransferError('擷取檔數量超過備份限制')
    return sorted(result)


def _summary(manifest, archive):
    return {key: manifest[key] for key in (
        'format_version', 'app_schema', 'created_at', 'timezone', 'counts',
        'captures', 'database_sha256', 'consistency')} | {
        'archive': str(archive), 'archive_sha256': _digest(archive),
        'saved_settings': {'desktop': 'settings.ini' in manifest['files'],
                           'chat_window': 'companion/window.ini' in manifest['files']},
        'files': len(manifest['files']),
        'uncompressed_bytes': sum(item['size'] for item in manifest['files'].values()),
    }


def create_backup(profile, destination, *, connection=None, include_captures=False):
    """Create an atomic ZIP_STORED snapshot. Caller owns its runtime/offline lock."""
    if type(include_captures) is not bool:
        raise TransferError('是否包含擷取必須是布林值')
    profile, destination = _safe(profile), _safe(destination)
    _regular(profile / DB_NAME)
    if destination.exists() or destination.name.lower() in ('settings.ini', 'window.ini', 'companion.sqlite3'):
        raise TransferError('備份輸出已存在或與資料檔衝突')
    if destination.is_relative_to(profile / 'captures'):
        raise TransferError('備份不能寫入擷取資料夾')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.TemporaryDirectory(prefix='desktopwaifu-export-') as folder:
            stage = Path(folder)
            target = stage / DB_NAME
            target.parent.mkdir(parents=True)
            with closing(sqlite3.connect(target)) as dest:
                dest.execute('PRAGMA max_page_count=' + str(MAX_DB // 4096))
                if connection is None:
                    with closing(sqlite3.connect((profile / DB_NAME).as_uri() + '?mode=ro', uri=True)) as source:
                        if source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0] > MAX_DB:
                            raise TransferError('資料庫超過 256 MiB 備份限制')
                        source.backup(dest)
                else:
                    if connection.in_transaction:
                        raise TransferError('請先完成目前的資料庫交易再建立備份')
                    if connection.execute('PRAGMA page_count').fetchone()[0] * connection.execute('PRAGMA page_size').fetchone()[0] > MAX_DB:
                        raise TransferError('資料庫超過 256 MiB 備份限制')
                    connection.backup(dest)
                # Online backup inherits the WAL header. Publish a self-contained
                # snapshot so inspecting it never creates session WAL/SHM files.
                dest.execute('PRAGMA journal_mode=DELETE')
            if target.stat().st_size > MAX_DB:
                raise TransferError('資料庫超過 256 MiB 備份限制')
            schema_hash, counts = _db_summary(target)
            for name in INI_NAMES:
                source = _safe(profile / name)
                if source.exists():
                    _copy_file(source, stage / name, MAX_INI)
            captures = _captures(profile) if include_captures else []
            for source in captures:
                dest = stage / 'captures' / source.name
                _copy_file(source, dest, MAX_CAPTURE)
                _png(dest)
            files = {}
            for source in sorted(stage.rglob('*')):
                if source.is_file():
                    name = source.relative_to(stage).as_posix()
                    _name(name)
                    files[name] = {'size': source.stat().st_size, 'sha256': _digest(source)}
            if sum(item['size'] for item in files.values()) > MAX_TOTAL:
                raise TransferError('備份超過 512 MiB 總容量限制')
            now = datetime.datetime.now().astimezone()
            manifest = {'format': FORMAT, 'format_version': FORMAT_VERSION, 'app_schema': APP_SCHEMA,
                        'created_at': now.isoformat(),
                        'timezone': {'name': str(now.tzinfo), 'utc_offset': now.strftime('%z')},
                        'schema_sha256': schema_hash, 'counts': counts, 'files': files,
                        'database_sha256': files[DB_NAME]['sha256'], 'consistency': CONSISTENCY,
                        'captures': {'included': include_captures, 'count': len(captures),
                                     'restore_policy': 'replace' if include_captures else 'preserve_destination'}}
            handle, name = tempfile.mkstemp(prefix='.desktopwaifu-backup-', suffix='.tmp', dir=destination.parent)
            os.close(handle)
            temporary = Path(name)
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('manifest.json', _json(manifest))
                for name in files:
                    archive.write(stage / name, name)
            # Verify the exact artifact before publishing it.
            inspect_backup(temporary)
            if destination.exists():
                raise TransferError('備份輸出已存在')
            os.rename(temporary, destination)
            temporary = None
            return _summary(manifest, destination)
    except (OSError, sqlite3.Error, zipfile.BadZipFile):
        raise TransferError('備份建立失敗，原始資料未更動') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _manifest_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TransferError('備份清單含重複欄位')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise TransferError('備份清單格式錯誤') from None


def _unpack(archive, stage):
    archive = _regular(archive)
    if archive.stat().st_size > MAX_TOTAL + MAX_MANIFEST + MAX_FILES * 512:
        raise TransferError('備份壓縮檔超過容量限制')
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if not 2 <= len(infos) <= MAX_FILES + 1:
                raise TransferError('備份檔案數超出限制')
            names = set()
            total = 0
            for info in infos:
                name = info.filename
                limit = MAX_MANIFEST if name == 'manifest.json' else _name(name)
                # Files only, no directory entries, links, devices or encryption.
                mode = info.external_attr >> 16
                if (info.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or
                        info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise TransferError('備份含不支援的壓縮或特殊檔案')
                canonical = name.casefold()
                if canonical in names:
                    raise TransferError('備份含重複檔名')
                names.add(canonical)
                if not 0 <= info.file_size <= limit:
                    raise TransferError('備份檔超過容量限制')
                total += info.file_size
                if total > MAX_TOTAL + MAX_MANIFEST:
                    raise TransferError('備份超過總容量限制')
            if 'manifest.json' not in names:
                raise TransferError('備份缺少清單')
            manifest_info = bundle.getinfo('manifest.json')
            with bundle.open(manifest_info) as stream:
                raw = stream.read(MAX_MANIFEST + 1)
                if len(raw) > MAX_MANIFEST:
                    raise TransferError('備份清單超過容量限制')
            manifest = _manifest_json(raw)
            required = {'format', 'format_version', 'app_schema', 'created_at', 'timezone', 'schema_sha256',
                        'counts', 'files', 'database_sha256', 'consistency', 'captures'}
            if (not isinstance(manifest, dict) or set(manifest) != required or manifest['format'] != FORMAT or
                    type(manifest['format_version']) is not int or manifest['format_version'] != FORMAT_VERSION or
                    type(manifest['app_schema']) is not int or manifest['app_schema'] != APP_SCHEMA):
                raise TransferError('不支援這個備份格式或版本')
            files = manifest['files']
            if not isinstance(files, dict) or set(files) != {info.filename for info in infos if info.filename != 'manifest.json'} or DB_NAME not in files:
                raise TransferError('備份檔案與清單不符')
            capture = manifest['captures']
            if (not isinstance(capture, dict) or set(capture) != {'included', 'count', 'restore_policy'} or
                    type(capture['included']) is not bool or type(capture['count']) is not int or
                    capture['count'] != sum(name.startswith('captures/') for name in files) or
                    capture['restore_policy'] != ('replace' if capture['included'] else 'preserve_destination') or
                    (not capture['included'] and capture['count'])):
                raise TransferError('擷取還原規則無效')
            try:
                stamp = datetime.datetime.fromisoformat(manifest['created_at'])
                timezone = manifest['timezone']
                if (stamp.utcoffset() is None or not isinstance(timezone, dict) or
                        set(timezone) != {'name', 'utc_offset'} or not isinstance(timezone['name'], str) or
                        timezone['utc_offset'] != stamp.strftime('%z') or manifest['consistency'] != CONSISTENCY):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise TransferError('備份時間與時區格式無效') from None
            total = 0
            for name, metadata in files.items():
                limit = _name(name)
                if (not isinstance(metadata, dict) or set(metadata) != {'size', 'sha256'} or
                        type(metadata['size']) is not int or not 0 <= metadata['size'] <= limit or
                        not isinstance(metadata['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', metadata['sha256'])):
                    raise TransferError('備份雜湊清單格式無效')
                info = bundle.getinfo(name)
                if info.file_size != metadata['size']:
                    raise TransferError('備份檔案大小與清單不符')
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                digest = hashlib.sha256()
                with bundle.open(info) as source, target.open('xb') as dest:
                    while block := source.read(1024 * 1024):
                        count += len(block)
                        total += len(block)
                        if count > limit or total > MAX_TOTAL:
                            raise TransferError('備份解壓縮超過容量限制')
                        digest.update(block)
                        dest.write(block)
                if count != metadata['size'] or digest.hexdigest() != metadata['sha256']:
                    raise TransferError('備份檔案完整性檢查失敗')
                if name.startswith('captures/'):
                    _png(target)
            schema_hash, counts = _db_summary(stage / DB_NAME)
            if (not isinstance(manifest['counts'], dict) or set(manifest['counts']) != set(TABLES) or
                    any(type(value) is not int or value < 0 for value in manifest['counts'].values()) or
                    manifest['schema_sha256'] != schema_hash or manifest['counts'] != counts or
                    manifest['database_sha256'] != files[DB_NAME]['sha256']):
                raise TransferError('資料庫結構、筆數或雜湊與清單不符')
            return manifest
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, EOFError, zlib.error):
        raise TransferError('無法讀取備份，可能已損毀') from None


def inspect_backup(archive):
    """Validate everything; return counts and metadata without private content."""
    archive = _safe(archive)
    with tempfile.TemporaryDirectory(prefix='desktopwaifu-inspect-') as folder:
        manifest = _unpack(archive, Path(folder))
        return _summary(manifest, archive)


def _install_file(source, destination):
    """Small test seam; all caller-provided paths have already been validated."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.desktopwaifu-restore-', dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _check_target(profile):
    _safe(profile)
    for name in (*INI_NAMES, DB_NAME, DB_NAME + '-wal', DB_NAME + '-shm', DB_NAME + '-journal'):
        path = _safe(profile / name)
        if path.exists() and not path.is_file():
            raise TransferError('目的資料路徑不是一般檔案')
    _captures(profile)


def _apply(stage, manifest, profile):
    _check_target(profile)
    for suffix in ('-wal', '-shm', '-journal'):
        (profile / (DB_NAME + suffix)).unlink(missing_ok=True)
    for name in (*INI_NAMES, DB_NAME):
        target = profile / name
        if name in manifest['files']:
            _install_file(stage / name, target)
        else:
            target.unlink(missing_ok=True)
    if manifest['captures']['included']:
        wanted = {name for name in manifest['files'] if name.startswith('captures/')}
        for path in _captures(profile):
            if 'captures/' + path.name not in wanted:
                path.unlink()
        for name in wanted:
            _install_file(stage / name, profile / name)


def restore_backup(archive, profile):
    """Offline replacement with a retained recovery ZIP and logical rollback."""
    from profile_guard import ProfileLease, ensure_profile_inactive
    profile, archive = _safe(profile), _safe(archive)
    with tempfile.TemporaryDirectory(prefix='desktopwaifu-restore-') as folder:
        stage = Path(folder) / 'incoming'
        stage.mkdir()
        manifest = _unpack(archive, stage)
        # Cache everything derived from the removable source before changing
        # the destination. The staged files are sufficient for the transaction.
        summary = _summary(manifest, archive)
        with ProfileLease(profile / 'companion', exclusive=True):
            ensure_profile_inactive(profile / 'companion')
            _check_target(profile)
            existing = (profile / DB_NAME).exists()
            # Existing INI/captures without a database are not a complete profile.
            # Do not silently discard such files during first-time import.
            if not existing and (any((profile / name).exists() for name in INI_NAMES) or _captures(profile)):
                raise TransferError('目的設定不完整，請先另行保存或選擇空資料夾')
            recovery = None
            rollback_manifest = None
            rollback_stage = Path(folder) / 'rollback'
            if existing:
                recovery = profile / 'companion/backups' / (
                    'before-restore-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.zip')
                create_backup(profile, recovery, include_captures=True)
                rollback_stage.mkdir()
                rollback_manifest = _unpack(recovery, rollback_stage)
            try:
                _apply(stage, manifest, profile)
                actual_schema, actual_counts = _db_summary(profile / DB_NAME)
                if actual_schema != manifest['schema_sha256'] or actual_counts != manifest['counts']:
                    raise TransferError('還原後驗證失敗')
            except Exception as error:
                try:
                    if rollback_manifest is not None:
                        _apply(rollback_stage, rollback_manifest, profile)
                    else:
                        for name in (*INI_NAMES, DB_NAME, DB_NAME + '-wal', DB_NAME + '-shm', DB_NAME + '-journal'):
                            _safe(profile / name).unlink(missing_ok=True)
                        for path in _captures(profile):
                            path.unlink()
                except Exception:
                    raise TransferError('還原未完成且自動復原失敗；請保留 before-restore 備份並停止啟動') from error
                raise TransferError('還原未完成，原資料已復原') from error
            return summary | {'restored': True, 'recovery_backup': str(recovery) if recovery else None,
                                                 'reminder_policy': 'Unix timestamps retained; pending overdue reminders catch up on next launch; notified/done reminders are not reset.',
                                                 'quiet_hours_policy': 'Saved clock times use the destination system timezone.'}
