"""裸 PostMessage 探针：绕开大漠，直接验证远程查看器窗口认不认"合成的鼠标消息"。

为什么需要它
------------
大漠 mouse="windows" 本质上就是往目标窗口投递 WM_MOUSEMOVE / WM_LBUTTONDOWN /
WM_LBUTTONUP 这类窗口消息。所以真正的问题不是"大漠怎么配"，而是：

    查看器（tvnviewer 之类）到底是从消息的 lParam 里取坐标，还是去读真实光标？

- 如果它用 lParam（大多数 VNC viewer 是这样），PostMessage 就够，大漠 windows 档能用。
- 如果它调 GetCursorPos() 或用了 Raw Input（WM_INPUT），PostMessage 完全无效，
  消息投进去也石沉大海——这种情况只能退到真实输入档（架构设计方案.md 10.6 优先级 3）。

这个探针把"查看器认不认合成消息"和"大漠参数对不对"两件事拆开了。探针能过而大漠
不过，问题在大漠的配置或绑定的 hwnd；探针也过不了，那就别再在消息模式上耗时间。

用法
----
    # 1) 只发 WM_MOUSEMOVE，安全，不会点到任何东西
    python probe_postmsg.py --title tvnviewer --x 400 --y 300

    # 2) 再发按下/弹起（点击会转发到副机，先瞄准副机桌面的空白处）
    python probe_postmsg.py --title tvnviewer --x 400 --y 300 --click

    # 3) 直接指定句柄
    python probe_postmsg.py --hwnd 123456 --x 400 --y 300

判定
----
盯副机屏幕（不是主控机的查看器画面，是副机本身）：
    - 光标跟着跑到 (x, y)  -> 查看器认 lParam，消息模式可行。
    - 光标纹丝不动         -> 查看器在读真实光标/Raw Input，消息模式不可行。

看控制台输出：
    - PostMessage 返回 0 且 err=5 -> UIPI 拦截（查看器进程权限比本进程高），
      用管理员终端重跑。这个坑对大漠同样成立，绑不上也可能就是这个原因。
"""

from __future__ import annotations

import argparse
import ctypes
import time
from ctypes import wintypes

# use_last_error=True 才能在失败时拿到真实错误码（区分 UIPI 拦截和句柄无效）
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.restype = wintypes.BOOL
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.restype = wintypes.BOOL
user32.IsZoomed.restype = wintypes.BOOL

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# 64 位下 HWND 超过 int 范围，必须显式声明返回类型，否则会被截断成负数
user32.RealChildWindowFromPoint.restype = wintypes.HWND
user32.RealChildWindowFromPoint.argtypes = [wintypes.HWND, POINT]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def make_lparam(x: int, y: int) -> int:
    """WM_MOUSEMOVE/WM_LBUTTONDOWN 的 lParam：低 16 位是客户区 x，高 16 位是 y。"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def win_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def win_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def win_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def client_size(hwnd: int) -> tuple[int, int]:
    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def find_windows(title_sub: str) -> list[int]:
    hits: list[int] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if title_sub.lower() in win_text(hwnd).lower():
            hits.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hits


def describe(hwnd: int) -> str:
    cw, ch = client_size(hwnd)
    flags = []
    if user32.IsIconic(hwnd):
        flags.append("已最小化(消息可能被提前 return)")
    if user32.IsZoomed(hwnd):
        flags.append("已最大化")
    extra = ("  " + " ".join(flags)) if flags else ""
    return (f"hwnd={hwnd} class={win_class(hwnd)!r} pid={win_pid(hwnd)} "
            f"client={cw}x{ch} title={win_text(hwnd)!r}{extra}")


def post(hwnd: int, msg: int, wparam: int, lparam: int, label: str) -> bool:
    ok = user32.PostMessageW(hwnd, msg, wparam, lparam)
    err = ctypes.get_last_error() if not ok else 0
    if ok:
        print(f"    {label:<16} -> 已投递")
        return True
    hint = ""
    if err == 5:
        hint = "  [UIPI 拦截：目标进程权限更高，请用管理员终端重跑]"
    elif err == 1400:
        hint = "  [句柄无效]"
    print(f"    {label:<16} -> 失败 err={err}{hint}")
    return False


def probe(hwnd: int, x: int, y: int, click: bool) -> None:
    print(f"\n目标: {describe(hwnd)}")
    cw, ch = client_size(hwnd)
    if not (0 <= x < cw and 0 <= y < ch):
        print(f"  !! 坐标 ({x},{y}) 不在客户区 {cw}x{ch} 内，跳过")
        return

    lparam = make_lparam(x, y)

    # 顶层窗口投递一次
    print(f"  -> 投递到顶层窗口 hwnd={hwnd}，客户区坐标 ({x},{y})")
    post(hwnd, WM_MOUSEMOVE, 0, lparam, "WM_MOUSEMOVE")
    time.sleep(0.2)
    if click:
        post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam, "WM_LBUTTONDOWN")
        time.sleep(0.05)
        post(hwnd, WM_LBUTTONUP, 0, lparam, "WM_LBUTTONUP")

    # 再看这个点下面是不是有子窗口。查看器的输入常常由子窗口处理，
    # 只发给顶层会被 DefWindowProc 吞掉——这正是大漠 windows3 存在的理由。
    pt = POINT(x, y)
    child = user32.RealChildWindowFromPoint(hwnd, pt)
    if child and child != hwnd:
        print(f"  -> 该点下还有子窗口 hwnd={child} class={win_class(child)!r}，"
              f"再投递一份（大漠 windows3 就是干这个的）")
        post(child, WM_MOUSEMOVE, 0, lparam, "WM_MOUSEMOVE")
        time.sleep(0.2)
        if click:
            post(child, WM_LBUTTONDOWN, MK_LBUTTON, lparam, "WM_LBUTTONDOWN")
            time.sleep(0.05)
            post(child, WM_LBUTTONUP, 0, lparam, "WM_LBUTTONUP")
    else:
        print("  -> 该点下没有独立子窗口，只有顶层窗口处理输入")


def main() -> None:
    ap = argparse.ArgumentParser(description="裸 PostMessage 探针")
    ap.add_argument("--title", default="tvnviewer", help="窗口标题包含的关键字（默认 tvnviewer）")
    ap.add_argument("--hwnd", type=int, default=0, help="直接指定窗口句柄，给了就不按标题找")
    ap.add_argument("--x", type=int, default=400, help="客户区 x（默认 400）")
    ap.add_argument("--y", type=int, default=300, help="客户区 y（默认 300）")
    ap.add_argument("--click", action="store_true", help="同时发按下/弹起（会真的点到副机上）")
    args = ap.parse_args()

    if args.hwnd:
        targets = [args.hwnd]
    else:
        targets = find_windows(args.title)
        if not targets:
            print(f"没找到标题包含 {args.title!r} 的可见窗口。"
                  f"加 --title 换关键字，或用 --hwnd 指定。")
            return
        print(f"找到 {len(targets)} 个候选窗口：")
        for h in targets:
            print("  " + describe(h))

    for h in targets:
        probe(h, args.x, args.y, args.click)

    print("\n现在去盯副机屏幕：光标有没有跑到 (x, y)？")
    print("  动了   -> 查看器认 lParam，大漠 windows/windows3 这条路可行")
    print("  没动   -> 查看器在读真实光标或 Raw Input，消息模式走不通，"
          "退到真实输入档（10.6 优先级 3）")


if __name__ == "__main__":
    main()
