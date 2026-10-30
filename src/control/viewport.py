from __future__ import annotations

from dataclasses import dataclass

GAME_W, GAME_H = 800, 600


@dataclass
class Viewport:
    origin: tuple[int, int] = (0, 0)
    game_size: tuple[int, int] = (GAME_W, GAME_H)
    input_scale: float = 1.0
    capture_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.input_scale <= 0 or self.capture_scale <= 0:
            raise ValueError("输入与截图尺度必须大于 0")

    def client_size(self) -> tuple[int, int]:
        return (round(self.game_size[0] * self.capture_scale),
                round(self.game_size[1] * self.capture_scale))

    def input_size(self) -> tuple[int, int]:
        return (round(self.game_size[0] * self.input_scale),
                round(self.game_size[1] * self.input_scale))

    def g_to_v(self, x: int, y: int) -> tuple[int, int]:
        return (round(x * self.input_scale) + self.origin[0],
                round(y * self.input_scale) + self.origin[1])

    def v_to_g(self, x: int, y: int) -> tuple[int, int]:
        return (round((x - self.origin[0]) / self.input_scale),
                round((y - self.origin[1]) / self.input_scale))

    def g_to_capture(self, x: int, y: int) -> tuple[int, int]:
        return (round(x * self.capture_scale) + self.origin[0],
                round(y * self.capture_scale) + self.origin[1])

    def capture_to_g(self, x: int, y: int) -> tuple[int, int]:
        return (round((x - self.origin[0]) / self.capture_scale),
                round((y - self.origin[1]) / self.capture_scale))

    def region_to_v(self, x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        a = self.g_to_v(x1, y1)
        b = self.g_to_v(x2, y2)
        return a[0], a[1], b[0], b[1]

    def region_to_capture(self, x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        a = self.g_to_capture(x1, y1)
        b = self.g_to_capture(x2, y2)
        return a[0], a[1], b[0], b[1]

    def contains_g(self, x: int, y: int) -> bool:
        return 0 <= x < self.game_size[0] and 0 <= y < self.game_size[1]

    def check_g(self, x: int, y: int) -> None:
        if not self.contains_g(x, y):
            raise ValueError(f"游戏坐标越界：({x},{y})，范围 0..{self.game_size[0] - 1} x 0..{self.game_size[1] - 1}")
