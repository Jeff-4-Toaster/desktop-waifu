#pragma once
// Shared read-only selection for the early probe and the actual WebView.
// Fixed Runtime is private to the application. No registry/ACL edits or installs.
#include <windows.h>
#include <shobjidl.h>
#include <filesystem>
#include <string>
#include <vector>
#include <cwctype>
#include <winver.h>

namespace desktop_webview {
namespace fs = std::filesystem;
struct Selection {
    fs::path folder;
    std::wstring version;
    std::string source="none", error="version_unavailable";
    HRESULT status=HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND);
    bool available=false, fixedPresent=false, envIgnored=false;
};
inline fs::path executablePath() {
    std::wstring value(32768,L'\0');
    DWORD size=GetModuleFileNameW(nullptr,value.data(),static_cast<DWORD>(value.size()));
    if(!size||size>=value.size())return {};
    value.resize(size);return fs::path(value);
}
inline bool reparseAncestor(const fs::path& path) {
    fs::path current=path.root_path();
    for(const auto& part:path.relative_path()) {
        current/=part;DWORD attributes=GetFileAttributesW(current.c_str());
        if(attributes==INVALID_FILE_ATTRIBUTES) {
            DWORD code=GetLastError();
            if(code==ERROR_FILE_NOT_FOUND||code==ERROR_PATH_NOT_FOUND)return false;
            return true;
        }
        if(attributes&FILE_ATTRIBUTE_REPARSE_POINT)return true;
    }
    return false;
}
inline bool clearEnvironmentOverrides(bool& cleared) {
    cleared=true;
    bool present=false;
    for(const wchar_t* key:{L"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",L"WEBVIEW2_USER_DATA_FOLDER",
        L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",L"WEBVIEW2_CHANNEL_SEARCH_KIND",L"WEBVIEW2_RELEASE_CHANNELS",
        L"WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",L"WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",L"WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER"}) {
        if(GetEnvironmentVariableW(key,nullptr,0)) {
            present=true;
            if(!SetEnvironmentVariableW(key,nullptr))cleared=false; // This child process only.
        }
    }
    return present;
}
inline bool policyConflict(const fs::path& executable) {
    // Microsoft documents AppUserModelID, executable name, then wildcard.
    // A policy can override even explicit browser/user-data folder arguments.
    std::vector<std::wstring> ids;
    PWSTR appId=nullptr;
    if(SUCCEEDED(GetCurrentProcessExplicitAppUserModelID(&appId))&&appId)ids.emplace_back(appId);
    CoTaskMemFree(appId);
    ids.push_back(executable.filename().wstring());ids.push_back(executable.wstring());
    ids.emplace_back(L"*");
    for(HKEY hive:{HKEY_LOCAL_MACHINE,HKEY_CURRENT_USER}) {
        for(const wchar_t* setting:{L"BrowserExecutableFolder",L"UserDataFolder",L"AdditionalBrowserArguments",
            L"ChannelSearchKind",L"ReleaseChannels"}) {
            std::wstring key=L"Software\\Policies\\Microsoft\\Edge\\WebView2\\";key+=setting;
            HKEY opened=nullptr;
            LSTATUS status=RegOpenKeyExW(hive,key.c_str(),0,KEY_QUERY_VALUE|KEY_WOW64_64KEY,&opened);
            if(status==ERROR_FILE_NOT_FOUND||status==ERROR_PATH_NOT_FOUND)continue;
            if(status!=ERROR_SUCCESS)return true;
            bool found=false;
            for(const auto& id:ids) {
                if(id==L"*"&&std::wstring(setting)==L"UserDataFolder")continue;
                DWORD type=0,size=0;
                LSTATUS read=RegQueryValueExW(opened,id.c_str(),nullptr,&type,nullptr,&size);
                if(read==ERROR_SUCCESS||read==ERROR_MORE_DATA){found=true;break;}
                if(read!=ERROR_FILE_NOT_FOUND){found=true;break;}
            }
            RegCloseKey(opened);if(found)return true;
        }
    }
    return false;
}
inline std::wstring fileVersion(const fs::path& executable) {
    DWORD ignored=0,size=GetFileVersionInfoSizeW(executable.c_str(),&ignored);
    if(!size||size>4*1024*1024)return {};
    std::vector<BYTE> bytes(size);VS_FIXEDFILEINFO* info=nullptr;UINT length=0;
    if(!GetFileVersionInfoW(executable.c_str(),0,size,bytes.data())||
       !VerQueryValueW(bytes.data(),L"\\",reinterpret_cast<void**>(&info),&length)||
       !info||length<sizeof(VS_FIXEDFILEINFO)||info->dwSignature!=0xfeef04bd)return {};
    return std::to_wstring(HIWORD(info->dwFileVersionMS))+L"."+std::to_wstring(LOWORD(info->dwFileVersionMS))+L"."+
        std::to_wstring(HIWORD(info->dwFileVersionLS))+L"."+std::to_wstring(LOWORD(info->dwFileVersionLS));
}
inline bool x64Binary(const fs::path& path) {
    HANDLE file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,
        nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE)return false;
    IMAGE_DOS_HEADER dos{};DWORD read=0;LARGE_INTEGER size{},offset{};
    bool valid=GetFileSizeEx(file,&size)&&ReadFile(file,&dos,sizeof(dos),&read,nullptr)&&read==sizeof(dos)&&
        dos.e_magic==IMAGE_DOS_SIGNATURE&&dos.e_lfanew>=static_cast<LONG>(sizeof(dos))&&
        dos.e_lfanew<16*1024*1024&&static_cast<LONGLONG>(dos.e_lfanew)+6<=size.QuadPart;
    if(valid) {
        offset.QuadPart=dos.e_lfanew;BYTE header[6]{};
        valid=SetFilePointerEx(file,offset,nullptr,FILE_BEGIN)&&ReadFile(file,header,sizeof(header),&read,nullptr)&&
            read==sizeof(header)&&header[0]=='P'&&header[1]=='E'&&header[2]==0&&header[3]==0&&header[4]==0x64&&header[5]==0x86;
    }
    CloseHandle(file);return valid;
}
inline bool numericVersion(const std::wstring& version) {
    if(version.empty()||version.size()>64)return false;
    int dots=0;bool digit=false;
    for(wchar_t value:version) {
        if(value>=L'0'&&value<=L'9'){digit=true;continue;}
        if(value!=L'.'||!digit)return false;
        ++dots;digit=false;
    }
    return digit&&dots==3;
}
inline Selection select() {
    Selection result;bool envCleared=false;result.envIgnored=clearEnvironmentOverrides(envCleared);
    fs::path executable=executablePath();
    if(executable.empty()){result.status=E_FAIL;return result;}
    if(executable.root_name().wstring().rfind(L"\\\\",0)==0) {
        result.status=HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);return result;
    }
    fs::path fixed=executable.parent_path()/L"runtime"/L"webview2";
    if(reparseAncestor(fixed)) {
        result.fixedPresent=true;result.source="fixed";result.folder=fixed;
        result.error="invalid_bundle";result.status=HRESULT_FROM_WIN32(ERROR_INVALID_DATA);return result;
    }
    DWORD attributes=GetFileAttributesW(fixed.c_str());
    DWORD missing=attributes==INVALID_FILE_ATTRIBUTES?GetLastError():ERROR_SUCCESS;
    result.fixedPresent=attributes!=INVALID_FILE_ATTRIBUTES||
        (missing!=ERROR_FILE_NOT_FOUND&&missing!=ERROR_PATH_NOT_FOUND);
    // A malformed runtime parent must not turn into a silent installed fallback.
    DWORD parentAttributes=GetFileAttributesW(fixed.parent_path().c_str());
    if(parentAttributes!=INVALID_FILE_ATTRIBUTES&&!(parentAttributes&FILE_ATTRIBUTE_DIRECTORY))result.fixedPresent=true;
    if(result.fixedPresent) {
        result.source="fixed";result.folder=fixed;result.error="invalid_bundle";
        result.status=HRESULT_FROM_WIN32(ERROR_INVALID_DATA);
        if(attributes==INVALID_FILE_ATTRIBUTES||!(attributes&FILE_ATTRIBUTE_DIRECTORY))return result;
        for(const wchar_t* file:{L"msedgewebview2.exe",L"msedge.dll",L"icudtl.dat"}) {
            fs::path path=fixed/file;DWORD value=GetFileAttributesW(path.c_str());
            if(value==INVALID_FILE_ATTRIBUTES||(value&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT)))return result;
        }
        if(!x64Binary(fixed/L"msedgewebview2.exe")||!x64Binary(fixed/L"msedge.dll"))return result;
        if(policyConflict(executable)) {result.error="policy_conflict";result.status=E_ACCESSDENIED;return result;}
    }
    if(!envCleared) {result.error="policy_conflict";result.status=E_ACCESSDENIED;return result;}
    wchar_t* version=nullptr;
    result.status=GetAvailableCoreWebView2BrowserVersionString(result.fixedPresent?result.folder.c_str():nullptr,&version);
    if(version)result.version=version;CoTaskMemFree(version);
    result.available=result.status==S_OK&&!result.version.empty();
    if(result.fixedPresent&&(!numericVersion(result.version)||result.version!=fileVersion(fixed/L"msedgewebview2.exe"))) {
        result.available=false;result.version.clear();result.status=HRESULT_FROM_WIN32(ERROR_INVALID_DATA);
    }
    if(result.available) {result.source=result.fixedPresent?"fixed":"installed";result.error="none";}
    else {result.version.clear();if(!result.fixedPresent)result.source="none";}
    if(!result.available&&result.error!="policy_conflict")result.error="version_unavailable";
    return result;
}
inline bool browserMatches(const Selection& selection,DWORD pid) {
    if(!selection.fixedPresent)return true;
    HANDLE process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,pid);
    if(!process)return false;
    std::wstring image(32768,L'\0');DWORD length=static_cast<DWORD>(image.size());
    bool valid=QueryFullProcessImageNameW(process,0,image.data(),&length)!=FALSE;
    CloseHandle(process);if(!valid)return false;image.resize(length);
    return _wcsicmp(fs::path(image).lexically_normal().c_str(),(selection.folder/L"msedgewebview2.exe").lexically_normal().c_str())==0;
}
} // namespace desktop_webview
