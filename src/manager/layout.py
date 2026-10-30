"""总览铺排的纯计算（不依赖界面，方便单元测试）。"""
from __future__ import annotations

import math
from dataclasses import dataclass

ASPECT = 4 / 3          # 游戏画面 800x600
MAX_TILE_W = 1600       # 超过游戏原画 800 宽只是插值放大，没有更多细节；留余量给高分屏上的放大视图


@dataclass(frozen=True)
class Grid:
    cols: int
    rows: int
    img_w: int          # 每个格子里画面的像素宽
    img_h: int
    cell_w: int
    cell_h: int


def fit_image(cell_w: int, cell_h: int, chrome_h: int) -> tuple[int, int]:
    """格子里扣掉标题/状态栏后，4:3 画面最大能放多大。"""
    avail_h = max(0, cell_h - chrome_h)
    w = int(min(cell_w, avail_h * ASPECT, MAX_TILE_W))
    w -= w % 2
    return max(0, w), max(0, int(w / ASPECT))


def compute_grid(n: int, avail_w: int, avail_h: int, chrome_h: int, gap: int, mode: str = "auto") -> Grid:
    """n 台机器铺在 avail_w x avail_h 的区域里。

    mode: "auto" 选画面最大的列数；"cols:N" 固定每行 N 台（N 超过 n 时按 n）。
    """
    n = max(1, n)
    if mode.startswith("cols:"):
        try:
            candidates = [max(1, min(n, int(mode.split(":", 1)[1])))]
        except ValueError:
            candidates = list(range(1, n + 1))
    else:
        candidates = list(range(1, n + 1))
    best: Grid | None = None
    for cols in candidates:
        rows = math.ceil(n / cols)
        cell_w = (avail_w - gap * (cols + 1)) // cols
        cell_h = (avail_h - gap * (rows + 1)) // rows
        img_w, img_h = fit_image(cell_w, cell_h, chrome_h)
        g = Grid(cols, rows, img_w, img_h, cell_w, cell_h)
        if best is None or g.img_w > best.img_w:
            best = g
    assert best is not None
    return best


def focus_image_size(avail_w: int, avail_h: int) -> tuple[int, int]:
    """放大视图里主画面的尺寸（4:3，尽量大）。"""
    return fit_image(avail_w, avail_h, 0)
