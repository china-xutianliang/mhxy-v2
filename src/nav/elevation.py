"""权限检查：Windows UIPI 会拒绝低权限进程对高权限窗口的 PrintWindow。

查看器由管理员启动时，普通权限的采集器每次都会收到 ERROR_ACCESS_DENIED，
表现为 PrintWindow 失败，但窗口本身可见、尺寸也正常。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

from common import winapi

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p

TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20


class _TokenElevation(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def process_elevated(pid: int) -> bool:
    """该进程是否以管理员身份运行；读不到就按 False 处理。"""
    if not pid:
        return False
    handle = winapi.kernel32.OpenProcess(winapi.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            info = _TokenElevation()
            size = wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(info),
                                                ctypes.sizeof(info), ctypes.byref(size)):
                return False
            return bool(info.TokenIsElevated)
        finally:
            winapi.kernel32.CloseHandle(token)
    finally:
        winapi.kernel32.CloseHandle(handle)


def viewer_elevated(viewer) -> bool:
    try:
        return process_elevated(winapi.pid_of(viewer.top))
    except Exception:  # noqa: BLE001 权限探测失败不能影响界面
        return False


def needs_elevation(viewer) -> bool:
    """查看器权限更高，当前进程截不到它。"""
    return viewer is not None and not winapi.is_admin() and viewer_elevated(viewer)


def _shell_execute_runas(exe: str, args: str, workdir: str | None) -> int:
    return int(shell32.ShellExecuteW(None, "runas", exe, args, workdir, 1) or 0)


def relaunch_as_admin() -> bool:
    """用 UAC 重新启动自己；用户取消 UAC 时返回 False。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = subprocess.list2cmdline(sys.argv[1:])
        workdir = os.path.dirname(exe) or None
    else:
        exe = sys.executable
        args = subprocess.list2cmdline(["-m", "nav.recorder_app", *sys.argv[1:]])
        workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        return _shell_execute_runas(exe, args, workdir) > 32
    except OSError:
        return False
