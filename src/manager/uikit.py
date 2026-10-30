"""管理台的配色、图标与品牌图形。

只负责“好看”：颜色、矢量化图标、窗口图标、空状态插画和状态胶囊。
业务逻辑仍然在 app.py / session.py 里，这里不碰。
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

# ---------------------------------------------------------------- 调色板
# 参考 Qoder 的浅色界面：白色主面、浅灰侧栏、发丝线、黑色主操作。
BG = "#f2f3f5"
HEADER = "#ffffff"
TOOLBAR = "#ffffff"
SIDE = "#f7f7f8"
FOOTER = "#fafafa"
CARD = "#ffffff"
CARD_HOVER = "#fafafa"
EDGE = "#e3e3e7"
EDGE_SOFT = "#ececef"
PIC_BG = "#f3f3f5"
INPUT = "#f5f5f7"
INPUT_HOVER = "#ececef"
FG = "#1d1d20"
FG_DIM = "#5f6068"
FG_FAINT = "#9a9aa3"
ACCENT = "#1d1d20"
ACCENT_HOVER = "#000000"
ACCENT_TEXT = "#ffffff"
SELECT = ACCENT
OK = "#1f883d"
WARN = "#bc4c00"
BAD = "#cf222e"
BUSY = "#9a6700"
IDLE = "#8c8c94"

LEVEL = {"ok": OK, "warn": WARN, "bad": BAD, "busy": BUSY, "idle": IDLE}
PILL_FILL = {"ok": "#e7f6ec", "warn": "#fff0e4", "bad": "#ffebe9", "busy": "#fbf3d9", "idle": "#f2f2f4"}
PILL_EDGE = {"ok": "#c3e8d0", "warn": "#f7d4b5", "bad": "#f5c9c6", "busy": "#ead9a6", "idle": "#e4e4e8"}

# 品牌图形走蓝色，只用在标题栏/任务栏图标和空状态插画上
BLUE = "#2563eb"
BLUE_LIGHT = "#4f8dff"
BLUE_SOFT = "#eef4ff"
BLUE_EDGE = "#d8e5ff"

# 画面展示台（截图居中，四周留浅灰底）
PIC_TEXT = "#a3a3ab"
PIC_GRID = "#ededf0"
PIC_CORNER = "#d9d9de"
PIC_LEVEL = dict(LEVEL)

# 画面格子的内边距/标题/页脚（逻辑像素，再乘界面缩放）
TILE_PAD, TILE_HEAD, TILE_GAP, TILE_FOOT = 8, 30, 5, 26
THUMB_PAD, THUMB_HEAD, THUMB_GAP = 6, 22, 4


def tile_chrome(ui: float, thumb: bool = False) -> int:
    """格子里除画面以外的高度（物理像素）。"""
    if thumb:
        return int((THUMB_PAD * 2 + THUMB_HEAD + THUMB_GAP) * ui)
    return int((TILE_PAD * 2 + TILE_HEAD + TILE_GAP * 2 + TILE_FOOT) * ui)


# ---------------------------------------------------------------- 矢量图标
def _glyph(draw: ImageDraw.ImageDraw, name: str, size: int, color: str) -> None:
    """在 size×size 的画布上绘制一个线性图标（坐标归一化）。"""
    s = float(size)
    w = max(1, round(s * 0.075))

    def pt(x: float, y: float) -> tuple[float, float]:
        return x * s, y * s

    def line(x1: float, y1: float, x2: float, y2: float, width: int | None = None) -> None:
        draw.line([pt(x1, y1), pt(x2, y2)], fill=color, width=width or w, joint="curve")

    def box(x1: float, y1: float, x2: float, y2: float, r: float = 0.12,
            outline: bool = True, fill: bool = False, width: int | None = None) -> None:
        draw.rounded_rectangle(
            [*pt(x1, y1), *pt(x2, y2)], radius=r * s,
            outline=color if outline else None, fill=color if fill else None,
            width=width or w,
        )

    def poly(points: list[tuple[float, float]], fill: bool = False) -> None:
        draw.polygon([pt(*p) for p in points], fill=color if fill else None,
                     outline=None if fill else color, width=0 if fill else w)

    def arc(cx: float, cy: float, r: float, start: float, end: float, width: int | None = None) -> None:
        draw.arc([*pt(cx - r, cy - r), *pt(cx + r, cy + r)], start=start, end=end,
                 fill=color, width=width or w)

    if name == "monitor":
        box(0.08, 0.12, 0.92, 0.66, 0.08)
        line(0.5, 0.66, 0.5, 0.8)
        line(0.3, 0.86, 0.7, 0.86)
    elif name == "alert":
        poly([(0.5, 0.08), (0.94, 0.88), (0.06, 0.88)])
        line(0.5, 0.36, 0.5, 0.62)
        draw.ellipse([*pt(0.46, 0.71), *pt(0.54, 0.79)], fill=color)
    elif name == "devices":
        box(0.03, 0.18, 0.45, 0.58, 0.07)
        line(0.24, 0.58, 0.24, 0.7)
        line(0.14, 0.76, 0.34, 0.76)
        box(0.55, 0.18, 0.97, 0.58, 0.07)
        line(0.76, 0.58, 0.76, 0.7)
        line(0.66, 0.76, 0.86, 0.76)
    elif name == "plus":
        line(0.5, 0.14, 0.5, 0.86)
        line(0.14, 0.5, 0.86, 0.5)
    elif name == "play":
        poly([(0.3, 0.16), (0.84, 0.5), (0.3, 0.84)], fill=True)
    elif name == "stop":
        box(0.26, 0.26, 0.74, 0.74, 0.12, outline=False, fill=True)
    elif name == "back":
        line(0.86, 0.5, 0.16, 0.5)
        line(0.4, 0.26, 0.16, 0.5)
        line(0.4, 0.74, 0.16, 0.5)
    elif name == "camera":
        line(0.36, 0.2, 0.64, 0.2)
        box(0.08, 0.28, 0.92, 0.86, 0.09)
        draw.ellipse([*pt(0.33, 0.45), *pt(0.67, 0.79)], outline=color, width=w)
    elif name == "edit":
        poly([(0.16, 0.84), (0.22, 0.62), (0.66, 0.18), (0.82, 0.34), (0.38, 0.78)])
        line(0.16, 0.84, 0.38, 0.78)
    elif name == "trash":
        line(0.14, 0.24, 0.86, 0.24)
        box(0.26, 0.24, 0.74, 0.9, 0.06)
        line(0.42, 0.36, 0.42, 0.78)
        line(0.58, 0.36, 0.58, 0.78)
        box(0.38, 0.13, 0.62, 0.24, 0.03)
    elif name == "refresh":
        arc(0.5, 0.5, 0.38, 35, 325)
        line(0.84, 0.32, 0.84, 0.54)
        line(0.84, 0.32, 0.62, 0.32)
    elif name == "power":
        arc(0.5, 0.54, 0.36, 295, 245)
        line(0.5, 0.12, 0.5, 0.5)
    elif name == "chevron":
        line(0.3, 0.4, 0.5, 0.62)
        line(0.5, 0.62, 0.7, 0.4)
    elif name == "check":
        line(0.16, 0.52, 0.4, 0.76)
        line(0.4, 0.76, 0.84, 0.26)
    elif name == "close":
        line(0.2, 0.2, 0.8, 0.8)
        line(0.8, 0.2, 0.2, 0.8)
    elif name == "more":
        for cx in (0.18, 0.5, 0.82):
            r = 0.075
            draw.ellipse([*pt(cx - r, 0.5 - r), *pt(cx + r, 0.5 + r)], fill=color)
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


@lru_cache(maxsize=256)
def make_icon(name: str, size: int, color: str) -> ctk.CTkImage:
    """逻辑尺寸 size 的 CTkImage；内部用 4 倍图，DPI 放大后依然清晰。"""
    img = _icon_master(name, size * 4, color)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


_PHOTO_CACHE: dict[tuple, ImageTk.PhotoImage] = {}


def canvas_icon(name: str, px: int, color: str) -> ImageTk.PhotoImage:
    key = (name, int(px), color)
    if key not in _PHOTO_CACHE:
        _PHOTO_CACHE[key] = ImageTk.PhotoImage(_icon_master(name, int(px), color))
    return _PHOTO_CACHE[key]


# ---------------------------------------------------------------- 品牌图形
@lru_cache(maxsize=8)
def _logo_master(px: int) -> Image.Image:
    """蓝色圆角标 + 白色显示器；小尺寸用纯色底，缩到 16px 也不会糊。"""
    n = max(16, int(px))
    s = n * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if n >= 48:
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
    else:
        ImageDraw.Draw(img).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.26, fill=BLUE)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle((s * 0.19, s * 0.20, s * 0.81, s * 0.62), radius=s * 0.055,
                        fill=(255, 255, 255, 255))
    d.rectangle((s * 0.455, s * 0.62, s * 0.545, s * 0.735), fill=(255, 255, 255, 255))
    d.rounded_rectangle((s * 0.32, s * 0.735, s * 0.68, s * 0.80), radius=s * 0.03,
                        fill=(255, 255, 255, 255))
    return img.resize((n, n), Image.LANCZOS)


@lru_cache(maxsize=4)
def make_empty_art(size: int) -> ctk.CTkImage:
    px = max(80, int(size))
    s = px * 3
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s / 2
    for r, color, width in ((s * 0.46, BLUE_EDGE, 2), (s * 0.37, "#f1f6ff", 2)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=width)
    # 外圈虚线
    for deg in range(0, 360, 18):
        rad = math.radians(deg)
        x1, y1 = cx + s * 0.455 * math.cos(rad), cy + s * 0.455 * math.sin(rad)
        x2, y2 = cx + s * 0.49 * math.cos(rad), cy + s * 0.49 * math.sin(rad)
        d.line((x1, y1, x2, y2), fill="#bdd3f8", width=2)
    # 轨道上的小节点
    for deg, color in ((-38, "#2fb36d"), (52, BLUE), (142, "#8ab4ff")):
        rad = math.radians(deg)
        x, y = cx + s * 0.415 * math.cos(rad), cy + s * 0.415 * math.sin(rad)
        r = s * 0.022
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)
    logo = _logo_master(int(s * 0.42))
    img.alpha_composite(logo, (int(cx - logo.width / 2), int(cy - logo.height / 2)))
    return ctk.CTkImage(light_image=img.resize((px, px), Image.LANCZOS),
                        dark_image=img.resize((px, px), Image.LANCZOS), size=(px, px))


def _asset_path(name: str) -> str:
    """打包后从 _MEIPASS/assets 取，源码运行时从 runtime/ 取。"""
    base = getattr(sys, "_MEIPASS", "")
    folder = os.path.join(base, "assets") if base else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "runtime")
    return os.path.join(folder, name)


def set_window_icon(root: tk.Misc) -> None:
    """用多尺寸 ICO 设置标题栏/任务栏图标；CTk 的 iconbitmap 会同时打上防覆盖标记。"""
    try:
        root.iconbitmap(_asset_path("mhxy_manager.ico"))
        return
    except tk.TclError:
        pass
    img = ImageTk.PhotoImage(_logo_master(64), master=root)
    root._app_icon = img  # type: ignore[attr-defined]
    root._iconbitmap_method_called = True  # 阻止 CustomTkinter 再换回默认图标
    try:
        root.iconphoto(True, img)
    except tk.TclError:
        pass


def set_light_titlebar(window: tk.Misc) -> None:
    """Windows 11 标题栏跟随浅色顶栏，避免窗口顶部露出深色条。"""
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    try:
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        off = ctypes.c_int(0)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(off), ctypes.sizeof(off))
        caption = ctypes.c_uint(0x00F6F6F6)  # COLORREF，HEADER 的 BGR
        text = ctypes.c_uint(0x00201D1D)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------- 占位画面
def draw_placeholder(canvas: tk.Canvas, w: int, h: int, short: str, level: str,
                     font_short: tuple, ui: float) -> None:
    """设备未出画面时，在截图区域画一个监控图标 + 状态说明。"""
    canvas.delete("all")
    if w < 40 or h < 30:
        return
    step = max(18, int(28 * ui))
    for x in range(step, w, step):
        canvas.create_line(x, 0, x, h, fill=PIC_GRID)
    for y in range(step, h, step):
        canvas.create_line(0, y, w, y, fill=PIC_GRID)

    arm = max(8, int(14 * ui))
    for x, y, dx, dy in ((10, 10, 1, 1), (w - 10, 10, -1, 1), (10, h - 10, 1, -1), (w - 10, h - 10, -1, -1)):
        canvas.create_line(x, y, x + dx * arm, y, fill=PIC_CORNER, width=max(1, int(ui)))
        canvas.create_line(x, y, x, y + dy * arm, fill=PIC_CORNER, width=max(1, int(ui)))

    color = PIC_LEVEL.get(level, PIC_TEXT)
    icon_px = int(min(58 * ui, max(26, w * 0.16), h * 0.30))
    icon = canvas_icon("monitor", icon_px, PIC_TEXT)
    cy = h / 2 - icon_px * 0.62
    canvas.create_image(w / 2, cy, image=icon)

    dot = canvas_icon("dot", max(7, int(9 * ui)), color)
    canvas.create_image(w / 2 + icon_px * 0.42, cy + icon_px * 0.36, image=dot)

    text_color = color if level != "idle" else FG_DIM
    canvas.create_text(w / 2, cy + icon_px * 0.72 + 6 * ui, text=short, fill=text_color, font=font_short)


# ---------------------------------------------------------------- 状态胶囊
class StatusPill(ctk.CTkFrame):
    """带图标的状态小胶囊，颜色随状态变化。"""

    def __init__(self, master, font: tuple, height: int = 22, icon: str = "dot",
                 icon_size: int = 9, pad: tuple[int, int] = (8, 9)) -> None:
        super().__init__(master, fg_color=PILL_FILL["idle"], corner_radius=max(2, height // 2), height=height)
        self._icon_name = icon
        self._icon_size = icon_size
        self._level = "idle"
        self._text = ""
        self._font = font
        self._pad = pad
        try:
            self._measure = font.measure
        except AttributeError:
            self._tkfont = tkfont.Font(font=font)
            self._measure = self._tkfont.measure
        self._img = make_icon(icon, icon_size, IDLE)
        self._lbl_icon = ctk.CTkLabel(self, text="", image=self._img, width=icon_size, height=icon_size,
                                      bg_color=PILL_FILL["idle"])
        self._lbl_icon.pack(side="left", padx=(pad[0], 4), pady=0)
        self._lbl_text = ctk.CTkLabel(self, text="", font=font, text_color=FG_DIM, height=height - 2,
                                      bg_color=PILL_FILL["idle"])
        self._lbl_text.pack(side="left", padx=(0, pad[1]), pady=0)

    def set(self, level: str, text: str) -> None:
        if level == self._level and text == self._text:
            return
        self._level, self._text = level, text
        color = LEVEL.get(level, IDLE)
        fill = PILL_FILL.get(level, PILL_FILL["idle"])
        self._img = make_icon(self._icon_name, self._icon_size, color)
        self._lbl_icon.configure(image=self._img, bg_color=fill)
        self._lbl_text.configure(text=text, text_color=color if level != "idle" else FG_DIM, bg_color=fill)
        # CTkFrame 默认最小宽 200，这里按内容收紧，右上角才排得下
        scale = self._get_widget_scaling() or 1.0
        fixed = self._icon_size + self._pad[0] + 4 + self._pad[1]
        self.configure(fg_color=fill, width=math.ceil(fixed + self._measure(text) / scale + 2))

    def bind_click(self, sequence: str, command) -> None:
        for w in (self, self._lbl_icon, self._lbl_text):
            w.bind(sequence, command)


# ---------------------------------------------------------------- 下拉选择
@lru_cache(maxsize=32)
def _select_bg(w: int, h: int, fill: str, edge: str, radius: int, border: int) -> Image.Image:
    s = 4
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        (0, 0, w * s - 1, h * s - 1), radius=radius * s, fill=fill, outline=edge, width=border * s)
    return img.resize((w, h), Image.LANCZOS)


class Select(tk.Canvas):
    """扁平下拉：发丝描边 + 细箭头 + 自绘弹层，避免 CTkComboBox 的粗箭头和接缝。"""

    def __init__(self, master, values, variable, width: int, height: int, font, ui: float,
                 bg: str = HEADER, command=None) -> None:
        self._cw, self._ch = int(width * ui), int(height * ui)
        super().__init__(master, width=self._cw, height=self._ch, bg=bg, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=0)
        self._values = list(values)
        self._var = variable
        self._font = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
        self._ui = ui
        self._command = command
        self._hover = False
        self._top: tk.Toplevel | None = None
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._radius = max(4, int(round(7 * ui)))
        self._border = max(1, int(round(ui * 0.5)))
        self._trace = variable.trace_add("write", lambda *_: self._paint())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._toggle())
        self.bind("<Destroy>", self._on_destroy)
        self._paint()

    def _paint(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        active = self._hover or self._top is not None
        fill = INPUT if active else HEADER
        edge = "#d2d2d8" if active else EDGE
        photo = self._photos.get((fill, edge))
        if photo is None:
            photo = ImageTk.PhotoImage(
                _select_bg(self._cw, self._ch, fill, edge, self._radius, self._border), master=self)
            self._photos[(fill, edge)] = photo
        self.create_image(0, 0, image=photo, anchor="nw")
        self.create_text(int(12 * self._ui), self._ch / 2, text=self._var.get(), anchor="w",
                         font=self._font, fill=FG)
        self.create_image(self._cw - int(12 * self._ui), self._ch / 2, anchor="e",
                          image=canvas_icon("chevron", int(12 * self._ui), FG_FAINT))

    def _set_hover(self, value: bool) -> None:
        if self._hover != value:
            self._hover = value
            self._paint()

    def _toggle(self) -> None:
        if self._top is not None:
            self._close()
        else:
            self._open()

    def _open(self) -> None:
        top = tk.Toplevel(self)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=EDGE)
        top.attributes("-topmost", True)
        card = tk.Frame(top, bg=CARD)
        card.pack(fill="both", expand=True, padx=self._border, pady=self._border)
        row_h, pad = int(30 * self._ui), int(4 * self._ui)
        width = max(self._cw, max(self._font.measure(v) for v in self._values) + int(48 * self._ui))
        current = self._var.get()
        for value in self._values:
            row = tk.Frame(card, bg=CARD, height=row_h)
            row.pack(fill="x", padx=pad)
            row.pack_propagate(False)
            lbl = tk.Label(row, text=value, font=self._font, bg=CARD,
                           fg=FG if value == current else FG_DIM, anchor="w")
            lbl.pack(side="left", fill="both", expand=True, padx=(int(8 * self._ui), 0))
            mark = tk.Label(row, text="", bg=CARD, fg=FG_DIM)
            mark.pack(side="right", padx=(0, int(8 * self._ui)))
            if value == current:
                mark.configure(image=canvas_icon("check", int(10 * self._ui), FG_DIM))
            widgets = (row, lbl, mark)

            def hover(_e, ws=widgets) -> None:
                for w in ws:
                    w.configure(bg=CARD_HOVER)

            def leave(_e, ws=widgets) -> None:
                for w in ws:
                    w.configure(bg=CARD)

            for w in widgets:
                w.bind("<Enter>", hover)
                w.bind("<Leave>", leave)
                w.bind("<Button-1>", lambda _e, v=value: self._choose(v))

        self._top = top
        top.update_idletasks()
        h = top.winfo_reqheight()
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + int(4 * self._ui)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if y + h > sh - int(8 * self._ui):
            y = self.winfo_rooty() - h - int(4 * self._ui)
        x = max(int(4 * self._ui), min(x, sw - width - int(8 * self._ui)))
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

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        self._close()
        try:
            self._var.trace_remove("write", self._trace)
        except tk.TclError:
            pass
