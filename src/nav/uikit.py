"""第三层录制器的界面部件：浅色、发丝线、蓝色品牌，与管理台同一套视觉语言。

只用 tkinter + Pillow，不依赖 customtkinter，也不改管理台代码。
所有尺寸按 DPI 缩放；图标和圆角都用 4 倍超采样绘制，放大后不发糊。
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache

from PIL import Image, ImageDraw, ImageTk

# ---------------------------------------------------------------- 调色板
BG = "#f2f3f5"
CARD = "#ffffff"
CARD_HOVER = "#fafafa"
EDGE = "#e3e3e7"
EDGE_SOFT = "#ececef"
INPUT = "#f5f5f7"
INPUT_HOVER = "#ececef"
FG = "#1d1d20"
FG_DIM = "#5f6068"
FG_FAINT = "#9a9aa3"
ACCENT = "#1d1d20"
ACCENT_HOVER = "#000000"
OK = "#1f883d"
WARN = "#bc4c00"
BAD = "#cf222e"
BUSY = "#9a6700"
IDLE = "#8c8c94"

# 品牌蓝只用在图标、焦点和只读提示上
BLUE = "#2563eb"
BLUE_LIGHT = "#4f8dff"
BLUE_SOFT = "#eef4ff"
BLUE_EDGE = "#d8e5ff"

LEVEL = {"ok": OK, "warn": WARN, "bad": BAD, "busy": BUSY, "idle": IDLE, "info": BLUE}
PILL_FILL = {"ok": "#e7f6ec", "warn": "#fff0e4", "bad": "#ffebe9",
             "busy": "#fbf3d9", "idle": "#f2f2f4", "info": BLUE_SOFT}
PILL_EDGE = {"ok": "#c3e8d0", "warn": "#f7d4b5", "bad": "#f5c9c6",
             "busy": "#ead9a6", "idle": "#e4e4e8", "info": BLUE_EDGE}

# 画面舞台
STAGE = "#f4f4f6"
STAGE_GRID = "#ececef"
STAGE_CORNER = "#d8d8de"
STAGE_TEXT = "#a3a3ab"

FAMILY = "Microsoft YaHei UI"


def dpi_scale() -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except (AttributeError, OSError):
        dpi = 96
    return max(1.0, dpi / 96.0)


def work_area() -> tuple[int, int, int, int]:
    """桌面可用区域（不含任务栏），用于把窗口完整放进屏幕。"""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    user32 = ctypes.windll.user32
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def font(ui: float, size: float = 12, bold: bool = False, family: str | None = None) -> tkfont.Font:
    """像素字号，避免 tk scaling 把点值再放大一次。"""
    fam = family or FAMILY
    if fam not in tkfont.families():
        fam = "Segoe UI"
    return tkfont.Font(family=fam, size=-max(9, int(round(size * ui))),
                       weight="bold" if bold else "normal")


# ---------------------------------------------------------------- 图标
def _glyph(draw: ImageDraw.ImageDraw, name: str, size: int, color: str) -> None:
    s = float(size)
    w = max(1, round(s * 0.075))

    def pt(x: float, y: float) -> tuple[float, float]:
        return x * s, y * s

    def line(x1, y1, x2, y2, width=None) -> None:
        draw.line([pt(x1, y1), pt(x2, y2)], fill=color, width=width or w, joint="curve")

    def box(x1, y1, x2, y2, r=0.12, outline=True, fill=False, width=None) -> None:
        draw.rounded_rectangle([*pt(x1, y1), *pt(x2, y2)], radius=r * s,
                               outline=color if outline else None,
                               fill=color if fill else None, width=width or w)

    def poly(points, fill=False) -> None:
        draw.polygon([pt(*p) for p in points], fill=color if fill else None,
                     outline=None if fill else color, width=0 if fill else w)

    def arc(cx, cy, r, start, end, width=None) -> None:
        draw.arc([*pt(cx - r, cy - r), *pt(cx + r, cy + r)], start=start, end=end,
                 fill=color, width=width or w)

    if name == "play":
        poly([(0.32, 0.18), (0.82, 0.5), (0.32, 0.82)], fill=True)
    elif name == "stop":
        box(0.28, 0.28, 0.72, 0.72, 0.14, outline=False, fill=True)
    elif name == "report":
        box(0.2, 0.08, 0.8, 0.92, 0.08)
        line(0.34, 0.34, 0.66, 0.34, width=max(1, w - 1))
        line(0.34, 0.5, 0.66, 0.5, width=max(1, w - 1))
        line(0.34, 0.66, 0.54, 0.66, width=max(1, w - 1))
    elif name == "folder":
        poly([(0.08, 0.26), (0.4, 0.26), (0.48, 0.36), (0.92, 0.36),
              (0.92, 0.84), (0.08, 0.84)])
        line(0.08, 0.36, 0.92, 0.36)
    elif name == "refresh":
        arc(0.5, 0.5, 0.37, 40, 330)
        line(0.82, 0.3, 0.82, 0.52)
        line(0.82, 0.3, 0.6, 0.3)
    elif name == "check":
        line(0.16, 0.52, 0.42, 0.78)
        line(0.42, 0.78, 0.85, 0.24)
    elif name == "chevron":
        line(0.3, 0.4, 0.5, 0.62)
        line(0.5, 0.62, 0.7, 0.4)
    elif name == "monitor":
        box(0.08, 0.12, 0.92, 0.66, 0.08)
        line(0.5, 0.66, 0.5, 0.8)
        line(0.3, 0.86, 0.7, 0.86)
    elif name == "dot":
        r = 0.38
        draw.ellipse([*pt(0.5 - r, 0.5 - r), *pt(0.5 + r, 0.5 + r)], fill=color)
    else:
        box(0.2, 0.2, 0.8, 0.8, 0.12)


@lru_cache(maxsize=256)
def _icon_master(name: str, px: int, color: str) -> Image.Image:
    px = max(16, int(px))
    scale = 4
    big = Image.new("RGBA", (px * scale, px * scale), (0, 0, 0, 0))
    _glyph(ImageDraw.Draw(big), name, px * scale, color)
    return big.resize((px, px), Image.LANCZOS)


@lru_cache(maxsize=512)
def canvas_icon(name: str, px: int, color: str) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(_icon_master(name, int(px), color))


# ---------------------------------------------------------------- 品牌图形
@lru_cache(maxsize=8)
def logo_master(px: int) -> Image.Image:
    """蓝色圆角标 + 白色路线：三个节点连成一条走过的路。"""
    n = max(16, int(px))
    s = n * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    top = tuple(int(BLUE_LIGHT[i:i + 2], 16) for i in (1, 3, 5))
    bottom = tuple(int(BLUE[i:i + 2], 16) for i in (1, 3, 5))
    grad = Image.new("RGB", (1, s))
    for y in range(s):
        t = y / (s - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    grad = grad.resize((s, s), Image.BILINEAR).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.26, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    if n >= 20:
        w = max(2, int(s * 0.055))
        pts = [(0.26 * s, 0.72 * s), (0.48 * s, 0.42 * s), (0.74 * s, 0.6 * s)]
        d.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
        for x, y in pts:
            r = s * 0.075
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255))
    else:
        d.ellipse((s * 0.3, s * 0.3, s * 0.7, s * 0.7), fill=(255, 255, 255, 255))
    return img.resize((n, n), Image.LANCZOS)


def _asset(name: str) -> str:
    base = getattr(sys, "_MEIPASS", "")
    if base:
        return os.path.join(base, "resources", "icons", name)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "resources", "icons", name)


def set_window_icon(root: tk.Misc) -> None:
    path = _asset("recorder.ico")
    if os.path.exists(path):
        try:
            root.iconbitmap(path)
            return
        except tk.TclError:
            pass
    try:
        img = ImageTk.PhotoImage(logo_master(64), master=root)
        root._app_icon = img  # type: ignore[attr-defined]
        root.iconphoto(True, img)
    except tk.TclError:
        pass


def set_light_titlebar(window: tk.Misc) -> None:
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    try:
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        off = ctypes.c_int(0)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(off), ctypes.sizeof(off))
        caption = ctypes.c_uint(0x00F7F7F8)
        text = ctypes.c_uint(0x00201D1D)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------- 圆角
@lru_cache(maxsize=384)
def _rounded_master(w: int, h: int, r: int, fill: str, edge: str | None, border: int) -> Image.Image:
    w, h = max(2, int(w)), max(2, int(h))
    s = 4
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        (0, 0, w * s - 1, h * s - 1), radius=max(0, int(r * s)),
        fill=fill, outline=edge if edge else None, width=max(0, int(border * s)))
    return img.resize((w, h), Image.LANCZOS)


def rounded_photo(master: tk.Misc, w: int, h: int, r: int, fill: str,
                  edge: str | None = None, border: int = 0) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(_rounded_master(int(w), int(h), int(r), fill, edge, int(border)), master=master)


def _parent_bg(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return BG


# ---------------------------------------------------------------- 按钮
_VARIANTS = {
    "primary": (ACCENT, ACCENT_HOVER, "#ffffff", None),
    "secondary": (CARD, INPUT, FG, EDGE),
    "ghost": (None, INPUT_HOVER, FG_DIM, None),
    "chip": (INPUT, INPUT_HOVER, FG_DIM, None),
    "danger": (CARD, "#fff1f0", BAD, EDGE),
    "quiet": (None, INPUT_HOVER, FG_FAINT, None),
}


class Button(tk.Canvas):
    """圆角按钮：发丝描边、清晰图标、悬停和禁用状态都由自己画。"""

    def __init__(self, master: tk.Misc, text: str, command, ui: float,
                 variant: str = "secondary", icon: str | None = None,
                 height: float = 32, min_width: float = 0, pad: float = 14,
                 text_font: tkfont.Font | None = None, bg: str | None = None) -> None:
        self.ui = ui
        self._text = text
        self._command = command
        self._variant = variant
        self._icon_name = icon
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._font = text_font or font(ui, 12, True)
        self._h = int(round(height * ui))
        self._pad = int(round(pad * ui))
        self._gap = int(round(7 * ui)) if icon else 0
        self._icon_px = int(round(14 * ui))
        width = self._measure_width(min_width)
        super().__init__(master, width=width, height=self._h, bg=bg or _parent_bg(master),
                         highlightthickness=0, bd=0, takefocus=0, cursor="hand2")
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._img = None
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Configure>", lambda _e: self._paint())  # grid/pack 拉伸后按新尺寸重画
        self._paint()

    def _measure_width(self, min_width: float = 0) -> int:
        content = self._font.measure(self._text) if self._text else 0
        icon = self._icon_px + self._gap if self._icon_name else 0
        return max(int(round(min_width * self.ui)), content + icon + 2 * self._pad,
                   int(round(28 * self.ui)) if not self._text else 0)

    def _colors(self) -> tuple[str, str, str, str | None]:
        fill, hover, fg, edge = _VARIANTS.get(self._variant, _VARIANTS["secondary"])
        fill = fill if fill is not None else self.cget("bg")
        if not self._enabled:
            if self._variant == "primary":
                return INPUT, FG_FAINT, FG_FAINT, None
            soft = EDGE_SOFT if self._variant in ("secondary", "danger") else None
            return fill, FG_FAINT, FG_FAINT, soft
        if self._hover or self._pressed:
            fill = hover
        return fill, fg, fg, edge

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self.cget("width")), self._h
        fill, _hover, fg, edge = self._colors()
        border = max(1, int(round(self.ui))) if edge else 0
        key = (w, h, fill, edge, border)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, w, h, int(round(8 * self.ui)), fill, edge, border)
            self._photos[key] = photo
        self.create_image(0, 0, image=photo, anchor="nw")
        x = self._pad
        if self._icon_name:
            self._img = canvas_icon(self._icon_name, self._icon_px, fg)
            self.create_image(w // 2 if not self._text else x, h // 2, image=self._img,
                              anchor="center" if not self._text else "w")
            x += self._icon_px + self._gap
        if self._text:
            self.create_text(w // 2 if not self._icon_name else x, h // 2,
                             anchor="w" if self._icon_name else "center",
                             text=self._text, font=self._font, fill=fg)

    def _set_hover(self, value: bool) -> None:
        if self._enabled and self._hover != value:
            self._hover = value
            self._paint()

    def _down(self, _e) -> None:
        if self._enabled:
            self._pressed = True
            self._paint()

    def _up(self, event) -> None:
        if not self._enabled:
            return
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._pressed = False
        self._paint()
        if inside and self._command is not None:
            self._command()

    def set_enabled(self, value: bool) -> None:
        value = bool(value)
        if value == self._enabled:
            return
        self._enabled = value
        self._hover = False
        self._pressed = False
        self.configure(cursor="hand2" if value else "arrow")
        self._paint()

    def set_variant(self, variant: str) -> None:
        if variant != self._variant:
            self._variant = variant
            self._paint()

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.configure(width=self._measure_width())
            self._paint()

    def set_icon(self, name: str | None) -> None:
        self._icon_name = name
        self._gap = int(round(7 * self.ui)) if name else 0
        self.configure(width=self._measure_width())
        self._paint()


# ---------------------------------------------------------------- 勾选
class Check(tk.Frame):
    def __init__(self, master: tk.Misc, text: str, variable: tk.BooleanVar, ui: float,
                 command=None, text_font: tkfont.Font | None = None) -> None:
        super().__init__(master, bg=_parent_bg(master), cursor="hand2")
        self.ui, self.var, self.command = ui, variable, command
        self._hover = False
        self._px = int(round(18 * ui))
        self.box = tk.Canvas(self, width=self._px, height=self._px, bg=_parent_bg(master),
                             highlightthickness=0, bd=0, cursor="hand2")
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, bg=_parent_bg(master), fg=FG_DIM,
                              font=text_font or font(ui, 11), cursor="hand2", anchor="w")
        self.label.pack(side="left", padx=(int(round(8 * ui)), 0))
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        for w in (self, self.box, self.label):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", lambda _e: self._set_hover(True))
            w.bind("<Leave>", lambda _e: self._set_hover(False))
        self.var.trace_add("write", lambda *_: self._paint())
        self._paint()

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self._paint()

    def _toggle(self, _e=None) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _paint(self) -> None:
        if not self.box.winfo_exists():
            return
        self.box.delete("all")
        on = self.var.get()
        fill = ACCENT if on else (EDGE_SOFT if self._hover else CARD)
        edge = ACCENT if on else ("#c9c9d0" if self._hover else EDGE)
        key = (fill, edge)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, self._px, self._px, int(round(4 * self.ui)), fill, edge,
                                  max(1, int(round(self.ui))))
            self._photos[key] = photo
        self.box.create_image(0, 0, image=photo, anchor="nw")
        if on:
            check = canvas_icon("check", int(round(11 * self.ui)), "#ffffff")
            self.box._check = check  # type: ignore[attr-defined]
            self.box.create_image(self._px // 2, self._px // 2, image=check)


# ---------------------------------------------------------------- 状态胶囊
class Pill(tk.Canvas):
    def __init__(self, master: tk.Misc, ui: float, text_font: tkfont.Font | None = None,
                 height: float = 26, bg: str | None = None) -> None:
        self.ui = ui
        self._font = text_font or font(ui, 11, True)
        self._h = int(round(height * ui))
        self._level, self._text, self._dot = "idle", "", True
        self._dot_override: str | None = None
        super().__init__(master, width=int(round(90 * ui)), height=self._h, bg=bg or _parent_bg(master),
                         highlightthickness=0, bd=0, takefocus=0)
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._dot_px = int(round(8 * ui))
        self._paint()

    def set(self, level: str, text: str, dot: bool = True) -> None:
        if (level, text, dot) == (self._level, self._text, getattr(self, "_dot", True)):
            return
        self._level, self._text, self._dot = level, text, dot
        self._paint()

    def set_dot_color(self, color: str | None) -> None:
        self._dot_override = color
        self._paint()

    def _width(self) -> int:
        pad = int(round(10 * self.ui))
        gap = int(round(6 * self.ui)) if self._dot else 0
        dot = self._dot_px if self._dot else 0
        return pad * 2 + dot + gap + self._font.measure(self._text)

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        w = max(int(round(40 * self.ui)), self._width())
        if w != int(self.cget("width")):
            self.configure(width=w)
        self.delete("all")
        fill = PILL_FILL.get(self._level, PILL_FILL["idle"])
        edge = PILL_EDGE.get(self._level, PILL_EDGE["idle"])
        key = (w, fill, edge)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, w, self._h, self._h // 2, fill, edge, max(1, int(round(self.ui))))
            self._photos[key] = photo
        self.create_image(0, 0, image=photo, anchor="nw")
        x = int(round(10 * self.ui))
        if self._dot:
            r = self._dot_px // 2
            color = self._dot_override or LEVEL.get(self._level, IDLE)
            self.create_oval(x, self._h // 2 - r, x + 2 * r, self._h // 2 + r, fill=color, outline=color)
            x += 2 * r + int(round(6 * self.ui))
        fg = LEVEL.get(self._level, FG_DIM) if self._level != "idle" else FG_DIM
        self.create_text(x, self._h // 2, text=self._text, anchor="w", font=self._font, fill=fg)


# ---------------------------------------------------------------- 下拉
class Select(tk.Canvas):
    """扁平下拉：发丝描边、细箭头、自绘弹层；不打开时和输入框完全一致。"""

    def __init__(self, master: tk.Misc, values, variable: tk.StringVar, ui: float,
                 width: float = 240, height: float = 32, text_font: tkfont.Font | None = None,
                 on_change=None, placeholder: str = "未选择", bg: str | None = None) -> None:
        self.ui = ui
        self._values = list(values)
        self._var = variable
        self._font = text_font or font(ui, 12)
        self._h = int(round(height * ui))
        self._placeholder = placeholder
        self._command = on_change
        self._enabled = True
        self._hover = False
        self._top: tk.Toplevel | None = None
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        super().__init__(master, width=int(round(width * ui)), height=self._h,
                         bg=bg or _parent_bg(master), highlightthickness=0, bd=0,
                         takefocus=0, cursor="hand2")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._toggle())
        self.bind("<Configure>", lambda _e: self._paint())
        self._trace = variable.trace_add("write", lambda *_: self._paint())
        self.bind("<Destroy>", self._destroyed)
        self._paint()

    def set_values(self, values, placeholder: str | None = None) -> None:
        self._values = list(values)
        if placeholder is not None:
            self._placeholder = placeholder
        self._paint()

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self.configure(cursor="hand2" if value else "arrow")
        self._paint()

    def _set_hover(self, value: bool) -> None:
        if self._hover != value:
            self._hover = value
            self._paint()

    def _current(self) -> str:
        text = self._var.get()
        return text if text else self._placeholder

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self.cget("width")), self._h
        active = self._enabled and (self._hover or self._top is not None)
        fill = INPUT if active else CARD
        edge = "#d2d2d8" if active else EDGE
        if not self._enabled:
            fill, edge = INPUT, EDGE_SOFT
        key = (w, h, fill, edge)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, w, h, int(round(7 * self.ui)), fill, edge, max(1, int(round(self.ui))))
            self._photos[key] = photo
        self.create_image(0, 0, image=photo, anchor="nw")
        pad = int(round(11 * self.ui))
        text = self._current()
        avail = w - pad * 2 - int(round(18 * self.ui))
        text = _elide(self._font, text, avail)
        empty = not self._var.get()
        fg = FG_FAINT if empty or not self._enabled else FG
        self.create_text(pad, h // 2, text=text, anchor="w", font=self._font, fill=fg)
        arrow = canvas_icon("chevron", int(round(12 * self.ui)), FG_FAINT)
        self._arrow = arrow
        self.create_image(w - pad, h // 2, image=arrow, anchor="e")

    def _toggle(self) -> None:
        if not self._enabled:
            return
        if self._top is not None:
            self._close()
        elif self._values:
            self._open()

    def _open(self) -> None:
        top = tk.Toplevel(self)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=EDGE)
        top.attributes("-topmost", True)
        card = tk.Frame(top, bg=CARD)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        row_h = int(round(32 * self.ui))
        pad = int(round(4 * self.ui))
        width = max(self.winfo_width(), max(self._font.measure(v) for v in self._values) + int(round(56 * self.ui)))
        current = self._var.get()
        for value in self._values:
            row = tk.Frame(card, bg=CARD, height=row_h)
            row.pack(fill="x", padx=pad)
            row.pack_propagate(False)
            lbl = tk.Label(row, text=value, font=self._font, bg=CARD,
                           fg=FG if value == current else FG_DIM, anchor="w")
            lbl.pack(side="left", fill="both", expand=True, padx=(int(round(8 * self.ui)), 0))
            mark = tk.Label(row, text="", bg=CARD)
            mark.pack(side="right", padx=(0, int(round(8 * self.ui))))
            if value == current:
                img = canvas_icon("check", int(round(11 * self.ui)), FG_DIM)
                mark._img = img  # type: ignore[attr-defined]
                mark.configure(image=img)
            widgets = (row, lbl, mark)

            def hover(_e, ws=widgets):
                for widget in ws:
                    widget.configure(bg=CARD_HOVER)

            def leave(_e, ws=widgets):
                for widget in ws:
                    widget.configure(bg=CARD)

            for widget in widgets:
                widget.bind("<Enter>", hover)
                widget.bind("<Leave>", leave)
                widget.bind("<Button-1>", lambda _e, v=value: self._choose(v))

        self._top = top
        top.update_idletasks()
        h = top.winfo_reqheight()
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + int(round(4 * self.ui))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if y + h > sh - int(round(8 * self.ui)):
            y = self.winfo_rooty() - h - int(round(4 * self.ui))
        x = max(int(round(4 * self.ui)), min(x, sw - width - int(round(8 * self.ui))))
        top.geometry(f"{width}x{h}+{x}+{y}")
        top.bind("<Escape>", lambda _e: self._close())
        top.bind("<Button-1>", lambda e: self._close() if e.widget in (top, card) else None)
        top.deiconify()
        top.lift()
        top.grab_set()
        top.focus_force()
        self._paint()

    def _choose(self, value: str) -> None:
        self._var.set(value)
        self._close()
        if self._command is not None:
            self._command()

    def _close(self) -> None:
        top, self._top = self._top, None
        if top is not None:
            try:
                top.grab_release()
                top.destroy()
            except tk.TclError:
                pass
        self._paint()

    def _destroyed(self, event) -> None:
        if event.widget is not self:
            return
        self._close()
        try:
            self._var.trace_remove("write", self._trace)
        except tk.TclError:
            pass


def _elide(fnt: tkfont.Font, text: str, avail: int) -> str:
    if avail <= 0 or fnt.measure(text) <= avail:
        return text
    out = text
    while out and fnt.measure(out + "…") > avail:
        out = out[:-1]
    return (out + "…") if out else "…"


def match_entries(entries, query: str):
    """按名称、分类和别名做不区分大小写的包含匹配；空查询返回全部。"""
    q = (query or "").strip().lower()
    if not q:
        return list(entries)
    out = []
    for name, hint, aliases in entries:
        hay = " ".join((str(name), str(hint), *[str(a) for a in aliases])).lower()
        if q in hay:
            out.append((name, hint, aliases))
    return out


# ---------------------------------------------------------------- 输入框
class EntryBox(tk.Canvas):
    """圆角输入框：外层画发丝描边，内嵌真正的 Entry，焦点时描边变蓝。"""

    def __init__(self, master: tk.Misc, variable: tk.StringVar, ui: float,
                 height: float = 32, text_font: tkfont.Font | None = None, bg: str | None = None) -> None:
        self.ui = ui
        self._h = int(round(height * ui))
        self._focused = False
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        super().__init__(master, width=int(round(180 * ui)), height=self._h,
                         bg=bg or _parent_bg(master), highlightthickness=0, bd=0)
        self.entry = tk.Entry(self, textvariable=variable, font=text_font or font(ui, 12),
                              bg=INPUT, fg=FG, disabledbackground=INPUT, disabledforeground=FG_FAINT,
                              relief="flat", bd=0, highlightthickness=0, insertbackground=FG,
                              selectbackground=BLUE_SOFT, selectforeground=FG)
        pad = int(round(11 * ui))
        self._win = self.create_window(pad, self._h // 2, anchor="w", window=self.entry,
                                       width=1, height=max(10, self._h - 2 * max(1, int(round(ui)))))
        self._pad = pad
        self.bind("<Configure>", self._resize)
        self.bind("<Button-1>", lambda _e: self.focus_set())
        self.entry.bind("<FocusIn>", lambda _e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._focus(False))
        self._paint()

    def _resize(self, event) -> None:
        self.itemconfigure(self._win, width=max(1, event.width - 2 * self._pad),
                           height=max(10, self._h - 2 * max(1, int(round(self.ui)))))
        self._paint()

    def _focus(self, value: bool) -> None:
        self._focused = value
        self._paint()

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        w = self.winfo_width()
        if w <= 1:
            w = int(self.cget("width"))
        h = self._h
        self.delete("bg")
        edge = BLUE if self._focused else EDGE
        key = (w, h, edge)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, w, h, int(round(7 * self.ui)), INPUT, edge,
                                  max(1, int(round(self.ui))))
            self._photos[key] = photo
        self.create_image(0, 0, image=photo, anchor="nw", tags="bg")
        self.tag_lower("bg")

    def focus_set(self) -> None:  # type: ignore[override]
        self.entry.focus_set()

    def set_enabled(self, value: bool) -> None:
        self.entry.configure(state="normal" if value else "disabled")
        self.configure(cursor="" if value else "arrow")


# ---------------------------------------------------------------- 可搜索下拉
class SearchSelect(tk.Canvas):
    """内嵌输入框的下拉：弹层里可按名称/分类/别名搜索、键盘选择，也能添加自定义项。

    ``provider`` 每次打开时重新取全部条目；``on_add(name)`` 返回 True 表示已保存，
    组件会自动刷新并选中。条目格式 ``(名称, 分类, 别名元组)``。
    """

    def __init__(self, master: tk.Misc, variable: tk.StringVar, ui: float, entries=None,
                 provider=None, on_add=None, height: float = 32,
                 text_font: tkfont.Font | None = None, placeholder: str = "输入或选择",
                 bg: str | None = None) -> None:
        self.ui = ui
        self._var = variable
        self._entries = list(entries or [])
        self._provider = provider
        self._on_add = on_add
        self._font = text_font or font(ui, 12)
        self._h = int(round(height * ui))
        self._placeholder = placeholder
        self._enabled = True
        self._focused = False
        self._hover = False
        self._top: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None
        self._query: tk.StringVar | None = None
        self._query_trace = ""
        self._shown: list[str] = []
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        super().__init__(master, width=int(round(200 * ui)), height=self._h,
                         bg=bg or _parent_bg(master), highlightthickness=0, bd=0,
                         takefocus=0, cursor="hand2")
        self.entry = tk.Entry(self, textvariable=variable, font=self._font,
                              bg=INPUT, fg=FG, disabledbackground=INPUT, disabledforeground=FG_FAINT,
                              relief="flat", bd=0, highlightthickness=0, insertbackground=FG,
                              selectbackground=BLUE_SOFT, selectforeground=FG)
        self._pad = int(round(11 * ui))
        self._arrow_px = int(round(12 * ui))
        self._arrow = canvas_icon("chevron", self._arrow_px, FG_FAINT)
        self._win = self.create_window(self._pad, self._h // 2, anchor="w", window=self.entry,
                                       width=1, height=max(10, self._h - 2 * max(1, int(round(ui)))))
        self._arrow_id = self.create_image(0, 0, image=self._arrow, anchor="e")
        self._hint = tk.Label(self, text=placeholder, bg=INPUT, fg=FG_FAINT, font=self._font,
                              anchor="w", cursor="xterm")
        self._hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.bind("<Configure>", self._layout)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self.entry.bind("<FocusIn>", lambda _e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._focus(False))
        self.entry.bind("<Down>", lambda _e: (self._open(), "break")[1])
        self.entry.bind("<Escape>", lambda _e: self._close(focus=True))
        self._trace = variable.trace_add("write", lambda *_: self._sync_hint())
        self.bind("<Destroy>", self._destroyed)
        self._paint()
        self._sync_hint()

    # ------------------------------------------------------------ 外观
    def _layout(self, _event=None) -> None:
        w = self.winfo_width()
        if w <= 1:
            w = int(self.cget("width"))
        avail = max(1, w - 2 * self._pad - self._arrow_px - int(round(10 * self.ui)))
        self.coords(self._win, self._pad, self._h // 2)
        self.itemconfigure(self._win, width=avail,
                           height=max(10, self._h - 2 * max(1, int(round(self.ui)))))
        self.coords(self._arrow_id, w - self._pad, self._h // 2)
        if self._hint.winfo_manager():
            self._hint.place_configure(x=self._pad + 1, y=0, height=self._h, width=avail)
        self._paint()

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        w = self.winfo_width()
        if w <= 1:
            w = int(self.cget("width"))
        if not self._enabled:
            edge = EDGE_SOFT
        elif self._focused:
            edge = BLUE
        elif self._hover or self._top is not None:
            edge = "#d2d2d8"
        else:
            edge = EDGE
        key = (w, self._h, edge)
        photo = self._photos.get(key)
        if photo is None:
            photo = rounded_photo(self, w, self._h, int(round(7 * self.ui)), INPUT, edge,
                                  max(1, int(round(self.ui))))
            self._photos[key] = photo
        self.delete("bg")
        self.create_image(0, 0, image=photo, anchor="nw", tags="bg")
        self.tag_lower("bg")

    def _sync_hint(self) -> None:
        if not self.winfo_exists():
            return
        show = self._enabled and not self._focused and not self._var.get().strip()
        if show:
            w = self.winfo_width()
            if w <= 1:
                w = int(self.cget("width"))
            avail = max(1, w - 2 * self._pad - self._arrow_px - int(round(10 * self.ui)))
            self._hint.place(x=self._pad + 1, y=0, height=self._h, width=avail)
            self._hint.lift()
        else:
            self._hint.place_forget()

    def _set_hover(self, value: bool) -> None:
        if self._hover != value:
            self._hover = value
            self._paint()

    def _focus(self, value: bool) -> None:
        self._focused = value
        self._paint()
        self._sync_hint()

    def _click(self, event) -> None:
        if not self._enabled:
            return
        chevron = self.winfo_width() - self._pad - self._arrow_px - int(round(6 * self.ui))
        if event.x >= chevron:
            self._open()
        else:
            self.entry.focus_set()

    def set_enabled(self, value: bool) -> None:
        value = bool(value)
        self._enabled = value
        self.entry.configure(state="normal" if value else "disabled")
        self.configure(cursor="hand2" if value else "arrow")
        if not value:
            self._close()
        self._sync_hint()
        self._paint()

    def set_entries(self, entries) -> None:
        self._entries = list(entries)
        if self._top is not None:
            self._filter()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self._hint.configure(text=text)

    # ------------------------------------------------------------ 弹层
    def _open(self) -> None:
        if not self._enabled:
            return
        if self._top is not None:
            self._close()
            return
        if self._provider is not None:
            try:
                self._entries = list(self._provider())
            except Exception:  # noqa: BLE001 地图表读失败不应让下拉打不开
                pass
        top = tk.Toplevel(self)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=EDGE)
        top.attributes("-topmost", True)
        card = tk.Frame(top, bg=CARD)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=int(round(8 * self.ui)),
                  pady=int(round(8 * self.ui)))

        self._query = tk.StringVar()
        search = EntryBox(body, self._query, self.ui, height=30, text_font=self._font)
        search.pack(fill="x")
        search.entry.bind("<Return>", lambda _e: self._choose_current())
        search.entry.bind("<Escape>", lambda _e: self._close(focus=True))
        search.entry.bind("<Down>", lambda _e: self._move(1))
        search.entry.bind("<Up>", lambda _e: self._move(-1))

        holder = tk.Frame(body, bg=CARD)
        holder.pack(fill="both", expand=True, pady=(int(round(6 * self.ui)), 0))
        self._listbox = tk.Listbox(holder, font=self._font, bg=CARD, fg=FG_DIM,
                                   selectbackground=BLUE_SOFT, selectforeground=FG,
                                   relief="flat", bd=0, highlightthickness=0, borderwidth=0,
                                   activestyle="none", exportselection=False, cursor="hand2",
                                   height=9)
        self._listbox.pack(fill="both", expand=True)
        self._listbox.bind("<ButtonRelease-1>", lambda _e: self._choose_current())
        self._listbox.bind("<Return>", lambda _e: self._choose_current())
        self._listbox.bind("<Escape>", lambda _e: self._close(focus=True))
        self._listbox.bind("<MouseWheel>", self._wheel)
        search.entry.bind("<MouseWheel>", self._wheel)

        self._add = tk.Label(body, text="", bg=BLUE_SOFT, fg=BLUE, font=self._font,
                             anchor="w", cursor="hand2",
                             padx=int(round(9 * self.ui)), pady=int(round(6 * self.ui)))
        self._add.bind("<Button-1>", lambda _e: self._add_current())
        self._add.bind("<Enter>", lambda _e: self._add.configure(bg="#e3edff"))
        self._add.bind("<Leave>", lambda _e: self._add.configure(bg=BLUE_SOFT))
        self._foot = tk.Label(body, text="", bg=CARD, fg=FG_FAINT, font=font(self.ui, 10),
                              anchor="w")
        self._foot.pack(fill="x", pady=(int(round(6 * self.ui)), 0))

        top.bind("<Escape>", lambda _e: self._close(focus=True))
        top.bind("<Button-1>", lambda e: self._close() if e.widget in (top, card) else None)
        self._top = top
        self._query_trace = self._query.trace_add("write", lambda *_: self._filter())
        self._filter()
        top.deiconify()
        top.lift()
        top.grab_set()
        search.entry.focus_force()
        self._paint()

    def _filter(self) -> None:
        if self._top is None or self._listbox is None or self._query is None:
            return
        query = self._query.get().strip()
        matches = match_entries(self._entries, query)
        self._shown = []
        self._listbox.delete(0, "end")
        for name, hint, _aliases in matches:
            self._shown.append(str(name))
            display = str(name) + (f"   ·   {hint}" if hint else "")
            self._listbox.insert("end", display)
        if self._shown:
            self._listbox.selection_set(0)
            self._listbox.activate(0)
        else:
            self._listbox.insert("end", "没有匹配的地图")
        self._listbox.configure(height=max(2, min(9, len(self._shown))))
        names = {str(name) for name, _hint, _aliases in self._entries}
        if query and query not in names and self._on_add is not None:
            self._add.configure(text=f"＋  添加自定义地图「{_elide(self._font, query, int(round(220 * self.ui)))}」")
            if not self._add.winfo_manager():
                self._add.pack(fill="x", pady=(int(round(6 * self.ui)), 0), before=self._foot)
        elif self._add.winfo_manager():
            self._add.pack_forget()
        if self._shown:
            tail = "滚轮查看更多" if len(self._shown) > 9 else "回车选择"
            self._foot.configure(text=f"共 {len(self._shown)} 个 · 输入筛选，{tail}", fg=FG_FAINT)
        else:
            self._foot.configure(text="没有匹配 · 输入完整名称后可添加", fg=FG_FAINT)
        self._resize()

    def _resize(self) -> None:
        top = self._top
        if top is None:
            return
        top.update_idletasks()
        w = max(self.winfo_width(), int(round(340 * self.ui)))
        h = top.winfo_reqheight()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + int(round(4 * self.ui))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if y + h > sh - int(round(8 * self.ui)):
            y = self.winfo_rooty() - h - int(round(4 * self.ui))
        x = max(int(round(4 * self.ui)), min(x, sw - w - int(round(8 * self.ui))))
        top.geometry(f"{w}x{h}+{x}+{y}")

    def _move(self, delta: int):
        if self._listbox is None or not self._shown:
            return "break"
        selected = self._listbox.curselection()
        index = (selected[0] if selected else -1) + delta
        index = max(0, min(len(self._shown) - 1, index))
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(index)
        self._listbox.activate(index)
        self._listbox.see(index)
        return "break"

    def _wheel(self, event):
        if self._listbox is not None and self._shown:
            self._listbox.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _choose_current(self) -> None:
        if self._listbox is None:
            return
        selected = self._listbox.curselection()
        if not selected:
            return
        index = selected[0]
        if 0 <= index < len(self._shown):
            self._choose(self._shown[index])

    def _choose(self, name: str) -> None:
        self._var.set(name)
        self._close(focus=True)

    def _add_current(self) -> None:
        if self._query is None or self._on_add is None:
            return
        name = self._query.get().strip()
        if not name:
            return
        try:
            ok = bool(self._on_add(name))
        except Exception:  # noqa: BLE001 保存失败时停在弹层里，方便重试
            ok = False
        if not ok:
            self._foot.configure(text="名称不可用：可能过长或已存在", fg=WARN)
            return
        if self._provider is not None:
            try:
                self._entries = list(self._provider())
            except Exception:  # noqa: BLE001
                self._entries.append((name, "自定义", ()))
        else:
            self._entries.append((name, "自定义", ()))
        self._choose(name)

    def _close(self, focus: bool = False) -> None:
        top, self._top = self._top, None
        if self._query is not None:
            try:
                self._query.trace_remove("write", self._query_trace)
            except (tk.TclError, ValueError):
                pass
            self._query = None
        self._listbox = None
        if top is not None:
            try:
                top.grab_release()
                top.destroy()
            except tk.TclError:
                pass
        if focus and self.winfo_exists():
            self.entry.focus_set()
        self._paint()

    def _destroyed(self, event) -> None:
        if event.widget is not self:
            return
        self._close()
        try:
            self._var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass


# ---------------------------------------------------------------- 卡片
class Card(tk.Frame):
    """圆角白卡片：内边距至少等于圆角半径，四角不会被内容盖住。"""

    def __init__(self, master: tk.Misc, ui: float, pad: tuple[float, float] = (16, 14),
                 radius: float = 10, stretch: bool = False) -> None:
        super().__init__(master, bg=BG, bd=0, highlightthickness=0)
        self.ui = ui
        self._padx = int(round(pad[0] * ui))
        self._pady = int(round(pad[1] * ui))
        self._radius = int(round(radius * ui))
        self._stretch = stretch
        self._need = 0
        self._last: tuple[int, int] | None = None
        self._bg_photos: dict[tuple, ImageTk.PhotoImage] = {}
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=CARD)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas)
        self.body.bind("<Configure>", self._on_body)

    def _on_canvas(self, event) -> None:
        size = (event.width, event.height)
        if size == self._last:
            return
        self._last = size
        w, h = size
        self.canvas.itemconfigure(self._win, width=max(1, w - 2 * self._padx))
        if self._stretch:
            self.canvas.itemconfigure(self._win, height=max(1, h - 2 * self._pady))
        self.canvas.coords(self._win, self._padx, self._pady)
        self.canvas.delete("bg")
        key = (w, h)
        photo = self._bg_photos.get(key)
        if photo is None:
            photo = rounded_photo(self.canvas, w, h, self._radius, CARD, EDGE_SOFT,
                                  max(1, int(round(self.ui))))
            self._bg_photos[key] = photo
        self.canvas.create_image(0, 0, image=photo, anchor="nw", tags="bg")
        self.canvas.tag_lower("bg")

    def _on_body(self, event) -> None:
        if self._stretch:
            return
        need = event.height + 2 * self._pady
        if need != self._need:
            self._need = need
            self.canvas.configure(height=need)
