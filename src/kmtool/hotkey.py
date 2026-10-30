"""测试工具自己的全局热键，独立于急停热键，避免改动第一层公用模块。"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class GlobalHotkey:
    def __init__(self, vk: int, *, modifiers: int = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                 hotkey_id: int = 0x4D49):
        self.vk = vk
        self.modifiers = modifiers
        self.hotkey_id = hotkey_id
        self.ok = False
        self._fired = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    def start(self, timeout: float = 2.0) -> bool:
        if self._thread is not None:
            return self.ok
        self._thread = threading.Thread(target=self._loop, name="kmtool-hotkey", daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        return self.ok

    def _loop(self) -> None:
        u = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            self.ok = bool(u.RegisterHotKey(None, self.hotkey_id, self.modifiers, self.vk))
        except Exception:
            self.ok = False
        self._started.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    self._fired.set()
        finally:
            u.UnregisterHotKey(None, self.hotkey_id)

    def take(self) -> bool:
        if self._fired.is_set():
            self._fired.clear()
            return True
        return False

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
