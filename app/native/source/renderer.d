module cinderella.renderer;
import inochi2d;
import inochi2d.core.animation.player;
import bindbc.opengl;
import core.sys.windows.windows : LoadLibraryW, GetProcAddress;
import std.utf : toUTF16z;
import std.string : fromStringz, toStringz;
import std.exception : enforce;
import std.algorithm : min, max, clamp;
import std.math : sin, cos, PI, exp, abs, isFinite;
import std.json : JSONValue;
import std.file : write;
import std.conv : to;
import std.algorithm.searching : canFind, startsWith;
import imagefmt : write_image_mem, IF_PNG;

private {
    Puppet puppet;
    Parameter[string] params;
    AnimationPlayer animationPlayer;
    Part iris;
    string lastError;
    string gpuError;
    ulong uniformWarnings;
    bool unexpectedGPUError;
    string infoString;
    double simTime = 0, elapsed = 0, accumulator = 0;
    double blinkAt = 2.4, blinkStarted = -100, reactionUntil = 0;
    double reactionCooldownUntil = 0;
    float viewZoom = 1, panX = 0, panY = 0;
    string activeExpression;
    immutable string[] expressionNames = ["Expr_basic", "Expr_smile", "Expr_delight", "Expr_amazed", "Expr_sadness", "Expr_angry", "Expr_fury", "Expr_fear", "Expr_pain", "Expr_feeling_empty"];
    ulong expressionCount;
    ulong frames, physicsSteps, blinkCount, clickCount;
    vec4 bounds;
    ubyte[] rawPixels, bgraPixels;
    int renderWidth, renderHeight;
    float gazeX = 0, gazeY = 0;
    float voiceMouth = 0, smoothMouth = 0;
    float hairOffset = 0, hairVelocity = 0;
    bool expressionHeld = false;
    float[string] physicsPeaks;
    double clock() { return simTime; }
    void checkGL(string stage) {
        auto error = glGetError();
        enforce(error == GL_NO_ERROR, stage ~ ": OpenGL error " ~ to!string(error) ~ " " ~ gpuError);
    }
    extern(C) void debugMessage(uint source, uint type, uint id, uint severity, int length, const(char)* message, const(void)* user) nothrow {
        if (type == 0x824C) try {
            auto detail = fromStringz(message);
            if (detail.canFind("<location> is invalid")) ++uniformWarnings;
            else { unexpectedGPUError = true; if (gpuError.length < 16000) gpuError ~= detail ~ "\n"; }
        } catch (Throwable) {}
    }

    alias SDLInit = extern(C) int function(uint);
    alias SDLSetAttribute = extern(C) int function(int, int);
    alias SDLCreateWindow = extern(C) void* function(const(char)*, int, int, int, int, uint);
    alias SDLCreateContext = extern(C) void* function(void*);

    void createContext(string path) {
        auto library = LoadLibraryW(toUTF16z(path));
        enforce(library !is null, "Cannot load packaged SDL2.dll");
        auto initialize = cast(SDLInit)GetProcAddress(library, "SDL_Init");
        auto setAttribute = cast(SDLSetAttribute)GetProcAddress(library, "SDL_GL_SetAttribute");
        auto createWindow = cast(SDLCreateWindow)GetProcAddress(library, "SDL_CreateWindow");
        auto createGLContext = cast(SDLCreateContext)GetProcAddress(library, "SDL_GL_CreateContext");
        enforce(initialize && setAttribute && createWindow && createGLContext, "SDL2 entry points missing");
        enforce(initialize(0x20) == 0, "SDL2 video initialization failed");
        setAttribute(17, 3);
        setAttribute(18, 3);
        setAttribute(21, 1);
        // Hidden GL context; the Windows host displays the alpha framebuffer.
        auto window = createWindow("Cinderella renderer", 0, 0, 64, 64, 0x2 | 0x8);
        enforce(window !is null && createGLContext(window) !is null, "OpenGL context creation failed");
        enforce(loadOpenGL() >= GLSupport.gl32, "OpenGL 3.2 or later required");
        alias GetGLProc = extern(C) void* function(const(char)*);
        auto getGLProc = cast(GetGLProc)GetProcAddress(library, "SDL_GL_GetProcAddress");
        alias DebugMessageCallback = extern(C) void function(typeof(&debugMessage), const(void)*);
        auto callback = cast(DebugMessageCallback)getGLProc("glDebugMessageCallback");
        if (callback) { callback(&debugMessage, null); glEnable(0x92E0); glEnable(0x8242); }
    }

    void setParam(string name, float value) {
        if (auto target = name in params) {
            (*target).value.x = clamp(value, (*target).min.x, (*target).max.x);
        }
    }

    void resetPhysics() {
        hairOffset = 0; hairVelocity = 0;
        params["BodyPhysicsInput"].value = vec2(0);
        puppet.enableDrivers = false;
        inUpdate();
        puppet.update();
        puppet.resetDrivers();
        puppet.enableDrivers = true;
        accumulator = 0;
    }
    vec2 irisCenter() {
        vec2 center = vec2(0);
        if (!iris || iris.vertices.length == 0) return center;
        auto matrix = puppet.transform.matrix * iris.getDynamicMatrix();
        foreach (i, vertex; iris.vertices) center += vec2(matrix * vec4(vertex + iris.deformation[i], 0, 1));
        return center / cast(float)iris.vertices.length;
    }
    void playExpression(string name, double seconds) {
        enforce(name in puppet.getAnimations(), "Missing expression animation: " ~ name);
        animationPlayer.stopAll(true);
        auto playback = animationPlayer.play(name);
        enforce(playback !is null, "Cannot play expression: " ~ name);
        playback.play(false, true);
        reactionUntil = elapsed + seconds;
        activeExpression = name;
    }
}

extern(C) export nothrow {
    const(char)* dw_error() {
        try { return toStringz(lastError); } catch (Throwable) { return "Native renderer error"; }
    }

    int dw_init(const(char)* modelPath, const(char)* sdlPath) {
        try {
            enforce(puppet is null, "Renderer is already initialized");
            createContext(fromStringz(sdlPath).idup);
            inInit(&clock);
            // SDK initialization creates framebuffer names after its first viewport call.
            inSetViewport(64, 64);
            while (glGetError() != GL_NO_ERROR) {}
            unexpectedGPUError = false;
            gpuError = "";
            inSetUpdateBounds(true);
            puppet = inLoadPuppet(fromStringz(modelPath).idup);
            foreach (p; puppet.parameters) params[p.name] = p;
            foreach (p; puppet.getAllParts()) if (p.name.startsWith("Iris_Complete")) { iris = p; break; }
            foreach (name; ["EyeOpen", "GazeX", "GazeY", "Breath", "BodyPhysicsInput", "ChestPhysics", "ThighPhysicsVLeft", "ThighPhysicsVRight"])
                enforce(name in params, "Missing required model parameter: " ~ name);
            resetPhysics();
            bounds = puppet.getCombinedBounds!true();
            enforce(bounds.z > bounds.x && bounds.w > bounds.y, "Invalid model bounds");
            animationPlayer = new AnimationPlayer(puppet);
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    double dw_aspect() {
        return max(0.25, (bounds.z - bounds.x) / (bounds.w - bounds.y));
    }

    void dw_reset() {
        try { if (puppet) resetPhysics(); } catch (Throwable error) { lastError = error.msg; }
    }

    int dw_react(int region) {
        try {
            if (elapsed < reactionCooldownUntil) return 0;
            auto name = region == 0 ? "Expr_smile" : "Expr_amazed";
            playExpression(name, 1.8);
            reactionCooldownUntil = elapsed + 1.8;
            ++clickCount;
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    int dw_expression(int index) {
        try {
            if (index == -1) {
                animationPlayer.stopAll(true); reactionUntil = elapsed;
                reactionCooldownUntil = elapsed; activeExpression = ""; return 1;
            }
            enforce(index >= 0 && index < expressionNames.length, "Invalid expression index");
            auto name = expressionNames[index];
            enforce(name in puppet.getAnimations(), "Missing expression animation: " ~ name);
            auto animation = puppet.getAnimations()[name];
            playExpression(name, animation.length * animation.timestep);
            reactionCooldownUntil = elapsed + 0.25;
            ++expressionCount;
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    void dw_hold(int enabled) { expressionHeld = enabled != 0; }
    void dw_mouth(float value) { voiceMouth = isFinite(value) ? clamp(value, 0.0f, 1.0f) : 0; }

    int dw_set_view(float zoom, float x, float y) {
        try {
            enforce(isFinite(zoom) && isFinite(x) && isFinite(y), "Non-finite view input");
            viewZoom = clamp(zoom, 1.0f, 4.0f);
            const limit = (viewZoom - 1) * 0.5f;
            panX = clamp(x, -limit, limit); panY = clamp(y, -limit, limit);
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    int dw_eye_position(float* x, float* y) {
        try {
            if (!iris || renderWidth == 0 || !x || !y) return 0;
            auto clip = inGetCamera().matrix * vec4(irisCenter(), 0, 1);
            *x = 0.5f + clip.x * 0.5f; *y = 0.5f - clip.y * 0.5f;
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    // Returned pixels are top-down, premultiplied BGRA, valid until next render.
    const(ubyte)* dw_render(int width, int height, double dt, float motionX, float motionY, float lookX, float lookY, int state) {
        try {
            enforce(puppet !is null && width >= 64 && height >= 64 && width <= 2048 && height <= 2048, "Invalid render size");
            if (!isFinite(dt) || dt < 0) dt = 0;
            if (dt > 0.25) { resetPhysics(); dt = 1.0 / 30; }
            dt = min(dt, 0.2);
            elapsed += dt;
            smoothMouth += (voiceMouth - smoothMouth) * cast(float)(1 - exp(-dt * (voiceMouth > smoothMouth ? 20 : 13)));
            if (expressionHeld && activeExpression.length && elapsed >= reactionUntil) {
                auto name = activeExpression;
                auto animation = puppet.getAnimations()[name];
                playExpression(name, animation.length * animation.timestep);
            }
            if (width != renderWidth || height != renderHeight) {
                renderWidth = width; renderHeight = height;
                rawPixels.length = cast(size_t)width * height * 4;
                bgraPixels.length = rawPixels.length;
                inSetViewport(width, height);
            }
            const scale = viewZoom * min((width - 24.0f) / (bounds.z - bounds.x), (height - 24.0f) / (bounds.w - bounds.y));
            inGetCamera().scale = vec2(scale);
            inGetCamera().position = -vec2((bounds.x + bounds.z) * 0.5f, (bounds.y + bounds.w) * 0.5f) + vec2(panX * width / scale, panY * height / scale);
            gazeX += (clamp(lookX, -0.65f, 0.65f) - gazeX) * cast(float)(1 - exp(-dt * 7));
            gazeY += (clamp(lookY, -0.55f, 0.55f) - gazeY) * cast(float)(1 - exp(-dt * 7));
            accumulator += dt;
            while (accumulator + 1e-8 >= 1.0 / 60) {
                const step = 1.0 / 60;
                simTime += step;
                accumulator -= step;
                foreach (p; puppet.parameters) {
                    if (p.name != "ChestPhysics" && p.name != "ThighPhysicsVLeft" && p.name != "ThighPhysicsVRight") p.value = p.defaults;
                }
                if (elapsed >= blinkAt) {
                    blinkStarted = elapsed;
                    blinkAt = elapsed + 3.2 + 1.2 * (0.5 + 0.5 * sin(elapsed * 1.7));
                    ++blinkCount;
                }
                const blinkAge = elapsed - blinkStarted;
                const openness = blinkAge < 0.18 ? cast(float)(abs(blinkAge - 0.09) / 0.09) : 1.0f;
                setParam("EyeOpen", params["EyeOpen"].defaults.x * openness);
                setParam("GazeX", gazeX);
                setParam("GazeY", gazeY);
                setParam("Breath", cast(float)(0.12 + 0.10 * sin(simTime * 1.55)));
                setParam("BodySway", cast(float)(0.022 * sin(simTime * 0.57)));
                setParam("HeadRoll", cast(float)(0.015 * sin(simTime * 0.71)));
                // Existing authored hair bindings, with a small bounded spring response.
                const hairTarget = clamp(motionX * 0.26f + motionY * 0.08f, -0.14f, 0.14f);
                hairVelocity += cast(float)((28 * (hairTarget - hairOffset) - 10 * hairVelocity) * step);
                hairOffset = clamp(hairOffset + hairVelocity * cast(float)step, -0.18f, 0.18f);
                setParam("HairLeft", hairOffset + cast(float)(0.014 * sin(simTime * 0.63)));
                setParam("HairRight", hairOffset * 0.8f + cast(float)(0.011 * sin(simTime * 0.59 + 0.7)));
                params["BodyPhysicsInput"].value = vec2(clamp(motionX, -0.45f, 0.45f), clamp(motionY, -0.45f, 0.45f));
                if (elapsed < reactionUntil) animationPlayer.update(cast(float)step);
                else { animationPlayer.stopAll(true); activeExpression = ""; }
                if (state == 4) {
                    setParam("HeadRoll", cast(float)(0.025 + 0.012 * sin(simTime * 0.8)));
                    setParam("HeadNod", cast(float)(0.018 * sin(simTime * 0.65)));
                }
                if (state == 5 || smoothMouth > 0.005) setParam("MouthOpen", smoothMouth);
                inUpdate();
                puppet.update();
                foreach (name; ["ChestPhysics", "ThighPhysicsVLeft", "ThighPhysicsVRight"]) {
                    auto value = params[name].value;
                    enforce(isFinite(value.x) && isFinite(value.y), "Non-finite native physics output");
                    physicsPeaks[name] = max(physicsPeaks.get(name, 0), max(abs(value.x), abs(value.y)));
                }
                ++physicsSteps;
            }
            checkGL("model update/viewport");
            inBeginScene();
            checkGL("begin scene");
            enforce(glCheckFramebufferStatus(GL_DRAW_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE, "Incomplete SDK framebuffer");
            puppet.draw();
            // 0.8.6 uses a basic-shader offset location while drawing masks.
            // Creator tolerates the rejected uniform; retain that native behavior.
            auto drawError = glGetError();
            enforce(!unexpectedGPUError && (drawError == GL_NO_ERROR || (drawError == GL_INVALID_OPERATION && uniformWarnings > 0)), "puppet draw: " ~ gpuError);
            glBindFramebuffer(GL_READ_FRAMEBUFFER, inGetFramebuffer());
            glReadBuffer(GL_COLOR_ATTACHMENT0);
            glPixelStorei(GL_PACK_ALIGNMENT, 1);
            glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, rawPixels.ptr);
            checkGL("frame readback");
            inEndScene();
            // SDK restores an attachment draw-buffer on the default framebuffer.
            // The host never uses that framebuffer; clear that reset-only GL error.
            while (glGetError() != GL_NO_ERROR) {}
            unexpectedGPUError = false;
            gpuError = "";
            for (int y; y < height; ++y) {
                const src = cast(size_t)(height - 1 - y) * width * 4;
                const dst = cast(size_t)y * width * 4;
                for (int x; x < width; ++x) {
                    const s = src + x * 4, d = dst + x * 4;
                    const a = rawPixels[s + 3];
                    if (a < 4) bgraPixels[d .. d + 4] = 0;
                    else {
                        bgraPixels[d] = min(rawPixels[s + 2], a);
                        bgraPixels[d + 1] = min(rawPixels[s + 1], a);
                        bgraPixels[d + 2] = min(rawPixels[s], a);
                        bgraPixels[d + 3] = a;
                    }
                }
            }
            ++frames;
            return bgraPixels.ptr;
        } catch (Throwable error) { lastError = error.msg; return null; }
    }

    int dw_capture(const(char)* path) {
        try {
            enforce(bgraPixels.length > 0, "Render a frame before capturing");
            ubyte[] rgba = new ubyte[bgraPixels.length];
            for (size_t i; i < rgba.length; i += 4) {
                const a = bgraPixels[i + 3];
                rgba[i + 3] = a;
                if (a) {
                    rgba[i] = cast(ubyte)min(255u, bgraPixels[i + 2] * 255u / a);
                    rgba[i + 1] = cast(ubyte)min(255u, bgraPixels[i + 1] * 255u / a);
                    rgba[i + 2] = cast(ubyte)min(255u, bgraPixels[i] * 255u / a);
                }
            }
            int error;
            auto encoded = write_image_mem(IF_PNG, renderWidth, renderHeight, rgba, 4, error);
            enforce(error == 0, "PNG encoding failed");
            write(fromStringz(path).idup, encoded);
            return 1;
        } catch (Throwable error) { lastError = error.msg; return 0; }
    }

    int dw_export(const(char)* path) {
        try { inWriteINPPuppet(puppet, fromStringz(path).idup); return 1; }
        catch (Throwable error) { lastError = error.msg; return 0; }
    }

    const(char)* dw_info() {
        try {
            JSONValue info;
            info["sdk"] = "0.8.6";
            info["bridge_version"] = "0.3.0";
            info["expression_held"] = expressionHeld;
            info["audio_mouth"] = smoothMouth;
            info["mouth_parameter"] = params["MouthOpen"].value.x;
            info["hair_parameters"] = JSONValue([params["HairLeft"].value.x, params["HairRight"].value.x]);
            info["hair_inertia"] = hairOffset;
            info["view"] = JSONValue([viewZoom, panX, panY]);
            info["gaze"] = JSONValue([params["GazeX"].value.x, params["GazeY"].value.x]);
            info["iris_model_center"] = JSONValue([irisCenter().x, irisCenter().y]);
            info["active_expression"] = activeExpression;
            info["expression_names"] = JSONValue(expressionNames);
            info["expression_plays"] = expressionCount;
            info["native_uniform_warnings"] = uniformWarnings;
            info["frames"] = frames;
            info["physics_steps"] = physicsSteps;
            info["blinks"] = blinkCount;
            info["click_reactions"] = clickCount;
            info["bounds"] = JSONValue([bounds.x, bounds.y, bounds.z, bounds.w]);
            info["physics_peaks"] = JSONValue(physicsPeaks);
            JSONValue outputs;
            foreach (name; ["ChestPhysics", "ThighPhysicsVLeft", "ThighPhysicsVRight"]) outputs[name] = JSONValue([params[name].value.x, params[name].value.y]);
            info["physics_current"] = outputs;
            info["parameter_count"] = puppet.parameters.length;
            info["animation_count"] = puppet.getAnimations().length;
            info["gpu"] = fromStringz(cast(const(char)*)glGetString(GL_RENDERER)).idup;
            infoString = info.toString();
            return toStringz(infoString);
        } catch (Throwable error) { lastError = error.msg; return "{}"; }
    }
}
