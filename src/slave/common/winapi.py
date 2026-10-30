"""Win32 薄封装（只用 ctypes，副机上的 game_share 也要用，所以不依赖第三方库）。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HWND = wintypes.HWND

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [HWND, ctypes.c_void_p, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.FindWindowExW.argtypes = [HWND, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = HWND
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.IsIconic.argtypes = [HWND]
user32.IsWindow.argtypes = [HWND]
user32.IsWindowVisible.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SendMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetParent.argtypes = [HWND]
user32.GetParent.restype = HWND
user32.GetForegroundWindow.restype = HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

SW_SHOWNOACTIVATE = 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SW_HIDE = 0
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
BM_CLICK = 0x00F5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)


def hide_from_taskbar(h: int) -> bool:
    """改成工具窗口样式，任务栏不再显示它的图标；窗口本身照常存在、照常能截图。

    样式要在窗口隐藏状态下改，再重新显示，任务栏才会刷新。已经是工具窗口则什么都不做。
    """
    user32.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [HWND, ctypes.c_int, ctypes.c_long]
    ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
        return False
    was_visible = bool(user32.IsWindowVisible(h))
    if was_visible:
        user32.ShowWindow(h, SW_HIDE)
    user32.SetWindowLongW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    if was_visible:
        user32.ShowWindow(h, SW_SHOWNOACTIVATE)
    return True


def set_dpi_aware() -> None:
    """Per-Monitor V2。必须在创建任何窗口、读取任何窗口尺寸之前调用。"""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except OSError:
        pass


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def enum_top_windows() -> list[int]:
    out: list[int] = []

    def cb(h, _l):
        out.append(h or 0)
        return True

    user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return out


def enum_children(parent: int) -> list[int]:
    out: list[int] = []

    def cb(h, _l):
        out.append(h or 0)
        return True

    user32.EnumChildWindows(parent, _WNDENUMPROC(cb), 0)
    return out


def class_name(h: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def title(h: int) -> str:
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(max(n + 1, 2))
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def pid_of(h: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return pid.value


def image_path(pid: int) -> str:
    hp = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hp:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(hp)


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _ProcEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_paths(exe_name: str) -> list[str]:
    """按进程名（不区分大小写）找正在运行的进程，返回它们的完整路径（读不到路径的跳过）。"""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == INVALID_HANDLE:
        return []
    out: list[str] = []
    try:
        pe = _ProcEntry()
        pe.dwSize = ctypes.sizeof(_ProcEntry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == exe_name.lower():
                p = image_path(pe.th32ProcessID)
                if p and p not in out:
                    out.append(p)
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return out


def find_child(parent: int, cls: str | None = None, text: str | None = None) -> int:
    return user32.FindWindowExW(parent, None, cls, text) or 0


def parent_of(h: int) -> int:
    return user32.GetParent(h) or 0


def is_window(h: int) -> bool:
    return bool(user32.IsWindow(h))


def is_iconic(h: int) -> bool:
    return bool(user32.IsIconic(h))


def show_noactivate(h: int) -> None:
    user32.ShowWindow(h, SW_SHOWNOACTIVATE)


def window_rect(h: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def client_size(h: int) -> tuple[int, int]:
    r = wintypes.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def client_origin_on_screen(h: int) -> tuple[int, int]:
    p = wintypes.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(p))
    return p.x, p.y


def set_window_size(h: int, w: int, hgt: int, x: int | None = None, y: int | None = None) -> bool:
    """调整大小（可选移动），不激活、不改 Z 序。"""
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if x is None or y is None:
        flags |= SWP_NOMOVE
        x = y = 0
    return bool(user32.SetWindowPos(h, None, x, y, w, hgt, flags))


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def click_button(h: int) -> None:
    user32.SendMessageW(h, BM_CLICK, 0, 0)


def find_top_windows(pred: Callable[[int], bool]) -> list[int]:
    return [h for h in enum_top_windows() if pred(h)]
