"""安全兜底: 关键场景转人工 + 每日上限 + 限速。"""
import datetime
import re
import threading

import config

_lock = threading.Lock()
_state = {"date": "", "sent": 0, "errors": 0, "last_send": 0.0}


def _roll_date():
    today = datetime.date.today().isoformat()
    if _state["date"] != today:
        _state["date"] = today
        _state["sent"] = 0
    return today


def needs_human(text, reply=None):
    """判断是否需要人工确认。

    返回 (是否转人工, 原因)。
    """
    if not text:
        return True, "空消息"
    blob = text + " " + (reply or "")
    for kw in config.SENSITIVE_KEYWORDS:
        if kw in blob:
            return True, f"命中关键词: {kw}"
    # 金额/数字组合, 如 "转你500" "8号给你2000"
    if re.search(r"\d+\s*(元|块|万|k|K|w)", reply or ""):
        return True, "回复涉及金额"
    return False, ""


def can_send():
    """是否可以发送。返回 (允许, 原因)。"""
    _roll_date()
    if _state["sent"] >= config.DAILY_SEND_LIMIT:
        return False, f"已达今日上限 {config.DAILY_SEND_LIMIT} 条"
    if _state["errors"] >= config.MAX_CONSECUTIVE_ERRORS:
        return False, f"连续错误 {_state['errors']} 次, 已自动停机"
    return True, ""


def rate_limit_ok():
    """限速: 距上次发送是否够久。返回需等待秒数, 0 表示可以发。"""
    import time
    wait = config.MIN_SEND_INTERVAL - (time.time() - _state["last_send"])
    return max(0, wait)


def mark_sent():
    with _lock:
        _roll_date()
        import time
        _state["sent"] += 1
        _state["last_send"] = time.time()
        return _state["sent"]


def mark_error():
    with _lock:
        _state["errors"] += 1
        return _state["errors"]


def reset_errors():
    with _lock:
        _state["errors"] = 0


def today_sent():
    _roll_date()
    return _state["sent"]