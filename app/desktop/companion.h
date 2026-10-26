#pragma once
#include <ctime>

// Only children created by this instance enter these jobs. No global process killing.
struct CompanionLink {
    fs::path folder,data;HANDLE process{},serviceJob{},chatJob{};HWND bubble{},owner{};
    std::wstring bubbleText;double nextPoll=0,nextPresence=0,retryAfter=0,bubbleUntil=0;
    int activity=0,emotion=-1;unsigned long long emotionVersion=0,notice=0;
    float mouth=0;bool quiet=false,hideFullscreen=true,fullscreen=false,pendingChat=false;
    int restarts=0;bool enabled=false,shutdownForced=false;std::string error;
    static std::wstring quote(const fs::path& path) {return L"\""+path.wstring()+L"\"";}
    static std::string readText(const fs::path& path) {auto file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);if(file==INVALID_HANDLE_VALUE)return {};char bytes[2048];DWORD count=0;bool ok=ReadFile(file,bytes,sizeof(bytes),&count,nullptr)!=FALSE;CloseHandle(file);return ok?std::string(bytes,count):std::string();}
    static HANDLE job() {auto h=CreateJobObjectW(nullptr,nullptr);if(h) {JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{};info.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;SetInformationJobObject(h,JobObjectExtendedLimitInformation,&info,sizeof(info));}return h;}
    HANDLE launch(const fs::path& exe,std::wstring args,HANDLE group,bool visible) {
        STARTUPINFOW si{sizeof(si)};si.dwFlags=STARTF_USESHOWWINDOW;si.wShowWindow=visible?SW_SHOWNORMAL:SW_HIDE;
        PROCESS_INFORMATION pi{};auto command=quote(exe)+L" "+args;
        if(!CreateProcessW(exe.c_str(),command.data(),nullptr,nullptr,FALSE,CREATE_SUSPENDED|(visible?0:CREATE_NO_WINDOW),nullptr,folder.c_str(),&si,&pi)) {error="Child start failed: "+std::to_string(GetLastError());return nullptr;}
        if(!group||!AssignProcessToJobObject(group,pi.hProcess)) {TerminateProcess(pi.hProcess,1);CloseHandle(pi.hThread);CloseHandle(pi.hProcess);error="Cannot contain companion process";return nullptr;}
        ResumeThread(pi.hThread);CloseHandle(pi.hThread);return pi.hProcess;
    }
    void start() {
        auto python=folder/L"python/python.exe",script=folder/L"companion/service.py";
        if(!fs::is_regular_file(python))python=folder.parent_path().parent_path()/L".tools/python-3.13.13-embed/python.exe";
        if(!fs::is_regular_file(script))script=folder.parent_path().parent_path()/L"app/companion/service.py";
        if(!fs::is_regular_file(python)||!fs::is_regular_file(script)) {error="Companion runtime unavailable";retryAfter=now()+60;return;}
        fs::create_directories(data);
        if(serviceJob) {TerminateJobObject(serviceJob,0);CloseHandle(serviceJob);}serviceJob=job();
        // The desktop mutex owns this profile. Never publish the previous session
        // while the new child is still initializing.
        std::error_code ec;fs::remove(data/L"quit-request",ec);fs::remove(data/L"connection.txt",ec);
        process=launch(python,L"-u "+quote(script)+L" --data "+quote(data)+L" --app-root "+quote(folder),serviceJob,false);
        retryAfter=now()+30;if(process)++restarts;
    }
    void init(const fs::path& app,const fs::path& config,HWND hwnd,bool on) {folder=app;data=config/L"companion";owner=hwnd;enabled=on;if(enabled)start();}
    bool alive() const {return process&&WaitForSingleObject(process,0)==WAIT_TIMEOUT;}
    void clearSession(DWORD pid) {
        std::istringstream file(readText(data/L"connection.txt"));int port=0;std::string token;DWORD ownerPid=0;
        std::error_code ec;if(file>>port>>token>>ownerPid&&ownerPid==pid)fs::remove(data/L"connection.txt",ec);
        fs::remove(data/L"quit-request",ec);
    }
    bool connection(std::wstring& url) {
        if(!alive())return false;std::istringstream file(readText(data/L"connection.txt"));int port=0;std::string token;DWORD pid=0;
        if(!(file>>port>>token>>pid)||pid!=GetProcessId(process)||port<1024||port>65535||token.size()<32||token.size()>128||token.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)return false;
        url=L"http://127.0.0.1:"+std::to_wstring(port)+L"/#token="+wide(token);return true;
    }
    void openChat() {
        std::wstring url;if(!connection(url)) {pendingChat=true;if(!alive()&&now()>=retryAfter)start();return;}
        pendingChat=false;auto exe=folder/L"CompanionChat.exe";if(!fs::is_regular_file(exe)) {error="Chat host unavailable";return;}
        if(!chatJob)chatJob=job();auto child=launch(exe,L"--url="+quote(url)+L" --data="+quote(data),chatJob,true);if(child)CloseHandle(child);
    }
    static LRESULT CALLBACK bubbleProc(HWND h,UINT msg,WPARAM wp,LPARAM lp) {
        auto p=reinterpret_cast<CompanionLink*>(GetWindowLongPtrW(h,GWLP_USERDATA));
        if(msg==WM_NCCREATE) {p=static_cast<CompanionLink*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(p));}
        if(msg==WM_NCHITTEST)return HTTRANSPARENT;if(msg==WM_MOUSEACTIVATE)return MA_NOACTIVATE;
        if(msg==WM_ERASEBKGND)return 1;
        if(msg==WM_PAINT&&p) {PAINTSTRUCT ps;auto dc=BeginPaint(h,&ps);RECT r{};GetClientRect(h,&r);auto mem=CreateCompatibleDC(dc);auto bmp=CreateCompatibleBitmap(dc,r.right,r.bottom);auto old=SelectObject(mem,bmp);auto brush=CreateSolidBrush(RGB(248,250,253));FillRect(mem,&r,brush);DeleteObject(brush);auto pen=CreatePen(PS_SOLID,1,RGB(207,220,232));auto op=SelectObject(mem,pen);auto ob=SelectObject(mem,GetStockObject(NULL_BRUSH));RoundRect(mem,0,0,r.right,r.bottom,16,16);SelectObject(mem,ob);SelectObject(mem,op);DeleteObject(pen);auto font=CreateFontW(-MulDiv(14,GetDpiForWindow(h),96),0,0,0,FW_NORMAL,FALSE,FALSE,FALSE,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Microsoft JhengHei UI");auto of=SelectObject(mem,font);SetBkMode(mem,TRANSPARENT);SetTextColor(mem,RGB(47,62,82));InflateRect(&r,-14,-12);DrawTextW(mem,p->bubbleText.c_str(),-1,&r,DT_WORDBREAK|DT_END_ELLIPSIS|DT_NOPREFIX);SelectObject(mem,of);DeleteObject(font);GetClientRect(h,&r);BitBlt(dc,0,0,r.right,r.bottom,mem,0,0,SRCCOPY);SelectObject(mem,old);DeleteObject(bmp);DeleteDC(mem);EndPaint(h,&ps);return 0;}
        return DefWindowProcW(h,msg,wp,lp);
    }
    void say(const std::wstring& text,int px,int py,int width,int height) {
        if(quiet||fullscreen||text.empty())return;
        if(!bubble) {WNDCLASSW wc{};wc.lpfnWndProc=bubbleProc;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"DesktopWaifu.Bubble.v03";RegisterClassW(&wc);bubble=CreateWindowExW(WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE|WS_EX_TRANSPARENT,wc.lpszClassName,L"灰姑娘 · 回應",WS_POPUP,0,0,320,96,owner,nullptr,wc.hInstance,this);}
        bubbleText=text.substr(0,140);bubbleUntil=now()+7;int dpi=GetDpiForWindow(owner),bw=MulDiv(320,dpi,96),bh=MulDiv(text.size()>60?126:96,dpi,96);RECT target{px,py,px+width,py+height};MONITORINFO monitor{sizeof(monitor)};GetMonitorInfoW(MonitorFromRect(&target,MONITOR_DEFAULTTONEAREST),&monitor);auto r=monitor.rcWork;int bx=std::clamp<int>(px+(width-bw)/2,r.left,std::max(r.left,r.right-bw)),by=std::clamp<int>(py-bh-8,r.top,std::max(r.top,r.bottom-bh));SetWindowPos(bubble,HWND_TOPMOST,bx,by,bw,bh,SWP_NOACTIVATE|SWP_SHOWWINDOW);InvalidateRect(bubble,nullptr,FALSE);
    }
    bool isFullscreen() {
        HWND foreground=GetForegroundWindow();if(!foreground||foreground==owner||foreground==bubble||IsIconic(foreground))return false;
        wchar_t cls[256]{};GetClassNameW(foreground,cls,256);std::wstring name=cls;
        if(name.rfind(L"DesktopWaifu.",0)==0||name==L"Progman"||name==L"WorkerW"||name==L"Shell_TrayWnd")return false;
        RECT rect{};if(!GetWindowRect(foreground,&rect))return false;MONITORINFO monitor{sizeof(monitor)};GetMonitorInfoW(MonitorFromWindow(foreground,MONITOR_DEFAULTTONEAREST),&monitor);auto r=monitor.rcMonitor;
        return rect.left<=r.left&&rect.top<=r.top&&rect.right>=r.right&&rect.bottom>=r.bottom;
    }
    void tick(int px,int py,int width,int height) {
        if(!enabled)return;
        const double t=now();if(bubble&&IsWindowVisible(bubble)&&(t>bubbleUntil||quiet||fullscreen))ShowWindow(bubble,SW_HIDE);
        if(t<nextPoll)return;nextPoll=t+.1;
        if(process&&!alive()) {clearSession(GetProcessId(process));CloseHandle(process);process=nullptr;activity=0;mouth=0;emotion=-1;emotionVersion=0;quiet=false;}
        if(!process&&t>=retryAfter)start();if(pendingChat&&alive())openChat();
        fullscreen=isFullscreen();if(t>=nextPresence&&alive()) {nextPresence=t+2;LASTINPUTINFO input{sizeof(input)};GetLastInputInfo(&input);auto temp=data/L"presence.tmp";{std::ofstream f(temp);f<<std::time(nullptr)<<' '<<fullscreen<<' '<<DWORD(GetTickCount()-input.dwTime)/1000.;}MoveFileExW(temp.c_str(),(data/L"presence.txt").c_str(),MOVEFILE_REPLACE_EXISTING);}
        std::istringstream file(readText(data/L"desktop-state.txt"));double stamp=0,value=0;int act=0,em=-1,q=0,hide=1;unsigned long long version=0,id=0;
        if(file>>stamp>>act>>em>>version>>value>>q>>id>>hide&&alive()&&std::isfinite(stamp)&&std::abs(double(std::time(nullptr))-stamp)<3&&std::isfinite(value)&&act>=0&&act<=5&&em>=-1&&em<10) {
            activity=act;emotion=em;emotionVersion=version;mouth=float(std::clamp(value,0.,1.));quiet=q!=0;hideFullscreen=hide!=0;
            std::string line;std::getline(file,line);std::getline(file,line);if(id!=notice) {notice=id;say(wide(line.substr(0,600)),px,py,width,height);}
        }else {activity=0;mouth=0;emotion=-1;}
    }
    void stop() {
        enabled=false;pendingChat=false;
        // Give our chat host WM_CLOSE before closing its job so window settings
        // and WebView2 can be saved instead of terminating them immediately.
        if(chatJob) {
            EnumWindows([](HWND hwnd,LPARAM value)->BOOL {
                DWORD pid=0;GetWindowThreadProcessId(hwnd,&pid);auto child=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,pid);
                if(child) {BOOL owned=FALSE;if(IsProcessInJob(child,reinterpret_cast<HANDLE>(value),&owned)&&owned)PostMessageW(hwnd,WM_CLOSE,0,0);CloseHandle(child);}return TRUE;
            },reinterpret_cast<LPARAM>(chatJob));
            const auto deadline=GetTickCount64()+2000;
            while(GetTickCount64()<deadline) {JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info{};if(!QueryInformationJobObject(chatJob,JobObjectBasicAccountingInformation,&info,sizeof(info),nullptr)||!info.ActiveProcesses)break;Sleep(20);}
            CloseHandle(chatJob);chatJob=nullptr;
        }
        if(process) {
            const DWORD pid=GetProcessId(process);
            try {std::ofstream file(data/L"quit-request");file<<"quit";}catch(...){}
            // Model shutdown and an in-flight request may need several seconds.
            // Only our contained service is terminated if graceful exit stalls.
            if(WaitForSingleObject(process,12000)!=WAIT_OBJECT_0) {shutdownForced=true;if(serviceJob)TerminateJobObject(serviceJob,1);}
            if(WaitForSingleObject(process,3000)==WAIT_OBJECT_0)clearSession(pid);
            CloseHandle(process);process=nullptr;
        }
        if(serviceJob){CloseHandle(serviceJob);serviceJob=nullptr;}if(bubble){DestroyWindow(bubble);bubble=nullptr;}
    }
    ~CompanionLink() {stop();}
};
