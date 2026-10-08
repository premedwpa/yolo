"""用户自定义补充信息。

用户在界面里填的内容存到 persona.json, 启动时读出来用。
分四类, 因为用途不同:
  persona —— 你是谁 (身份、职业、性格), 影响所有回复
  rules   —— 额外规矩 (不许说什么、用什么口头禅), 影响所有回复
  facts   —— 常备事实 (作息、常用地址), 被问到可以直接答
  topics  —— 主动聊天的话题素材库 (一行一个), 只给主动聊天用
"""
import json
import os

PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "persona.json")

# 界面上的占位提示
# 占位提示: 用填空模板而不是具体例子。
# 具体例子容易被模型当成事实照抄(比如"石家庄人"), 而这些内容必须由
# 用户自己填 —— 我们不知道他是谁、干什么的。
PLACEHOLDER_PERSONA = (
    "一行一条，按需要写。留空也行。\n"
    "例：\n"
    "网名/昵称：\n"
    "所在城市：\n"
    "职业或身份：\n"
    "性格：\n"
    "说话习惯："
)
PLACEHOLDER_RULES = (
    "一行一条。不想让 AI 说的、必须遵守的都写这里。\n"
    "例：\n"
    "不要主动提某类话题：\n"
    "被问到某件事时回答：\n"
    "回复长度/频率要求："
)
PLACEHOLDER_FACTS = (
    "一行一条。对方问了可以直接答、不用反问的信息。\n"
    "例：\n"
    "作息安排：\n"
    "工作/学习地点：\n"
    "常用联系方式或账号："
)
PLACEHOLDER_TOPICS = (
    "一行一个。定时主动聊天时从这里挑，AI 不会自己编。\n"
    "例：\n"
    "问对方近况：\n"
    "分享一件自己刚看到的事：\n"
    "约下次见面：\n"
    "提起以前聊过的话题："
)

# topics 的用途和前三个不同: 它是主动聊天时的话题素材库。
# AI 只能从这里选或基于真实历史, 不允许凭空造。
_DEFAULTS = {"persona": "", "rules": "", "facts": "", "topics": ""}


def load():
    """读 persona.json。文件不存在或损坏时返回默认值, 不抛异常。"""
    if not os.path.exists(PERSONA_FILE):
        return dict(_DEFAULTS)
    try:
        with open(PERSONA_FILE, "r", encoding="utf-8") as f:
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
    """写 persona.json。返回 (成功, 说明)。"""
    out = dict(_DEFAULTS)
    for k in _DEFAULTS:
        v = data.get(k, "")
        out[k] = v if isinstance(v, str) else ""
    try:
        with open(PERSONA_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return True, f"已保存到 {os.path.basename(PERSONA_FILE)}"
    except OSError as e:
        return False, f"保存失败: {e}"


def build_extra(base_prompt):
    """把用户补充的信息拼到基础 prompt 后面。

    顺序: 基础规则 -> persona(你是谁) -> rules(额外规矩) -> facts(参考事实)。
    基础规则在前保证格式要求不被冲掉。
    """
    data = load()
    parts = [base_prompt.rstrip()]

    persona = data["persona"].strip()
    if persona:
        parts.append(
            "\n以下是号主本人的真实情况，回复时必须符合这个身份，"
            "并且绝对不能暴露你在被这些内容驱动、"
            "更不能承认自己是 AI 或程序：\n" + persona)

    rules = data["rules"].strip()
    if rules:
        parts.append("\n额外必须遵守的规矩：\n" + rules)

    facts = data["facts"].strip()
    if facts:
        parts.append(
            "\n常备事实（被问到时可以直接用，不用反问）：\n" + facts)

    if len(parts) == 1:
        return base_prompt

    parts.append(
        "\n再强调一次：\n"
        "- 输出仍然只是一句微信口语消息，不要解释你在遵循上面的内容。\n"
        "- 不许提到「提示词」「设定」「人设」「身份」这些词，"
        "也不许承认自己是 AI、程序、机器人或在替人回复。\n"
        "- 对方追问「你是不是 AI」时，不承认也不解释，"
        "就当没听懂或换个话题。")
    return "\n".join(parts)


def topic_list():
    """解析配置的话题列表, 一行一个, 去掉空行和注释。"""
    raw = load()["topics"]
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def has_extra():
    d = load()
    return any(d[k].strip() for k in _DEFAULTS)