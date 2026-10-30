"""查看器窗口管理：找窗口、还原、设定尺寸、启动与确认框、断线检测。

不缓存 hwnd 的长期有效性：窗口失效（is_window 为假）时重新查找。
密码只作为命令行参数传给查看器，不写日志、不落盘。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from common import winapi
from common.log import log
from control import viewer_std
from control.viewer_std import STANDARD_OPTIONS, ViewerSettingsError, ensure_standard, standard_args  # noqa: F401

VIEWER_CLASS = "tvnviewer-session-window"
CONFIRM_BUTTON_PATTERN = re.compile(r"without encryption|不加密|未加密|无加密", re.I)
DEFAULT_VIEWER_EXE = r"C:\Program Files\TightVNC\tvnviewer.exe"


@dataclass(frozen=True)
class ViewerHandles:
    top: int
    child: int

    def alive(self) -> bool:
        return winapi.is_window(self.top) and winapi.is_window(self.child)


def find_viewers(title_contains: str | None = None) -> list[ViewerHandles]:
    """找所有查看器会话窗口：顶层窗口 + 它内部同类名的画面子窗口。"""
    found: list[ViewerHandles] = []
    for h in winapi.enum_top_windows():
        if winapi.class_name(h) != VIEWER_CLASS:
            continue
        if title_contains and title_contains.lower() not in winapi.title(h).lower():
            continue
        child = winapi.find_child(h, VIEWER_CLASS)
        if child:
            found.append(ViewerHandles(h, child))
    return found


def ensure_restored(h: ViewerHandles, wait_s: float = 3.0) -> bool:
    """最小化的查看器客户区是 0x0 且不刷新画面，先还原（不激活、不抢焦点）。"""
    if not winapi.is_iconic(h.top):
        return True
    winapi.show_noactivate(h.top)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not winapi.is_iconic(h.top) and winapi.client_size(h.child)[0] > 0:
            return True
        time.sleep(0.2)
    return not winapi.is_iconic(h.top)


def reveal(h: ViewerHandles, wait_s: float = 2.0) -> bool:
    """最小化时用两种不激活原语还原；只新增函数，不改 ensure_restored 既有行为。

    先 ShowWindow(SW_SHOWNOACTIVATE)，不行再 SetWindowPlacement(SW_SHOWNOACTIVATE)。
    查看器刷画面忙时第一次可能来不及还原，调用方可稍后重试。
    """
    if not winapi.is_iconic(h.top):
        return True
    for attempt in (winapi.show_noactivate, winapi.restore_noactivate):
        attempt(h.top)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not winapi.is_iconic(h.top) and winapi.client_size(h.child)[0] > 0:
                return True
            time.sleep(0.2)
        if not winapi.is_iconic(h.top) and winapi.client_size(h.child)[0] > 0:
            return True
    return not winapi.is_iconic(h.top)


def plan_window_size(outer: tuple[int, int], child_client: tuple[int, int],
                     target_client: tuple[int, int]) -> tuple[int, int]:
    """要把画面子窗口客户区调到 target，顶层窗口外框应设为多大。

    外框与子窗口客户区的差值是标题栏、边框、工具栏，调整过程中基本不变。
    """
    dx = outer[0] - child_client[0]
    dy = outer[1] - child_client[1]
    return target_client[0] + dx, target_client[1] + dy


def fit_window(h: ViewerHandles, target_client: tuple[int, int], tries: int = 3) -> tuple[int, int]:
    """主动把子窗口客户区调到 target；返回最终实测尺寸（调不到时不抛错，由上层校验判定）。"""
    ensure_restored(h)
    for _ in range(tries):
        left, top, right, bottom = winapi.window_rect(h.top)
        outer = (right - left, bottom - top)
        cur = winapi.client_size(h.child)
        if cur == target_client:
            return cur
        w, hgt = plan_window_size(outer, cur, target_client)
        winapi.set_window_size(h.top, w, hgt)
        time.sleep(0.4)
    return winapi.client_size(h.child)


def restore_maximized(h: ViewerHandles, wait_s: float = 1.5) -> bool:
    """最大化的查看器直接 SetWindowPos 不会改变客户区，先还原成普通窗口（不激活）。

    只新增此函数，不改变 fit_window/ensure_restored 的既有行为；调用方在
    fit_window 之前调用即可。返回是否发生了还原。
    """
    if not winapi.is_zoomed(h.top):
        return False
    winapi.restore_noactivate(h.top)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not winapi.is_zoomed(h.top):
            return True
        time.sleep(0.1)
    return not winapi.is_zoomed(h.top)


def launch_viewer(host: str, port: int, password: str, exe: str = DEFAULT_VIEWER_EXE) -> subprocess.Popen:
    """按标准参数（见 viewer_std）启动查看器。

    先关掉连着同一地址但参数不标准的查看器；启动后读回命令行核对，不合格则重开一次，
    仍不合格抛 ViewerSettingsError（OSError 子类，错误文字可直接给用户看）。
    """
    if not os.path.exists(exe):
        raise FileNotFoundError(f"找不到查看器：{exe}")
    viewer_std.prepare(exe)
    killed = viewer_std.ensure_standard(host, port)
    args = [exe, *viewer_std.standard_args(host, port, password)]
    bad: list[str] = []
    for _ in range(2):
        proc = subprocess.Popen(args)
        bad = viewer_std.verify_process(proc.pid, host, port)
        if not bad:
            log(f"{'已按标准参数重开' if killed else '启动'}查看器 {host}::{port}")  # 不记录参数，避免泄露密码
            return proc
        log(f"查看器启动参数核对不合格 {host}::{port}：{'；'.join(bad)}")
        proc.kill()
        proc.wait()
    raise ViewerSettingsError("查看器设置不对：" + "；".join(bad))


def confirm_unencrypted(pid: int, timeout_s: float = 10.0) -> bool:
    """点掉"未加密连接"确认框。返回是否点到了按钮。窗口类 #32770，按钮文字匹配英文/中文。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for top in winapi.enum_top_windows():
            if winapi.pid_of(top) != pid or winapi.class_name(top) != "#32770":
                continue
            for kid in winapi.enum_children(top):
                if winapi.class_name(kid) == "Button" and CONFIRM_BUTTON_PATTERN.search(winapi.title(kid)):
                    winapi.click_button(kid)
                    return True
        time.sleep(0.3)
    return False


def wait_for_viewer(title_contains: str | None, timeout_s: float = 15.0) -> ViewerHandles | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        vs = find_viewers(title_contains)
        if vs and winapi.client_size(vs[0].child)[0] > 0:
            return vs[0]
        time.sleep(0.4)
    return None
