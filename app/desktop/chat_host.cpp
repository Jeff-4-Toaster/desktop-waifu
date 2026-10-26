#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <wrl.h>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <algorithm>
#include "WebView2.h"
#include "webview_runtime.h"
using Microsoft::WRL::ComPtr;
using Microsoft::WRL::Callback;
namespace fs=std::filesystem;
// This path must run before COM, profile paths, mutexes, or any visible window.
// The statically linked loader only discovers the selected runtime; it does not
// create an environment or a WebView user-data directory.
int probeRuntime(const desktop_webview::Selection& runtime) {
    const wchar_t* version=runtime.version.c_str();
    std::string text; if(*version) {int length=WideCharToMultiByte(CP_UTF8,0,version,-1,nullptr,0,nullptr,nullptr);if(length>1) {text.resize(length);WideCharToMultiByte(CP_UTF8,0,version,-1,text.data(),length,nullptr,nullptr);text.pop_back();}}
    std::string escaped;for(unsigned char value:text) {if(value=='"'||value=='\\')escaped+='\\';if(value>=32)escaped+=value;}
    std::string json="{\"available\":"+std::string(runtime.available?"true":"false")+",\"version\":\""+escaped+"\",\"hresult\":"+std::to_string(static_cast<unsigned long>(runtime.status))+
        ",\"source\":\""+runtime.source+"\",\"fixed_present\":"+(runtime.fixedPresent?"true":"false")+",\"error\":\""+runtime.error+
        "\",\"fixed_relative\":\""+(runtime.fixedPresent?"runtime/webview2":"")+"\",\"env_overrides_ignored\":"+(runtime.envIgnored?"true":"false")+"}\n";
    HANDLE output=GetStdHandle(STD_OUTPUT_HANDLE);DWORD written=0;return output!=INVALID_HANDLE_VALUE&&WriteFile(output,json.data(),static_cast<DWORD>(json.size()),&written,nullptr)&&written==json.size()?0:3;
}
std::string readText(const fs::path& path) {auto file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);if(file==INVALID_HANDLE_VALUE)return {};char bytes[2048];DWORD count=0;bool ok=ReadFile(file,bytes,sizeof(bytes),&count,nullptr)!=FALSE;CloseHandle(file);return ok?std::string(bytes,count):std::string();}
struct ChatHost {
    desktop_webview::Selection runtime;
    HWND window{};ComPtr<ICoreWebView2Controller> controller;ComPtr<ICoreWebView2> view;
    fs::path data,capture,script,result;std::wstring url,origin,className;ULONGLONG started=GetTickCount64(),lastConnection=0;double duration=0,captureAfter=4;bool captured=false;
    void save() {RECT r{};if(!GetWindowRect(window,&r))return;auto ini=data/L"window.ini";for(auto pair:{std::pair<const wchar_t*,int>{L"x",r.left},{L"y",r.top},{L"w",r.right-r.left},{L"h",r.bottom-r.top}}) WritePrivateProfileStringW(L"Chat",pair.first,std::to_wstring(pair.second).c_str(),ini.c_str());}
    void bounds() {if(controller) {RECT r{};GetClientRect(window,&r);controller->put_Bounds(r);}}
    void initialize() {
        if(!runtime.available) {MessageBoxW(window,L"聊天執行環境未能通過檢查。請重新解壓完整包或執行「檢查執行環境」。",L"灰姑娘",MB_OK);return;}
        auto webData=data/(runtime.fixedPresent?L"webview-fixed-"+runtime.version:L"webview");fs::create_directories(webData);
        HRESULT hr=CreateCoreWebView2EnvironmentWithOptions(runtime.fixedPresent?runtime.folder.c_str():nullptr,webData.c_str(),nullptr,Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>([this](HRESULT status,ICoreWebView2Environment* environment)->HRESULT {
            if(FAILED(status)||!environment) {MessageBoxW(window,L"無法啟動聊天視窗。請確認 Microsoft Edge WebView2 執行環境可用。",L"灰姑娘",MB_OK);return S_OK;}
            return environment->CreateCoreWebView2Controller(window,Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>([this](HRESULT created,ICoreWebView2Controller* value)->HRESULT {
                if(FAILED(created)||!value)return S_OK;controller=value;controller->get_CoreWebView2(&view);bounds();
                UINT32 browser=0;
                if(FAILED(view->get_BrowserProcessId(&browser))||!desktop_webview::browserMatches(runtime,browser)) {
                    controller->Close();view.Reset();controller.Reset();
                    MessageBoxW(window,L"聊天未使用所選執行環境，已停止載入。請檢查環境或系統政策。",L"灰姑娘",MB_OK);return S_OK;
                }
                ComPtr<ICoreWebView2Settings> settings;view->get_Settings(&settings);settings->put_AreDefaultContextMenusEnabled(FALSE);settings->put_IsStatusBarEnabled(FALSE);
                EventRegistrationToken event{};
                view->add_NavigationStarting(Callback<ICoreWebView2NavigationStartingEventHandler>([this](ICoreWebView2*,ICoreWebView2NavigationStartingEventArgs* args)->HRESULT {wchar_t* uri{};args->get_Uri(&uri);std::wstring target=uri?uri:L"";CoTaskMemFree(uri);if(target.rfind(origin+L"/",0)!=0&&target!=L"about:blank") args->put_Cancel(TRUE);return S_OK;}).Get(),&event);
                view->add_NewWindowRequested(Callback<ICoreWebView2NewWindowRequestedEventHandler>([](ICoreWebView2*,ICoreWebView2NewWindowRequestedEventArgs* args)->HRESULT {args->put_Handled(TRUE);return S_OK;}).Get(),&event);
                view->add_NavigationCompleted(Callback<ICoreWebView2NavigationCompletedEventHandler>([this](ICoreWebView2*,ICoreWebView2NavigationCompletedEventArgs*)->HRESULT {
                    if(!script.empty()) {std::ifstream stream(script,std::ios::binary);std::string raw((std::istreambuf_iterator<char>(stream)),{});int count=MultiByteToWideChar(CP_UTF8,0,raw.data(),int(raw.size()),nullptr,0);std::wstring code(count,0);MultiByteToWideChar(CP_UTF8,0,raw.data(),int(raw.size()),code.data(),count);view->ExecuteScript(code.c_str(),Callback<ICoreWebView2ExecuteScriptCompletedHandler>([this](HRESULT,LPCWSTR value)->HRESULT {if(!result.empty()) {std::wofstream file(result);file<<(value?value:L"null");}return S_OK;}).Get());}return S_OK;
                }).Get(),&event);
                view->Navigate(url.c_str());return S_OK;
            }).Get());
        }).Get());
        if(FAILED(hr)) MessageBoxW(window,L"聊天視窗未能建立，桌寵仍可使用。",L"灰姑娘",MB_OK);
    }
    void tick() {
        if(view&&GetTickCount64()-lastConnection>1000) {lastConnection=GetTickCount64();std::istringstream file(readText(data/L"connection.txt"));int port=0;std::string token;if(file>>port>>token&&port>=1024&&port<=65535&&token.size()>=32&&token.size()<=128&&token.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")==std::string::npos) {std::wstring next=L"http://127.0.0.1:"+std::to_wstring(port)+L"/#token="+std::wstring(token.begin(),token.end());if(next!=url) {url=next;origin=L"http://127.0.0.1:"+std::to_wstring(port);view->Navigate(url.c_str());}}}
        double age=(GetTickCount64()-started)/1000.;
        if(view&&!capture.empty()&&!captured&&age>=captureAfter) {captured=true;fs::create_directories(capture.parent_path());ComPtr<IStream> stream;if(SUCCEEDED(SHCreateStreamOnFileEx(capture.c_str(),STGM_CREATE|STGM_WRITE|STGM_SHARE_EXCLUSIVE,FILE_ATTRIBUTE_NORMAL,TRUE,nullptr,&stream))) view->CapturePreview(COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG,stream.Get(),Callback<ICoreWebView2CapturePreviewCompletedHandler>([stream](HRESULT)->HRESULT {stream->Commit(STGC_DEFAULT);return S_OK;}).Get());}
        if(duration>0&&age>=duration) DestroyWindow(window);
    }
    static LRESULT CALLBACK proc(HWND h,UINT msg,WPARAM wp,LPARAM lp) {
        auto self=reinterpret_cast<ChatHost*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(msg==WM_NCCREATE) {self=static_cast<ChatHost*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);self->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(self));}if(!self)return DefWindowProcW(h,msg,wp,lp);
        switch(msg) {
        case WM_SIZE:self->bounds();return 0;
        case WM_DPICHANGED:{auto r=reinterpret_cast<RECT*>(lp);SetWindowPos(h,nullptr,r->left,r->top,r->right-r->left,r->bottom-r->top,SWP_NOZORDER|SWP_NOACTIVATE);self->bounds();return 0;}
        case WM_GETMINMAXINFO:{auto limits=reinterpret_cast<MINMAXINFO*>(lp);limits->ptMinTrackSize={420,560};return 0;}
        case WM_TIMER:self->tick();return 0;
        case WM_ERASEBKGND:{RECT r{};GetClientRect(h,&r);auto brush=CreateSolidBrush(RGB(248,250,253));FillRect(reinterpret_cast<HDC>(wp),&r,brush);DeleteObject(brush);return 1;}
        case WM_CLOSE:DestroyWindow(h);return 0;
        case WM_DESTROY:self->save();if(self->controller)self->controller->Close();self->view.Reset();self->controller.Reset();PostQuitMessage(0);return 0;
        }
        return DefWindowProcW(h,msg,wp,lp);
    }
};
int WINAPI wWinMain(HINSTANCE instance,HINSTANCE,PWSTR,int) {
    int earlyCount=0;auto earlyArgs=CommandLineToArgvW(GetCommandLineW(),&earlyCount);bool probe=false;for(int i=1;earlyArgs&&i<earlyCount;i++)if(std::wstring(earlyArgs[i])==L"--probe-runtime")probe=true;LocalFree(earlyArgs);
    auto runtime=desktop_webview::select();if(probe)return probeRuntime(runtime);
    if(!runtime.available) {
        MessageBoxW(nullptr,L"聊天執行環境未通過檢查，已停止開啟聊天。請執行包內的「檢查執行環境」。桌寵仍可使用。",L"灰姑娘",MB_OK|MB_ICONWARNING);
        return 4;
    }
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);if(FAILED(CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED)))return 1;
    ChatHost app;app.runtime=runtime;int argc=0;auto argv=CommandLineToArgvW(GetCommandLineW(),&argc);
    for(int i=1;i<argc;i++) {std::wstring arg=argv[i];if(arg.rfind(L"--url=",0)==0)app.url=arg.substr(6);else if(arg.rfind(L"--data=",0)==0)app.data=arg.substr(7);else if(arg.rfind(L"--capture=",0)==0)app.capture=arg.substr(10);else if(arg.rfind(L"--script=",0)==0)app.script=arg.substr(9);else if(arg.rfind(L"--result=",0)==0)app.result=arg.substr(9);else if(arg.rfind(L"--duration=",0)==0)app.duration=_wtof(arg.substr(11).c_str());else if(arg.rfind(L"--capture-after=",0)==0)app.captureAfter=_wtof(arg.substr(16).c_str());}LocalFree(argv);
    if(app.data.empty()||app.url.rfind(L"http://127.0.0.1:",0)!=0) {CoUninitialize();return 2;}auto slash=app.url.find(L'/',17);if(slash==std::wstring::npos){CoUninitialize();return 2;}app.origin=app.url.substr(0,slash);fs::create_directories(app.data);
    auto hash=std::to_wstring(std::hash<std::wstring>{}(fs::absolute(app.data).wstring()));app.className=L"DesktopWaifu.Companion.Chat."+hash;
    auto name=L"Local\\DesktopWaifu.Chat."+hash;HANDLE mutex=CreateMutexW(nullptr,FALSE,name.c_str());if(GetLastError()==ERROR_ALREADY_EXISTS) {HWND existing=FindWindowW(app.className.c_str(),nullptr);if(existing) {ShowWindow(existing,SW_RESTORE);SetForegroundWindow(existing);}if(mutex)CloseHandle(mutex);CoUninitialize();return 0;}
    WNDCLASSEXW wc{sizeof(wc)};wc.hInstance=instance;wc.lpfnWndProc=ChatHost::proc;wc.lpszClassName=app.className.c_str();wc.hCursor=LoadCursorW(nullptr,IDC_ARROW);wc.hIcon=LoadIconW(nullptr,IDI_APPLICATION);RegisterClassExW(&wc);
    auto ini=app.data/L"window.ini";auto get=[&](const wchar_t* key,int value){return int(GetPrivateProfileIntW(L"Chat",key,value,ini.c_str()));};
    RECT r{get(L"x",120),get(L"y",80),0,0};MONITORINFO monitor{sizeof(monitor)};GetMonitorInfoW(MonitorFromRect(&r,MONITOR_DEFAULTTONEAREST),&monitor);auto work=monitor.rcWork;int width=std::clamp(get(L"w",510),420,std::max(420,int(work.right-work.left)));int height=std::clamp(get(L"h",780),560,std::max(560,int(work.bottom-work.top)));r.left=std::clamp<int>(r.left,work.left,work.right-width);r.top=std::clamp<int>(r.top,work.top,work.bottom-height);
    app.window=CreateWindowExW(0,wc.lpszClassName,L"灰姑娘 · 聊天與日常",WS_OVERLAPPEDWINDOW|WS_CLIPCHILDREN,r.left,r.top,width,height,nullptr,nullptr,instance,&app);ShowWindow(app.window,SW_SHOW);app.initialize();SetTimer(app.window,1,100,nullptr);MSG msg{};while(GetMessageW(&msg,nullptr,0,0)>0){TranslateMessage(&msg);DispatchMessageW(&msg);}if(mutex)CloseHandle(mutex);CoUninitialize();return 0;
}
