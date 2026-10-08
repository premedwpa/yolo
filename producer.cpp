#define _CRT_SECURE_NO_WARNINGS
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <d2d1.h>
#include <dwrite.h>
#include <onnxruntime_cxx_api.h>
#include <dml_provider_factory.h>
#include <vector>
#include <string>
#include <chrono>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdarg>
#include <memory>
#include <cwchar>

#include "SharedMem.h"   // 共享内存协议 —— 与 Consumer 共用同一份定义

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d2d1.lib")
#pragma comment(lib, "dwrite.lib")
#pragma comment(lib, "onnxruntime.lib")

// 共享内存协议（SHARED_MEM_NAME / MAX_TEXTURES / SharedMemory / TEXTURE_KEY /
// ProducerStage）一律来自 SharedMem.h，此处不再重复定义。

// ================================================================
// 配置
// ================================================================
int         INPUT_W = 640;      // 如果模型期望别的尺寸，会自动调整
int         INPUT_H = 640;
const float CONF_THRESHOLD = 0.5f;
const float NMS_THRESHOLD = 0.45f;
const int   PERSON_CLASS_ID = 0;
const float HEAD_RATIO = 0.20f;  // 头部中心：检测框顶部往下 20% 处
const wchar_t MODEL_PATH[] = L"D:\\YOLOV8\\yolov8n.onnx";

struct Detection { float x, y, w, h, conf; };

// ================================================================
// 全局
// ================================================================
ID3D11Device* g_d3dDevice = nullptr;
ID3D11DeviceContext* g_d3dContext = nullptr;
ID2D1Factory* g_d2dFactory = nullptr;

ID3D11Texture2D* g_sharedTex[MAX_TEXTURES] = { nullptr, nullptr };
IDXGIKeyedMutex* g_keyedMutex[MAX_TEXTURES] = { nullptr, nullptr };
ID2D1RenderTarget* g_d2dRT[MAX_TEXTURES] = { nullptr, nullptr };
ID2D1SolidColorBrush* g_brush[MAX_TEXTURES] = { nullptr, nullptr };

SharedMemory* g_shared = nullptr;
HANDLE        g_hMapFile = nullptr;

// 鼠标自动跟随开关（F8 切换）
volatile bool g_mouseFollow = true;

// 运行标志：Ctrl+C / 关窗口时置 false，让主循环自己走完收尾
volatile bool g_running = true;

static void Log(const char* fmt, ...);   // 定义在下方，先前向声明

// ================================================================
// 对外声明退出
//
// 旧代码的 producerAlive 只被置 1、从来没有清 0，是个粘滞标志，导致
// Consumer 的"Producer lost"分支永远不可能触发：Producer 崩了或被杀，
// Consumer 会抱着最后一帧一直卡在屏幕上。真正的活性判据是 frameCounter，
// producerAlive 现在改成真正的双向标志（1=在跑，0=已退出）。
// ================================================================
static void MarkExited() {
    if (g_shared) {
        InterlockedExchange(&g_shared->producerAlive, 0);
        SmSetStage(g_shared, STAGE_EXITED);
    }
}

// 任何 return / 异常路径都会触发，保证一定对外声明过退出
struct ShutdownGuard {
    ~ShutdownGuard() {
        MarkExited();
        Log("[Producer] Exit\n");
    }
};

static BOOL WINAPI ConsoleCtrlHandler(DWORD type) {
    switch (type) {
    case CTRL_C_EVENT:
    case CTRL_BREAK_EVENT:
    case CTRL_CLOSE_EVENT:
    case CTRL_LOGOFF_EVENT:
    case CTRL_SHUTDOWN_EVENT:
        g_running = false;
        MarkExited();   // 立刻声明，别让 Consumer 一直等超时
        return TRUE;
    default:
        return FALSE;
    }
}

// ================================================================
// 日志：同时写控制台、调试器输出和 Producer.log（启动慢这类问题必须有时间线）
// ================================================================
static FILE* g_logFile = nullptr;

static void OpenLogFile() {
    WCHAR path[MAX_PATH + 8] = {};
    if (!GetModuleFileNameW(nullptr, path, MAX_PATH)) return;
    WCHAR* slash = wcsrchr(path, L'\\');
    if (slash) slash[1] = L'\0';
    wcscat_s(path, L"Producer.log");
    g_logFile = _wfopen(path, L"w");   // 每次启动覆盖，只保留最近一次现场
    // 写 UTF-8 BOM：记事本 / VS 靠它自动识别编码，否则按 GBK 打开就是乱码
    if (g_logFile) fputs("\xEF\xBB\xBF", g_logFile);
    if (!g_logFile) printf("[Producer] (无法创建 Producer.log: error=%lu)\n", GetLastError());
}

static void Log(const char* fmt, ...) {
    char buf[1024];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);

    SYSTEMTIME st;
    GetLocalTime(&st);
    char line[1152];
    int n = _snprintf_s(line, sizeof(line), _TRUNCATE, "[%02d:%02d:%02d.%03d] %s",
                        st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, buf);
    if (n < 0) return;

    fputs(line, stdout);
    fflush(stdout);
    OutputDebugStringA(line);
    if (g_logFile) { fputs(line, g_logFile); fflush(g_logFile); }
}

// ================================================================
// 屏幕尺寸
// ================================================================
static void GetScreenSize(int& w, int& h) {
    w = GetSystemMetrics(SM_CXSCREEN);
    h = GetSystemMetrics(SM_CYSCREEN);
    if (w <= 0) w = 1920;
    if (h <= 0) h = 1080;
}

// ================================================================
// GPU 适配器选择
// ================================================================
static IDXGIAdapter1* PickBestAdapter() {
    IDXGIFactory1* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&factory)))
        return nullptr;

    IDXGIAdapter1* best = nullptr;
    SIZE_T bestVram = 0;

    IDXGIAdapter1* adapter = nullptr;
    for (UINT i = 0; factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; i++) {
        DXGI_ADAPTER_DESC1 desc = {};
        adapter->GetDesc1(&desc);

        if (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) {
            adapter->Release();
            continue;
        }

        if (desc.DedicatedVideoMemory > bestVram) {
            if (best) best->Release();
            best = adapter;
            bestVram = desc.DedicatedVideoMemory;
        } else {
            adapter->Release();
        }
    }
    factory->Release();
    return best;
}

// ================================================================
// D3D11 设备
// ================================================================
static bool CreateD3D11Device() {
    IDXGIAdapter1* adapter = PickBestAdapter();
    if (adapter) {
        DXGI_ADAPTER_DESC1 desc = {};
        adapter->GetDesc1(&desc);
        Log("[Producer] Using GPU: %ls (VRAM=%llu MB)\n",
            desc.Description, desc.DedicatedVideoMemory / (1024 * 1024));
    }

    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
    D3D_FEATURE_LEVEL obtained;

    HRESULT hr = D3D11CreateDevice(
        adapter,
        adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE,
        nullptr, flags, levels, 2, D3D11_SDK_VERSION,
        &g_d3dDevice, &obtained, &g_d3dContext);

    if (adapter) adapter->Release();

    if (FAILED(hr)) {
        Log("[Producer] Hardware D3D11 failed (hr=0x%08X), trying WARP...\n", hr);
        hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
            flags, levels, 2, D3D11_SDK_VERSION,
            &g_d3dDevice, &obtained, &g_d3dContext);
    }
    return SUCCEEDED(hr);
}

// ================================================================
// 共享纹理 + D2D 绑定
// ================================================================
static bool CreateSharedTextures(UINT w, UINT h) {
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = w;
    desc.Height = h;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    desc.MiscFlags = D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX;

    for (int i = 0; i < MAX_TEXTURES; i++) {
        HRESULT hr = g_d3dDevice->CreateTexture2D(&desc, nullptr, &g_sharedTex[i]);
        if (FAILED(hr)) {
            Log("[Producer] CreateTexture2D[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }

        hr = g_sharedTex[i]->QueryInterface(__uuidof(IDXGIKeyedMutex), (void**)&g_keyedMutex[i]);
        if (FAILED(hr)) {
            Log("[Producer] KeyedMutex[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }

        IDXGISurface* surface = nullptr;
        hr = g_sharedTex[i]->QueryInterface(__uuidof(IDXGISurface), (void**)&surface);
        if (FAILED(hr)) {
            Log("[Producer] IDXGISurface[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }

        D2D1_RENDER_TARGET_PROPERTIES rtProps = D2D1::RenderTargetProperties(
            D2D1_RENDER_TARGET_TYPE_DEFAULT,
            D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED),
            96.0f, 96.0f);

        hr = g_d2dFactory->CreateDxgiSurfaceRenderTarget(surface, &rtProps, &g_d2dRT[i]);
        surface->Release();
        if (FAILED(hr)) {
            Log("[Producer] D2D RT[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }

        hr = g_d2dRT[i]->CreateSolidColorBrush(
            D2D1::ColorF(D2D1::ColorF::Lime), &g_brush[i]);
        if (FAILED(hr)) {
            Log("[Producer] Brush[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }

        // 纹理刚创建时显存内容未初始化。持锁清一次，保证 Consumer 第一次看到
        // 的是干净的全透明帧，而不是随机垃圾。注意这里必须用 TEXTURE_KEY(=0)：
        // 新建 keyed mutex 的 key 初始就是 0，Acquire(0) 会立即成功。
        if (SUCCEEDED(g_keyedMutex[i]->AcquireSync(TEXTURE_KEY, 1000))) {
            g_d2dRT[i]->BeginDraw();
            g_d2dRT[i]->Clear(D2D1::ColorF(0, 0, 0, 0));
            g_d2dRT[i]->EndDraw();
            g_keyedMutex[i]->ReleaseSync(TEXTURE_KEY);
        } else {
            Log("[Producer] texture[%d] 初始清理时取锁失败\n", i);
        }

        // 句柄放在最后取：等纹理内容已经干净了再对外暴露
        IDXGIResource* res = nullptr;
        hr = g_sharedTex[i]->QueryInterface(__uuidof(IDXGIResource), (void**)&res);
        if (FAILED(hr)) {
            Log("[Producer] IDXGIResource[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }
        hr = res->GetSharedHandle(&g_shared->textureHandles[i]);
        res->Release();
        if (FAILED(hr)) {
            Log("[Producer] GetSharedHandle[%d] failed, hr=0x%08X\n", i, hr);
            return false;
        }
        Log("[Producer] texture[%d] %ux%u handle=%p 已就绪\n",
            i, w, h, g_shared->textureHandles[i]);
    }
    return true;
}

// ================================================================
// 屏幕捕获 → letterbox 到 INPUT_W × INPUT_H
// ================================================================
// 复用的静态资源：只在屏幕尺寸/模型尺寸变化时重建
static HDC      g_hdcScreen = nullptr;
static HDC      g_hdcMem    = nullptr;
static HBITMAP  g_hbm       = nullptr;
static int      g_scrW = 0, g_scrH = 0;
static std::vector<uint8_t> g_bgra;   // 全屏 BGRA 原始数据
static std::vector<float>   g_input;   // 复用：缩放区每帧被全量覆写
static int      g_rw = 0, g_rh = 0, g_padX = 0, g_padY = 0;

// 最近邻缩放的查表：源列的字节偏移 / 源行号。几何不变时整帧复用，
// 把原本内层循环里每像素一次的 float 乘法和越界判断全部提出来。
static std::vector<int> g_xoff, g_yoff;
static constexpr float  kInv255 = 1.0f / 255.0f;

static void CaptureScreen(int& outW, int& outH,
    int& outPadX, int& outPadY, std::vector<float>& input) {
    int sw = 0, sh = 0;
    GetScreenSize(sw, sh);
    outW = sw;
    outH = sh;

    // ---- GDI 资源：按需重建（分辨率变化才动）----
    if (!g_hdcScreen) g_hdcScreen = GetDC(NULL);
    if (!g_hdcMem)    g_hdcMem = CreateCompatibleDC(g_hdcScreen);
    if (!g_hbm || sw != g_scrW || sh != g_scrH) {
        if (g_hbm) { SelectObject(g_hdcMem, g_hbm); DeleteObject(g_hbm); }
        g_hbm = CreateCompatibleBitmap(g_hdcScreen, sw, sh);
        SelectObject(g_hdcMem, g_hbm);
        g_scrW = sw; g_scrH = sh;
        g_bgra.resize((size_t)sw * sh * 4);
    }

    BitBlt(g_hdcMem, 0, 0, sw, sh, g_hdcScreen, 0, 0, SRCCOPY);

    BITMAPINFOHEADER bi = {};
    bi.biSize = sizeof(bi);
    bi.biWidth = sw;
    bi.biHeight = -sh;
    bi.biPlanes = 1;
    bi.biBitCount = 32;
    bi.biCompression = BI_RGB;
    GetDIBits(g_hdcMem, g_hbm, 0, sh, g_bgra.data(), (BITMAPINFO*)&bi, DIB_RGB_COLORS);

    // ---- letterbox 几何 ----
    float scale = std::min((float)INPUT_W / sw, (float)INPUT_H / sh);
    int rw = (int)(sw * scale);
    int rh = (int)(sh * scale);
    outPadX = g_padX = (INPUT_W - rw) / 2;
    outPadY = g_padY = (INPUT_H - rh) / 2;

    // ---- 缩放查表：只在几何变化时重建（注意要在 g_rw/g_rh 被覆盖之前判断）----
    if (rw != g_rw || rh != g_rh || (int)g_xoff.size() != rw) {
        const float invScale = 1.0f / scale;
        g_xoff.resize(rw);
        for (int dx = 0; dx < rw; dx++) {
            int sx = (int)(dx * invScale);
            if (sx >= sw) sx = sw - 1;
            g_xoff[dx] = sx * 4;          // 直接存字节偏移，内层免乘
        }
        g_yoff.resize(rh);
        for (int dy = 0; dy < rh; dy++) {
            int sy = (int)(dy * invScale);
            if (sy >= sh) sy = sh - 1;
            g_yoff[dy] = sy;
        }
    }

    const size_t plane = (size_t)INPUT_W * INPUT_H;
    // 尺寸变化才重新铺灰底；稳态下缩放区被全量覆写，无需重置
    if (input.size() != 3 * plane ||
        rw != g_rw || rh != g_rh ||
        g_padX != (INPUT_W - rw) / 2 || g_padY != (INPUT_H - rh) / 2) {
        input.assign(3 * plane, 114.0f * kInv255);
    }
    g_rw = rw; g_rh = rh;

    const uint8_t* base = g_bgra.data();
    for (int dy = 0; dy < rh; dy++) {
        const uint8_t* srcRow = base + (size_t)g_yoff[dy] * sw * 4;
        size_t o = (size_t)(dy + g_padY) * INPUT_W + g_padX;
        for (int dx = 0; dx < rw; dx++, o++) {
            const uint8_t* p = srcRow + g_xoff[dx];
            input[0 * plane + o] = p[2] * kInv255;   // BGR -> RGB
            input[1 * plane + o] = p[1] * kInv255;
            input[2 * plane + o] = p[0] * kInv255;
        }
    }
}

// ================================================================
// 后处理（带越界保护）
// ================================================================
static void PostProcess(const float* output, int numBoxes,
    int sw, int sh, int padX, int padY,
    std::vector<Detection>& dets) {
    // ⭐ 越界保护
    if (numBoxes <= 0 || numBoxes > 100000) {
        Log("[Producer] PostProcess: invalid numBoxes=%d, skipping\n", numBoxes);
        return;
    }
    if (sw <= 0 || sh <= 0) return;

    float scale = std::min((float)INPUT_W / sw, (float)INPUT_H / sh);
    const float* conf = output + (size_t)(4 + PERSON_CLASS_ID) * numBoxes;

    for (int i = 0; i < numBoxes; i++) {
        if (conf[i] < CONF_THRESHOLD) continue;
        float cx = output[0 * numBoxes + i];
        float cy = output[1 * numBoxes + i];
        float w = output[2 * numBoxes + i];
        float h = output[3 * numBoxes + i];

        Detection d;
        d.w = w / scale;
        d.h = h / scale;
        d.x = (cx - w / 2.0f - padX) / scale;
        d.y = (cy - h / 2.0f - padY) / scale;
        d.conf = conf[i];
        dets.push_back(d);
    }

    std::sort(dets.begin(), dets.end(),
        [](const Detection& a, const Detection& b) { return a.conf > b.conf; });

    std::vector<Detection> result;
    std::vector<char> suppressed(dets.size(), 0);
    for (size_t i = 0; i < dets.size(); i++) {
        if (suppressed[i]) continue;
        result.push_back(dets[i]);
        for (size_t j = i + 1; j < dets.size(); j++) {
            if (suppressed[j]) continue;
            float x1 = std::max(dets[i].x, dets[j].x);
            float y1 = std::max(dets[i].y, dets[j].y);
            float x2 = std::min(dets[i].x + dets[i].w, dets[j].x + dets[j].w);
            float y2 = std::min(dets[i].y + dets[i].h, dets[j].y + dets[j].h);
            float inter = std::max(0.0f, x2 - x1) * std::max(0.0f, y2 - y1);
            float iou = inter / (dets[i].w * dets[i].h + dets[j].w * dets[j].h - inter + 1e-6f);
            if (iou > NMS_THRESHOLD) suppressed[j] = 1;
        }
    }
    dets = result;
}

// ================================================================
// 渲染到共享纹理
//
// 双方统一使用 TEXTURE_KEY(=0)。因为 key 恒为 0，Producer 永远拿得到锁，
// 不依赖 Consumer 是否存在 —— 旧代码把初始 key 设成 1，Producer 每帧
// AcquireSync(0) 必然超时失败，于是纹理一个像素都没画过，而且白烧 16ms/帧。
//
// 取锁用超时 0（非阻塞）：抢不到就跳过这一帧。Producer 是唯一写者，没有它
// 就没有框，所以绝不能因为一个缺席的观察者而卡住或放弃绘制。
// ================================================================
static volatile LONG g_renderDrawn   = 0;   // 成功绘制的帧
static volatile LONG g_renderSkipped = 0;   // 因 Consumer 正在读而跳过的帧

static void RenderFrame(int index, const std::vector<Detection>& dets) {
    if (!g_keyedMutex[index] || !g_d2dRT[index] || !g_brush[index]) return;

    HRESULT hr = g_keyedMutex[index]->AcquireSync(TEXTURE_KEY, 0);
    if (hr != S_OK) {
        // WAIT_TIMEOUT / WAIT_ABANDONED 都表示 Consumer 正在读这张纹理，跳过本帧
        InterlockedIncrement(&g_renderSkipped);
        return;
    }

    ID2D1RenderTarget* rt = g_d2dRT[index];
    rt->BeginDraw();
    rt->Clear(D2D1::ColorF(0, 0, 0, 0));

    // 整帧颜色相同，没必要在循环里反复 SetColor
    g_brush[index]->SetColor(D2D1::ColorF(D2D1::ColorF::Lime));
    for (const auto& d : dets) {
        rt->DrawRectangle(D2D1::RectF(d.x, d.y, d.x + d.w, d.y + d.h),
            g_brush[index], 3.0f);
    }

    hr = rt->EndDraw();
    g_keyedMutex[index]->ReleaseSync(TEXTURE_KEY);

    if (FAILED(hr)) {
        Log("[Producer] EndDraw[%d] failed, hr=0x%08X\n", index, hr);
    } else {
        InterlockedIncrement(&g_renderDrawn);
    }
}

// ================================================================
// 主函数
// ================================================================
int main() {
    AllocConsole();
    freopen("CONOUT$", "w", stdout);
    freopen("CONOUT$", "w", stderr);
    SetConsoleTitleW(L"Producer - YOLO Overlay");
    OpenLogFile();
    SetConsoleOutputCP(CP_UTF8);   // 控制台切 UTF-8，否则中文日志在 GBK 控制台上是乱码
    SetConsoleCtrlHandler(ConsoleCtrlHandler, TRUE);
    ShutdownGuard shutdownGuard;   // 作用域退出时统一声明退出

    Log("[Producer] ===== START =====\n");
    Log("[Producer] MODEL_PATH = %ls\n", MODEL_PATH);

    try {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

        // ---- 共享内存 ----
        Log("[Producer] Step 1/5: Creating shared memory...\n");
        g_hMapFile = CreateFileMapping(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
            0, sizeof(SharedMemory), SHARED_MEM_NAME);
        if (!g_hMapFile) {
            Log("[Producer] FATAL: CreateFileMapping failed, error=%lu\n", GetLastError());
            system("pause"); return 1;
        }
        g_shared = (SharedMemory*)MapViewOfFile(g_hMapFile, FILE_MAP_ALL_ACCESS,
            0, 0, sizeof(SharedMemory));
        if (!g_shared) {
            Log("[Producer] FATAL: MapViewOfFile failed, error=%lu\n", GetLastError());
            system("pause"); return 1;
        }
        ZeroMemory(g_shared, sizeof(SharedMemory));
        SmSetStage(g_shared, STAGE_BOOTING);
        InterlockedExchange(&g_shared->producerAlive, 1);
        InterlockedExchange(&g_shared->producerPid, (LONG)GetCurrentProcessId());
        Log("[Producer] Shared memory OK (stage=BOOTING, pid=%lu)\n", GetCurrentProcessId());

        // ---- D3D11 ----
        Log("[Producer] Step 2/5: Creating D3D11 device...\n");
        if (!CreateD3D11Device()) {
            Log("[Producer] FATAL: D3D11 device creation failed\n");
            system("pause"); return 1;
        }
        Log("[Producer] D3D11 device OK\n");

        // ---- D2D ----
        Log("[Producer] Step 3/5: Creating D2D factory...\n");
        HRESULT hr = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, &g_d2dFactory);
        if (FAILED(hr)) {
            Log("[Producer] FATAL: D2D1CreateFactory failed, hr=0x%08X\n", hr);
            system("pause"); return 1;
        }
        Log("[Producer] D2D factory OK\n");
        SmSetStage(g_shared, STAGE_DEVICE);

        // ---- 共享纹理 ------------------------------------------------
        // ⚠ 必须排在 ONNX 加载之前。
        //   旧代码把纹理创建放在模型加载之后，于是"就绪信号"(textureHandles)
        //   是整个启动流程里最后才发布的一项，而它的前面紧挨着最贵的
        //   ONNX + DirectML 初始化（首次还要编译 shader 缓存）。Consumer 只能
        //   靠一个固定超时去猜，通常就误判成"Producer 没起来"。
        //   纹理本身只依赖屏幕尺寸，跟模型输入尺寸无关，挪到这里是安全的。
        int scrW = 0, scrH = 0;
        GetScreenSize(scrW, scrH);
        Log("[Producer] Step 3/5: Creating shared textures (%dx%d)...\n", scrW, scrH);
        g_shared->width  = scrW;
        g_shared->height = scrH;

        if (!CreateSharedTextures(scrW, scrH)) {
            Log("[Producer] FATAL: Shared texture creation failed\n");
            system("pause"); return 1;
        }
        // 两张纹理的句柄都写完之后才对外宣布就绪，Consumer 只需判断这一个字段，
        // 不会再撞上"句柄0非空但句柄1还是空"的中间态。
        SmSetStage(g_shared, STAGE_TEXTURES);
        Log("[Producer] Shared textures OK (stage=TEXTURES, Consumer 可附着)\n");

        // ---- 加载 YOLO 先看模型的期望输入尺寸 ----
        // 这一步最慢（ONNX + DirectML 首次还要编译 shader）。放在纹理之后，
        // 慢的代价就不再拖累 Consumer 的握手了。
        Log("[Producer] Step 4/5: Loading YOLO model (可能耗时数十秒)...\n");
        SmSetStage(g_shared, STAGE_MODEL);
        Ort::Env env(ORT_LOGGING_LEVEL_WARNING, "Producer");
        Ort::SessionOptions opts;
        opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

        // 启用 DirectML GPU 推理（失败则回退 CPU）
        bool useDml = false;
        try {
            Ort::ThrowOnError(OrtSessionOptionsAppendExecutionProvider_DML(opts, 0));
            useDml = true;
            Log("[Producer] DirectML GPU provider enabled\n");
        } catch (const Ort::Exception& e) {
            Log("[Producer] DirectML unavailable (%s), falling back to CPU\n", e.what());
        }

        std::unique_ptr<Ort::Session> session;
        try {
            session = std::make_unique<Ort::Session>(env, MODEL_PATH, opts);
        }
        catch (const Ort::Exception& e) {
            Log("[Producer] FATAL: Model load failed: %s\n", e.what());
            system("pause"); return 1;
        }
        Log("[Producer] YOLO model loaded OK\n");

        // ⭐ 打印模型期望的输入 shape，并自动调整 INPUT_W / INPUT_H
        {
            auto inputInfo = session->GetInputTypeInfo(0);
            auto tensorInfo = inputInfo.GetTensorTypeAndShapeInfo();
            auto modelShape = tensorInfo.GetShape();

            Log("[Producer] Model input shape:");
            for (auto d : modelShape) Log(" %lld", (long long)d);
            Log("\n");

            // 格式一般为 [1, 3, H, W]
            if (modelShape.size() == 4) {
                if (modelShape[2] > 0) INPUT_H = (int)modelShape[2];
                if (modelShape[3] > 0) INPUT_W = (int)modelShape[3];
                Log("[Producer] Using INPUT_W=%d, INPUT_H=%d\n", INPUT_W, INPUT_H);
            }
        }

        Ort::AllocatorWithDefaultOptions alloc;
        auto inName = session->GetInputNameAllocated(0, alloc);
        auto outName = session->GetOutputNameAllocated(0, alloc);
        Log("[Producer] Input name  = %s\n", inName.get());
        Log("[Producer] Output name = %s\n", outName.get());

        const char* inputNames[] = { inName.get() };
        const char* outputNames[] = { outName.get() };

        std::vector<int64_t> inputShape = { 1, 3, INPUT_H, INPUT_W };
        Ort::MemoryInfo memInfo = Ort::MemoryInfo::CreateCpu(
            OrtArenaAllocator, OrtMemTypeDefault);

        // ---- 主循环 ----
        Log("[Producer] Step 5/5: Entering main loop\n");
        Log("[Producer] 鼠标跟随：开  (按 F8 切换)\n");
        Log("[Producer] 不需要 Consumer 也能工作：框直接画进共享纹理\n");

        int writeIndex = 0;
        int frameCount = 0;
        bool firstInferenceDone = false;
        bool readyAnnounced = false;
        std::vector<float> inputBuf;   // 复用缓冲区，跨帧不重新分配

        while (g_running) {
            int pw = 0, ph = 0, padX = 0, padY = 0;
            CaptureScreen(pw, ph, padX, padY, inputBuf);

            Ort::Value tensor = Ort::Value::CreateTensor<float>(
                memInfo, inputBuf.data(), inputBuf.size(),
                inputShape.data(), inputShape.size());

            std::vector<Detection> dets;
            try {
                auto outputs = session->Run(Ort::RunOptions{ nullptr },
                    inputNames, &tensor, 1, outputNames, 1);

                float* outData = outputs[0].GetTensorMutableData<float>();
                auto shape = outputs[0].GetTensorTypeAndShapeInfo().GetShape();

                // ⭐ 首次推理后打印输出 shape，便于排查
                if (!firstInferenceDone) {
                    Log("[Producer] Output shape:");
                    for (auto d : shape) Log(" %lld", (long long)d);
                    Log("\n");
                    firstInferenceDone = true;
                }

                int numBoxes = 0;
                if (shape.size() == 3) {
                    if (shape[1] == 84)      numBoxes = (int)shape[2];
                    else if (shape[2] == 84) numBoxes = (int)shape[1];
                    else                     numBoxes = (int)shape[2];
                }

                PostProcess(outData, numBoxes, pw, ph, padX, padY, dets);
            }
            catch (const Ort::Exception& e) {
                static bool printed = false;
                if (!printed) {
                    Log("[Producer] Inference failed: %s\n", e.what());
                    printed = true;
                }
                Sleep(100);
                continue;
            }

            // ===== F8 切换鼠标跟随开关 =====
            // 低位(0x0001)="自上次调用以来发生过按下"，锁存事件，快按也不会漏
            if (GetAsyncKeyState(VK_F8) & 0x0001) {
                g_mouseFollow = !g_mouseFollow;
                Log("[Producer] 鼠标跟随 = %s\n", g_mouseFollow ? "开" : "关");
            }

            // ===== 每帧跟随：选离当前鼠标最近的目标，移到其头部 =====
            if (g_mouseFollow && !dets.empty()) {
                POINT cur;
                if (GetCursorPos(&cur)) {
                    // 用头部位置算距离，保证"选目标"和"移到哪"标准一致
                    const Detection* best = &dets[0];
                    double bestDist = DBL_MAX;
                    int bx = 0, by = 0;
                    for (const auto& d : dets) {
                        int cx = (int)(d.x + d.w / 2.0f);
                        int cy = (int)(d.y + d.h * HEAD_RATIO);
                        double dist = std::hypot(cur.x - cx, cur.y - cy);
                        if (dist < bestDist) {
                            bestDist = dist;
                            best = &d;
                            bx = cx; by = cy;
                        }
                    }
                    if (cur.x != bx || cur.y != by) {
                        SetCursorPos(bx, by);
                    }
                }
            }

            RenderFrame(writeIndex, dets);

            // 先把帧内容和帧号发布出去，再宣布 READY —— 保证 Consumer 一旦看到
            // STAGE_READY 就一定已经能拿到至少一帧，不会读到空窗。
            InterlockedExchange(&g_shared->frameIndex, writeIndex);
            InterlockedIncrement(&g_shared->frameCounter);
            InterlockedExchange(&g_shared->producerPid, (LONG)GetCurrentProcessId());
            InterlockedExchange(&g_shared->producerAlive, 1);
            if (!readyAnnounced) {
                readyAnnounced = true;
                SmSetStage(g_shared, STAGE_READY);
                Log("[Producer] ===== READY (stage=READY, 首帧已发布) =====\n");
            }

            writeIndex = (writeIndex + 1) % MAX_TEXTURES;

            if ((++frameCount % 60) == 0) {
                Log("[Producer] Frame %d, dets=%zu, writeIdx=%d, drawn=%ld, skipped=%ld\n",
                    frameCount, dets.size(), writeIndex,
                    InterlockedCompareExchange(&g_renderDrawn, 0, 0),
                    InterlockedCompareExchange(&g_renderSkipped, 0, 0));
            }

            if (!g_running) break;
            Sleep(16);
        }
    }
    catch (const Ort::Exception& e) {
        Log("[Producer] ONNX exception: %s\n", e.what());
        system("pause");
        return 1;
    }
    catch (const std::exception& e) {
        Log("[Producer] std::exception: %s\n", e.what());
        system("pause");
        return 1;
    }
    catch (...) {
        Log("[Producer] Unknown exception\n");
        system("pause");
        return 1;
    }

    return 0;
}
