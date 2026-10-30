"""大漠后台绑定探针：交互式工具，验证"主控机绑定远程查看器窗口，能否后台发出鼠标/键盘事件"。

必须：32位 Python 运行、管理员终端启动（大漠 Reg 需要）。

用法：
    python dm_bg_test.py
    > list vnc              # 列出标题/类名含 vnc 的所有窗口
    > bind 1 A              # 绑定序号 1 的窗口，用模式 A（见下）
    > info                  # 打印当前窗口客户区大小、鼠标位置
    > m 400 300             # 移动到 (400, 300)（游戏坐标，若未设 origin 则等同于查看器客户区坐标）
    > c 400 300             # 左键点击 (400, 300)，前后各截一张图并对比变化率
    > r 400 300             # 右键点击（相对安全，很多界面右键只弹菜单）
    > k enter               # 按一次回车
    > snap                  # 当前抓一张图
    > occlude               # 把一个大窗口盖住查看器，再做一次抓图，验证"被遮挡仍能工作"
    > rebind A              # 换模式重新绑定（UnBind + BindEx）
    > unbind
    > quit

模式预置（display | mouse | keypad | mode）：
    A  normal | windows   | windows   | 0    最温和，纯窗口消息，不注入查看器进程
    B  normal | windows3  | windows   | 0    消息+后台混合
    C  gdi    | windows   | windows   | 0    抓图走 gdi，输入仍走消息
    D  gdi2   | windows2  | windows2  | 0    gdi2 后台抓图+消息输入
    E  dx2    | dx.mouse.state.api|dx.mouse.api|dx.mouse.cursor | dx.keypad.state.api|dx.keypad.api | 101
       大漠传统后台模式，会注入查看器进程，有把查看器搞崩的风险

探针判定方法（避免"看上去动了但其实没过去"的假阳性）：
    每次 c/r/k 之前抓一张 before.png，之后 300ms 再抓一张 after.png。
    对两张图做"字节数差异 + 像素采样差异"，输出变化率。
    - 变化率显著 → 事件可能到了副机（也可能只是查看器自己的 UI 反应，需要配合副机侧目视确认）。
    - 变化率为零 → 事件几乎肯定没过去。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
import time
from ctypes import wintypes
from typing import Optional

import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.config import load_yaml, resources_path  # noqa: E402

OUT_DIR = os.path.join(tempfile.gettempdir(), "mhxy_dm_bg_test")
os.makedirs(OUT_DIR, exist_ok=True)

MODES = {
    "A": ("normal", "windows", "windows", "", 0),
    "B": ("normal", "windows3", "windows", "", 0),
    "C": ("gdi", "windows", "windows", "", 0),
    "D": ("gdi2", "windows2", "windows2", "", 0),
    "E": (
        "dx2",
        "dx.mouse.state.api|dx.mouse.api|dx.mouse.cursor",
        "dx.keypad.state.api|dx.keypad.api",
        "dx.public.active.api|dx.public.active.message|dx.public.hide.dll|"
        "dx.public.graphic.protect|dx.public.anti.api|dx.public.km.protect|dx.public.down.cpu",
        101,
    ),
}


def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except OSError:
        pass


def make_dm():
    raw = load_yaml("config", "driver.yaml")
    dmreg = ctypes.WinDLL(resources_path("dll", raw["dll"]["dmreg_file"]))
    dmreg.SetDllPathW.argtypes = [wintypes.LPCWSTR, wintypes.LONG]
    dmreg.SetDllPathW.restype = wintypes.LONG
    if dmreg.SetDllPathW(resources_path("dll", raw["dll"]["dm_file"]), 0) != 1:
        raise RuntimeError("SetDllPathW 失败")
    dm = win32com.client.Dispatch("dm.dmsoft")
    ret = dm.Reg(raw["auth"]["reg_code"], raw["auth"]["ver_info"])
    if ret != 1:
        raise RuntimeError(
            f"大漠注册失败 ret={ret}，-2 表示要管理员权限，4 表示 reg_code/ver_info 错"
        )
    return dm


def list_windows(keyword: str) -> list[tuple[int, str, str, str]]:
    """枚举所有可见顶层窗口，按关键字过滤。返回 (hwnd, cls, title, process)。"""
    import psutil  # 仅在 list 时用；如果没装就退化成不显示进程名

    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    GetWindowTextW = user32.GetWindowTextW
    GetClassNameW = user32.GetClassNameW
    IsWindowVisible = user32.IsWindowVisible
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId

    results: list[tuple[int, str, str, str]] = []

    def cb(hwnd, _):
        if not IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, buf, 256)
        title = buf.value
        buf2 = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, buf2, 256)
        cls = buf2.value
        if not title and not cls:
            return True
        pid = ctypes.c_ulong()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name()
        except Exception:
            proc = "?"
        hay = (title + " " + cls + " " + proc).lower()
        if keyword.lower() in hay:
            results.append((hwnd, cls, title, proc))
        return True

    EnumWindows(EnumWindowsProc(cb), None)
    return results


def snap(dm, path: str, x1=0, y1=0, x2=0, y2=0) -> bool:
    if x2 == 0 and y2 == 0:
        ret_cs, cw, ch = dm.GetClientSize(dm._bound_hwnd)  # type: ignore[attr-defined]
        if ret_cs != 1:
            return False
        x2, y2 = cw, ch
    return dm.CapturePng(x1, y1, x2, y2, path) == 1


def diff(a: str, b: str) -> tuple[float, int]:
    """简易对比：返回 (0~1 的字节变化率, 文件大小差)。"""
    try:
        with open(a, "rb") as f1, open(b, "rb") as f2:
            da, db = f1.read(), f2.read()
    except OSError:
        return (-1.0, 0)
    n = min(len(da), len(db))
    if n == 0:
        return (-1.0, 0)
    # 取 256 个均匀采样点比较，足够区分"完全没变"和"变了一点"
    step = max(1, n // 256)
    diffs = sum(1 for i in range(0, n, step) if da[i] != db[i])
    return (diffs / (n // step), abs(len(da) - len(db)))


def probe(dm, label: str, action):
    """抓前后两张图，执行 action()，输出变化率。"""
    before = os.path.join(OUT_DIR, f"{label}_before.png")
    after = os.path.join(OUT_DIR, f"{label}_after.png")
    ok1 = snap(dm, before)
    action()
    time.sleep(0.3)
    ok2 = snap(dm, after)
    if not (ok1 and ok2):
        print(f"  抓图失败 before={ok1} after={ok2}")
        return
    rate, sz = diff(before, after)
    print(f"  before/after 变化率={rate:.3f}  文件大小差={sz}B  ({before})")


def do_bind(dm, hwnd: int, mode_key: str) -> bool:
    if mode_key not in MODES:
        print(f"  未知模式 {mode_key}，可选: {list(MODES.keys())}")
        return False
    display, mouse, keypad, public, mode = MODES[mode_key]
    dm.UnBindWindow()
    ret = dm.BindWindowEx(hwnd, display, mouse, keypad, public, mode)
    err = dm.GetLastError() if ret != 1 else 0
    print(f"  BindWindowEx ret={ret} err={err} mode={mode_key}: {display}|{mouse}|{keypad}|{mode}")
    if ret == 1:
        dm._bound_hwnd = hwnd  # type: ignore[attr-defined]
        dm.EnableRealKeypad(1)
        dm.SetExactOcr(1)
        ret_cs, cw, ch = dm.GetClientSize(hwnd)
        print(f"  client size: {cw}x{ch}  (GetClientSize ret={ret_cs})")
    return ret == 1


def occlude(dm) -> None:
    """用 Explorer 的"显示桌面"把所有窗口最小化，再立即恢复查看器窗口。
    其实更简单的办法是把一个 cmd 置顶盖住查看器 10 秒，这里给出命令提示。"""
    print("  手动把一个记事本或资源管理器窗口拖到查看器上盖住它，然后在这里按回车继续...")
    input()


def repl() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aware", action="store_true", help="声明本进程 DPI 感知")
    args = ap.parse_args()
    if args.aware:
        set_dpi_aware()

    print("启动中... 32位Python+管理员+Reg")
    dm = make_dm()
    dm._bound_hwnd = 0  # type: ignore[attr-defined]
    print(f"dm.Ver() = {dm.Ver()}  aware={args.aware}")
    print(f"截图目录: {OUT_DIR}")
    print("输入 help 查看命令")
    windows: list[tuple[int, str, str, str]] = []

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, *rest = line.split()
        try:
            if cmd in ("help", "?"):
                print(__doc__)
            elif cmd == "list":
                kw = rest[0] if rest else ""
                windows = list_windows(kw)
                for i, (h, cls, title, proc) in enumerate(windows, 1):
                    print(f"  {i:>2}. hwnd={h:<10} proc={proc:<20} class={cls:<30} title={title}")
                if not windows:
                    print("  没有匹配的窗口")
            elif cmd == "bind":
                if len(rest) != 2:
                    print("  用法: bind <序号或hwnd> <模式A-E>")
                    continue
                sel, mode = rest
                if sel.isdigit() and 1 <= int(sel) <= len(windows):
                    hwnd = windows[int(sel) - 1][0]
                else:
                    hwnd = int(sel)
                do_bind(dm, hwnd, mode.upper())
            elif cmd == "rebind":
                if not dm._bound_hwnd:  # type: ignore[attr-defined]
                    print("  还没绑过窗口")
                    continue
                do_bind(dm, dm._bound_hwnd, rest[0].upper() if rest else "A")  # type: ignore[attr-defined]
            elif cmd == "info":
                h = dm._bound_hwnd  # type: ignore[attr-defined]
                if not h:
                    print("  未绑定")
                    continue
                ret_cs, cw, ch = dm.GetClientSize(h)
                ret_mp, mx, my = dm.GetCursorPos()
                print(f"  hwnd={h} client={cw}x{ch} cursor(screen)=({mx},{my})")
            elif cmd == "snap":
                p = os.path.join(OUT_DIR, f"snap_{int(time.time())}.png")
                print("  CapturePng ok" if snap(dm, p) else "  CapturePng 失败")
                print("  ->", p)
            elif cmd == "m":
                x, y = int(rest[0]), int(rest[1])
                probe(dm, f"m_{x}_{y}", lambda: dm.MoveTo(x, y))
            elif cmd == "c":
                x, y = int(rest[0]), int(rest[1])

                def act():
                    dm.MoveTo(x, y)
                    time.sleep(0.1)
                    dm.LeftClick()

                probe(dm, f"c_{x}_{y}", act)
            elif cmd == "r":
                x, y = int(rest[0]), int(rest[1])

                def act_r():
                    dm.MoveTo(x, y)
                    time.sleep(0.1)
                    dm.RightClick()

                probe(dm, f"r_{x}_{y}", act_r)
            elif cmd == "k":
                key = rest[0]
                probe(dm, f"k_{key}", lambda: dm.KeyPressChar(key) if len(key) == 1 else dm.KeyPressStr(key, 50))
            elif cmd == "occlude":
                occlude(dm)
                p = os.path.join(OUT_DIR, f"occluded_{int(time.time())}.png")
                print("  被遮挡抓图:", "ok" if snap(dm, p) else "失败")
                print("  ->", p)
            elif cmd == "unbind":
                dm.UnBindWindow()
                dm._bound_hwnd = 0  # type: ignore[attr-defined]
                print("  已解绑")
            elif cmd in ("q", "quit", "exit"):
                dm.UnBindWindow()
                break
            else:
                print("  未知命令，输入 help")
        except Exception as e:  # noqa: BLE001
            print(f"  !! {type(e).__name__}: {e}")


if __name__ == "__main__":
    repl()
