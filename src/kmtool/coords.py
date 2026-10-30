"""坐标换算与输入解析：只处理数字，不碰窗口和 Tk，便于单测。"""
from __future__ import annotations

GAME_W, GAME_H = 800, 600


def view_to_game(px: int, py: int, width: int, height: int) -> tuple[int, int] | None:
    """把预览画布或查看器客户区里的像素点换算成游戏坐标；落在范围外返回 None。"""
    if width <= 0 or height <= 0:
        return None
    if px < 0 or py < 0 or px >= width or py >= height:
        return None
    return min(GAME_W - 1, int(px * GAME_W / width)), min(GAME_H - 1, int(py * GAME_H / height))


def parse_point(x_text, y_text) -> tuple[int, int]:
    try:
        x = int(str(x_text).strip())
        y = int(str(y_text).strip())
    except (TypeError, ValueError):
        raise ValueError("X 和 Y 需要填写整数") from None
    if not (0 <= x < GAME_W and 0 <= y < GAME_H):
        raise ValueError("X 需在 0–799，Y 需在 0–599 之间")
    return x, y


ALIGN_TOL_RANGE = (1, 20)
ALIGN_MAX_S_RANGE = (3, 120)


def parse_align(tol_text, max_text) -> tuple[int, int]:
    """箭头对齐的容差（像素）和最长对齐时间（秒）。"""
    try:
        tol = int(str(tol_text).strip())
        max_s = int(str(max_text).strip())
    except (TypeError, ValueError):
        raise ValueError("容差和最长时间需要填整数") from None
    if not (ALIGN_TOL_RANGE[0] <= tol <= ALIGN_TOL_RANGE[1]):
        raise ValueError(f"容差需在 {ALIGN_TOL_RANGE[0]}–{ALIGN_TOL_RANGE[1]} 像素之间")
    if not (ALIGN_MAX_S_RANGE[0] <= max_s <= ALIGN_MAX_S_RANGE[1]):
        raise ValueError(f"最长时间需在 {ALIGN_MAX_S_RANGE[0]}–{ALIGN_MAX_S_RANGE[1]} 秒之间")
    return tol, max_s


def parse_hold(text) -> tuple[int, int]:
    parts = [p for p in str(text).replace("，", ",").replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError("按住范围写成“最小,最大”，例如 60,140")
    try:
        lo, hi = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("按住范围需要两个整数，例如 60,140") from None
    if not (0 < lo <= hi <= 2000):
        raise ValueError("按住范围需满足 0 < 最小 ≤ 最大 ≤ 2000")
    return lo, hi
