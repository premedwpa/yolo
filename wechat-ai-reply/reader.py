"""微信数据库读取层 (只读)。

用 wechatauto.WeChatDB 读消息原文 (SQLCipher 解密), 不依赖 UIA,
所以微信窗口最小化也照样能读, 且拿到精确原文而非 OCR 猜测。

真实 API (已用 inspect 核实, 不是猜的):
    WeChatDB()                        -> 连接/解密
    get_sessions(limit)               -> 会话列表
    get_messages(user, limit)         -> 某会话最近消息, List[dict]
    get_new_messages(user, since_seq) -> 新消息 (轮询用)
    nickname_map() / username_by_nickname(name)
user 参数要的是 **微信号/wxid**, 不是昵称, 所以要先做昵称->wxid 映射。
"""


class MessageReader:
    def __init__(self):
        self._db = None
        self._nick_to_wxid = {}
        self._last_seq = {}
        self._self_wxid = ""

    # ---------- 连接 ----------

    def connect(self):
        try:
            from wechatauto import WeChatDB
            self._db = WeChatDB()
            # 记下自己的 wxid, 作为 sender_username 的兜底判断依据
            try:
                info = self._db.get_self_info()
                if isinstance(info, dict):
                    self._self_wxid = str(
                        info.get("wxid") or info.get("username")
                        or info.get("user_name") or "")
            except Exception:
                pass
            return True, "数据库连接成功"
        except Exception as e:
            return False, f"连接失败: {type(e).__name__}: {e}"

    def _require(self):
        if not self._db:
            raise RuntimeError("未连接数据库")

    @staticmethod
    def _d(row):
        if isinstance(row, dict):
            return row
        return getattr(row, "__dict__", {}) or {}

    # ---------- 会话 / 联系人 ----------

    def get_chats(self):
        """列出有聊天记录的会话。

        已实测: get_sessions() 返回的 username 就是 wxid, nickname 字段
        为空, 所以昵称必须从 nickname_map (3908 条) 反查。
        返回 [{name, wxid, last_msg}], name 是中文昵称。
        """
        self._require()
        nick_map = {}
        try:
            mm = self._db.nickname_map()
            if isinstance(mm, dict):
                nick_map = {str(k): str(v) for k, v in mm.items()}
        except Exception:
            pass

        try:
            rows = self._db.get_sessions(limit=300)
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            d = self._d(r)
            wxid = str(d.get("username") or d.get("user") or d.get("wxid") or "")
            if not wxid:
                continue
            nick = str(d.get("nickname") or d.get("name") or "")
            if not nick:
                nick = nick_map.get(wxid, wxid)
            last = d.get("last_msg") or d.get("content") or d.get("last_message") or ""
            self._nick_to_wxid[nick] = wxid
            out.append({"name": nick, "wxid": wxid, "last_msg": str(last)[:60]})
        return out

    def build_nickname_map(self):
        """wxid -> 中文昵称 全表, 供 UI 搜索和预览用。"""
        try:
            mm = self._db.nickname_map()
            if isinstance(mm, dict):
                self._nick_to_wxid.update({str(v): str(k) for k, v in mm.items()})
        except Exception:
            pass
        return dict(self._nick_to_wxid)

    def resolve(self, name):
        """昵称 -> wxid。找不到返回 None。"""
        if name in self._nick_to_wxid:
            return self._nick_to_wxid[name]
        try:
            wxid = self._db.username_by_nickname(name)
            if wxid:
                self._nick_to_wxid[name] = str(wxid)
                return str(wxid)
        except Exception:
            pass
        return None

    def search(self, keyword):
        """按关键字搜联系人昵称。返回 [昵称...]"""
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return []
        return [n for n in self.build_nickname_map() if keyword in n.lower()]

    # ---------- 消息 ----------

    def _seq(self, row):
        d = self._d(row)
        for k in ("sort_seq", "seq", "local_id", "id", "create_time", "timestamp"):
            v = d.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return 0

    def _text(self, row):
        d = self._d(row)
        return str(d.get("content") or d.get("msg") or d.get("text") or "").strip()

    def _sender(self, row):
        d = self._d(row)
        return str(d.get("sender") or d.get("from_user") or d.get("talker") or "")

    def _is_self(self, row, is_group=False):
        """判断这条是不是自己发的。

        实测 4.1.13 单聊和群聊的方向语义完全不同, 不能共用一套规则:

        单聊 (wxid 不带 @chatroom):
            sender_id == 1 -> 自己, == 2 -> 对方
            sender_username 为空, 只能靠 sender_id。

        群聊 (wxid 带 @chatroom):
            sender_id 是群成员序号(实测出现过 5、6), 1 并不代表自己,
            所以拿 sender_id 判断会把别人的消息当成自己发的(漏回),
            也会把自己的当成别人的(自己回自己)。
            真正的发送者写在消息内容前缀里: "wxid_xxx:\\n正文"。

        之前这里只找 is_self/from_self, 那些字段压根不存在, 于是永远返回
        False, 造成自己回自己的死循环。
        """
        d = self._d(row)

        # 明确标记(将来版本若提供, 优先用)
        for k in ("is_self", "isSelf", "from_self"):
            if k in d:
                return bool(d[k])

        # 系统消息(撤回提示等)不是任何人发的
        if str(d.get("type") or "") in ("系统消息", "sysmsg"):
            return True

        if is_group:
            # 群聊: 从内容前缀取真实发送者。
            # 实测格式是 "wxid_xxx:\n正文" —— 冒号在换行前, 必须剥掉。
            if self._self_wxid:
                prefix = str(d.get("content") or "").split("\n", 1)[0].strip()
                if prefix.endswith(":"):
                    prefix = prefix[:-1]
                if prefix.startswith("wxid_"):
                    return prefix == self._self_wxid
            # 取不到就保守当作「非自己」, 否则会漏回别人消息
            return False

        # 单聊: sender_username 等于自己 wxid 时最可靠
        su = d.get("sender_username")
        if su and self._self_wxid:
            return str(su) == str(self._self_wxid)

        sid = d.get("sender_id")
        if sid is not None:
            try:
                return int(sid) == 1
            except (TypeError, ValueError):
                pass
        return False

    def new_messages(self, names):
        """取指定昵称会话里未处理过的新消息, 但**不推进水位**。

        返回 [(nick, sender, text, is_self, seq)], 按 sort_seq 去重。

        为什么不直接推进: 之前这里是读到就推进水位, 结果只要引擎在处理时
        抛异常(A 卡住/模型挂了), 这些消息已经被标记成读过了, 下轮再也拿不到
        —— 静默丢消息。改成「读不改水位, 处理完再 commit」。
        """
        self._require()
        out = []
        for nick in names:
            wxid = self.resolve(nick)
            if not wxid:
                continue
            since = self._last_seq.get(wxid, 0)
            try:
                rows = self._db.get_new_messages(user=wxid, since_seq=since,
                                                 limit=50)
            except Exception:
                continue
            is_group = str(wxid).endswith("@chatroom")
            for r in rows or []:
                seq = self._seq(r)
                if seq and seq > since:
                    out.append((nick, self._sender(r), self._text(r),
                                self._is_self(r, is_group), seq))
        return out

    def commit(self, nick, seq):
        """确认这个会话已经处理到 seq, 才推进水位。

        只前进不回退, 避免乱序 commit 把水位拽回去导致重复处理。
        """
        wxid = self.resolve(nick)
        if not wxid:
            return
        if seq and seq > self._last_seq.get(wxid, 0):
            self._last_seq[wxid] = seq

    def recent(self, nick, limit=10):
        """某会话最近消息预览。只读。返回 [(sender, text, is_self)]"""
        self._require()
        wxid = self.resolve(nick)
        if not wxid:
            return []
        try:
            rows = self._db.get_messages(user=wxid, limit=limit)
        except Exception:
            return []
        is_group = str(wxid).endswith("@chatroom")
        return [(self._sender(r), self._text(r), self._is_self(r, is_group))
                for r in (rows or [])]

    def mark_all_seen(self):
        """首次启动时把所有会话水位推到当前, 跳过历史消息。"""
        self._require()
        for info in self.get_chats():
            wxid = info["wxid"]
            try:
                rows = self._db.get_messages(user=wxid, limit=1)
                if rows:
                    self._last_seq[wxid] = self._seq(rows[0])
            except Exception:
                continue