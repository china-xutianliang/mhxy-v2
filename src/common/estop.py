"""全局急停：热键 Ctrl+Alt+Q 置位事件。所有长时间动作必须检查 `estop.is_set()`。

热键线程只置位事件，不调用大漠（COM 对象只能在创建它的线程里用）。
释放修饰键等善后由主线程在看到事件后执行。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from common.log import log

_event = threading.Event()
_started = False

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY = 0x0312
VK_Q = 0x51
_HOTKEY_ID = 0x4D48


class Aborted(RuntimeError):
    """急停或超时导致的中止。"""


def is_set() -> bool:
    return _event.is_set()


def trigger(reason: str = "手动") -> None:
    if not _event.is_set():
        log(f"!! 急停触发：{reason}")
    _event.set()


def clear() -> None:
    _event.clear()


def check() -> None:
    if _event.is_set():
        raise Aborted("急停")


def start_hotkey() -> bool:
    """注册 Ctrl+Alt+Q。重复调用安全；注册失败（被占用）返回 False。"""
    global _started
    if _started:
        return True
    ok = threading.Event()
    result = {"ok": False}

    def loop() -> None:
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, _HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q):
            ok.set()
            return
        result["ok"] = True
        ok.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                trigger("热键 Ctrl+Alt+Q")

    threading.Thread(target=loop, name="estop-hotkey", daemon=True).start()
    ok.wait(2.0)
    _started = result["ok"]
    if not _started:
        log("急停热键 Ctrl+Alt+Q 注册失败（可能被其他程序占用），请用界面上的急停按钮")
    return _started
