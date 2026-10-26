#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <windowsx.h>
#include <shellapi.h>
#include <shlobj.h>
#include <psapi.h>
#include <mmsystem.h>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include "view_controls.h"

namespace fs = std::filesystem;
constexpr wchar_t ClassName[] = L"DesktopWaifu.Cinderella.v01";
constexpr UINT TrayMessage = WM_APP + 1, RestoreMessage = WM_APP + 2, ChatMessage = WM_APP + 3;
enum Command { Interact=100, Pass, Top, Rest, Smaller, Larger, Reset, Help, Quit,
    Tracking, FitScreen, FullBody, Detail2, Detail3, Detail4, Snapshot, OpenCaptures,
    ExpressionReset, MenuZoom, MenuFavorite, MenuTab, Chat, ExpressionHold,
    ExpressionFirst=200, ExpressionLast=209, SaveViewFirst=400, LoadViewFirst=410 };
const wchar_t* ExpressionLabels[]={L"平常",L"微笑",L"喜悅",L"驚訝",L"悲傷",L"生氣",L"憤怒",L"害怕",L"疼痛",L"空虛"};
double now() { static LARGE_INTEGER f=[] { LARGE_INTEGER v; QueryPerformanceFrequency(&v); return v; }(); LARGE_INTEGER v; QueryPerformanceCounter(&v); return double(v.QuadPart)/f.QuadPart; }
std::string utf8(const std::wstring& s) { if(s.empty()) return {}; int n=WideCharToMultiByte(CP_UTF8,0,s.data(),int(s.size()),nullptr,0,nullptr,nullptr); std::string r(n,0); WideCharToMultiByte(CP_UTF8,0,s.data(),int(s.size()),r.data(),n,nullptr,nullptr); return r; }
std::wstring wide(const std::string& s) { if(s.empty()) return {}; int n=MultiByteToWideChar(CP_UTF8,0,s.data(),int(s.size()),nullptr,0); std::wstring r(n,0); MultiByteToWideChar(CP_UTF8,0,s.data(),int(s.size()),r.data(),n); return r; }
std::string quoted(const std::string& s) { std::string r="\""; for(unsigned char c:s) { if(c=='\\'||c=='\"') r+='\\'; if(c>=32) r+=c; } return r+'\"'; }
uint64_t filetime(FILETIME v) { return (uint64_t(v.dwHighDateTime)<<32)|v.dwLowDateTime; }
#include "pet_menu.h"
#include "companion.h"

struct ProfileLease {
    HANDLE handle=INVALID_HANDLE_VALUE;
    void acquire(const fs::path& data) {
        fs::create_directories(data);
        handle=CreateFileW((data/L"profile.lock").c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(handle==INVALID_HANDLE_VALUE) {
            const auto code=GetLastError();
            if(code==ERROR_SHARING_VIOLATION||code==ERROR_LOCK_VIOLATION)
                throw std::runtime_error("資料正在備份或還原，請等作業完成後再啟動桌寵。");
            throw std::runtime_error("Cannot acquire profile lifetime lease (Windows error "+std::to_string(code)+")");
        }
    }
    ~ProfileLease() {if(handle!=INVALID_HANDLE_VALUE)CloseHandle(handle);}
};

struct Renderer {
    HMODULE module{};
    int (*init)(const char*,const char*);
    const unsigned char* (*render)(int,int,double,float,float,float,float,int);
    const char* (*error)(); const char* (*info)(); double (*aspect)();
    void (*reset)(); int (*react)(int); int (*capture)(const char*);
    int (*setView)(float,float,float); int (*eyePosition)(float*,float*); int (*expression)(int);
    void (*hold)(int);void (*mouth)(float);
    template<class T> void bind(T& fn,const char* name) { fn=reinterpret_cast<T>(GetProcAddress(module,name)); if(!fn) throw std::runtime_error(std::string("Missing renderer function: ")+name); }
    void load(const fs::path& folder,const fs::path& model) {
        module=LoadLibraryExW((folder/L"cinderella_renderer.dll").c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if(!module) throw std::runtime_error("Cannot load cinderella_renderer.dll (Windows error "+std::to_string(GetLastError())+")");
        bind(init,"dw_init"); bind(render,"dw_render"); bind(error,"dw_error"); bind(info,"dw_info"); bind(aspect,"dw_aspect"); bind(reset,"dw_reset"); bind(react,"dw_react"); bind(capture,"dw_capture");
        bind(setView,"dw_set_view"); bind(eyePosition,"dw_eye_position"); bind(expression,"dw_expression");
        bind(hold,"dw_hold");bind(mouth,"dw_mouth");
        if(!init(utf8(model.wstring()).c_str(),utf8((folder/L"SDL2.dll").wstring()).c_str())) throw std::runtime_error(error());
    }
};

struct Pet {
    Renderer sdk;
    PetMenu panel;
    // Declared before CompanionLink, so it outlives service shutdown/destruction.
    ProfileLease profileLease;
    CompanionLink companion;
    HWND window{}; HDC memory{}; HBITMAP bitmap{},oldBitmap{}; void* dib{}; HICON icon{};
    NOTIFYICONDATAW tray{}; UINT taskbarCreated{};
    fs::path folder,configDir,ini,report,capture;
    std::wstring className;
    desktop::View view,panOrigin;
    float eyeX=.58f,eyeY=.16f;
    double started=now(),lastFrame=0,lastActivity=now(),reactionUntil=0,duration=0,nextReport=0;
    int x=0,y=0,width=380,height=600,heightDip=600,frameCount=0,errors=0,dragCount=0,clickCount=0;
    int desiredFPS=30,minObservedFPS=100,maxObservedFPS=0; double intervalStart=now(); int intervalFrames=0;
    double maxCPU=0,maxFrameMs=0,totalFrameMs=0; SIZE_T maxMemory=0;
    double motionX=0,motionY=0,previousMove=now(),lastCPUWall=now(); uint64_t lastCPU=0;
    POINT down{},origin{},lastPosition{};
    POINT panDown{};
    bool downHeld=false,dragging=false,pass=false,top=true,rest=false,selfTest=false,uiTest=false,quitting=false;
    bool tracking=true,panning=false,startFit=false;
    bool menuPreview=false,menuTest=false,menuTestDone=false,testMenu=false;
    fs::path menuCaptures;
    int favoriteExpressions=14,menuTab=0;
    struct SavedView {bool valid=false;int height=540;desktop::View view;};
    std::array<SavedView,3> savedViews;
    double startZoom=0;
    int selectedExpression=-1;
    bool expressionHold=false,noCompanion=false,fullscreenHidden=false,aiExpression=false,openChatInitially=false;
    unsigned long long lastEmotionVersion=0;double overrideUntil=0,pressedAt=0;
    bool testAlpha=false,testPass=false,testScale=false,testClamp=false,testCooldown=false,testDrag=false,hotkeys[3]{};
    bool testView=false,testPan=false,testGaze=false,testExpression=false;
    bool suspended=false,recoveryTest=false,recoveryTestDone=false;
    bool testNativeNames=false,testSliderNames=false,testKeyboard=false,testSuspendDrag=false,testSuspendPan=false,testSuspendSlider=false,testResume=false;
    int suspendCount=0,resumeCount=0;
    int testPhase=0; std::string failure;
    bool settingsDirty=false;double saveAfter=0;

    RECT workAt(RECT rect) { MONITORINFO m{sizeof(m)}; GetMonitorInfoW(MonitorFromRect(&rect,MONITOR_DEFAULTTONEAREST),&m); return m.rcWork; }
    RECT work() { return workAt({x,y,x+width,y+height}); }
    int dpiAt(int px,int py) { using Fn=HRESULT(WINAPI*)(HMONITOR,int,UINT*,UINT*); static HMODULE sh=LoadLibraryW(L"shcore.dll"); static Fn fn=sh?reinterpret_cast<Fn>(GetProcAddress(sh,"GetDpiForMonitor")):nullptr; POINT p{px,py}; UINT dx=96,dy=96; if(fn) fn(MonitorFromPoint(p,MONITOR_DEFAULTTONEAREST),0,&dx,&dy); return int(dx); }
    void save(bool force=false) {
        if(ini.empty()) return;
        if(!force) {settingsDirty=true;saveAfter=now()+.25;return;}
        settingsDirty=false;
        auto put=[&](const wchar_t* k,int v) { WritePrivateProfileStringW(L"DesktopWaifu",k,std::to_wstring(v).c_str(),ini.c_str()); };
        put(L"x",x); put(L"y",y); put(L"heightDip",heightDip); put(L"topmost",top); put(L"passthrough",pass); put(L"rest",rest);
        put(L"tracking",tracking); put(L"zoomPermille",int(std::lround(view.zoom*1000))); put(L"panXPermille",int(std::lround(view.panX*1000))); put(L"panYPermille",int(std::lround(view.panY*1000)));
        put(L"favoriteExpressions",favoriteExpressions);put(L"menuTab",menuTab);
        put(L"expressionHold",expressionHold);
        for(int i=0;i<3;i++) {auto prefix=L"view"+std::to_wstring(i+1);auto& s=savedViews[i];put((prefix+L"Valid").c_str(),s.valid);put((prefix+L"Height").c_str(),s.height);put((prefix+L"Zoom").c_str(),int(std::lround(s.view.zoom*1000)));put((prefix+L"X").c_str(),int(std::lround(s.view.panX*1000)));put((prefix+L"Y").c_str(),int(std::lround(s.view.panY*1000)));}
    }
    void resetMotion() { motionX=motionY=0; previousMove=now(); lastPosition={x,y}; sdk.reset(); }
    void powerState(WPARAM event) {
        if(event!=PBT_APMSUSPEND&&event!=PBT_APMRESUMEAUTOMATIC&&event!=PBT_APMRESUMESUSPEND) return;
        suspended=event==PBT_APMSUSPEND;
        if(suspended) ++suspendCount;else ++resumeCount;
        panel.hide();
        // Cancel the gesture before releasing capture: WM_CAPTURECHANGED must
        // never turn a pre-sleep drag into a click or continue panning on wake.
        downHeld=dragging=panning=false;
        const HWND captured=GetCapture();
        if(captured==window||captured==panel.window||(panel.window&&IsChild(panel.window,captured))) ReleaseCapture();
        if(companion.bubble) ShowWindow(companion.bubble,SW_HIDE);
        move(true,true);lastFrame=0;lastActivity=now();save();
        if(!suspended) {companion.nextPoll=0;companion.nextPresence=0;}
    }
    void move(bool constrain=true,bool teleport=false) {
        if(constrain) { auto r=work(); x=std::clamp<int>(x,r.left,std::max(r.left,r.right-width)); y=std::clamp<int>(y,r.top,std::max(r.top,r.bottom-height)); }
        SetWindowPos(window,top?HWND_TOPMOST:HWND_NOTOPMOST,x,y,width,height,SWP_NOACTIVATE);
        if(teleport) resetMotion();
    }
    void resize(int dip,bool keepCenter=true) {
        int oldW=width,oldH=height; heightDip=std::clamp(dip,desktop::MinHeightDip,desktop::MaxHeightDip);
        auto r=work(); height=std::min<int>({MulDiv(heightDip,dpiAt(x,y),96),r.bottom-r.top-16,int((r.right-r.left-16)/sdk.aspect()),2048}); height=std::max(64,height);
        width=std::max(120,int(height*sdk.aspect()+0.5)); width=std::min<int>(width,r.right-r.left);
        if(keepCenter) { x+=(oldW-width)/2; y+=oldH-height; }
        if(bitmap) { SelectObject(memory,oldBitmap); DeleteObject(bitmap); bitmap=nullptr; }
        BITMAPINFO b{}; b.bmiHeader.biSize=sizeof(BITMAPINFOHEADER); b.bmiHeader.biWidth=width; b.bmiHeader.biHeight=-height; b.bmiHeader.biPlanes=1; b.bmiHeader.biBitCount=32; b.bmiHeader.biCompression=BI_RGB;
        bitmap=CreateDIBSection(memory,&b,DIB_RGB_COLORS,&dib,nullptr,0);
        if(!bitmap) throw std::runtime_error("Cannot allocate desktop alpha surface");
        auto previous=SelectObject(memory,bitmap); if(!oldBitmap) oldBitmap=static_cast<HBITMAP>(previous);
        move(true,true); lastActivity=now(); save();
    }
    void resetPosition() { RECT r{}; POINT p{0,0}; MONITORINFO m{sizeof(m)}; GetMonitorInfoW(MonitorFromPoint(p,MONITOR_DEFAULTTOPRIMARY),&m); r=m.rcWork; x=r.right-width-32; y=r.bottom-height-24; move(true,true); save(); lastActivity=now(); }
    void setPass(bool enabled) { pass=enabled; auto style=GetWindowLongPtrW(window,GWL_EXSTYLE); SetWindowLongPtrW(window,GWL_EXSTYLE,enabled?(style|WS_EX_TRANSPARENT):(style&~WS_EX_TRANSPARENT)); SetWindowPos(window,nullptr,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED); if(enabled && (downHeld||panning)) { downHeld=dragging=panning=false; ReleaseCapture(); } updateTray(); save(); }
    void setView(desktop::View value) {
        view=desktop::constrain(value);
        if(!sdk.setView(float(view.zoom),float(view.panX),float(view.panY))) throw std::runtime_error(sdk.error());
        lastActivity=now(); save();
    }
    void detail(double zoom) {
        double baseX=.5+(eyeX-.5-view.panX)/view.zoom,baseY=.5+(eyeY-.5-view.panY)/view.zoom;
        setView({zoom,-zoom*(baseX-.5),-.2-zoom*(baseY-.5)});
    }
    void wheel(int delta,POINT pt,bool detailOnly) {
        if(!delta) return;
        double factor=std::pow(1.1,delta/double(WHEEL_DELTA));
        auto r=work(); bool screenFit=height>=r.bottom-r.top-18 || width>=r.right-r.left-18 || height>=2048;
        if(detailOnly || view.zoom>1.001 || (delta>0&&(screenFit||heightDip>=desktop::MaxHeightDip))) {
            setView(desktop::zoomAt(view,view.zoom*factor,{double(pt.x-x)/width,double(pt.y-y)/height}));
        } else resize(int(std::lround(heightDip*factor)));
    }
    void panStart(POINT pt) { panning=true; panDown=pt; panOrigin=view; SetCapture(window); lastActivity=now(); }
    void panMove(POINT pt) { if(panning) setView({panOrigin.zoom,panOrigin.panX+double(pt.x-panDown.x)/width,panOrigin.panY+double(pt.y-panDown.y)/height}); }
    void panStop() { if(panning) { panning=false;ReleaseCapture();save(); } }
    void snapshot() {
        auto dir=configDir/L"captures";fs::create_directories(dir);SYSTEMTIME t;GetLocalTime(&t);wchar_t name[96];
        swprintf_s(name,L"Cinderella-%04u%02u%02u-%02u%02u%02u-%03u.png",t.wYear,t.wMonth,t.wDay,t.wHour,t.wMinute,t.wSecond,t.wMilliseconds);
        if(!sdk.capture(utf8((dir/name).wstring()).c_str())) throw std::runtime_error(sdk.error());
        if(!menuTest) {tray.uFlags=NIF_INFO;wcscpy_s(tray.szInfoTitle,L"透明截圖已保存");wcscpy_s(tray.szInfo,L"可從右鍵選單開啟截圖資料夾。");tray.dwInfoFlags=NIIF_INFO;Shell_NotifyIconW(NIM_MODIFY,&tray);}
    }
    void updateTray(bool add=false) {
        tray.cbSize=sizeof(tray); tray.hWnd=window; tray.uID=1; tray.uFlags=NIF_MESSAGE|NIF_ICON|NIF_TIP|NIF_SHOWTIP; tray.uCallbackMessage=TrayMessage; tray.hIcon=icon;
        std::wstring tip=L"桌面灰姑娘 · "; tip+=pass?L"點擊穿透":rest?L"休息中":L"互動模式";
        wcscpy_s(tray.szTip,tip.c_str()); Shell_NotifyIconW(add?NIM_ADD:NIM_MODIFY,&tray);
        if(add) { tray.uVersion=NOTIFYICON_VERSION_4; Shell_NotifyIconW(NIM_SETVERSION,&tray); }
    }
    void command(int id) {
        lastActivity=now();
        if(id>=SaveViewFirst&&id<SaveViewFirst+3) {savedViews[id-SaveViewFirst]={true,heightDip,view};save();return;}
        if(id>=LoadViewFirst&&id<LoadViewFirst+3) {auto s=savedViews[id-LoadViewFirst];if(s.valid) {resize(s.height);setView(s.view);}return;}
        if(id>=ExpressionFirst&&id<=ExpressionLast) {
            aiExpression=false;overrideUntil=now()+8;sdk.hold(expressionHold);
            selectedExpression=id-ExpressionFirst;
            if(!sdk.expression(selectedExpression)) throw std::runtime_error(sdk.error());
            reactionUntil=now()+8;return;
        }
        switch(id) {
        case Chat:panel.hide();companion.openChat();break;
        case ExpressionHold:expressionHold=!expressionHold;sdk.hold(expressionHold);if(selectedExpression>=0) {aiExpression=false;overrideUntil=now()+8;sdk.expression(selectedExpression);reactionUntil=now()+8;}break;
        case Interact: setPass(false); rest=false; updateTray(); break;
        case Pass: setPass(!pass); break;
        case Top: top=!top; move(); break;
        case Rest: rest=!rest; updateTray(); break;
        case Smaller: {POINT pt{x+width/2,y+height/2};wheel(-WHEEL_DELTA,pt,false);break;}
        case Larger: {POINT pt{x+width/2,y+height/2};wheel(WHEEL_DELTA,pt,false);break;}
        case Tracking: tracking=!tracking;break;
        case FitScreen: setView({});resize(desktop::MaxHeightDip);break;
        case FullBody: setView({});break;
        case Detail2: detail(2);break;
        case Detail3: detail(3);break;
        case Detail4: detail(4);break;
        case Snapshot: snapshot();break;
        case OpenCaptures: {auto dir=configDir/L"captures";fs::create_directories(dir);ShellExecuteW(window,L"open",dir.c_str(),nullptr,nullptr,SW_SHOWNORMAL);break;}
        case ExpressionReset: sdk.expression(-1);selectedExpression=-1;reactionUntil=0;aiExpression=false;overrideUntil=now()+8;break;
        case Reset: setPass(false);setView({});sdk.expression(-1);selectedExpression=-1;aiExpression=false;overrideUntil=now()+8;reactionUntil=0;resetPosition();break;
        case Help: MessageBoxW(window,L"左鍵拖曳角色：移動桌面位置\n滾輪：調整大小，滿螢幕後繼續放大細節\nCtrl + 滾輪：直接縮放細節（1～4 倍）\n按住滑鼠中鍵拖曳：平移放大後的畫面\n\n右鍵角色／系統匣：表情、視線開關、放大預設、透明截圖\n表情播放約 8 秒，可選擇保持；動畫保留原作視線。\n點頭部／長按輕撫／點身體：觸摸回應（冷卻 1.8 秒）\n選單「聊聊」：離線聊天、記憶、提醒、按住說話與設定\n\nCtrl + Alt + P：切換點擊穿透\nCtrl + Alt + Home：恢復全身、位置及互動\nCtrl + Alt + Q：退出\n\n穿透後可按系統匣圖示恢復互動。\n位置、大小、細節縮放與視線開關會自動保存。\n快捷鍵衝突時可使用系統匣選單。",L"桌面灰姑娘 v0.3.1 · 操作說明",MB_OK|MB_ICONINFORMATION); break;
        case Quit: panel.hide();DestroyWindow(window); break;
        } save();
    }
    PetMenu::State menuState() {
        PetMenu::State s;s.hold=expressionHold;s.tracking=tracking;s.top=top;s.rest=rest;s.pass=pass;s.expression=(now()<reactionUntil||expressionHold&&!aiExpression)?selectedExpression:-1;s.favorites=favoriteExpressions;s.tab=menuTab;s.height=heightDip;s.zoom=view.zoom;
        for(int i=0;i<3;i++) s.saved[i]=savedViews[i].valid;return s;
    }
    void menuAction(int id,double value) {
        if(id==MenuZoom) setView(desktop::zoomAt(view,value,{.5,.3}));
        else if(id==MenuFavorite) {int index=int(value);if(index>=0&&index<10) favoriteExpressions^=1<<index;save();}
        else if(id==MenuTab) {menuTab=std::clamp(int(value),0,3);save();}
        else command(id);
        if(!quitting) panel.update(menuState());
    }
    void menu(POINT pt) {
        if(GetKeyState(VK_SHIFT)&0x8000) nativeMenu(pt);
        else panel.show(pt,menuState());
    }
    void nativeMenu(POINT pt) {
        HMENU m=CreatePopupMenu();
        AppendMenuW(m,MF_STRING,Chat,L"聊天、記憶與提醒…");AppendMenuW(m,MF_SEPARATOR,0,nullptr);
        AppendMenuW(m,MF_STRING|(!pass?MF_CHECKED:0),Interact,L"恢復互動"); AppendMenuW(m,MF_STRING|(pass?MF_CHECKED:0),Pass,L"點擊穿透\tCtrl+Alt+P");
        AppendMenuW(m,MF_STRING|(top?MF_CHECKED:0),Top,L"保持置頂"); AppendMenuW(m,MF_STRING|(rest?MF_CHECKED:0),Rest,L"休息／省資源");
        AppendMenuW(m,MF_STRING|(tracking?MF_CHECKED:0),Tracking,L"眼睛跟隨游標");
        HMENU expressions=CreatePopupMenu();
        for(int i=0;i<10;i++) AppendMenuW(expressions,MF_STRING|(i==selectedExpression&&now()<reactionUntil?MF_CHECKED:0),ExpressionFirst+i,ExpressionLabels[i]);
        AppendMenuW(expressions,MF_STRING|(expressionHold?MF_CHECKED:0),ExpressionHold,L"保持選擇的表情");
        AppendMenuW(expressions,MF_SEPARATOR,0,nullptr);AppendMenuW(expressions,MF_STRING,ExpressionReset,L"恢復預設表情");AppendMenuW(m,MF_POPUP,reinterpret_cast<UINT_PTR>(expressions),L"表情（播放約 8 秒）");
        HMENU zoom=CreatePopupMenu();AppendMenuW(zoom,MF_STRING,Smaller,L"縮小\t滾輪向下");AppendMenuW(zoom,MF_STRING,Larger,L"放大\t滾輪向上");AppendMenuW(zoom,MF_STRING,FitScreen,L"全身放大至螢幕");
        AppendMenuW(zoom,MF_SEPARATOR,0,nullptr);AppendMenuW(zoom,MF_STRING,Detail2,L"臉部細節 2 倍");AppendMenuW(zoom,MF_STRING,Detail3,L"臉部細節 3 倍");AppendMenuW(zoom,MF_STRING,Detail4,L"臉部細節 4 倍");AppendMenuW(zoom,MF_STRING,FullBody,L"恢復全身視角");AppendMenuW(m,MF_POPUP,reinterpret_cast<UINT_PTR>(zoom),L"大小與細節縮放");
        AppendMenuW(m,MF_SEPARATOR,0,nullptr);AppendMenuW(m,MF_STRING,Snapshot,L"保存透明 PNG 截圖");AppendMenuW(m,MF_STRING,OpenCaptures,L"開啟截圖資料夾");AppendMenuW(m,MF_STRING,Reset,L"恢復全身、位置及互動\tCtrl+Alt+Home");
        AppendMenuW(m,MF_SEPARATOR,0,nullptr); AppendMenuW(m,MF_STRING,Help,L"操作說明"); AppendMenuW(m,MF_STRING,Quit,L"退出\tCtrl+Alt+Q");
        SetForegroundWindow(window); int id=TrackPopupMenu(m,TPM_RETURNCMD|TPM_RIGHTBUTTON,pt.x,pt.y,0,window,nullptr); DestroyMenu(m); PostMessageW(window,WM_NULL,0,0); if(id) command(id);
    }
    void pointerDown(POINT pt) { downHeld=true; dragging=false; down=pt; origin={x,y}; SetCapture(window); pressedAt=now();lastActivity=now(); }
    void pointerMove(POINT pt) {
        if(!downHeld) return; int dx=pt.x-down.x,dy=pt.y-down.y;
        if(!dragging && (abs(dx)>5||abs(dy)>5)) { dragging=true; ++dragCount;overrideUntil=now()+8;if(aiExpression){sdk.expression(-1);aiExpression=false;reactionUntil=0;selectedExpression=-1;} }
        if(dragging) {
            int nx=origin.x+dx,ny=origin.y+dy; double t=now(),dt=std::max(0.01,t-previousMove); double vx=(nx-x)/dt/1600,vy=(ny-y)/dt/1600;
            motionX=std::clamp(motionX*.65+vx*.35,-.4,.4); motionY=std::clamp(motionY*.65+vy*.35,-.4,.4); previousMove=t; x=nx; y=ny; move(false); lastActivity=t;
        }
    }
    void pointerUp(POINT pt) {
        if(!downHeld) return; bool wasDrag=dragging; downHeld=dragging=false; ReleaseCapture();
        if(wasDrag) { move(true); save(); }
        else {bool head=pt.y-y < eyeY*height+height*.11*view.zoom;bool stroke=head&&now()-pressedAt>.65;sdk.hold(false);if(sdk.react(head?0:1)) {++clickCount;selectedExpression=-1;aiExpression=false;overrideUntil=now()+8;reactionUntil=now()+1.8;companion.say(stroke?L"這樣輕輕的，很舒服。":head?L"我在這裡。今天也照自己的步調來吧。":L"嗯？有什麼想聊的嗎？",x,y,width,height);} }
        lastActivity=now();
    }
    bool solid(int px,int py) { return dib && px>=0&&py>=0&&px<width&&py<height&&static_cast<unsigned char*>(dib)[(size_t(py)*width+px)*4+3]>=8; }
    void checkMenu() {
        if(menuTestDone) return;menuTestDone=true;
        fs::create_directories(menuCaptures);panel.show({x+width/2,y+height/3},menuState());bool valid=true;
        for(int tab=0;tab<4;tab++) {panel.activate(PetMenu::TabFirst+tab);valid=valid&&panel.controlsWithinBounds()&&panel.capture(menuCaptures/(L"menu-"+std::to_wstring(tab)+L".png"));}
        for(int percent:{100,150,200}) {panel.diagnosticScale(percent/100.f);for(int tab=0;tab<4;tab++) {panel.activate(PetMenu::TabFirst+tab);valid=valid&&panel.controlsWithinBounds()&&panel.capture(menuCaptures/(L"menu-"+std::to_wstring(tab)+L"-"+std::to_wstring(percent)+L".png"));}}panel.diagnosticScale(1);
        valid=panel.renderingRegression(menuCaptures/L"painting")&&valid;
        panel.activate(PetMenu::TabFirst);SetFocus(GetDlgItem(panel.window,ExpressionFirst+3));MSG enter{};enter.hwnd=GetFocus();enter.message=WM_KEYDOWN;enter.wParam=VK_RETURN;panel.dialogMessage(enter);valid=valid&&selectedExpression==3;
        int oldFavorites=favoriteExpressions;panel.activate(PetMenu::Favorite);valid=valid&&favoriteExpressions==(oldFavorites^(1<<3));panel.activate(PetMenu::FavoriteFilter);panel.activate(PetMenu::FavoriteFilter);
        panel.activate(PetMenu::TabFirst+1);setView({2,.12,.4});panel.activate(SaveViewFirst);setView({4,0,0});panel.activate(LoadViewFirst);valid=valid&&view.zoom==2&&std::abs(view.panX-.12)<.001&&std::abs(view.panY-.4)<.001;
        panel.activate(PetMenu::TabFirst+2);bool oldTracking=tracking;panel.activate(Tracking);valid=valid&&tracking!=oldTracking;panel.activate(Tracking);
        panel.activate(PetMenu::TabFirst+3);panel.activate(Snapshot);bool found=false;for(auto& entry:fs::directory_iterator(configDir/L"captures")) if(entry.path().extension()==L".png") found=true;valid=valid&&found;
        MSG escape{};escape.hwnd=panel.window;escape.message=WM_KEYDOWN;escape.wParam=VK_ESCAPE;panel.dialogMessage(escape);valid=valid&&!panel.visible();
        panel.show({x+width/2,y+height/3},menuState());valid=valid&&panel.visible();panel.activate(PetMenu::Close);valid=valid&&!panel.visible();testMenu=valid;
        panel.show({x+width/2,y+height/3},menuState());SendMessageW(panel.window,WM_ACTIVATE,WA_INACTIVE,0);valid=valid&&!panel.visible();testMenu=valid;
        if(!valid) {++errors;failure="Custom menu regression failed";}panel.activate(PetMenu::TabFirst);command(ExpressionReset);
    }
    void checkRecovery() {
        if(recoveryTestDone) return;recoveryTestDone=true;
        auto name=[](HWND control) {wchar_t value[256]{};GetWindowTextW(control,value,256);return std::wstring(value);};
        panel.show({x+width/2,y+height/3},menuState());panel.activate(PetMenu::TabFirst);
        SetFocus(GetDlgItem(panel.window,ExpressionFirst+1));
        MSG key{};key.hwnd=GetFocus();key.message=WM_KEYDOWN;key.wParam=VK_RETURN;
        testKeyboard=panel.dialogMessage(key)&&selectedExpression==1;
        auto expressionName=name(GetDlgItem(panel.window,ExpressionFirst+1));
        auto tabName=name(GetDlgItem(panel.window,PetMenu::TabFirst));
        testNativeNames=expressionName.find(L"已選擇")!=std::wstring::npos&&expressionName.find(L"播放中")!=std::wstring::npos&&tabName.find(L"目前分頁")!=std::wstring::npos;
        panel.activate(PetMenu::TabFirst+1);
        std::array<std::wstring,3> saveNames;
        for(int i=0;i<3;i++) {saveNames[i]=name(GetDlgItem(panel.window,SaveViewFirst+i));testNativeNames=testNativeNames&&saveNames[i].find(std::to_wstring(i+1))!=std::wstring::npos;}
        testNativeNames=testNativeNames&&saveNames[0]!=saveNames[1]&&saveNames[1]!=saveNames[2]&&name(GetDlgItem(panel.window,PetMenu::TabFirst)).find(L"目前分頁")==std::wstring::npos;
        SendMessageW(panel.slider,TBM_SETPOS,TRUE,2500);SendMessageW(panel.window,WM_HSCROLL,TB_THUMBPOSITION,reinterpret_cast<LPARAM>(panel.slider));
        auto sliderName=name(panel.slider);testSliderNames=sliderName.find(L"2.50 倍")!=std::wstring::npos;
        SetFocus(panel.slider);SendMessageW(panel.slider,WM_KEYDOWN,VK_RIGHT,0);
        testKeyboard=testKeyboard&&std::abs(view.zoom-2.55)<.0001;testSliderNames=testSliderNames&&name(panel.slider).find(L"2.55 倍")!=std::wstring::npos;
        SendMessageW(panel.slider,WM_KEYDOWN,VK_HOME,0);testKeyboard=testKeyboard&&view.zoom==1;testSliderNames=testSliderNames&&name(panel.slider).find(L"1.00 倍")!=std::wstring::npos;
        SendMessageW(panel.slider,WM_KEYDOWN,VK_END,0);testKeyboard=testKeyboard&&view.zoom==4;testSliderNames=testSliderNames&&name(panel.slider).find(L"4.00 倍")!=std::wstring::npos;
        SetFocus(GetDlgItem(panel.window,PetMenu::TabFirst+1));key.hwnd=GetFocus();key.wParam=VK_TAB;
        testKeyboard=panel.dialogMessage(key)&&GetFocus()==GetDlgItem(panel.window,PetMenu::TabFirst+2)&&testKeyboard;
        // Only synthesized product events are used here, never a machine sleep.
        const POINT point{x+width/2,y+height/2};const int oldClicks=clickCount;
        pointerDown(point);pointerMove({point.x+20,point.y+12});
        const bool dragWasActive=downHeld&&dragging&&GetCapture()==window;
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMSUSPEND,0);
        testSuspendDrag=dragWasActive&&suspended&&!downHeld&&!dragging&&!panning&&GetCapture()!=window&&!panel.visible()&&motionX==0&&motionY==0;
        pointerUp(point);testSuspendDrag=testSuspendDrag&&clickCount==oldClicks;
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMRESUMEAUTOMATIC,0);
        testResume=!suspended&&lastFrame==0&&motionX==0&&motionY==0;
        panel.show(point,menuState());panStart(point);panMove({point.x+10,point.y+10});
        const bool panWasActive=panning&&GetCapture()==window;
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMSUSPEND,0);
        testSuspendPan=panWasActive&&suspended&&!downHeld&&!dragging&&!panning&&GetCapture()!=window&&!panel.visible();
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMRESUMESUSPEND,0);
        testResume=testResume&&!suspended&&lastFrame==0&&motionX==0&&motionY==0;
        panel.show(point,menuState());panel.activate(PetMenu::TabFirst+1);
        RECT thumb{};SendMessageW(panel.slider,TBM_GETTHUMBRECT,0,reinterpret_cast<LPARAM>(&thumb));
        SendMessageW(panel.slider,WM_LBUTTONDOWN,MK_LBUTTON,MAKELPARAM((thumb.left+thumb.right)/2,(thumb.top+thumb.bottom)/2));
        const bool sliderWasActive=GetCapture()==panel.slider;
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMSUSPEND,0);
        const int framesBefore=frameCount;frame();
        testSuspendSlider=sliderWasActive&&GetCapture()!=panel.slider&&!panel.visible()&&frameCount==framesBefore&&desiredFPS==2;
        SendMessageW(window,WM_POWERBROADCAST,PBT_APMRESUMEAUTOMATIC,0);
        testResume=testResume&&!suspended&&lastFrame==0&&suspendCount==3&&resumeCount==3;
        const bool valid=testNativeNames&&testSliderNames&&testKeyboard&&testSuspendDrag&&testSuspendPan&&testSuspendSlider&&testResume;
        std::ofstream checks(configDir/L"recovery-checks.json",std::ios::binary);
        checks<<"{\"passed\":"<<valid<<",\"native_control_names\":"<<testNativeNames<<",\"slider_multiplier_names\":"<<testSliderNames<<",\"native_keyboard\":"<<testKeyboard<<",\"suspend_cancels_drag_capture_and_menu\":"<<testSuspendDrag<<",\"suspend_cancels_pan_capture_and_menu\":"<<testSuspendPan<<",\"suspend_cancels_slider_capture_and_rendering\":"<<testSuspendSlider<<",\"automatic_and_user_resume_reset_motion\":"<<testResume<<",\"suspend_events\":"<<suspendCount<<",\"resume_events\":"<<resumeCount<<",\"expression_name\":"<<quoted(utf8(expressionName))<<",\"tab_name\":"<<quoted(utf8(tabName))<<",\"slider_name_at_2_50\":"<<quoted(utf8(sliderName))<<",\"save_view_names\":[";
        for(int i=0;i<3;i++) {if(i) checks<<',';checks<<quoted(utf8(saveNames[i]));}checks<<"]}";
        if(!valid) {++errors;failure="Native accessibility names or power recovery regression failed";}
        setView({});command(ExpressionReset);
    }
    void selfCheck(double age) {
        // These checks call the product's own event handlers; physical input is tested separately.
        if(testPhase==0 && age>.4) {
            auto b=static_cast<unsigned char*>(dib); testAlpha=b[3]==0&&b[(size_t(width)*height-1)*4+3]==0;
            setPass(true); bool a=(GetWindowLongPtrW(window,GWL_EXSTYLE)&WS_EX_TRANSPARENT)!=0; setPass(false); testPass=a&&!(GetWindowLongPtrW(window,GWL_EXSTYLE)&WS_EX_TRANSPARENT);
            int old=heightDip; resize(old+30); testScale=heightDip==old+30;
            x=-50000;y=-50000;move(true,true); auto r=work(); testClamp=x>=r.left&&y>=r.top;
            resetPosition(); bool first=sdk.react(0)!=0; bool second=sdk.react(1)!=0; testCooldown=first&&!second;
            setView({2,0,0});testView=view.zoom==2;
            panStart({x+width/2,y+height/2});panMove({panDown.x+width/5,panDown.y+height/5});panStop();testPan=view.panX>.19&&view.panY>.19;
            setView({});command(Tracking);auto disabledGaze=desktop::gaze({100,0},{100,100},400,600,1,tracking);command(Tracking);auto enabledGaze=desktop::gaze({100,0},{100,100},400,600,1,tracking);testGaze=disabledGaze.y==0&&enabledGaze.y>0;
            testPhase=1;
        }
        if(testPhase==1 && age>2.5) { pointerDown({x+width/2,y+height/2}); previousMove=now()-.02;pointerMove({down.x+35,down.y-25}); pointerUp({down.x+35,down.y-25}); testDrag=dragCount==1&&clickCount==0; testPhase=2; }
        if(testPhase==2 && age>3.5) { pointerDown({x+width/2,y+height/5}); pointerUp(down); testPhase=3; }
        if(testPhase==3 && age>5.5) { pointerDown({x+width/2,y+height/2}); pointerUp(down); testPhase=4; }
        if(testPhase==4 && age>8) { command(ExpressionFirst+2);testExpression=true;command(Detail4);testPhase=7; }
        if(testPhase==7 && age>17) { command(FullBody);command(ExpressionReset);command(Rest);testPhase=5; }
        if(testPhase==5 && age>25) { command(Rest);testPhase=6; }
    }
    void writeReport(bool final=false) {
        FILETIME c,e,k,u; GetProcessTimes(GetCurrentProcess(),&c,&e,&k,&u); double t=now(),wall=t-lastCPUWall; uint64_t cpu=filetime(k)+filetime(u);
        SYSTEM_INFO si{}; GetSystemInfo(&si); if(lastCPU && wall>.01) maxCPU=std::max(maxCPU,double(cpu-lastCPU)/1e7/wall/std::max(1ul,si.dwNumberOfProcessors)*100); lastCPU=cpu; lastCPUWall=t;
        PROCESS_MEMORY_COUNTERS_EX mem{}; mem.cb=sizeof(mem); GetProcessMemoryInfo(GetCurrentProcess(),reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&mem),sizeof(mem)); maxMemory=std::max(maxMemory,mem.WorkingSetSize);
        if(report.empty()) return;
        std::ostringstream s;
        s<<"{\n\"companion\":{\"running\":"<<companion.alive()<<",\"starts\":"<<companion.restarts<<",\"shutdown_forced\":"<<(companion.shutdownForced?"true":"false")<<",\"activity\":"<<companion.activity<<",\"mouth\":"<<companion.mouth<<",\"fullscreen\":"<<companion.fullscreen<<",\"quiet\":"<<companion.quiet<<",\"expression_hold\":"<<expressionHold<<"},\n\"version\":\"0.3.1\",\"final\":"<<(final?"true":"false")<<",\"seconds\":"<<(t-started)<<",\"frames\":"<<frameCount<<",\"errors\":"<<errors<<",\"failure\":"<<quoted(failure)<<",\"average_fps\":"<<frameCount/std::max(.001,t-started)<<",\"target_fps\":"<<desiredFPS<<",\"average_render_ms\":"<<totalFrameMs/std::max(1,frameCount)<<",\"max_render_ms\":"<<maxFrameMs<<",\"peak_cpu_percent_normalized\":"<<maxCPU<<",\"working_set_mib\":"<<mem.WorkingSetSize/1048576.0<<",\"peak_working_set_mib\":"<<maxMemory/1048576.0<<",\"x\":"<<x<<",\"y\":"<<y<<",\"height_dip\":"<<heightDip<<",\"width\":"<<width<<",\"height\":"<<height<<",\"dpi\":"<<dpiAt(x,y)<<",\"passthrough\":"<<(pass?"true":"false")<<",\"rest\":"<<(rest?"true":"false")<<",\"drags\":"<<dragCount<<",\"clicks\":"<<clickCount<<",\"hotkeys\":["<<hotkeys[0]<<","<<hotkeys[1]<<","<<hotkeys[2]<<"],\"self_test\":{\"enabled\":"<<(selfTest?"true":"false")<<",\"alpha\":"<<testAlpha<<",\"passthrough_restore\":"<<testPass<<",\"scale\":"<<testScale<<",\"offscreen_recovery\":"<<testClamp<<",\"cooldown\":"<<testCooldown<<",\"drag_not_click\":"<<testDrag<<",\"detail_zoom\":"<<testView<<",\"pan\":"<<testPan<<",\"gaze_toggle_and_y\":"<<testGaze<<",\"expression\":"<<testExpression<<",\"custom_menu\":"<<testMenu<<"},\"favorite_expressions\":"<<favoriteExpressions<<",\"menu_tab\":"<<menuTab<<",\"saved_views\":["<<savedViews[0].valid<<","<<savedViews[1].valid<<","<<savedViews[2].valid<<"],\"tracking\":"<<(tracking?"true":"false")<<",\"view\":["<<view.zoom<<","<<view.panX<<","<<view.panY<<"],\"eye_projection\":["<<eyeX<<","<<eyeY<<"],\"renderer\":"<<sdk.info()<<"\n}";
        fs::create_directories(report.parent_path()); auto temp=report;temp+=L".tmp"; {std::ofstream f(temp,std::ios::binary); f<<s.str();} MoveFileExW(temp.c_str(),report.c_str(),MOVEFILE_REPLACE_EXISTING);
        if(!final) { std::string line=s.str();line.erase(std::remove(line.begin(),line.end(),'\n'),line.end());std::ofstream log(report.wstring()+L".jsonl",std::ios::app|std::ios::binary);log<<line<<"\n"; }
    }
    void frame() {
        if(suspended) {desiredFPS=2;if(settingsDirty&&now()>=saveAfter)save(true);return;}
        companion.tick(x,y,width,height);bool hidden=companion.fullscreen&&companion.hideFullscreen;
        if(hidden!=fullscreenHidden) {fullscreenHidden=hidden;ShowWindow(window,hidden?SW_HIDE:SW_SHOWNOACTIVATE);if(hidden)panel.hide();else {lastFrame=0;resetMotion();}}
        if(hidden) {desiredFPS=2;if(now()>=nextReport){writeReport();nextReport=now()+5;}if(settingsDirty&&now()>=saveAfter)save(true);if(duration>0&&now()-started>=duration)DestroyWindow(window);return;}
        if(companion.emotionVersion!=lastEmotionVersion) {lastEmotionVersion=companion.emotionVersion;if(!downHeld&&!panning&&now()>overrideUntil&&!(expressionHold&&selectedExpression>=0&&!aiExpression)) {if(companion.emotion>=0) {sdk.hold(false);sdk.expression(companion.emotion);selectedExpression=companion.emotion;reactionUntil=now()+8;aiExpression=true;}else if(aiExpression) {sdk.expression(-1);aiExpression=false;selectedExpression=-1;reactionUntil=0;}}}
        sdk.mouth(dragging?0:companion.mouth);
        double t=now(),dt=lastFrame?t-lastFrame:1./30;lastFrame=t;
        if(settingsDirty&&t>=saveAfter) save(true);
        if(!dragging || t-previousMove>.1) { double decay=exp(-dt*9);motionX*=decay;motionY*=decay; }
        POINT mouse;GetCursorPos(&mouse);auto look=desktop::gaze({double(mouse.x),double(mouse.y)},{x+eyeX*width,y+eyeY*height},width,height,view.zoom,tracking);
        bool active=dragging||panning||t<reactionUntil||companion.activity==4||companion.activity==5||companion.mouth>.005;int state=dragging?2:companion.activity>=4?companion.activity:t<reactionUntil?1:rest?3:0;
        desiredFPS=active?30:rest?8:(t-lastActivity>30?15:30);
        auto pixels=sdk.render(width,height,dt,float(motionX),float(motionY),float(look.x),float(look.y),state);
        if(!pixels) { ++errors; failure=sdk.error(); DestroyWindow(window); return; }
        sdk.eyePosition(&eyeX,&eyeY);
        panel.update(menuState());
        memcpy(dib,pixels,size_t(width)*height*4); SIZE size{width,height};POINT dst{x,y},src{};BLENDFUNCTION blend{AC_SRC_OVER,0,255,AC_SRC_ALPHA};
        if(!UpdateLayeredWindow(window,nullptr,&dst,&size,memory,&src,0,&blend,ULW_ALPHA)) { ++errors;failure="UpdateLayeredWindow failed: "+std::to_string(GetLastError());DestroyWindow(window);return; }
        ++frameCount; double ms=(now()-t)*1000;maxFrameMs=std::max(maxFrameMs,ms);totalFrameMs+=ms;
        if(selfTest) selfCheck(t-started);
        if(menuTest&&!menuTestDone&&frameCount>3) checkMenu();
        if(recoveryTest&&!recoveryTestDone&&frameCount>3) checkRecovery();
        if(t>=nextReport) { writeReport(); nextReport=t+5; }
        if(duration>0 && t-started>=duration) DestroyWindow(window);
    }
    void createIcon() {
        // Original geometric crystal icon, drawn by the host without external artwork.
        HDC dc=GetDC(nullptr),mc=CreateCompatibleDC(dc);HBITMAP color=CreateCompatibleBitmap(dc,32,32);auto old=SelectObject(mc,color);RECT r{0,0,32,32};FillRect(mc,&r,static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
        POINT diamond[]={{16,1},{30,12},{16,31},{2,12}};HBRUSH brush=CreateSolidBrush(RGB(169,221,247));auto ob=SelectObject(mc,brush);Polygon(mc,diamond,4);SelectObject(mc,ob);DeleteObject(brush);SelectObject(mc,old);
        HBITMAP mask=CreateBitmap(32,32,1,1,nullptr);auto om=SelectObject(mc,mask);PatBlt(mc,0,0,32,32,BLACKNESS);SelectObject(mc,om);ICONINFO ii{TRUE,0,0,mask,color};icon=CreateIconIndirect(&ii);DeleteObject(color);DeleteObject(mask);DeleteDC(mc);ReleaseDC(nullptr,dc);
    }
    void create() {
        memory=CreateCompatibleDC(nullptr);createIcon();
        WNDCLASSEXW wc{sizeof(wc)};wc.lpfnWndProc=proc;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=className.c_str();wc.hCursor=LoadCursorW(nullptr,IDC_ARROW);wc.hIcon=icon;RegisterClassExW(&wc);
        auto get=[&](const wchar_t*k,int v) {return int(GetPrivateProfileIntW(L"DesktopWaifu",k,v,ini.c_str()));};
        x=get(L"x",-99999);y=get(L"y",-99999);heightDip=get(L"heightDip",540);top=get(L"topmost",1)!=0;pass=get(L"passthrough",0)!=0;rest=get(L"rest",0)!=0;
        tracking=get(L"tracking",1)!=0;view=desktop::constrain({get(L"zoomPermille",1000)/1000.,get(L"panXPermille",0)/1000.,get(L"panYPermille",0)/1000.});
        favoriteExpressions=get(L"favoriteExpressions",14)&1023;menuTab=std::clamp(get(L"menuTab",0),0,3);
        expressionHold=get(L"expressionHold",0)!=0;
        for(int i=0;i<3;i++) {auto prefix=L"view"+std::to_wstring(i+1);auto& s=savedViews[i];s.valid=get((prefix+L"Valid").c_str(),0)!=0;s.height=std::clamp(get((prefix+L"Height").c_str(),540),desktop::MinHeightDip,desktop::MaxHeightDip);s.view=desktop::constrain({get((prefix+L"Zoom").c_str(),1000)/1000.,get((prefix+L"X").c_str(),0)/1000.,get((prefix+L"Y").c_str(),0)/1000.});}
        bool initial=x==-99999||y==-99999;if(initial) x=y=0;
        window=CreateWindowExW(WS_EX_LAYERED|(uiTest?WS_EX_APPWINDOW:WS_EX_TOOLWINDOW)|WS_EX_NOACTIVATE,className.c_str(),L"桌面灰姑娘 v0.3.1",WS_POPUP,x,y,width,height,nullptr,nullptr,wc.hInstance,this);
        if(!window) throw std::runtime_error("Cannot create desktop window");
        SetPropW(window,L"DesktopWaifu.ProfileLease.v1",reinterpret_cast<HANDLE>(1));
        resize(heightDip,false);if(initial) resetPosition();setPass(pass);taskbarCreated=RegisterWindowMessageW(L"TaskbarCreated");updateTray(true);
        setView(view);if(startFit) command(FitScreen);
        // Obtain the current eye projection before applying an optional face preset.
        if(!sdk.render(width,height,1./60,0,0,0,0,0)) throw std::runtime_error(sdk.error());sdk.eyePosition(&eyeX,&eyeY);
        if(startZoom>0) detail(startZoom);
        panel.create(window,folder,[this](int id,double value) {menuAction(id,value);});
        companion.init(folder,configDir,window,!noCompanion&&!selfTest&&!menuTest&&!recoveryTest);if(openChatInitially)companion.openChat();
        hotkeys[0]=RegisterHotKey(window,1,MOD_CONTROL|MOD_ALT|MOD_NOREPEAT,'P')!=0;hotkeys[1]=RegisterHotKey(window,2,MOD_CONTROL|MOD_ALT|MOD_NOREPEAT,VK_HOME)!=0;hotkeys[2]=RegisterHotKey(window,3,MOD_CONTROL|MOD_ALT|MOD_NOREPEAT,'Q')!=0;
        ShowWindow(window,SW_SHOWNOACTIVATE);frame();
        if(menuPreview) panel.show({x+width/2,y+height/3},menuState());
    }
    static LRESULT CALLBACK proc(HWND h,UINT msg,WPARAM wp,LPARAM lp) {
        Pet* p=reinterpret_cast<Pet*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(msg==WM_NCCREATE) {p=static_cast<Pet*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);p->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(p));}
        if(!p) return DefWindowProcW(h,msg,wp,lp);
        try {
            if(msg==p->taskbarCreated && p->taskbarCreated) {p->updateTray(true);return 0;}
            switch(msg) {
            case WM_MOUSEACTIVATE:return MA_NOACTIVATE;
            case WM_NCHITTEST: {POINT pt{GET_X_LPARAM(lp),GET_Y_LPARAM(lp)};ScreenToClient(h,&pt);return !p->pass&&p->solid(pt.x,pt.y)?HTCLIENT:HTTRANSPARENT;}
            case WM_LBUTTONDOWN: {POINT pt;GetCursorPos(&pt);p->pointerDown(pt);return 0;}
            case WM_MOUSEMOVE: {POINT pt;GetCursorPos(&pt);if(p->downHeld) p->pointerMove(pt);if(p->panning) p->panMove(pt);return 0;}
            case WM_LBUTTONUP: {POINT pt;GetCursorPos(&pt);p->pointerUp(pt);return 0;}
            case WM_MBUTTONDOWN: {POINT pt;GetCursorPos(&pt);p->panStart(pt);return 0;}
            case WM_MBUTTONUP:p->panStop();return 0;
            case WM_CAPTURECHANGED:if(p->downHeld||p->panning) {p->downHeld=p->dragging=p->panning=false;p->move(true);p->save();}return 0;
            case WM_MOUSEWHEEL:p->wheel(GET_WHEEL_DELTA_WPARAM(wp),{GET_X_LPARAM(lp),GET_Y_LPARAM(lp)},(GET_KEYSTATE_WPARAM(wp)&MK_CONTROL)!=0);return 0;
            case WM_RBUTTONUP: {POINT pt;GetCursorPos(&pt);p->menu(pt);return 0;}
            case WM_COMMAND:p->command(LOWORD(wp));return 0;
            case WM_HOTKEY:p->command(wp==1?Pass:wp==2?Reset:Quit);return 0;
            case RestoreMessage:p->command(Interact);p->move(true,true);return 0;
            case ChatMessage:p->command(Chat);return 0;
            case TrayMessage: {UINT event=LOWORD(lp);if(event==WM_CONTEXTMENU||event==WM_RBUTTONUP) {POINT pt;GetCursorPos(&pt);p->menu(pt);}else if(event==NIN_SELECT||event==NIN_KEYSELECT||event==WM_LBUTTONUP) p->command(Interact);return 0;}
            case WM_DISPLAYCHANGE:case WM_DPICHANGED:case WM_SETTINGCHANGE:p->panel.hide();p->resize(p->heightDip,false);return 0;
            case WM_POWERBROADCAST:p->powerState(wp);return TRUE;
            case WM_CLOSE:DestroyWindow(h);return 0;
            case WM_DESTROY:
                p->quitting=true;p->panel.hide();p->save(true);p->companion.stop();if(!p->capture.empty()) {fs::create_directories(p->capture.parent_path());if(!p->sdk.capture(utf8(p->capture.wstring()).c_str())) {++p->errors;p->failure=p->sdk.error();}}
                p->writeReport(true);Shell_NotifyIconW(NIM_DELETE,&p->tray);for(int i=1;i<=3;i++) UnregisterHotKey(h,i);PostQuitMessage(p->errors?1:0);return 0;
            }
        } catch(const std::exception& e) {p->failure=e.what();++p->errors;if(msg!=WM_DESTROY) DestroyWindow(h);return 0;}
        return DefWindowProcW(h,msg,wp,lp);
    }
};

int WINAPI wWinMain(HINSTANCE,HINSTANCE,PWSTR,int) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    Pet p; HANDLE mutex{};bool probeProfileLock=false;
    try {
        wchar_t exe[32768];GetModuleFileNameW(nullptr,exe,32768);p.folder=fs::path(exe).parent_path();
        const bool previewMode=fs::is_regular_file(p.folder/L"preview-mode.flag");
        PWSTR local{};if(SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData,0,nullptr,&local))) {p.configDir=fs::path(local)/(previewMode?L"DesktopWaifu-Preview":L"DesktopWaifu");CoTaskMemFree(local);}else p.configDir=p.folder/(previewMode?L"settings-preview":L"settings");
        fs::path model=p.folder/L"model.inp";bool requestQuit=false;int argc;auto argv=CommandLineToArgvW(GetCommandLineW(),&argc);
        for(int i=1;i<argc;i++) {std::wstring arg=argv[i];auto val=[&](const wchar_t* k) {return arg.substr(wcslen(k));};if(arg.rfind(L"--config-dir=",0)==0) p.configDir=val(L"--config-dir=");else if(arg.rfind(L"--model=",0)==0) model=val(L"--model=");else if(arg.rfind(L"--report=",0)==0) p.report=val(L"--report=");else if(arg.rfind(L"--capture=",0)==0) p.capture=val(L"--capture=");else if(arg.rfind(L"--duration=",0)==0) p.duration=std::stod(val(L"--duration="));else if(arg==L"--quit") requestQuit=true;else if(arg==L"--fit") p.startFit=true;else if(arg.rfind(L"--zoom=",0)==0) p.startZoom=std::stod(val(L"--zoom="));else if(arg==L"--self-test") p.selfTest=true;else if(arg==L"--menu-preview") p.menuPreview=true;else if(arg==L"--menu-test") p.menuTest=true;else if(arg==L"--recovery-test") p.recoveryTest=true;else if(arg.rfind(L"--menu-captures=",0)==0) p.menuCaptures=val(L"--menu-captures=");else if(arg==L"--ui-test") p.uiTest=true;else if(arg==L"--no-companion") p.noCompanion=true;else if(arg==L"--chat") p.openChatInitially=true;}
        for(int i=1;i<argc;i++)if(std::wstring(argv[i])==L"--probe-profile-lock")probeProfileLock=true;
        LocalFree(argv);p.configDir=fs::absolute(p.configDir);fs::create_directories(p.configDir);p.ini=p.configDir/L"settings.ini";if(p.menuCaptures.empty()) p.menuCaptures=p.configDir/L"menu-review";
        auto hash=std::to_wstring(std::hash<std::wstring>{}(p.configDir.wstring()));p.className=L"DesktopWaifu.Cinderella."+hash;
        auto name=L"Local\\DesktopWaifu."+hash;mutex=CreateMutexW(nullptr,FALSE,name.c_str());bool existingInstance=GetLastError()==ERROR_ALREADY_EXISTS;
        if(existingInstance||requestQuit) {
            if(existingInstance) {HWND existing=FindWindowW(p.className.c_str(),nullptr);if(!existing) existing=FindWindowW(ClassName,nullptr);if(existing) PostMessageW(existing,requestQuit?WM_CLOSE:p.openChatInitially?ChatMessage:RestoreMessage,0,0);}
            if(mutex) CloseHandle(mutex);return 0;
        }
        p.profileLease.acquire(p.configDir/L"companion");
        if(probeProfileLock) {if(mutex)CloseHandle(mutex);return 0;}
        p.sdk.load(p.folder,fs::absolute(model));p.create();timeBeginPeriod(1);
        MSG msg{};double next=now();
        while(!p.quitting) {
            while(PeekMessageW(&msg,nullptr,0,0,PM_REMOVE)) {if(msg.message==WM_QUIT) {p.quitting=true;break;}if(p.panel.dialogMessage(msg)) continue;TranslateMessage(&msg);DispatchMessageW(&msg);}
            if(p.quitting) break;
            double t=now();if(t>=next) {p.frame();next=t+1./p.desiredFPS;}
            MsgWaitForMultipleObjectsEx(0,nullptr,DWORD(std::clamp(ceil((next-now())*1000),1.,100.)),QS_ALLINPUT,MWMO_INPUTAVAILABLE);
        }
        timeEndPeriod(1);if(mutex) CloseHandle(mutex);return p.errors?1:0;
    } catch(const std::exception& e) {if(mutex) CloseHandle(mutex);if(probeProfileLock)return 3;MessageBoxW(nullptr,wide(std::string("無法啟動桌面灰姑娘：\n")+e.what()+"\n\n若正在備份或還原，請等作業完成後重試；其他錯誤請確認執行包檔案齊全。").c_str(),L"桌面灰姑娘",MB_OK|MB_ICONERROR);return 1;}
}
