from __future__ import annotations

import csv
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from ctypes import wintypes
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import estop, winapi, wincap
from common.config import runtime_dir
from control import viewer_link
from control.target import TargetError, resolve
from control.worker import InputWorker
from kmtool import connect
from kmtool.coords import GAME_H, GAME_W, parse_align, parse_hold, parse_point, view_to_game
from kmtool.hotkey import GlobalHotkey
from manager.machines import Store

BG = "#f6f8fb"
CARD = "#ffffff"
FIELD = "#f8fafc"
EDGE = "#e5e9f0"
FG = "#1f2937"
DIM = "#667085"
MUTED = "#98a2b3"
BLUE = "#2563eb"
BLUE_DARK = "#1d4ed8"
BLUE_SOFT = "#eef4ff"
GREEN = "#15803d"
GREEN_SOFT = "#e9f7ef"
RED = "#dc2626"
RED_DARK = "#b91c1c"
RED_SOFT = "#fef2f2"
CANVAS_BG = "#0b1220"
UI = "Microsoft YaHei UI"
MONO = "Consolas"

F_TITLE = (UI, 11, "bold")
F_SECTION = (UI, 10, "bold")
F_BODY = (UI, 9)
F_SMALL = (UI, 8)
F_TINY = (UI, 7)
F_BUTTON = (UI, 9)
F_MONO = (MONO, 9)
F_NUM = (MONO, 18, "bold")
PICK_HINT = "悬停实时取值 · 单击画面填入并复制 · Ctrl+Alt+C 读取远程窗口鼠标"
POINTS_PATH = os.path.join(runtime_dir(), "kmtool_points.json")

BUTTON_STYLES = {
    "primary": dict(bg=BLUE, fg="#ffffff", activebackground=BLUE_DARK, activeforeground="#ffffff",
                    disabled_bg="#c7d6f8", disabled_fg="#ffffff"),
    "secondary": dict(bg="#eef2f7", fg=FG, activebackground="#e2e8f0", activeforeground=FG,
                      disabled_bg="#f1f4f8", disabled_fg="#98a2b3"),
    "soft": dict(bg=BLUE_SOFT, fg=BLUE, activebackground="#dbeafe", activeforeground=BLUE,
                 disabled_bg="#f3f6fb", disabled_fg="#a9b6c8"),
    "ghost": dict(bg=CARD, fg=FG, activebackground="#f6f8fb", activeforeground=FG,
                  disabled_bg=CARD, disabled_fg="#b6bfcc"),
    "danger": dict(bg=RED, fg="#ffffff", activebackground=RED_DARK, activeforeground="#ffffff",
                   disabled_bg="#f0b4b4", disabled_fg="#ffffff"),
}


def display_scale():
    dpi = winapi.user32.GetDpiForSystem() if hasattr(winapi.user32, "GetDpiForSystem") else 96
    return max(1.0, dpi / 96.0)


def standard_client():
    """画面子窗口客户区：恒为 800x600 物理像素，与系统缩放无关（第一层 v1.6，-scale=100）。"""
    return connect.STANDARD_CLIENT


def fit_standard(target):
    want = standard_client()
    actual = winapi.client_size(target.child)
    if actual != want:
        handles = viewer_link.ViewerHandles(target.top, target.child)
        viewer_link.restore_maximized(handles)
        actual = viewer_link.fit_window(handles, want)
        target = resolve(target.child)
    if actual != want:
        raise RuntimeError(f"画面子窗口无法调到标准 {want[0]}×{want[1]}，当前 {actual[0]}×{actual[1]}")
    return target


def grab_full(hwnd):
    """800x600 全尺寸画面（客户区已是 800x600 时无缩放）；失败返回 None。判定画面是否出齐不能用缩略图。"""
    cap = wincap.capture_scaled(hwnd, GAME_W, GAME_H)
    if not cap.get("ok"):
        return None
    return cap["rgb"], cap["w"], cap["h"]


def reveal_child(hwnd):
    """不激活还原最小化查看器；hwnd 可以是顶层或画面子窗口。"""
    top = hwnd
    while winapi.parent_of(top):
        top = winapi.parent_of(top)
    return viewer_link.reveal(viewer_link.ViewerHandles(top, hwnd))


def wait_target(children, timeout_s=8.0):
    """等待画面子窗口真正可见：刚启动的查看器会先有句柄和尺寸，稍后才显示。"""
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        for child in children:
            reveal_child(child)
            try:
                return resolve(child)
            except TargetError as exc:
                last = exc
        if time.monotonic() >= deadline:
            raise last or RuntimeError("查看器画面尚未准备好")
        time.sleep(0.2)


def copy_text(text: str) -> bool:
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.restype = wintypes.BOOL
    u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    opened = False
    for _ in range(6):
        if u.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        return False
    handle = None
    try:
        if not u.EmptyClipboard():
            return False
        data = (str(text) + "\0").encode("utf-16-le")
        handle = k.GlobalAlloc(0x0002, len(data))
        if not handle:
            return False
        ptr = k.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        k.GlobalUnlock(handle)
        if not u.SetClipboardData(13, handle):
            return False
        handle = None
        return True
    finally:
        if handle:
            k.GlobalFree(handle)
        u.CloseClipboard()


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = wintypes.POINT(x, y)
    winapi.user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    winapi.user32.ScreenToClient(hwnd, ctypes.byref(point))
    return point.x, point.y


def window_at_cursor() -> int:
    point = wintypes.POINT()
    winapi.user32.GetCursorPos(ctypes.byref(point))
    winapi.user32.WindowFromPoint.argtypes = [wintypes.POINT]
    winapi.user32.WindowFromPoint.restype = wintypes.HWND
    return int(winapi.user32.WindowFromPoint(point) or 0)


def run_async(root, work, done=None, failed=None):
    def deliver(callback, value):
        try:
            root.after(0, callback, value)
        except (tk.TclError, RuntimeError):
            pass

    def runner():
        try:
            result = work()
        except Exception as exc:
            if failed:
                deliver(failed, exc)
            else:
                deliver(lambda e: messagebox.showerror("操作失败", str(e)), exc)
        else:
            if done:
                deliver(done, result)

    threading.Thread(target=runner, daemon=True).start()


def make_button(parent, text, command, kind="secondary", **kwargs):
    style = BUTTON_STYLES[kind]
    options = dict(text=text, command=command, font=F_BUTTON, bd=0, relief="flat", cursor="hand2",
                   padx=12, pady=7, bg=style["bg"], fg=style["fg"],
                   activebackground=style["activebackground"], activeforeground=style["activeforeground"],
                   disabledforeground=style["disabled_fg"], takefocus=0,
                   highlightthickness=1 if kind == "ghost" else 0,
                   highlightbackground=EDGE, highlightcolor=EDGE)
    options.update(kwargs)
    button = tk.Button(parent, **options)
    button._kind = kind
    button._enabled_bg = style["bg"]
    button._enabled_fg = style["fg"]
    return button


def set_button_enabled(button, enabled):
    kind = getattr(button, "_kind", "secondary")
    style = BUTTON_STYLES[kind]
    if enabled:
        button.configure(state="normal", bg=button._enabled_bg, fg=button._enabled_fg, cursor="hand2")
    else:
        button.configure(state="disabled", bg=style["disabled_bg"], fg=style["disabled_fg"], cursor="")


def make_entry(parent, variable, *, width=None, show=None, justify="left", font=None):
    options = dict(textvariable=variable, justify=justify, font=font or F_BODY, bg=FIELD, fg=FG,
                   insertbackground=FG, relief="flat", bd=0, highlightthickness=1,
                   highlightbackground=EDGE, highlightcolor=BLUE)
    if width is not None:
        options["width"] = width
    if show:
        options["show"] = show
    return tk.Entry(parent, **options)


def make_card(parent, padx=16, pady=14):
    outer = tk.Frame(parent, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
    inner = tk.Frame(outer, bg=CARD)
    inner.pack(fill="both", expand=True, padx=padx, pady=pady)
    return outer, inner


def section(parent, title, *, badge=None, subtitle=None, wrap=330):
    head = tk.Frame(parent, bg=CARD)
    head.pack(fill="x", pady=(0, 6))
    if badge is not None:
        marker = tk.Canvas(head, width=22, height=22, bg=CARD, highlightthickness=0)
        marker.create_oval(1, 1, 21, 21, fill=BLUE, outline="")
        marker.create_text(11, 11, text=str(badge), fill="#ffffff", font=(UI, 8, "bold"))
        marker.pack(side="left", padx=(0, 8))
    tk.Label(head, text=title, bg=CARD, fg=FG, font=F_SECTION).pack(side="left")
    if subtitle:
        tk.Label(parent, text=subtitle, bg=CARD, fg=DIM, font=F_SMALL, justify="left",
                 wraplength=wrap).pack(anchor="w", pady=(0, 10))
    return head


class Highlight:
    def __init__(self, root):
        self.parts = []
        for _ in range(4):
            part = tk.Toplevel(root)
            part.overrideredirect(True)
            part.attributes("-topmost", True)
            part.configure(bg="#ef4444")
            part.withdraw()
            self.parts.append(part)
        self.hwnd = 0

    def show(self, hwnd):
        self.hide()
        if not hwnd or not winapi.is_window(hwnd):
            return
        self.hwnd = hwnd
        x1, y1, x2, y2 = winapi.window_rect(hwnd)
        w, h, t = x2 - x1, y2 - y1, 3
        for part, geo in zip(self.parts, ((w, t, x1, y1), (w, t, x1, y2 - t),
                                          (t, h, x1, y1), (t, h, x2 - t, y1))):
            pw, ph, px, py = geo
            part.geometry(f"{max(1, pw)}x{max(1, ph)}+{px}+{py}")
            part.deiconify()

    def hide(self):
        self.hwnd = 0
        for part in self.parts:
            part.withdraw()

    def destroy(self):
        for part in self.parts:
            part.destroy()


class KmTool:
    def __init__(self, root):
        self.root = root
        self.workers = {}
        self.current_hwnd = None
        self.current_target = None
        self.photo = None
        self.photo_size = (GAME_W, GAME_H)
        self.view_scale = 1.0
        self.last_png = None
        self.last_path = []
        self.last_click = None
        self.capture_busy = False
        self.bind_cancel = None
        self.capture_token = 0
        self.last_capture_error = 0.0
        self._last_fit_try = 0.0
        self.last_color_at = 0.0
        self.color_busy = False
        self.color_job = None
        self.image_item = None
        self.hover_point = None
        self.hover_color = tk.StringVar(value="#------")
        self.region_start = None
        self.drag_region = None
        self.region_mode = False
        self.region_set = False
        self.region = (0, 0, GAME_W - 1, GAME_H - 1)
        self.selected_point = (400, 300)
        self.press_point = None
        self.logs = deque(maxlen=200)
        self.calibration_rows = []
        self.calibrating = False
        self.closing = False
        self.read_active = False
        self.read_countdown = 0
        self.estop_active = False
        self.finder_target = 0
        self.action_widgets = []
        self.pick_hint = PICK_HINT
        self.store = Store()
        self.store.load()
        self.points = self._load_points()
        self.fps = tk.DoubleVar(value=5.0)
        self.speed = tk.DoubleVar(value=1.0)
        self.speed_buttons = {}
        self.visual = tk.BooleanVar(value=True)
        self.grid = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="准备就绪 · 请先连接设备")
        self.conn_name = tk.StringVar(value="未连接设备")
        self.conn_sub = tk.StringVar(value="把准星拖到远程画面，或从左侧手动连接")
        self.live_x = tk.StringVar(value="--")
        self.live_y = tk.StringVar(value="--")
        self.source = tk.StringVar(value="起点：--")
        self.hint_var = tk.StringVar(value=PICK_HINT)
        self.host = tk.StringVar(value="100.64.0.10")
        self.port = tk.IntVar(value=5900)
        self.password = tk.StringVar()
        self.target_x = tk.StringVar(value="400")
        self.target_y = tk.StringVar(value="300")
        self.hold = tk.StringVar(value="60,140")
        self.hold_hint = tk.StringVar(value="自动生效 · 随机 60–140")
        self.hold.trace_add("write", self._hold_changed)
        self.align_tol = tk.StringVar(value="3")
        self.align_max = tk.StringVar(value="30")
        self.align_hint = tk.StringVar(value="自动生效")
        self.align_tol.trace_add("write", self._align_changed)
        self.align_max.trace_add("write", self._align_changed)
        self.key = tk.StringVar(value="e")
        self.combo = tk.StringVar(value="alt,e")
        self.text = tk.StringVar()
        self.device_choice = tk.StringVar()
        self._build()
        self.highlight = Highlight(root)
        estop.clear()
        self.hotkey_ok = estop.start_hotkey()
        self.pick_hotkey = GlobalHotkey(0x43)
        self.pick_hotkey_ok = self.pick_hotkey.start()
        if not self.pick_hotkey_ok:
            self.pick_hint = "悬停实时取值 · 单击画面填入并复制 · 快捷键被占用，请用“读取鼠标”按钮"
            self.hint_var.set(self.pick_hint)
            self.root.bind("<Control-Alt-c>", lambda _e: self.read_cursor_position())
        self.root.bind("<Escape>", self._on_escape)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.log("急停热键 Ctrl+Alt+Q 已就绪" if self.hotkey_ok else "急停热键被占用，请使用界面上的红色急停按钮")
        self.log("取坐标 Ctrl+Alt+C 已就绪" if self.pick_hotkey_ok else "取坐标快捷键被占用，请使用“读取鼠标”按钮")
        self.root.after(250, self._capture_tick)
        self.root.after(300, self._estop_tick)
        self.root.after(120, self._hotkey_tick)

    def _build(self):
        self.root.title("后台鼠键测试工具")
        width, height = 1802, 1320
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(width, sw - 120)
        height = min(height, sh - 110)
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2 - 30)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(1802, width), min(1180, height))
        self.root.configure(bg=BG)
        self._style()
        self._build_header()
        self._build_footer()
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=18, pady=16)
        self.body.columnconfigure(0, minsize=292)
        self.body.columnconfigure(1, weight=1)
        self.body.columnconfigure(2, minsize=500)
        self.body.rowconfigure(0, weight=1)
        self._build_left(self.body)
        self._build_center(self.body)
        self._build_right(self.body)
        self._refresh_connection_ui()
        self._sync_actions()

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("K.TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                        bordercolor=EDGE, arrowcolor=DIM, padding=5, relief="flat")
        style.map("K.TCombobox",
                  fieldbackground=[("readonly", FIELD)],
                  selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)],
                  bordercolor=[("focus", BLUE)])
        style.configure("K.TCheckbutton", background=CARD, foreground=DIM, font=F_SMALL)
        style.map("K.TCheckbutton", background=[("active", CARD)], foreground=[("active", FG)])
        style.configure("K.Vertical.TScrollbar", background="#dbe2ea", troughcolor=FIELD,
                        bordercolor=FIELD, arrowcolor=DIM, relief="flat")
        style.map("K.Vertical.TScrollbar", background=[("active", "#c3cddb")])

    def _build_header(self):
        self.header = tk.Frame(self.root, bg=CARD, height=96, highlightbackground=EDGE, highlightthickness=1)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        left = tk.Frame(self.header, bg=CARD)
        left.pack(side="left", padx=22, pady=10)
        top = tk.Frame(left, bg=CARD)
        top.pack(anchor="w")
        self.conn_dot = tk.Canvas(top, width=10, height=10, bg=CARD, highlightthickness=0)
        self.conn_dot_item = self.conn_dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        self.conn_dot.pack(side="left", padx=(0, 8))
        tk.Label(top, textvariable=self.conn_name, bg=CARD, fg=FG, font=F_TITLE).pack(side="left")
        self.conn_pill = tk.Label(top, text="未连接", bg="#f2f4f7", fg=DIM, font=(UI, 7, "bold"),
                                  padx=7, pady=2)
        self.conn_pill.pack(side="left", padx=(10, 0))
        tk.Label(left, textvariable=self.conn_sub, bg=CARD, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(4, 0))
        right = tk.Frame(self.header, bg=CARD)
        right.pack(side="right", padx=20, pady=12)
        self.estop_btn = make_button(right, "急停  Ctrl+Alt+Q", self.emergency, "danger",
                                     font=(UI, 10, "bold"), padx=16, pady=8)
        self.estop_btn.pack(side="right")
        make_button(right, "解除急停", self.clear_estop, "secondary").pack(side="right", padx=(0, 8))
        make_button(right, "诊断", self.diagnostics, "secondary").pack(side="right", padx=(0, 8))
        self.estop_banner = tk.Frame(self.root, bg="#fff1f2", height=38,
                                     highlightbackground="#fecdd3", highlightthickness=1)
        self.estop_banner.pack_propagate(False)
        tk.Frame(self.estop_banner, bg=RED, width=4).pack(side="left", fill="y")
        tk.Label(self.estop_banner, text="急停已触发：新动作被拒绝，正在释放所有按键",
                 bg="#fff1f2", fg=RED_DARK, font=(UI, 9, "bold")).pack(side="left", padx=14)
        make_button(self.estop_banner, "解除急停", self.clear_estop, "secondary", pady=2,
                    padx=8).pack(side="right", padx=12, pady=4)

    def _build_left(self, parent):
        left = tk.Frame(parent, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        card, inner = make_card(left)
        card.pack(fill="x", pady=(0, 12))
        section(inner, "连接设备", badge="1",
                subtitle="按住准星拖到远程画面，或点选下方已连接的设备", wrap=258)
        self._build_finder(inner)
        self.device_rows = tk.Frame(inner, bg=CARD)
        self.device_rows.pack(fill="x", pady=(10, 0))
        self.unbind_btn = make_button(inner, "解除当前设备", self.unbind, "secondary", pady=5)
        self.unbind_btn.pack(fill="x", pady=(8, 0))
        tk.Frame(inner, bg=EDGE, height=1).pack(fill="x", pady=(10, 8))
        viewer_row = tk.Frame(inner, bg=CARD)
        viewer_row.pack(fill="x")
        tk.Label(viewer_row, text="远程窗口", bg=CARD, fg=MUTED, font=F_TINY).pack(side="left")
        self.viewer_hide_btn = make_button(viewer_row, "放回屏幕外", lambda: self.viewer_visible(False), "ghost",
                                           pady=2, padx=6, font=F_SMALL)
        self.viewer_hide_btn.pack(side="right")
        self.viewer_show_btn = make_button(viewer_row, "显示", lambda: self.viewer_visible(True), "ghost",
                                           pady=2, padx=6, font=F_SMALL)
        self.viewer_show_btn.pack(side="right", padx=(0, 4))
        self.refresh_devices()

        card, inner = make_card(left)
        card.pack(fill="x", pady=(0, 12))
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="手动连接", bg=CARD, fg=FG, font=F_SECTION).pack(side="left")
        self.manual_toggle = make_button(head, "展开", self.toggle_manual, "ghost", padx=8, pady=2)
        self.manual_toggle.pack(side="right")
        self.manual_body = tk.Frame(inner, bg=CARD)
        self._build_manual(self.manual_body)

        card, inner = make_card(left)
        card.pack(side="bottom", fill="x", pady=(12, 0))
        section(inner, "使用提示", wrap=258)
        for line in ("悬停画面：实时看坐标",
                     "单击画面：填入并复制",
                     "Ctrl+Alt+C：远程窗口取坐标"):
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="·", bg=CARD, fg=BLUE, font=F_SMALL).pack(side="left", anchor="n")
            tk.Label(row, text=line, bg=CARD, fg=DIM, font=F_SMALL, anchor="w",
                     wraplength=230, justify="left").pack(side="left", fill="x", expand=True,
                                                          padx=(6, 0))
        tk.Label(inner, text="输入只发到远程画面，不移动本机鼠标", bg=CARD, fg=MUTED,
                 font=F_TINY, anchor="w", wraplength=258, justify="left").pack(
            fill="x", pady=(6, 0))

    def _build_finder(self, parent):
        self.finder = tk.Frame(parent, bg=BLUE_SOFT, height=84, cursor="crosshair",
                               highlightbackground="#c9d9ff", highlightthickness=1)
        self.finder.pack(fill="x")
        self.finder.pack_propagate(False)
        self.finder_icon = tk.Label(self.finder, text="⌖", bg=BLUE_SOFT, fg=BLUE, font=("Arial", 24),
                                    cursor="crosshair")
        self.finder_icon.place(relx=0.5, rely=0.36, anchor="center")
        self.finder_text = tk.Label(self.finder, text="按住拖到远程画面", bg=BLUE_SOFT, fg="#3b5bdb",
                                    font=F_SMALL, cursor="crosshair")
        self.finder_text.place(relx=0.5, rely=0.74, anchor="center")
        for widget in (self.finder, self.finder_icon, self.finder_text):
            widget.bind("<ButtonPress-1>", self.finder_start)
            widget.bind("<B1-Motion>", self.finder_move)
            widget.bind("<ButtonRelease-1>", self.finder_end)

    def _build_manual(self, parent):
        names = [f"{m.name} | {m.host}:{m.port}" for m in self.store.machines]
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        self.machine_box = ttk.Combobox(row, textvariable=self.device_choice, values=names,
                                        state="readonly", style="K.TCombobox")
        self.machine_box.pack(side="left", fill="x", expand=True)
        self.machine_box.bind("<<ComboboxSelected>>", self.machine_selected)
        make_button(row, "刷新", self.reload_machines, "secondary", padx=9, pady=4).pack(side="left",
                                                                                    padx=(6, 0))
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="地址", bg=CARD, fg=DIM, font=F_SMALL, width=5, anchor="w").pack(side="left")
        make_entry(row, self.host).pack(side="left", fill="x", expand=True, ipady=5)
        tk.Label(row, text="端口", bg=CARD, fg=DIM, font=F_SMALL).pack(side="left", padx=(8, 4))
        make_entry(row, self.port, width=5, justify="center").pack(side="left", ipady=5)
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="密码", bg=CARD, fg=DIM, font=F_SMALL, width=5, anchor="w").pack(side="left")
        make_entry(row, self.password, show="•").pack(side="left", fill="x", expand=True, ipady=5)
        self.connect_btn = make_button(parent, "连接并绑定", self.launch_bind, "primary", pady=7)
        self.connect_btn.pack(fill="x", pady=(10, 0))

    def _build_center(self, parent):
        center = tk.Frame(parent, bg=BG)
        center.grid(row=0, column=1, sticky="nsew")
        card, inner = make_card(center, padx=14, pady=12)
        card.pack(fill="both", expand=True)
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="实时画面", bg=CARD, fg=FG, font=F_SECTION).pack(side="left")
        self.region_btn = make_button(head, "框选区域", self.region_toggle, "secondary", padx=10, pady=5)
        self.region_btn.pack(side="right")
        ttk.Checkbutton(head, text="网格", variable=self.grid, command=self.draw_overlays,
                        style="K.TCheckbutton").pack(side="right", padx=(10, 0))
        tk.Label(head, text="刷新", bg=CARD, fg=DIM, font=F_SMALL).pack(side="right", padx=(14, 4))
        ttk.Combobox(head, textvariable=self.fps, values=(1.0, 2.0, 3.0, 5.0, 8.0, 10.0), width=4,
                     state="readonly", style="K.TCombobox").pack(side="right")
        holder = tk.Frame(inner, bg=CARD)
        holder.pack(fill="x")
        self.canvas = tk.Canvas(holder, width=GAME_W, height=GAME_H, bg=CANVAS_BG,
                                highlightbackground=EDGE, highlightthickness=1, cursor="arrow")
        self.canvas.pack(anchor="n", pady=(0, 12))
        self.canvas.bind("<Motion>", self.canvas_move)
        self.canvas.bind("<ButtonPress-1>", self.canvas_down)
        self.canvas.bind("<B1-Motion>", self.canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_up)
        bar = tk.Frame(inner, bg=FIELD, highlightbackground=EDGE, highlightthickness=1)
        bar.pack(fill="x")
        bar.columnconfigure(0, weight=1)
        live = tk.Frame(bar, bg=FIELD)
        live.grid(row=0, column=0, sticky="w", padx=14, pady=(8, 0))
        tk.Label(live, text="指向坐标", bg=FIELD, fg=DIM, font=F_SMALL).pack(anchor="w")
        values = tk.Frame(live, bg=FIELD)
        values.pack(anchor="w")
        tk.Label(values, text="X", bg=FIELD, fg=DIM, font=F_SMALL).pack(side="left")
        tk.Label(values, textvariable=self.live_x, bg=FIELD, fg=FG, font=F_NUM).pack(side="left",
                                                                                      padx=(4, 16))
        tk.Label(values, text="Y", bg=FIELD, fg=DIM, font=F_SMALL).pack(side="left")
        tk.Label(values, textvariable=self.live_y, bg=FIELD, fg=FG, font=F_NUM).pack(side="left",
                                                                                      padx=(4, 18))
        self.swatch = tk.Frame(values, width=18, height=18, bg=EDGE, highlightbackground=EDGE,
                               highlightthickness=1)
        self.swatch.pack(side="left", pady=6)
        tk.Label(values, textvariable=self.hover_color, bg=FIELD, fg=DIM, font=F_MONO).pack(
            side="left", padx=(6, 0))
        buttons = tk.Frame(bar, bg=FIELD)
        buttons.grid(row=0, column=1, sticky="e", padx=12, pady=(8, 0))
        self.fill_btn = make_button(buttons, "填入目标", self.use_hover_point, "soft", pady=5)
        self.fill_btn.pack(side="left")
        self.copy_live_btn = make_button(buttons, "复制", self.copy_live, "secondary", pady=5)
        self.copy_live_btn.pack(side="left", padx=6)
        self.read_btn = make_button(buttons, "读取鼠标", self.start_mouse_read, "secondary", pady=5)
        self.read_btn.pack(side="left")
        tk.Label(bar, textvariable=self.hint_var, bg=FIELD, fg=MUTED, font=F_SMALL, anchor="w").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 9))
        spacer = tk.Frame(inner, bg=CARD)
        spacer.pack(fill="both", expand=True)
        self._draw_empty_state()

    def _build_right(self, parent):
        right = tk.Frame(parent, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        right.grid(row=0, column=2, sticky="nsew", padx=(14, 0))
        top = tk.Frame(right, bg=CARD)
        top.pack(fill="x", padx=10, pady=(10, 0))
        track = tk.Frame(top, bg=FIELD)
        track.pack(fill="x")
        self.tabs = {}
        for index, (key, text) in enumerate((("mouse", "鼠标"), ("keys", "键盘"),
                                             ("calib", "校准"), ("logs", "日志"))):
            button = tk.Button(track, text=text, command=lambda k=key: self.show_pane(k),
                               font=F_BUTTON, bd=0, relief="flat", bg=FIELD, fg=DIM,
                               activebackground=BLUE_SOFT, activeforeground=BLUE, cursor="hand2",
                               padx=6, pady=6, highlightthickness=1, highlightbackground=FIELD,
                               highlightcolor=FIELD, takefocus=0)
            button.grid(row=0, column=index, sticky="ew")
            track.columnconfigure(index, weight=1)
            self.tabs[key] = button
        self.pane_host = tk.Frame(right, bg=CARD)
        self.pane_host.pack(fill="both", expand=True, padx=14, pady=(8, 14))
        self.pane_host.rowconfigure(0, weight=1)
        self.pane_host.columnconfigure(0, weight=1)
        self.panes = {}
        builders = {"mouse": self._build_mouse_pane, "keys": self._build_keys_pane,
                    "calib": self._build_calib_pane, "logs": self._build_logs_pane}
        for key, builder in builders.items():
            pane = tk.Frame(self.pane_host, bg=CARD)
            pane.grid(row=0, column=0, sticky="nsew")
            self.panes[key] = pane
            builder(pane)
        self.show_pane("mouse")
        self.refresh_points()
        self.calib_text.configure(state="normal")
        self.calib_text.insert("1.0", "尚未校准。\n点击上方按钮开始九点校准，结束后这里会列出九个点。")
        self.calib_text.configure(state="disabled")

    def _build_mouse_pane(self, pane):
        section(pane, "确认目标", badge="2", subtitle="点画面自动填入；也可以直接改数字", wrap=460)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x")
        for index, (label, variable) in enumerate((("X", self.target_x), ("Y", self.target_y))):
            cell = tk.Frame(row, bg=CARD)
            cell.pack(side="left", fill="x", expand=True, padx=(0, 8) if index == 0 else (0, 0))
            tk.Label(cell, text=label, bg=CARD, fg=DIM, font=F_SMALL).pack(anchor="w")
            make_entry(cell, variable, width=6, justify="center", font=(MONO, 12, "bold")).pack(
                fill="x", ipady=5, pady=(2, 0))
        tk.Label(pane, text="范围 0–799 / 0–599；点画面可自动填入", bg=CARD, fg=MUTED,
                 font=F_TINY, wraplength=460, justify="left").pack(anchor="w", pady=(4, 8))
        section(pane, "发送动作", badge="3", wrap=460)
        self.primary_click = make_button(pane, "走到目标", lambda: self.action("click"), "primary",
                                         font=(UI, 10, "bold"), pady=9)
        self.primary_click.pack(fill="x")
        tk.Label(pane, text="先沿真人轨迹移动指针，再左键点击，人物会走向该点\n"
                            "人物按格子寻路，点 NPC/玩家/物品时在交互距离停下\n"
                            "点不可走地形停在最近可走处\n"
                            "红十字是目标点，黄圈是实际点击落点\n"
                            "开启“水晶箭头对齐”后，点击前反复修正，直到箭尖进入下面的容差\n"
                            "游戏光标由游戏自绘，首发常偏几十px；每轮约1~2秒，链路慢时更久\n"
                            "超过最长时间仍没到位就按当前位置执行，日志会写偏差\n"
                            "认不出水晶（登录页、光标变形）时按目标点直接点",
                 bg=CARD, fg=DIM, font=F_TINY, justify="left", wraplength=470).pack(anchor="w", pady=(4, 6))
        self.visual_check = ttk.Checkbutton(
            pane, text="水晶箭头对齐（点击前把箭头尖对准黄圈）",
            variable=self.visual, command=self._visual_changed, style="K.TCheckbutton")
        self.visual_check.pack(anchor="w", pady=(0, 4))
        align_row = tk.Frame(pane, bg=CARD)
        align_row.pack(fill="x", pady=(0, 8))
        tk.Label(align_row, text="容差 ±", bg=CARD, fg=DIM, font=F_SMALL).pack(side="left")
        self.align_tol_entry = make_entry(align_row, self.align_tol, width=4, justify="center")
        self.align_tol_entry.pack(side="left", padx=(4, 4), ipady=2)
        tk.Label(align_row, text="px　最长", bg=CARD, fg=DIM, font=F_SMALL).pack(side="left")
        self.align_max_entry = make_entry(align_row, self.align_max, width=4, justify="center")
        self.align_max_entry.pack(side="left", padx=(4, 4), ipady=2)
        tk.Label(align_row, text="秒", bg=CARD, fg=DIM, font=F_SMALL).pack(side="left")
        tk.Label(align_row, textvariable=self.align_hint, bg=CARD, fg=MUTED,
                 font=F_TINY).pack(side="right")
        grid = tk.Frame(pane, bg=CARD)
        grid.pack(fill="x")
        actions = (
            ("只移动指针", lambda: self.action("move_to")),
            ("右键点击", lambda: self.action("click", button="right")),
            ("双击", lambda: self.action("double_click")),
            ("悬停", lambda: self.action("hover")),
            ("滚轮向上", lambda: self.action("wheel", notches=1)),
            ("滚轮向下", lambda: self.action("wheel", notches=-1)),
        )
        for index, (label, command) in enumerate(actions):
            button = make_button(grid, label, command, "secondary", padx=8, pady=6)
            button.grid(row=index // 2, column=index % 2, sticky="ew",
                        padx=(0, 6) if index % 2 == 0 else (0, 0), pady=3)
            grid.columnconfigure(index % 2, weight=1)
            self.action_widgets.append(button)
        self.action_widgets.append(self.primary_click)
        box = tk.Frame(pane, bg=FIELD, highlightbackground=EDGE, highlightthickness=1)
        box.pack(fill="x", pady=(10, 0))
        box_inner = tk.Frame(box, bg=FIELD)
        box_inner.pack(fill="x", padx=12, pady=8)
        behavior = tk.Frame(box_inner, bg=FIELD)
        behavior.pack(fill="x")
        tk.Label(behavior, text="点击按住", bg=FIELD, fg=DIM, font=F_SMALL).pack(side="left")
        self.hold_entry = make_entry(behavior, self.hold, width=8, justify="center")
        self.hold_entry.pack(side="left", padx=(6, 4), ipady=3)
        tk.Label(behavior, text="毫秒", bg=FIELD, fg=MUTED, font=F_SMALL).pack(side="left")
        tk.Label(behavior, textvariable=self.hold_hint, bg=FIELD, fg=MUTED,
                 font=F_TINY).pack(side="right")
        speed_row = tk.Frame(box_inner, bg=FIELD)
        speed_row.pack(fill="x", pady=(8, 0))
        tk.Label(speed_row, text="指针速度", bg=FIELD, fg=DIM, font=F_SMALL).pack(side="left")
        chips = tk.Frame(speed_row, bg=FIELD)
        chips.pack(side="right")
        for value, label in ((0.7, "慢 0.7×"), (1.0, "标准 1×"), (1.4, "快 1.4×"), (2.0, "极快 2×")):
            button = tk.Button(chips, text=label, font=F_SMALL, bd=0, relief="flat", cursor="hand2",
                               padx=6, pady=3, command=lambda v=value: self.set_speed(v))
            button.pack(side="left", padx=(4, 0))
            self.speed_buttons[value] = button
        self._sync_speed_buttons()
        self.confirm_btn = make_button(pane, "画面变化确认（移动并轮询）", self.action_confirmed,
                                       "secondary", pady=6)
        self.confirm_btn.pack(fill="x", pady=(10, 0))
        self.action_widgets.append(self.confirm_btn)
        head = tk.Frame(pane, bg=CARD)
        head.pack(fill="x", pady=(12, 4))
        tk.Label(head, text="常用坐标", bg=CARD, fg=FG, font=F_SECTION).pack(side="left")
        tk.Label(head, text="双击复用", bg=CARD, fg=MUTED, font=F_TINY).pack(side="right")
        list_wrap = tk.Frame(pane, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        self.point_list = tk.Listbox(list_wrap, height=5, width=30, bg=FIELD, fg=FG, relief="flat",
                                     highlightthickness=1, highlightbackground=EDGE,
                                     selectbackground=BLUE_SOFT, selectforeground=FG,
                                     font=F_SMALL, exportselection=False, activestyle="none")
        self.point_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.point_list.yview,
                               style="K.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.point_list.configure(yscrollcommand=scroll.set)
        self.point_list.bind("<Double-Button-1>", self.point_selected)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        make_button(row, "使用所选", self.use_selected_point, "soft", pady=5).pack(
            side="left", fill="x", expand=True)
        make_button(row, "保存当前", self.save_named_point, "soft", pady=5).pack(
            side="left", fill="x", expand=True, padx=6)
        make_button(row, "删除", self.delete_point, "secondary", pady=5).pack(
            side="left", fill="x", expand=True)

    def _build_keys_pane(self, pane):
        section(pane, "单键", subtitle="例如 e、space、enter；按下与抬起自动成对", wrap=460)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x")
        make_entry(row, self.key, width=10).pack(side="left", fill="x", expand=True, ipady=5)
        button = make_button(row, "发送单键", lambda: self.action("key_press", self.key.get()),
                             "secondary")
        button.pack(side="left", padx=(8, 0))
        self.action_widgets.append(button)
        section(pane, "组合键", subtitle="按顺序按下，再倒序抬起；可点下方快捷组合", wrap=460)
        presets = tk.Frame(pane, bg=CARD)
        presets.pack(fill="x", pady=(0, 6))
        for label, combo in (("Alt+E", "alt,e"), ("Alt+W", "alt,w"), ("Alt+Q", "alt,q")):
            preset = make_button(presets, label, lambda c=combo: self.send_combo(c), "soft",
                                 padx=8, pady=4)
            preset.pack(side="left", padx=(0, 6))
            self.action_widgets.append(preset)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x")
        make_entry(row, self.combo, width=10).pack(side="left", fill="x", expand=True, ipady=5)
        button = make_button(row, "发送组合键", self.send_combo, "secondary")
        button.pack(side="left", padx=(8, 0))
        self.action_widgets.append(button)
        section(pane, "文本", subtitle="仅 ASCII；逐键发送并带随机节奏", wrap=460)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x")
        make_entry(row, self.text, width=10).pack(side="left", fill="x", expand=True, ipady=5)
        button = make_button(row, "逐键发送", lambda: self.action("type_text", self.text.get()),
                             "secondary")
        button.pack(side="left", padx=(8, 0))
        self.action_widgets.append(button)
        tk.Frame(pane, bg=EDGE, height=1).pack(fill="x", pady=12)
        section(pane, "区域与拖拽", subtitle="先在上方画面拖出区域，再执行下面的动作", wrap=460)
        for label, command in (
            ("从目标拖到区域中心", self.drag_region),
            ("把区域中心设为目标", self.region_center),
            ("抬起全部键", lambda: self.action("release_all", needs_point=False)),
        ):
            button = make_button(pane, label, command, "secondary", pady=6)
            button.pack(fill="x", pady=3)
            self.action_widgets.append(button)
        tk.Label(pane, text="异常、急停或退出时，工具会自动抬起所有键和鼠标按钮。",
                 bg=CARD, fg=MUTED, font=F_TINY, wraplength=356, justify="left").pack(
            anchor="w", pady=(8, 0))

    def _build_calib_pane(self, pane):
        section(pane, "九点校准", subtitle="逐点移动并检测指针；检测不可靠时以跟踪值为依据", wrap=460)
        note = tk.Frame(pane, bg=FIELD, highlightbackground=EDGE, highlightthickness=1)
        note.pack(fill="x", pady=(0, 10))
        tk.Label(note, text="校准期间请勿操作远程窗口。自动检测只作参考，\n"
                            "当前版本以跟踪值兜底，起点不会被当成已观测。",
                 bg=FIELD, fg=DIM, font=F_SMALL, justify="left", wraplength=440).pack(
            anchor="w", padx=10, pady=8)
        self.calib_btn = make_button(pane, "开始九点校准", self.calibrate, "primary", pady=8)
        self.calib_btn.pack(fill="x")
        self.replay_btn = make_button(pane, "回放最近轨迹", self.replay, "secondary", pady=6)
        self.replay_btn.pack(fill="x", pady=(6, 10))
        self.action_widgets.extend((self.calib_btn, self.replay_btn))
        text_wrap = tk.Frame(pane, bg=CARD)
        text_wrap.pack(fill="both", expand=True)
        self.calib_text = tk.Text(text_wrap, width=30, height=12, bg=FIELD, fg=FG, relief="flat",
                                  font=("Consolas", 8), wrap="word", highlightthickness=1,
                                  highlightbackground=EDGE, padx=8, pady=6, state="disabled")
        self.calib_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.calib_text.yview,
                               style="K.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.calib_text.configure(yscrollcommand=scroll.set)

    def _build_logs_pane(self, pane):
        section(pane, "运行日志", subtitle="不记录密码；最多保留 200 条", wrap=460)
        row = tk.Frame(pane, bg=CARD)
        row.pack(fill="x", pady=(0, 8))
        tools = (
            ("区域另存 PNG", self.save_region),
            ("导出轨迹 CSV", self.export_path),
            ("清空轨迹", self.clear_path),
        )
        for index, (label, command) in enumerate(tools):
            button = make_button(row, label, command, "secondary", padx=6, pady=5)
            button.grid(row=0, column=index, sticky="ew", padx=(0, 4) if index < 2 else (0, 0))
            row.columnconfigure(index, weight=1)
            if label != "清空轨迹":
                self.action_widgets.append(button)
        text_wrap = tk.Frame(pane, bg=CARD)
        text_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(text_wrap, width=30, height=12, bg=FIELD, fg=FG, relief="flat",
                                font=("Consolas", 8), wrap="word", highlightthickness=1,
                                highlightbackground=EDGE, padx=8, pady=6, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.log_text.yview,
                               style="K.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

    def _build_footer(self):
        footer = tk.Frame(self.root, bg=CARD, height=38, highlightbackground=EDGE, highlightthickness=1)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        tk.Label(footer, textvariable=self.status, bg=CARD, fg=DIM, anchor="w", font=F_SMALL).pack(
            side="left", fill="x", expand=True, padx=18)
        tk.Label(footer, text="仅发送到已校验画面", bg=CARD, fg=MUTED,
                 font=F_SMALL).pack(side="right", padx=18)
        tk.Label(footer, textvariable=self.source, bg="#f2f4f7", fg=DIM, font=F_SMALL,
                 padx=8, pady=2).pack(side="right", padx=(0, 10))

    def show_pane(self, key):
        for name, pane in self.panes.items():
            if name == key:
                pane.grid()
            else:
                pane.grid_remove()
        for name, button in self.tabs.items():
            if name == key:
                button.configure(bg=CARD, fg=BLUE, highlightbackground=EDGE)
            else:
                button.configure(bg=FIELD, fg=DIM, highlightbackground=FIELD)
        if key == "logs" and hasattr(self, "log_text"):
            self.log_text.see("end")

    def _refresh_connection_ui(self):
        connected = self.current_hwnd in self.workers
        target = self.current_target
        if connected and target:
            name = target.address or target.title or f"HWND {self.current_hwnd}"
            self.conn_name.set(name)
            self.conn_sub.set(f"已连接 · {len(self.workers)} 台设备 · 画面坐标 800×600")
            self.conn_dot.itemconfigure(self.conn_dot_item, fill=GREEN)
            self._set_pill("已连接", GREEN, GREEN_SOFT)
            self.canvas.configure(cursor="tcross" if self.region_mode else "crosshair")
        else:
            self.conn_name.set("未连接设备")
            self.conn_sub.set("把准星拖到远程画面，或从左侧手动连接")
            self.conn_dot.itemconfigure(self.conn_dot_item, fill=MUTED)
            self._set_pill("未连接", DIM, "#f2f4f7")
            self.canvas.configure(cursor="arrow")
            self.source.set("起点：--")
        if estop.is_set():
            self._set_pill("已急停", RED, RED_SOFT)

    def _set_pill(self, text, fg, bg):
        self.conn_pill.configure(text=text, fg=fg, bg=bg)

    def _sync_actions(self):
        connected = self.current_hwnd in self.workers
        enabled = connected and not estop.is_set()
        for widget in self.action_widgets:
            set_button_enabled(widget, enabled)
        for widget in (self.unbind_btn, self.viewer_show_btn, self.viewer_hide_btn,
                       self.region_btn):
            set_button_enabled(widget, connected)
        set_button_enabled(self.read_btn, enabled and not self.read_active)

    def toggle_manual(self):
        if self.manual_body.winfo_manager():
            self.manual_body.pack_forget()
            self.manual_toggle.configure(text="展开")
        else:
            self.manual_body.pack(fill="x", after=self.manual_toggle.master, pady=(0, 0))
            self.manual_toggle.configure(text="收起")

    def finder_start(self, _event=None):
        self.finder_target = 0
        self.finder.configure(bg="#dbeafe")
        self.finder_icon.configure(bg="#dbeafe", text="⌖")
        self.finder_text.configure(bg="#dbeafe", text="拖到目标窗口后松开")
        self.finder.grab_set()
        self.finder_move()

    def finder_move(self, _event=None):
        hwnd = window_at_cursor()
        parts = {part.winfo_id() for part in self.highlight.parts} if hasattr(self, "highlight") else set()
        if not hwnd or hwnd in parts or winapi.pid_of(hwnd) == os.getpid():
            return
        self.finder_target = hwnd
        self.highlight.show(hwnd)
        self.status.set(f"松开绑定：{winapi.title(hwnd) or winapi.class_name(hwnd)}  HWND={hwnd}")

    def finder_end(self, _event=None):
        hwnd = self.finder_target
        self.finder.grab_release()
        self.finder.configure(bg=BLUE_SOFT)
        self.finder_icon.configure(bg=BLUE_SOFT, text="⌖")
        self.finder_text.configure(bg=BLUE_SOFT, text="按住拖到远程画面")
        if hasattr(self, "highlight"):
            self.highlight.hide()
        if not hwnd:
            messagebox.showinfo("没有选中画面", "请从准星按住，拖到远程查看器的画面区域后松开。")
            return
        reveal_child(hwnd)
        try:
            target = resolve(hwnd)
        except Exception as exc:
            messagebox.showerror("无法绑定", str(exc))
            self.log(f"绑定被拒绝：{exc}")
            return
        self._bind_target(target)

    def _bind_target(self, target):
        """拖拽绑定：先核对这个查看器是标准参数启动的，再走统一的"调尺寸 → 等画面出齐 → 绑定"。"""
        def acquire(_ops):
            bad = connect.standard_problems(target.pid)
            if bad:
                raise RuntimeError("这个查看器不是按标准参数启动的（" + "；".join(bad) + "），画面可能被缩放或偏移，"
                                   "坐标会错位。请展开“手动连接”，用“连接并绑定”让工具重新打开它。")
            return target
        self._run_bind(acquire, "无法绑定")

    def _connect_ops(self):
        return connect.Ops(
            ensure_standard=viewer_link.ensure_standard,
            find_viewers=viewer_link.find_viewers,
            launch_viewer=viewer_link.launch_viewer,
            confirm_unencrypted=viewer_link.confirm_unencrypted,
            wait_target=wait_target,
            pid_of=winapi.pid_of,
            client_width=lambda hwnd: winapi.client_size(hwnd)[0],
            fit=fit_standard,
            grab=grab_full,
            address_viewers=lambda host, port: connect.address_viewers(
                host, port, viewer_link.find_viewers, winapi.pid_of))

    def _post(self, callback):
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _run_bind(self, acquire, fail_title):
        """后台线程里：acquire(ops) 拿到窗口 → 调到 800x600 → 等画面出齐；回到主线程再绑定。可取消。"""
        if self.bind_cancel is not None:
            self._toast("正在连接，请先等待完成或点“取消连接”", "error")
            return
        cancel = threading.Event()
        self.bind_cancel = cancel
        self.connect_btn.configure(state="normal", text="取消连接", command=self.cancel_bind)
        ops = self._connect_ops()
        shown = {"first": True}

        def progress(elapsed):
            first, shown["first"] = shown["first"], False
            self._post(lambda: self._show_wait(elapsed, first))

        def work():
            target = acquire(ops)
            return connect.prepare_target(target, ops, cancel, progress)

        def finish():
            self.bind_cancel = None
            self.connect_btn.configure(state="normal", text="连接并绑定", command=self.launch_bind)
            if not self.workers:
                self._draw_empty_state()

        def done(target):
            finish()
            try:
                self.add_worker(target)
            except Exception as exc:
                messagebox.showerror("无法绑定", str(exc))
                self.log(f"绑定被拒绝：{exc}")

        def failed(exc):
            finish()
            if isinstance(exc, connect.Cancelled):
                self.log("已取消连接")
                self._toast("已取消连接")
                return
            messagebox.showerror(fail_title, str(exc))
            self.log(f"{fail_title}：{exc}")

        run_async(self.root, work, done, failed)

    def cancel_bind(self):
        if self.bind_cancel is not None:
            self.bind_cancel.set()
            self.connect_btn.configure(text="正在取消…")

    def _show_wait(self, elapsed, first):
        if self.bind_cancel is None:
            return
        if first:
            self.log("画面窗口已出现，等待画面传输完整（慢链路首屏可能要 10~30 秒，多台共用链路更久）")
        self.status.set(f"画面传输中… 已等 {int(elapsed)} 秒 · 画面铺满后才允许绑定，可点“取消连接”")
        if not self.workers:
            self._show_loading_state("画面传输中…", f"已等 {int(elapsed)} 秒，画面铺满后自动绑定")

    def add_worker(self, target):
        if not viewer_link.reveal(viewer_link.ViewerHandles(target.top, target.child)):
            raise RuntimeError("查看器处于最小化状态且无法不激活还原，请手动恢复窗口后重试")
        target = fit_standard(target)
        physical = winapi.client_size(target.child)
        if physical != standard_client():
            raise RuntimeError(f"画面窗口不是 800×600（当前 {physical[0]}×{physical[1]}），拒绝绑定")
        old = self.workers.pop(target.child, None)
        if old:
            old.stop()
        worker = InputWorker(target.child, target.address or str(target.child),
                             input_scale=1.0, capture_scale=1.0).wait_ready()
        worker.set_visual(self.visual.get())
        self._apply_align_to(worker)
        self.workers[target.child] = worker
        self._apply_hold_to(worker)
        self.current_hwnd = target.child
        self.current_target = target
        self._reset_frame()
        self._reset_hover()
        self.capture_token += 1
        self._show_loading_state()
        self.refresh_devices()
        self._refresh_connection_ui()
        self._sync_actions()
        self.log(f"已连接 {target.address or target.child}，画面 800×600，可以开始无害测试")

    def _ensure_geometry(self, notify=False):
        """查看器被最大化、最小化或缩放后输入/抓图会失效，发现变化就恢复。"""
        target = self.current_target
        if not target or self.current_hwnd not in self.workers:
            return True
        if winapi.is_iconic(target.top):
            now = time.monotonic()
            if now - self._last_fit_try < 2.0:
                return False
            self._last_fit_try = now
            if not viewer_link.reveal(viewer_link.ViewerHandles(target.top, target.child)):
                self.log("查看器处于最小化状态，无法不激活还原")
                if notify:
                    self._toast("查看器最小化且无法还原，请手动恢复窗口", "error")
                return False
            self.log("查看器已不激活还原")
            if notify:
                self._toast("查看器已还原")
        want = standard_client()
        try:
            actual = winapi.client_size(target.child)
        except Exception:
            return False
        if actual == want:
            return True
        now = time.monotonic()
        if now - self._last_fit_try < 2.0:
            return False
        self._last_fit_try = now
        try:
            target = fit_standard(target)
        except Exception as exc:
            self.log(f"查看器尺寸 {actual[0]}×{actual[1]} 无法恢复：{exc}")
            if notify:
                self._toast("查看器尺寸异常，请重新连接", "error")
            return False
        self.current_target = target
        self.log(f"查看器尺寸 {actual[0]}×{actual[1]} 已恢复为 {want[0]}×{want[1]}")
        if notify:
            self._toast("查看器尺寸被改变，已自动恢复")
        return True

    def refresh_devices(self):
        for child in self.device_rows.winfo_children():
            child.destroy()
        items = list(self.workers.items())
        if not items:
            empty = tk.Frame(self.device_rows, bg=FIELD, highlightbackground=EDGE, highlightthickness=1)
            empty.pack(fill="x")
            tk.Label(empty, text="还没有已连接设备", bg=FIELD, fg=MUTED, font=F_SMALL).pack(
                anchor="w", padx=10, pady=9)
            return
        for hwnd, worker in items[:5]:
            selected = hwnd == self.current_hwnd
            row_bg = BLUE_SOFT if selected else FIELD
            row = tk.Frame(self.device_rows, bg=row_bg,
                           highlightbackground="#c9d9ff" if selected else EDGE, highlightthickness=1,
                           cursor="hand2")
            row.pack(fill="x", pady=(0, 4))
            dot = tk.Canvas(row, width=10, height=10, bg=row_bg, highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=GREEN if selected else MUTED, outline="")
            dot.pack(side="left", padx=(8, 6), pady=9)
            name = tk.Label(row, text=worker.slave or str(hwnd), bg=row_bg, fg=FG if selected else DIM,
                            font=F_SMALL, anchor="w")
            name.pack(side="left", fill="x", expand=True)
            if selected:
                tk.Label(row, text="当前", bg=row_bg, fg=BLUE, font=(UI, 7, "bold")).pack(
                    side="right", padx=8)
            for widget in (row, dot, name):
                widget.bind("<Button-1>", lambda _e, h=hwnd: self.select_device(h))
        if len(items) > 5:
            tk.Label(self.device_rows, text=f"另有 {len(items) - 5} 台未显示", bg=CARD, fg=MUTED,
                     font=F_TINY).pack(anchor="w")

    def select_device(self, hwnd):
        if hwnd == self.current_hwnd:
            return
        reveal_child(hwnd)
        try:
            target = resolve(hwnd)
        except Exception as exc:
            self._toast("设备窗口已失效，请重新绑定", "error")
            self.log(f"切换失败：{exc}")
            return
        self.current_hwnd = hwnd
        self.current_target = target
        self._reset_frame()
        self._reset_hover()
        self.capture_token += 1
        self._show_loading_state()
        self.refresh_devices()
        self._refresh_connection_ui()
        self._sync_actions()
        self.log(f"已切换到 {target.address or target.child}")

    def machine_selected(self, _event=None):
        index = self.machine_box.current()
        if 0 <= index < len(self.store.machines):
            machine = self.store.machines[index]
            self.host.set(machine.host)
            self.port.set(machine.port)
            self.password.set(machine.password)

    def reload_machines(self):
        self.store.load()
        self.machine_box.configure(values=[f"{m.name} | {m.host}:{m.port}" for m in self.store.machines])
        self._toast("设备清单已刷新")
        self.log("设备清单已重新读取，未修改原文件")

    def launch_bind(self):
        host, port, password = self.host.get().strip(), self.port.get(), self.password.get()
        if not host:
            messagebox.showwarning("缺少地址", "请输入设备地址")
            return
        if self.bind_cancel is not None:
            self._toast("正在连接，请先等待完成或点“取消连接”", "error")
            return
        self.log(f"正在连接 {host}:{port}，密码不会写入日志")
        self._run_bind(lambda ops: connect.acquire_target(host, port, password, ops), "连接失败")

    def unbind(self):
        if self.current_hwnd not in self.workers:
            self._toast("当前没有已连接设备")
            return
        if self.region_mode:
            self._set_region_mode(False)
        worker = self.workers.pop(self.current_hwnd)
        worker.stop()
        self.capture_token += 1
        self._reset_frame()
        self._reset_hover()
        if self.workers:
            self.current_hwnd = next(iter(self.workers))
            reveal_child(self.current_hwnd)
            try:
                self.current_target = resolve(self.current_hwnd)
                self._show_loading_state()
            except Exception:
                self.current_hwnd = None
                self.current_target = None
                self._draw_empty_state()
        else:
            self.current_hwnd = None
            self.current_target = None
            self._draw_empty_state()
        self.refresh_devices()
        self._refresh_connection_ui()
        self._sync_actions()
        self.log("已解除绑定并释放所有按键")

    def viewer_visible(self, visible):
        target = self.current_target
        if not target:
            self._toast("请先连接设备")
            return
        try:
            target = fit_standard(target)
            self.current_target = target
            left, top_, right, bottom = winapi.window_rect(target.top)
            outer = viewer_link.plan_window_size((right - left, bottom - top_),
                                                 winapi.client_size(target.child), standard_client())
            if visible:
                winapi.set_window_size(target.top, *outer, 80, 60)
                winapi.show_noactivate(target.top)
            else:
                winapi.set_window_size(target.top, *outer, -30000, 0)
            self.log("查看器已显示；按 Ctrl+Alt+C 可直接读取它上面的鼠标" if visible else "查看器已放回屏幕外")
        except Exception as exc:
            messagebox.showerror("窗口操作失败", str(exc))
            self.log(f"窗口操作失败：{exc}")

    def _reset_frame(self):
        self.photo = None
        self.image_item = None
        self.last_png = None
        self.last_path = []
        self.last_click = None
        self.photo_size = (GAME_W, GAME_H)
        self.view_scale = 1.0
        self.source.set("起点：--")

    def _reset_hover(self):
        self.hover_point = None
        self.live_x.set("--")
        self.live_y.set("--")
        self.hover_color.set("#------")
        self._set_swatch(EDGE)

    def _show_loading_state(self, title="正在读取远程画面…", hint="画面出现后，把鼠标移上来即可实时取坐标"):
        self.photo = None
        self.image_item = None
        self.canvas.delete("all")
        self.canvas.create_text(GAME_W // 2, GAME_H // 2 - 10, text=title,
                                fill="#cbd5e1", font=(UI, 11))
        self.canvas.create_text(GAME_W // 2, GAME_H // 2 + 26, text=hint,
                                fill="#64748b", font=F_SMALL)

    def _draw_empty_state(self):
        self.photo = None
        self.image_item = None
        self.photo_size = (GAME_W, GAME_H)
        self.view_scale = 1.0
        self.canvas.delete("all")
        self.canvas.create_text(GAME_W // 2, 240, text="还没有连接远程画面",
                                fill="#e2e8f0", font=(UI, 15, "bold"))
        lines = (
            "①  按住左侧准星，拖到远程画面后松开",
            "②  鼠标移到画面上，坐标和颜色实时显示",
            "③  单击画面拾取坐标，再在右侧发送动作",
        )
        for index, line in enumerate(lines):
            self.canvas.create_text(GAME_W // 2, 296 + index * 36, text=line,
                                    fill="#94a3b8", font=F_BODY)
        self.canvas.create_text(GAME_W // 2, 424,
                                text="也可以展开“手动连接”，按地址和端口启动查看器",
                                fill="#64748b", font=F_SMALL)

    def _capture_once(self):
        if self.closing:
            return
        self._ensure_geometry()
        target = self.current_target
        if target and winapi.is_iconic(target.top):
            return
        if self.current_hwnd in self.workers and not self.capture_busy:
            self.capture_busy = True
            worker = self.worker()
            token = self.capture_token

            def done(result):
                self.capture_busy = False
                if self.closing or token != self.capture_token:
                    return
                self.last_png, path, source, click = result
                self.last_path = list(path or [])
                self.last_click = click
                try:
                    raw = tk.PhotoImage(data=self.last_png)
                    photo = raw if raw.width() <= GAME_W else raw.subsample(2)
                    self.photo = photo
                    self.photo_size = (photo.width(), photo.height())
                    self.view_scale = photo.width() / GAME_W
                    try:
                        if self.image_item is None:
                            self.image_item = self.canvas.create_image(0, 0, image=photo, anchor="nw")
                        else:
                            self.canvas.itemconfigure(self.image_item, image=photo)
                    except tk.TclError:
                        self.image_item = self.canvas.create_image(0, 0, image=photo, anchor="nw")
                    self.draw_overlays(self.last_path, self.last_click)
                    self.canvas.configure(cursor="tcross" if self.region_mode else "crosshair")
                    labels = {"detected": "画面检测", "tracked": "跟踪值",
                              "unknown": "未知（画面外进入）"}
                    self.source.set("起点：" + labels.get(source, str(source)))
                except Exception as exc:
                    self.log(f"画面解码失败：{exc}")

            def failed(exc):
                self.capture_busy = False
                if token != self.capture_token:
                    return
                now = time.monotonic()
                if now - self.last_capture_error > 4:
                    self.last_capture_error = now
                    self.log(f"抓图：{exc}")

            run_async(self.root, worker.frame, done, failed)

    def _capture_tick(self):
        if self.closing:
            return
        self._capture_once()
        interval = max(80, round(1000 / max(0.5, self.fps.get())))
        self.root.after(interval, self._capture_tick)

    def _refresh_soon(self):
        for delay in (150, 450, 900):
            self.root.after(delay, self._capture_once)

    def _canvas_to_game(self, x, y):
        if not self.photo or not self.photo_size:
            return None
        width, height = self.photo_size
        if x < 0 or y < 0 or x >= width or y >= height:
            return None
        return view_to_game(int(x), int(y), width, height)

    def canvas_move(self, event):
        point = self._canvas_to_game(event.x, event.y)
        if point is None:
            return
        self.hover_point = point
        self.live_x.set(str(point[0]))
        self.live_y.set(str(point[1]))
        self._show_local_color(*point)
        self._schedule_color(*point)

    def _display_color(self, x, y):
        if not self.photo or not self.photo_size:
            return None
        width, height = self.photo_size
        px = min(width - 1, max(0, int((x + 0.5) * width / GAME_W)))
        py = min(height - 1, max(0, int((y + 0.5) * height / GAME_H)))
        try:
            values = self.photo.get(px, py)
            r, g, b = (int(value) for value in values[:3])
        except (tk.TclError, TypeError, ValueError):
            return None
        return f"#{r:02X}{g:02X}{b:02X}"

    def _show_local_color(self, x, y):
        color = self._display_color(x, y)
        if color:
            self.hover_color.set(color)
            self._set_swatch(color)

    def _schedule_color(self, x, y):
        if self.color_job is not None:
            try:
                self.root.after_cancel(self.color_job)
            except tk.TclError:
                pass
        self.color_job = self.root.after(220, lambda: self._request_color(x, y))

    def _request_color(self, x, y):
        self.color_job = None
        if self.current_hwnd not in self.workers or self.color_busy:
            return
        if time.monotonic() - self.last_color_at < 0.12:
            return
        self.color_busy = True
        self.last_color_at = time.monotonic()
        worker = self.worker()

        def done(value):
            self.color_busy = False
            if self.hover_point != (x, y):
                return
            text = str(value).strip().lstrip("#")
            if len(text) == 6:
                try:
                    int(text, 16)
                except ValueError:
                    return
                self.hover_color.set("#" + text.upper())
                self._set_swatch("#" + text)

        def failed(_exc):
            self.color_busy = False

        run_async(self.root, lambda: worker.call("color", x, y, timeout=3.0), done, failed)

    def _set_swatch(self, color):
        try:
            self.swatch.configure(bg=color)
        except tk.TclError:
            self.swatch.configure(bg=EDGE)

    def canvas_down(self, event):
        if not self.photo:
            return
        point = self._canvas_to_game(event.x, event.y)
        if point is None:
            return
        self.press_point = (event.x, event.y)
        if self.region_mode:
            self.region_start = point
            self.drag_region = (point[0], point[1], point[0], point[1])
            self.draw_overlays()

    def canvas_drag(self, event):
        if not self.region_mode or self.region_start is None:
            return
        width, height = self.photo_size
        x = max(0, min(width - 1, event.x))
        y = max(0, min(height - 1, event.y))
        point = view_to_game(x, y, width, height)
        if point is None:
            return
        self.drag_region = (self.region_start[0], self.region_start[1], point[0], point[1])
        self.draw_overlays()

    def canvas_up(self, event):
        if self.region_mode and self.region_start is not None:
            width, height = self.photo_size
            x = max(0, min(width - 1, event.x))
            y = max(0, min(height - 1, event.y))
            end = view_to_game(x, y, width, height)
            if end is None:
                return
            x1, y1 = self.region_start
            x2, y2 = end
            left, right = min(x1, x2), max(x1, x2)
            top, bottom = min(y1, y2), max(y1, y2)
            self.region_start = None
            self.drag_region = None
            if right - left < 3 or bottom - top < 3:
                self._toast("区域太小，请重新拖动", "error")
                self.draw_overlays()
                return
            self.region = (left, top, right, bottom)
            self.region_set = True
            self._set_region_mode(False)
            self._toast(f"已框选区域 ({left}, {top}) → ({right}, {bottom})", "success")
            self.log(f"框选区域 G={self.region}")
            self.draw_overlays()
            return
        press = self.press_point
        self.press_point = None
        if press is None:
            return
        if abs(event.x - press[0]) > 6 or abs(event.y - press[1]) > 6:
            return
        point = self._canvas_to_game(event.x, event.y)
        if point is not None:
            self._apply_point(*point, copy=True)

    def draw_overlays(self, path=None, click=None):
        self.canvas.delete("overlay")
        if not self.photo:
            return
        scale = self.view_scale
        if self.grid.get():
            for x in range(0, GAME_W, 40):
                sx = x * scale
                self.canvas.create_line(sx, 0, sx, GAME_H * scale, fill="#24324a", tags="overlay")
            for y in range(0, GAME_H, 40):
                sy = y * scale
                self.canvas.create_line(0, sy, GAME_W * scale, sy, fill="#24324a", tags="overlay")
        if path:
            points = [(x * scale, y * scale) for x, y, _dt in path]
            if len(points) > 1:
                flat = [value for point in points for value in point]
                self.canvas.create_line(*flat, fill="#60a5fa", width=2, tags="overlay")
            for x, y in points[::max(1, len(points) // 25)]:
                self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#bfdbfe", outline="",
                                        tags="overlay")
        if click:
            x, y = click[0] * scale, click[1] * scale
            self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline="#fbbf24", width=2,
                                    tags="overlay")
        region = self.drag_region
        if region is None and self.region_set:
            region = self.region
        if region is not None:
            x1, y1, x2, y2 = region
            left, right = min(x1, x2) * scale, max(x1, x2) * scale
            top, bottom = min(y1, y2) * scale, max(y1, y2) * scale
            self.canvas.create_rectangle(left, top, right, bottom, outline="#38bdf8", width=2,
                                         dash=(6, 4), tags="overlay")
        try:
            x, y = parse_point(self.target_x.get(), self.target_y.get())
        except ValueError:
            return
        self.selected_point = (x, y)
        sx, sy = x * scale, y * scale
        for width, color in ((4, CANVAS_BG), (2, "#f43f5e")):
            self.canvas.create_line(sx - 10, sy, sx + 10, sy, fill=color, width=width, tags="overlay")
            self.canvas.create_line(sx, sy - 10, sx, sy + 10, fill=color, width=width, tags="overlay")
        self.canvas.create_oval(sx - 7, sy - 7, sx + 7, sy + 7, outline="#f43f5e", width=2,
                                tags="overlay")

    def _apply_point(self, x, y, *, copy=False):
        self.selected_point = (x, y)
        self.hover_point = (x, y)
        self.live_x.set(str(x))
        self.live_y.set(str(y))
        self.target_x.set(str(x))
        self.target_y.set(str(y))
        self._show_local_color(x, y)
        self._schedule_color(x, y)
        self.draw_overlays()
        copied = copy_text(f"({x}, {y})") if copy else False
        message = f"目标已设为 ({x}, {y})" + ("，已复制" if copied else "")
        self.status.set(message)
        self._toast(message, "success")

    def use_hover_point(self):
        if self.hover_point is None:
            self._toast("先把鼠标移到画面上", "error")
            return
        self._apply_point(*self.hover_point, copy=True)

    def copy_live(self):
        if self.hover_point is None:
            self._toast("先把鼠标移到画面上", "error")
            return
        x, y = self.hover_point
        if copy_text(f"({x}, {y})"):
            self.status.set(f"已复制 ({x}, {y})")
            self._toast(f"已复制 ({x}, {y})", "success")
        else:
            self._toast("复制失败，剪贴板可能被占用", "error")

    def start_mouse_read(self):
        if self.current_hwnd not in self.workers:
            self._toast("请先连接设备", "error")
            return
        if self.read_active:
            return
        self.read_active = True
        self.read_countdown = 3
        set_button_enabled(self.read_btn, False)
        self._read_tick()

    def _read_tick(self):
        if not self.read_active:
            return
        if self.closing:
            self.read_active = False
            return
        if self.current_hwnd not in self.workers:
            self.read_active = False
            self._restore_hint()
            self._sync_actions()
            return
        if self.read_countdown > 0:
            self.hint_var.set(f"{self.read_countdown} 秒后读取鼠标位置，请移到远程画面或预览上…")
            self.read_countdown -= 1
            self.root.after(1000, self._read_tick)
            return
        self.read_active = False
        self._restore_hint()
        self._sync_actions()
        self.read_cursor_position()

    def read_cursor_position(self):
        if self.current_hwnd not in self.workers:
            self._toast("请先连接设备", "error")
            return
        sx, sy = winapi.cursor_pos()
        hwnd = window_at_cursor()
        x1, y1 = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        on_canvas = (x1 <= sx < x1 + self.canvas.winfo_width()
                     and y1 <= sy < y1 + self.canvas.winfo_height())
        if on_canvas and hwnd and winapi.pid_of(hwnd) == os.getpid() and self.photo:
            point = self._canvas_to_game(sx - x1, sy - y1)
            if point is not None:
                self._apply_point(*point, copy=True)
                return
        target = self.current_target
        if target and hwnd and winapi.is_window(target.child) and winapi.pid_of(hwnd) == winapi.pid_of(target.top):
            cx, cy = screen_to_client(target.child, sx, sy)
            width, height = winapi.client_size(target.child)
            point = view_to_game(cx, cy, width, height) if 0 <= cx < width and 0 <= cy < height else None
            if point is not None:
                self._apply_point(*point, copy=True)
                return
        self._toast("没有读到坐标：请把鼠标移到预览或远程画面上", "error")
        self.status.set("未读取：鼠标不在远程画面范围内")

    def _restore_hint(self):
        if self.region_mode:
            self.hint_var.set("在画面上拖出矩形区域；按 Esc 取消")
        else:
            self.hint_var.set(self.pick_hint)

    def _set_region_mode(self, active):
        self.region_mode = active
        self.region_start = None
        self.drag_region = None
        self.press_point = None
        if active:
            self.region_btn.configure(text="取消框选", bg=BLUE_SOFT, fg=BLUE,
                                      highlightbackground="#c9d9ff")
            self.canvas.configure(cursor="tcross")
            self.hint_var.set("在画面上拖出矩形区域；按 Esc 取消")
        else:
            self.region_btn.configure(text="框选区域", bg=CARD, fg=FG, highlightbackground=EDGE)
            self.canvas.configure(cursor="crosshair" if self.current_hwnd in self.workers else "arrow")
            self._restore_hint()
        self.draw_overlays()

    def region_toggle(self):
        if self.region_mode:
            self._set_region_mode(False)
            return
        if self.current_hwnd not in self.workers:
            self._toast("请先连接设备", "error")
            return
        self._set_region_mode(True)

    def _on_escape(self, _event=None):
        if self.region_mode:
            self._set_region_mode(False)
        elif self.read_active:
            self.read_active = False
            self._restore_hint()
            self._sync_actions()

    def _hotkey_tick(self):
        if self.closing:
            return
        if self.pick_hotkey.take():
            self.read_cursor_position()
        self.root.after(120, self._hotkey_tick)

    def parsed_point(self):
        return parse_point(self.target_x.get(), self.target_y.get())

    def action(self, name, *args, needs_point=True, **kwargs):
        if self.current_hwnd not in self.workers:
            self._toast("请先连接设备", "error")
            return
        if estop.is_set():
            self._toast("已急停，先解除急停", "error")
            return
        if not self._ensure_geometry(notify=True):
            return
        try:
            worker = self.worker()
            if needs_point:
                x, y = self.parsed_point()
                args = (x, y) + args
            if name in ("move_to", "click", "double_click", "hover", "wheel"):
                kwargs["speed"] = self.speed.get()
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        labels = {"move_to": "移动指针", "click": "走到目标", "double_click": "双击", "hover": "悬停",
                  "wheel": "滚轮", "key_press": "按键", "key_combo": "组合键",
                  "type_text": "文本", "release_all": "释放按键"}
        label = labels.get(name, name)

        def done(_result):
            self.status.set(f"{label}已发送")
            self.log(f"{label}已发送到 {worker.slave}")
            self._refresh_soon()

        def failed(exc):
            self.status.set(f"{label}失败：{exc}")
            self.log(f"{label}失败：{exc}")
            self._toast(f"{label}失败：{exc}", "error")

        run_async(self.root, lambda: worker.call(name, *args, **kwargs), done, failed)

    def action_confirmed(self):
        if self.current_hwnd not in self.workers:
            self._toast("请先连接设备", "error")
            return
        if estop.is_set():
            self._toast("已急停，先解除急停", "error")
            return
        if not self._ensure_geometry(notify=True):
            return
        try:
            worker = self.worker()
            x, y = self.parsed_point()
        except Exception as exc:
            self._toast(str(exc), "error")
            return

        def work():
            before = worker.frame()[0]
            worker.call("move_to", x, y, speed=self.speed.get())
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                time.sleep(0.35)
                if worker.frame()[0] != before:
                    return True
            return False

        def done(changed):
            message = "画面有变化" if changed else "4 秒内未确认画面变化，可用截图人工核验"
            self.log(message)
            self._toast(message, "success" if changed else "info")
            self._refresh_soon()

        def failed(exc):
            self.log(f"画面确认失败：{exc}")
            self._toast(f"画面确认失败：{exc}", "error")

        run_async(self.root, work, done, failed)

    def _sync_speed_buttons(self):
        value = self.speed.get()
        for speed, button in self.speed_buttons.items():
            selected = abs(speed - value) < 0.01
            button.configure(bg=BLUE_SOFT if selected else "#ffffff",
                             fg=BLUE if selected else DIM,
                             activebackground=BLUE_SOFT if selected else "#f1f5f9",
                             activeforeground=BLUE if selected else FG,
                             highlightthickness=1,
                             highlightbackground="#bfdbfe" if selected else EDGE)

    def set_speed(self, value):
        self.speed.set(float(value))
        self._sync_speed_buttons()

    def _visual_changed(self):
        enabled = self.visual.get()
        workers = list(self.workers.values())
        for worker in workers:
            try:
                worker.set_visual(enabled)
            except Exception as exc:
                self.log(f"设置水晶箭头对齐失败：{exc}")
        message = "水晶箭头对齐已开启" if enabled else "水晶箭头对齐已关闭"
        message += f"（{len(workers)} 台设备）" if workers else "，连接设备后生效"
        self.status.set(message)
        self.log(message)

    def _align_values(self):
        try:
            return parse_align(self.align_tol.get(), self.align_max.get())
        except ValueError:
            return None

    def _align_changed(self, *_):
        values = self._align_values()
        invalid = values is None
        for entry in (getattr(self, "align_tol_entry", None), getattr(self, "align_max_entry", None)):
            if entry is not None:
                entry.configure(highlightbackground=RED if invalid else EDGE)
        if invalid:
            self.align_hint.set("容差 1–20，时间 3–120")
            return
        tol, max_s = values
        self.align_hint.set(f"自动生效 · ±{tol}px · ≤{max_s}秒")
        for worker in list(self.workers.values()):
            try:
                worker.set_align(tol, max_s)
            except Exception:
                pass

    def _apply_align_to(self, worker):
        values = self._align_values()
        if values:
            try:
                worker.set_align(*values)
            except Exception:
                pass

    def _hold_range(self):
        try:
            return parse_hold(self.hold.get())
        except ValueError:
            return None

    def _hold_changed(self, *_):
        hold = self._hold_range()
        entry = getattr(self, "hold_entry", None)
        if hold is None:
            if entry is not None:
                entry.configure(highlightbackground=RED)
            self.hold_hint.set("格式如 60,140")
            return
        if entry is not None:
            entry.configure(highlightbackground=EDGE)
        lo, hi = hold
        self.hold_hint.set(f"自动生效 · 随机 {lo}–{hi}")
        for worker in list(self.workers.values()):
            try:
                worker.submit("profile", hold_ms=(lo, hi))
            except Exception:
                pass

    def _apply_hold_to(self, worker):
        hold = self._hold_range()
        if hold:
            try:
                worker.submit("profile", hold_ms=hold)
            except Exception:
                pass

    def send_combo(self, value=None):
        keys = tuple(k.strip() for k in (value or self.combo.get()).split(",") if k.strip())
        if len(keys) < 2:
            self._toast("组合键至少两个键，例如 alt,e", "error")
            return
        self.action("key_combo", *keys, needs_point=False)

    def region_center(self):
        if not self.region_set:
            self._toast("请先在画面上框选区域", "error")
            return
        x1, y1, x2, y2 = self.region
        point = ((x1 + x2) // 2, (y1 + y2) // 2)
        self._apply_point(*point)
        self._toast(f"区域中心 ({point[0]}, {point[1]}) 已设为目标", "success")

    def drag_region(self):
        if not self.region_set:
            self._toast("请先在画面上框选区域", "error")
            return
        try:
            worker = self.worker()
            x1, y1 = self.parsed_point()
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        x2, y2 = (self.region[0] + self.region[2]) // 2, (self.region[1] + self.region[3]) // 2
        run_async(self.root, lambda: worker.call("drag", x1, y1, x2, y2),
                  lambda _r: self.log(f"拖拽完成：({x1}, {y1}) → ({x2}, {y2})"),
                  lambda exc: self._toast(f"拖拽失败：{exc}", "error"))

    def save_named_point(self):
        try:
            point = self.parsed_point()
        except ValueError as exc:
            self._toast(str(exc), "error")
            return
        name = simpledialog.askstring("保存坐标", "给这个坐标起个名字：", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        existed = name in self.points
        self.points[name] = point
        self._write_points()
        self.refresh_points()
        self._toast(f"坐标“{name}”已{'更新' if existed else '保存'}", "success")
        self.log(f"坐标 {name} = {point} 已保存")

    def _load_points(self):
        try:
            with open(POINTS_PATH, encoding="utf-8") as handle:
                return {str(k): tuple(v) for k, v in json.load(handle).items()}
        except (OSError, ValueError):
            return {}

    def _write_points(self):
        try:
            os.makedirs(os.path.dirname(POINTS_PATH), exist_ok=True)
            with open(POINTS_PATH, "w", encoding="utf-8") as handle:
                json.dump(self.points, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def refresh_points(self):
        self.point_list.delete(0, "end")
        if not self.points:
            self.point_list.insert("end", "暂无保存的坐标")
            return
        for name, (x, y) in self.points.items():
            self.point_list.insert("end", f"{name}    ({x}, {y})")

    def point_selected(self, _event=None):
        selected = self.point_list.curselection()
        if not selected or selected[0] >= len(self.points):
            return
        name = list(self.points)[selected[0]]
        self._apply_point(*self.points[name])
        self._toast(f"已填入“{name}” ({self.points[name][0]}, {self.points[name][1]})", "success")

    def use_selected_point(self):
        selected = self.point_list.curselection()
        if not selected or selected[0] >= len(self.points):
            self._toast("先在列表里选一个坐标", "error")
            return
        name = list(self.points)[selected[0]]
        self._apply_point(*self.points[name])
        self._toast(f"已填入“{name}”", "success")

    def delete_point(self):
        selected = self.point_list.curselection()
        if not selected or selected[0] >= len(self.points):
            self._toast("先在列表里选一个坐标", "error")
            return
        name = list(self.points)[selected[0]]
        self.points.pop(name, None)
        self._write_points()
        self.refresh_points()
        self._toast(f"已删除“{name}”")
        self.log(f"坐标 {name} 已删除")

    def save_region(self):
        if not self.region_set:
            self._toast("请先在画面上框选区域", "error")
            return
        x1, y1, x2, y2 = self.region
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG 图片", "*.png")],
                                            initialfile=f"区域_{x1}_{y1}_{x2}_{y2}.png")
        if not path:
            return
        try:
            worker = self.worker()
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        run_async(self.root, lambda: worker.call("region", x1, y1, x2, y2, path),
                  lambda _r: self.log(f"区域模板已保存：{path}"),
                  lambda exc: self._toast(f"区域保存失败：{exc}", "error"))

    def export_path(self):
        try:
            state = self.worker().state()
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV 表格", "*.csv")],
                                            initialfile="轨迹.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(("x", "y", "dt_ms"))
                writer.writerows(state["path"])
        except OSError as exc:
            self._toast(f"导出失败：{exc}", "error")
            return
        self.log(f"轨迹已导出：{path}")
        self._toast("轨迹已导出", "success")

    def clear_path(self):
        self.last_path = []
        self.last_click = None
        self.draw_overlays()
        if self.current_hwnd in self.workers:
            worker = self.worker()

            def done(_result):
                self.last_path = []
                self.draw_overlays()
                self._toast("轨迹已清空")

            run_async(self.root, lambda: worker.call("clear_path", timeout=2.0), done,
                      lambda exc: self.log(f"清空轨迹失败：{exc}"))
        else:
            self._toast("轨迹已清空")

    def replay(self):
        try:
            worker = self.worker()
        except Exception as exc:
            self._toast(str(exc), "error")
            return

        def done(state):
            path = list(state["path"] or [])
            self.last_path = path
            self.last_click = state["click"]
            self.draw_overlays(path, self.last_click)
            if not path:
                self._toast("还没有可回放的轨迹", "info")
                return
            intervals = [dt for _x, _y, dt in path if dt]
            timing = ""
            if intervals:
                timing = f"，间隔 {min(intervals)}~{max(intervals)} ms / 平均 {sum(intervals) / len(intervals):.1f} ms"
            labels = {"detected": "画面检测", "tracked": "跟踪值", "unknown": "未知（画面外进入）"}
            self.source.set("起点：" + labels.get(state["start_source"], str(state["start_source"])))
            self.log(f"回放最近轨迹：{len(path)} 点{timing}，点击={state['click']}")
            self._toast(f"已回放 {len(path)} 个轨迹点", "success")

        run_async(self.root, worker.state, done, lambda exc: self._toast(f"回放失败：{exc}", "error"))

    def calibrate(self):
        if self.calibrating:
            return
        try:
            worker = self.worker()
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        self.calibrating = True
        for widget in self.action_widgets:
            set_button_enabled(widget, False)
        self.status.set("九点校准进行中，请勿操作远程窗口…")
        self.calib_text.configure(state="normal")
        self.calib_text.delete("1.0", "end")
        self.calib_text.insert("1.0", "校准中，请稍候…")
        self.calib_text.configure(state="disabled")

        def done(result):
            self.calibrating = False
            rows, passed, detail = result
            self.calib_text.configure(state="normal")
            self.calib_text.delete("1.0", "end")
            for index, row in enumerate(rows, 1):
                self.calib_text.insert(
                    "end", f"{index} 目标{row.target}\n  检测{row.detected}\n"
                           f"  偏差{row.deviation}  {row.detect_ms}ms\n")
            self.calib_text.insert("end", f"\n{'PASS' if passed else 'FAIL'}：{detail}\n"
                                          "E2 不通过时按规范使用跟踪值兜底。")
            self.calib_text.configure(state="disabled")
            self._sync_actions()
            self.log(f"九点校准结束：{detail}")
            self._toast("九点校准结束", "success" if passed else "info")

        def failed(exc):
            self.calibrating = False
            self.calib_text.configure(state="normal")
            self.calib_text.delete("1.0", "end")
            self.calib_text.insert("1.0", f"校准失败：{exc}")
            self.calib_text.configure(state="disabled")
            self._sync_actions()
            self.log(f"九点校准失败：{exc}")
            self._toast(f"校准失败：{exc}", "error")

        run_async(self.root, lambda: worker.call("calibrate", timeout=120), done, failed)

    def diagnostics(self):
        bits = struct.calcsize("P") * 8
        target = self.current_target
        text = (f"Python：{bits} 位\n管理员：{winapi.is_admin()}\nDPI：{display_scale():g}×\n"
                f"已绑定：{len(self.workers)} 台\n水晶箭头对齐：{'开' if self.visual.get() else '关'}\n"
                f"急停热键：{self.hotkey_ok}\n取坐标热键：{self.pick_hotkey_ok}\n"
                f"当前目标：{target.address if target else '无'}\n")
        if target:
            size = winapi.client_size(target.child)
            text += f"窗口：{target.title}\n子窗口：{target.child} · {size[0]}×{size[1]}\n"
        text += "最近日志：\n" + "\n".join(self.logs)
        if copy_text(text):
            self._toast("诊断信息已复制（不含密码）", "success")
        else:
            messagebox.showinfo("诊断信息", text)

    def emergency(self):
        estop.trigger("界面急停按钮")
        for worker in self.workers.values():
            worker.estop()
        self.log("急停已触发：停止后续动作，工作线程将释放所有键")
        self._update_estop_ui(True)

    def clear_estop(self):
        estop.clear()
        self._update_estop_ui(False)

        def work():
            for worker in list(self.workers.values()):
                try:
                    worker.call("release_all", timeout=3)
                    worker.call("reset_cursor", timeout=3)
                except Exception:
                    pass

        run_async(self.root, work, lambda _r: self.log("急停已解除，按键已释放，起点已重置为未知"),
                  lambda exc: self.log(f"解除急停时出错：{exc}"))
        self.log("急停已解除，正在释放按键并重置起点")

    def _update_estop_ui(self, active):
        self.estop_active = active
        if active:
            if not self.estop_banner.winfo_manager():
                self.estop_banner.pack(fill="x", after=self.header)
            self._set_pill("已急停", RED, RED_SOFT)
            self.status.set("已急停 · 新动作被拒绝；确认现场后点击“解除急停”")
            self.source.set("起点：急停")
            self._sync_actions()
        else:
            if self.estop_banner.winfo_manager():
                self.estop_banner.pack_forget()
            self.source.set("起点：未知")
            self._refresh_connection_ui()
            self._sync_actions()

    def _estop_tick(self):
        if self.closing:
            return
        active = estop.is_set()
        if active != self.estop_active:
            self._update_estop_ui(active)
        self.root.after(300, self._estop_tick)

    def _toast(self, text, kind="info"):
        if self.closing:
            return
        if hasattr(self, "toast_label") and self.toast_label.winfo_exists():
            self.toast_label.destroy()
        colors = {"success": "#166534", "error": RED_DARK, "info": "#111827"}
        self.toast_label = tk.Label(self.canvas, text=text, bg=colors.get(kind, "#111827"),
                                    fg="#ffffff", font=F_SMALL, padx=12, pady=6)
        self.toast_label.place(relx=0.5, rely=0.94, anchor="s")
        self.toast_label.lift()
        self.root.after(1800, lambda: self.toast_label.destroy()
                        if hasattr(self, "toast_label") and self.toast_label.winfo_exists() else None)

    def log(self, text):
        line = time.strftime("%H:%M:%S") + "  " + str(text)
        self.logs.append(line)
        self.status.set(str(text))
        if hasattr(self, "log_text"):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", "\n".join(self.logs))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def worker(self):
        if self.current_hwnd not in self.workers:
            raise RuntimeError("请先绑定一台设备")
        return self.workers[self.current_hwnd]

    def close(self):
        if self.closing:
            return
        self.closing = True
        try:
            self.pick_hotkey.stop()
        except Exception:
            pass
        estop.trigger("测试工具退出")
        for worker in list(self.workers.values()):
            try:
                worker.stop()
            except Exception:
                pass
        try:
            self.highlight.destroy()
        except Exception:
            pass
        self.root.destroy()


def relaunch_admin():
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
        exe = sys.executable
    else:
        params = subprocess.list2cmdline([os.path.abspath(__file__), *sys.argv[1:]])
        exe = sys.executable
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


def main():
    winapi.set_dpi_aware()
    root = tk.Tk()
    if struct.calcsize("P") * 8 != 32:
        messagebox.showerror("位数不符", "后台鼠键必须使用 32 位 Python 或打包后的 kmtool.exe。")
        root.destroy()
        return 2
    if not winapi.is_admin():
        if messagebox.askyesno("需要管理员权限", "大漠注册需要管理员权限。是否现在重新以管理员身份启动？"):
            relaunch_admin()
        root.destroy()
        return 0
    KmTool(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
