"""tkinter 界面: 白名单勾选 / 启停 / 日志 / 待人工确认队列。"""
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import messagebox

import ttkbootstrap as ttk

import config
import engine as engine_mod
import llm
import modelcfg
import persona as persona_mod

POLL_MS = 400

# 界面尺寸与字号
WIN_W, WIN_H = 1500, 950
MIN_W, MIN_H = 1180, 720
FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE = 12


def _install_placeholder(text, placeholder, real_fg):
    """灰字占位提示, 一敲键盘就消失并恢复正常颜色。

    比原来那个浮在输入框上的 label 好: 不挡字, 而且显示完整示例。
    """
    ph = placeholder.strip()
    if not ph:
        return
    text.insert("1.0", ph)
    try:
        text.configure(fg="#6b6b6b")
    except tk.TclError:
        pass

    def on_key(_evt=None):
        # 只在内容仍等于占位文案时才清, 避免删掉用户已输入的字
        if text.get("1.0", "end").strip() == ph:
            text.delete("1.0", "end")
            try:
                text.configure(fg=real_fg)
            except tk.TclError:
                pass
            # 解绑要用 bind 返回的 funcid。unbind(seq, func) 这种传函数的
            # 写法 tkinter 不支持, 会抛 TypeError。
            if funcid:
                try:
                    text.unbind("<KeyPress>", funcid)
                except tk.TclError:
                    pass

    funcid = text.bind("<KeyPress>", on_key)


def apply_font(size=FONT_SIZE, family=FONT_FAMILY):
    """套用 ttkbootstrap 主题 + 放大字体。

    ttk 控件不接受 -font 参数(会报 unknown option "-font"), 只能改样式。
    而 Text/Listbox 这类原生 tk 控件 ttkbootstrap 管不到, 深色主题下会
    白底黑字非常刺眼, 所以要从主题里取色手动配。
    """
    st = ttk.Style()
    try:
        st.theme_use(config.UI_THEME)
    except Exception:
        st.theme_use("darkly")

    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                 "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=size)
        except tk.TclError:
            pass

    st.configure("TLabel", font=(family, size, "bold"))
    st.configure("TButton", font=(family, size))
    st.configure("TCheckbutton", font=(family, size))
    st.configure("TRadiobutton", font=(family, size))
    st.configure("TNotebook.Tab", font=(family, size))
    try:
        st.configure(".", font=(family, size))
    except tk.TclError:
        pass

    # 原生 tk 控件跟随主题配色, 否则深色主题下白底会很跳
    try:
        bg = st.colors.inputbg
        fg = st.colors.inputfg
        st.configure("TEntry", fieldbackground=bg, foreground=fg,
                     insertcolor=fg, bordercolor=st.colors.border)
        st.configure("TCombobox", fieldbackground=bg, foreground=fg,
                     insertcolor=fg, bordercolor=st.colors.border)
        st.configure("TSpinbox", fieldbackground=bg, foreground=fg,
                     insertcolor=fg, bordercolor=st.colors.border)
        st.configure("TNotebook", background=st.colors.bg)
        st.configure("TNotebook.Tab", background=st.colors.bg)
        return st, bg, fg
    except Exception:
        return st, "#ffffff", "#000000"


class App:
    def __init__(self, root):
        self.root = root
        apply_font()
        root.title("微信 AI 自动回复 - 本地模型")
        root.geometry(f"{WIN_W}x{WIN_H}")
        root.minsize(MIN_W, MIN_H)
        try:
            root.state("zoomed")
        except tk.TclError:
            pass

        self.engine = engine_mod.Engine(
            on_log=self._post_log,
            on_pending=self._post_pending,
            on_status=self._post_status,
        )

        self._q = queue.Queue()
        self._pending = []
        self._log_lines = []

        # 白名单勾选状态: 昵称 -> BooleanVar
        self._chk = {}
        self._chats = []

        # 主题色, 给原生 tk 控件(Text/Listbox)用
        self._style, self._c_bg, self._c_fg = apply_font()

        self._build()
        self.root.after(POLL_MS, self._pump)
        threading.Thread(target=self._load_chats, daemon=True).start()

    # ---------- 布局 ----------

    def _build(self):
        # 字体必须在任何控件创建前定好, 否则 ttk 用的是旧默认值
        self.font_title = (FONT_FAMILY, FONT_SIZE + 1, "bold")
        self.font_body = (FONT_FAMILY, FONT_SIZE)
        self.font_small = (FONT_FAMILY, FONT_SIZE - 1)
        self.tk_text_kw = dict(font=self.font_body, bg=self._c_bg,
                               fg=self._c_fg, insertbackground=self._c_fg,
                               relief="solid", borderwidth=1,
                               highlightthickness=1,
                               highlightbackground=self._style.colors.border)

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="未启动")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=(4, 0))

        ttk.Button(top, text="模型设置", command=self._open_model).pack(
            side="right", padx=6)
        ttk.Button(top, text="身份设定", command=self._open_persona).pack(
            side="right", padx=6)

        ttk.Button(top, text="刷新会话",
                   command=self._load_chats).pack(side="right")
        self.btn_start = ttk.Button(top, text="开始监听",
                                    command=self._start)
        self.btn_start.pack(side="right", padx=6)
        self.btn_stop = ttk.Button(top, text="停止",
                                    command=self._stop, state="disabled")
        self.btn_stop.pack(side="right", padx=6)

        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="试运行 (只生成回复, 不真发)",
                        variable=self.dry_var,
                        command=lambda: self._set_mode("dry")).pack(
                            side="right", padx=14)

        self.full_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="全自动 (敏感话题也直接发, 不问我)",
                        variable=self.full_auto_var,
                        command=lambda: self._set_mode("full")).pack(
                            side="right", padx=14)

        self.mode_var = tk.StringVar(value="试运行(不真发)")
        ttk.Label(top, textvariable=self.mode_var).pack(side="right", padx=4)

        pane = ttk.Panedwindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=6, pady=4)

        # 用 grid 而不是 pack: pack 的 expand 会让先 pack 的控件吃光剩余
        # 空间, 把后面的控件挤出可视区 (log_text 曾经把待确认区挤没了)。
        # grid + rowconfigure(weight) 才能确定性分配各区块高度。

        # 左: 白名单
        left = ttk.Frame(pane, padding=4)
        pane.add(left, weight=1)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        ttk.Label(left, text="白名单 (只回复勾选的人)").grid(
            row=0, column=0, sticky="w", pady=(2, 2))
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *_: self._refresh_list())
        se = ttk.Entry(left, textvariable=self.search_var)
        se.grid(row=1, column=0, sticky="ew", ipady=5, pady=(2, 6))

        self.listbox_frame = ttk.Frame(left)
        self.listbox_frame.grid(row=2, column=0, sticky="nsew")
        self.list_canvas = tk.Canvas(self.listbox_frame, highlightthickness=0)
        self.list_scroll = ttk.Scrollbar(self.listbox_frame,
                                          orient="vertical",
                                          command=self.list_canvas.yview)
        self.list_inner = ttk.Frame(self.list_canvas)
        self.list_inner.bind("<Configure>",
                              lambda e: self.list_canvas.configure(
                                  scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.create_window((0, 0), window=self.list_inner,
                                       anchor="nw")
        self.list_canvas.configure(yscrollcommand=self.list_scroll.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_scroll.pack(side="right", fill="y")

        sel_bar = ttk.Frame(left)
        sel_bar.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(sel_bar, text="全选", width=7,
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(sel_bar, text="全不选", width=7,
                   command=lambda: self._set_all(False)).pack(side="left",
                                                               padx=4)
        ttk.Button(sel_bar, text="反选", width=7,
                   command=self._invert).pack(side="left")
        self.sel_label = ttk.Label(sel_bar, text="已选: 0")
        self.sel_label.pack(side="left", padx=12)

        hist_bar = ttk.Frame(left)
        hist_bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(hist_bar, text="抓历史范围:").pack(side="left",
                                                    padx=(0, 6))
        self.hist_scope = tk.StringVar(value="all")
        ttk.Radiobutton(hist_bar, text="已勾选的人", value="all",
                        variable=self.hist_scope).pack(side="left")
        ttk.Radiobutton(hist_bar, text="仅", value="one",
                        variable=self.hist_scope).pack(side="left", padx=(8, 2))
        self.hist_var = tk.StringVar()
        ttk.Entry(hist_bar, textvariable=self.hist_var, width=14).pack(
            side="left", ipady=4)
        # 历史条数。Spinbox 必须给 to=, 所以这里给个实际用不完的大数当
        # "没上限"。之前默认 30 + to=200, 想抓多点还改不动。
        self.hist_count = tk.IntVar(value=999)
        ttk.Spinbox(hist_bar, from_=5, to=99999, increment=5,
                    textvariable=self.hist_count, width=6).pack(side="left",
                                                                padx=4)

        # 右: 日志 + 待确认
        right = ttk.Frame(pane, padding=4)
        pane.add(right, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=3)
        right.rowconfigure(5, weight=2)

        log_bar = ttk.Frame(right)
        log_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(log_bar, text="日志").pack(side="left", pady=(2, 2))
        self.btn_hist = ttk.Button(log_bar, text="获取历史消息",
                                   command=self._show_history)
        self.btn_hist.pack(side="right")

        self.log_text = tk.Text(right, wrap="word", height=8, **self.tk_text_kw)
        self.log_text.grid(row=1, column=0, sticky="nsew")

        # 主动聊天设置
        pro_bar = ttk.Frame(right)
        pro_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.proactive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pro_bar, text="定时主动聊天",
                        variable=self.proactive_var).pack(side="left")
        ttk.Label(pro_bar, text="每").pack(side="left", padx=(8, 2))
        self.pro_min_var = tk.StringVar(value=str(config.PROACTIVE_INTERVAL_MIN))
        self.pro_spin = ttk.Spinbox(pro_bar, from_=10, to=1440, increment=10,
                                    textvariable=self.pro_min_var, width=8)
        self.pro_spin.pack(side="left")
        ttk.Label(pro_bar, text="分钟 找一个人聊 (白名单轮转)").pack(side="left",
                                                                padx=4)
        self.schedule_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pro_bar, text="定时任务",
                        variable=self.schedule_var).pack(side="left",
                                                        padx=(14, 0))
        self.btn_sched = ttk.Button(pro_bar, text="编辑时刻和内容",
                                    command=self._show_schedules)
        self.btn_sched.pack(side="left", padx=(6, 0))

        # 微信窗口: 固定「最小化 + 安静发送」, 不给选了。
        # 实测下来三档(压层/最小化/正常)对发消息没有区别, 区别只在
        # 有多打扰; 最小化 + 安静发送是其中最安静的组合:
        # 平时真收进任务栏, 发送时不最小化你的窗口、不抢焦点。
        hide_bar = ttk.Frame(right)
        hide_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(hide_bar, text="微信窗口:").pack(side="left", padx=(0, 6))
        ttk.Label(hide_bar, text="最小化 · 安静发送(不抢你的窗口)",
                  foreground="#888").pack(side="left")
        ttk.Button(hide_bar, text="显示微信窗口",
                   command=self._show_wechat).pack(side="right")

        ttk.Label(right, text="待人工确认 (需你手动发)").grid(
            row=4, column=0, sticky="w", pady=(10, 2))
        pf = ttk.Frame(right)
        pf.grid(row=5, column=0, sticky="nsew")
        self.pending_list = tk.Listbox(pf, height=6, font=self.font_body,
                                       bg=self._c_bg, fg=self._c_fg,
                                       selectbackground=self._style.colors.primary,
                                       selectforeground=self._style.colors.primary,
                                       relief="solid", borderwidth=1)
        self.pending_list.pack(side="left", fill="both", expand=True)
        ps = ttk.Frame(pf)
        ps.pack(side="right", fill="y", padx=(8, 0))
        ttk.Button(ps, text="修改并发送", width=11,
                   command=self._open_send_dialog).pack(pady=3)
        ttk.Label(ps, text="不选也能写", foreground="#888").pack(pady=(2, 0))
        ttk.Button(ps, text="丢弃", width=11,
                   command=lambda: self._mark_done("已丢弃")).pack(pady=3)

    # ---------- 模型设置 (本地 / 外部 API) ----------

    def _open_model(self):
        win = tk.Toplevel(self.root)
        win.title("模型设置")
        win.geometry("640x440")
        win.minsize(560, 420)
        win.transient(self.root)

        cfg = modelcfg.load()
        state = {"provider": cfg["provider"], "win": win}
        info = tk.StringVar(value="")

        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="接入方式").grid(row=0, column=0, sticky="w",
                                              pady=(0, 10))
        prov = ttk.Frame(body)
        prov.grid(row=0, column=1, sticky="w", pady=(0, 10))
        ttk.Radiobutton(prov, text="本地模型", value="local",
                        variable=state["provider"],
                        command=lambda: _toggle()).pack(side="left")
        ttk.Radiobutton(prov, text="外部 API", value="api",
                        variable=state["provider"],
                        command=lambda: _toggle()).pack(side="left",
                                                        padx=(14, 0))

        rows = {}

        def add_row(idx, key, label, secret=False):
            ttk.Label(body, text=label).grid(row=idx, column=0, sticky="w",
                                             pady=6, padx=(0, 12))
            e = ttk.Entry(body, show="*" if secret else "")
            e.grid(row=idx, column=1, sticky="ew", ipady=5)
            e.insert(0, cfg.get(key, ""))
            rows[key] = e
            return idx + 1

        i = 1
        i = add_row(i, "api_base", "Base URL")
        i = add_row(i, "api_model", "模型名")
        i = add_row(i, "api_key", "API Key", secret=True)

        ttk.Label(body, text=(
            "本地: Base URL 填 llama.cpp / Ollama 的地址, 模型名一般留空。\n"
                             "外部: 任意 OpenAI 兼容服务, Base URL 通常带 /v1, 模型名必填。\n"
                             "API Key 只在外部模式生效; 留空则读环境变量 "
                             "WECHAT_LLM_API_KEY。"),
                  foreground="#999", justify="left").grid(
            row=i, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(win, textvariable=info, foreground="#c00", wraplength=590,
                  justify="left").pack(fill="x", padx=14, pady=(6, 0))

        def collect():
            data = {k: e.get().strip() for k, e in rows.items()}
            data["provider"] = state["provider"]
            return data

        def _toggle():
            # 之前这里把 API Key 置灰, 结果本地模式下根本点不进去,
            # 没法提前粘贴。改成永远可输入, 只用提示文字说明何时生效。
            key = rows["api_key"]
            key.configure(state="normal")
            if state["provider"] == "api":
                rows["api_model"].configure(state="normal")
            else:
                rows["api_model"].configure(state="normal")

        bar = ttk.Frame(win, padding=(14, 4, 14, 14))
        bar.pack(fill="x")

        def do_test():
            modelcfg.save(collect())
            info.set("测试中...")
            win.update_idletasks()

            def work():
                ok, detail = llm.check_alive()
                self._post_log(f"[模型] 测试 {state['provider']} -> {ok} {detail}")
                self._q.put(("ui_call", lambda: info.set(
                    ("可用  | " if ok else "不可用  | ") + str(detail)[:400])))

            threading.Thread(target=work, daemon=True).start()

        def do_save():
            ok, msg = modelcfg.save(collect())
            self._post_log(f"[模型] {msg} (provider={state['provider']})")
            info.set(msg)

        ttk.Button(bar, text="保存", command=do_save).pack(side="right")
        ttk.Button(bar, text="测试连接", command=do_test).pack(side="right",
                                                               padx=6)
        _toggle()

    # ---------- 身份设定 (用户补充提示词) ----------

    def _open_persona(self):
        win = tk.Toplevel(self.root)
        win.title("身份设定 - 在基础提示词上补充你自己的信息")
        win.geometry("760x720")
        win.minsize(600, 560)
        win.transient(self.root)

        head = ttk.Frame(win, padding=10)
        head.pack(fill="x")
        ttk.Label(head, text=(
            "下面三栏会追加到基础提示词后面，存到 persona.json，不用改代码。"
            "左边写身份，模型就按那个人设回消息。"), wraplength=720,
                  justify="left").pack(anchor="w")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        data = persona_mod.load()
        texts = {}

        fields = (
            ("persona", "我是谁", persona_mod.PLACEHOLDER_PERSONA,
             "模型会按这里的人设回消息。写身份、性格、说话习惯。"),
            ("rules", "额外规矩", persona_mod.PLACEHOLDER_RULES,
             "硬性约束，优先于模型自己的判断。例：不许主动提借钱。"),
            ("facts", "常备事实", persona_mod.PLACEHOLDER_FACTS,
             "被问到可以直接答，不用反问。写作息、地点、常用信息。"),
            ("topics", "话题库", persona_mod.PLACEHOLDER_TOPICS,
             "主动聊天的话题素材，一行一个。只从这里挑或接真实历史。"),
        )
        for key, tab, placeholder, hint in fields:
            page = ttk.Frame(nb, padding=8)
            nb.add(page, text=tab)
            ttk.Label(page, text=hint, wraplength=700,
                      foreground="#aaa",
                      justify="left").pack(anchor="w", pady=(0, 6))
            t = tk.Text(page, wrap="word", **self.tk_text_kw)
            t.pack(fill="both", expand=True)
            saved = data.get(key, "").strip()
            if saved:
                t.insert("1.0", saved)
            else:
                _install_placeholder(t, placeholder, self._c_fg)
            texts[key] = t

        bar = ttk.Frame(win, padding=(10, 4, 10, 10))
        bar.pack(fill="x")
        self.persona_status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.persona_status).pack(side="left")

        def do_save():
            payload = {k: t.get("1.0", "end").strip() for k, t in texts.items()}
            ok, msg = persona_mod.save(payload)
            self.persona_status.set(msg)
            if ok:
                n = sum(1 for v in payload.values() if v)
                self._post_log(
                    f"[身份设定] 已保存 ({n}/3 项生效), 下次生成回复时立即使用")

        def do_clear():
            for t in texts.values():
                t.delete("1.0", "end")
            self.persona_status.set("已清空, 点保存后生效")

        ttk.Button(bar, text="清空", command=do_clear).pack(side="right")
        ttk.Button(bar, text="保存", command=do_save).pack(side="right",
                                                          padx=6)

    # ---------- 历史消息 ----------

    def _show_history(self):
        # 抓取范围: 白名单(所有勾选的) 还是 单个对象
        if self.hist_scope.get() == "all":
            targets = self._selected()
            if not targets:
                messagebox.showwarning("提示", "没有勾选任何联系人")
                return
        else:
            nick = self.hist_var.get().strip()
            if not nick:
                messagebox.showwarning("提示", "先在上面填要看历史的聊天对象名")
                return
            targets = [nick]

        limit = self.hist_count.get()
        self.btn_hist.configure(state="disabled")
        self._post_status(f"正在读取 {len(targets)} 个会话的历史...")
        self._post_log(f"开始抓取历史: {len(targets)} 个会话, 每个最多 {limit} 条")

        def work():
            try:
                data = self.engine.preview_many(targets, limit=limit)
            except Exception as e:
                self._post_log(f"[错误] 读取历史失败: {type(e).__name__}: {e}")
                self._post_status("读取历史失败")
                self._post_log("__ENABLE_HIST__")
                return

            empty = [n for n in targets if n not in data]
            lines = [f"######## 历史抓取结果: {len(data)}/{len(targets)} 个会话有消息 ########"]
            for nick, rows in data.items():
                lines.append(f"\n===== {nick} (最新在前, {len(rows)} 条) =====")
                for sender, text, is_self in rows:
                    tag = "我  " if is_self else "对方"
                    lines.append(f"[{tag}] {text}")
            if empty:
                lines.append("")
                lines.append(f"以下 {len(empty)} 个会话没有读到消息: "
                             + ", ".join(empty[:20])
                             + ("..." if len(empty) > 20 else ""))
            self._post_log("\n".join(lines))
            self._post_status(f"历史抓取完成: {len(data)}/{len(targets)}")
            self._post_log("__ENABLE_HIST__")

        threading.Thread(target=work, daemon=True).start()

# ---------- 白名单 ----------

    def _load_chats(self):
        """读会话。DB 读取放后台线程, 控件创建必须在主线程。

        tkinter 不是线程安全的: 在子线程里碰任何控件都会抛
        RuntimeError: main thread is not in main loop, 而且是时序相关的,
        表现为「有时能载入有时不能」。所以这里只把数据丢回队列,
        由 _pump 在主线程建控件。
        """
        self._post_status("正在读取微信会话...")

        def work():
            try:
                chats = self.engine.load_chats()
            except Exception as e:
                self._q.put(("chats_error",
                             f"{type(e).__name__}: {e}"))
                return
            self._q.put(("chats", chats))

        threading.Thread(target=work, daemon=True).start()

    def _build_chat_widgets(self, chats):
        """在主线程建白名单控件。"""
        self._chats = chats
        keep = set(config.WHITELIST)
        self._chk.clear()
        for w in self.list_inner.winfo_children():
            w.destroy()
        for c in chats:
            v = tk.BooleanVar(value=(c["name"] in keep))
            self._chk[c["name"]] = v
        self.search_var.set("")
        self._refresh_list()
        self._post_log(f"已载入 {len(chats)} 个会话")
        self._post_status("未启动")

    def _refresh_list(self):
        kw = self.search_var.get().strip().lower()
        for w in self.list_inner.winfo_children():
            w.destroy()
        n = 0
        for c in self._chats:
            if kw and kw not in c["name"].lower():
                continue
            v = self._chk[c["name"]]
            label = c["last_msg"] or c["wxid"]
            ttk.Checkbutton(self.list_inner, text=f"{c['name']}  ({label[:20]})",
                            variable=v, command=self._update_count
                            ).pack(anchor="w", pady=1)
            n += 1
        self._update_count()

    def _selected(self):
        return [k for k, v in self._chk.items() if v.get()]

    def _update_count(self):
        self.sel_label.configure(text=f"已选: {len(self._selected())}")

    def _set_all(self, value):
        """全选/全不选。只改当前可见(搜索过滤后)的项, 否则搜索状态下
        点全选会去勾看不见的人, 那不是用户看到的范围。"""
        kw = self.search_var.get().strip().lower()
        n = 0
        for c in self._chats:
            if kw and kw not in c["name"].lower():
                continue
            var = self._chk.get(c["name"])
            if var is not None:
                var.set(value)
                n += 1
        self._update_count()
        self._post_log(f"[白名单] {'全选' if value else '全不选'} {n} 个"
                       + (f" (搜索: {kw})" if kw else ""))

    def _invert(self):
        kw = self.search_var.get().strip().lower()
        n = 0
        for c in self._chats:
            if kw and kw not in c["name"].lower():
                continue
            var = self._chk.get(c["name"])
            if var is not None:
                var.set(not var.get())
                n += 1
        self._update_count()
        self._post_log(f"[白名单] 反选 {n} 个"
                       + (f" (搜索: {kw})" if kw else ""))

    # ---------- 启停 ----------

    def _mode_name(self):
        if self.dry_var.get():
            return "试运行(不真发)"
        if self.full_auto_var.get():
            return "全自动(敏感也直发)"
        return "真发(敏感转人工)"

    def _push_mode(self):
        """把界面上的模式推到 engine。

        必须做成「随时可推」而不是只在 _start 时推一次, 否则监听过程中改
        勾选框不会生效 —— 界面显示全自动, 引擎还在试运行, 于是发不出去。
        """
        self.engine.dry_run = self.dry_var.get()
        self.engine.auto_send = not self.dry_var.get()
        self.engine.full_auto = self.full_auto_var.get()
        if hasattr(self, "mode_var"):
            self.mode_var.set(self._mode_name())

    def _set_mode(self, which):
        """明确指定点了哪个, 不靠猜哪个勾上了。

        两者互斥: 点全自动就取消试运行, 点试运行就取消全自动。
        两个都不点是合法模式(真发+敏感转人工), 所以不做强制互斥弹窗。
        """
        if which == "full":
            self.full_auto_var.set(True)
            self.dry_var.set(False)
        else:
            self.dry_var.set(True)
            self.full_auto_var.set(False)
        self._push_mode()
        if self.engine.running:
            self._post_log(f"[模式] 运行中切到 {self._mode_name()}")
            if not self.engine.dry_run:
                # 从试运行切到真发, 发送端要现连
                threading.Thread(target=self._warm_wx, daemon=True).start()

    def _warm_wx(self):
        ok, msg = self.engine.connect_wx()
        self._post_log(f"[模式] {msg}")

    def _show_wechat(self):
        """把最小化的微信叫回屏幕中间。"""
        def work():
            import wechat_ui
            ok, msg = wechat_ui.restore_window()
            self._post_log(f"[窗口] {msg}" if ok else f"[窗口] {msg}")
        threading.Thread(target=work, daemon=True).start()

    def _show_schedules(self):
        """定时任务编辑器: 时刻和内容都自己填。

        改动即时保存到 schedules.json, 不用点保存按钮 —— 少一个忘点
        就白填的场景。运行中新增的任务会在下次启动监听时生效。
        """
        import schedule

        win = tk.Toplevel(self.root)
        win.title("定时任务 — 到点自己找一个人开口")
        win.geometry("620x460")
        win.minsize(520, 380)
        win.transient(self.root)

        tip = tk.StringVar(value="")
        ttk.Label(win, textvariable=tip, foreground="#888").pack(
            anchor="w", padx=12, pady=(10, 4))

        listf = ttk.Frame(win)
        listf.pack(fill="both", expand=True, padx=12)
        listf.columnconfigure(1, weight=0)
        listf.columnconfigure(2, weight=1)

        head = ttk.Frame(listf)
        head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        ttk.Label(head, text="开", width=3).pack(side="left")
        ttk.Label(head, text="时刻", width=7).pack(side="left", padx=(4, 0))
        ttk.Label(head, text="内容 (一句话说清要聊什么)").pack(side="left",
                                                            padx=(4, 0))

        def refresh_tip():
            nxt = schedule.next_times(3)
            tip.set(f"共 {len(schedule.load())} 个任务。下一批: "
                    + (", ".join(f"{t} {c}" for t, c in nxt) if nxt else "无"))

        def refresh():
            for w in listf.winfo_children():
                if w is not head:
                    w.destroy()
            for r, it in enumerate(schedule.load(), 1):
                sid = it["id"]
                on = tk.BooleanVar(value=it.get("enabled", True))
                tv = tk.StringVar(value=it.get("time", ""))
                cv = tk.StringVar(value=it.get("content", ""))

                def commit(sid=sid, tv=tv, cv=cv, on=on):
                    items = schedule.load()
                    for x in items:
                        if x["id"] == sid:
                            x["time"] = tv.get().strip()
                            x["content"] = cv.get()
                            x["enabled"] = bool(on.get())
                    schedule.save(items)
                    refresh_tip()

                ttk.Checkbutton(listf, variable=on,
                                command=commit).grid(row=r, column=0,
                                                     sticky="w")
                e1 = ttk.Entry(listf, textvariable=tv, width=8)
                e1.grid(row=r, column=1, sticky="w", padx=(4, 0))
                e1.bind("<FocusOut>", lambda ev: commit())
                e1.bind("<Return>", lambda ev: commit())
                e2 = ttk.Entry(listf, textvariable=cv)
                e2.grid(row=r, column=2, sticky="ew", padx=(4, 0))
                e2.bind("<FocusOut>", lambda ev: commit())
                ttk.Button(listf, text="删", width=4,
                           command=lambda s=sid: self._del_schedule(s, refresh)
                           ).grid(row=r, column=3, padx=(6, 0))

        def add():
            tv = tk.StringVar()
            cv = tk.StringVar()
            row = ttk.Frame(win)
            row.pack(fill="x", padx=12, pady=(8, 0))
            ttk.Label(row, text="新增").pack(side="left")
            e1 = ttk.Entry(row, textvariable=tv, width=8)
            e1.pack(side="left", padx=(4, 0))
            e1.insert(0, datetime.now().strftime("%H:%M"))

            def do_add(cv=cv, tv=tv):
                try:
                    schedule.add(tv.get(), cv.get())
                except ValueError as ex:
                    messagebox.showwarning("时间不对", str(ex), parent=win)
                    return
                cv.set("")
                refresh()

            e2 = ttk.Entry(row, textvariable=cv)
            e2.pack(side="left", fill="x", expand=True, padx=4)
            e2.bind("<Return>", lambda ev: do_add())
            ttk.Button(row, text="添加", width=6,
                       command=do_add).pack(side="left")

        refresh()
        add()
        refresh_tip()

    def _del_schedule(self, sid, refresh):
        import schedule
        schedule.remove(sid)
        refresh()

    def _do_restore(self):
        import wechat_ui
        ok, msg = wechat_ui.restore_window()
        self._post_log(f"[窗口] 已还原: {msg}")

    def _start(self):
        sel = self._selected()
        if not sel:
            messagebox.showwarning("提示", "请先勾选至少一个联系人")
            return
        self._push_mode()
        self.engine.proactive_enabled = self.proactive_var.get()
        self.engine.schedule_enabled = self.schedule_var.get()
        self.engine.quiet_send = True
        try:
            # 以 Spinbox 自身显示值为准, 不信任变量, 避免被截断/回绕成别的值
            shown = self.pro_spin.get()
            mins = int(float(shown))
        except (ValueError, tk.TclError):
            mins = config.PROACTIVE_INTERVAL_MIN
            self.pro_min_var.set(str(mins))
        # 硬下限 30 分钟。主动聊天间隔太短会像刷屏, 直接挡掉。
        mins = max(30, mins)
        self.pro_min_var.set(str(mins))
        self.engine.proactive_interval_min = mins
        if self.proactive_var.get():
            n = len(sel)
            if not messagebox.askyesno(
                    "确认定时主动聊天",
                    f"将每隔 {mins} 分钟，"
                    f"轮流向 {n} 个白名单联系人主动发一条消息。\n\n"
                    f"对方没回你也会主动开口，可能显得唐突或像骚扰。\n"
                    f"确定要开启吗？"):
                self.engine.proactive_enabled = False
                self.proactive_var.set(False)
        if self.full_auto_var.get():
            n = len(sel)
            if not messagebox.askyesno(
                    "确认全自动",
                    f"将对 {n} 个白名单联系人自动发送真实消息，\n"
                    f"涉及钱/见面/承诺的敏感话题也不再问你。\n\n"
                    f"确定要开始吗？"):
                return
        if self.engine.start(sel):
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")

    def _stop(self):
        self.engine.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _mark_done(self, tag):
        sel = self.pending_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self._pending):
            p = self._pending.pop(idx)
            self._post_log(f"[{tag}] {p['nick']}: {p['reply'][:40]}")
        self._post_pending(self._pending)

    def _reply_all(self):
        """给所有勾选的人**分别**生成回复。每个人的内容基于各自历史。"""
        sel = self._selected()
        if not sel:
            messagebox.showwarning("提示", "先勾选要回复的联系人")
            return
        if not messagebox.askyesno(
                "逐人回复",
                f"将对 {len(sel)} 个勾选的联系人分别生成一条回复。\n\n"
                f"每个人用的是自己那份聊天记录，内容互不相同。\n"
                f"没有历史又没配话题的会跳过。\n\n"
                f"当前模式: {self._mode_name()}\n"
                f"确定开始吗？"):
            return
        self.btn_all.configure(state="disabled")
        self._post_status(f"逐人回复中 (0/{len(sel)})")

        def work():
            def prog(i, total, nick):
                self._post_status(f"逐人回复中 ({i}/{total}) 当前: {nick}")

            res = self.engine.reply_all(sel, on_progress=prog)
            okc = sum(1 for r in res if r[2])
            skip = sum(1 for r in res if not r[2] and r[3] in ("无素材", "模型返回空"))
            self._post_log(f"[逐人回复] 完成: 成功 {okc} / 跳过 {skip} / "
                           f"共 {len(res)}")
            self._post_status(f"逐人回复完成: 成功 {okc}")

            def done():
                self.btn_all.configure(state="normal")

            self._q.put(("ui_call", done))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 人工确认发送 (可编辑) ----------

    def _open_send_dialog(self):
        # 没选中也允许打开 = 自由输入模式。
        # 选了就是"在 AI 草稿基础上改", 没选就是空白自己写。
        sel = self.pending_list.curselection()
        item = None
        pid = None
        if sel and sel[0] < len(self._pending):
            item = dict(self._pending[sel[0]])   # 快照, 防列表刷新后变化
            pid = item.get("pid", sel[0])         # 稳定ID, 不是索引

        win = tk.Toplevel(self.root)
        win.title("修改并发送" if item else "写消息并发送")
        win.geometry("660x520")
        win.minsize(540, 440)
        win.transient(self.root)
        win.grab_set()

        head = ttk.Frame(win, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        ttk.Label(head, text="发给", font=self.font_body).pack(anchor="w")
        # 收件人可改可自由输入: 下拉给白名单, 但也能直接打字填别人
        who_var = tk.StringVar(
            value=item["nick"] if item else "")
        who_box = ttk.Combobox(head, textvariable=who_var,
                               values=list(self.engine.whitelist),
                               font=self.font_body)
        who_box.pack(fill="x", ipady=4, pady=(3, 0))
        if item:
            ttk.Label(head, text=f"对方原话: {item['text'][:60]}",
                      foreground="#999").pack(anchor="w", pady=(4, 0))

        ttk.Label(win, text="要发的话 (可自由修改):").pack(anchor="w", padx=12,
                                                       pady=(8, 2))
        body = ttk.Frame(win, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, wrap="word", height=7, **self.tk_text_kw)
        txt.pack(fill="both", expand=True)
        original = item["reply"] if item else ""
        txt.insert("1.0", original)
        txt.focus_set()
        if original:
            txt.tag_add("sel", "1.0", "end-1c")

        hint = tk.StringVar(value="")
        ttk.Label(win, textvariable=hint, foreground="#888").pack(
            anchor="w", padx=12, pady=(2, 0))
        info = tk.StringVar(value="")
        ttk.Label(win, textvariable=info, foreground="#c00").pack(
            anchor="w", padx=12)

        def on_edit(_evt=None):
            cur = txt.get("1.0", "end").strip()
            n = len(cur)
            if not original:
                hint.set(f"字数 {n}" if n else "空白输入, 直接打字后点发送")
            else:
                hint.set(f"字数 {n}"
                         + ("  (已修改)" if cur != original.strip() else ""))

        txt.bind("<KeyRelease>", on_edit)
        on_edit()

        def do_send():
            content = txt.get("1.0", "end").strip()
            target = who_var.get().strip()
            if not target:
                info.set("收件人不能为空")
                who_box.focus_set()
                return
            if not content:
                info.set("内容为空, 不能发")
                txt.focus_set()
                return
            btn_send.configure(state="disabled", text="发送中...")
            info.set("")

            def work():
                ok, msg = self.engine.send_manual(target, content)
                self._post_log(f"[{'已发' if ok else '发送失败'}] "
                               f"{target}: {content[:40]} ({msg})")
                # 不能在工作线程里调 win.after —— tkinter 不是线程安全的,
                # 和之前 _load_chats 在子线程建控件是同一类问题。
                # 统一丢回队列, 由 _pump 在主线程执行。
                # target/content 要传给 finish, 它俩是 do_send 的局部变量。
                self._q.put(("ui_call", lambda: finish(ok, msg, target)))

            threading.Thread(target=work, daemon=True).start()

        def finish(ok, msg, target=""):
            if ok:
                if pid is not None:
                    self._drop_pending(pid)
                win.destroy()
                # 不再弹「已发送」小框: 自动回复时它会突然抢焦点,
                # 用户明确要求全静默。成功与否看日志区那一行即可。
            else:
                btn_send.configure(state="normal", text="发送")
                info.set(f"失败: {msg}")

        def do_discard():
            # 自由输入模式下没有对应待办条目, 只是关窗
            if pid is not None:
                self._drop_pending(pid)
            win.destroy()

        bar = ttk.Frame(win, padding=12)
        bar.pack(fill="x")
        btn_send = ttk.Button(bar, text="发送", command=do_send)
        btn_send.pack(side="right")
        ttk.Button(bar, text="丢弃", command=do_discard).pack(side="right",
                                                            padx=6)
        self.btn_all = ttk.Button(bar, text="逐人回复全部",
                                  command=self._reply_all)
        self.btn_all.pack(side="left", padx=(6, 0))

    def _drop_pending(self, pid):
        """按稳定ID删除。用索引会因列表刷新而删错条目。"""
        for i, p in enumerate(self._pending):
            if p.get("pid") == pid:
                self._pending.pop(i)
                break
        self._post_pending(list(self._pending))

    # ---------- 线程回调 (走队列, 由 _pump 在主线程消费) ----------

    def _post_log(self, msg):
        self._q.put(("log", msg))

    def _post_pending(self, items):
        self._q.put(("pending", items))

    def _post_status(self, s):
        self._q.put(("status", s))

    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    if payload == "__ENABLE_HIST__":
                        self.btn_hist.configure(state="normal")
                        continue
                    self._log_lines.append(payload)
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    if len(self._log_lines) > 500:
                        del self._log_lines[:100]
                        self.log_text.delete("1.0", "50.0")
                elif kind == "pending":
                    self._pending = payload
                    self.pending_list.delete(0, "end")
                    for p in payload:
                        self.pending_list.insert(
                            "end", f"{p['nick']} | 原文: {p['text'][:28]} | "
                                    f"拟回复: {p['reply'][:40]}")
                elif kind == "ui_call":
                    try:
                        payload()
                    except tk.TclError:
                        pass          # 窗口已销毁
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "chats":
                    # 控件只能在主线程建
                    self._build_chat_widgets(payload)
                elif kind == "chats_error":
                    self._log_lines.append(f"[错误] 读取会话失败: {payload}")
                    self.log_text.insert("end",
                                         f"[错误] 读取会话失败: {payload}\n")
                    self._post_status("读取会话失败")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._pump)


def main():
    root = tk.Tk()

    # Tk 默认会把 command 回调里的异常丢到 stderr, 界面上什么也不显示,
    # 表现就是「按钮点了没反应」。这里改成冒泡到日志, 并弹窗告知。
    def on_callback_error(exc_type, exc, tb):
        import traceback
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        print(detail, file=sys.stderr)
        try:
            App.last_error = f"{exc_type.__name__}: {exc}"
            messagebox.showerror(
                "出错了",
                f"{exc_type.__name__}: {exc}\n\n"
                f"详细信息已打印到控制台。\n常见原因: "
                f"刚改过某个方法名但没同步调用处。")
        except Exception:
            pass

    root.report_callback_exception = on_callback_error

    app = App(root)
    app.root.mainloop()


if __name__ == "__main__":
    main()