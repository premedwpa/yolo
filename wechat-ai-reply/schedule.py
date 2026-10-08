"""定时主动聊天: 到点自己找一个人开口。

和「主动聊天(轮转)」的区别:
  主动聊天(轮转) 固定间隔扫白名单, 谁没回过就找谁, 内容靠历史话头。
  定时任务       你指定时刻和内容, 到点就照着说。

时间自己填(HH:MM), 内容自己填(一句话说清要聊什么)。内容会当成
「话头」喂给模型, 模型用你的口气把它说出来, 不会照抄。
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

import config

FILE = os.path.join(config.BASE_DIR, "schedules.json")
# 必须用 RLock: add/remove/toggle 持锁后还会调 save(), 而 Lock 不可重入,
# 用普通 Lock 会自己把自己锁死。
_LOCK = threading.RLock()

# 到点后多久还算数。比如 09:30 的任务, 你 09:50 才开机, 补发一次;
# 过了就跳过, 不然半夜开机会把早上的话一起发出去。
GRACE_MINUTES = 20


def _valid_time(s):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", (s or "").strip()))


def load():
    """返回 [任务dict]。文件不存在或坏了就当空, 绝不让它把程序搞崩。"""
    if not os.path.exists(FILE):
        return []
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        out = []
        for i, it in enumerate(data, 1):
            if not isinstance(it, dict):
                continue
            t = str(it.get("time", "")).strip()
            if not _valid_time(t):
                continue                      # 时间不合法直接丢
            out.append({
                "id": int(it.get("id") or i),
                "time": t,
                "content": str(it.get("content", "")).strip(),
                "enabled": bool(it.get("enabled", True)),
            })
        return out
    except Exception:
        return []


def save(items):
    with _LOCK:
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE)


def add(time_str, content):
    if not _valid_time(time_str):
        raise ValueError("时间格式应为 HH:MM, 例如 09:30")
    with _LOCK:
        items = load()
        nid = max([i.get("id", 0) for i in items] or [0]) + 1
        items.append({"id": nid, "time": time_str.strip(),
                      "content": (content or "").strip(),
                      "enabled": True})
        save(items)
        return nid


def remove(sid):
    with _LOCK:
        save([i for i in load() if i.get("id") != sid])


def toggle(sid, on):
    with _LOCK:
        items = load()
        for i in items:
            if i.get("id") == sid:
                i["enabled"] = bool(on)
        save(items)


def next_times(limit=6):
    """给 UI 看的「接下来几点会发」, 纯粹方便预览, 不参与判断。"""
    items = [i for i in load() if i.get("enabled") and _valid_time(i.get("time"))]
    if not items:
        return []
    now = datetime.now()
    out = []
    for i in sorted(items, key=lambda x: x["time"]):
        hh, mm = i["time"].split(":")
        t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        out.append((t.strftime("%m-%d %H:%M"), i.get("content") or "(无内容)"))
    return out[:limit]


def due(now=None, fired=None):
    """返回 [(任务, 内容), ...]: 到点该发、且今天还没发过的。

    fired 是 {任务id: "YYYY-MM-DD"} 的已发记录, 由调用方持有, 用来防止
    同一个任务在同一分钟内被重复触发(轮询是 3 秒一次)。
    """
    now = now or datetime.now()
    fired = fired if fired is not None else {}
    today = now.strftime("%Y-%m-%d")
    out = []
    for it in load():
        if not it.get("enabled"):
            continue
        t = it.get("time", "")
        if not _valid_time(t):
            continue
        hh, mm = t.split(":")
        target = now.replace(hour=int(hh), minute=int(mm),
                             second=0, microsecond=0)
        delta = (now - target).total_seconds() / 60.0
        if 0 <= delta <= GRACE_MINUTES and fired.get(it["id"]) != today:
            out.append((it, it.get("content", "")))
    return out


def mark_fired(sid, date=None):
    d = date or datetime.now().strftime("%Y-%m-%d")
    return {int(sid): d}