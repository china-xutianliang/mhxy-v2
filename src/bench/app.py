"""ControlBench：控制层测试台（主控机 A，管理员运行，32 位 Python）。

【已退役，不再维护】这是 816x616 边带时代的旧测试台，目标尺寸和抓图尺度仍按"系统缩放 × 816x616"计算，
与第一层 v1.6（查看器 -scale=100，画面子窗口恒 800x600 物理像素，V = G）不兼容；连接、启动、绑定三个入口
已拒绝执行。后台鼠键测试请用 src/kmtool。

界面分两档：
  - 新手只看"开始"页：第 1 步连接副机，第 2 步一键自检，结果用绿灯/红灯列出来，
    红灯旁边的"怎么办"栏直接写下一步该做什么。
  - 需要细节时去"画面 / 输入 / 高级 / 日志"，这些不挡主流程。

安全规则（来自之前的锁屏事故）：
  - 除"遮挡测试"的一个小窗外，不创建全屏/置顶窗口；遮挡窗点击或按键即关，30 秒自动消失。
  - 全局急停 Ctrl+Alt+Q（或界面按钮）：中止所有动作并抬起所有修饰键/鼠标键。
  - 所有耗时操作都在主线程里用 pump_sleep 分片等待，界面不会假死，大漠对象只在主线程使用。
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import estop, winapi  # noqa: E402
from common import log as logmod  # noqa: E402
from common.config import runtime_dir  # noqa: E402
from common.inspector import InspectorFrame  # noqa: E402
from common.env_check import fix_viewer_dpi, run_checks  # noqa: E402

winapi.set_dpi_aware()  # 必须早于 Tk()

from control import guard, viewer_link  # noqa: E402
from control.actuator import Actuator  # noqa: E402
from control.channel import Channel  # noqa: E402
from control.viewport import Viewport  # noqa: E402

MODE_SHARED = "共享游戏区域（800x600）"
MODE_DESKTOP = "整桌面（调试用）"
TARGET_SHARED = (816, 616)
GAME_W, GAME_H = 800, 600
RETIRED_TEXT = "旧测试台已退役，不适用于第一层 v1.6（画面子窗口恒 800x600），不能连接或绑定。请改用 kmtool。"

COLOR_OK, COLOR_BAD, COLOR_WARN, COLOR_INFO = "#1e8449", "#c0392b", "#b9770e", "#4a4a4a"


class Bench:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ControlBench  控制层测试台（已退役，请用 kmtool）")
        self.ui = max(1.0, winapi.user32.GetDpiForSystem() / 96.0) if hasattr(winapi.user32, "GetDpiForSystem") else 1.0
        self.root.tk.call("tk", "scaling", self.ui * 96 / 72)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(int(1180 * self.ui), int(sw * 0.94)), min(int(840 * self.ui), int(sh * 0.92))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(min(int(900 * self.ui), w), min(int(600 * self.ui), h))

        self._setup_fonts()
        self._setup_style()

        self.dm = None
        self.channel: Channel | None = None
        self.handles: viewer_link.ViewerHandles | None = None
        self.viewport = Viewport(input_scale=1.0, capture_scale=self.ui)
        self.actuator: Actuator | None = None
        self.busy = False
        self.photo = None
        self.scale_n = 1
        self.sel_start = None
        self.sel_rect = None
        self.frame_path = os.path.join(runtime_dir(), "frame.png")
        self.diag: list[str] = []

        self.v_mode = tk.StringVar(value=MODE_DESKTOP)
        self.v_banner = tk.StringVar(value=RETIRED_TEXT)
        self._build_banner()
        self.set_banner(RETIRED_TEXT, "bad")
        self._build_tabs()
        self.root.after(200, self._poll_estop)
        estop.start_hotkey()
        logmod.add_listener(self._on_log_line)
        logmod.log("测试台启动")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 外观
    def _setup_fonts(self) -> None:
        fam = "Microsoft YaHei UI"
        try:
            if fam not in tkfont.families(self.root):
                fam = tkfont.nametofont("TkDefaultFont").cget("family")
        except tk.TclError:
            fam = "Segoe UI"
        for name, size, weight in (("TkDefaultFont", 11, "normal"), ("TkTextFont", 11, "normal"),
                                   ("TkMenuFont", 11, "normal"), ("TkHeadingFont", 12, "bold"),
                                   ("TkCaptionFont", 12, "bold"), ("TkIconFont", 11, "normal"),
                                   ("TkTooltipFont", 10, "normal"), ("TkSmallCaptionFont", 10, "normal")):
            try:
                tkfont.nametofont(name).configure(family=fam, size=size, weight=weight)
            except tk.TclError:
                pass
        self.f_base = (fam, 11)
        self.f_small = (fam, 10)
        self.f_bold = (fam, 11, "bold")
        self.f_step = (fam, 14, "bold")
        self.f_banner = (fam, 13, "bold")
        self.f_huge = (fam, 15, "bold")

    def _setup_style(self) -> None:
        st = ttk.Style(self.root)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        # 之前列表文字上下挤在一起，就是因为行高没跟着字号放大
        st.configure("Treeview", rowheight=int(line * 1.9))
        st.configure("Treeview.Heading", font=self.f_bold, padding=(4, 6))
        st.configure("TButton", padding=(10, 7))
        st.configure("TNotebook.Tab", padding=(18, 9), font=self.f_bold)
        st.configure("TLabelframe.Label", font=self.f_bold)
        st.configure("TCheckbutton", padding=(2, 4))
        self.root.option_add("*TCombobox*Listbox.font", self.f_base)

    def _big_button(self, parent, text, command, bg="#2d6cdf") -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=self.f_huge, bg=bg, fg="white",
                         activebackground="#1b4fa8", activeforeground="white", relief="flat",
                         cursor="hand2", padx=int(18 * self.ui), pady=int(10 * self.ui))

    def _build_banner(self) -> None:
        bar = tk.Frame(self.root, bg=COLOR_INFO)
        bar.pack(fill="x")
        self.banner = tk.Label(bar, textvariable=self.v_banner, font=self.f_banner, bg=COLOR_INFO, fg="white",
                               anchor="w", justify="left", wraplength=int(900 * self.ui), padx=int(14 * self.ui),
                               pady=int(10 * self.ui))
        self.banner.pack(side="left", fill="x", expand=True)
        self.estop_btn = tk.Button(bar, text="急停\nCtrl+Alt+Q", command=self.on_estop_click, font=self.f_bold,
                                   bg="#d9534f", fg="white", relief="flat", padx=int(16 * self.ui),
                                   pady=int(6 * self.ui), cursor="hand2")
        self.estop_btn.pack(side="right", padx=int(10 * self.ui), pady=int(6 * self.ui))

    def set_banner(self, text: str, level: str = "info") -> None:
        color = {"ok": COLOR_OK, "bad": COLOR_BAD, "warn": COLOR_WARN}.get(level, COLOR_INFO)
        self.v_banner.set(text)
        self.banner.config(bg=color)
        self.banner.master.config(bg=color)

    # ------------------------------------------------------------------ 基础
    def pump_sleep(self, sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.root.update()
            time.sleep(min(0.03, max(0.0, end - time.monotonic())))

    def run(self, fn, *args) -> None:
        """串行执行一个测试动作；执行期间忽略其他按钮；任何异常显示在日志里而不是弹死界面。"""
        if self.busy:
            logmod.log("上一个操作还没结束，已忽略本次点击")
            return
        self.busy = True
        try:
            fn(*args)
        except estop.Aborted:
            logmod.log("操作被急停中止")
            self.set_banner("操作被急停中止。点右上角按钮可以恢复。", "warn")
        except Exception as e:  # noqa: BLE001
            logmod.log(f"操作失败：{type(e).__name__}: {e}")
            self.set_banner(f"操作失败：{e}（详情见“日志”页）", "bad")
        finally:
            self.busy = False

    def need_bound(self) -> bool:
        if not (self.channel and self.channel.bound and self.handles and self.handles.alive()):
            logmod.log("还没有连接副机，请先在“开始”页做第 1 步")
            self.set_banner("还没连接副机。先做第 1 步。", "warn")
            return False
        return True

    def _mode_text(self) -> str:
        w, h = self.viewport.game_size
        if self.v_mode.get() == MODE_SHARED:
            return "只共享游戏区域 800x600"
        return f"整桌面 {w}x{h}（备用模式）"

    def set_status(self) -> None:
        if self.handles and self.handles.alive():
            cw, ch = winapi.client_size(self.handles.child)
            self.v_view_info.set(f"查看器画面 {cw}x{ch} · {self._mode_text()}")
        else:
            self.v_view_info.set("未连接")

    # ------------------------------------------------------------------ 急停
    def on_estop_click(self) -> None:
        if estop.is_set():
            estop.clear()
            logmod.log("已恢复，可继续测试")
            self.set_banner("已恢复，可以继续。", "info")
        else:
            estop.trigger("界面按钮")

    def _poll_estop(self) -> None:
        if estop.is_set():
            self.estop_btn.config(text="已急停\n点此恢复", bg="#f0ad4e")
            if self.actuator:
                self.actuator.release_modifiers()
        else:
            self.estop_btn.config(text="急停\nCtrl+Alt+Q", bg="#d9534f")
        self.root.after(200, self._poll_estop)

    # ------------------------------------------------------------------ 分页
    def _build_tabs(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=int(8 * self.ui), pady=int(8 * self.ui))
        self.nb = nb
        self.tab_home = ttk.Frame(nb, padding=12)
        self.tab_view = ttk.Frame(nb, padding=10)
        self.tab_input = ttk.Frame(nb, padding=10)
        self.tab_adv = ttk.Frame(nb, padding=8)
        self.tab_log = ttk.Frame(nb, padding=8)
        for t, name in ((self.tab_home, "开始"), (self.tab_view, "画面"), (self.tab_input, "输入"),
                        (self.tab_adv, "高级"), (self.tab_log, "日志")):
            nb.add(t, text=name)
        self._build_home()
        self._build_view()
        self._build_input()
        self._build_adv()
        self._build_log()

    # ================================================================ 开始页
    def _build_home(self) -> None:
        f = self.tab_home
        self.v_host = tk.StringVar(value="100.64.0.10")
        self.v_port = tk.StringVar(value="5900")
        self.v_pw = tk.StringVar()
        self.v_title = tk.StringVar()
        self.v_tw = tk.StringVar(value=str(round(TARGET_SHARED[0] * self.ui)))
        self.v_th = tk.StringVar(value=str(round(TARGET_SHARED[1] * self.ui)))
        self.v_origin_x = tk.StringVar(value="0")
        self.v_origin_y = tk.StringVar(value="0")
        self.v_view_info = tk.StringVar(value="未连接")
        self.v_click_test = tk.BooleanVar(value=False)

        # 第 1 步
        s1 = ttk.LabelFrame(f, text="第 1 步　连接副机", padding=12)
        s1.pack(fill="x")
        ttk.Label(s1, text="填副机的 Tailscale 地址和 VNC 密码，点右边的按钮。程序会自己打开查看器并连上。",
                  font=self.f_small, foreground="#555555").pack(anchor="w")
        r1 = ttk.Frame(s1)
        r1.pack(fill="x", pady=(10, 0))
        ttk.Label(r1, text="副机地址").pack(side="left")
        ttk.Entry(r1, textvariable=self.v_host, width=18, font=self.f_base).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="端口").pack(side="left")
        ttk.Entry(r1, textvariable=self.v_port, width=7, font=self.f_base).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="VNC 密码").pack(side="left")
        ttk.Entry(r1, textvariable=self.v_pw, show="*", width=18, font=self.f_base).pack(side="left", padx=(4, 14))
        self._big_button(r1, "连接副机", lambda: self.run(self.do_connect)).pack(side="left")
        self.conn_state = ttk.Label(s1, text="状态：未连接", font=self.f_bold, foreground=COLOR_BAD)
        self.conn_state.pack(anchor="w", pady=(8, 0))

        # 第 2 步
        s2 = ttk.LabelFrame(f, text="第 2 步　一键自检", padding=12)
        s2.pack(fill="x", pady=(12, 0))
        ttk.Label(s2, text="依次检查：运行环境、大漠注册、能不能连上画面、画面是不是 800x600、画面在不在动。"
                           "全部绿灯就可以开工。", font=self.f_small, foreground="#555555",
                  wraplength=int(820 * self.ui), justify="left").pack(anchor="w")
        r2 = ttk.Frame(s2)
        r2.pack(fill="x", pady=(10, 0))
        self._big_button(r2, "开始自检", lambda: self.run(self.do_selfcheck), bg="#1e8449").pack(side="left")
        ttk.Checkbutton(r2, text="同时做点击测试（会在游戏里点一下画面中心，默认关）",
                        variable=self.v_click_test).pack(side="left", padx=(16, 0))
        ttk.Button(r2, text="复制诊断信息", command=self.copy_diag).pack(side="right")

        # 结果表
        s3 = ttk.LabelFrame(f, text="自检结果", padding=8)
        s3.pack(fill="both", expand=True, pady=(12, 0))
        cols = ("step", "res", "detail", "hint")
        self.sc_tree = ttk.Treeview(s3, columns=cols, show="headings", height=8)
        for c, text, width in (("step", "检查项", 190), ("res", "结果", 80), ("detail", "说明", 380), ("hint", "不通过时怎么办", 420)):
            self.sc_tree.heading(c, text=text)
            self.sc_tree.column(c, width=int(width * self.ui), anchor="w", stretch=(c in ("detail", "hint")))
        self.sc_tree.tag_configure("ok", foreground=COLOR_OK)
        self.sc_tree.tag_configure("bad", foreground=COLOR_BAD)
        self.sc_tree.tag_configure("warn", foreground=COLOR_WARN)
        sb = ttk.Scrollbar(s3, orient="vertical", command=self.sc_tree.yview)
        self.sc_tree.configure(yscrollcommand=sb.set)
        self.sc_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(f, text="绿灯=正常；红灯=看最右边一列照做；黄灯=不影响使用但要留意。",
                  font=self.f_small, foreground="#555555").pack(anchor="w", pady=(8, 0))

    # ---- 自检行 ----
    def _sc_row(self, step: str, status: str, detail: str, hint: str = "") -> None:
        label = {"ok": "通过", "bad": "不通过", "warn": "注意"}[status]
        self.sc_tree.insert("", "end", values=(step, label, detail, hint), tags=(status,))
        self.diag.append(f"{step}\t{label}\t{detail}\t{hint}")
        self.root.update_idletasks()

    def copy_diag(self) -> None:
        text = "\n".join(self.diag) if self.diag else "还没有自检记录。"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        logmod.log("诊断信息已复制到剪贴板")
        self.set_banner("诊断信息已复制到剪贴板，可直接粘贴发给别人看。", "info")

    # ---- 第 1 步：连接 ----
    def _retired(self) -> bool:
        self.set_banner(RETIRED_TEXT, "bad")
        logmod.log(RETIRED_TEXT)
        return True

    def do_connect(self) -> None:
        if self._retired():
            return
        host = self.v_host.get().strip()
        port = int(self.v_port.get())
        pw = self.v_pw.get()
        self.conn_state.config(text="状态：正在打开查看器……", foreground=COLOR_WARN)
        self.set_banner(f"正在连接 {host}:{port} ……", "info")
        existed = viewer_link.find_viewers(host)
        if not existed:
            proc = viewer_link.launch_viewer(host, port, pw)
            ok = viewer_link.confirm_unencrypted(proc.pid, 12)
            logmod.log("已点掉未加密确认框" if ok else "没出现确认框，可能已直接连上")
            # 等窗口出现并抓到画面
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not viewer_link.find_viewers(host):
                self.pump_sleep(0.5)
        h = self._bind_viewer(host)
        if not h:
            self.conn_state.config(text="状态：没连上", foreground=COLOR_BAD)
            self.set_banner("没连上副机。看下面“自检结果”里“找到查看器”那一行的提示。", "bad")
            self._sc_row("找到查看器", "bad", f"标题里含 {host} 的查看器窗口不存在",
                         "确认副机开着、TightVNC 服务端在运行、地址和密码正确；也可以手动打开查看器后重试")
            return
        res = self._detect_mode()
        self.conn_state.config(text=f"状态：已连接（{self._mode_text()}）", foreground=COLOR_OK)
        if res.mode == "shared":
            self.set_banner("已连接副机，画面就是 800x600 的游戏画面。可以做第 2 步了。", "ok")
        else:
            self.set_banner("已连接副机，但副机还在共享整个桌面。想只共享游戏区域，先在副机装一次 "
                            "slave_pkg（每台装一次就够）。现在也能用，只是坐标要加偏移。", "warn")
        self.do_grab()

    def _bind_viewer(self, keyword: str | None) -> viewer_link.ViewerHandles | None:
        if self.dm is None:
            try:
                self.do_init_dm()
            except Exception as e:  # noqa: BLE001
                logmod.log(f"大漠注册失败：{e}")
                self._sc_row("大漠注册", "bad", str(e), "用管理员身份重新运行本程序")
                return None
        vs = viewer_link.find_viewers(keyword or None)
        if not vs:
            return None
        if len(vs) > 1:
            logmod.log(f"找到 {len(vs)} 个查看器，用第一个：{winapi.title(vs[0].top)}")
        self.handles = vs[0]
        viewer_link.ensure_restored(self.handles)
        self.channel = Channel(self.dm)
        self.channel.bind(self.handles.child)
        self.apply_mode()
        self.set_status()
        self.show_info()
        logmod.log(f"已绑定查看器画面子窗口 {self.handles.child}")
        return self.handles

    def _detect_mode(self) -> guard.ModeResult:
        target = (round(TARGET_SHARED[0] * self.ui), round(TARGET_SHARED[1] * self.ui))
        res = guard.detect_share_mode(self.channel, self.handles, target, (GAME_W, GAME_H),
                                     viewer_link.fit_window, sleep=self.pump_sleep, scale=self.ui)
        self.v_mode.set(MODE_SHARED if res.mode == "shared" else MODE_DESKTOP)
        self.apply_mode()
        self.set_status()
        self.show_info()
        self._guard_out(res.l0)
        return res

    def do_detect_mode(self) -> None:
        if not self.need_bound():
            return
        logmod.log("正在检测共享模式：把查看器调到 816x616，最多等 10 秒……")
        self.set_banner("正在检测画面尺寸，最多等 10 秒……", "info")
        res = self._detect_mode()
        self.set_banner(("检测完成：" + res.reason), "ok" if res.mode == "shared" else "warn")

    # ---- 第 2 步：自检 ----
    def do_selfcheck(self) -> None:
        self.diag = []
        self.sc_tree.delete(*self.sc_tree.get_children())
        self.set_banner("正在自检，请不要动鼠标键盘……", "info")
        fails = 0

        # 1) 运行环境
        env_bad = []
        for c in run_checks():
            self._sc_row(c.name, "ok" if c.ok else "bad", c.detail, "" if c.ok else c.hint)
            if not c.ok:
                env_bad.append(c.name)
        fails += len(env_bad)

        # 2) 大漠注册
        if self.dm is None:
            try:
                self.do_init_dm()
                self._sc_row("大漠注册", "ok", f"版本 {self.dm.Ver()}", "")
            except Exception as e:  # noqa: BLE001
                self._sc_row("大漠注册", "bad", f"{type(e).__name__}: {e}", "用管理员身份重新运行本程序（右键 → 以管理员身份运行）")
                fails += 1
        else:
            self._sc_row("大漠注册", "ok", f"已注册，版本 {self.dm.Ver()}", "")
        if self.dm is None:
            self._finish(fails)
            return

        # 3) 绑定查看器
        if not (self.channel and self.channel.bound and self.handles and self.handles.alive()):
            kw = self.v_host.get().strip() or None
            if self._bind_viewer(kw) is None:
                self._sc_row("找到查看器", "bad", "没有可绑定的查看器窗口",
                             "先做第 1 步连接副机；或手动打开 TightVNC 查看器后重试")
                self._finish(fails + 1)
                return
        self._sc_row("找到查看器", "ok", f"画面子窗口 {self.handles.child}，顶层 {self.handles.top}", "")
        if estop.is_set():
            self._sc_row("急停状态", "bad", "急停处于按下状态", "点右上角按钮恢复后再自检")
            self._finish(fails + 1)
            return
        self._sc_row("急停状态", "ok", "未触发", "")

        # 4) 画面尺寸 / 共享模式
        res = self._detect_mode()
        if res.mode == "shared":
            self._sc_row("画面尺寸 800x600", "ok", res.l0.detail, "")
        else:
            self._sc_row("画面尺寸 800x600", "warn",
                         res.l0.detail + "，已退回整桌面模式",
                         "副机上运行过 slave_pkg 里的 install.bat 后，游戏打开就会自动只共享游戏区域")

        # 5) 抓图
        cw, ch = winapi.client_size(self.handles.child)
        if not self.channel.capture_png(0, 0, cw, ch, self.frame_path):
            self._sc_row("能否抓图", "bad", "抓图失败", "查看器是否被最小化；点“画面”页的“抓一帧”再试")
            fails += 1
        else:
            self._sc_row("能否抓图", "ok", f"抓到 {cw}x{ch}", "")
            self.do_grab()

        # 6) 画面在不在动
        step = max(20, cw // 40, ch // 40)
        a = guard.grid_signature(self.channel, (0, 0, cw, ch), step)
        self.pump_sleep(2.5)
        b = guard.grid_signature(self.channel, (0, 0, cw, ch), step)
        diff = sum(1 for x, y in zip(a, b) if x != y)
        if diff:
            self._sc_row("画面在动", "ok", f"2.5 秒内 {diff}/{len(a)} 个取样点变化", "")
        else:
            self._sc_row("画面在动", "warn", "2.5 秒内画面完全没变",
                         "游戏可能停在静止画面；进游戏走两步再看。若一直不动，画面可能是卡住的")

        # 7) 可选点击测试
        if self.v_click_test.get():
            self._click_test()

        self._finish(fails)

    def _click_test(self) -> None:
        """在画面中心点一下，看周边画面有没有反应。只点一次，不连点。"""
        if not self.actuator:
            self._sc_row("点击测试", "bad", "还没有可用的点击通道", "先完成前面的步骤")
            return
        cw, ch = winapi.client_size(self.handles.child)
        region = (max(0, cw // 2 - 120), max(0, ch // 2 - 120), min(cw, cw // 2 + 120), min(ch, ch // 2 + 120))
        before = guard.grid_signature(self.channel, region, 20)
        self.actuator.click(GAME_W // 2, GAME_H // 2, "left")
        ok = self.actuator.wait_until(lambda: guard.grid_signature(self.channel, region, 20) != before, 8.0, 0.3)
        if ok:
            self._sc_row("点击测试", "ok", "点击后画面有反应，输入通了", "")
        else:
            self._sc_row("点击测试", "warn", "点击后 8 秒内画面没变化",
                         "可能这处地面不能点；换“输入”页手动点一个按钮试试")

    def _finish(self, fails: int) -> None:
        if fails:
            self.set_banner(f"自检完成：{fails} 项不通过。看“自检结果”最右边一列照着做。", "bad")
        else:
            self.set_banner("自检完成：全部通过，可以开工。", "ok")
        self.conn_state.config(text=f"状态：已连接（{self._mode_text()}）" if self.handles else "状态：未连接",
                               foreground=COLOR_OK if self.handles else COLOR_BAD)

    # ================================================================ 画面页
    def _build_view(self) -> None:
        f = self.tab_view
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self._big_button(bar, "刷新画面", lambda: self.run(self.do_grab), bg="#2d6cdf").pack(side="left")
        self.v_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="每秒自动刷新", variable=self.v_auto, command=self._auto_tick).pack(side="left", padx=(16, 0))
        self.v_click_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="在画面上点击 = 真点游戏（勾上才生效）",
                        variable=self.v_click_on).pack(side="left", padx=(16, 0))
        self.v_right = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="右键", variable=self.v_right).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="保存截图", command=lambda: self.run(self.do_save_shot)).pack(side="right")
        ttk.Button(bar, text="遮挡测试", command=lambda: self.run(self.do_occlude)).pack(side="right", padx=6)

        self.v_coord = tk.StringVar(value="把鼠标移到画面上，这里显示游戏坐标；按住拖一个框，坐标自动复制。")
        ttk.Label(f, textvariable=self.v_coord, font=self.f_small).pack(fill="x", pady=6)
        self.v_view_info = getattr(self, "v_view_info", tk.StringVar(value="未连接"))
        ttk.Label(f, textvariable=self.v_view_info, font=self.f_bold, foreground=COLOR_INFO).pack(fill="x", pady=(0, 6))
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg="#202020", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def do_grab(self) -> None:
        if not self.need_bound():
            return
        cw, ch = winapi.client_size(self.handles.child)
        if cw <= 0 or ch <= 0:
            logmod.log("查看器客户区为 0（被最小化了？）")
            self.set_banner("查看器被最小化了。先把它还原（“高级 → 绑定”里有还原按钮）。", "warn")
            return
        if not self.channel.capture_png(0, 0, cw, ch, self.frame_path):
            logmod.log("抓图失败")
            return
        photo = tk.PhotoImage(file=self.frame_path)
        n = 1
        while photo.width() // n > 1400 or photo.height() // n > 700:
            n += 1
        self.scale_n = n
        self.photo = photo.subsample(n, n) if n > 1 else photo
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.sel_rect = None
        self.set_status()

    def _auto_tick(self) -> None:
        if self.v_auto.get():
            if not self.busy:
                self.run(self.do_grab)
            self.root.after(1000, self._auto_tick)

    def _v_coord(self, ev) -> tuple[int, int]:
        return int(ev.x * self.scale_n), int(ev.y * self.scale_n)

    def _on_motion(self, ev) -> None:
        vx, vy = self._v_coord(ev)
        gx, gy = self.viewport.capture_to_g(vx, vy)
        self.v_coord.set(f"游戏坐标 G=({gx},{gy})　画面坐标 V=({vx},{vy})")

    def _on_press(self, ev) -> None:
        self.sel_start = (ev.x, ev.y)

    def _on_drag(self, ev) -> None:
        if self.sel_start:
            if self.sel_rect:
                self.canvas.delete(self.sel_rect)
            x0, y0 = self.sel_start
            self.sel_rect = self.canvas.create_rectangle(x0, y0, ev.x, ev.y, outline="#00ff66", width=2)

    def _on_release(self, ev) -> None:
        if not self.sel_start:
            return
        x0, y0 = self.sel_start
        self.sel_start = None
        if abs(ev.x - x0) < 4 and abs(ev.y - y0) < 4:
            if self.v_click_on.get():
                self.run(self.do_canvas_click, ev.x, ev.y)
            return
        a = self.viewport.capture_to_g(int(min(x0, ev.x) * self.scale_n), int(min(y0, ev.y) * self.scale_n))
        b = self.viewport.capture_to_g(int(max(x0, ev.x) * self.scale_n), int(max(y0, ev.y) * self.scale_n))
        text = f"{{x1: {a[0]}, y1: {a[1]}, x2: {b[0]}, y2: {b[1]}}}"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.v_coord.set(f"已复制区域 {text}　大小 {b[0] - a[0]}x{b[1] - a[1]}")
        logmod.log(f"选区(G) {text}")

    def do_canvas_click(self, cx: int, cy: int) -> None:
        if not self.need_bound():
            return
        vx, vy = int(cx * self.scale_n), int(cy * self.scale_n)
        gx, gy = self.viewport.capture_to_g(vx, vy)
        color = self.channel.get_color(vx, vy)
        btn = "right" if self.v_right.get() else "left"
        self.actuator.click(gx, gy, btn)
        logmod.log(f"后台{'右' if btn == 'right' else '左'}键点击 G=({gx},{gy}) 颜色 {color}")

    def do_save_shot(self) -> None:
        if not self.need_bound():
            return
        path = os.path.join(runtime_dir(), time.strftime("shot_%H%M%S.png"))
        cw, ch = winapi.client_size(self.handles.child)
        ok = self.channel.capture_png(0, 0, cw, ch, path)
        logmod.log(f"截图{'已保存' if ok else '失败'}：{path}")
        self.set_banner(f"截图已保存到 {path}" if ok else "截图失败", "ok" if ok else "bad")

    def do_occlude(self) -> None:
        """在查看器画面中央盖一个小窗，验证被遮挡时抓图/输入仍然有效。点击或按键即关，30 秒自动关。"""
        if not self.need_bound():
            return
        ox, oy = winapi.client_origin_on_screen(self.handles.child)
        cw, ch = winapi.client_size(self.handles.child)
        top = tk.Toplevel(self.root)
        top.title("遮挡测试（点击或按键关闭）")
        w, h = int(360 * self.ui), int(220 * self.ui)
        top.geometry(f"{w}x{h}+{ox + max(0, (cw - w) // 2)}+{oy + max(0, (ch - h) // 2)}")
        top.configure(bg="#00cc66")
        top.attributes("-topmost", True)
        tk.Label(top, text="遮挡测试\n点击或按任意键关闭\n30 秒后自动关闭", bg="#00cc66",
                 font=self.f_bold).pack(expand=True)
        top.bind("<Button>", lambda _e: top.destroy())
        top.bind("<Key>", lambda _e: top.destroy())
        top.after(30000, lambda: top.winfo_exists() and top.destroy())
        top.focus_force()
        logmod.log("遮挡窗已出现。此时刷新画面：画面里不应出现绿色。")

    # ================================================================ 输入页
    def _build_input(self) -> None:
        f = self.tab_input
        g = ttk.LabelFrame(f, text="按坐标点击游戏（坐标按要求换算成游戏坐标）", padding=10)
        g.pack(fill="x")
        self.v_cx, self.v_cy = tk.StringVar(value=str(GAME_W // 2)), tk.StringVar(value=str(GAME_H // 2))
        ttk.Label(g, text="x").pack(side="left")
        ttk.Entry(g, textvariable=self.v_cx, width=6, font=self.f_base).pack(side="left", padx=4)
        ttk.Label(g, text="y").pack(side="left")
        ttk.Entry(g, textvariable=self.v_cy, width=6, font=self.f_base).pack(side="left", padx=4)
        ttk.Button(g, text="左键点这里", command=lambda: self.run(self.do_click, "left")).pack(side="left", padx=6)
        ttk.Button(g, text="右键点这里", command=lambda: self.run(self.do_click, "right")).pack(side="left")

        k = ttk.LabelFrame(f, text="按键", padding=10)
        k.pack(fill="x", pady=10)
        self.v_key = tk.StringVar(value="esc")
        self.v_mod = tk.StringVar(value="")
        ttk.Label(k, text="修饰键(alt/ctrl/shift，可空)").pack(side="left")
        ttk.Entry(k, textvariable=self.v_mod, width=8, font=self.f_base).pack(side="left", padx=4)
        ttk.Label(k, text="按键").pack(side="left")
        ttk.Entry(k, textvariable=self.v_key, width=8, font=self.f_base).pack(side="left", padx=4)
        ttk.Button(k, text="发送", command=lambda: self.run(self.do_key)).pack(side="left", padx=6)
        ttk.Button(k, text="抬起所有修饰键/鼠标键", command=lambda: self.run(self.do_release)).pack(side="left", padx=6)

        r = ttk.LabelFrame(f, text="输入回环测试：动作 A → 画面变化 → 动作 B 还原，顺便测出延迟", padding=10)
        r.pack(fill="x")
        self.v_rl_region = tk.StringVar(value="0,0,400,200")
        self.v_rl_a = tk.StringVar(value="右键 400,300")
        self.v_rl_b = tk.StringVar(value="按键 esc")
        for i, (lab, var, w) in enumerate((("取样区(V)", self.v_rl_region, 20), ("动作A", self.v_rl_a, 16), ("动作B", self.v_rl_b, 12))):
            ttk.Label(r, text=lab).grid(row=0, column=i * 2, padx=4)
            ttk.Entry(r, textvariable=var, width=w, font=self.f_base).grid(row=0, column=i * 2 + 1)
        ttk.Button(r, text="运行 ×3", command=lambda: self.run(self.do_loopback)).grid(row=0, column=6, padx=8)
        ttk.Label(r, text="动作写法：左键 x,y / 右键 x,y / 按键 名称。在游戏里请用无害动作（右键空地、Esc 等）。",
                  font=self.f_small, foreground="#555555").grid(row=1, column=0, columnspan=8, sticky="w", pady=6)

        d = ttk.LabelFrame(f, text="act_until 演示：动作 → 轮询确认 → 失败重试", padding=10)
        d.pack(fill="x", pady=10)
        ttk.Label(d, text="用上面回环测试的动作A和取样区作为确认条件（区域变化即算成功）").pack(side="left")
        ttk.Button(d, text="演示", command=lambda: self.run(self.do_act_until)).pack(side="left", padx=8)

    def do_click(self, button: str) -> None:
        if not self.need_bound():
            return
        x, y = int(self.v_cx.get()), int(self.v_cy.get())
        self.actuator.click(x, y, button)
        logmod.log(f"后台点击 G=({x},{y}) {button}")
        self.set_banner(f"已点击游戏坐标 ({x},{y})，看游戏有没有反应。", "info")

    def do_key(self) -> None:
        if not self.need_bound():
            return
        mod, key = self.v_mod.get().strip().lower(), self.v_key.get().strip().lower()
        if mod:
            self.actuator.key_combo(mod, key)
        else:
            self.actuator.key_press(key)
        logmod.log(f"后台按键 {mod + '+' if mod else ''}{key}")

    def do_release(self) -> None:
        if self.actuator:
            self.actuator.release_modifiers()
        logmod.log("已发送：抬起 alt/ctrl/shift 与鼠标左右键")

    def _parse_action(self, text: str):
        kind, _, arg = text.strip().partition(" ")
        if kind in ("左键", "右键"):
            x, y = (int(v) for v in arg.split(","))
            btn = "left" if kind == "左键" else "right"
            return lambda: self.actuator.click(*self.viewport.capture_to_g(x, y), btn)
        if kind == "按键":
            return lambda: self.actuator.key_press(arg.strip().lower())
        raise ValueError(f"看不懂的动作：{text}")

    def _grid(self, region) -> tuple:
        return guard.grid_signature(self.channel, region, 25)

    def do_loopback(self) -> None:
        if not self.need_bound():
            return
        region = tuple(int(x) for x in self.v_rl_region.get().split(","))
        act_a, act_b = self._parse_action(self.v_rl_a.get()), self._parse_action(self.v_rl_b.get())
        times = []
        for i in range(3):
            base = self._grid(region)
            act_a()
            t0 = time.monotonic()
            changed = self.actuator.wait_until(lambda: self._grid(region) != base, 8.0, 0.2)
            dt = time.monotonic() - t0
            act_b()
            back = self.actuator.wait_until(lambda: self._grid(region) == base, 8.0, 0.2)
            logmod.log(f"回环 #{i + 1}: A 后画面{'变化' if changed else '未变化'} {dt * 1000:.0f}ms；"
                       f"B 后{'还原' if back else '未还原'}")
            if changed:
                times.append(dt)
            if not (changed and back):
                logmod.log("回环失败，停止后续轮次（避免状态错乱）")
                break
        if times:
            avg = sum(times) / len(times)
            self.actuator.latency_s = max(0.2, avg)
            logmod.log(f"实测延迟平均 {avg * 1000:.0f}ms，已用于后续轮询与超时")

    def do_act_until(self) -> None:
        if not self.need_bound():
            return
        region = tuple(int(x) for x in self.v_rl_region.get().split(","))
        act_a, act_b = self._parse_action(self.v_rl_a.get()), self._parse_action(self.v_rl_b.get())
        base = self._grid(region)
        ok = self.actuator.act_until(act_a, lambda: self._grid(region) != base)
        logmod.log(f"act_until 结果：{'成功' if ok else '失败（重试用尽）'}  超时={self.actuator.default_timeout():.1f}s "
                   f"轮询={self.actuator.default_poll():.2f}s")
        act_b()

    # ================================================================ 高级
    def _build_adv(self) -> None:
        nb = ttk.Notebook(self.tab_adv)
        nb.pack(fill="both", expand=True)
        self.sub_env = ttk.Frame(nb, padding=10)
        self.sub_bind = ttk.Frame(nb, padding=10)
        self.sub_guard = ttk.Frame(nb, padding=10)
        self.sub_insp = InspectorFrame(nb)
        for t, name in ((self.sub_env, "环境"), (self.sub_bind, "绑定细节"), (self.sub_guard, "校验 L0/L4"),
                        (self.sub_insp, "窗口检查器（本机）")):
            nb.add(t, text=name)
        self.tab_env, self.tab_conn, self.tab_guard = self.sub_env, self.sub_bind, self.sub_guard
        self._build_env()
        self._build_conn()
        self._build_guard()

    # ---- 环境 ----
    def _build_env(self) -> None:
        f = self.tab_env
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="重新检查", command=lambda: self.run(self.do_env)).pack(side="left")
        ttk.Button(btns, text="初始化大漠（注册）", command=lambda: self.run(self.do_init_dm)).pack(side="left", padx=8)
        ttk.Button(btns, text="修复查看器 DPI", command=lambda: self.run(self.do_fix_dpi)).pack(side="left")
        self.env_tree = ttk.Treeview(f, columns=("ok", "detail", "hint"), show="headings", height=14)
        for c, text, width in (("ok", "结果", 70), ("detail", "详情", 360), ("hint", "不通过时怎么办", 420)):
            self.env_tree.heading(c, text=text)
            self.env_tree.column(c, width=int(width * self.ui), anchor="w")
        self.env_tree.tag_configure("bad", foreground=COLOR_BAD)
        self.env_tree.tag_configure("good", foreground=COLOR_OK)
        self.env_tree.pack(fill="both", expand=True, pady=10)
        ttk.Label(f, text="这些检查在“开始”页的一键自检里也会跑一遍。", font=self.f_small,
                  foreground="#555555").pack(anchor="w")

    def do_env(self) -> None:
        self.env_tree.delete(*self.env_tree.get_children())
        for c in run_checks():
            self.env_tree.insert("", "end", values=("通过" if c.ok else "不通过", c.detail, "" if c.ok else c.hint),
                                 tags=("good" if c.ok else "bad",))

    def do_init_dm(self) -> None:
        from control.dm_factory import make_dm
        self.dm = make_dm()
        logmod.log(f"大漠已注册，版本 {self.dm.Ver()}")

    def do_fix_dpi(self) -> None:
        fix_viewer_dpi()
        logmod.log("已写入查看器 DPI 兼容设置，关闭并重新打开查看器后生效")
        self.do_env()

    # ---- 绑定细节 ----
    def _build_conn(self) -> None:
        f = self.tab_conn
        g2 = ttk.LabelFrame(f, text="绑定（正常流程由“开始”页第 1 步自动完成）", padding=10)
        g2.pack(fill="x")
        ttk.Label(g2, text="窗口标题包含").grid(row=0, column=0, padx=4)
        ttk.Entry(g2, textvariable=self.v_title, width=22, font=self.f_base).grid(row=0, column=1)
        ttk.Button(g2, text="查找并绑定", command=lambda: self.run(self.do_bind)).grid(row=0, column=2, padx=8)
        ttk.Button(g2, text="解除绑定", command=lambda: self.run(self.do_unbind)).grid(row=0, column=3)
        ttk.Button(g2, text="自动检测共享模式", command=lambda: self.run(self.do_detect_mode)).grid(row=0, column=4, padx=8)
        ttk.Label(g2, text="目标画面尺寸").grid(row=1, column=0, padx=4, pady=8)
        ttk.Entry(g2, textvariable=self.v_tw, width=6, font=self.f_base).grid(row=1, column=1, sticky="w")
        ttk.Entry(g2, textvariable=self.v_th, width=6, font=self.f_base).grid(row=1, column=1, sticky="e")
        ttk.Button(g2, text="调整到这里", command=lambda: self.run(self.do_fit)).grid(row=1, column=2, padx=8)
        ttk.Label(g2, text="origin X,Y").grid(row=1, column=3)
        ttk.Entry(g2, textvariable=self.v_origin_x, width=5, font=self.f_base).grid(row=1, column=4)
        ttk.Entry(g2, textvariable=self.v_origin_y, width=5, font=self.f_base).grid(row=1, column=5)
        ttk.Button(g2, text="应用", command=self.apply_mode).grid(row=1, column=6, padx=4)

        g4 = ttk.LabelFrame(f, text="坐标模式（正常由自检自动判定，这里可手动改）", padding=10)
        g4.pack(fill="x", pady=10)
        cb = ttk.Combobox(g4, textvariable=self.v_mode, values=[MODE_SHARED, MODE_DESKTOP],
                          state="readonly", width=26, font=self.f_base)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.apply_mode())
        ttk.Label(g4, text="共享游戏区域时，画面坐标就是游戏坐标；整桌面时要靠 origin 偏移。",
                  font=self.f_small, foreground="#555555").pack(side="left", padx=12)

        g3 = ttk.LabelFrame(f, text="窗口状态测试", padding=10)
        g3.pack(fill="x", pady=10)
        ttk.Button(g3, text="最小化查看器", command=lambda: self.run(self.do_minimize)).pack(side="left")
        ttk.Button(g3, text="还原（不抢焦点）", command=lambda: self.run(self.do_restore)).pack(side="left", padx=8)
        ttk.Button(g3, text="刷新状态", command=lambda: (self.set_status(), self.show_info())).pack(side="left")
        self.info = tk.Text(f, height=12, font=self.f_base)
        self.info.pack(fill="both", expand=True, pady=10)

    def show_info(self) -> None:
        self.info.delete("1.0", "end")
        if not (self.handles and self.handles.alive()):
            self.info.insert("end", "未连接\n")
            return
        h = self.handles
        l, t, r, b = winapi.window_rect(h.top)
        cw, ch = winapi.client_size(h.child)
        self.info.insert("end", f"顶层窗口 {h.top}　标题: {winapi.title(h.top)}\n"
                                f"顶层外框 {r - l}x{b - t}　位置 ({l},{t})\n"
                                f"画面子窗口 {h.child}　客户区 {cw}x{ch}\n"
                                f"外框与画面客户区差值 ({r - l - cw}, {b - t - ch})\n"
                                f"最小化: {winapi.is_iconic(h.top)}\n"
                                f"模式: {self.v_mode.get()}　origin: {self.viewport.origin}"
                                f"　坐标范围: {self.viewport.game_size}\n")

    def do_launch(self) -> None:
        if self._retired():
            return
        host = self.v_host.get().strip()
        proc = viewer_link.launch_viewer(host, int(self.v_port.get()), self.v_pw.get())
        ok = viewer_link.confirm_unencrypted(proc.pid, 10)
        logmod.log("已点掉未加密确认框" if ok else "没有出现确认框，可能已直接连上")
        if not self.v_title.get():
            self.v_title.set(host)

    def do_bind(self) -> None:
        if self._retired():
            return
        kw = self.v_title.get().strip() or self.v_host.get().strip() or None
        if self._bind_viewer(kw):
            self.set_banner("已绑定查看器，可以刷新画面了。", "ok")
        else:
            self.set_banner("没找到查看器窗口。", "bad")

    def do_unbind(self) -> None:
        if self.channel:
            self.channel.unbind()
        self.handles = None
        self.set_status()
        self.v_view_info.set("未连接")
        logmod.log("已解除绑定")

    def do_fit(self) -> None:
        if not self.handles:
            self.set_banner("还没连接副机。", "warn")
            return
        target = (int(self.v_tw.get()), int(self.v_th.get()))
        got = viewer_link.fit_window(self.handles, target)
        logmod.log(f"调整窗口：目标 {target[0]}x{target[1]}，实际 {got[0]}x{got[1]}"
                   + ("" if got == target else "（没调到，可能被系统限制）"))
        self.set_status()
        self.show_info()

    def do_minimize(self) -> None:
        if self.handles:
            winapi.user32.ShowWindow(self.handles.top, 6)  # SW_MINIMIZE
            self.pump_sleep(1.0)
            logmod.log(f"已最小化，画面客户区 {winapi.client_size(self.handles.child)}（预期 0x0）")
            self.set_status()

    def do_restore(self) -> None:
        if self.handles:
            ok = viewer_link.ensure_restored(self.handles)
            logmod.log(f"还原{'成功' if ok else '失败'}，画面客户区 {winapi.client_size(self.handles.child)}")
            self.set_status()

    def apply_mode(self) -> None:
        try:
            origin = (int(self.v_origin_x.get()), int(self.v_origin_y.get()))
        except ValueError:
            origin = (0, 0)
        if self.v_mode.get() == MODE_SHARED:
            self.viewport = Viewport(origin=origin, game_size=(GAME_W, GAME_H),
                                     input_scale=1.0, capture_scale=self.ui)
            self.v_tw.set(str(round(TARGET_SHARED[0] * self.ui)))
            self.v_th.set(str(round(TARGET_SHARED[1] * self.ui)))
        else:
            size = winapi.client_size(self.handles.child) if (self.handles and self.handles.alive()) else (GAME_W, GAME_H)
            self.viewport = Viewport(origin=origin, game_size=size,
                                     input_scale=1.0, capture_scale=self.ui)
        if self.channel:
            self.actuator = Actuator(self.channel, self.viewport, self.actuator.latency_s if self.actuator else 0.8,
                                     sleep=self.pump_sleep)
        try:
            self.set_status()
        except (AttributeError, tk.TclError):
            pass

    # ---- 校验 ----
    def _build_guard(self) -> None:
        f = self.tab_guard
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.v_bg = tk.StringVar()
        ttk.Label(bar, text="已知背景色（可空）").pack(side="left")
        ttk.Entry(bar, textvariable=self.v_bg, width=9, font=self.f_base).pack(side="left", padx=4)
        ttk.Button(bar, text="L0 窗口/边带", command=lambda: self.run(self.do_l0)).pack(side="left", padx=4)
        ttk.Button(bar, text="L4 画面新鲜度", command=lambda: self.run(self.do_l4)).pack(side="left")
        ttk.Label(bar, text="取样区 x1,y1,x2,y2").pack(side="left", padx=(12, 2))
        self.v_l4region = tk.StringVar(value="0,0,200,100")
        ttk.Entry(bar, textvariable=self.v_l4region, width=16, font=self.f_base).pack(side="left")
        self.guard_text = tk.Text(f, height=18, font=self.f_base)
        self.guard_text.pack(fill="both", expand=True, pady=10)
        ttk.Label(f, text="L1/L2 要等 game.yaml 里配好锚点模板才能用；L6 看“输入”页的回环测试。",
                  font=self.f_small, foreground="#555555").pack(anchor="w")

    def _guard_out(self, res) -> None:
        self.guard_text.insert("end", f"{time.strftime('%H:%M:%S')} {res}\n")
        if res.data:
            self.guard_text.insert("end", f"          {res.data}\n")
        self.guard_text.see("end")
        logmod.log(str(res))

    def do_l0(self) -> None:
        if not self.need_bound():
            return
        expect = (int(self.v_tw.get()), int(self.v_th.get()))
        self._guard_out(guard.run_l0(self.channel, self.handles, expect, self.viewport.game_size,
                                     self.v_bg.get().strip() or None, self.ui))

    def do_l4(self) -> None:
        if not self.need_bound():
            return
        region = tuple(int(x) for x in self.v_l4region.get().split(","))
        self._guard_out(guard.run_l4(self.channel, region, 3.0, sleep=self.pump_sleep))

    # ================================================================ 日志
    def _build_log(self) -> None:
        bar = ttk.Frame(self.tab_log)
        bar.pack(fill="x")
        ttk.Button(bar, text="清空显示", command=lambda: self.log_text.config(state="normal") or
                              (self.log_text.delete("1.0", "end"), self.log_text.config(state="disabled"))).pack(side="left")
        ttk.Button(bar, text="复制诊断信息", command=self.copy_diag).pack(side="left", padx=8)
        self.log_text = tk.Text(self.tab_log, state="disabled", font=self.f_base)
        self.log_text.pack(fill="both", expand=True, pady=8)
        for line in logmod.recent():
            self._append_log(line)

    def _append_log(self, line: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _on_log_line(self, line: str) -> None:
        try:
            self._append_log(line)
        except tk.TclError:
            pass

    def on_close(self) -> None:
        try:
            if self.actuator:
                self.actuator.release_modifiers()
            if self.channel and self.channel.bound:
                self.channel.unbind()
        finally:
            self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not winapi.is_admin():
        messagebox.showwarning("建议用管理员身份运行",
                               "检查大漠注册需要管理员权限。\n\n"
                               "可以继续打开界面，但“自检”里的部分项目会不通过。\n"
                               "关闭本窗口后，右键 → 以管理员身份运行 即可。")
    Bench().mainloop()


if __name__ == "__main__":
    main()
