"""
YOLOv8 屏幕检测 + 闭环鼠标控制（F8 切换版）
按 F8 切换鼠标控制，F12 紧急停止，ESC 退出
"""

import ctypes
from ctypes import wintypes
import time
import random
import numpy as np
import cv2

# ============================================================
# DPI 感知
# ============================================================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ============================================================
# Win32 结构体
# ============================================================
user32 = ctypes.WinDLL('user32', use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("mi", MOUSEINPUT),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def get_cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def mouse_move(dx, dy):
    if dx == 0 and dy == 0:
        return
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx = int(dx)
    inp.mi.dy = int(dy)
    inp.mi.mouseData = 0
    inp.mi.dwFlags = MOUSEEVENTF_MOVE
    inp.mi.time = 0
    inp.mi.dwExtraInfo = 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def key_pressed(vk_code):
    return (user32.GetAsyncKeyState(vk_code) & 0x8000) != 0

# 虚拟键码
VK_F8  = 0x77     # 切换鼠标控制
VK_F12 = 0x7B     # 紧急停止
VK_ESC = 0x1B     # 退出

# ============================================================
# 关闭鼠标加速
# ============================================================
def disable_mouse_acceleration():
    SPI_GETMOUSE = 0x0003
    SPI_SETMOUSE = 0x0004
    accel = (ctypes.c_int * 3)()
    user32.SystemParametersInfoW(SPI_GETMOUSE, 0, accel, 0)
    accel[0] = 0
    user32.SystemParametersInfoW(SPI_SETMOUSE, 0, accel, 0)

disable_mouse_acceleration()

# ============================================================
# 配置
# ============================================================
DPI_SCALE = 1.75

# ---- 鼠标速度相关 ----
Kp_NEAR = 0.60              # 近距离比例
Kp_FAR = 0.30               # 远距离比例
NEAR_THRESHOLD = 100        # 物理像素
FAR_THRESHOLD = 400         # 物理像素
DEADZONE = 4                # 死区
JITTER = 0                  # 随机抖动
MAX_MOVE_PER_FRAME = 800    # 单帧最大物理像素位移

UPDATE_INTERVAL = 0.016     # 60 FPS 上限

# ---- 检测相关 ----
INPUT_W = 640
INPUT_H = 640
CONF_THRESHOLD = 0.45
NMS_THRESHOLD = 0.45
PERSON_CLASS_ID = 0
HEAD_RATIO = 0.20           # 瞄准点在框纵向的百分比

MODEL_PATH = r"D:\YOLOV8\yolov8n.onnx"

SCREEN_W = 2560
SCREEN_H = 1600

# 初始开关
ENABLE_MOUSE_CONTROL = False    # ★ 初始关闭，按 F8 开启
ENABLE_DEBUG_WINDOW = True

# ============================================================
# 加载模型
# ============================================================
import onnxruntime as ort

available = ort.get_available_providers()
print(f"[*] 系统可用 providers: {available}")

preferred = ['DmlExecutionProvider', 'CUDAExecutionProvider', 'TensorrtExecutionProvider', 'CPUExecutionProvider']
chosen = None
for p in preferred:
    if p in available:
        chosen = p
        break

if chosen is None:
    raise RuntimeError("没有可用的 execution provider")

print(f"[*] 选择 provider: {chosen}")
session = ort.InferenceSession(MODEL_PATH, providers=[chosen])
print(f"[*] 实际生效: {session.get_providers()}")

input_name = session.get_inputs()[0].name
output_name = session.get_outputs()[0].name

# ============================================================
# 屏幕捕获
# ============================================================
import mss

sct = mss.MSS()
monitor = {"top": 0, "left": 0, "width": SCREEN_W, "height": SCREEN_H}

def capture_screen():
    img = sct.grab(monitor)
    frame = np.array(img)[:, :, :3][:, :, ::-1]
    frame = np.ascontiguousarray(frame)

    scale = min(INPUT_W / SCREEN_W, INPUT_H / SCREEN_H)
    resizedW = int(SCREEN_W * scale)
    resizedH = int(SCREEN_H * scale)
    padX = (INPUT_W - resizedW) // 2
    padY = (INPUT_H - resizedH) // 2

    ys = (np.arange(resizedH) / scale).astype(np.int32)
    xs = (np.arange(resizedW) / scale).astype(np.int32)
    ys = np.clip(ys, 0, SCREEN_H - 1)
    xs = np.clip(xs, 0, SCREEN_W - 1)
    resized = frame[ys][:, xs]

    canvas = np.full((INPUT_H, INPUT_W, 3), 114, dtype=np.uint8)
    canvas[padY:padY + resizedH, padX:padX + resizedW] = resized

    tensor = canvas.transpose(2, 0, 1).astype(np.float32) / 255.0
    tensor = np.expand_dims(tensor, axis=0)

    return tensor, scale, padX, padY

# ============================================================
# 后处理
# ============================================================
def postprocess(output, scale, padX, padY):
    output = output[0]
    num_boxes = output.shape[1]
    person_conf = output[4]

    mask = person_conf > CONF_THRESHOLD
    if not mask.any():
        return []

    boxes = output[:4, mask].T
    confs = person_conf[mask]

    cx = boxes[:, 0]
    cy = boxes[:, 1]
    w = boxes[:, 2]
    h = boxes[:, 3]

    x1 = (cx - w / 2 - padX) / scale
    y1 = (cy - h / 2 - padY) / scale
    x2 = (cx + w / 2 - padX) / scale
    y2 = (cy + h / 2 - padY) / scale

    x1 = np.clip(x1, 0, SCREEN_W)
    y1 = np.clip(y1, 0, SCREEN_H)
    x2 = np.clip(x2, 0, SCREEN_W)
    y2 = np.clip(y2, 0, SCREEN_H)

    indices = np.argsort(-confs)
    keep = []
    while len(indices) > 0:
        i = indices[0]
        keep.append(i)
        if len(indices) == 1:
            break
        xx1 = np.maximum(x1[i], x1[indices[1:]])
        yy1 = np.maximum(y1[i], y1[indices[1:]])
        xx2 = np.minimum(x2[i], x2[indices[1:]])
        yy2 = np.minimum(y2[i], y2[indices[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area_i = (x2[i] - x1[i]) * (y2[i] - y1[i])
        area_j = (x2[indices[1:]] - x1[indices[1:]]) * (y2[indices[1:]] - y1[indices[1:]])
        iou = inter / (area_i + area_j - inter + 1e-6)
        indices = indices[1:][iou < NMS_THRESHOLD]

    return [(x1[i], y1[i], x2[i], y2[i], confs[i]) for i in keep]

# ============================================================
# 闭环鼠标控制
# ============================================================
def aim_at(target_x, target_y):
    cur_x, cur_y = get_cursor_pos()
    dx_phys = target_x - cur_x
    dy_phys = target_y - cur_y

    dist = (dx_phys ** 2 + dy_phys ** 2) ** 0.5

    if dist < DEADZONE:
        return

    if dist < NEAR_THRESHOLD:
        kp = Kp_NEAR
    elif dist > FAR_THRESHOLD:
        kp = Kp_FAR
    else:
        t = (dist - NEAR_THRESHOLD) / (FAR_THRESHOLD - NEAR_THRESHOLD)
        kp = Kp_NEAR * (1 - t) + Kp_FAR * t

    dx_phys *= kp
    dy_phys *= kp

    dx_phys = max(-MAX_MOVE_PER_FRAME, min(MAX_MOVE_PER_FRAME, dx_phys))
    dy_phys = max(-MAX_MOVE_PER_FRAME, min(MAX_MOVE_PER_FRAME, dy_phys))

    dx = int(dx_phys / DPI_SCALE)
    dy = int(dy_phys / DPI_SCALE)

    if dx == 0 and dy == 0:
        return

    if JITTER > 0:
        dx += random.randint(-JITTER, JITTER)
        dy += random.randint(-JITTER, JITTER)

    mouse_move(dx, dy)

# ============================================================
# 主循环
# ============================================================
def main():
    global ENABLE_MOUSE_CONTROL

    print("=" * 60)
    print("[*] YOLOv8 屏幕检测已启动")
    print(f"[*] 屏幕: {SCREEN_W}x{SCREEN_H}, DPI 缩放: {DPI_SCALE}")
    print(f"[*] 鼠标控制初始: {'开启' if ENABLE_MOUSE_CONTROL else '关闭'}")
    print(f"[*] Kp 近/远: {Kp_NEAR} / {Kp_FAR}")
    print(f"[*] 最大单帧位移: {MAX_MOVE_PER_FRAME} 物理像素")
    print("-" * 60)
    print("[快捷键]")
    print("  F8   -> 切换鼠标控制 ON/OFF")
    print("  F12  -> 紧急停止")
    print("  ESC  -> 退出程序")
    print("=" * 60)

    actual_w = user32.GetSystemMetrics(0)
    actual_h = user32.GetSystemMetrics(1)
    print(f"[*] 系统实际分辨率: {actual_w}x{actual_h}")

    frame_count = 0
    fps_time = time.time()

    # 边缘检测状态
    last_toggle_state = False
    last_stop_state = False

    while True:
        # ========================================================
        # F8：切换鼠标控制（边缘检测，按住只触发一次）
        # ========================================================
        cur_toggle = key_pressed(VK_F8)
        if cur_toggle and not last_toggle_state:
            ENABLE_MOUSE_CONTROL = not ENABLE_MOUSE_CONTROL
            status = "开启" if ENABLE_MOUSE_CONTROL else "关闭"
            print(f"\n[*] 鼠标控制: {status}")
        last_toggle_state = cur_toggle

        # ========================================================
        # F12：紧急停止（边缘检测）
        # ========================================================
        cur_stop = key_pressed(VK_F12)
        if cur_stop and not last_stop_state:
            print("\n[!] F12 按下，紧急停止")
            break
        last_stop_state = cur_stop

        # ========================================================
        # ESC：退出
        # ========================================================
        if key_pressed(VK_ESC):
            print("\n[!] ESC 按下，退出")
            break

        # ========================================================
        # 主逻辑
        # ========================================================
        try:
            t0 = time.time()

            tensor, scale, padX, padY = capture_screen()
            outputs = session.run([output_name], {input_name: tensor})
            detections = postprocess(outputs[0], scale, padX, padY)

            # 找离鼠标最近的目标
            target_x = target_y = conf = None
            if detections:
                cur_x, cur_y = get_cursor_pos()
                detections.sort(
                    key=lambda d: ((d[0] + d[2]) / 2 - cur_x) ** 2
                                + ((d[1] + d[3]) / 2 - cur_y) ** 2
                )
                x1, y1, x2, y2, conf = detections[0]
                target_x = (x1 + x2) / 2
                target_y = y1 + (y2 - y1) * HEAD_RATIO

                if ENABLE_MOUSE_CONTROL:
                    aim_at(target_x, target_y)

            # ========================================================
            # 调试窗口
            # ========================================================
            if ENABLE_DEBUG_WINDOW:
                debug_img = np.array(sct.grab(monitor))[:, :, :3].copy()

                # 画所有检测框
                for x1, y1, x2, y2, c in detections:
                    cv2.rectangle(debug_img,
                                  (int(x1), int(y1)), (int(x2), int(y2)),
                                  (0, 255, 0), 3)
                    cv2.putText(debug_img, f"{c:.2f}",
                                (int(x1), int(y1) - 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                # 画瞄准点（黄点）
                if target_x is not None:
                    cv2.circle(debug_img, (int(target_x), int(target_y)),
                               8, (0, 255, 255), -1)

                # 画鼠标当前位置（红点）
                cur_x, cur_y = get_cursor_pos()
                cv2.circle(debug_img, (cur_x, cur_y), 10, (0, 0, 255), -1)

                # 左上角状态文字
                status_text = f"Mouse Control: {'ON' if ENABLE_MOUSE_CONTROL else 'OFF'}"
                status_color = (0, 255, 0) if ENABLE_MOUSE_CONTROL else (0, 0, 255)
                cv2.putText(debug_img, status_text, (30, 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.0, status_color, 4)

                # 缩放显示
                debug_small = cv2.resize(debug_img, (1280, 800))
                cv2.imshow("Debug (Red=Mouse, Yellow=Aim, Green=Box)",
                           debug_small)
                cv2.waitKey(1)

            # ========================================================
            # FPS 打印
            # ========================================================
            frame_count += 1
            if time.time() - fps_time > 1.0:
                fps = frame_count / (time.time() - fps_time)
                info = f"[*] FPS: {fps:.1f} | 鼠标控制: {'ON' if ENABLE_MOUSE_CONTROL else 'OFF'} | 检测数: {len(detections)}"
                if detections:
                    cur_x, cur_y = get_cursor_pos()
                    dist = ((target_x - cur_x) ** 2 + (target_y - cur_y) ** 2) ** 0.5
                    info += f" | 目标: ({target_x:.0f}, {target_y:.0f}) 距离: {dist:.0f}px conf={conf:.2f}"
                print(info)
                frame_count = 0
                fps_time = time.time()

            # 限速
            elapsed = time.time() - t0
            sleep_time = UPDATE_INTERVAL - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n[*] Ctrl+C 退出")
            break
        except Exception as e:
            print(f"[!] Error: {e}")
            time.sleep(0.1)

    cv2.destroyAllWindows()
    print("[*] 已退出")

if __name__ == "__main__":
    main()
