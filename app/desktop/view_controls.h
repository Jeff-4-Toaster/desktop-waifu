#pragma once
#include <algorithm>
#include <cmath>

namespace desktop {
constexpr int MinHeightDip = 260;
constexpr int MaxHeightDip = 2200;
constexpr double MaxDetailZoom = 4.0;
struct Point { double x, y; };
struct View { double zoom=1, panX=0, panY=0; };

inline View constrain(View view) {
    if(!std::isfinite(view.zoom)) view.zoom=1;
    view.zoom=std::clamp(view.zoom,1.,MaxDetailZoom);
    double limit=(view.zoom-1)/2;
    view.panX=std::isfinite(view.panX)?std::clamp(view.panX,-limit,limit):0;
    view.panY=std::isfinite(view.panY)?std::clamp(view.panY,-limit,limit):0;
    return view;
}
inline View zoomAt(View old,double zoom,Point anchor) {
    old=constrain(old);
    zoom=std::clamp(zoom,1.,MaxDetailZoom);
    double ratio=zoom/old.zoom;
    return constrain({zoom,(anchor.x-.5)*(1-ratio)+old.panX*ratio,(anchor.y-.5)*(1-ratio)+old.panY*ratio});
}
inline Point gaze(Point cursor,Point eye,double width,double height,double zoom,bool tracking) {
    if(!tracking) return {0,0};
    // Screen Y increases downwards; authored GazeY increases upwards.
    return {(cursor.x-eye.x)/std::max(1.,width*zoom),(eye.y-cursor.y)/std::max(1.,height*zoom)};
}
}
