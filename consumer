#define _CRT_SECURE_NO_WARNINGS
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <cstdio>
#include <cstdarg>
#include <cstdint>
#include <cstring>
#include <cwchar>

#include "SharedMem.h"   // 共享内存协议 —— 与 Producer 共用同一份定义

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

// ================================================================
// Consumer 的全部职责
// ---------------------------------------------------------------
// 它是一个**只读预览窗口**：把 Producer 画进共享纹理的检测框拷到自己窗口上。
//
// 它不做屏幕捕获、不做推理、不做鼠标跟随 —— 全部在 Producer 里。
// 删掉 Consumer.exe，Producer 的瞄准功能一点不受影响。
//
// 帧同步约定（两边必须一致）：双方都用 TEXTURE_KEY(=0)。
//   Producer: Acquire(0, 非阻塞) 画 -> Release(0)
//   Consumer: Acquire(0, 有超时) 拷 -> Release(0)
// key 恒为 0，所以 Producer 不依赖 Consumer 是否存在。
// ================================================================

// ---- 等待预算 ------------------------------------------------------
// 这些上限不是"猜测 Producer 有多慢"，而是纯粹的兜底：正常路径下由
// producerStage 驱动立刻返回，只有 Producer 真出问题时才会走到超时。
// ONNX + DirectML 首次加载可能要几十秒（要编译 shader 缓存），所以给得宽。
const DWORD MAP_WAIT_MS    = 60000;    // 等共享内存出现（Producer 还没启动时）
const DWORD TEX_WAIT_MS    = 180000;   // 等纹理句柄（现在 Producer 很早就发布，理论秒进）
const DWORD READY_WAIT_MS  = 300000;   // 等首帧（真正慢的是这一步）
const DWORD STALL_MS       = 6000;     // stage=READY 之后帧号停滞多久判定卡死

// ================================================================
// 全局
// ================================================================
HWND g_hwnd = nullptr;
ID3D11Device* g_device = nullptr;
ID3D11DeviceContext* g_context = nullptr;
IDXGISwapChain1* g_swapChain = nullptr;
ID3D11Texture2D* g_backBuf = nullptr;   // CopySubresourceRegion 需要 ID3D11Resource*
ID3D11RenderTargetView* g_rtv = nullptr; // 仅用于把窗口多出来的区域清干净
UINT g_backBufW = 0, g_backBufH = 0;

ID3D11Texture2D* g_sharedTex[MAX_TEXTURES] = { nullptr, nullptr };
IDXGIKeyedMutex* g_keyedMutex[MAX_TEXTURES] = { nullptr, nullptr };

SharedMemory* g_shared = nullptr;
HANDLE        g_hMapFile = nullptr;

bool g_running = true;
bool g_needReattach = false;   // 收到 WAIT_ABANDONED：上一个纹理持有者崩了

// overlay 模式（--overlay）：全屏透明置顶窗口，框直接叠在游戏画面上
bool g_overlayMode = false;

// ================================================================
// 日志：控制台 + 调试器 + Consumer.log
// ================================================================
static FILE* g_logFile = nullptr;

static void OpenLogFile() {
    WCHAR path[MAX_PATH + 8] = {};
    if (!GetModuleFileNameW(nullptr, path, MAX_PATH)) return;
    WCHAR* slash = wcsrchr(path, L'\\');
    if (slash) slash[1] = L'\0';
    wcscat_s(path, L"Consumer.log");
    g_logFile = _wfopen(path, L"w");
    // 写 UTF-8 BOM：记事本 / VS 靠它自动识别编码，否则按 GBK 打开就是乱码
    if (g_logFile) fputs("\xEF\xBB\xBF", g_logFile);
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

static const char* StageName(LONG s) {
    switch (s) {
    case STAGE_BOOTING:  return "BOOTING(加载中)";
    case STAGE_DEVICE:   return "DEVICE(设备就绪)";
    case STAGE_TEXTURES: return "TEXTURES(纹理就绪)";
    case STAGE_MODEL:    return "MODEL(模型加载中)";
    case STAGE_READY:    return "READY(可收帧)";
    case STAGE_EXITED:   return "EXITED(已退出)";
    default:             return "UNKNOWN";
    }
}

// ================================================================
// 选择显存最大的独显（与 Producer 一致）
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
        }
        else {
            adapter->Release();
        }
    }
    factory->Release();
    return best;
}

// ================================================================
// 窗口过程
// ================================================================
LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_DESTROY) {
        g_running = false;
        PostQuitMessage(0);
        return 0;
    }
    // overlay 模式下绝不能因为鼠标落在窗口上就抢走焦点，
    // 否则玩家一移动鼠标焦点就跳到 overlay 上，游戏直接失去输入。
    if (m == WM_MOUSEACTIVATE) {
        return MA_NOACTIVATE;
    }
    if (m == WM_SIZE && w != SIZE_MINIMIZED) {
        // 刻意不 ResizeBuffers：交换链保持共享纹理的原始尺寸(全屏)，
        // 由 DXGI 在 Present 时自动缩放到窗口客户区。这样无论怎么拖动窗口，
        // 看到的都是**完整的一帧**；一旦 ResizeBuffers 把 backbuffer 改成
        // 客户区尺寸，就只剩左上角裁剪了。
        return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

// ================================================================
// D3D11 + SwapChain（两步走，显式选择与 Producer 同一块 GPU）
// ================================================================
static bool MakeBackBuffer(UINT& w, UINT& h) {
    ID3D11Texture2D* back = nullptr;
    HRESULT hr = g_swapChain->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&back);
    if (FAILED(hr)) {
        Log("[Consumer] GetBuffer failed, hr=0x%08X\n", hr);
        return false;
    }
    D3D11_TEXTURE2D_DESC d = {};
    back->GetDesc(&d);
    w = d.Width; h = d.Height;

    if (g_rtv) { g_rtv->Release(); g_rtv = nullptr; }
    hr = g_device->CreateRenderTargetView(back, nullptr, &g_rtv);
    if (FAILED(hr)) {
        Log("[Consumer] CreateRenderTargetView failed, hr=0x%08X\n", hr);
        back->Release();
        return false;
    }
    if (g_backBuf) g_backBuf->Release();
    g_backBuf = back;
    return true;
}

static bool InitD3D(HWND hwnd, UINT width, UINT height) {
    IDXGIAdapter1* adapter = PickBestAdapter();
    if (adapter) {
        DXGI_ADAPTER_DESC1 desc = {};
        adapter->GetDesc1(&desc);
        Log("[Consumer] Using GPU: %ls (VRAM=%llu MB)\n",
            desc.Description, desc.DedicatedVideoMemory / (1024 * 1024));
    }

    D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
    D3D_FEATURE_LEVEL obtained;

    // 第 1 步：创建设备
    HRESULT hr = D3D11CreateDevice(
        adapter,
        adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE,
        nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        levels, 2, D3D11_SDK_VERSION,
        &g_device, &obtained, &g_context);

    if (adapter) adapter->Release();

    if (FAILED(hr)) {
        Log("[Consumer] D3D11CreateDevice failed, hr=0x%08X\n", hr);
        return false;
    }

    // 第 2 步：创建 SwapChain（用同一个 device）
    IDXGIFactory2* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory2), (void**)&factory))) {
        Log("[Consumer] CreateDXGIFactory1 failed\n");
        return false;
    }

    DXGI_SWAP_CHAIN_DESC1 scd = {};
    scd.Width = width;
    scd.Height = height;
    scd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    scd.SampleDesc.Count = 1;
    scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.BufferCount = 2;
    scd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    // overlay 必须用预乘 alpha 交换链；预览窗口保持 opaque。
    // Producer 画的框本来就是"透明底 + 预乘 alpha"，两者一致才能正确合成。
    scd.AlphaMode = g_overlayMode ? DXGI_ALPHA_MODE_PREMULTIPLIED
                                  : DXGI_ALPHA_MODE_IGNORE;

    hr = factory->CreateSwapChainForHwnd(
        g_device, hwnd, &scd, nullptr, nullptr, &g_swapChain);
    factory->Release();

    if (FAILED(hr)) {
        Log("[Consumer] CreateSwapChainForHwnd failed, hr=0x%08X\n", hr);
        return false;
    }
    return MakeBackBuffer(g_backBufW, g_backBufH);
}

// ================================================================
// 共享纹理的附着 / 分离
// ================================================================
static void DetachTextures() {
    for (int i = 0; i < MAX_TEXTURES; i++) {
        // 必须先放 mutex，再放纹理：还持有锁就销毁资源会让 mutex 处于 abandoned
        if (g_keyedMutex[i]) { g_keyedMutex[i]->Release(); g_keyedMutex[i] = nullptr; }
        if (g_sharedTex[i]) { g_sharedTex[i]->Release();  g_sharedTex[i] = nullptr; }
    }
}

static bool AttachTextures() {
    for (int i = 0; i < MAX_TEXTURES; i++) {
        HANDLE h = SmGetTexture(g_shared, i);
        if (!h) {
            Log("[Consumer] textureHandles[%d] 仍是 NULL\n", i);
            DetachTextures();
            return false;
        }
        HRESULT hr = g_device->OpenSharedResource(
            h, __uuidof(ID3D11Texture2D), (void**)&g_sharedTex[i]);
        if (FAILED(hr)) {
            Log("[Consumer] OpenSharedResource[%d] failed, hr=0x%08X handle=%p\n", i, hr, h);
            DetachTextures();
            return false;
        }
        hr = g_sharedTex[i]->QueryInterface(__uuidof(IDXGIKeyedMutex), (void**)&g_keyedMutex[i]);
        if (FAILED(hr)) {
            Log("[Consumer] QueryInterface(KeyedMutex)[%d] failed, hr=0x%08X\n", i, hr);
            DetachTextures();
            return false;
        }
        D3D11_TEXTURE2D_DESC d = {};
        g_sharedTex[i]->GetDesc(&d);
        Log("[Consumer] shared texture[%d] 已附着 %ux%u handle=%p\n", i, d.Width, d.Height, h);
    }
    return true;
}

// ================================================================
// 按 producerStage 等待，而不是拍脑袋的固定超时
// ---------------------------------------------------------------
// 旧代码只有"死等 textureHandles[0] 10 秒"，Producer 慢一点就直接
// system("pause") 挂死，而且日志里看不出它卡在启动链的哪一步。
// 这里改成：等目标 stage、周期性打印当前 stage / 句柄 / 已耗时，
// 慢启动能看得见、真卡死能定位到具体环节。
// ================================================================
static bool WaitForStage(LONG target, DWORD budgetMs, const char* what) {
    const DWORD t0 = GetTickCount();
    LONG lastStage = -2;

    while (g_running) {
        LONG stage = SmGetStage(g_shared);

        if (stage == STAGE_EXITED) {
            Log("[Consumer] Producer 已退出，放弃等待 %s\n", what);
            return false;
        }
        if (stage >= target) {
            Log("[Consumer] %s 就绪 (stage=%s, 耗时 %.1fs)\n",
                what, StageName(stage), (GetTickCount() - t0) / 1000.0);
            return true;
        }

        DWORD el = GetTickCount() - t0;
        if (stage != lastStage) {
            lastStage = stage;
            Log("[Consumer] 等待 %s: stage=%s pid=%ld 句柄=%p/%p (%.1fs)\n",
                what, StageName(stage), SmGetPid(g_shared),
                SmGetTexture(g_shared, 0), SmGetTexture(g_shared, 1),
                el / 1000.0);
        }
        else if (el % 3000 < 120) {
            Log("[Consumer]   ...仍在等待 %s: stage=%s (%.1fs)\n",
                what, StageName(stage), el / 1000.0);
        }

        if (el > budgetMs) {
            Log("[Consumer] 超时(%lu ms): 等待 %s 未达成，Producer 停在 stage=%s\n",
                budgetMs, what, StageName(stage));
            return false;
        }
        Sleep(100);
    }
    return false;
}

// ================================================================
// 把共享纹理拷到窗口
// ================================================================
static bool BlitSharedFrame(int idx) {
    if (idx < 0 || idx >= MAX_TEXTURES) return false;
    if (!g_keyedMutex[idx] || !g_sharedTex[idx] || !g_swapChain) return false;

    // 与 Producer 一致用 TEXTURE_KEY(=0)。key 恒为 0，这里能稳定拿到锁；
    // 拿不到只是说明 Producer 正在写这一张，跳过即可，绝不阻塞渲染。
    HRESULT hr = g_keyedMutex[idx]->AcquireSync(TEXTURE_KEY, 100);
    if (hr == WAIT_ABANDONED) {
        // 上一个持有者进程崩了，这张纹理的内容已经不可信，必须重新附着
        Log("[Consumer] texture[%d] AcquireSync 返回 WAIT_ABANDONED\n", idx);
        g_needReattach = true;
        return false;
    }
    if (FAILED(hr)) return false;

    // ⚠ 必须每帧重新取 back buffer，不能缓存指针。
    //   DXGI_SWAP_EFFECT_DISCARD 是 blit model，双缓冲在 Present 时轮换缓冲区
    //   索引：缓存下来的 buffer 0 在第一帧之后就变成了前台缓冲，于是我们一直
    //   在写前台缓冲，而 Present 显示的是另一张从未写入的缓冲 —— 表现为纯黑。
    ID3D11Texture2D* back = nullptr;
    HRESULT hrGet = g_swapChain->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&back);
    if (FAILED(hrGet)) {
        g_keyedMutex[idx]->ReleaseSync(TEXTURE_KEY);
        Log("[Consumer] GetBuffer failed, hr=0x%08X\n", (unsigned)hrGet);
        return false;
    }

    D3D11_TEXTURE2D_DESC sd = {}, bd = {};
    g_sharedTex[idx]->GetDesc(&sd);
    back->GetDesc(&bd);

    if (bd.Width == sd.Width && bd.Height == sd.Height) {
        g_context->CopyResource(back, g_sharedTex[idx]);
    } else {
        // 窗口比共享纹理小（换了分辨率之类），只拷重叠区域避免尺寸不匹配
        UINT cw = (bd.Width  < sd.Width)  ? bd.Width  : sd.Width;
        UINT ch = (bd.Height < sd.Height) ? bd.Height : sd.Height;
        D3D11_BOX box = { 0, 0, cw, ch, 0, 1 };
        g_context->CopySubresourceRegion(back, 0, 0, 0, 0, g_sharedTex[idx], 0, &box);
    }

    back->Release();
    g_keyedMutex[idx]->ReleaseSync(TEXTURE_KEY);
    HRESULT hrPresent = g_swapChain->Present(0, 0);
    if (FAILED(hrPresent)) {
        Log("[Consumer] Present failed, hr=0x%08X\n", (unsigned)hrPresent);
    }
    return true;
}

// ================================================================
// 主函数
// ================================================================
int main(int argc, char** argv) {
    AllocConsole();
    freopen("CONOUT$", "w", stdout);
    freopen("CONOUT$", "w", stderr);
    SetConsoleTitleW(L"Consumer - YOLO Overlay");
    OpenLogFile();
    // 控制台代码页切 UTF-8，否则中文日志在 GBK 控制台上是乱码。
    // 必须在第一行 Log 之前设置。
    SetConsoleOutputCP(CP_UTF8);

    Log("[Consumer] ===== START =====\n");
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    // --overlay：全屏透明置顶，框直接叠在游戏画面上（鼠标穿透、不抢焦点）
    // 无参数：普通预览窗口，用于调试看框
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--overlay") == 0) {
            g_overlayMode = true;
        }
    }
    Log("[Consumer] 模式 = %s\n",
        g_overlayMode ? "overlay (全屏透明置顶/鼠标穿透)" : "preview (预览窗口)");

    // ---- 等共享内存出现 --------------------------------------------
    // 旧代码在这里一失败就退出，所以"Consumer 先启动"直接没戏。
    // 现在改成轮询等待，让启动顺序无关。
    Log("[Consumer] 等待 Producer 创建共享内存...\n");
    {
        const DWORD t0 = GetTickCount();
        bool first = true;
        while (g_running) {
            g_hMapFile = OpenFileMapping(FILE_MAP_ALL_ACCESS, FALSE, SHARED_MEM_NAME);
            if (g_hMapFile) break;
            DWORD el = GetTickCount() - t0;
            if (first || el % 3000 < 120) {
                first = false;
                Log("[Consumer]   ...尚未发现共享内存 (%.1fs)\n", el / 1000.0);
            }
            if (el > MAP_WAIT_MS) {
                Log("[Consumer] 超时(%lu ms): Producer 似乎没在运行\n", MAP_WAIT_MS);
                return 1;
            }
            Sleep(100);
        }
        if (!g_hMapFile) { Log("[Consumer] 退出\n"); return 1; }
    }
    g_shared = (SharedMemory*)MapViewOfFile(g_hMapFile, FILE_MAP_ALL_ACCESS,
        0, 0, sizeof(SharedMemory));
    if (!g_shared) {
        Log("[Consumer] MapViewOfFile failed, error=%lu\n", GetLastError());
        return 1;
    }
    Log("[Consumer] 共享内存已映射\n");

    // ---- 等纹理句柄（Producer 一旦走到 TEXTURES 就立刻返回）---------
    if (!WaitForStage(STAGE_TEXTURES, TEX_WAIT_MS, "Producer 纹理")) {
        return 1;
    }

    // Producer 在宣布 TEXTURES 之前已经把两个句柄都写好了，所以这里
    // 不再需要"看句柄0非空就以为就绪"这种会撞上中间态的猜测。
    Log("[Consumer] Producer 屏幕尺寸 = %ux%u\n", g_shared->width, g_shared->height);

    // ---- 建窗口 ---------------------------------------------------
    WNDCLASSEXW wc = { sizeof(wc) };
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = g_overlayMode ? L"OverlayWnd" : L"ConsumerWnd";
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    RegisterClassExW(&wc);

    if (g_overlayMode) {
        // ---- overlay 窗口 ----
        // WS_POPUP 无边框；WS_EX_TRANSPARENT 让鼠标穿透到下层游戏；
        // WS_EX_NOACTIVATE + WM_MOUSEACTIVATE(MA_NOACTIVATE) 保证永不抢焦点；
        // WS_EX_TOOLWINDOW 不进 Alt+Tab；WS_EX_LAYERED + LWA_ALPHA 开启分层透明。
        g_hwnd = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            wc.lpszClassName, L"YOLO Overlay",
            WS_POPUP,
            0, 0, (int)g_shared->width, (int)g_shared->height,
            nullptr, nullptr, wc.hInstance, nullptr);
        if (!g_hwnd) {
            Log("[Consumer] overlay 窗口创建失败, error=%lu\n", GetLastError());
            return 1;
        }
        // 全局 alpha=255：不额外调暗，颜色就是 Producer 画的值
        if (!SetLayeredWindowAttributes(g_hwnd, RGB(0, 0, 0), 255, LWA_ALPHA)) {
            Log("[Consumer] SetLayeredWindowAttributes 失败, error=%lu\n", GetLastError());
        }
        // SW_SHOWNOACTIVATE：显示但不给焦点
        ShowWindow(g_hwnd, SW_SHOWNOACTIVATE);
        SetWindowPos(g_hwnd, HWND_TOPMOST, 0, 0,
                     (int)g_shared->width, (int)g_shared->height,
                     SWP_NOACTIVATE | SWP_SHOWWINDOW);
        Log("[Consumer] overlay 窗口已创建（%ux%u 全屏置顶+穿透）\n",
            g_shared->width, g_shared->height);
    } else {
        // ---- 预览窗口 ----
        int winW = (int)g_shared->width / 2;
        int winH = (int)g_shared->height / 2;

        g_hwnd = CreateWindowExW(0, wc.lpszClassName, L"Consumer - YOLO Overlay",
            WS_OVERLAPPEDWINDOW | WS_VISIBLE, 100, 100, winW, winH,
            nullptr, nullptr, wc.hInstance, nullptr);
        if (!g_hwnd) {
            Log("[Consumer] CreateWindow failed, error=%lu\n", GetLastError());
            return 1;
        }
        Log("[Consumer] 预览窗口已创建\n");
    }

    if (!InitD3D(g_hwnd, g_shared->width, g_shared->height)) {
        Log("[Consumer] D3D 初始化失败\n");
        return 1;
    }
    if (!AttachTextures()) {
        Log("[Consumer] 附着共享纹理失败\n");
        return 1;
    }

    // ---- 等首帧 ---------------------------------------------------
    // 真正的耗时在 Producer 侧（ONNX + DirectML 首次要编译 shader），
    // 所以这里必须等到 READY 再开始计活性，超时也绝不能当成"Producer 挂了"。
    if (!WaitForStage(STAGE_READY, READY_WAIT_MS, "Producer 首帧")) {
        DetachTextures();
        return 1;
    }

    Log("[Consumer] ===== READY (开始接收帧) =====\n");

    // ---- 主循环 ---------------------------------------------------
    MSG msg = {};
    LONG   lastCounter    = SmGetFrameCounter(g_shared);
    LONG   lastPid        = SmGetPid(g_shared);
    DWORD  lastProgress   = GetTickCount();
    int    lastFrame      = -1;

    while (msg.message != WM_QUIT && g_running) {
        while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }
        if (msg.message == WM_QUIT || !g_running) break;

        LONG stage = SmGetStage(g_shared);
        LONG pid   = SmGetPid(g_shared);

        // (1) Producer 主动退出 —— 立刻关窗，不留最后一帧假装还在工作
        if (stage == STAGE_EXITED) {
            Log("[Consumer] Producer 已正常退出，关闭预览窗口\n");
            break;
        }

        // (2) Producer 重启：stage 被打回 BOOTING 且 pid 变了。
        //     共享内存 section 因为本进程还持有句柄而存活，所以地址不变、
        //     只是内容被新 Producer 重新填过 —— 必须重新附着新句柄。
        if (stage <= STAGE_BOOTING && pid != lastPid) {
            Log("[Consumer] 检测到 Producer 重启 (pid %ld -> %ld)，重新附着\n",
                lastPid, pid);
            DetachTextures();
            lastFrame = -1;
            lastPid = pid;

            if (!WaitForStage(STAGE_TEXTURES, TEX_WAIT_MS, "重启后的纹理") ||
                !AttachTextures() ||
                !WaitForStage(STAGE_READY, READY_WAIT_MS, "重启后的首帧")) {
                Log("[Consumer] 重连失败，退出\n");
                break;
            }
            lastCounter  = SmGetFrameCounter(g_shared);
            lastProgress = GetTickCount();
            lastPid      = SmGetPid(g_shared);
            Log("[Consumer] 已重新附着，继续接收帧\n");
            continue;
        }

        // (3) 活性：用真正在推进的 frameCounter，而不是粘滞的 producerAlive。
        //     旧代码用 producerAlive（只置 1 从不清 0）判断存活，那个
        //     "Producer lost" 分支永远不可能执行，Producer 崩了也看不出来。
        //     这段判活从"等到 READY 之后"才开始计时，不会误杀加载中的 Producer。
        LONG counter = SmGetFrameCounter(g_shared);
        if (counter != lastCounter) {
            lastCounter = counter;
            lastProgress = GetTickCount();
        }
        else if (GetTickCount() - lastProgress > STALL_MS) {
            Log("[Consumer] Producer 帧号停滞 %lu 帧超过 %lu ms，判定卡死并退出\n",
                counter - lastCounter, STALL_MS);
            break;
        }

        // (4) 有新帧就显示
        int idx = (int)InterlockedCompareExchange(&g_shared->frameIndex, 0, 0);
        if (idx != lastFrame) {
            lastFrame = idx;
            BlitSharedFrame(idx);
            if (g_needReattach) {
                g_needReattach = false;
                Log("[Consumer] 重新附着共享纹理\n");
                DetachTextures();
                if (!AttachTextures()) { Log("[Consumer] 重附着失败\n"); break; }
                lastFrame = -1;
            }
        }

        Sleep(1);
    }

    Log("[Consumer] 收尾：共处理 %ld 帧\n", SmGetFrameCounter(g_shared) - lastCounter);

    DetachTextures();
    if (g_backBuf) { g_backBuf->Release();  g_backBuf = nullptr; }
    if (g_rtv) { g_rtv->Release(); g_rtv = nullptr; }
    if (g_swapChain) { g_swapChain->Release(); g_swapChain = nullptr; }
    if (g_context) { g_context->Release();   g_context = nullptr; }
    if (g_device) { g_device->Release();    g_device = nullptr; }
    if (g_shared) { UnmapViewOfFile(g_shared); g_shared = nullptr; }
    if (g_hMapFile) { CloseHandle(g_hMapFile); g_hMapFile = nullptr; }
    if (g_logFile) { fclose(g_logFile); g_logFile = nullptr; }

    Log("[Consumer] Exit\n");
    return 0;
}
