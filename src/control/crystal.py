"""游戏内水晶光标的识别：找绿色宝石，纯 Python，不依赖 OpenCV/Pillow。

真机测量：只有水晶左上角的箭头尖是有效点击点；宝石只是定位水晶用的，
宝石中心在箭头尖右下约 (13, 14)px。宝石是紧凑的亮黄绿色块（8x8 内约 34~42
像素），外面有一圈金色镶边；树叶/草地更暗、更细长，也没有金边。只在目标点
附近 57x57 的窗口里找，真机 15 帧均唯一命中，单帧约 4ms。
"""
from __future__ import annotations

GEM_TO_TIP = (13, 14)
SEARCH_HALF = 28
MIN_AREA = 18
MIN_FILL = 0.55
MIN_YELLOW = 3
CURSOR_SEARCH_HALF = 110
CURSOR_MIN_AREA = 14
CURSOR_MAX_AREA = 60
CURSOR_MIN_BODY = 50


def is_gem_pixel(r: int, g: int, b: int) -> bool:
    return g >= 155 and r >= 95 and b <= 160 and g >= r + 25 and g >= b + 35


def _is_gold_pixel(r: int, g: int, b: int) -> bool:
    return r >= 190 and g >= 160 and b <= 110


def arrow_tip(gem: tuple[int, int, int]) -> tuple[int, int]:
    return gem[0] - GEM_TO_TIP[0], gem[1] - GEM_TO_TIP[1]


def _is_body_pixel(r: int, g: int, b: int) -> bool:
    """水晶身体的深蓝/青蓝像素（箭头边缘和下半身）。"""
    return b >= 150 and r <= 90 and g <= 200


def body_score(rgb, width: int, height: int, gx: int, gy: int) -> int:
    """宝石周围水晶身体的蓝色像素数。真光标 90~110；场景里的绿色物件和发光特效只有 0~2。"""
    count = 0
    for y in range(max(0, gy - 15), min(height, gy + 10)):
        base = y * width * 3
        for x in range(max(0, gx - 14), min(width, gx + 11)):
            i = base + x * 3
            if _is_body_pixel(rgb[i], rgb[i + 1], rgb[i + 2]):
                count += 1
    return count


def find_gem(rgb, width: int, height: int, center: tuple[int, int], *,
             half: int = SEARCH_HALF, min_area: int = MIN_AREA,
             max_area: int | None = None, body_min: int = 0,
             min_yellow: int = MIN_YELLOW) -> tuple[int, int, int] | None:
    """在 center 附近的方形窗口里找绿色宝石，返回 (x, y, area)，找不到返回 None。

    x/y 是宝石像素质心。候选必须面积足够、形状紧凑、周围有金色镶边，
    以此排除同样偏绿但细长、无金边的树叶。多个候选时取离 center 最近的。
    max_area / body_min 用于大窗口搜索：面积太大的绿色物件、周围没有水晶青蓝身体的
    绿色物件都不是光标（见 find_cursor）。
    """
    if width <= 0 or height <= 0 or len(rgb) < width * height * 3:
        return None
    cx, cy = int(center[0]), int(center[1])
    x0, x1 = max(0, cx - half), min(width, cx + half + 1)
    y0, y1 = max(0, cy - half), min(height, cy + half + 1)
    points = set()
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(x0, x1):
            i = base + x * 3
            if is_gem_pixel(rgb[i], rgb[i + 1], rgb[i + 2]):
                points.add((x, y))
    if not points:
        return None
    best = None
    seen = set()
    for point in points:
        if point in seen:
            continue
        stack = [point]
        seen.add(point)
        component = []
        while stack:
            qx, qy = stack.pop()
            component.append((qx, qy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (qx + dx, qy + dy)
                    if nxt in points and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        area = len(component)
        if area < min_area or (max_area is not None and area > max_area):
            continue
        xs = [p[0] for p in component]
        ys = [p[1] for p in component]
        box_w = max(xs) - min(xs) + 1
        box_h = max(ys) - min(ys) + 1
        if area / (box_w * box_h) < MIN_FILL:
            continue
        gold = 0
        for y in range(max(0, min(ys) - 2), min(height, max(ys) + 3)):
            base = y * width * 3
            for x in range(max(0, min(xs) - 2), min(width, max(xs) + 3)):
                i = base + x * 3
                if _is_gold_pixel(rgb[i], rgb[i + 1], rgb[i + 2]):
                    gold += 1
        if gold < min_yellow:
            continue
        mx = sum(xs) / area
        my = sum(ys) / area
        if body_min and body_score(rgb, width, height, int(round(mx)), int(round(my))) < body_min:
            continue
        distance = (mx - cx) ** 2 + (my - cy) ** 2
        if best is None or distance < best[0] or (distance == best[0] and area > best[3]):
            best = (distance, mx, my, area)
    if best is None:
        return None
    _distance, mx, my, area = best
    return int(round(mx)), int(round(my)), area


def find_cursor(rgb, width: int, height: int, center: tuple[int, int], *,
                half: int = CURSOR_SEARCH_HALF) -> tuple[int, int, int] | None:
    """大窗口里找水晶光标：宝石 + 周围的青蓝身体，面积不能超过光标宝石的范围。

    真机（建邺城，2026-10-29）：光标宝石面积 34~43，身体分 160~185；场景里的绿色物件
    要么面积 100 以上，要么身体分不超过 60。光标会闪烁，闪烁相位里宝石外圈的金边会消失，
    所以这里不要求金边，改由身体分把关。
    """
    return find_gem(rgb, width, height, center, half=half, min_area=CURSOR_MIN_AREA,
                    max_area=CURSOR_MAX_AREA, body_min=CURSOR_MIN_BODY, min_yellow=0)
