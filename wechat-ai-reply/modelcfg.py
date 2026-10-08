"""模型接入配置。

支持两种:
  local —— 本地 llama.cpp / Ollama 等 OpenAI 兼容端点(默认)
  api   —— 外部 API(OpenAI 兼容, 填 base_url + model + key 即可)

API key 优先读环境变量 WECHAT_LLM_API_KEY, 其次读本地 model.json。
不把 key 写进 config.py, 免得误提交。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE = os.path.join(BASE_DIR, "model.json")
ENV_KEY = "WECHAT_LLM_API_KEY"

PLACEHOLDER = (
    "local = 本机 llama.cpp / Ollama, 填 127.0.0.1:端口 就行\n"
    "api   = 外部服务, 任意 OpenAI 兼容接口"
)

_DEFAULTS = {
    "provider": "local",
    "api_base": "http://127.0.0.1:8081/v1",
    "api_model": "",
    "api_key": "",
}


def load():
    if not os.path.exists(MODEL_FILE):
        return dict(_DEFAULTS)
    try:
        with open(MODEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = dict(_DEFAULTS)
        if isinstance(data, dict):
            for k in _DEFAULTS:
                v = data.get(k, "")
                out[k] = v if isinstance(v, str) else ""
        return out
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)


def save(data):
    out = dict(_DEFAULTS)
    for k in _DEFAULTS:
        v = data.get(k, "")
        out[k] = v.strip() if isinstance(v, str) else ""
    try:
        with open(MODEL_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return True, f"已保存到 {os.path.basename(MODEL_FILE)}"
    except OSError as e:
        return False, f"保存失败: {e}"


def api_key():
    """环境变量优先, 避免把 key 落在磁盘上。"""
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        return env
    return load()["api_key"].strip()


def is_external():
    return load()["provider"] == "api"