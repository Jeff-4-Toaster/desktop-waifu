#pragma once
#include <gdiplus.h>
#include <commctrl.h>
#include <functional>
#include <memory>
#include <array>

// All dimensions are DIP. Native buttons retain names, focus, Tab and Space support.
class PetMenu {
public:
    enum { TabFirst=900, Close=910, Favorite=911, FavoriteFilter=912, ZoomSlider=913 };
    struct State {
        bool tracking=true,top=true,rest=false,pass=false,hold=false;
        int expression=-1,favorites=14,tab=0,height=540;
        double zoom=1;
        std::array<bool,3> saved{};
    } state;
    HWND window{},slider{};
    std::function<void(int,double)> action;
    int chosen=1;
    bool favoritesOnly=false;
    float scale=1;
    int controlBuilds=0,captionWrites=0,sliderPaints=0,backgroundPaints=0;
    double maxTabMilliseconds=0;
private:
    static constexpr int W=416,H=588;
    struct Item { HWND handle{};int id;float x,y,w,h;std::wstring label,description,caption;int kind=0,image=-1,page=-1;bool hover=false; };
    std::vector<std::unique_ptr<Item>> items;
    std::unique_ptr<Gdiplus::Bitmap> portraits;
    ULONG_PTR gdiplusToken{};
    HWND owner{};
    int buildingPage=-1,paintedChosen=1;
    float builtScale=0;
    std::wstring sliderCaption;
    using Color=Gdiplus::Color;
    Color ink{255,47,62,82},muted{255,103,120,139},ruby{255,157,57,77},line{255,218,228,238};

    static void rounded(Gdiplus::Graphics& g,float x,float y,float w,float h,float radius,Color fill,Color border) {
        if(w<=0||h<=0) return;radius=std::min(radius,std::min(w,h)/2);
        Gdiplus::GraphicsPath path;float d=radius*2;
        path.AddArc(x,y,d,d,180,90);path.AddArc(x+w-d,y,d,d,270,90);
        path.AddArc(x+w-d,y+h-d,d,d,0,90);path.AddArc(x,y+h-d,d,d,90,90);path.CloseFigure();
        Gdiplus::SolidBrush b(fill);g.FillPath(&b,&path);Gdiplus::Pen p(border,1);g.DrawPath(&p,&path);
    }
    void text(Gdiplus::Graphics& g,const std::wstring& value,float x,float y,float w,float h,float size,Color color,bool bold=false,bool center=false) {
        Gdiplus::Font f(L"Microsoft JhengHei UI",size,bold?Gdiplus::FontStyleBold:Gdiplus::FontStyleRegular,Gdiplus::UnitPixel);
        Gdiplus::SolidBrush b(color);Gdiplus::StringFormat format;format.SetLineAlignment(Gdiplus::StringAlignmentCenter);
        if(center) format.SetAlignment(Gdiplus::StringAlignmentCenter);
        format.SetFormatFlags(Gdiplus::StringFormatFlagsNoWrap);format.SetTrimming(Gdiplus::StringTrimmingEllipsisCharacter);
        g.DrawString(value.c_str(),-1,&f,Gdiplus::RectF(x,y,w,h),&format,&b);
    }
    bool selected(const Item& item) const {
        if(item.kind==1) return item.id-TabFirst==state.tab;
        if(item.kind==2) return item.image==state.expression;
        if(item.kind==3) return item.id==Tracking?state.tracking:item.id==Top?state.top:item.id==Rest?state.rest:state.pass;
        return item.id==ExpressionHold?state.hold:item.id==FavoriteFilter&&favoritesOnly;
    }
    void drawButton(Gdiplus::Graphics& g,const Item& item,bool focus=false) {
        bool on=selected(item),enabled=IsWindowEnabled(item.handle)!=FALSE;
        Color fill=on?Color(255,238,244,249):item.hover?Color(255,242,247,251):Color(255,255,255,255);
        Color border=on?Color(255,171,191,211):line;
        if(item.kind==1) {fill=on?Color(255,255,255,255):Color(255,235,241,247);border=on?line:fill;}
        if(item.kind==4) { fill=Color(255,157,57,77);border=fill; }
        rounded(g,.5f,.5f,item.w-1,item.h-1,item.kind==2?12.f:10.f,fill,border);
        if(item.kind==2) {
            if(portraits) g.DrawImage(portraits.get(),Gdiplus::RectF(4,4,item.w-8,60),float(item.image*128),0,128,128,Gdiplus::UnitPixel);
            text(g,item.label,0,65,item.w,23,12,ink,true,true);
            if(state.favorites&(1<<item.image)) text(g,L"★",item.w-20,3,16,18,12,ruby,false,true);
            if(on) {Gdiplus::SolidBrush accent(ruby);g.FillEllipse(&accent,item.w/2-2,item.h-7,4.f,4.f);}
        } else if(item.kind==3) {
            text(g,item.label,16,8,item.w-88,26,14,ink,true);
            text(g,item.description,16,34,item.w-84,20,11,muted);
            rounded(g,item.w-60,22,42,22,11,on?ruby:Color(255,193,205,217),Color(0,0,0,0));
            Gdiplus::SolidBrush knob(Color(255,255,255,255));g.FillEllipse(&knob,item.w-58+(on?20.f:0),24.f,18.f,18.f);
        } else if(!item.description.empty()) {
            text(g,item.label,14,9,item.w-28,25,14,ink,true);
            text(g,item.description,14,34,item.w-28,20,11,muted);
        } else {
            auto color=item.kind==4?Color(255,255,255,255):item.id==Quit?ruby:!enabled?Color(255,164,176,190):ink;
            text(g,item.id==Close?L"×":item.label,5,0,item.w-10,item.h,item.id==Close?20.f:item.kind==1?13.f:12.f,color,item.kind==1||item.kind==4,true);
        }
        if(focus) rounded(g,3,3,item.w-6,item.h-6,8,Color(0,0,0,0),ruby);
    }
    void background(Gdiplus::Graphics& g) {
        g.Clear(Color(255,248,250,253));
        Gdiplus::LinearGradientBrush header(Gdiplus::Point(0,0),Gdiplus::Point(W,90),Color(255,255,255,255),Color(255,230,239,248));g.FillRectangle(&header,0,0,W,91);
        Gdiplus::PointF crystal[]={{39,23},{51,38},{39,58},{27,38}};Gdiplus::SolidBrush blue(Color(255,193,214,234));g.FillPolygon(&blue,crystal,4);Gdiplus::Pen silver(Color(255,145,171,198),1);g.DrawPolygon(&silver,crystal,4);
        Gdiplus::SolidBrush red(ruby);g.FillEllipse(&red,35,35,8,8);
        text(g,L"灰姑娘",65,20,240,29,21,ink,true);text(g,L"CINDERELLA  /  桌面伴侶",65,50,260,18,10,muted);
        Gdiplus::Pen divider(line,1);g.DrawLine(&divider,24,552,W-24,552);
        text(g,L"即時套用  ·  Esc 關閉",24,558,260,22,10,muted);text(g,L"v0.3.0",320,558,72,22,10,muted,false,true);
        const wchar_t* titles[]={L"每一種心情，都在這裡",L"靠近一點，看看細節",L"用舒服的方式陪著你",L"留下此刻，也整理日常"};
        const wchar_t* subtitles[]={L"選擇表情播放約 8 秒，星號代表常用。",L"儲存目前倍率、平移與大小，隨時回來。",L"所有開關都會自動保存。",L"截圖保留透明背景，方便收藏與使用。"};
        text(g,titles[state.tab],24,145,368,30,17,ink,true);text(g,subtitles[state.tab],24,176,368,24,11,muted);
        if(state.tab==0) {
            std::wstring hint=favoritesOnly&&state.favorites==0?L"尚無常用表情；切回全部後即可加入。":L"目前選擇："+std::wstring(ExpressionLabels[chosen]);
            text(g,hint,24,438,368,26,12,muted);
        }
        if(state.tab==1) {
            wchar_t value[48];swprintf_s(value,L"細節倍率  %.2f ×",state.zoom);text(g,value,24,343,368,28,13,ink,true);
            text(g,L"1 ×",24,398,80,20,10,muted);text(g,L"4 ×",350,398,42,20,10,muted,false,true);
            text(g,L"我的視角",24,427,200,24,13,ink,true);
        }
    }
    void drawSlider(Gdiplus::Graphics& g,float width,float height,int value) {
        float cy=height/2,left=10,right=width-10,thumb=left+(right-left)*(value-1000)/3000;
        // Use native hit-test geometry for the thumb, including endpoint rounding.
        if(slider) {RECT native{};SendMessageW(slider,TBM_GETTHUMBRECT,0,reinterpret_cast<LPARAM>(&native));thumb=(native.left+native.right)/(2*scale);left=std::min(left,thumb);right=std::max(right,thumb);}
        thumb=std::clamp(thumb,9.f,width-9.f);
        rounded(g,left,cy-2,right-left,4,2,Color(255,215,226,237),Color(0,0,0,0));
        if(thumb>left) rounded(g,left,cy-2,thumb-left,4,2,ruby,Color(0,0,0,0));
        Gdiplus::SolidBrush b(Color(255,255,255,255));Gdiplus::Pen p(ruby,2);g.FillEllipse(&b,thumb-7,cy-7,14.f,14.f);g.DrawEllipse(&p,thumb-7,cy-7,14.f,14.f);
    }
    void add(int id,float x,float y,float w,float h,const std::wstring& label,int kind=0,const std::wstring& desc=L"",int image=-1,bool enabled=true) {
        auto item=std::make_unique<Item>();item->id=id;item->x=x;item->y=y;item->w=w;item->h=h;item->label=label;item->kind=kind;item->description=desc;item->image=image;
        item->page=buildingPage;item->caption=label;if(kind==3) item->caption+=selected(*item)?L"：已開啟":L"：已關閉";
        item->handle=CreateWindowExW(0,L"BUTTON",item->caption.c_str(),WS_CHILD|WS_TABSTOP|BS_OWNERDRAW,int(x*scale),int(y*scale),int(w*scale),int(h*scale),window,reinterpret_cast<HMENU>(INT_PTR(id)),GetModuleHandleW(nullptr),nullptr);
        SetWindowLongPtrW(item->handle,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(item.get()));SetWindowSubclass(item->handle,buttonProc,1,reinterpret_cast<DWORD_PTR>(this));EnableWindow(item->handle,enabled);items.push_back(std::move(item));
    }
    void rebuild() {
        if(!items.empty()&&builtScale==scale) {syncCaptions();if(slider&&SendMessageW(slider,TBM_GETPOS,0,0)!=int(state.zoom*1000)) SendMessageW(slider,TBM_SETPOS,FALSE,int(state.zoom*1000));pageVisibility();return;}
        ++controlBuilds;builtScale=scale;buildingPage=-1;
        for(auto& item:items) DestroyWindow(item->handle);items.clear();if(slider) {DestroyWindow(slider);slider=nullptr;}sliderCaption.clear();
        add(Close,360,22,32,32,L"關閉選單");
        add(Chat,282,24,66,30,L"聊聊");
        const wchar_t* tabs[]={L"表情",L"視角",L"互動",L"工具"};for(int i=0;i<4;i++) add(TabFirst+i,24.f+94*i,100,86,34,tabs[i],1);
        for(int page=0;page<4;page++) {buildingPage=page;
        if(page==0) {
            for(int i=0;i<10;i++) add(ExpressionFirst+i,24.f+(i%5)*75,218.f+(i/5)*102,68,94,ExpressionLabels[i],2,L"",i);
            add(Favorite,24,474,174,38,(state.favorites&(1<<chosen))?L"★  從常用移除":L"☆  加入常用");
            add(FavoriteFilter,208,474,184,38,favoritesOnly?L"顯示全部表情":L"只看常用表情");add(ExpressionHold,24,520,174,26,L"保持表情");add(ExpressionReset,208,520,184,26,L"恢復預設表情");
        } else if(page==1) {
            add(FullBody,24,216,178,52,L"全身",0,L"恢復完整視角");add(FitScreen,214,216,178,52,L"放大至螢幕",0,L"完整呈現全身");
            add(Detail2,24,278,178,52,L"半身  2 ×",0,L"欣賞衣裝與動作");add(Detail4,214,278,178,52,L"臉部  4 ×",0,L"近看表情與眼睛");
            slider=CreateWindowExW(0,TRACKBAR_CLASSW,L"細節縮放倍率",WS_CHILD|WS_TABSTOP|TBS_NOTICKS|TBS_FIXEDLENGTH,int(24*scale),int(373*scale),int(368*scale),int(28*scale),window,reinterpret_cast<HMENU>(ZoomSlider),GetModuleHandleW(nullptr),nullptr);
            SendMessageW(slider,TBM_SETTHUMBLENGTH,int(18*scale),0);
            SendMessageW(slider,TBM_SETRANGE,TRUE,MAKELPARAM(1000,4000));SendMessageW(slider,TBM_SETPAGESIZE,0,250);SendMessageW(slider,TBM_SETLINESIZE,0,50);SendMessageW(slider,TBM_SETPOS,TRUE,int(state.zoom*1000));
            SetWindowSubclass(slider,sliderProc,1,reinterpret_cast<DWORD_PTR>(this));
            for(int i=0;i<3;i++) {std::wstring label=L"視角 "+std::to_wstring(i+1);add(LoadViewFirst+i,24.f+i*126,459,116,36,state.saved[i]?label:L"尚未儲存",0,L"",-1,state.saved[i]);add(SaveViewFirst+i,24.f+i*126,501,116,30,L"儲存目前視角");}
        } else if(page==2) {
            add(Tracking,24,216,368,66,L"眼睛跟隨游標",3,L"讓視線隨著你的游標移動");add(Top,24,292,368,66,L"保持置頂",3,L"讓角色顯示在其他視窗上方");
            add(Rest,24,368,368,66,L"休息與省資源",3,L"閒置時降低更新頻率");add(Pass,24,444,368,66,L"點擊穿透",3,L"使用系統匣或 Ctrl + Alt + P 恢復");
            add(Interact,24,520,368,26,L"恢復互動模式");
        } else {
            add(Snapshot,24,216,368,48,L"保存透明 PNG 截圖",4);add(OpenCaptures,24,274,368,42,L"開啟截圖資料夾");add(Reset,24,326,368,52,L"找回角色與全身視角",0,L"Ctrl + Alt + Home");
            add(Help,24,388,368,42,L"操作說明與快捷鍵");add(Quit,24,485,368,42,L"保存設定並退出桌寵");
        }}
        buildingPage=-1;syncCaptions();pageVisibility();
    }
    bool itemVisible(const Item& item) const {return (item.page<0||item.page==state.tab)&&!(item.kind==2&&favoritesOnly&&!(state.favorites&(1<<item.image)));}
    void dirty(float x,float y,float w,float h) {RECT r{int(x*scale),int(y*scale),int((x+w)*scale+1),int((y+h)*scale+1)};InvalidateRect(window,&r,FALSE);}
    void pageVisibility() {
        bool shown=visible();if(shown) SendMessageW(window,WM_SETREDRAW,FALSE,0);
        auto batch=BeginDeferWindowPos(int(items.size())+1);int slot=0;
        for(auto& item:items) {
            if(item->kind==2&&(!favoritesOnly||(state.favorites&(1<<item->image)))) {item->x=24.f+(slot%5)*75;item->y=218.f+(slot/5)*102;++slot;}
            bool show=itemVisible(*item);batch=DeferWindowPos(batch,item->handle,nullptr,int(item->x*scale),int(item->y*scale),0,0,SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|(show?SWP_SHOWWINDOW:SWP_HIDEWINDOW));
        }
        if(slider) batch=DeferWindowPos(batch,slider,nullptr,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|(state.tab==1?SWP_SHOWWINDOW:SWP_HIDEWINDOW));
        if(batch) EndDeferWindowPos(batch);
        if(shown) SendMessageW(window,WM_SETREDRAW,TRUE,0);
        dirty(24,144,368,404);for(auto& item:items) if(item->kind==1) InvalidateRect(item->handle,nullptr,FALSE);
    }
    void syncCaptions() {
        for(auto& item:items) {
            if(item->id==Favorite) item->label=(state.favorites&(1<<chosen))?L"★  從常用移除":L"☆  加入常用";
            if(item->id==ExpressionHold) item->label=state.hold?L"保持表情：開啟":L"保持表情：關閉";
            if(item->id==FavoriteFilter) item->label=favoritesOnly?L"顯示全部表情":L"只看常用表情";
            bool slot=item->id>=LoadViewFirst&&item->id<LoadViewFirst+3;
            if(slot) item->label=state.saved[item->id-LoadViewFirst]?L"視角 "+std::to_wstring(item->id-LoadViewFirst+1):L"尚未儲存";
            auto caption=item->label;if(item->kind==3) caption+=selected(*item)?L"：已開啟":L"：已關閉";
            // Owner drawing changes pixels only. Keep the native names equally
            // useful for keyboard users and Windows accessibility clients.
            if(item->kind==1&&selected(*item)) caption+=L"：目前分頁";
            if(item->kind==2) {
                if(item->image==chosen) caption+=L"：已選擇";
                if(item->image==state.expression) caption+=L"，播放中";
                if(state.favorites&(1<<item->image)) caption+=L"，常用";
            }
            if(slot) caption=L"視角 "+std::to_wstring(item->id-LoadViewFirst+1)+(state.saved[item->id-LoadViewFirst]?L"：載入已儲存視角":L"：尚未儲存");
            if(item->id>=SaveViewFirst&&item->id<SaveViewFirst+3) caption=L"儲存目前視角到視角 "+std::to_wstring(item->id-SaveViewFirst+1);
            if(item->id==Favorite) caption=std::wstring(ExpressionLabels[chosen])+((state.favorites&(1<<chosen))?L"：從常用移除":L"：加入常用");
            if(caption!=item->caption) {item->caption=caption;SetWindowTextW(item->handle,caption.c_str());++captionWrites;}
            bool enabled=!slot||state.saved[item->id-LoadViewFirst];if((IsWindowEnabled(item->handle)!=FALSE)!=enabled) EnableWindow(item->handle,enabled);
        }
        if(slider) {
            wchar_t value[96];swprintf_s(value,L"細節縮放倍率：%.2f 倍；左右鍵每次 0.05 倍",state.zoom);
            if(sliderCaption!=value) {sliderCaption=value;SetWindowTextW(slider,value);++captionWrites;}
        }
    }
    template<class Draw> void buffered(HDC target,int width,int height,Draw draw) {
        HDC memory=CreateCompatibleDC(target);HBITMAP bitmap=CreateCompatibleBitmap(target,width,height);auto old=SelectObject(memory,bitmap);
        {Gdiplus::Graphics g(memory);g.Clear(Color(255,248,250,253));g.SetSmoothingMode(Gdiplus::SmoothingModeAntiAlias);g.ScaleTransform(scale,scale);draw(g);}
        BitBlt(target,0,0,width,height,memory,0,0,SRCCOPY);SelectObject(memory,old);DeleteObject(bitmap);DeleteDC(memory);
    }
    void paintSlider(HDC dc) {RECT r{};GetClientRect(slider,&r);buffered(dc,r.right,r.bottom,[&](Gdiplus::Graphics& g) {drawSlider(g,r.right/scale,r.bottom/scale,int(SendMessageW(slider,TBM_GETPOS,0,0)));});++sliderPaints;}
    static LRESULT CALLBACK sliderProc(HWND h,UINT msg,WPARAM wp,LPARAM lp,UINT_PTR,DWORD_PTR data) {
        auto p=reinterpret_cast<PetMenu*>(data);
        if(msg==WM_ERASEBKGND) return 1;
        if(msg==WM_PAINT) {PAINTSTRUCT ps;auto dc=BeginPaint(h,&ps);p->paintSlider(dc);EndPaint(h,&ps);return 0;}
        if(msg==WM_PRINTCLIENT) {p->paintSlider(reinterpret_cast<HDC>(wp));return 0;}
        if(msg==WM_NCDESTROY) RemoveWindowSubclass(h,sliderProc,1);
        auto result=DefSubclassProc(h,msg,wp,lp);
        if(msg==TBM_SETPOS||msg==WM_MOUSEMOVE||msg==WM_LBUTTONDOWN||msg==WM_LBUTTONUP||msg==WM_KEYDOWN||msg==WM_MOUSEWHEEL||msg==WM_SETFOCUS||msg==WM_KILLFOCUS) InvalidateRect(h,nullptr,FALSE);
        return result;
    }
    static LRESULT CALLBACK buttonProc(HWND h,UINT msg,WPARAM wp,LPARAM lp,UINT_PTR,DWORD_PTR data) {
        auto panel=reinterpret_cast<PetMenu*>(data);auto item=reinterpret_cast<Item*>(GetWindowLongPtrW(h,GWLP_USERDATA));
        if(msg==WM_MOUSEMOVE&&item&&!item->hover) {item->hover=true;TRACKMOUSEEVENT track{sizeof(track),TME_LEAVE,h,0};TrackMouseEvent(&track);InvalidateRect(h,nullptr,FALSE);}
        if(msg==WM_MOUSELEAVE&&item) {item->hover=false;InvalidateRect(h,nullptr,FALSE);}
        if(msg==WM_KEYDOWN&&wp==VK_ESCAPE) {panel->hide();return 0;}
        if(msg==WM_NCDESTROY) RemoveWindowSubclass(h,buttonProc,1);
        return DefSubclassProc(h,msg,wp,lp);
    }
    static LRESULT CALLBACK proc(HWND h,UINT msg,WPARAM wp,LPARAM lp) {
        auto p=reinterpret_cast<PetMenu*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(msg==WM_NCCREATE) {p=static_cast<PetMenu*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);p->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(p));}if(!p) return DefWindowProcW(h,msg,wp,lp);
        try {switch(msg) {
        case WM_ERASEBKGND:return 1;
        case WM_PAINT: {PAINTSTRUCT ps;auto dc=BeginPaint(h,&ps);RECT r{};GetClientRect(h,&r);p->buffered(dc,r.right,r.bottom,[&](Gdiplus::Graphics& g) {p->background(g);});++p->backgroundPaints;EndPaint(h,&ps);return 0;}
        case WM_DRAWITEM: {auto di=reinterpret_cast<DRAWITEMSTRUCT*>(lp);auto item=reinterpret_cast<Item*>(GetWindowLongPtrW(di->hwndItem,GWLP_USERDATA));if(item) p->buffered(di->hDC,di->rcItem.right,di->rcItem.bottom,[&](Gdiplus::Graphics& g) {p->drawButton(g,*item,(di->itemState&ODS_FOCUS)!=0);});return TRUE;}
        // Native trackbar input is retained; all pixels are painted by sliderProc.
        case WM_NOTIFY: {auto hdr=reinterpret_cast<NMHDR*>(lp);if(hdr->hwndFrom==p->slider&&hdr->code==NM_CUSTOMDRAW) return CDRF_SKIPDEFAULT;break;}
        case WM_HSCROLL:if(reinterpret_cast<HWND>(lp)==p->slider) {double value=double(SendMessageW(p->slider,TBM_GETPOS,0,0))/1000;if(p->action&&value!=p->state.zoom) p->action(MenuZoom,value);InvalidateRect(p->slider,nullptr,FALSE);}return 0;
        case WM_COMMAND:if(HIWORD(wp)==BN_CLICKED) p->activate(LOWORD(wp));return 0;
        case WM_ACTIVATE:if(LOWORD(wp)==WA_INACTIVE) p->hide();return 0;
        case WM_KEYDOWN:if(wp==VK_ESCAPE) {p->hide();return 0;}break;
        case WM_CLOSE:p->hide();return 0;
        }} catch(const std::exception& e) {p->hide();MessageBoxW(p->owner,wide(std::string("選單操作未完成：\n")+e.what()).c_str(),L"灰姑娘",MB_OK|MB_ICONWARNING);return 0;}
        return DefWindowProcW(h,msg,wp,lp);
    }
public:
    ~PetMenu() {if(window) DestroyWindow(window);portraits.reset();if(gdiplusToken) Gdiplus::GdiplusShutdown(gdiplusToken);}
    void create(HWND parent,const fs::path& folder,std::function<void(int,double)> callback) {
        owner=parent;action=callback;Gdiplus::GdiplusStartupInput input;if(Gdiplus::GdiplusStartup(&gdiplusToken,&input,nullptr)!=Gdiplus::Ok) throw std::runtime_error("Cannot initialize menu graphics");
        INITCOMMONCONTROLSEX common{sizeof(common),ICC_BAR_CLASSES};InitCommonControlsEx(&common);
        auto atlas=std::make_unique<Gdiplus::Bitmap>((folder/L"menu-expressions.png").c_str());if(atlas->GetLastStatus()==Gdiplus::Ok) portraits=std::move(atlas);
        WNDCLASSEXW wc{sizeof(wc)};wc.lpfnWndProc=proc;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"DesktopWaifu.CrystalMenu.v012";wc.hCursor=LoadCursorW(nullptr,IDC_ARROW);wc.style=CS_DROPSHADOW;RegisterClassExW(&wc);
        window=CreateWindowExW(WS_EX_TOOLWINDOW|WS_EX_CONTROLPARENT|WS_EX_COMPOSITED,wc.lpszClassName,L"灰姑娘 · 自訂選單",WS_POPUP|WS_CLIPCHILDREN,0,0,W,H,owner,nullptr,wc.hInstance,this);if(!window) throw std::runtime_error("Cannot create custom menu");
    }
    bool visible() const {return window&&IsWindowVisible(window);}
    void update(State next) {
        State old=state;bool selectionChanged=paintedChosen!=chosen;
        bool changed=old.hold!=next.hold||old.tracking!=next.tracking||old.top!=next.top||old.rest!=next.rest||old.pass!=next.pass||old.expression!=next.expression||old.favorites!=next.favorites||old.zoom!=next.zoom||old.saved!=next.saved||old.tab!=next.tab||selectionChanged;
        state=next;if(!visible()||!changed) return;
        syncCaptions();
        if(old.tab!=state.tab||(favoritesOnly&&old.favorites!=state.favorites)) pageVisibility();
        if(selectionChanged) dirty(24,438,368,26);paintedChosen=chosen;
        if(old.zoom!=state.zoom) {dirty(24,343,368,28);if(slider&&SendMessageW(slider,TBM_GETPOS,0,0)!=int(state.zoom*1000)) SendMessageW(slider,TBM_SETPOS,FALSE,int(state.zoom*1000));if(slider) InvalidateRect(slider,nullptr,FALSE);}
        for(auto& item:items) {
            bool redraw=item->kind==2&&(item->image==old.expression||item->image==state.expression||((old.favorites^state.favorites)&(1<<item->image)));
            if(item->id==ExpressionHold)redraw=old.hold!=state.hold;
            if(item->kind==3) redraw=item->id==Tracking?old.tracking!=state.tracking:item->id==Top?old.top!=state.top:item->id==Rest?old.rest!=state.rest:old.pass!=state.pass;
            if(redraw&&itemVisible(*item)) InvalidateRect(item->handle,nullptr,FALSE);
        }
    }
    void show(POINT anchor,State next) {
        state=next;MONITORINFO monitor{sizeof(monitor)};GetMonitorInfoW(MonitorFromPoint(anchor,MONITOR_DEFAULTTONEAREST),&monitor);auto work=monitor.rcWork;
        using GetDpi=HRESULT(WINAPI*)(HMONITOR,int,UINT*,UINT*);static HMODULE shcore=LoadLibraryW(L"shcore.dll");auto getDpi=shcore?reinterpret_cast<GetDpi>(GetProcAddress(shcore,"GetDpiForMonitor")):nullptr;
        UINT dx=96,dy=96;if(getDpi) getDpi(MonitorFromPoint(anchor,MONITOR_DEFAULTTONEAREST),0,&dx,&dy);scale=dx/96.f;
        scale=std::min({scale,float(work.right-work.left-24)/W,float(work.bottom-work.top-24)/H});
        int width=int(W*scale),height=int(H*scale);int x=anchor.x+12;if(x+width>work.right-12) x=anchor.x-width-12;int y=anchor.y-90;
        x=std::clamp<int>(x,work.left+12,work.right-width-12);y=std::clamp<int>(y,work.top+12,work.bottom-height-12);
        SetWindowPos(window,HWND_TOPMOST,x,y,width,height,SWP_NOACTIVATE);SetWindowRgn(window,CreateRoundRectRgn(0,0,width+1,height+1,int(24*scale),int(24*scale)),TRUE);
        rebuild();ShowWindow(window,SW_SHOW);SetForegroundWindow(window);SetFocus(GetDlgItem(window,TabFirst+state.tab));
    }
    void hide() {if(window) ShowWindow(window,SW_HIDE);}
    void activate(int id) {
        if(id>=TabFirst&&id<TabFirst+4) {double start=now();int tab=id-TabFirst;if(tab!=state.tab) {state.tab=tab;syncCaptions();pageVisibility();if(action) action(MenuTab,state.tab);}SetFocus(GetDlgItem(window,id));maxTabMilliseconds=std::max(maxTabMilliseconds,(now()-start)*1000);return;}
        if(id==Close) {hide();return;}
        if(id==FavoriteFilter) {favoritesOnly=!favoritesOnly;syncCaptions();pageVisibility();SetFocus(GetDlgItem(window,id));return;}
        if(id>=ExpressionFirst&&id<=ExpressionLast) chosen=id-ExpressionFirst;
        if(action) action(id==Favorite?MenuFavorite:id,chosen);
    }
    bool dialogMessage(MSG& msg) {
        if(!visible()) return false;if(msg.message==WM_KEYDOWN&&msg.wParam==VK_ESCAPE) {hide();return true;}
        if(msg.message==WM_KEYDOWN&&msg.wParam==VK_RETURN&&GetParent(GetFocus())==window) {wchar_t name[32]{};GetClassNameW(GetFocus(),name,32);if(wcscmp(name,L"Button")==0) {activate(GetDlgCtrlID(GetFocus()));return true;}}
        return IsDialogMessageW(window,&msg)!=FALSE;
    }
    bool capture(const fs::path& path) {
        Gdiplus::Bitmap bitmap(int(W*scale),int(H*scale),PixelFormat32bppARGB);
        {Gdiplus::Graphics g(&bitmap);g.SetSmoothingMode(Gdiplus::SmoothingModeAntiAlias);g.ScaleTransform(scale,scale);background(g);for(auto& item:items) if(itemVisible(*item)) {auto saved=g.Save();g.TranslateTransform(item->x,item->y);drawButton(g,*item);g.Restore(saved);}if(slider&&state.tab==1) {g.TranslateTransform(24,373);drawSlider(g,368,28,int(state.zoom*1000));}}
        const CLSID png={0x557cf406,0x1a04,0x11d3,{0x9a,0x73,0x00,0x00,0xf8,0x1e,0xf3,0x2e}};
        return bitmap.Save(path.c_str(),&png,nullptr)==Gdiplus::Ok;
    }
    bool controlsWithinBounds() const {
        RECT parent{};GetClientRect(window,&parent);for(auto& item:items) {RECT r{};GetWindowRect(item->handle,&r);MapWindowPoints(nullptr,window,reinterpret_cast<POINT*>(&r),2);if(r.left<0||r.top<0||r.right>parent.right||r.bottom>parent.bottom) return false;}return true;
    }
    void diagnosticScale(float value) {scale=value;int w=int(W*scale),h=int(H*scale);SetWindowPos(window,nullptr,0,0,w,h,SWP_NOMOVE|SWP_NOACTIVATE|SWP_NOZORDER);SetWindowRgn(window,CreateRoundRectRgn(0,0,w+1,h+1,int(24*scale),int(24*scale)),TRUE);rebuild();}
    bool renderingRegression(const fs::path& output) {
        fs::create_directories(output);bool valid=true;int samples=0,differingPixels=0;bool endpointsUnclipped=true;
        auto flush=[&] {RedrawWindow(window,nullptr,nullptr,RDW_UPDATENOW|RDW_ALLCHILDREN);GdiFlush();};
        activate(TabFirst+1);flush();int builds=controlBuilds;auto originalSlider=slider;auto originalTab=GetDlgItem(window,TabFirst);
        for(int i=0;i<80;i++) {activate(TabFirst+i%4);flush();}
        bool cached=builds==controlBuilds&&originalSlider==slider&&originalTab==GetDlgItem(window,TabFirst);valid=valid&&cached;
        int writes=captionWrites,paints=backgroundPaints;for(int i=0;i<100;i++) update(state);flush();
        bool idleStable=writes==captionWrites&&paints==backgroundPaints;valid=valid&&idleStable;
        auto raster=[&](bool reference) {
            RECT r{};GetClientRect(slider,&r);HDC source=GetDC(slider),memory=CreateCompatibleDC(source);BITMAPINFO info{};info.bmiHeader.biSize=sizeof(BITMAPINFOHEADER);info.bmiHeader.biWidth=r.right;info.bmiHeader.biHeight=-r.bottom;info.bmiHeader.biPlanes=1;info.bmiHeader.biBitCount=32;info.bmiHeader.biCompression=BI_RGB;
            void* bits{};HBITMAP image=CreateDIBSection(source,&info,DIB_RGB_COLORS,&bits,nullptr,0);auto old=SelectObject(memory,image);
            if(reference) SendMessageW(slider,WM_PRINTCLIENT,reinterpret_cast<WPARAM>(memory),PRF_CLIENT);else BitBlt(memory,0,0,r.right,r.bottom,source,0,0,SRCCOPY);
            GdiFlush();std::vector<uint32_t> result(static_cast<uint32_t*>(bits),static_cast<uint32_t*>(bits)+r.right*r.bottom);SelectObject(memory,old);DeleteObject(image);DeleteDC(memory);ReleaseDC(slider,source);for(auto& pixel:result) pixel|=0xff000000;return result;
        };
        auto saveRaster=[&](std::vector<uint32_t>& pixels,const fs::path& path) {RECT r{};GetClientRect(slider,&r);Gdiplus::Bitmap bitmap(r.right,r.bottom,r.right*4,PixelFormat32bppARGB,reinterpret_cast<BYTE*>(pixels.data()));const CLSID png={0x557cf406,0x1a04,0x11d3,{0x9a,0x73,0x00,0x00,0xf8,0x1e,0xf3,0x2e}};return bitmap.Save(path.c_str(),&png,nullptr)==Gdiplus::Ok;};
        for(int percent:{100,150,200}) {
            diagnosticScale(percent/100.f);MONITORINFO monitor{sizeof(monitor)};GetMonitorInfoW(MonitorFromWindow(owner,MONITOR_DEFAULTTONEAREST),&monitor);SetWindowPos(window,nullptr,monitor.rcWork.left+12,monitor.rcWork.top+12,0,0,SWP_NOSIZE|SWP_NOACTIVATE|SWP_NOZORDER);
            activate(TabFirst+1);flush();
            for(int value:{1000,1001,1040,1800,2500,3999,4000,1500,3500,1000}) {
                SendMessageW(slider,TBM_SETPOS,TRUE,value);SendMessageW(window,WM_HSCROLL,TB_THUMBTRACK,reinterpret_cast<LPARAM>(slider));flush();
                auto actual=raster(false),expected=raster(true);int different=0;for(size_t i=0;i<actual.size();i++) if(actual[i]!=expected[i]) ++different;
                if(value==1000||value==4000) {RECT client{};GetClientRect(slider,&client);for(int y=0;y<client.bottom;y++) if(actual[size_t(y)*client.right]!=0xfff8fafd||actual[size_t(y)*client.right+client.right-1]!=0xfff8fafd) endpointsUnclipped=false;}
                differingPixels+=different;++samples;
                valid=saveRaster(actual,output/(L"slider-"+std::to_wstring(percent)+L"-"+std::to_wstring(value)+L".png"))&&valid;
                if(different) saveRaster(expected,output/L"slider-reference.png");
            }
            RECT thumb{},client{};SendMessageW(slider,TBM_GETTHUMBRECT,0,reinterpret_cast<LPARAM>(&thumb));GetClientRect(slider,&client);
            SendMessageW(slider,WM_LBUTTONDOWN,MK_LBUTTON,MAKELPARAM((thumb.left+thumb.right)/2,client.bottom/2));
            for(int step=0;step<=40;step++) SendMessageW(slider,WM_MOUSEMOVE,MK_LBUTTON,MAKELPARAM(12+int((client.right-24)*step/40),client.bottom/2));
            SendMessageW(slider,WM_LBUTTONUP,0,MAKELPARAM(client.right-12,client.bottom/2));flush();
            auto actual=raster(false),expected=raster(true);for(size_t i=0;i<actual.size();i++) if(actual[i]!=expected[i]) ++differingPixels;++samples;
        }
        valid=valid&&differingPixels==0&&endpointsUnclipped;diagnosticScale(1);
        std::ofstream report(output/L"paint-regression.json");report<<"{\"cached_page_controls\":"<<cached<<",\"idle_no_caption_or_background_updates\":"<<idleStable<<",\"native_slider_samples\":"<<samples<<",\"different_pixels_from_full_render\":"<<differingPixels<<",\"slider_endpoints_unclipped\":"<<endpointsUnclipped<<",\"max_tab_action_ms\":"<<maxTabMilliseconds<<",\"passed\":"<<valid<<"}";
        return valid;
    }
};
