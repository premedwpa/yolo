"""LLM 客户端: 本地 llama.cpp / 外部 OpenAI 兼容 API 都能走。

两种模式的差异不只是地址:
  - 本地 llama.cpp 认chat_template_kwargs(关思考模式), 外部 API 不认,
    带上会 400。
  - 外部 API 基本都要 Authorization: Bearer。
所以分支要显式做, 不能只换 URL。
"""
import requests

import config
import modelcfg

_session = requests.Session()


def _base():
    if modelcfg.is_external():
        cfg = modelcfg.load()
        return cfg["api_base"].rstrip("/")
    return config.LLM_BASE.rstrip("/")


def _model():
    if modelcfg.is_external():
        return modelcfg.load()["api_model"].strip()
    return config.LLM_MODEL


def _headers():
    h = {"Content-Type": "application/json"}
    key = modelcfg.api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _timeout():
    if modelcfg.is_external():
        return config.API_TIMEOUT
    return config.LLM_TIMEOUT


def build_system(system=None, style_note=None):
    """拼 system prompt。

    style_note 是从对方真实消息里提取的说话习惯(见 style.analyze),
    追加在 persona 之后 —— 它比通用规则更贴合具体这个人。
    """
    if system:
        base = system
    else:
        import persona as persona_mod
        base = persona_mod.build_extra(config.SYSTEM_PROMPT)

    if style_note:
        base = base + "\n" + style_note
    return base


def chat(user_text, history=None, system=None, style_note=None):
    """发一条消息, 返回回复纯文本。失败抛异常。"""
    msgs = [{"role": "system", "content": build_system(system, style_note)}]
    for h in (history or [])[-8:]:
        msgs.append(h)
    msgs.append({"role": "user", "content": user_text})

    body = {
        "model": _model(),
        "messages": msgs,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    if not modelcfg.is_external():
        # Qwen/llama.cpp 专属: 关思考模式回复更快更像闲聊。
        # 外部接口不认这个字段, 带上会 400, 所以只在本地模式加。
        body["chat_template_kwargs"] = {"enable_thinking": False}

    url = _base() + "/chat/completions"
    last_err = None
    for _ in range(config.LLM_RETRIES + 1):
        try:
            r = _session.post(url, json=body, headers=_headers(),
                              timeout=_timeout())
            r.raise_for_status()
            return _extract(r.json())
        except Exception as e:
            last_err = e
    raise RuntimeError(f"LLM 调用失败 ({_base()}): {last_err}")


def _extract(data):
    """兼容几种返回结构, 免得换服务就炸。"""
    choices = data.get("choices")
    if not choices:
        raise RuntimeError(f"返回里没有 choices: {str(data)[:200]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if content is None:
        # 有些网关把内容放在 reasoning_content 或 text
        content = msg.get("reasoning_content") or choices[0].get("text")
    if content is None:
        raise RuntimeError(f"返回里没有消息内容: {str(data)[:200]}")
    return str(content).strip()


def check_alive():
    """检查服务是否可用。返回 (是否可用, 详细信息)。"""
    mode = "外部 API" if modelcfg.is_external() else "本地"
    try:
        r = _session.get(_base() + "/models", headers=_headers(), timeout=8)
        ok = r.status_code < 400
        detail = f"{mode} | {_base()} | HTTP {r.status_code}"
        if ok:
            try:
                ids = [m.get("id") for m in r.json().get("data", [])][:6]
                detail += f" | 可用模型: {', '.join(str(i) for i in ids)}"
            except Exception:
                pass
        else:
            detail += f" | {r.text[:120]}"
        return ok, detail
    except Exception as e:
        return False, f"{mode} | {_base()} | 不可达: {type(e).__name__}: {e}"


def strip_markdown(text):
    """把模型可能的 markdown/emoji 残留压成口语一句话。"""
    out = (text or "").strip()
    for ch in ("*", "#", "`", ">", "_", "~"):
        out = out.replace(ch, "")
    lines = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln[0] in "📌💡🎯✅⚡📎🔍" or ln.startswith(("示例", "示例输出")):
            continue
        lines.append(ln)
    out = " ".join(lines).strip()
    # 微信口语消息句末一般不带标点, 模型偶尔还是会加, 去掉更像真人打字
    out = out.rstrip("。.！!？?~～ ")
    if len(out) > config.MAX_REPLY_CHARS:
        out = out[:config.MAX_REPLY_CHARS - 3] + "..."
    return out