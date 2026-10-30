from __future__ import annotations

from common.config import load_yaml
from control.dm_factory import DmError
from control import target as target_mod


class Channel:
    def __init__(self, dm, params: dict | None = None):
        self.dm = dm
        p = params or load_yaml("config", "driver.yaml")["bind_viewer"]
        self.display = p["display"]
        self.mouse = p["mouse"]
        self.keypad = p["keypad"]
        self.public = p.get("public", "")
        self.mode = int(p.get("mode", 0))
        self.hwnd = 0
        self.target: target_mod.ViewerTarget | None = None

    def bind(self, hwnd: int) -> None:
        resolved = target_mod.validate_child(hwnd)
        self.dm.UnBindWindow()
        ret = self.dm.BindWindowEx(resolved.child, self.display, self.mouse, self.keypad, self.public, self.mode)
        if ret != 1:
            raise DmError(f"BindWindowEx 失败 ret={ret} err={self.dm.GetLastError()} hwnd={resolved.child}")
        self.dm.EnableRealKeypad(1)
        self.hwnd = resolved.child
        self.target = resolved

    def assert_target(self) -> target_mod.ViewerTarget:
        if not self.hwnd:
            raise target_mod.TargetError("尚未绑定画面子窗口")
        self.target = target_mod.validate_child(self.hwnd)
        return self.target

    def unbind(self) -> None:
        self.dm.UnBindWindow()
        self.hwnd = 0
        self.target = None

    @property
    def bound(self) -> bool:
        return self.hwnd != 0

    def client_size(self) -> tuple[int, int]:
        _, w, h = self.dm.GetClientSize(self.hwnd)
        return int(w), int(h)

    def move_to(self, x: int, y: int) -> None:
        self.dm.MoveTo(x, y)

    def down(self, button: str) -> None:
        (self.dm.LeftDown if button == "left" else self.dm.RightDown)()

    def up(self, button: str) -> None:
        (self.dm.LeftUp if button == "left" else self.dm.RightUp)()

    def wheel(self, notches: int) -> None:
        method = self.dm.WheelUp if notches > 0 else self.dm.WheelDown
        for _ in range(abs(notches)):
            method()

    def key_down(self, key: str) -> None:
        self.dm.KeyDownChar(key.lower())

    def key_up(self, key: str) -> None:
        self.dm.KeyUpChar(key.lower())

    def press(self, key: str) -> None:
        self.dm.KeyPressChar(key)

    def get_color(self, x: int, y: int) -> str:
        return self.dm.GetColor(x, y)

    def capture_png(self, x1: int, y1: int, x2: int, y2: int, path: str) -> bool:
        return self.dm.CapturePng(x1, y1, x2, y2, path) == 1

    def capture_jpg(self, x1: int, y1: int, x2: int, y2: int, path: str, quality: int = 70) -> bool:
        """留证用的小图：800x600 约 110KB、12ms，PNG 要 900KB、110ms。要无损的模板截图别用它。"""
        return self.dm.CaptureJpg(x1, y1, x2, y2, path, quality) == 1

    def find_pic(self, x1, y1, x2, y2, pic: str, delta="202020", sim=0.9) -> tuple[int, int] | None:
        ret, x, y = self.dm.FindPic(x1, y1, x2, y2, pic, delta, sim, 0)
        return (int(x), int(y)) if ret >= 0 else None

    def set_pic_dir(self, path: str) -> None:
        self.dm.SetPath(path)
