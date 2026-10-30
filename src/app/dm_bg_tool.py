"""大漠后台绑定测试台

用途：验证"绑到某个窗口之后，后台鼠标/键盘到底能不能操作它"，并把每次结果
记录下来，方便横向比较不同目标、不同绑定档位。

两条测试路径（参数阶梯完全不同，切换时整体替换）：
  本机程序  —— 游戏/程序本体：DX 渲染 + 自己读输入 + 可能有保护 → 必须注入
  远程窗口  —— 远程查看器：GDI 渲染 + 标准窗口消息 + 无保护 → 消息模式即可

为什么不能只看"变化率"：
  大漠某种抓图档（例如用 dx2 去抓 GDI 窗口）可能拿到一张**永不刷新的静态位图**，
  这时变化率永远是 0，看起来像"输入全失败"，其实是测量坏了。所以每次动作都同时
  抓两路：大漠抓的 + 用 PrintWindow 直接抓的参照图，两者对比才能判读：
      大漠0 / 参照>0  → 输入生效了，是大漠抓图冻结（换抓图档）
      大漠0 / 参照0   → 输入真的没生效
      大漠>0          → 正常

用法：32 位 Python + 管理员权限（大漠 Reg 需要）。打包版直接双击。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
import tkinter as tk
import traceback
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import pythoncom
import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.config import load_yaml, resources_path  # noqa: E402

OUT_DIR = os.path.join(tempfile.gettempdir(), "mhxy_dm_bg_tool")
os.makedirs(OUT_DIR, exist_ok=True)

STATE_DIR = os.path.join(os.environ.get("APPDATA") or tempfile.gettempdir(), "mhxy")
STATE_FILE = os.path.join(STATE_DIR, "dm_bg_tool.json")

# 判定用阈值：低于这个比例认为"画面没变"
CHANGE_EPS = 0.002
# 大漠图与真实画面差异超过这个比例，判定该档抓图不可信
CAPTURE_MISMATCH = 0.05


# =============================================================================
# 参数阶梯
# =============================================================================
# tuple 结构：(display, mouse, keypad, public, mode, 说明)
# 每级只改一个变量，方便定位到底是抓图档还是输入档的问题。

LOCAL_MODES: dict[str, tuple[str, str, str, str, int, str]] = {
    "L1 · dx2 + dx.mouse.* · 101":
        ("dx2",
         "dx.mouse.state.api|dx.mouse.api|dx.mouse.cursor",
         "dx.keypad.state.api|dx.keypad.api",
         "dx.public.active.api|dx.public.active.message|dx.public.hide.dll|"
         "dx.public.graphic.protect|dx.public.anti.api|dx.public.km.protect|"
         "dx.public.down.cpu",
         101,
         "就是 driver.yaml 里 bind 那套，注入+隐藏 dll，需要管理员"),
    "L2 · dx2 + windows2 · 0":
        ("dx2", "windows2", "windows", "", 0,
         "windows2 是 dx 缩写(等价 dx.mouse.position.lock.api|.lock.message|.state.message)，"
         "仍是注入，但比 L1 温和"),
    "L3 · dx2 + windows · 0":
        ("dx2", "windows", "windows", "", 0,
         "抓图用 dx2，输入纯消息。游戏不读窗口消息，预期点击无效——用来验证这个判断"),
    "L4 · gdi + windows · 0":
        ("gdi", "windows", "windows", "", 0,
         "和远程窗口那条路的第一档完全一样，换到本机程序上做交叉对照"),
    "L5 · normal + windows · 0":
        ("normal", "windows", "windows", "", 0,
         "前台截屏 + 消息输入，要求窗口可见，仅作对照"),
}

REMOTE_MODES: dict[str, tuple[str, str, str, str, int, str]] = {
    "1 · gdi + windows（首选）":
        ("gdi", "windows", "windows", "", 0,
         "查看器是 GDI 窗口，纯消息模式即可，不往 tvnviewer 注入任何东西"),
    "2 · gdi2 + windows":
        ("gdi2", "windows", "windows", "", 0,
         "gdi 能点但抓图黑屏/后台不刷新时换这个，兼容性更强但更慢"),
    "3 · gdi + windows3":
        ("gdi", "windows3", "windows", "", 0,
         "点击落到了错误的子窗口上时用，windows3 支持多子窗口的后台"),
    "4 · gdi + windows + active":
        ("gdi", "windows", "windows", "dx.public.active.api", 0,
         "画面正常但点击完全没反应时试。注意它需要注入查看器进程，已不再是零注入"),
    "5 · normal + windows":
        ("normal", "windows", "windows", "", 0,
         "normal 是前台截屏，要求查看器一直可见不被遮挡，仅作对照"),
    "6 · dx2 + windows2":
        ("dx2", "windows2", "windows", "", 0,
         "注入查看器进程。查看器没有 DX 管线，这一档大概率没收益，只作验证"),
    "7 · dx2 + dx.mouse.* · 101":
        ("dx2",
         "dx.mouse.state.api|dx.mouse.api|dx.mouse.cursor",
         "dx.keypad.state.api|dx.keypad.api",
         "dx.public.active.api|dx.public.active.message|dx.public.hide.dll|"
         "dx.public.graphic.protect|dx.public.anti.api|dx.public.km.protect|"
         "dx.public.down.cpu",
         101,
         "注入查看器进程，可能让查看器崩，先备好 VNC 密码"),
}

TARGETS: dict[str, dict] = {
    "本机程序": {
        "desc": "目标 = 程序本体（DX 渲染 + DX 输入 + 可能有保护）。"
                "注入通道才有效；L3/L4 是纯消息对照，预期点击无效。",
        "title_keys": ["梦幻西游", "记事本", "notepad", "画图", "mspaint"],
        "class_keys": ["wsgame", "notepad", "mspaintapp"],
        "modes": LOCAL_MODES,
    },
    "远程窗口": {
        "desc": "目标 = 远程查看器（GDI 渲染 + 标准窗口消息 + 无保护）。"
                "纯消息模式即可，不需要注入。",
        "title_keys": ["vnc", "viewer", "远程"],
        "class_keys": ["tvnviewer", "vncviewer"],
        "modes": REMOTE_MODES,
    },
}

VERDICT_OK = "输入生效"
VERDICT_FROZEN = "抓图冻结"
VERDICT_NONE = "无变化"
VERDICT_FAIL = "抓图失败"

HELP_TEXT = """一、先想清楚测什么
  本机程序：目标自己读输入（DirectInput/轮询按键），必须注入，看 L1/L2。
  远程窗口：查看器是普通 GDI 窗口，消息模式就够，看 1/2/3 档。

二、绑定
  1) 选测试路径
  2) 拖准星选窗口，或点"自动查找"
  3) 选档位 → Bind
  绑定成功后会自动跑一次"抓图自检"，先确认这一档抓图可信，
  否则后面的变化率全是 0，会误导你以为输入失败。

三、取点与发事件
  鼠标移到目标位置 → 按 F8（或"取坐标(3秒)"）→ X/Y 自动填好
  → 把鼠标移开 → 点左键/右键/双击/按键…
  每次动作都会同时报"大漠变化率"和"参照变化率"：
    大漠0 参照>0 → 输入生效，但大漠抓图冻结，换抓图档
    两个都 0     → 输入没生效，或这个动作本身没有视觉反馈

四、判读与记录
  每次动作都会进"结果"标签页，可以导出 CSV。
  同一目标想一次试完所有档位，用"全档扫描"（会自动逐档绑定、自检、点击、解绑）。

五、常用排错
  · 变化率永远 0.000 → 先点"抓图自检"，多半是抓图档选错了
  · 点击无反应 → 先用 SetMouseDelay 思路调大"等待"毫秒，再考虑换档
  · 绑定失败 err=5 / 无权限 → 用管理员身份运行
  · Move 没有可见变化是正常的，判断输入要用能看见效果的动作"""


# =============================================================================
# Win32 基础
# =============================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.windll.gdi32

# WindowFromPoint 需要物理像素，所以本进程声明 per-monitor v2 DPI awareness
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except OSError:
    pass


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _win_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _win_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def win_from_point(x: int, y: int) -> int:
    pt = POINT(x, y)
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    return root or hwnd


def client_coords(hwnd: int, sx: int, sy: int) -> tuple[int, int]:
    """屏幕坐标 -> 目标窗口客户区坐标。

    大漠所有接口用的都是客户区坐标（不含标题栏/边框），取点必须做这个换算，
    否则拿屏幕坐标去 MoveTo 会整体偏一条标题栏。
    """
    pt = POINT(sx, sy)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def client_size(hwnd: int) -> tuple[int, int]:
    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def win_info(hwnd: int) -> dict:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil  # 可选，没有就显示 ?
        proc = psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        proc = "?"
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    cw, ch = client_size(hwnd)
    return {
        "hwnd": hwnd, "title": _win_title(hwnd), "class": _win_class(hwnd),
        "process": proc,
        "window_rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
        "client_size": (cw, ch),
        "iconic": bool(user32.IsIconic(hwnd)),
        "visible": bool(user32.IsWindowVisible(hwnd)),
    }


def enum_visible_windows() -> list[int]:
    hits: list[int] = []

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return hits


def match_windows(title_keys: list[str], class_keys: list[str]) -> list[int]:
    """按标题/类名关键字找窗口，忽略大小写，命中任一即可。"""
    out = []
    for hwnd in enum_visible_windows():
        title = _win_title(hwnd).lower()
        cls = _win_class(hwnd).lower()
        if any(k.lower() in title for k in title_keys) or any(k.lower() in cls for k in class_keys):
            out.append(hwnd)
    return out


def draw_focus_rect(hwnd: int, prev: Optional[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """用 R2_NOT 在屏幕上画框。再次用同样参数画一次就相当于擦除。"""
    r = RECT()
    if hwnd:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
    else:
        rect = (0, 0, 0, 0)
    hdc = user32.GetDC(0)
    pen = gdi32.CreatePen(0, 3, 0x0000FF)
    old_pen = gdi32.SelectObject(hdc, pen)
    old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))  # NULL_BRUSH
    old_rop = gdi32.SetROP2(hdc, 6)  # R2_NOT
    if prev:
        gdi32.Rectangle(hdc, prev[0], prev[1], prev[2], prev[3])
    if hwnd:
        gdi32.Rectangle(hdc, rect[0], rect[1], rect[2], rect[3])
    gdi32.SetROP2(hdc, old_rop)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.DeleteObject(pen)
    user32.ReleaseDC(0, hdc)
    return rect


# =============================================================================
# 抓图：大漠一路 + PrintWindow 参照一路
# =============================================================================
def gdi_grab(hwnd: int) -> Optional[dict]:
    """完全绕开大漠，用 Windows 的 PrintWindow 直接抓一份参照画面。

    存在的理由：大漠某种抓图档可能拿到永不刷新的静态位图，此时"变化率=0"
    是测量坏了，不代表输入没生效。有这条独立通道才能把两种失败区分开。

    坑：PrintWindow 会把整个窗口（含标题栏）画到 (0,0)，所以必须按窗口矩形
    开画布，再用 ClientToScreen 求偏移裁出客户区，否则画面整体下移一条标题栏。
    """
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    ww, wh = r.right - r.left, r.bottom - r.top
    cw, ch = client_size(hwnd)
    if ww <= 0 or wh <= 0 or cw <= 0 or ch <= 0:
        return None

    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    ox, oy = pt.x - r.left, pt.y - r.top
    if ox < 0 or oy < 0 or ox + cw > ww or oy + ch > wh:
        ox = oy = 0
        cw, ch = min(cw, ww), min(ch, wh)

    hdc_win = user32.GetDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, ww, wh)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    got = 0
    method = ""
    try:
        ok = user32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT
        if ok:
            method = "PrintWindow"
        else:
            ok = user32.PrintWindow(hwnd, hdc_mem, 0)
            method = "PrintWindow0" if ok else ""
        if not ok:
            # 兜底：直接从窗口 DC 位块传送（要求窗口可见、未被遮挡）
            gdi32.BitBlt(hdc_mem, ox, oy, cw, ch, hdc_win, ox, oy, 0x00CC0020)
            method = "BitBlt"

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = ww
        bi.bmiHeader.biHeight = -wh  # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0  # BI_RGB
        full = ctypes.create_string_buffer(ww * wh * 4)
        got = gdi32.GetDIBits(hdc_mem, hbmp, 0, wh, full, ctypes.byref(bi), 0)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    if not got:
        return None

    src = full.raw
    out = bytearray(cw * ch * 4)
    for y in range(ch):
        s = ((y + oy) * ww + ox) * 4
        out[y * cw * 4:(y + 1) * cw * 4] = src[s:s + cw * 4]
    return {"w": cw, "h": ch, "px": bytes(out), "method": method}


def img_stats(img: Optional[dict]) -> Optional[dict]:
    """采样统计：平均亮度 + 唯一色数量，一眼看出全黑/全白/纯色。"""
    if not img:
        return None
    w, h, px = img["w"], img["h"], img["px"]
    total = w * h
    step = max(1, total // 8000)
    colors = []
    for i in range(0, total, step):
        o = i * 4
        colors.append((px[o + 2], px[o + 1], px[o]))  # BGRA -> RGB
    if not colors:
        return None
    return {"mean": sum(sum(c) for c in colors) / (3 * len(colors)),
            "uniq": len(set(colors)), "n": len(colors),
            "method": img.get("method", "")}


def diff_gdi(a: Optional[dict], b: Optional[dict]) -> float:
    """两张参照图的变化率（0~1），按采样像素逐点比。"""
    if not a or not b or a["w"] != b["w"] or a["h"] != b["h"]:
        return -1.0
    total = a["w"] * a["h"]
    step = max(1, total // 8000)
    pa, pb = a["px"], b["px"]
    diff = n = 0
    for i in range(0, total, step):
        o = i * 4
        if (abs(pa[o] - pb[o]) > 8 or abs(pa[o + 1] - pb[o + 1]) > 8
                or abs(pa[o + 2] - pb[o + 2]) > 8):
            diff += 1
        n += 1
    return diff / max(1, n)


def diff_gdi_vs_bmp(img: Optional[dict], bmp_path: str) -> float:
    """参照图 vs 一张大漠 BMP，用来判断大漠抓图是否可信。"""
    if not img or not os.path.exists(bmp_path):
        return -1.0
    with open(bmp_path, "rb") as f:
        data = f.read()
    if len(data) < 54 or data[:2] != b"BM":
        return -1.0
    off = int.from_bytes(data[10:14], "little")
    bw = int.from_bytes(data[18:22], "little", signed=True)
    bh = abs(int.from_bytes(data[22:26], "little", signed=True))
    bpp = int.from_bytes(data[28:30], "little")
    if bpp not in (24, 32) or bw != img["w"] or bh != img["h"]:
        return -1.0
    stride = ((bw * bpp // 8) + 3) // 4 * 4
    px = img["px"]
    total = bw * bh
    step = max(1, total // 8000)
    diff = n = 0
    for i in range(0, total, step):
        y, x = divmod(i, bw)
        src = off + (bh - 1 - y) * stride + x * (bpp // 8)  # BMP 默认自下而上
        if src + 2 >= len(data):
            continue
        o = i * 4
        if (abs(px[o] - data[src]) > 8 or abs(px[o + 1] - data[src + 1]) > 8
                or abs(px[o + 2] - data[src + 2]) > 8):
            diff += 1
        n += 1
    return diff / max(1, n)


def diff_bytes(a: str, b: str) -> float:
    """两张 BMP 的字节差比例。只对无压缩格式有意义（PNG 字节差会被压缩放大）。"""
    try:
        with open(a, "rb") as f1:
            da = f1.read()
        with open(b, "rb") as f2:
            db = f2.read()
    except OSError:
        return -1.0
    n = min(len(da), len(db))
    if n == 0:
        return -1.0
    step = max(1, n // 8000)
    diffs = sum(1 for i in range(0, n, step) if da[i] != db[i])
    size_delta = abs(len(da) - len(db)) / max(len(da), len(db))
    return max(diffs / max(1, n // step), size_delta)


def load_dm():
    """加载并注册大漠。必须在主线程（已 CoInitialize 的线程）调用。"""
    raw = load_yaml("config", "driver.yaml")
    dmreg = ctypes.WinDLL(resources_path("dll", raw["dll"]["dmreg_file"]))
    dmreg.SetDllPathW.argtypes = [wintypes.LPCWSTR, wintypes.LONG]
    dmreg.SetDllPathW.restype = wintypes.LONG
    if dmreg.SetDllPathW(resources_path("dll", raw["dll"]["dm_file"]), 0) != 1:
        raise RuntimeError("SetDllPathW 失败")
    dm = win32com.client.Dispatch("dm.dmsoft")
    ret = dm.Reg(raw["auth"]["reg_code"], raw["auth"]["ver_info"])
    if ret != 1:
        raise RuntimeError(f"大漠 Reg 失败 ret={ret}（-2 需要管理员权限）")
    return dm


# =============================================================================
# 界面
# =============================================================================
RESULT_COLS = [
    ("time", "时间", 58, "w"),
    ("target", "目标", 62, "w"),
    ("mode", "档位", 48, "w"),
    ("action", "动作", 78, "w"),
    ("xy", "坐标", 78, "w"),
    ("dm", "大漠", 52, "e"),
    ("ref", "参照", 52, "e"),
    ("verdict", "判读", 84, "w"),
    ("detail", "说明", 260, "w"),
]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("大漠后台绑定测试台")

        self.dm = None
        self._dm_ver = ""
        self.bound_hwnd = 0
        self.target_info: dict = {}
        self.results: list[dict] = []
        self._drag_prev_rect: Optional[tuple[int, int, int, int]] = None
        self._img_before: Optional[tk.PhotoImage] = None
        self._img_after: Optional[tk.PhotoImage] = None
        self._mark: Optional[tuple[int, int]] = None
        self._f8_down = False
        self._closing = False
        self._hotkey_after = None
        self._busy = False

        self._setup_scale()
        self._build_ui()
        self._apply_geometry()
        self._load_state()
        root.report_callback_exception = self._on_tk_error
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._init_dm)  # 先画窗口，再加载大漠
        self._poll_hotkey()

    # ---------------------------------------------------------------- 缩放/主题
    def _setup_scale(self) -> None:
        """按系统 DPI 缩放整套界面。

        本进程为了拿物理像素（WindowFromPoint / PrintWindow 都要）声明了
        per-monitor v2 DPI 感知，副作用是 Tk 自己不会缩放，在 200% 屏上字会小一半。
        所以这里显式把 tk scaling 设成 dpi/72，并让所有"点"单位的字号跟着走。
        """
        try:
            dpi = user32.GetDpiForSystem()
        except Exception:  # noqa: BLE001
            dpi = 96
        if dpi < 72 or dpi > 480:
            dpi = 96
        self.scale = max(1.0, dpi / 96.0)
        self.root.tk.call("tk", "scaling", dpi / 72.0)

        style = ttk.Style()
        for name in ("vista", "winnative", "clam"):
            if name in style.theme_names():
                style.theme_use(name)
                break

        ui_font = ("Microsoft YaHei UI", 9)
        bold_font = ("Microsoft YaHei UI", 9, "bold")
        title_font = ("Microsoft YaHei UI", 12, "bold")
        self._font_title = title_font
        self.root.option_add("*Font", ui_font)
        style.configure(".", font=ui_font)
        style.configure("Treeview", font=ui_font, rowheight=int(23 * self.scale))
        style.configure("Treeview.Heading", font=bold_font)
        style.configure("TLabelframe.Label", font=bold_font)
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Ok.TLabel", foreground="#0a7a45")
        style.configure("Bad.TLabel", foreground="#b3261e")
        style.configure("Head.TLabel", font=title_font)

    def _screen_info(self) -> str:
        wa = RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
        sw = wa.right - wa.left
        sh = wa.bottom - wa.top
        return f"屏幕 {sw}x{sh} · 缩放 {self.scale * 100:.0f}%"

    def _apply_geometry(self) -> None:
        """按屏幕可用区域自动定尺寸并居中，永远不用手拖。"""
        wa = RECT()
        ok = user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA
        sw = (wa.right - wa.left) if ok else user32.GetSystemMetrics(0)
        sh = (wa.bottom - wa.top) if ok else user32.GetSystemMetrics(1)
        if sw < 200 or sh < 200:
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        margin = int(20 * self.scale)
        # 设计尺寸按"逻辑像素"给，再乘缩放；同时不超过屏幕，屏幕小就自动收
        w = min(int(1180 * self.scale), sw - margin)
        h = min(int(940 * self.scale), sh - margin)
        w = max(w, min(int(760 * self.scale), sw - margin))
        h = max(h, min(int(520 * self.scale), sh - margin))
        x = wa.left + max(0, (sw - w) // 2)
        y = wa.top + max(0, (sh - h) // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(int(900 * self.scale), sw - margin),
                          min(int(560 * self.scale), sh - margin))

    # ---------------------------------------------------------------- UI 构建
    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="大漠后台绑定测试台", style="Head.TLabel").pack(side="left")
        self.lbl_header = ttk.Label(header, text="大漠未加载", style="Hint.TLabel")
        self.lbl_header.pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True)

        # 左栏放进可滚动容器：任何分辨率下都不会把按钮挤到看不见
        left_wrap = ttk.Frame(body)
        left_wrap.pack(side="left", fill="y")
        self.left_canvas = tk.Canvas(left_wrap, highlightthickness=0, borderwidth=0,
                                     width=int(420 * self.scale))
        self.left_canvas.pack(side="left", fill="y", expand=True)
        vsb = ttk.Scrollbar(left_wrap, orient="vertical", command=self.left_canvas.yview)
        vsb.pack(side="right", fill="y")
        self.left_canvas.configure(yscrollcommand=vsb.set)

        left = ttk.Frame(self.left_canvas, padding=(12, 2, 12, 12))
        self.left_inner = left
        self._left_window = self.left_canvas.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>", self._on_left_configure)
        self.left_canvas.bind("<Configure>", self._on_left_canvas_configure)

        right = ttk.Frame(body, padding=(0, 2, 14, 10))
        right.pack(side="right", fill="both", expand=True)

        self._build_path_box(left)
        self._build_window_box(left)
        self._build_bind_box(left)
        self._build_action_box(left)
        self._build_batch_box(left)
        self._build_right(right)
        self._build_statusbar()
        self._bind_wheel_recursive(left)

    def _on_left_configure(self, _event=None) -> None:
        self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all"))
        # 画布宽度跟着内容实际需要的宽度走：内容再宽也不会被裁掉
        need = self.left_inner.winfo_reqwidth()
        if need > 0 and abs(need - self.left_canvas.winfo_width()) > 2:
            self.left_canvas.configure(width=need)

    def _on_left_canvas_configure(self, event) -> None:
        self.left_canvas.itemconfigure(self._left_window, width=event.width)

    def _bind_wheel_recursive(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child)

    def _on_wheel(self, event) -> None:
        first, last = self.left_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return  # 不需要滚动就别抢滚轮
        self.left_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _build_path_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="1. 测试路径", padding=(10, 6))
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.target_var = tk.StringVar(value=next(iter(TARGETS)))
        for name in TARGETS:
            ttk.Radiobutton(row, text=name, value=name, variable=self.target_var,
                            command=self._on_target_change).pack(side="left", padx=(0, 14))
        self.lbl_target_desc = ttk.Label(box, text="", style="Hint.TLabel",
                                        wraplength=int(330 * self.scale))
        self.lbl_target_desc.pack(fill="x", pady=(6, 0))

    def _build_window_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="2. 目标窗口", padding=(10, 6))
        box.pack(fill="x", pady=(6, 0))

        top = ttk.Frame(box)
        top.pack(fill="x")
        size = int(64 * self.scale)
        self._cross_size = size
        self.target_canvas = tk.Canvas(top, width=size, height=size, bg="#f4f4f4",
                                       highlightthickness=1, highlightbackground="#9a9a9a")
        self.target_canvas.pack(side="left")
        self._draw_crosshair()
        self.target_canvas.configure(cursor="plus")
        self.target_canvas.bind("<ButtonPress-1>", self._on_pick_press)
        self.target_canvas.bind("<B1-Motion>", self._on_pick_motion)
        self.target_canvas.bind("<ButtonRelease-1>", self._on_pick_release)

        info = ttk.Frame(top)
        info.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_hwnd = self._info_row(info, "HWND")
        self.lbl_class = self._info_row(info, "Class")
        self.lbl_size = self._info_row(info, "Client")
        self.lbl_state = self._info_row(info, "状态")

        btn = ttk.Frame(box)
        btn.pack(fill="x", pady=(6, 0))
        ttk.Button(btn, text="自动查找当前路径的窗口",
                   command=self._auto_find).pack(side="left")
        ttk.Label(box, text="或把左上角准星拖到任意窗口上",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_bind_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="3. 绑定", padding=(10, 6))
        box.pack(fill="x", pady=(6, 0))
        self._bind_box = box

        self.mode_var = tk.StringVar(value=next(iter(LOCAL_MODES)))
        self.mode_combo = ttk.Combobox(box, textvariable=self.mode_var,
                                       values=list(LOCAL_MODES.keys()),
                                       state="readonly", width=46)
        self.mode_combo.pack(fill="x")
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _: self._refresh_mode_desc())
        self.lbl_mode_desc = ttk.Label(box, text="", style="Hint.TLabel",
                                      wraplength=int(330 * self.scale))
        self.lbl_mode_desc.pack(fill="x", pady=(4, 6))

        # 自定义参数：任何组合都能试，不用改代码
        self.custom_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="使用自定义参数（忽略上面的档位）",
                        variable=self.custom_var,
                        command=self._toggle_custom).pack(anchor="w")
        self.custom_frame = ttk.Frame(box)
        self._custom_entries: dict[str, ttk.Entry] = {}
        for i, key in enumerate(["display", "mouse", "keypad", "public", "mode"]):
            r, c = divmod(i, 2)
            ttk.Label(self.custom_frame, text=key, width=7).grid(
                row=r, column=c * 2, sticky="w", pady=2)
            ent = ttk.Entry(self.custom_frame, width=24)
            ent.grid(row=r, column=c * 2 + 1, sticky="we", padx=(0, 8), pady=2)
            self._custom_entries[key] = ent
        self.custom_frame.columnconfigure(1, weight=1)
        self.custom_frame.columnconfigure(3, weight=1)

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self._bind_row = row
        self.btn_bind = ttk.Button(row, text="Bind", command=self._do_bind, state="disabled")
        self.btn_bind.pack(side="left")
        self.btn_unbind = ttk.Button(row, text="Unbind", command=self._do_unbind,
                                     state="disabled")
        self.btn_unbind.pack(side="left", padx=(6, 0))
        self.lbl_bind_status = ttk.Label(row, text="未绑定", foreground="#b00")
        self.lbl_bind_status.pack(side="left", padx=10)

    def _build_action_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="4. 发事件", padding=(10, 6))
        box.pack(fill="x", pady=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="X").pack(side="left")
        self.ent_x = ttk.Entry(row, width=6)
        self.ent_x.insert(0, "400")
        self.ent_x.pack(side="left", padx=(2, 8))
        ttk.Label(row, text="Y").pack(side="left")
        self.ent_y = ttk.Entry(row, width=6)
        self.ent_y.insert(0, "300")
        self.ent_y.pack(side="left", padx=(2, 8))
        ttk.Label(row, text="Key").pack(side="left")
        self.ent_key = ttk.Entry(row, width=8)
        self.ent_key.insert(0, "enter")
        self.ent_key.pack(side="left", padx=(2, 8))
        ttk.Label(row, text="等待").pack(side="left")
        self.ent_wait = ttk.Entry(row, width=5)
        self.ent_wait.insert(0, "500")
        self.ent_wait.pack(side="left", padx=(2, 0))
        ttk.Label(row, text="ms").pack(side="left")

        pick = ttk.Frame(box)
        pick.pack(fill="x", pady=(6, 0))
        ttk.Button(pick, text="取坐标(3秒)", command=self._act_pick_coord).pack(side="left")
        ttk.Label(pick, text="或鼠标停住按 F8", foreground="#777").pack(side="left", padx=8)
        self.lbl_coord = ttk.Label(box, text="未取坐标", foreground="#555")
        self.lbl_coord.pack(fill="x", pady=(4, 0))

        self.action_buttons: list[ttk.Button] = []
        grid = ttk.Frame(box)
        grid.pack(fill="x", pady=(8, 0))
        items = [
            ("Move", self._act_move),
            ("左键", lambda: self._act_click("left")),
            ("右键", lambda: self._act_click("right")),
            ("双击", self._act_double),
            ("滚轮上", lambda: self._act_wheel("up")),
            ("滚轮下", lambda: self._act_wheel("down")),
            ("按键", self._act_key),
            ("输入文字", self._act_text),
            ("截图", self._act_snap),
            ("抓图自检", self._act_capture_check),
            ("拖动", self._act_drag),
        ]
        for i, (text, cmd) in enumerate(items):
            r, c = divmod(i, 3)
            b = ttk.Button(grid, text=text, command=cmd, state="disabled")
            b.grid(row=r, column=c, sticky="we", padx=(0, 4), pady=2)
            self.action_buttons.append(b)
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        r3 = ttk.Frame(box)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="拖动到 X2", style="Hint.TLabel").pack(side="left")
        self.ent_x2 = ttk.Entry(r3, width=6)
        self.ent_x2.insert(0, "500")
        self.ent_x2.pack(side="left", padx=(4, 6))
        ttk.Label(r3, text="Y2", style="Hint.TLabel").pack(side="left")
        self.ent_y2 = ttk.Entry(r3, width=6)
        self.ent_y2.insert(0, "400")
        self.ent_y2.pack(side="left", padx=(4, 0))

    def _build_batch_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="5. 批量与记录", padding=(10, 6))
        box.pack(fill="x", pady=(6, 0))
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        self.btn_sweep = ttk.Button(grid, text="全档扫描", command=self._act_sweep,
                                    state="disabled")
        self.btn_sweep.grid(row=0, column=0, sticky="we", padx=(0, 4), pady=2)
        for i, (text, cmd) in enumerate([
            ("导出结果", self._export_results),
            ("清空结果", self._clear_results),
            ("保存日志", self._save_log),
            ("使用说明", self._show_help),
        ], start=1):
            r, c = divmod(i, 3)
            ttk.Button(grid, text=text, command=cmd).grid(
                row=r, column=c, sticky="we", padx=(0, 4), pady=2)
        for c in range(3):
            grid.columnconfigure(c, weight=1)

    def _build_right(self, parent: ttk.Frame) -> None:
        pane = ttk.PanedWindow(parent, orient="vertical")
        pane.pack(fill="both", expand=True)

        prev = ttk.LabelFrame(pane, text="预览（点图打开截图目录）", padding=6)
        for side, padx in (("before", (0, 4)), ("after", (4, 0))):
            col = ttk.Frame(prev)
            col.pack(side="left", fill="both", expand=True, padx=padx)
            ttk.Label(col, text=side, style="Hint.TLabel").pack(anchor="w")
            cv = tk.Canvas(col, bg="#1b1b1f", highlightthickness=0,
                           height=int(190 * self.scale))
            cv.pack(fill="both", expand=True)
            cv.bind("<Button-1>", lambda _e: os.startfile(OUT_DIR))
            cv.bind("<Configure>", lambda _e: self._render_previews())
            setattr(self, f"cv_{side}", cv)
        pane.add(prev, weight=2)

        nb = ttk.Notebook(pane)
        tab_res = ttk.Frame(nb, padding=4)
        self.tree = ttk.Treeview(tab_res, columns=[c[0] for c in RESULT_COLS],
                                 show="headings", height=8)
        for key, title, width, anchor in RESULT_COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=int(width * self.scale), anchor=anchor,
                             minwidth=int(width * self.scale * 0.8),
                             stretch=(key == "detail"))
        sb = ttk.Scrollbar(tab_res, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground="#0a6b3d")
        self.tree.tag_configure("frozen", foreground="#8a5a00")
        self.tree.tag_configure("none", foreground="#a33")
        self.tree.tag_configure("fail", foreground="#888")
        nb.add(tab_res, text="结果")

        tab_log = ttk.Frame(nb, padding=4)
        self.txt = tk.Text(tab_log, height=8, wrap="none", font=("Consolas", 10))
        lsb = ttk.Scrollbar(tab_log, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=lsb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        nb.add(tab_log, text="日志")
        pane.add(nb, weight=3)

    def _build_statusbar(self) -> None:
        self.status = ttk.Label(self.root, text="就绪", anchor="w", padding=(10, 3),
                                relief="sunken")
        self.status.pack(side="bottom", fill="x")

    def _info_row(self, parent: ttk.Frame, label: str) -> ttk.Label:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text=f"{label}:", width=7, foreground="#555").pack(side="left")
        v = ttk.Label(row, text="-", font=("Consolas", 9))
        v.pack(side="left", fill="x", expand=True)
        return v

    def _draw_crosshair(self) -> None:
        c = self.target_canvas
        s = self._cross_size / 64.0
        c.create_oval(10 * s, 10 * s, 54 * s, 54 * s, outline="#b3261e", width=2)
        c.create_oval(26 * s, 26 * s, 38 * s, 38 * s, outline="#b3261e", width=2)
        c.create_line(32 * s, 4 * s, 32 * s, 60 * s, fill="#b3261e", width=2)
        c.create_line(4 * s, 32 * s, 60 * s, 32 * s, fill="#b3261e", width=2)

    # ---------------------------------------------------------------- 路径与窗口
    def _current_modes(self) -> dict:
        return TARGETS[self.target_var.get()]["modes"]

    def _refresh_mode_desc(self) -> None:
        modes = self._current_modes()
        k = self.mode_var.get()
        if k in modes:
            self.lbl_mode_desc.config(text=modes[k][5])

    def _on_target_change(self) -> None:
        modes = self._current_modes()
        self.mode_combo.config(values=list(modes.keys()))
        self.mode_var.set(next(iter(modes)))
        self.lbl_target_desc.config(text=TARGETS[self.target_var.get()]["desc"])
        self._refresh_mode_desc()
        self._log(f"测试路径切换为「{self.target_var.get()}」，参数阶梯共 {len(modes)} 档")

    def _toggle_custom(self) -> None:
        if self.custom_var.get():
            # 插在 Bind 按钮行上面，别排到最后去
            self.custom_frame.pack(fill="x", pady=(6, 0), before=self._bind_row)
            if not any(e.get() for e in self._custom_entries.values()):
                self._fill_custom_from_mode()
        else:
            self.custom_frame.pack_forget()

    def _fill_custom_from_mode(self) -> None:
        k = self.mode_var.get()
        modes = self._current_modes()
        if k not in modes:
            return
        display, mouse, keypad, public, mode, _ = modes[k]
        for key, val in (("display", display), ("mouse", mouse), ("keypad", keypad),
                         ("public", public), ("mode", str(mode))):
            ent = self._custom_entries[key]
            ent.delete(0, "end")
            ent.insert(0, val)

    def _bind_params(self) -> tuple[str, str, str, str, int, str]:
        """当前要用的绑定参数：自定义优先，否则取档位。"""
        if self.custom_var.get():
            e = self._custom_entries
            try:
                mode = int(e["mode"].get().strip() or "0")
            except ValueError:
                mode = 0
            return (e["display"].get().strip(), e["mouse"].get().strip(),
                    e["keypad"].get().strip(), e["public"].get().strip(),
                    mode, "自定义")
        k = self.mode_var.get()
        modes = self._current_modes()
        if k not in modes:
            k = next(iter(modes))
        return modes[k]

    def _auto_find(self) -> None:
        name = self.target_var.get()
        cfg = TARGETS[name]
        hits = match_windows(cfg["title_keys"], cfg["class_keys"])
        if not hits:
            self._log(f"没找到「{name}」对应的窗口。标题关键字={cfg['title_keys']} "
                      f"类名关键字={cfg['class_keys']}；也可以直接拖准星选。")
            return
        self._log(f"「{name}」匹配到 {len(hits)} 个窗口：")
        for h in hits:
            info = win_info(h)
            self._log(f"    hwnd={info['hwnd']} class={info['class']} "
                      f"client={info['client_size'][0]}x{info['client_size'][1]} "
                      f"title={info['title']!r}")
        self._set_target(win_info(hits[0]))

    def _set_target(self, info: dict) -> None:
        self.target_info = info
        self.lbl_hwnd.config(text=str(info["hwnd"]))
        self.lbl_class.config(text=info["class"] or "-")
        cw, ch = info["client_size"]
        self.lbl_size.config(text=f"{cw}x{ch}")
        if info.get("iconic"):
            state = "最小化（后台可能不刷新）"
        elif not info.get("visible"):
            state = "不可见"
        else:
            state = "正常"
        self.lbl_state.config(text=state)
        self._log(f"选中窗口 hwnd={info['hwnd']} class={info['class']} "
                  f"client={cw}x{ch} title={info['title']!r}")
        if self.dm:
            self.btn_bind.config(state="normal")

    # ---------------------------------------------------------------- 准星
    def _on_pick_press(self, _event) -> None:
        self.target_canvas.configure(cursor="target")
        self.target_canvas.grab_set_global()
        self._drag_prev_rect = None
        self._log("开始拖拽，松开鼠标时的位置将被捕获")

    def _on_pick_motion(self, _event) -> None:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd = win_from_point(pt.x, pt.y)
        if hwnd == self.target_canvas.winfo_toplevel().winfo_id():
            return
        self._drag_prev_rect = draw_focus_rect(hwnd, self._drag_prev_rect)

    def _on_pick_release(self, _event) -> None:
        self.target_canvas.grab_release()
        self.target_canvas.configure(cursor="plus")
        if self._drag_prev_rect:
            draw_focus_rect(0, self._drag_prev_rect)
            self._drag_prev_rect = None
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd = win_from_point(pt.x, pt.y)
        if not hwnd:
            self._log("没捕获到窗口")
            return
        self._set_target(win_info(hwnd))

    # ---------------------------------------------------------------- 大漠
    def _init_dm(self) -> None:
        """在主线程里创建大漠对象。

        必须和后续所有调用在同一个线程（Tk 主线程），否则是跨套间使用 IDispatch：
        轻则报 CO_E_NOTINITIALIZED(-2147221008 '尚未调用 CoInitialize')，重则崩溃。
        """
        self._log("加载大漠（32位 Python + 管理员才能通过 Reg）...")
        self.lbl_bind_status.config(text="大漠加载中…", foreground="#555")
        self.root.update_idletasks()
        try:
            self.dm = load_dm()
            ver = self.dm.Ver()
            self._log(f"大漠就绪 ver={ver}")
            self.lbl_bind_status.config(text="未绑定", foreground="#b00")
            if self.target_info:
                self.btn_bind.config(state="normal")
            self._dm_ver = ver
            self.status.config(text=f"大漠 {ver} 就绪 · {self._screen_info()}")
            self.lbl_header.config(text=f"大漠 {ver} · 未绑定", style="Hint.TLabel")
        except Exception as e:  # noqa: BLE001
            self._log(f"!! {type(e).__name__}: {e}")
            self.status.config(text="大漠加载失败")
            messagebox.showerror("大漠加载失败", str(e))

    def _do_bind(self) -> None:
        if not self.target_info or not self.dm:
            return
        self._bind(self.target_info["hwnd"])

    def _bind(self, hwnd: int, params: Optional[tuple] = None) -> tuple[bool, str]:
        display, mouse, keypad, public, mode, label = params or self._bind_params()
        if self.bound_hwnd:
            try:
                self.dm.UnBindWindow()
            except Exception:  # noqa: BLE001
                pass
            self.bound_hwnd = 0
        try:
            ret = self.dm.BindWindowEx(hwnd, display, mouse, keypad, public, mode)
            err = 0 if ret == 1 else self.dm.GetLastError()
        except Exception as e:  # noqa: BLE001
            self._log(f"BindWindowEx 异常: {type(e).__name__}: {e}")
            return False, str(e)

        self._log(f"【{self.target_var.get()}】BindWindowEx ret={ret} err={err} 档位={label}")
        self._log(f"  display={display!r} mouse={mouse!r} keypad={keypad!r} "
                  f"public={public!r} mode={mode}")
        if ret != 1:
            self.lbl_bind_status.config(text=f"绑定失败 err={err}", foreground="#b00")
            return False, f"ret={ret} err={err}"

        self.bound_hwnd = hwnd
        for fn, arg in ((self.dm.EnableRealKeypad, 1), (self.dm.SetExactOcr, 1)):
            try:
                fn(arg)
            except Exception:  # noqa: BLE001
                pass
        # 远程链路有延迟，异步发送时"移动后立刻点击"可能被丢（架构文档 10.7）
        for fn in (self.dm.EnableMouseSync, self.dm.EnableKeypadSync):
            try:
                fn(1, 200)
            except Exception:  # noqa: BLE001
                pass
        self._log("  已开启鼠标/键盘同步发送(200ms 超时)")
        time.sleep(1.0)  # 大漠文档要求：部分窗口绑定后要等 1~2 秒再发输入

        cw, ch = client_size(hwnd)
        self._log(f"  客户区 {cw}x{ch}")
        self.lbl_bind_status.config(text=f"已绑定 {label}", foreground="#070")
        self.lbl_header.config(text=f"大漠 {self._dm_ver} · 已绑定 {label}", style="Ok.TLabel")
        self.btn_unbind.config(state="normal")
        self._set_actions_enabled(True)
        # 绑定后立刻自检：这档抓图可不可信，不做这步会得到一堆误导性的 0.000
        self._act_capture_check()
        return True, ""

    def _do_unbind(self) -> None:
        if self.dm and self.bound_hwnd:
            try:
                self.dm.UnBindWindow()
            except Exception as e:  # noqa: BLE001
                self._log(f"UnBindWindow 异常: {e}")
        self.bound_hwnd = 0
        self.lbl_bind_status.config(text="未绑定", foreground="#b00")
        self.lbl_header.config(text=f"大漠 {self._dm_ver} · 未绑定", style="Hint.TLabel")
        self.btn_unbind.config(state="disabled")
        self._set_actions_enabled(False)
        self._log("已解绑")

    def _set_actions_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for b in self.action_buttons:
            b.config(state=state)
        self.btn_sweep.config(state=state)

    # ---------------------------------------------------------------- 取点
    def _coord_hwnd(self) -> int:
        if self.bound_hwnd:
            return self.bound_hwnd
        return int(self.target_info.get("hwnd") or 0)

    def _act_pick_coord(self) -> None:
        hwnd = self._coord_hwnd()
        if not hwnd:
            self._log("取坐标失败：先拖准星选一个目标窗口，或者先 Bind")
            return
        self._log("3 秒后读取鼠标位置，现在把鼠标移到目标位置并停住…")
        self._countdown(3, hwnd)

    def _countdown(self, n: int, hwnd: int) -> None:
        if n > 0:
            self.lbl_coord.config(text=f"取坐标倒计时 {n}…", foreground="#555")
            self.root.after(1000, lambda: self._countdown(n - 1, hwnd))
        else:
            self._grab_cursor(hwnd)

    def _grab_cursor(self, hwnd: int = 0) -> None:
        hwnd = hwnd or self._coord_hwnd()
        if not hwnd:
            self._log("取坐标失败：没有目标窗口")
            return
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cx, cy = client_coords(hwnd, pt.x, pt.y)
        cw, ch = client_size(hwnd)
        inside = 0 <= cx < cw and 0 <= cy < ch

        self.ent_x.delete(0, "end")
        self.ent_x.insert(0, str(cx))
        self.ent_y.delete(0, "end")
        self.ent_y.insert(0, str(cy))
        self.lbl_coord.config(
            text=f"已取 ({cx}, {cy})" + ("" if inside else f"  ← 超出客户区 {cw}x{ch}"),
            foreground="#070" if inside else "#b00")
        self._log(f"取坐标：屏幕({pt.x},{pt.y}) → 客户区({cx},{cy})"
                  + ("" if inside else f"  ⚠ 不在客户区 {cw}x{ch} 内，大漠会点到窗口外"))

    # ---------------------------------------------------------------- 热键
    def _poll_hotkey(self) -> None:
        """F8 取坐标。用 GetAsyncKeyState 轮询而不是 RegisterHotKey：
        WM_HOTKEY 是线程消息，Tk 的消息泵会先取走丢弃，after() 里轮不到。
        """
        if self._closing:
            return
        down = bool(user32.GetAsyncKeyState(0x77) & 0x8000)  # VK_F8
        if down and not self._f8_down:
            self._f8_down = True
            self._grab_cursor()
        elif not down:
            self._f8_down = False
        self._hotkey_after = self.root.after(50, self._poll_hotkey)

    # ---------------------------------------------------------------- 动作
    def _wait_ms(self) -> int:
        try:
            return max(50, int(self.ent_wait.get().strip()))
        except ValueError:
            return 500

    def _xy(self) -> tuple[int, int]:
        return int(self.ent_x.get()), int(self.ent_y.get())

    def _snap_png(self, name: str) -> Optional[str]:
        if not self.bound_hwnd:
            return None
        cw, ch = client_size(self.bound_hwnd)
        if cw <= 0 or ch <= 0:
            return None
        path = os.path.join(OUT_DIR, name)
        try:
            if self.dm.CapturePng(0, 0, cw, ch, path) != 1:
                return None
        except Exception:  # noqa: BLE001
            return None
        return path

    def _snap_bmp(self, name: str) -> Optional[str]:
        """BMP 无压缩，字节差就是真实像素差；PNG 压缩后字节差没有意义。"""
        if not self.bound_hwnd:
            return None
        cw, ch = client_size(self.bound_hwnd)
        if cw <= 0 or ch <= 0:
            return None
        path = os.path.join(OUT_DIR, name)
        try:
            if self.dm.Capture(0, 0, cw, ch, path) != 1:
                return None
        except Exception:  # noqa: BLE001
            return None
        return path

    def _act_move(self) -> None:
        x, y = self._xy()
        self._probe("Move", lambda: self.dm.MoveTo(x, y), f"({x},{y})", (x, y))

    def _act_click(self, button: str) -> None:
        x, y = self._xy()
        name = "左键" if button == "left" else "右键"

        def act():
            self.dm.MoveTo(x, y)
            time.sleep(0.05)
            if button == "left":
                self.dm.LeftClick()
            else:
                self.dm.RightClick()

        self._probe(name, act, f"({x},{y})", (x, y))

    def _act_double(self) -> None:
        x, y = self._xy()

        def act():
            self.dm.MoveTo(x, y)
            time.sleep(0.05)
            self.dm.LeftDoubleClick()

        self._probe("双击", act, f"({x},{y})", (x, y))

    def _act_wheel(self, direction: str) -> None:
        x, y = self._xy()

        def act():
            self.dm.MoveTo(x, y)
            time.sleep(0.05)
            if direction == "up":
                self.dm.WheelUp()
            else:
                self.dm.WheelDown()

        self._probe("滚轮上" if direction == "up" else "滚轮下", act, f"({x},{y})", (x, y))

    def _act_drag(self) -> None:
        x, y = self._xy()
        x2, y2 = int(self.ent_x2.get()), int(self.ent_y2.get())

        def act():
            self.dm.MoveTo(x, y)
            time.sleep(0.05)
            self.dm.LeftDown()
            time.sleep(0.08)
            self.dm.MoveTo(x2, y2)
            time.sleep(0.08)
            self.dm.LeftUp()

        self._probe("拖动", act, f"({x},{y})->({x2},{y2})", (x, y))

    def _act_key(self) -> None:
        key = self.ent_key.get().strip()
        if not key:
            return

        def act():
            if len(key) == 1:
                self.dm.KeyPressChar(key)
            else:
                self.dm.KeyPressStr(key, 50)

        self._probe(f"按键[{key}]", act)

    def _act_text(self) -> None:
        text = self.ent_key.get().strip()
        if not text:
            return

        def act():
            try:
                self.dm.SendString(text)
            except Exception:  # noqa: BLE001
                self.dm.KeyPressStr(text, 50)

        self._probe(f"文字[{text}]", act)

    def _act_snap(self) -> None:
        path = self._snap_png(f"snap_{int(time.time())}.png")
        if path:
            self._log(f"截图 -> {path}")
            self._show_preview(path, path)
        else:
            self._log("截图失败")

    def _act_capture_check(self) -> dict:
        """抓图自检：大漠这一档抓到的画面，和真实窗口画面是不是一回事。"""
        if not self.bound_hwnd:
            return {}
        bmp = self._snap_bmp(f"chk_{int(time.time())}.bmp")
        ref = gdi_grab(self.bound_hwnd)
        st = img_stats(ref)
        self._log(f"抓图自检 [{self._ctx()}]")
        if not bmp or not ref or not st:
            self._log("  自检失败：大漠抓图或参照抓图返回空（窗口可能已失效）")
            return {"rate": -1.0, "ok": False}
        self._log(f"  参照图({st['method']}) {ref['w']}x{ref['h']} "
                  f"亮度={st['mean']:.0f} 色数={st['uniq']}")
        if st["uniq"] <= 2:
            self._log("  参照图几乎是纯色：窗口可能被遮挡/最小化，或被锁屏盖住")
        rate = diff_gdi_vs_bmp(ref, bmp)
        if rate < 0:
            self._log("  大漠图与参照图尺寸/格式对不上，无法比较（可能抓的不是客户区）")
            return {"rate": rate, "ok": False}
        ok = rate <= CAPTURE_MISMATCH
        if ok:
            self._log(f"  大漠图 vs 真实画面 差异={rate:.1%} → 一致，这一档抓图可用")
        else:
            self._log(f"  大漠图 vs 真实画面 差异={rate:.1%} → 不一致，"
                      f"这一档抓图不可信（多半是冻结的静态位图），换抓图档")
        return {"rate": rate, "ok": ok}

    def _probe(self, action: str, fn: Callable[[], None], xy: str = "-",
               mark: Optional[tuple[int, int]] = None) -> dict:
        """发一次动作，双通道抓图前后对比，给出判读并记进结果表。"""
        if not self.bound_hwnd:
            return {}
        ts = int(time.time())
        png_b = self._snap_png(f"{action}_{ts}_before.png")
        bmp_b = self._snap_bmp(f"{action}_{ts}_before.bmp")
        ref_b = gdi_grab(self.bound_hwnd)

        err = ""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        time.sleep(self._wait_ms() / 1000.0)

        png_a = self._snap_png(f"{action}_{ts}_after.png")
        bmp_a = self._snap_bmp(f"{action}_{ts}_after.bmp")
        ref_a = gdi_grab(self.bound_hwnd)

        dm_rate = diff_bytes(bmp_b, bmp_a) if bmp_b and bmp_a else -1.0
        ref_rate = diff_gdi(ref_b, ref_a)
        st = img_stats(ref_b)
        verdict, detail = self._judge(dm_rate, ref_rate, err)

        self._log(f"  [{self._ctx()}] {action} {xy}  大漠={dm_rate:.3f} "
                  f"参照={ref_rate:.3f}"
                  + (f"  亮度={st['mean']:.0f}/色数={st['uniq']}" if st else ""))
        if err:
            self._log(f"    动作异常: {err}")
        self._log(f"    判读：{detail}")

        res = {"time": time.strftime("%H:%M:%S"), "target": self.target_var.get()[:4],
               "mode": self._mode_label(), "action": action, "xy": xy,
               "dm": f"{dm_rate:.3f}" if dm_rate >= 0 else "—",
               "ref": f"{ref_rate:.3f}" if ref_rate >= 0 else "—",
               "verdict": verdict, "detail": detail}
        self._add_result(res)
        if png_b and png_a:
            self._show_preview(png_b, png_a, mark)
        return res

    def _judge(self, dm_rate: float, ref_rate: float, err: str) -> tuple[str, str]:
        if err:
            return VERDICT_FAIL, f"动作抛异常（{err}），先看是不是参数/句柄问题"
        if dm_rate < 0 or ref_rate < 0:
            return VERDICT_FAIL, "抓图失败，数据不全，无法判断"
        if dm_rate > CHANGE_EPS and ref_rate > CHANGE_EPS:
            return VERDICT_OK, "输入生效，画面确实变了"
        if dm_rate <= CHANGE_EPS and ref_rate > CHANGE_EPS:
            return VERDICT_FROZEN, ("输入其实生效了，是大漠这一档抓图冻结"
                                    "（真实画面变了但大漠图没变）→ 换抓图档 gdi/gdi2/normal")
        return VERDICT_NONE, ("画面完全没变 → 输入没生效，或这个动作本身不产生视觉反馈"
                              "（Move 常常没有可见变化，换成能看见效果的动作再测）")

    def _mode_label(self) -> str:
        if self.custom_var.get():
            return "自定义"
        return self.mode_var.get().split("·")[0].strip()

    def _ctx(self) -> str:
        return f"{self.target_var.get()}|{self._mode_label()}"

    def _show_preview(self, before: Optional[str], after: Optional[str],
                      mark: Optional[tuple[int, int]] = None) -> None:
        try:
            self._img_before = tk.PhotoImage(file=before) if before else None
            self._img_after = tk.PhotoImage(file=after) if after else None
        except Exception as e:  # noqa: BLE001
            self._log(f"  预览失败: {e}")
            return
        self._mark = mark
        self._render_previews()

    def _render_previews(self) -> None:
        self._draw_canvas(self.cv_before, self._img_before)
        self._draw_canvas(self.cv_after, self._img_after)

    def _draw_canvas(self, canvas: tk.Canvas, img: Optional[tk.PhotoImage]) -> None:
        """把图按可用空间等比缩放到画布中央，并在取点位置画个十字。"""
        canvas.delete("all")
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        if img is None:
            canvas.create_text(cw // 2, ch // 2, text="还没有截图", fill="#777777")
            return
        f = max(1, -(-img.width() // cw), -(-img.height() // ch))  # 向上取整
        scaled = img.subsample(f, f) if f > 1 else img
        canvas._img = scaled  # 保住引用，否则会被 GC 掉变成空白
        x0 = (cw - scaled.width()) // 2
        y0 = (ch - scaled.height()) // 2
        canvas.create_image(x0, y0, anchor="nw", image=scaled)

        if self._mark:
            mx = x0 + self._mark[0] / f
            my = y0 + self._mark[1] / f
            if x0 <= mx <= x0 + scaled.width() and y0 <= my <= y0 + scaled.height():
                r = int(12 * self.scale)
                canvas.create_line(mx - r, my, mx + r, my, fill="#ff3b30", width=2)
                canvas.create_line(mx, my - r, mx, my + r, fill="#ff3b30", width=2)
                canvas.create_oval(mx - 4, my - 4, mx + 4, my + 4, outline="#ff3b30", width=2)

    # ---------------------------------------------------------------- 全档扫描
    def _act_sweep(self) -> None:
        hwnd = self._coord_hwnd()
        if not hwnd or not self.dm:
            self._log("全档扫描：先选目标窗口并确保大漠已加载")
            return
        if self.custom_var.get():
            self._log("全档扫描：请先取消「使用自定义参数」，扫描是按档位逐个跑的")
            return
        modes = self._current_modes()
        try:
            x, y = self._xy()
        except ValueError:
            self._log("全档扫描：X/Y 不是数字")
            return
        if not messagebox.askyesno(
                "全档扫描",
                f"将依次用 {len(modes)} 个档位绑定当前窗口，每档发一次左键点击 ({x},{y})，"
                f"最后解绑。\n\n注入类档位（dx2 / 模式101）可能让目标程序不稳定，"
                f"查看器甚至可能崩。\n\n确认继续？"):
            return

        self._busy = True
        self._set_actions_enabled(False)
        self.btn_bind.config(state="disabled")
        summary = []
        try:
            for name, params in modes.items():
                if self._closing:
                    break
                self.mode_var.set(name)
                self._refresh_mode_desc()
                self.root.update()
                ok, err = self._bind(hwnd, params)
                if not ok:
                    summary.append((name, "绑定失败", err))
                    self._add_result({"time": time.strftime("%H:%M:%S"),
                                      "target": self.target_var.get()[:4],
                                      "mode": name.split("·")[0].strip(), "action": "Bind",
                                      "xy": "-", "dm": "—", "ref": "—",
                                      "verdict": VERDICT_FAIL, "detail": err})
                    continue
                chk = self._act_capture_check()
                res = self._probe("左键", self._make_click(x, y), f"({x},{y})", (x, y))
                cap = "一致" if chk.get("ok") else "不一致"
                summary.append((name, cap, res.get("verdict", "?")))
                self.root.update()
        finally:
            self._do_unbind()
            self._busy = False
            self.btn_bind.config(state="normal" if self.target_info else "disabled")

        self._log("")
        self._log("===== 全档扫描结果 =====")
        for name, cap, verdict in summary:
            mark = "✓" if (cap == "一致" and verdict == VERDICT_OK) else " "
            self._log(f" {mark} {name} → 抓图:{cap} 输入:{verdict}")
        self._log("  结论：抓图「一致」且输入「生效」的档位才可用；"
                  "抓图「不一致」说明这档抓图冻结，它的判读不可信")
        self._log("")

    def _make_click(self, x: int, y: int) -> Callable[[], None]:
        def act():
            self.dm.MoveTo(x, y)
            time.sleep(0.05)
            self.dm.LeftClick()

        return act

    # ---------------------------------------------------------------- 结果表
    def _add_result(self, res: dict) -> None:
        self.results.append(res)
        tag = {VERDICT_OK: "ok", VERDICT_FROZEN: "frozen",
               VERDICT_NONE: "none", VERDICT_FAIL: "fail"}.get(res.get("verdict"), "")
        self.tree.insert("", 0, values=[res.get(c[0], "") for c in RESULT_COLS],
                         tags=(tag,))

    def _clear_results(self) -> None:
        self.results.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._log("结果已清空")

    def _export_results(self) -> None:
        if not self.results:
            self._log("没有结果可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出测试结果",
            defaultextension=".csv",
            initialfile=f"dm_bind_test_{time.strftime('%m%d_%H%M')}.csv",
            filetypes=[("CSV", "*.csv"), ("文本", "*.txt")])
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8") as f:
                    for r in self.results:
                        f.write(f"{r['time']}\t{r['target']}\t{r['mode']}\t{r['action']}\t"
                                f"{r['xy']}\t大漠={r['dm']}\t参照={r['ref']}\t"
                                f"{r['verdict']}\t{r.get('detail', '')}\n")
            else:
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["时间", "目标", "档位", "动作", "坐标", "大漠变化率",
                                "参照变化率", "判读", "说明"])
                    for r in self.results:
                        w.writerow([r["time"], r["target"], r["mode"], r["action"],
                                    r["xy"], r["dm"], r["ref"], r["verdict"],
                                    r.get("detail", "")])
            self._log(f"已导出 {len(self.results)} 条结果 -> {path}")
        except Exception as e:  # noqa: BLE001
            self._log(f"导出失败: {e}")

    # ---------------------------------------------------------------- 状态
    def _save_log(self) -> None:
        text = self.txt.get("1.0", "end")
        if not text.strip():
            self._log("日志是空的")
            return
        path = filedialog.asksaveasfilename(
            title="保存日志", defaultextension=".txt",
            initialfile=f"dm_bind_log_{time.strftime('%m%d_%H%M')}.txt",
            filetypes=[("文本", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._log(f"日志已保存 -> {path}")
        except Exception as e:  # noqa: BLE001
            self._log(f"保存日志失败: {e}")

    def _show_help(self) -> None:
        messagebox.showinfo("使用说明", HELP_TEXT)

    def _load_state(self) -> None:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            st = {}
        if st.get("target") in TARGETS:
            self.target_var.set(st["target"])
            self._on_target_change()
        if st.get("mode") in self._current_modes():
            self.mode_var.set(st["mode"])
        for key, ent in (("x", self.ent_x), ("y", self.ent_y), ("key", self.ent_key),
                         ("wait", self.ent_wait), ("x2", self.ent_x2), ("y2", self.ent_y2)):
            if st.get(key):
                ent.delete(0, "end")
                ent.insert(0, str(st[key]))
        self._refresh_mode_desc()

    def _save_state(self) -> None:
        st = {"target": self.target_var.get(), "mode": self.mode_var.get(),
              "x": self.ent_x.get(), "y": self.ent_y.get(), "key": self.ent_key.get(),
              "wait": self.ent_wait.get(), "x2": self.ent_x2.get(), "y2": self.ent_y2.get()}
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        self._closing = True
        if self._hotkey_after:
            try:
                self.root.after_cancel(self._hotkey_after)
            except Exception:  # noqa: BLE001
                pass
            self._hotkey_after = None
        self._save_state()
        try:
            if self.dm and self.bound_hwnd:
                self.dm.UnBindWindow()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    def _on_tk_error(self, exc, val, tb) -> None:
        """GUI 回调里的未捕获异常：窗口版 exe 没有控制台，必须显示出来。"""
        text = "".join(traceback.format_exception(exc, val, tb))
        self._log("!! 未捕获异常：\n" + text)
        self.status.config(text="发生异常，详见日志")
        messagebox.showerror("程序异常", f"{val}\n\n详情见日志标签页")

    # ---------------------------------------------------------------- 日志
    def _log(self, s: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.txt.insert("end", f"[{ts}] {s}\n")
        self.txt.see("end")


def main() -> None:
    # 主线程必须先初始化 COM（单线程套间）。大漠是 COM 组件，不初始化就会报
    # CO_E_NOTINITIALIZED(-2147221008 '尚未调用 CoInitialize')。
    pythoncom.CoInitialize()
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
