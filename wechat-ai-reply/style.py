"""从对方真实消息里提取说话风格, 让回复更像真人。

设计要点(都是踩过的坑):
1. 只描述**风格**, 不引用对方原话。引用了模型会连内容一起抄, 变成复读机。
2. 样本不足就不学。3 条以内没��, 否则会把偶然当习惯。
3. 有上限。只看最近若干条, 且限制注入的描述长度, 避免占满提示词。
4. 分档而不是给精确数值 —— 「很短」比「平均 6.2 字」更好理解也更稳。
"""
import re

# 语气词表, 命中就说明对方习惯用这些。
# 长短语放在前面(如 哈哈 先于 哈), 后面会被前缀过滤掉, 避免
# 同时列出「哈」和「哈哈」这种重复。
_MODES = ["哈哈", "嗯嗯", "哇", "哎", "诶", "嘿", "喔", "噢", "唔",
          "哈", "啊", "嗯", "呀", "嘛", "呗", "嘞", "哦"]

# 表情/颜文字
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF☀-➿️⬀-⯿]")

# 重复字, 如「你你」「我我」「不不不」
_REPEAT = re.compile(r"([一-鿿])\1{1,}")


def pick_topics(hist, limit=5, min_len=2):
    """从真实聊天记录里挑出「可以主动开口聊」的候选话题。

    hist: [(sender, text, is_self)] 时间倒序。

    核心思路: **对方发过但我没回的消息**是最自然的开场素材 ——
    那个话头还悬着, 接上去最自然, 而且是真实发生过的, 不是编的。
    其次是对方最近说的、有点内容的话。
    最后才是对方自己提过但我没参与的方向。

    返回 [(话题文本, 优先级)] 优先级 1 最高。不引用自己发的话。
    """
    peers = [(t.strip(), me) for _s, t, me in hist if (t or "").strip()]
    peer_only = [t for t, me in peers if not me]

    if len(peer_only) < 2:
        return []

    cands = []

    # 1) 找「对方说完、我没接」的话头。
    #    hist 是倒序(索引 0 最新)。对第 i 条的对方消息来说,
    #    它之后一条是 i-1; 如果那条是我发的, 就说明我已经回过了。
    for i, (t, me) in enumerate(peers):
        if me:
            continue
        answered = (i > 0 and peers[i - 1][1])
        if not answered and len(t) >= min_len:
            cands.append((1, t))

    # 2) 对方最近说的其他内容
    for t in peer_only[:6]:
        cands.append((2, t))

    # 去重(去掉被更高优先级包含的), 按优先级+位置排序, 取前 limit
    seen = set()
    out = []
    for pri, t in sorted(cands, key=lambda x: x[0]):
        key = t[:12]
        if key in seen:
            continue
        # 太短没内容
        if len(t) < min_len:
            continue
        seen.add(key)
        out.append((t, pri))
        if len(out) >= limit:
            break
    return out


def analyze(hist, limit=20, max_chars=320):
    """hist: [(sender, text, is_self)] 时间倒序(库里就是)。

    返回风格描述字符串, 或者 None 表示样本不够、别学。
    """
    peers = []
    for _s, t, me in hist[:limit]:
        if me:
            continue
        t = (t or "").strip()
        if t:
            peers.append(t)
    if len(peers) < 3:
        return None

    n = len(peers)
    lens = [len(p) for p in peers]
    avg = sum(lens) / n

    lines = []

    # 长度档位
    if avg <= 8:
        lines.append(f"对方每条平均 {avg:.0f} 字, 属于极短, 你也要跟着短")
    elif avg <= 18:
        lines.append(f"对方每条平均 {avg:.0f} 字, 偏简短, 你也别长篇大论")
    elif avg <= 40:
        lines.append(f"对方每条平均 {avg:.0f} 字, 中等长度")
    else:
        lines.append(f"对方爱说长句, 平均 {avg:.0f} 字, 可以稍微多说点")

    # 最长的一条有多长 —— 决定上限
    longest = max(lens)

    # 标点习惯
    with_punct = sum(1 for p in peers if p.endswith(("。", "?", "？", "!", "！")))
    ratio = with_punct / n
    if ratio < 0.2:
        lines.append("对方几乎不加句末标点, 你也别加")
    elif ratio > 0.7:
        lines.append("对方习惯用标点结尾, 你可以偶尔用")

    # 问号
    q = sum(1 for p in peers if "?" in p or "？" in p)
    if q / n > 0.4:
        lines.append("对方爱反问, 你回答时可以多问一句回去")

    # 语气词。只留最长的那个代表, 避免「哈」和「哈哈」同时出现。
    found = []
    for m in _MODES:
        if not any(m in p for p in peers):
            continue
        # m 是已收录词的子集(如 哈 是 哈哈 的子集), 跳过
        if any(m in s for s in found):
            continue
        found.append(m)
    if len(found) >= 2:
        lines.append("对方常用这些语气词: " + "、".join(found[:5])
                     + ", 你可以自然地用上")
    elif len(found) == 1:
        lines.append(f"对方爱说「{found[0]}」这种语气词")

    # 重复字。跳过已被语气词覆盖的字, 否则会列成「像哈这样」很怪。
    reps = set()
    for p in peers:
        reps.update(_REPEAT.findall(p))
    reps = {c for c in reps if not any(c in m for m in found)}
    if reps:
        sample = "".join(sorted(reps)[:3])
        lines.append(f"对方喜欢用重复字加强语气(像{sample}这样), 你偶尔也用")

    # 表情
    e = sum(1 for p in peers if _EMOJI.search(p))
    if e / n < 0.15:
        lines.append("对方几乎不用表情, 你也别加")
    elif e / n > 0.5:
        lines.append("对方爱发表情, 你可以偶尔用一两个")

    # 纯文字还是夹杂链接/英文
    if any(("http" in p) for p in peers):
        lines.append("对方会发链接")

    out = "\n".join("- " + x for x in lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n- (略)"
    return (
        "以下是对方真实的说话习惯。只学说话方式, "
        "不要抄具体内容和信息, 也不要说出来你在分析对方:\n" + out
    )