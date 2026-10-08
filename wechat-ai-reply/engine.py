"""引擎: 监听 -> 生成回复 -> 安全检查 -> 发送。

默认 dry_run=True: 只生成回复并显示, 不真发。要真发必须在界面上
显式关掉试运行, 且通过 guard 的所有检查。
"""
import queue
import random
import threading
import time
from datetime import datetime

import config
import guard
import llm
import persona
import schedule
import style as style_mod
import wechat_ui


def _resp_fields(resp):
    """取 WxResponse 的 status/message。

    WxResponse 是 dict 子类, 不是普通对象: getattr(resp, 'status') 恒为
    None。所以必须用下标/取值, 属性访问只作为兜底。
    """
    status = message = ""
    if hasattr(resp, "get"):
        status = resp.get("status", "") or ""
        message = resp.get("message", "") or ""
    if not status:
        status = getattr(resp, "status", "") or ""
    if not message:
        message = getattr(resp, "message", "") or ""
    return str(status), str(message)


class Engine:
    def __init__(self, on_log=None, on_pending=None, on_status=None):
        self.on_log = on_log or (lambda m: None)
        self.on_pending = on_pending or (lambda items: None)
        self.on_status = on_status or (lambda s: None)

        self._reader = None
        self._wx = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending = []          # 待人工确认
        self._pending_lock = threading.Lock()
        self._hist = {}             # chat -> [(role, content)]
        # 每个联系人一个队列 + 一个独立工作线程, 互不阻塞
        self._chat_queues = {}
        self._chat_lock = threading.Lock()
        # 在途消息占位: nick -> {seq,...}。水位是处理完才提交的,
        # 处理期间每 3 秒的轮询会重复读到同一条, 用它挡住。
        self._inflight = {}
        # 待人工确认条目的自增ID。列表会被新消息刷新重排, 所以删除必须
        # 靠这个ID定位, 用索引会删错人。
        self._next_pid = 1
        self.dry_run = True
        self.whitelist = []
        self.running = False
        self.auto_send = False
        # 全自动: 命中敏感场景也直接发, 不进人工队列。
        # 用户明确要求「不经过我的同意」时打开。每日上限依然生效。
        self.full_auto = False
        # 主动聊天: 定时挑白名单里的人, 读历史后主动开口
        self.proactive_enabled = False
        # 定时任务: 到点自己找一个人开口, 时刻和内容都由用户指定
        self.schedule_enabled = False
        # 安静发送: 不最小化你的窗口、不把微信顶到前面。
        # 失败会自动退回库原本的行为重试一次, 保证不丢消息。
        self.quiet_send = True
        self._sched_fired = {}      # {任务id: 已发日期}, 防同一分钟重复触发
        self._sched_idx = 0         # 轮转挑人用的游标
        self.proactive_interval_min = config.PROACTIVE_INTERVAL_MIN
        # 微信窗口行为: "silent"(最小化不打扰) | "normal"(原版自动弹)
        self.hide_mode = config.HIDE_MODE
        self._proactive_thread = None
        self._last_proactive = None
        self._proactive_idx = 0

    # ---------- 资源 ----------

    def _log(self, msg):
        self.on_log(msg)

    def _status(self, s):
        self.on_status(s)

    # ---------- 安静发送(不让别人家的窗口被抢) ----------

    def _send_msg(self, msg, nick):
        """发一条。安静模式失败时退回库原本行为重试一次。

        返回 (status, message)。安静模式下第一次失败是有可能的: 会话刚
        切过、微信刚重启、输入框焦点丢了 —— 这些都得让库把窗口抬起来
        才能操作。所以失败必须重试, 否则等于为了不打扰你而丢消息。
        """
        resp = self._wx.SendMsg(msg=msg, who=nick, exact=True)
        status, message = _resp_fields(resp)
        if status == "\u6210\u529f" or not self.quiet_send:
            return status, message
        if not self._unpatch_quiet():
            return status, message          # \u8865\u4e01没装上, 无可退
        self._log("[\u5b89\u9759] \u7b2c\u4e00\u6b21\u6ca1\u6210, \u9000\u56de"
                  "\u6b63\u5e38\u65b9\u5f0f\u91cd\u8bd5"
                  "(\u4f1a\u77ed\u6682\u62a2\u7126\u70b9)")
        try:
            resp = self._wx.SendMsg(msg=msg, who=nick, exact=True)
            status, message = _resp_fields(resp)
        finally:
            self._repatch_quiet()
        return status, message

    def _patch_quiet(self):
        """把库的 ensure_visible 换成空操作, 让发送尽量不惊动桌面。

        库的 ensure_visible() 会做两件事, 都和你「别的窗口永远在微信
        前面」的要求冲突:
          _minimize_blockers() 把你挡在微信上面的窗口全部最小化
          bring_to_front()      把微信顶到最上层, 还置顶

        实测(2026-10-08): 只要会话已经开着、输入框有焦点, 发送走的是
        UIA 路径, 一次点击都不需要 —— 即使微信被 Chrome 完全盖住, 照样
        发得出去。所以直接把这一步变空操作, 你的窗口就一点不被动。

        打补丁后仍有失败可能(会话刚切、微信刚重启、焦点丢了), 这时
        _send_quiet_retry 会退回库原本的行为重试一次, 保证不丢消息。
        """
        if self._wx is None:
            return False
        gui = getattr(self._wx, "_gui", None)
        if gui is None or not hasattr(gui, "ensure_visible"):
            return False
        if not hasattr(gui, "_ensure_visible_orig"):
            gui._ensure_visible_orig = gui.ensure_visible
        gui.ensure_visible = lambda keep_topmost=True: True
        return True

    def _unpatch_quiet(self):
        """恢复库原本的 ensure_visible(重试时用)。"""
        if self._wx is None:
            return False
        gui = getattr(self._wx, "_gui", None)
        orig = getattr(gui, "_ensure_visible_orig", None)
        if orig is None:
            return False
        gui.ensure_visible = orig
        return True

    def _repatch_quiet(self):
        """重试之后把空操作补丁重新装回去。"""
        gui = getattr(self._wx, "_gui", None)
        if gui is None or not hasattr(gui, "_ensure_visible_orig"):
            return False
        gui.ensure_visible = lambda keep_topmost=True: True
        return True

    def _ensure_reader(self):
        if self._reader is None:
            # 延迟导入: reader 会拉起 wechatauto.db, 顶层导出会拖慢启动。
            # 注意这个模块名必须叫 reader_mod, 下面用的是这个名字。
            import reader as reader_mod
            self._reader = reader_mod.MessageReader()
        return self._reader

    def _connect_db(self):
        self._ensure_reader()
        if self._reader._db:
            return True, "已连接"
        ok, msg = self._reader.connect()
        return ok, msg

    def connect_wx(self):
        """连接微信发送端 (只在真要发时才连)。"""
        if self._wx is not None:
            return True, "已连接"
        try:
            from wechatauto import WeChat
            self._wx = WeChat()
            if self.quiet_send and self._patch_quiet():
                return True, "微信发送端已连接(安静模式)"
            return True, "微信发送端已连接"
        except Exception as e:
            self._wx = None
            return False, f"微信连接失败: {type(e).__name__}: {e}"

    # ---------- 读取列表 (给 UI 用) ----------

    def load_chats(self):
        """读取全部会话供 UI 勾选。返回 [{name, wxid, last_msg}]"""
        ok, msg = self._connect_db()
        if not ok:
            raise RuntimeError(msg)
        return self._reader.get_chats()

    def preview(self, nick, limit=8):
        """预览某会话最近消息, 给 UI 看。"""
        ok, msg = self._connect_db()
        if not ok:
            return []
        return self._reader.recent(nick, limit=limit)

    def preview_many(self, nicks, limit=10, on_progress=None):
        """批量读取多个会话的历史 -> {昵称: [(sender,text,is_self)]}。

        给「一键抓取白名单全部历史」用。单个会话读失败不影响其它。
        """
        ok, msg = self._connect_db()
        if not ok:
            return {}
        out = {}
        total = len(nicks)
        for i, nick in enumerate(nicks, 1):
            if on_progress:
                on_progress(i, total, nick)
            try:
                rows = self._reader.recent(nick, limit=limit)
            except Exception as e:
                self._log(f"[历史] {nick} 读取失败: {type(e).__name__}: {e}")
                continue
            if rows:
                out[nick] = rows
        return out

    # ---------- 主循环 ----------

    def start(self, whitelist):
        with self._lock:
            self.whitelist = list(whitelist)
        self._stop.clear()

        ok, msg = self._connect_db()
        if not ok:
            self._log(f"[错误] 数据库连接失败: {msg}")
            self._status("数据库连接失败")
            return False

        # 首次启动跳过历史消息
        self._reader.mark_all_seen()
        self._log(f"已跳过历史消息, 水位对齐完成")

        # 检查模型
        alive, info = llm.check_alive()
        if not alive:
            self._log(f"[警告] 模型服务不可达 ({config.LLM_BASE}), "
                      f"监听会启动但无法生成回复: {info}")
        else:
            self._log(f"模型服务在线: {config.LLM_BASE}")

        if self.hide_mode != "normal":
            ok2, m2 = wechat_ui.hide_window(self.hide_mode)
            if ok2:
                self._log(f"[窗口] 全静默模式: {m2}")
            else:
                # 启动瞬间微信可能还没把窗口打开(比如收在托盘)。
                # 原来只试一次就永久放弃, 结果发送时微信照样弹到前台,
                # 等于全静默失效。这里起后台线程重试几次。
                self._log(f"[窗口] 全静默暂不可用: {m2}, 后台重试中")
                threading.Thread(target=self._retry_hide, daemon=True).start()
        else:
            self._log("[窗口] 原版模式, 发送时微信会自己弹出")

        if not self.dry_run:
            ok3, m3 = self.connect_wx()
            self._log(m3)
            if not ok3:
                self._log("[错误] 无法连接微信, 已退回试运行")
                self.dry_run = True

        self.running = True
        self._status("监听中" + (" (试运行)" if self.dry_run else " (真发)"))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        if self.proactive_enabled:
            if self.dry_run or not self.auto_send:
                self._log("[主动] 试运行模式下不主动发消息, 已忽略主动聊天设置")
                self.proactive_enabled = False
            else:
                self._proactive_thread = threading.Thread(
                    target=self._loop_proactive, daemon=True)
                self._proactive_thread.start()
                self._log(f"[主动] 已启用, 每 {self.proactive_interval_min} 分钟"
                          f"对白名单轮转发起一次聊天")
        # 定时任务独立成线: 它按「点」触发(9:30 就 9:30 发), 主动聊天按
        # 「间隔」, 两者节律不同, 互不影响。
        if self.schedule_enabled and schedule.load():
            self._sched_thread = threading.Thread(
                target=self._loop_schedule, daemon=True)
            self._sched_thread.start()
            nxt = schedule.next_times(3)
            self._log(f"[定时] 已启用, 共 {len(schedule.load())} 个任务"
                      + (f", 最近: {nxt[0][0]}" if nxt else ""))
        return True

    def _last_seq_for(self, nick):
        """当前会话最新一条的 sort_seq, 用来把水位推过自己刚发的消息。"""
        try:
            rows = self._reader.recent(nick, limit=1)
        except Exception:
            return 0
        if not rows:
            return 0
        try:
            wxid = self._reader.resolve(nick)
            rows2 = self._reader._db.get_messages(user=wxid, limit=1)
            if rows2:
                return self._reader._seq(rows2[0])
        except Exception:
            pass
        return 0

    def _rehide(self):
        """发送后把微信窗口藏回去(全静默模式)。

        原来是先 wait(1.0) 再藏 —— 那 1 秒窗口正好亮在屏幕上,
        所以用户看得到一闪。现在改成: 立刻藏, 然后短时间内再补两刀。
        微信把窗口推到前台的时机不固定, 藏一次会漏掉, 所以补刀。
        """
        ok_count = 0
        for delay in (0.0, 0.25, 0.8):
            if self._stop.wait(delay):
                return
            try:
                ok, _msg = wechat_ui.hide_window(self.hide_mode)
            except Exception:
                return
            if ok:
                ok_count += 1
                self._log("[窗口] 已重新隐藏")
            if ok_count >= 2:      # 藏住两次就够了, 微信不会顶第三次
                return

    def _release_fg(self):
        """发完收尾: 取消微信置顶 + 还原被库最小化的遮挡窗口。

        必须调。库的 ensure_visible() 会做两件会残留的事:
          1. bring_to_front(keep_topmost=True) -> 微信永远压在所有窗口之上
          2. _minimize_blockers()             -> 把你正在用的 Chrome/编辑器
                                                最小化, 而且不自己还原
        库的 docstring 明确写了这一步要调用方在整批操作结束后做一次。
        我们之前从没调过, 所以每发一条消息就攒一份: 微信置顶不退,
        别人的窗口越攒越多被最小化。
        """
        wx = self._wx
        if wx is None:
            return
        # self._wx 是 wechatauto.wx.WeChat, 它自己不暴露置顶管理;
        # 真正有这个能力的是它内部的 WeChatGUI(构造时 self._gui = WeChatGUI())。
        gui = getattr(wx, "_gui", None)
        fn = getattr(gui, "release_foreground", None)
        if callable(fn):
            try:
                n = fn()
                if n:
                    self._log(f"[窗口] 已还原被最小化的窗口 {n} 个")
                return
            except Exception as e:
                self._log(f"[窗口] release_foreground 失败: "
                          f"{type(e).__name__}: {e}")
        else:
            self._log("[窗口] 拿不到 release_foreground, 只撤置顶")
        # 兜底: 至少把微信的置顶撤掉, 这个我们自己就能做
        try:
            ok, m = wechat_ui.clear_topmost()
            if ok:
                self._log(f"[窗口] {m}")
        except Exception as e:
            self._log(f"[窗口] 撤置顶也失败: {type(e).__name__}")

    def _retry_hide(self, tries=6):
        """后台重试最小化。全静默要真的静默, 不能只试一次。"""
        for i in range(tries):
            if self._stop.wait(3.0):
                return
            ok, msg = wechat_ui.hide_window(self.hide_mode)
            if ok:
                self._log(f"[窗口] 全静默已生效 (第 {i + 1} 次重试)")
                return
        self._log("[窗口] 重试多次仍未最小化, 发送时微信可能会弹出")

    def stop(self):
        self._stop.set()
        self.running = False
        self.proactive_enabled = False
        self.schedule_enabled = False
        # 叫醒所有联系人线程让它们退出
        with self._chat_lock:
            queues = list(self._chat_queues.values())
        for q in queues:
            q.put(None)
        self._status("已停止")
        self._log("监听已停止")
        if self.hide_mode != "normal":
            wechat_ui.restore_window()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                n = guard.mark_error()
                self._log(f"[错误] 轮询异常 ({n}/{config.MAX_CONSECUTIVE_ERRORS}): "
                          f"{type(e).__name__}: {e}")
                if n >= config.MAX_CONSECUTIVE_ERRORS:
                    self._log("[停机] 连续错误达上限, 已自动停止监听")
                    self.stop()
                    return
            self._stop.wait(config.POLL_INTERVAL)

        # 收尾
        if self._wx is not None:
            try:
                self._wx.StopListening()
            except Exception:
                pass

    # ---------- 定时主动聊天 ----------

    def _loop_proactive(self):
        """独立线程: 到点挑一个白名单的人, 读历史, 主动发一条。"""
        while not self._stop.is_set() and self.proactive_enabled:
            # 首次先等一个间隔, 不要一启动就发
            if self._stop.wait(config.PROACTIVE_TICK * 60):
                return

            if not self.proactive_enabled:
                return
            try:
                self._proactive_once()
            except Exception as e:
                n = guard.mark_error()
                self._log(f"[错误] 主动聊天异常 ({n}/{config.MAX_CONSECUTIVE_ERRORS}): "
                          f"{type(e).__name__}: {e}")
                if n >= config.MAX_CONSECUTIVE_ERRORS:
                    self._log("[停机] 主动聊天连续错误达上限, 已关闭")

    # ---------- 定时任务 ----------

    def _loop_schedule(self):
        """独立线程: 每 30 秒看一眼有没有到点的任务。

        用自己的时钟, 不复用 PROACTIVE_TICK —— 定时任务按「点」触发,
        主动聊天按「间隔」, 节律不同。
        """
        while not self._stop.is_set() and self.schedule_enabled:
            if self._stop.wait(30):
                return
            try:
                self._schedule_tick()
            except Exception as e:
                n = guard.mark_error()
                self._log(f"[错误] 定时任务异常 ({n}): "
                          f"{type(e).__name__}: {e}")

    def _schedule_tick(self):
        today = datetime.now().strftime("%Y-%m-%d")
        for item, content in schedule.due(fired=self._sched_fired):
            # 先占位再执行: 轮询 30 秒一次, 不先占位会在同一分钟内重复发
            self._sched_fired[item["id"]] = today
            try:
                ok, why = self._schedule_once(item, content)
            except Exception as e:
                ok, why = False, f"{type(e).__name__}: {e}"
            if ok:
                self._log(f"[定时-已发] {item['time']} → {why}")
            else:
                self._log(f"[定时-跳过] {item['time']}: {why}")

    def _schedule_once(self, item, content):
        """到点了: 挑一个白名单的人, 把你写的内容当话头让他开口。

        返回 (是否发了, 说明/内容)。内容直接当话头喂给模型, 模型用你
        那个人的口气把它说出来, 不会照抄你写的原句。
        """
        text = (content or "").strip()
        if not text:
            return False, "任务没写内容"
        if not self.whitelist:
            return False, "白名单是空的, 没人可发"
        ok, why = guard.can_send()
        if not ok:
            return False, why

        if self._sched_idx >= len(self.whitelist):
            self._sched_idx = 0
        nick = self.whitelist[self._sched_idx]
        self._sched_idx += 1

        reply = self._generate_proactive(nick, [], [(text, 2)], "定时任务")
        reply = llm.strip_markdown(reply)
        if not reply:
            return False, "模型返回空"

        # 复用 _send: 它已经处理了延迟、连接、发送、水位、重新压层、
        # 撤置顶还原被最小化的窗口。
        self._send(nick, f"(定时/{item['time']})", reply)
        return True, f"{nick}: {reply}"

    def _proactive_due(self):
        """距上次主动发是否已到间隔。返回 (是否该发, 原因)。"""
        if not self.proactive_enabled:
            return False, "未启用主动聊天"
        if not self.whitelist:
            return False, "白名单为空"
        # 只有真发模式才主动发, 试运行不主动骚扰别人
        if self.dry_run or not self.auto_send:
            return False, "试运行模式不主动发"
        if self._last_proactive is None:
            return True, "首次触发"
        gap = (time.time() - self._last_proactive) / 60.0
        if gap < self.proactive_interval_min:
            return False, f"距上次仅 {gap:.0f} 分钟, 间隔 {self.proactive_interval_min} 分"
        ok, why = guard.can_send()
        if not ok:
            return False, why
        return True, f"距上次 {gap:.0f} 分钟"

    def _proactive_pick(self, nick):
        """决定这次主动聊天的素材。返回 (来源, peer_msgs, topic_pairs)。

        topic_pairs 是 [(话题, 优先级)], 优先级 1 = 对方说了但我没接的
        话头(最适合现在接回去), 2 = 只是最近聊过。带着优先级回去,
        否则模型会随机挑一条然后答非所问。

        判定顺序(越靠前越可靠):
          1. 对方有未回消息   -> 直接回它
          2. 历史里有话头     -> 接真实聊过的话题
          3. 配了话题库       -> 用配置话题
          4. 什么都没有       -> 不发
        """
        hist = self._reader.recent(nick, limit=config.PROACTIVE_HISTORY)
        cfg_topics = persona.topic_list()
        cfg_pairs = [(t, 2) for t in cfg_topics]

        if not hist:
            # 没有任何聊天记录, 但主人自己配了话题 —— 那也是真实素材。
            if cfg_pairs:
                return "用配置话题", [], cfg_pairs
            return "无历史", [], []

        # recent() 是按时间倒序, 所以 [0] 是最新一条
        newest_is_peer = bool(hist) and not hist[0][2]
        peer_msgs = [t for _s, t, me in hist if not me and t]

        if newest_is_peer:
            return "接未回消息", peer_msgs, []

        hist_pairs = style_mod.pick_topics(hist)
        if hist_pairs:
            # 历史里出现过的话题就不再重复加配置版
            seen = {t for t, _ in hist_pairs}
            merged = hist_pairs + [(t, 2) for t in cfg_topics if t not in seen]
            return "接历史话头", peer_msgs, merged
        if cfg_pairs:
            return "用配置话题", peer_msgs, cfg_pairs
        if peer_msgs:
            return "接历史话题", peer_msgs, []
        return "无素材", [], []

    def _proactive_once(self):
        due, why = self._proactive_due()
        if not due:
            self._log(f"[主动-跳过] {why}")
            return

        # 轮转挑人, 避免每次都找同一个人
        if self._proactive_idx >= len(self.whitelist):
            self._proactive_idx = 0
        nick = self.whitelist[self._proactive_idx]
        self._proactive_idx += 1

        source, peer_msgs, topic_pairs = self._proactive_pick(nick)

        if source == "无素材" or source == "无历史":
            self._log(f"[主动-跳过] {nick}: 没有历史也没有配置话题, "
                      f"没素材就不硬编。去「身份设定 → 话题库」加几条。")
            return

        self._log(f"[主动-开始] {nick} (来源={source}, {why})")

        try:
            reply = self._generate_proactive(nick, peer_msgs, topic_pairs, source)
        except Exception as e:
            self._log(f"[错误] 主动生成失败: {type(e).__name__}: {e}")
            return

        reply = llm.strip_markdown(reply)
        if not reply:
            self._log("[主动-跳过] 模型返回空")
            return

        self._log(f"[主动-生成] {nick} ({source}): {reply}")
        self._send(nick, f"(主动/{source})", reply)

    def _generate_proactive(self, nick, peer_msgs, topic_pairs, source):
        """构造 prompt 并调用模型。

        关键: user 角色那一轮放的是**真实素材** (对方原话或真实聊过的话头),
        不是「请主动找话题聊」这种空指令。
        """
        hist = self._reader.recent(nick, limit=config.PROACTIVE_HISTORY)
        history = self._build_hist_from(hist)
        pairs = list(topic_pairs or [])

        if source == "接未回消息":
            trigger = peer_msgs[0] if peer_msgs else ""
            system = config.PROACTIVE_SYSTEM_PROMPT_ANSWER
            user_turn = f"（对方刚发了这句，我没回。帮我回一句。）\n{trigger}"
        else:
            system = config.PROACTIVE_SYSTEM_PROMPT_OPENER
            if pairs:
                # 区分「该接的话头」和「只是最近聊过」。不区分的话模型会
                # 随机挑一条然后答非所问。
                dangling = [t for t, pri in pairs if pri == 1]
                recent = [t for t, pri in pairs if pri != 1]
                seg = []
                if dangling:
                    seg.append("对方说了这些、但当时没接上的话头"
                               "(最适合现在接回去):\n"
                               + "\n".join(f"- {t}" for t in dangling[:4]))
                if recent:
                    seg.append("你们之前聊过这些:\n"
                               + "\n".join(f"- {t}" for t in recent[:4]))
                user_turn = (
                    "（对方没说话，是你自己想开口。\n"
                    "从上面真实聊过的话题里挑一个接上去，说一句让人想接话的话。\n"
                    "注意：要接着往下聊或问一句，不是自己回答自己"
                    "（别自己回答自己说的话）。不要照抄原文，"
                    "不要编造没发生过的共同经历。）\n"
                    + "\n".join(seg))
            elif peer_msgs:
                # 历史里有对方的话, 但都太短/太重复没被 pick_topics 挑中
                # (比如只回「嗯嗯」的人)。别退回「随便找个切口」—— 那等于
                # 凭空开聊。拿对方最近真实说过的话当锚。
                shown = "\n".join(f"- {t}" for t in peer_msgs[:5])
                user_turn = (
                    "（对方没说话，是你自己想开口。\n"
                    "对方之前只说过下面这些很短的话"
                    "（可能是在敷衍，也可能是当时不方便聊）。\n"
                    + shown
                    + "\n不要复述或引用这些原话，就顺着当时的感觉随口问一句"
                    "（比如问他在忙什么、怎么今天才回）。"
                    "不要编造没发生过的共同经历。）")
            else:
                user_turn = "（随便找个自然的切口说一句。）"

        # 带上这个人的说话习惯, 语气才像在跟同一个人说话
        style_note = None
        try:
            style_note = style_mod.analyze(hist)
        except Exception:
            style_note = None

        return llm.chat(user_turn, history=history, system=system,
                        style_note=style_note)
    @staticmethod
    def _build_hist_from(hist):
        """把 recent() 的 [(sender,text,is_self)] 转成 chat history。"""
        out = []
        for sender, text, is_self in hist:
            role = "assistant" if is_self else "user"
            out.append({"role": role, "content": text})
        return out[-6:]

    def _tick(self):
        msgs = self._reader.new_messages(self.whitelist)
        if not msgs:
            return
        guard.reset_errors()

        # 每个联系人一个队列, 互不阻塞。
        # 之前是串行 for 循环: A 卡在模型生成上 30 秒, B 的消息就干等,
        # 而且 B 的水位已经被 reader 推进了, A 一旦抛异常 B 就直接丢消息。
        for nick, sender, text, is_self, seq in msgs:
            # 双保险: 即使 reader 方向判断出错, 也绝不回复自己发的消息,
            # 否则会出现「AI 回自己 → 读到自己的回复 → 再回」的死循环。
            if is_self:
                self._log(f"[已回复{nick}] : {text[:30]}")
                self._reader.commit(nick, seq)
                continue
            if not text:
                self._reader.commit(nick, seq)
                continue
            # 在途去重: 水位是「处理完才提交」, 而处理一条要十几秒,
            # 期间每 3 秒的轮询会把同一条再读一遍 → 同一条消息回复多次。
            # 这里按 seq 占位, 处理完(或失败)再释放。
            with self._chat_lock:
                inflight = self._inflight.setdefault(nick, set())
                if seq in inflight:
                    continue
                inflight.add(seq)
            self._enqueue(nick, sender, text, seq)

    def _enqueue(self, nick, sender, text, seq):
        """把一条消息投到这个联系人自己的队列。"""
        with self._chat_lock:
            q = self._chat_queues.get(nick)
            if q is None:
                q = queue.Queue()
                self._chat_queues[nick] = q
                threading.Thread(target=self._chat_worker, args=(nick, q),
                                 daemon=True, name=f"chat-{nick}").start()
        q.put((sender, text, seq))

    def _chat_worker(self, nick, q):
        """单个联系人的处理循环。

        独立线程 = 独立上下文、独立历史、独立失败。某个联系人卡住或出错,
        不影响其他联系人。停止时靠 _stop 标志退出。
        """
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if item is None:
                return
            sender, text, seq = item
            try:
                self._handle(nick, sender, text)
            except Exception as e:
                n = guard.mark_error()
                self._log(f"[错误] {nick} 处理失败 ({n}/{config.MAX_CONSECUTIVE_ERRORS}): "
                          f"{type(e).__name__}: {e}")
                # 失败: 释放占位但不 commit, 下轮还会拿到这条, 不丢消息。
                self._release(nick, seq)
                self._stop.wait(config.RETRY_BACKOFF)
                continue
            # 处理成功才推进水位并释放占位
            self._reader.commit(nick, seq)
            self._release(nick, seq)

    def _release(self, nick, seq):
        """释放在途占位, 让后续重复的同 seq 能被处理或重试。"""
        with self._chat_lock:
            s = self._inflight.get(nick)
            if s:
                s.discard(seq)

    # ---------- 单条消息处理 ----------

    def _handle(self, nick, sender, text):
        self._log(f"[收到] {nick}: {text[:50]}")

        # 1. 敏感场景: 全自动模式下照常生成并发送, 否则转人工
        need, why = guard.needs_human(text)
        if need and not self.full_auto:
            self._log(f"[转人工] {nick}: {why}")
            self._add_pending(nick, sender, text, "(未生成回复)")
            return
        if need and self.full_auto:
            self._log(f"[敏感-仍自动] {nick}: {why} (全自动模式)")

        # 2. 生成回复
        # 从这个联系人自己的真实消息里提取说话习惯, 让语气贴合对方。
        # 样本不够时 style.analyze 返回 None, 退回通用规则。
        style_note = None
        try:
            hist = self._reader.recent(nick, limit=20)
            style_note = style_mod.analyze(hist)
            if style_note:
                self._log(f"[风格] 已按 {nick} 的聊天习惯调整语气")
        except Exception:
            style_note = None

        try:
            raw = llm.chat(text, history=self._hist.get(nick),
                            style_note=style_note)
        except Exception as e:
            n = guard.mark_error()
            self._log(f"[错误] 模型调用失败 ({n}/{config.MAX_CONSECUTIVE_ERRORS}): {e}")
            return
        reply = llm.strip_markdown(raw)
        if not reply:
            self._log(f"[跳过] 模型返回空: {nick}")
            return
        self._log(f"[生成] {nick}: {reply[:50]}")

        # 3. 回复本身也要过安全检查
        need2, why2 = guard.needs_human(text, reply)
        if need2 and not self.full_auto:
            self._log(f"[转人工] {nick}: {why2}")
            self._add_pending(nick, sender, text, reply)
            return
        if need2 and self.full_auto:
            self._log(f"[敏感-仍自动] {nick}: {why2} (全自动模式)")

        # 4. 试运行: 到此为止
        if self.dry_run or not self.auto_send:
            self._add_pending(nick, sender, text, reply, auto=False)
            return

        self._send(nick, text, reply)

    def _send(self, nick, origin_text, reply):
        ok, why = guard.can_send()
        if not ok:
            self._log(f"[拦截] {nick}: {why}")
            self._add_pending(nick, "(原始)", origin_text, reply, auto=False)
            return

        wait = guard.rate_limit_ok()
        if wait > 0:
            self._stop.wait(wait)

        time.sleep(random.uniform(config.REPLY_DELAY_MIN, config.REPLY_DELAY_MAX))

        try:
            self.connect_wx()
            if self._wx is None:
                raise RuntimeError("微信未连接")
            status, message = self._send_msg(reply, nick)
            if status == "成功":
                cnt = guard.mark_sent()
                self._log(f"[已发] {nick}: {reply[:40]} (今日 {cnt}/{config.DAILY_SEND_LIMIT})")
                # 立刻把水位推过这条, 否则下一轮会把自己刚发的读回来,
                # 又触发一次"[已回复xxx]"的无用日志。
                self._reader.commit(nick, self._last_seq_for(nick))
                # 全静默: 发送过程会把微信窗口顶到前台, 发完立刻再藏回去。
    # 全静默: 发送过程会把微信窗口顶到前台, 发完要立刻藏回去。
                # 藏几次、隔多久由 _rehide 自己决定, 这里只负责起线程。
                if self.hide_mode != "normal":
                    threading.Thread(target=self._rehide, daemon=True).start()
                self._release_fg()
                self._hist.setdefault(nick, []).append({"role": "user", "content": origin_text})
                self._hist[nick].append({"role": "assistant", "content": reply})
                self._hist[nick] = self._hist[nick][-8:]
            else:
                self._log(f"[发送失败] {nick}: {status} {message}")
                self._add_pending(nick, origin_text, reply, auto=False)
        except Exception as e:
            n = guard.mark_error()
            self._log(f"[错误] 发送异常 ({n}/{config.MAX_CONSECUTIVE_ERRORS}): {e}")
            self._add_pending(nick, origin_text, reply, auto=False)

    def send_manual(self, nick, text):
        """人工确认后真实发送一条。返回 (成功, 说明)。

        绕过 dry_run 与敏感词检查 —— 用户在弹窗里明确按下发送, 就是最终决定。
        仍然计入每日上限, 保持计数一致。
        """
        text = (text or "").strip()
        if not text:
            return False, "内容为空"
        if not nick:
            return False, "没有目标会话"

        try:
            self.connect_wx()
            if self._wx is None:
                return False, "微信未连接"
        except Exception as e:
            return False, f"连接微信失败: {type(e).__name__}: {e}"

        wait = guard.rate_limit_ok()
        if wait > 0:
            time.sleep(wait)

        try:
            status, message = self._send_msg(text, nick)
            # 不管成没成都要收尾, 否则微信会一直置顶、别人的窗口一直被最小化
            self._release_fg()
            if status == "成功":
                cnt = guard.mark_sent()
                return True, f"已发送 (今日 {cnt}/{config.DAILY_SEND_LIMIT})"
            return False, f"{status} {message}".strip()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ---------- 逐人生成(按各自历史) ----------

    def reply_all(self, nicks, on_progress=None):
        """给白名单每个人**分别**生成一条回复并发送。

        每个人用的是自己的历史/自己的未回消息, 内容互不相同。
        返回 [(nick, 内容, 成功?, 说明)]。
        """
        results = []
        total = len(nicks)
        for i, nick in enumerate(nicks, 1):
            if self._stop.is_set():
                self._log("[逐人回复] 已停止, 中途退出")
                break
            if on_progress:
                on_progress(i, total, nick)
            try:
                source, peer_msgs, topic_pairs = self._proactive_pick(nick)
                if source in ("无素材", "无历史"):
                    self._log(f"[逐人-跳过] {nick}: 没有历史也没配话题")
                    results.append((nick, "", False, "无素材"))
                    continue
                content = self._generate_proactive(nick, peer_msgs,
                                                   topic_pairs, source)
                content = llm.strip_markdown(content)
                if not content:
                    results.append((nick, "", False, "模型返回空"))
                    continue

                if self.dry_run or not self.auto_send:
                    self._log(f"[逐人-试运行] {nick}: {content[:40]}")
                    self._add_pending(nick, "(逐人回复)", source, content,
                                      auto=False)
                    results.append((nick, content, False, "试运行未发"))
                    continue

                self._log(f"[逐人-生成] {nick} ({source}): {content[:40]}")
                ok, msg = self.send_manual(nick, content)
                if ok:
                    self._reader.commit(nick, self._last_seq_for(nick))
                else:
                    self._log(f"[逐人-失败] {nick}: {msg}")
                results.append((nick, content, ok, msg))
            except Exception as e:
                n = guard.mark_error()
                self._log(f"[逐人-错误] {nick}: {type(e).__name__}: {e} "
                          f"({n}/{config.MAX_CONSECUTIVE_ERRORS})")
                results.append((nick, "", False, f"{type(e).__name__}: {e}"))
        return results
    # ---------- 待人工确认队列 ----------

    def _add_pending(self, nick, sender, text, reply, auto=True):
        with self._pending_lock:
            self._pending.append({
                "pid": self._next_pid,          # 稳定ID, 不能用索引
                "nick": nick, "sender": sender, "text": text,
                "reply": reply, "auto": auto,
            })
            self._next_pid += 1
            items = list(self._pending)
        self.on_pending(items)

    def pending_list(self):
        with self._pending_lock:
            return list(self._pending)

    def clear_pending(self):
        with self._pending_lock:
            self._pending.clear()
        self.on_pending([])