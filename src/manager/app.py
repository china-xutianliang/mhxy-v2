"""远程控制管理台：一个窗口同时看所有设备的远程画面。

  总览：所有设备铺成一张表（自动铺满 / 每行 1~4 台），每格有状态和实时画面。
  放大：单击任意一格，放大看这一台，右边保留其他设备的小图，可一键切换；Esc 返回总览。
  这里只做"看"和"管理连接"，不向设备注入任何东西。

只在主控端运行；设备端只需要远程画面服务（TightVNC 服务端和共享助手）。
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import customtkinter as ctk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import winapi  # noqa: E402
from common.pngio import write_png  # noqa: E402
from control import viewer_link as vl  # noqa: E402
from manager import layout, status, uikit  # noqa: E402
from manager.machines import Machine, Store  # noqa: E402
from manager.session import CONNECTING, ERROR, IDLE, LOST, ONLINE, Session  # noqa: E402

LAYOUTS = {"自动铺满": "auto", "每行 1 台": "cols:1", "每行 2 台": "cols:2", "每行 3 台": "cols:3", "每行 4 台": "cols:4"}
QUALITY = {"流畅 · 5 帧/秒": "smooth", "均衡 · 2 帧/秒": "balanced", "省电 · 1 帧/秒": "saver"}
TILE_FPS = {"smooth": 5.0, "balanced": 2.0, "saver": 1.0}
FOCUS_FPS = {"smooth": 8.0, "balanced": 5.0, "saver": 3.0}
THUMB_FPS = 1.0
HIDDEN_FPS = 0.5
HIDDEN_SIZE = (160, 120)
TITLE = "远程控制管理台 v1.6"


def _key(mapping: dict, value: str) -> str:
    return next((k for k, v in mapping.items() if v == value), next(iter(mapping)))


def _act_style(state: str) -> tuple[str, str, str]:
    """操作按钮的（底色、悬停色、文字色）。"""
    if state == IDLE:
        return uikit.ACCENT, uikit.ACCENT_HOVER, uikit.ACCENT_TEXT
    if state in (LOST, ERROR):
        level = "bad" if state == ERROR else "warn"
        return uikit.PILL_FILL[level], uikit.INPUT_HOVER, uikit.LEVEL[level]
    return uikit.INPUT_HOVER, uikit.EDGE, uikit.FG_DIM


def _side_action(state: str) -> tuple[str, str, str, str]:
    """侧栏小按钮的（图标、图标色、底色、悬停色）。"""
    if state == IDLE:
        return "play", "#ffffff", uikit.ACCENT, uikit.ACCENT_HOVER
    if state in (LOST, ERROR):
        level = "bad" if state == ERROR else "warn"
        return "refresh", uikit.LEVEL[level], uikit.PILL_FILL[level], uikit.INPUT_HOVER
    return "stop", uikit.FG_DIM, uikit.INPUT, uikit.INPUT_HOVER


# ---------------------------------------------------------------- 单个格子
class Tile:
    def __init__(self, app: "ManagerApp", parent: tk.Misc, machine: Machine, thumb: bool = False) -> None:
        self.app, self.machine, self.thumb = app, machine, thumb
        self.seq = -1
        self._photo: tk.PhotoImage | None = None
        self._ph_sig = None
        ui = app.ui
        self.pad = int((uikit.THUMB_PAD if thumb else uikit.TILE_PAD) * ui)
        self.head_h = int((uikit.THUMB_HEAD if thumb else uikit.TILE_HEAD) * ui)
        self.gap = int((uikit.THUMB_GAP if thumb else uikit.TILE_GAP) * ui)
        self.foot_h = 0 if thumb else int(uikit.TILE_FOOT * ui)
        self.chrome = uikit.tile_chrome(ui, thumb)

        self.frame = tk.Frame(parent, bg=uikit.BG, bd=0, highlightthickness=0)
        self.frame.pack_propagate(False)
        self.card = ctk.CTkFrame(self.frame, fg_color=uikit.CARD, bg_color=uikit.BG, corner_radius=10,
                                 border_width=2, border_color=uikit.EDGE_SOFT)
        self.card.pack(fill="both", expand=True)

        self.head = tk.Frame(self.card, bg=uikit.CARD)
        self.dot = tk.Label(self.head, text="●", bg=uikit.CARD, fg=status.LIGHT["idle"], font=app.f_dot)
        self.dot.pack(side="left")
        self.name = tk.Label(self.head, text=machine.name, bg=uikit.CARD, fg=uikit.FG,
                             font=app.f_small_b if thumb else app.f_bold, anchor="w")
        self.name.pack(side="left", padx=(int(4 * ui), 0))
        self.addr: tk.Label | None = None
        if not thumb:
            self.addr = tk.Label(self.head, text=machine.address(), bg=uikit.CARD, fg=uikit.FG_FAINT, font=app.f_small)
            self.addr.pack(side="right")
        self.state = uikit.StatusPill(self.head, font=app.cf_pill, height=20 if not thumb else 18,
                                      icon="dot", icon_size=6, pad=(6, 7))
        self.state.pack(side="right", padx=(0, int(6 * ui) if not thumb else 0))
        self._name_font = tkfont.Font(root=app.root, font=self.name.cget("font"))
        self._addr_font = tkfont.Font(root=app.root, font=self.addr.cget("font")) if self.addr else None
        self._addr_on: bool | None = None
        self._state_on: bool | None = None
        self._line_on: bool | None = None

        self.box = tk.Frame(self.card, bg=uikit.PIC_BG)
        self.canvas = tk.Canvas(self.box, bg=uikit.PIC_BG, bd=0, highlightthickness=0, takefocus=0)
        self.canvas.pack(fill="both", expand=True)

        self.foot: tk.Frame | None = None
        self.foot_lbl: tk.Label | None = None
        self.btn: ctk.CTkButton | None = None
        if not thumb:
            self.foot = tk.Frame(self.card, bg=uikit.CARD)
            self.foot_lbl = tk.Label(self.foot, bg=uikit.CARD, fg=uikit.FG_DIM, font=app.f_small,
                                     anchor="w", justify="left")
            self.foot_lbl.pack(side="left", fill="x", expand=True)
            self.btn = ctk.CTkButton(self.foot, text="连接", font=app.cf_small, height=22, width=58,
                                     corner_radius=6, fg_color=uikit.ACCENT, hover_color=uikit.ACCENT_HOVER,
                                     bg_color=uikit.CARD, text_color="#ffffff",
                                     command=lambda: app.toggle(machine.id))
            self.btn.pack(side="right")

        self._bound = [w for w in (self.frame, self.card, self.head, self.dot, self.name, self.addr,
                                   self.box, self.canvas, self.foot, self.foot_lbl) if w is not None]
        for w in self._bound:
            w.bind("<Button-1>", lambda _e, i=machine.id: app.show_focus(i))
            w.bind("<Button-3>", lambda e, i=machine.id: app.popup_menu(i, e.x_root, e.y_root))
        self.set_cursor("hand2")
        self.set_layout(160, 120)

    def _clickable(self) -> list:
        return self._bound

    def set_cursor(self, cursor: str) -> None:
        for w in self._bound:
            try:
                w.configure(cursor=cursor)
            except tk.TclError:
                pass
        try:
            self.card._canvas.configure(cursor=cursor)
        except (AttributeError, tk.TclError):
            pass

    def set_layout(self, img_w: int, img_h: int, card_w: int | None = None) -> None:
        img_w, img_h = max(40, int(img_w)), max(30, int(img_h))
        card_w = max(60, int(card_w or (img_w + 2 * self.pad)))
        if (img_w, img_h) != getattr(self, "img_size", None):
            self.img_size = (img_w, img_h)
            self.seq = -1
            self._clear()
        self.card_w, self.card_h = card_w, img_h + self.chrome
        self.frame.configure(width=self.card_w, height=self.card_h)
        inner = max(20, card_w - 2 * self.pad)
        self.head.place(x=self.pad, y=self.pad, width=inner, height=self.head_h)
        self.box_w = inner
        box_y = self.pad + self.head_h + self.gap
        self.box.place(x=self.pad, y=box_y, width=inner, height=img_h)
        if self.foot is not None:
            self.foot.place(x=self.pad, y=box_y + img_h + self.gap, width=inner, height=self.foot_h)
        self._fit_head(inner)

    def _fit_name(self, avail: int) -> str:
        text = self.machine.name
        if self._name_font.measure(text) <= avail:
            return text
        while text and self._name_font.measure(text + "…") > avail:
            text = text[:-1]
        return f"{text}…" if text else "…"

    def _fit_head(self, inner: int) -> None:
        """格子变窄时先收地址、再收状态胶囊和页脚说明，名称过长就省略。"""
        ui = self.app.ui
        compact = inner < int(235 * ui)
        tiny = inner < int(165 * ui)
        want_addr = self.addr is not None and not compact
        want_state = not tiny
        if (want_addr, want_state) != (self._addr_on, self._state_on):
            self._addr_on, self._state_on = want_addr, want_state
            if self.addr is not None:
                self.addr.pack_forget()
            self.state.pack_forget()
            if self.addr is not None and want_addr:
                self.addr.pack(side="right")
            if want_state:
                self.state.pack(side="right", padx=(0, int(6 * ui) if not self.thumb else 0))
        if self.foot_lbl is not None:
            want_line = inner >= int(180 * ui)
            if want_line != self._line_on:
                self._line_on = want_line
                if want_line:
                    self.foot_lbl.pack(side="left", fill="x", expand=True)
                else:
                    self.foot_lbl.pack_forget()
        reserve = int(18 * ui) + int(12 * ui)
        if want_state:
            reserve += int(64 * ui)
        if want_addr:
            reserve += self._addr_font.measure(self.machine.address()) + int(8 * ui)
        self.name.configure(text=self._fit_name(max(int(24 * ui), inner - reserve)))

    def set_size(self, w: int, h: int) -> None:
        self.set_layout(w, h)

    def selected(self, on: bool) -> None:
        self.card.configure(border_color=uikit.SELECT if on else uikit.EDGE_SOFT)

    def _clear(self) -> None:
        self.canvas.delete("all")
        self._photo = None
        self._ph_sig = None

    def _show(self, frame) -> None:
        img = tk.PhotoImage(data=frame.ppm)
        self.canvas.delete("all")
        self.canvas.create_image(self.box_w // 2, self.img_size[1] // 2, image=img)
        self._photo = img
        self._ph_sig = None

    def _placeholder(self, short: str, level: str) -> None:
        w, h = self.box_w, self.img_size[1]
        sig = (short, level, w, h)
        if sig == self._ph_sig:
            return
        self._ph_sig = sig
        self._photo = None
        uikit.draw_placeholder(self.canvas, w, h, short, level, self.app.f_small, self.app.ui)

    def refresh(self, sess: Session) -> None:
        level, short, line = status.describe(sess.state, sess.message, sess.fps, sess.frame_age(), sess.static_for())
        self.dot.configure(fg=status.LIGHT[level])
        self.state.set(level, short)
        if self.btn is not None:
            fill, hover, text = _act_style(sess.state)
            self.btn.configure(text=status.action_label(sess.state), fg_color=fill, hover_color=hover, text_color=text)
        if self.foot_lbl is not None:
            self.foot_lbl.configure(text=line)
        f = sess.frame
        if (sess.state == ONLINE and f is not None
                and abs(f.w - self.img_size[0]) <= 2 and abs(f.h - self.img_size[1]) <= 2):
            if f.seq != self.seq:
                self.seq = f.seq
                self._show(f)
            return
        self.seq = -1
        self._placeholder(short, level)

    def destroy(self) -> None:
        self.frame.destroy()


# ---------------------------------------------------------------- 对话框
class MachineDialog:
    def __init__(self, app: "ManagerApp", machine: Machine | None) -> None:
        self.result: Machine | None = None
        self.app = app
        ui = app.ui
        top = self.top = tk.Toplevel(app.root)
        top.title("添加设备" if machine is None else "修改设备")
        top.transient(app.root)
        top.resizable(False, False)
        top.configure(bg=uikit.CARD)

        wrap = tk.Frame(top, bg=uikit.CARD)
        wrap.pack(padx=int(24 * ui), pady=int(20 * ui))
        head = tk.Frame(wrap, bg=uikit.CARD)
        head.pack(fill="x", pady=(0, int(14 * ui)))
        ctk.CTkLabel(head, text="", image=uikit.make_icon("devices", 18, uikit.ACCENT),
                     fg_color=uikit.CARD, bg_color=uikit.CARD).pack(side="left")
        tk.Label(head, text="设备连接信息", bg=uikit.CARD, fg=uikit.FG, font=app.f_bold).pack(
            side="left", padx=(int(8 * ui), 0))

        self.v_name = tk.StringVar(value=machine.name if machine else app.store.next_default_name())
        self.v_host = tk.StringVar(value=machine.host if machine else "")
        self.v_port = tk.StringVar(value=str(machine.port) if machine else "5900")
        self.v_pw = tk.StringVar(value=machine.password if machine else "")
        self.v_rem = tk.BooleanVar(value=machine.remember if machine else False)

        form = tk.Frame(wrap, bg=uikit.CARD)
        form.pack(fill="x")
        form.grid_columnconfigure(1, weight=1)
        rows = (("名称", self.v_name, ""), ("地址", self.v_host, ""), ("端口", self.v_port, ""),
                ("VNC 密码", self.v_pw, "*"))
        for i, (lab, var, show) in enumerate(rows):
            tk.Label(form, text=lab, bg=uikit.CARD, fg=uikit.FG_DIM, font=app.f_small).grid(
                row=i, column=0, sticky="w", pady=int(6 * ui), padx=(0, int(14 * ui)))
            entry = ctk.CTkEntry(form, textvariable=var, font=app.cf_base, height=34, width=280, show=show,
                                 fg_color=uikit.INPUT, bg_color=uikit.CARD, border_color=uikit.EDGE,
                                 text_color=uikit.FG, corner_radius=7)
            entry.grid(row=i, column=1, sticky="we", pady=int(6 * ui))
            if i == 1:
                entry.focus_set()
        tk.Label(wrap, text="地址填设备的 IP（例如 Tailscale 的 100.x.x.x）。\nVNC 密码：设备没设密码就留空。",
                 bg=uikit.CARD, fg=uikit.FG_FAINT, font=app.f_tiny, justify="left",
                 wraplength=int(390 * ui)).pack(anchor="w", pady=(int(6 * ui), 0))
        ctk.CTkCheckBox(wrap, text="记住密码（加密保存在本机，仅当前 Windows 用户能读）", variable=self.v_rem,
                        font=app.cf_small, text_color=uikit.FG_DIM, fg_color=uikit.ACCENT,
                        hover_color=uikit.ACCENT_HOVER, border_color=uikit.EDGE, bg_color=uikit.CARD,
                        checkbox_width=18, checkbox_height=18, corner_radius=4).pack(
            anchor="w", pady=(int(12 * ui), 0))

        bar = tk.Frame(wrap, bg=uikit.CARD)
        bar.pack(fill="x", pady=(int(18 * ui), 0))
        ctk.CTkButton(bar, text="取消", font=app.cf_bold, width=88, height=34, corner_radius=7,
                      fg_color=uikit.INPUT, hover_color=uikit.INPUT_HOVER, text_color=uikit.FG_DIM,
                      bg_color=uikit.CARD, command=top.destroy).pack(side="right")
        ctk.CTkButton(bar, text="保存", font=app.cf_bold, width=96, height=34, corner_radius=7,
                      fg_color=uikit.ACCENT, hover_color=uikit.ACCENT_HOVER, text_color="#ffffff",
                      bg_color=uikit.CARD, command=self.on_ok).pack(side="right", padx=(0, int(8 * ui)))

        self.machine = machine
        top.bind("<Return>", lambda _e: self.on_ok())
        top.bind("<Escape>", lambda _e: top.destroy())
        top.update_idletasks()
        px = app.root.winfo_rootx() + (app.root.winfo_width() - top.winfo_width()) // 2
        py = app.root.winfo_rooty() + (app.root.winfo_height() - top.winfo_height()) // 3
        top.geometry(f"+{max(0, px)}+{max(0, py)}")
        uikit.set_window_icon(top)
        uikit.set_light_titlebar(top)
        top.grab_set()
        app.root.wait_window(top)

    def on_ok(self) -> None:
        name, host = self.v_name.get().strip(), self.v_host.get().strip()
        try:
            port = int(self.v_port.get().strip())
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            messagebox.showwarning("端口不对", "端口要填 1 到 65535 的数字，一般是 5900。", parent=self.top)
            return
        if not host:
            messagebox.showwarning("还没填地址", "请填设备的 IP 地址。", parent=self.top)
            return
        if not name:
            name = host
        m = self.machine or Machine(name=name, host=host)
        m.name, m.host, m.port = name, host, port
        m.password, m.remember = self.v_pw.get(), self.v_rem.get()
        self.result = m
        self.top.destroy()


# ---------------------------------------------------------------- 主界面
class ManagerApp:
    def __init__(self, store: Store | None = None, viewer_exe: str = vl.DEFAULT_VIEWER_EXE,
                 park: bool = True, persist: bool = True) -> None:
        self.persist = persist
        self.store = store or Store()
        if store is None:
            self.store.load()
        self.viewer_exe, self.park = viewer_exe, park
        self.sessions: dict[str, Session] = {}
        self.tiles: dict[str, Tile] = {}
        self.thumbs: dict[str, Tile] = {}
        self.focus_id: str | None = None
        self._layout_key = None
        self._tick_n = 0
        self.scale = max(1.0, winapi.user32.GetDpiForSystem() / 96.0) if hasattr(winapi.user32, "GetDpiForSystem") else 1.0
        self.ui = self.scale

        ctk.set_appearance_mode("light")
        self.root = ctk.CTk(fg_color=uikit.BG)
        self.root.title(TITLE)
        self.root.tk.call("tk", "scaling", self.ui * 96 / 72)
        uikit.set_window_icon(self.root)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(1380, int(sw * 0.94 / self.ui))
        h = min(780, int(sh * 0.9 / self.ui))
        x, y = max(0, (sw - int(w * self.ui)) // 2), max(0, (sh - int(h * self.ui)) // 4)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(1000, 560)
        self._fonts()
        self._build()
        self.rebuild()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda _e: self.show_overview())
        self.root.bind("<Left>", lambda _e: self.step_focus(-1))
        self.root.bind("<Right>", lambda _e: self.step_focus(1))
        self.root.after(150, lambda: uikit.set_light_titlebar(self.root))
        self.root.after(700, lambda: uikit.set_light_titlebar(self.root))
        self.root.after(200, self.tick)

    # ---- 外观
    def _fonts(self) -> None:
        fam = "Microsoft YaHei UI"
        try:
            if fam not in tkfont.families(self.root):
                fam = tkfont.nametofont("TkDefaultFont").cget("family")
        except tk.TclError:
            fam = "Segoe UI"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family=fam, size=10)
            except tk.TclError:
                pass
        self.f_base, self.f_small = (fam, 10), (fam, 9)
        self.f_tiny = (fam, 8)
        self.f_bold, self.f_small_b = (fam, 10, "bold"), (fam, 9, "bold")
        self.f_dot, self.f_title = (fam, 10), (fam, 16, "bold")
        self.f_bar = (fam, 14, "bold")
        self.f_big = (fam, 18, "bold")
        self.cf_micro = ctk.CTkFont(family=fam, size=10)
        self.cf_small = ctk.CTkFont(family=fam, size=11)
        self.cf_base = ctk.CTkFont(family=fam, size=12)
        self.cf_bold = ctk.CTkFont(family=fam, size=12, weight="bold")
        self.cf_pill = ctk.CTkFont(family=fam, size=11)
        self.f_select = tkfont.Font(family=fam, size=-max(11, int(round(11 * self.ui))))

    def _build(self) -> None:
        ui = self.ui
        self._build_header()

        self.v_status = tk.StringVar(value="就绪 · 选择设备后点“全部连接”开始监控")
        foot = tk.Frame(self.root, bg=uikit.FOOTER, height=int(28 * ui))
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        tk.Frame(self.root, bg=uikit.EDGE, height=max(1, int(ui))).pack(fill="x", side="bottom")
        tk.Label(foot, textvariable=self.v_status, bg=uikit.FOOTER, fg=uikit.FG_DIM, font=self.f_small,
                 anchor="w").pack(side="left", padx=int(14 * ui), fill="x", expand=True)
        tk.Label(foot, text="单击画面放大 · 右键更多操作 · ← → 切换 · Esc 返回", bg=uikit.FOOTER,
                 fg=uikit.FG_FAINT, font=self.f_small).pack(side="right", padx=int(14 * ui))

        main = tk.Frame(self.root, bg=uikit.BG)
        main.pack(fill="both", expand=True)
        self._build_side(main)

        self.content = tk.Frame(main, bg=uikit.BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.content.bind("<Configure>", lambda _e: self._mark_dirty())

        self.empty = tk.Frame(self.content, bg=uikit.BG)
        holder = tk.Frame(self.empty, bg=uikit.BG)
        holder.pack(expand=True)
        ctk.CTkLabel(holder, text="", image=uikit.make_empty_art(132), fg_color=uikit.BG,
                     bg_color=uikit.BG).pack(pady=(0, int(10 * ui)))
        tk.Label(holder, text="还没有添加设备", bg=uikit.BG, fg=uikit.FG, font=self.f_title).pack()
        tk.Label(holder, text="先在设备上运行远程画面服务，再把设备地址添加到这里。\n添加后点“全部连接”，所有设备的画面会出现在这张表里。",
                 bg=uikit.BG, fg=uikit.FG_DIM, font=self.f_base, justify="center").pack(pady=(int(8 * ui), int(18 * ui)))
        ctk.CTkButton(holder, text="添加第一台设备", image=uikit.make_icon("plus", 13, "#ffffff"),
                      compound="left", font=self.cf_bold, height=38, width=180, corner_radius=8,
                      fg_color=uikit.ACCENT, hover_color=uikit.ACCENT_HOVER, text_color="#ffffff",
                      bg_color=uikit.BG, command=self.add_machine).pack()
        tk.Label(holder, text="支持自动铺满 / 每行 1~4 台；右键卡片可重连、截图、调整顺序。",
                 bg=uikit.BG, fg=uikit.FG_FAINT, font=self.f_small).pack(pady=(int(14 * ui), 0))

        self._build_focus()

    def _build_header(self) -> None:
        ui = self.ui
        bar = tk.Frame(self.root, bg=uikit.HEADER, height=int(62 * ui))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Frame(self.root, bg=uikit.EDGE, height=max(1, int(ui))).pack(fill="x")

        # 右侧动作先 pack：窗口变窄时，中间的布局/画面设置先让位
        right = tk.Frame(bar, bg=uikit.HEADER)
        right.pack(side="right", fill="y", padx=(0, int(16 * ui)))
        self.v_banner = tk.StringVar(value="")
        self.summary_pill = uikit.StatusPill(right, font=self.cf_pill, height=28, icon="dot", icon_size=7,
                                             pad=(10, 12))
        self.summary_pill.pack(side="left", padx=(0, 6))
        self.summary_pill.set("idle", "还没有添加设备")
        self.btn_off = ctk.CTkButton(right, text="全部断开", image=uikit.make_icon("stop", 11, uikit.FG_DIM),
                                     compound="left", font=self.cf_bold, height=30, width=86, corner_radius=8,
                                     fg_color=uikit.INPUT, hover_color=uikit.INPUT_HOVER, text_color=uikit.FG_DIM,
                                     border_width=1, border_color=uikit.EDGE, bg_color=uikit.HEADER,
                                     command=self.disconnect_all)
        self.btn_off.pack(side="left", padx=(8, 0))
        self.btn_all = ctk.CTkButton(right, text="全部连接", image=uikit.make_icon("play", 12, "#ffffff"),
                                     compound="left", font=self.cf_bold, height=30, width=92, corner_radius=8,
                                     fg_color=uikit.ACCENT, hover_color=uikit.ACCENT_HOVER, text_color="#ffffff",
                                     bg_color=uikit.HEADER, command=self.connect_all)
        self.btn_all.pack(side="left", padx=(6, 0))
        self.btn_add = ctk.CTkButton(right, text="添加设备", image=uikit.make_icon("plus", 12, uikit.FG_DIM),
                                     compound="left", font=self.cf_small, height=30, width=98, corner_radius=8,
                                     fg_color=uikit.HEADER, hover_color=uikit.INPUT, text_color=uikit.FG_DIM,
                                     border_width=1, border_color=uikit.EDGE, bg_color=uikit.HEADER,
                                     command=self.add_machine)
        self.btn_add.pack(side="left", padx=(6, 0))

        mid = tk.Frame(bar, bg=uikit.HEADER)
        mid.pack(side="left", padx=(int(20 * ui), 0), pady=int(16 * ui))
        self.v_layout = tk.StringVar(value=_key(LAYOUTS, self.store.settings["layout"]))
        self.sel_layout = uikit.Select(mid, list(LAYOUTS), self.v_layout, width=100, height=30,
                                       font=self.f_select, ui=ui, bg=uikit.HEADER,
                                       command=self.on_setting)
        self.sel_layout.pack(side="left", padx=(0, int(12 * ui)))
        self.v_quality = tk.StringVar(value=_key(QUALITY, self.store.settings["quality"]))
        self.sel_quality = uikit.Select(mid, list(QUALITY), self.v_quality, width=124, height=30,
                                        font=self.f_select, ui=ui, bg=uikit.HEADER,
                                        command=self.on_setting)
        self.sel_quality.pack(side="left", padx=(0, int(16 * ui)))
        self.v_auto = tk.BooleanVar(value=self.store.settings["auto_reconnect"])
        self.chk_auto = ctk.CTkCheckBox(mid, text="掉线自动重连", variable=self.v_auto, command=self.on_setting,
                                        font=self.cf_small, text_color=uikit.FG_DIM, fg_color=uikit.ACCENT,
                                        hover_color=uikit.ACCENT_HOVER, border_color=uikit.EDGE,
                                        bg_color=uikit.HEADER, checkbox_width=17, checkbox_height=17,
                                        corner_radius=4)
        self.chk_auto.pack(side="left")
        bar.bind("<Configure>", self._fit_header)

    def _fit_header(self, event) -> None:
        """窄窗口收起右上角“添加设备”，侧栏的“＋”仍可添加。"""
        w = int(event.width / max(1.0, self.ui))
        if w == getattr(self, "_bar_w", -1):
            return
        self._bar_w = w
        if w < 1040:
            if self.btn_add.winfo_manager():
                self.btn_add.pack_forget()
        elif not self.btn_add.winfo_manager():
            self.btn_add.pack(side="left", padx=(6, 0))

    def _build_side(self, main: tk.Frame) -> None:
        ui = self.ui
        self.side = tk.Frame(main, bg=uikit.SIDE, width=int(244 * ui))
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        tk.Frame(main, bg=uikit.EDGE, width=max(1, int(ui))).pack(side="left", fill="y")

        head = tk.Frame(self.side, bg=uikit.SIDE, height=int(46 * ui))
        head.pack(fill="x")
        head.pack_propagate(False)
        ctk.CTkButton(head, text="", image=uikit.make_icon("plus", 12, uikit.FG_DIM), width=26, height=26,
                      corner_radius=7, fg_color=uikit.SIDE, hover_color=uikit.INPUT_HOVER,
                      border_width=1, border_color=uikit.EDGE, bg_color=uikit.SIDE,
                      command=self.add_machine).pack(side="right", padx=int(10 * ui))
        self.v_side_count = tk.StringVar(value="0 台")
        tk.Label(head, textvariable=self.v_side_count, bg=uikit.SIDE, fg=uikit.FG_FAINT,
                 font=self.f_small).pack(side="right", padx=(0, int(8 * ui)))
        ctk.CTkLabel(head, text="", image=uikit.make_icon("devices", 15, uikit.FG_DIM), fg_color=uikit.SIDE,
                     bg_color=uikit.SIDE).pack(side="left", padx=(int(12 * ui), int(7 * ui)))
        tk.Label(head, text="设备", bg=uikit.SIDE, fg=uikit.FG, font=self.f_small_b).pack(side="left")
        tk.Frame(self.side, bg=uikit.EDGE_SOFT, height=max(1, int(ui))).pack(fill="x")

        self.side_list = ctk.CTkScrollableFrame(
            self.side, fg_color=uikit.SIDE, bg_color=uikit.SIDE, corner_radius=0, border_width=0,
            scrollbar_fg_color=uikit.SIDE, scrollbar_button_color=uikit.EDGE,
            scrollbar_button_hover_color=uikit.FG_FAINT, label_text="")
        self.side_list.pack(fill="both", expand=True, padx=int(6 * ui), pady=(int(6 * ui), 0))
        self.rows: dict[str, dict] = {}

    def _build_focus(self) -> None:
        ui = self.ui
        self.focus_frame = tk.Frame(self.content, bg=uikit.BG)
        top = tk.Frame(self.focus_frame, bg=uikit.BG, height=int(46 * ui))
        top.pack(fill="x", padx=int(12 * ui), pady=(int(8 * ui), 0))
        top.pack_propagate(False)
        ctk.CTkButton(top, text="返回总览", image=uikit.make_icon("back", 13, uikit.FG_DIM), compound="left",
                      font=self.cf_small, height=30, width=104, corner_radius=7, fg_color=uikit.INPUT,
                      hover_color=uikit.INPUT_HOVER, text_color=uikit.FG_DIM, bg_color=uikit.BG,
                      command=self.show_overview).pack(side="left")
        self.v_focus_title = tk.StringVar()
        tk.Label(top, textvariable=self.v_focus_title, bg=uikit.BG, fg=uikit.FG, font=self.f_bold).pack(
            side="left", padx=int(14 * ui))
        ctk.CTkButton(top, text="保存截图", image=uikit.make_icon("camera", 13, uikit.FG_DIM), compound="left",
                      font=self.cf_small, height=30, width=104, corner_radius=7, fg_color=uikit.INPUT,
                      hover_color=uikit.INPUT_HOVER, text_color=uikit.FG_DIM, bg_color=uikit.BG,
                      command=lambda: self.snapshot(self.focus_id)).pack(side="right")

        body = tk.Frame(self.focus_frame, bg=uikit.BG)
        body.pack(fill="both", expand=True, padx=int(12 * ui), pady=int(6 * ui))
        self.strip = tk.Frame(body, bg=uikit.BG, width=int(196 * ui))
        self.strip.pack(side="right", fill="y")
        self.strip.pack_propagate(False)
        self.focus_area = tk.Frame(body, bg=uikit.BG)
        self.focus_area.pack(side="left", fill="both", expand=True)
        self.focus_area.bind("<Configure>", lambda _e: self._mark_dirty())
        self.focus_tile: Tile | None = None

    # ---- 机器与会话
    def rebuild(self) -> None:
        for t in list(self.tiles.values()) + list(self.thumbs.values()):
            t.destroy()
        if self.focus_tile:
            self.focus_tile.destroy()
            self.focus_tile = None
        self.tiles.clear()
        self.thumbs.clear()
        self._build_side_rows()
        ids = {m.id for m in self.store.machines}
        for mid in [k for k in self.sessions if k not in ids]:
            self.sessions.pop(mid).close()
        for m in self.store.machines:
            if m.id not in self.sessions:
                self.sessions[m.id] = Session(m, self.viewer_exe, self.park, self.store.settings["auto_reconnect"])
            self.tiles[m.id] = Tile(self, self.content, m)
            self.thumbs[m.id] = Tile(self, self.strip, m, thumb=True)
        if self.focus_id and self.focus_id not in self.sessions:
            self.focus_id = None
        self._mark_dirty()

    def _build_side_rows(self) -> None:
        for w in self.side_list.winfo_children():
            w.destroy()
        self.rows = {}
        ui = self.ui
        self.v_side_count.set(f"{len(self.store.machines)} 台")
        if not self.store.machines:
            tk.Label(self.side_list, text="还没有设备\n点右上角“＋”添加", bg=uikit.SIDE, fg=uikit.FG_DIM,
                     font=self.f_base, justify="left").pack(anchor="w", padx=int(8 * ui), pady=int(12 * ui))
        for m in self.store.machines:
            mid = m.id
            row = ctk.CTkFrame(self.side_list, fg_color=uikit.CARD, bg_color=uikit.SIDE, corner_radius=8,
                               border_width=1, border_color=uikit.EDGE_SOFT)
            row.pack(fill="x", padx=int(2 * ui), pady=(0, int(4 * ui)))
            conn = ctk.CTkButton(row, text="", image=uikit.make_icon("play", 11, "#ffffff"), width=26,
                                 height=26, corner_radius=7, fg_color=uikit.ACCENT,
                                 hover_color=uikit.ACCENT_HOVER, bg_color=uikit.CARD,
                                 command=lambda i=mid: self.toggle(i))
            conn.pack(side="right", padx=(0, int(8 * ui)))
            menu = ctk.CTkButton(row, text="", image=uikit.make_icon("more", 14, uikit.FG_FAINT), width=24,
                                 height=24, corner_radius=7, fg_color=uikit.CARD,
                                 hover_color=uikit.INPUT_HOVER, bg_color=uikit.CARD)
            menu.configure(command=lambda b=menu, i=mid: self.popup_menu_at(i, b))
            menu.pack(side="right", padx=(0, int(2 * ui)))

            text = tk.Frame(row, bg=uikit.CARD)
            text.pack(side="left", fill="both", expand=True, padx=(int(10 * ui), int(4 * ui)),
                      pady=int(7 * ui))
            top_line = tk.Frame(text, bg=uikit.CARD)
            top_line.pack(fill="x")
            dot = tk.Label(top_line, text="●", bg=uikit.CARD, fg=status.LIGHT["idle"], font=self.f_dot)
            dot.pack(side="left")
            name = tk.Label(top_line, text=m.name, bg=uikit.CARD, fg=uikit.FG, font=self.f_small_b, anchor="w")
            name.pack(side="left", padx=(int(4 * ui), 0))
            bottom = tk.Frame(text, bg=uikit.CARD)
            bottom.pack(fill="x", pady=(int(1 * ui), 0))
            addr = tk.Label(bottom, text=m.host, bg=uikit.CARD, fg=uikit.FG_FAINT, font=self.f_tiny,
                            anchor="w")
            addr.pack(side="left", padx=(0, int(8 * ui)))
            st = tk.Label(bottom, text="", bg=uikit.CARD, fg=uikit.FG_DIM, font=self.f_tiny)
            st.pack(side="right")
            for w in (row, text, top_line, dot, name, bottom, addr, st):
                w.bind("<Button-1>", lambda _e, i=mid: self.show_focus(i))
                w.bind("<Button-3>", lambda e, i=mid: self.popup_menu(i, e.x_root, e.y_root))
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass
            try:
                row._canvas.configure(cursor="hand2")
            except (AttributeError, tk.TclError):
                pass
            self.rows[mid] = {"row": row, "dot": dot, "st": st, "conn": conn, "name": name, "addr": addr,
                              "state": None}
        self.root.after_idle(self._sync_scrollbar)

    def _sync_scrollbar(self) -> None:
        """设备不多时把滚动条收起来，别在侧栏中间挂一条灰杠。"""
        try:
            canvas = self.side_list._parent_canvas
            bar = self.side_list._scrollbar
            box = canvas.bbox("all") or (0, 0, 0, 0)
            if box[3] > canvas.winfo_height() + 1:
                bar.grid()
            else:
                bar.grid_remove()
        except (AttributeError, tk.TclError):
            pass

    def _refresh_side(self) -> None:
        for mid, r in self.rows.items():
            sess = self.sessions.get(mid)
            if not sess:
                continue
            level, short, _line = status.describe(sess.state, sess.message, sess.fps, sess.frame_age(), sess.static_for())
            r["dot"].configure(fg=status.LIGHT[level])
            r["st"].configure(text=short, fg=status.LIGHT[level] if level != "idle" else uikit.FG_DIM)
            if sess.state != r["state"]:
                r["state"] = sess.state
                icon, color, fill, hover = _side_action(sess.state)
                r["conn"].configure(image=uikit.make_icon(icon, 11, color), fg_color=fill, hover_color=hover)
            r["row"].configure(border_color=uikit.SELECT if mid == self.focus_id else uikit.EDGE_SOFT)

    def _mark_dirty(self) -> None:
        self._layout_key = None

    def save(self) -> None:
        if self.persist:
            try:
                self.store.save()
            except OSError as e:
                self.v_status.set(f"保存设置失败：{e}")

    def on_setting(self) -> None:
        s = self.store.settings
        s["layout"] = LAYOUTS[self.v_layout.get()]
        s["quality"] = QUALITY[self.v_quality.get()]
        s["auto_reconnect"] = self.v_auto.get()
        for sess in self.sessions.values():
            sess.auto_reconnect = s["auto_reconnect"]
        self.save()
        self._mark_dirty()

    def add_machine(self) -> None:
        d = MachineDialog(self, None)
        if d.result:
            self.store.add(d.result)
            self.save()
            self.rebuild()

    def edit_machine(self, mid: str) -> None:
        m = self.store.get(mid)
        if not m:
            return
        old = (m.host, m.port, m.password)
        d = MachineDialog(self, m)
        if d.result:
            if (m.host, m.port, m.password) != old:
                self.sessions[mid].disconnect()
                self.sessions[mid].machine = m
            self.save()
            self.rebuild()

    def move_machine(self, mid: str, d: int) -> None:
        ms = self.store.machines
        i = next((k for k, m in enumerate(ms) if m.id == mid), -1)
        j = i + d
        if i < 0 or not 0 <= j < len(ms):
            return
        ms[i], ms[j] = ms[j], ms[i]
        self.save()
        self.rebuild()

    def delete_machine(self, mid: str) -> None:
        m = self.store.get(mid)
        if m and messagebox.askyesno("删除设备", f"确定把“{m.name}”从列表里删除吗？\n（只删列表，不影响设备本身）"):
            self.store.remove(mid)
            self.save()
            if self.focus_id == mid:
                self.show_overview()
            self.rebuild()

    def connect(self, mid: str) -> None:
        m, sess = self.store.get(mid), self.sessions.get(mid)
        if m and sess:
            sess.machine = m
            sess.connect()

    def toggle(self, mid: str) -> None:
        sess = self.sessions.get(mid)
        if not sess:
            return
        if sess.state in (ONLINE, CONNECTING):
            sess.disconnect()
        else:
            self.connect(mid)

    def connect_all(self) -> None:
        for m in self.store.machines:
            if self.sessions[m.id].state in (IDLE, LOST, ERROR):
                self.connect(m.id)

    def disconnect_all(self) -> None:
        for s in self.sessions.values():
            s.disconnect()

    def snapshot(self, mid: str | None) -> None:
        sess = self.sessions.get(mid or "")
        cap = sess.grab() if sess else None
        if not cap:
            self.v_status.set("没法截图：这台设备还没在线")
            return
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "mhxy")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{sess.machine.name}_{time.strftime('%Y%m%d_%H%M%S')}.png")
        write_png(path, cap["rgb"], cap["w"], cap["h"])
        self.v_status.set(f"截图已保存：{path}")

    def popup_menu(self, mid: str, x: int, y: int) -> None:
        sess = self.sessions.get(mid)
        if not sess:
            return
        m = tk.Menu(self.root, tearoff=0, font=self.f_base, bg=uikit.CARD, fg=uikit.FG,
                    activebackground=uikit.INPUT_HOVER, activeforeground=uikit.FG, bd=0, relief="flat")
        m.add_command(label="放大查看", command=lambda: self.show_focus(mid))
        m.add_command(label=status.action_label(sess.state), command=lambda: self.toggle(mid))
        m.add_command(label="重新连接", command=lambda: (sess.disconnect(), self.connect(mid)))
        m.add_separator()
        m.add_command(label="保存当前截图", command=lambda: self.snapshot(mid))
        m.add_command(label="显示原始查看器窗口（排查用）", command=sess.show_viewer)
        m.add_command(label="收起原始查看器窗口", command=sess.hide_viewer)
        m.add_separator()
        m.add_command(label="在列表里前移", command=lambda: self.move_machine(mid, -1))
        m.add_command(label="在列表里后移", command=lambda: self.move_machine(mid, 1))
        m.add_command(label="修改……", command=lambda: self.edit_machine(mid))
        m.add_command(label="删除", command=lambda: self.delete_machine(mid))
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def popup_menu_at(self, mid: str, widget: tk.Misc) -> None:
        self.popup_menu(mid, widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 2)

    # ---- 视图切换
    def show_focus(self, mid: str) -> None:
        if mid in self.sessions:
            self.focus_id = mid
            self._mark_dirty()

    def show_overview(self) -> None:
        if self.focus_id is not None:
            self.focus_id = None
            self._mark_dirty()

    def step_focus(self, d: int) -> None:
        ids = [m.id for m in self.store.machines]
        if self.focus_id in ids and len(ids) > 1:
            self.show_focus(ids[(ids.index(self.focus_id) + d) % len(ids)])

    # ---- 布局
    def visible_ids(self) -> list[str]:
        return [m.id for m in self.store.machines]

    def _thumb_size(self) -> tuple[int, int]:
        tw = int(176 * self.ui)
        return tw, int(tw * 3 / 4)

    def _relayout(self) -> None:
        for t in self.tiles.values():
            t.frame.place_forget()
        self.empty.place_forget()
        self.focus_frame.place_forget()
        for t in self.thumbs.values():
            t.frame.pack_forget()
        if not self.store.machines:
            self.empty.place(relx=0, rely=0, relwidth=1, relheight=1)
            return
        if self.focus_id:
            self._layout_focus()
        else:
            self._layout_overview()

    def _chrome(self) -> int:
        return uikit.tile_chrome(self.ui)

    def _layout_overview(self) -> None:
        ids = self.visible_ids()
        W, H = max(200, self.content.winfo_width()), max(150, self.content.winfo_height())
        gap = int(10 * self.ui)
        chrome = self._chrome()
        g = layout.compute_grid(len(ids), W, H, chrome, gap, self.store.settings["layout"])
        self.grid = g
        tile_h = g.img_h + chrome
        tot_w = g.cols * g.cell_w + (g.cols - 1) * gap
        tot_h = g.rows * tile_h + (g.rows - 1) * gap
        x0, y0 = max(gap, (W - tot_w) // 2), max(gap, (H - tot_h) // 2)
        for i, mid in enumerate(ids):
            r, c = divmod(i, g.cols)
            t = self.tiles[mid]
            t.set_layout(g.img_w, g.img_h, g.cell_w)
            t.selected(False)
            in_row = min(g.cols, len(ids) - r * g.cols)
            row_w = in_row * g.cell_w + (in_row - 1) * gap
            row_x = x0 + max(0, (tot_w - row_w) // 2)
            t.frame.place(x=row_x + c * (g.cell_w + gap), y=y0 + r * (tile_h + gap),
                          width=g.cell_w, height=tile_h)

    def _layout_focus(self) -> None:
        self.focus_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.focus_frame.update_idletasks()
        ui = self.ui
        tw, th = self._thumb_size()
        others = [mid for mid in self.thumbs if mid != self.focus_id]
        for t in self.thumbs.values():
            t.frame.pack_forget()
        if others:
            if not self.strip.winfo_manager():
                self.strip.pack(side="right", fill="y", before=self.focus_area)
        elif self.strip.winfo_manager():
            self.strip.pack_forget()
        for mid in others:
            t = self.thumbs[mid]
            t.set_layout(tw, th, tw + 2 * t.pad)
            t.frame.pack(pady=int(5 * ui))
        self.strip.update_idletasks()
        self.focus_area.update_idletasks()
        aw = max(200, self.focus_area.winfo_width())
        ah = max(150, self.focus_area.winfo_height())
        pad = int(uikit.TILE_PAD * ui)
        chrome = self._chrome()
        fw, fh = layout.focus_image_size(max(160, aw - 2 * pad - int(10 * ui)),
                                         max(120, ah - chrome - int(10 * ui)))
        if fw < 80 or fh < 60:
            fw, fh = 320, 240
        if self.focus_tile is None or self.focus_tile.machine.id != self.focus_id:
            if self.focus_tile:
                self.focus_tile.destroy()
            self.focus_tile = Tile(self, self.focus_area, self.store.get(self.focus_id))
            for w in self.focus_tile._clickable():
                w.unbind("<Button-1>")
                w.bind("<Double-Button-1>", lambda _e: self.show_overview())
            self.focus_tile.set_cursor("arrow")
        self.focus_tile.set_layout(fw, fh, fw + 2 * pad)
        w, h = fw + 2 * pad, fh + chrome
        self.focus_tile.frame.place(x=max(0, (aw - w) // 2), y=int(4 * ui), width=w, height=h)
        self.focus_img = (fw, fh)
        self.v_focus_title.set(f"{self.focus_tile.machine.name}　{self.focus_tile.machine.address()}")

    # ---- 周期刷新
    def tick(self) -> None:
        try:
            self._tick()
        except Exception as e:  # noqa: BLE001  界面不能因为一次异常就停
            self.v_status.set(f"刷新出错：{type(e).__name__}: {e}")
        self.root.after(100, self.tick)

    def _tick(self) -> None:
        self._tick_n += 1
        ids = self.visible_ids()
        key = (self.focus_id, tuple(ids), self.content.winfo_width(), self.content.winfo_height(),
               self.focus_area.winfo_width(), self.focus_area.winfo_height(),
               self.store.settings["layout"], len(self.store.machines))
        if key != self._layout_key:
            self._layout_key = key
            self._relayout()
        quality = self.store.settings["quality"]
        if self.focus_id:
            fw, fh = getattr(self, "focus_img", (400, 300))
            tw, th = self._thumb_size()
            for mid, sess in self.sessions.items():
                if mid == self.focus_id:
                    sess.want = (fw, fh, FOCUS_FPS[quality])
                else:
                    sess.want = (tw, th, THUMB_FPS)
            if self.focus_tile:
                self.focus_tile.refresh(self.sessions[self.focus_id])
            for mid, t in self.thumbs.items():
                if mid != self.focus_id:
                    t.refresh(self.sessions[mid])
        else:
            g = getattr(self, "grid", None)
            for mid, sess in self.sessions.items():
                if mid in ids and g:
                    sess.want = (g.img_w, g.img_h, TILE_FPS[quality])
                    self.tiles[mid].refresh(sess)
                else:
                    sess.want = (*HIDDEN_SIZE, HIDDEN_FPS)
        if self._tick_n % 3 == 0:
            self._refresh_side()
            self._sync_scrollbar()
        if self._tick_n % 5 == 0:
            self._update_banner()

    def _update_banner(self) -> None:
        states = [s.state for s in self.sessions.values()]
        warns = sum(1 for s in self.sessions.values() if s.state == ONLINE and status.describe(
            s.state, s.message, s.fps, s.frame_age(), s.static_for())[0] != "ok")
        level, text = status.summarize(states, warns)
        self.v_banner.set(text)
        self.summary_pill.set(level, text)
        can_off = any(s in (ONLINE, CONNECTING) for s in states)
        can_on = any(s in (IDLE, LOST, ERROR) for s in states)
        if can_off != getattr(self, "_can_off", None):
            self._can_off = can_off
            self.btn_off.configure(
                fg_color=uikit.INPUT if can_off else uikit.HEADER,
                hover_color=uikit.INPUT_HOVER if can_off else uikit.HEADER,
                text_color=uikit.FG_DIM if can_off else uikit.FG_FAINT,
                border_color=uikit.EDGE if can_off else uikit.EDGE_SOFT,
                image=uikit.make_icon("stop", 11, uikit.FG_DIM if can_off else uikit.FG_FAINT))
        if can_on != getattr(self, "_can_on", None):
            self._can_on = can_on
            self.btn_all.configure(
                fg_color=uikit.ACCENT if can_on else uikit.INPUT,
                hover_color=uikit.ACCENT_HOVER if can_on else uikit.INPUT,
                text_color="#ffffff" if can_on else uikit.FG_FAINT,
                image=uikit.make_icon("play", 12, "#ffffff" if can_on else uikit.FG_FAINT))

    # ---- 退出
    def on_close(self) -> None:
        for s in self.sessions.values():
            s.close()
        self.root.destroy()

    def run(self) -> None:
        if self.store.settings.get("auto_connect_on_start"):
            self.root.after(500, self.connect_all)
        self.root.mainloop()


def main() -> int:
    winapi.set_dpi_aware()  # 必须早于 Tk()，否则 200% 缩放下读到的窗口尺寸和截图尺寸都不对
    app = ManagerApp(persist="--smoke" not in sys.argv)
    if "--smoke" in sys.argv:  # 打包后冒烟：开 3 秒自动关
        app.root.after(3000, app.on_close)
    app.run()
    if "--smoke" in sys.argv:
        print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
