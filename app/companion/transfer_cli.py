"""Offline profile backup/restore; interactive dialogs use built-in Windows APIs."""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_guard import ProfileLease, ensure_profile_inactive
from profile_transfer import create_backup, inspect_backup, restore_backup


def default_profile():
    shell = c.WinDLL('shell32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [c.c_void_p, w.DWORD, w.HANDLE, c.POINTER(c.c_void_p)]
    shell.SHGetKnownFolderPath.restype = c.c_long
    ole = c.WinDLL('ole32'); ole.CoTaskMemFree.argtypes = [c.c_void_p]
    identifier = (c.c_byte * 16).from_buffer_copy(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
    result = c.c_void_p()
    if shell.SHGetKnownFolderPath(c.byref(identifier), 0, None, c.byref(result)) < 0:
        raise RuntimeError('無法取得 Windows 使用者資料目錄，請以 --profile 指定資料夾。')
    try:
        preview = (Path(__file__).resolve().parent.parent / 'preview-mode.flag').is_file()
        return Path(c.wstring_at(result)) / ('DesktopWaifu-Preview' if preview else 'DesktopWaifu')
    finally:
        ole.CoTaskMemFree(result)


def dialog(text, flags=0x40):
    user = c.WinDLL('user32'); user.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
    return user.MessageBoxW(None, text, '灰姑娘 · 本機資料移轉', flags)


class OpenFileName(c.Structure):
    _fields_ = [('lStructSize', w.DWORD), ('hwndOwner', w.HWND), ('hInstance', w.HINSTANCE),
                ('lpstrFilter', w.LPCWSTR), ('lpstrCustomFilter', w.LPWSTR), ('nMaxCustFilter', w.DWORD),
                ('nFilterIndex', w.DWORD), ('lpstrFile', w.LPWSTR), ('nMaxFile', w.DWORD),
                ('lpstrFileTitle', w.LPWSTR), ('nMaxFileTitle', w.DWORD), ('lpstrInitialDir', w.LPCWSTR),
                ('lpstrTitle', w.LPCWSTR), ('Flags', w.DWORD), ('nFileOffset', w.WORD), ('nFileExtension', w.WORD),
                ('lpstrDefExt', w.LPCWSTR), ('lCustData', w.LPARAM), ('lpfnHook', c.c_void_p),
                ('lpTemplateName', w.LPCWSTR), ('pvReserved', c.c_void_p), ('dwReserved', w.DWORD), ('FlagsEx', w.DWORD)]


def pick_archive(save=False):
    common = c.WinDLL('comdlg32', use_last_error=True)
    common.CommDlgExtendedError.restype = w.DWORD
    buffer = c.create_unicode_buffer(32768)
    if save:
        buffer.value = 'DesktopWaifu-backup-' + time.strftime('%Y%m%d-%H%M%S') + '.zip'
    form = OpenFileName(); form.lStructSize = c.sizeof(form)
    form.lpstrFilter = '灰姑娘資料備份 (*.zip)\0*.zip\0\0'; form.nFilterIndex = 1
    form.lpstrFile = c.cast(buffer, w.LPWSTR); form.nMaxFile = len(buffer)
    form.lpstrDefExt = 'zip'; form.lpstrTitle = '選擇新的備份檔案位置' if save else '選擇要預覽及還原的備份'
    form.Flags = 0x80000 | 0x8 | 0x800 | (0x2 if save else 0x1000)
    method = common.GetSaveFileNameW if save else common.GetOpenFileNameW
    method.argtypes = [c.POINTER(OpenFileName)]; method.restype = w.BOOL
    if not method(c.byref(form)):
        error = common.CommDlgExtendedError()
        if error:
            raise RuntimeError('無法開啟檔案選擇視窗：' + str(error))
        return None
    return Path(buffer.value)


def preview_text(summary):
    counts = summary['counts']; captures = summary['captures']
    saved = summary.get('saved_settings', {})
    reset = []
    if saved.get('desktop') is False:
        reset.append('桌面位置與操作設定')
    if saved.get('chat_window') is False:
        reset.append('聊天視窗位置')
    return ('備份時間：' + summary['created_at'] + '\n'
            + '對話訊息：' + str(counts['messages']) + '　記憶：' + str(counts['memories']) + '\n'
            + '提醒：' + str(counts['reminders']) + '　設定項目：' + str(counts['settings']) + '\n'
            + ('截圖：' + str(captures['count']) + ' 張，取代目的端 PNG 截圖。' if captures['included'] else '備份未包含截圖，保留目的端截圖。') + '\n'
            + '提醒的實際到期時間保留；安靜時段依新電腦當地時間執行。'
            + ('\n未包含' + '、'.join(reset) + '，目的端這些設定將回到預設。' if reset else ''))


def main(argv=None):
    parser = argparse.ArgumentParser(description='灰姑娘本機資料備份與還原；不包含模型及執行環境。')
    sub = parser.add_subparsers(dest='mode', required=True)
    export = sub.add_parser('export'); export.add_argument('--output', type=Path)
    export.add_argument('--captures', action='store_true')
    restore = sub.add_parser('restore'); restore.add_argument('archive', nargs='?', type=Path)
    restore.add_argument('--apply', action='store_true', help='明確套用整份取代；預設只驗證與預覽。')
    for command in (export, restore):
        command.add_argument('--profile', type=Path, help='預設為本機 Windows 使用者的 DesktopWaifu 資料目錄。')
        command.add_argument('--interactive', action='store_true')
    args = parser.parse_args(argv)
    try:
        profile = (args.profile or default_profile()).absolute()
        if args.mode == 'export':
            output = args.output or (pick_archive(True) if args.interactive else None)
            if output is None:
                if args.interactive:
                    return 0
                raise ValueError('請使用 --output 指定新的 ZIP 檔案路徑。')
            include = args.captures
            if args.interactive and not include:
                choice = dialog('是否包含你保存的透明 PNG 截圖？\n\n是：包含截圖\n否：只備份對話、記憶、提醒與設定', 0x23)
                if choice == 2:
                    return 0
                include = choice == 6
            with ProfileLease(profile / 'companion', exclusive=True):
                ensure_profile_inactive(profile / 'companion')
                summary = create_backup(profile, output.absolute(), include_captures=include)
            if args.interactive:
                dialog('備份已建立。請妥善保存，此檔含私人資料且未加密。\n\n' + str(output.absolute()))
            else:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            archive = args.archive or (pick_archive() if args.interactive else None)
            if archive is None:
                if args.interactive:
                    return 0
                raise ValueError('請指定要預覽的 ZIP 備份檔。')
            summary = inspect_backup(archive)
            apply = args.apply
            if args.interactive:
                apply = dialog(preview_text(summary) + '\n\n目的資料夾：' + str(profile)
                               + '\n\n將整份取代現有對話、記憶、提醒與設定，不合併。'
                               + '\n原資料會先建立復原備份。請先退出桌寵及聊天。\n\n確定還原？', 0x134) == 6
                if not apply:
                    return 0
            if not apply:
                print(json.dumps({'preview_only': True, 'summary': summary}, ensure_ascii=False, indent=2))
                return 0
            result = restore_backup(archive, profile)
            if args.interactive:
                dialog('資料已還原。請從原啟動入口開啟桌寵。\n模型與系統依賴需由目的電腦另行備妥。'
                       + '\n\n原資料復原備份：\n' + str(result.get('recovery_backup') or '目的資料夾原本沒有資料庫，未產生舊資料備份。'))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        if args.interactive:
            dialog('未完成，請確認桌寵與聊天已退出，再檢查備份檔。\n\n' + str(error), 0x10)
        else:
            print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
