from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class Profile:
    fitts_a_ms: tuple[float, float] = (80.0, 120.0)
    fitts_b_ms: tuple[float, float] = (90.0, 150.0)
    target_width_px: float = 32.0
    duration_jitter: float = 0.15
    sample_ms: tuple[int, int] = (8, 16)
    control_offset: tuple[float, float] = (0.05, 0.20)
    noise_px: float = 1.0
    overshoot_probability: tuple[float, float] = (0.15, 0.30)
    overshoot_ratio: tuple[float, float] = (0.02, 0.06)
    pre_press_ms: tuple[int, int] = (40, 120)
    hold_ms: tuple[int, int] = (60, 140)
    double_interval_ms: tuple[int, int] = (80, 160)
    key_hold_ms: tuple[int, int] = (50, 120)
    modifier_delay_ms: tuple[int, int] = (30, 80)
    modifier_release_delay_ms: tuple[int, int] = (20, 60)
    wheel_ms: tuple[int, int] = (30, 90)
    # 看到偏差后的小幅修正（plan_correction）：比首次移动短，近似直线，起步快收尾慢。
    correction_a_ms: tuple[float, float] = (50.0, 90.0)
    correction_b_ms: tuple[float, float] = (70.0, 110.0)
    correction_target_px: float = 12.0
    correction_control_offset: tuple[float, float] = (0.02, 0.06)
    # 小于 1：速度峰值前移，减速段更长，像人用眼睛盯着目标收手。
    correction_skew: float = 0.8
    # 人看到偏差到手开始动之间的反应时间，叠加在画面稳定等待之后。
    reaction_ms: tuple[int, int] = (60, 160)
    # 接近目标时不一次性吃满误差，留一个很小的下一帧确认，避免像算法直接刹停。
    # 只在误差大于容差且进入 terminal_radius_px 时使用。
    terminal_radius_px: float = 18.0
    terminal_gain: tuple[float, float] = (1.0, 1.0)
    # 闭环每一轮的基础修正比例。默认档为 1，LAN 档逐轮逼近。
    correction_gain: tuple[float, float] = (1.0, 1.0)
    # 首次移动故意停在目标前的一小段，随后由视觉闭环补齐；默认档为 0，保持原行为。
    initial_approach_gap_px: tuple[float, float] = (0.0, 0.0)
    # 修正轨迹可以比主路径更密，避免小段移动被整数坐标量化成卡顿；None 表示沿用 sample_ms。
    correction_sample_ms: tuple[int, int] | None = None
    # 修正前的短反应停顿；None 表示沿用 reaction_ms。
    correction_reaction_ms: tuple[int, int] | None = None
    # 收尾确认后的极短停留，给手眼协调留下自然的停顿。
    terminal_hold_ms: tuple[int, int] = (0, 0)
    # 轨迹节奏：值小于 1 时峰值速度略微前移，收尾更长。
    path_skew: float = 1.0
    # 局域网自然轨迹使用同侧、相关的横向偏移和低频微抖动；
    # 默认档保持原行为，便于离线逻辑和历史调用方稳定复现。
    correlated_controls: bool = False
    correlated_noise: bool = False

    @classmethod
    def lan(cls) -> "Profile":
        """800x600 局域网查看器使用的自然轨迹档。

        远程输入坐标仍然是精确的，变化只发生在到达目标前的轨迹和节奏。
        末点始终由 ``plan_path`` 精确写入，箭尖闭环由 Actuator 单独负责。
        """
        return cls(
            sample_ms=(10, 14),
            control_offset=(0.02, 0.08),
            noise_px=0.6,
            overshoot_probability=(0.08, 0.18),
            overshoot_ratio=(0.02, 0.04),
            path_skew=0.88,
            correlated_controls=True,
            correlated_noise=True,
            correction_control_offset=(0.01, 0.04),
            correction_skew=0.78,
            reaction_ms=(45, 100),
            terminal_radius_px=24.0,
            terminal_gain=(0.58, 0.78),
            correction_gain=(0.72, 0.88),
            initial_approach_gap_px=(8.0, 16.0),
            correction_sample_ms=(8, 11),
            correction_reaction_ms=(18, 38),
            terminal_hold_ms=(45, 95),
        )


_THINK_RANGES = {
    "default": (120, 350),
    "move": (60, 180),
    "click": (120, 350),
    "double_click": (150, 400),
    "drag": (200, 500),
    "key": (60, 180),
    "combo": (100, 300),
    "text": (40, 120),
    "page": (300, 800),
    "battle": (200, 600),
}


def _check_point(point):
    x, y = point
    if not (0 <= x <= 799 and 0 <= y <= 599):
        raise ValueError("point outside 800x600")
    return int(x), int(y)


def _clamp(x, y):
    return min(799, max(0, int(round(x)))), min(599, max(0, int(round(y))))


def _range_value(rng, value):
    if isinstance(value, (tuple, list)):
        return rng.uniform(value[0], value[1])
    return float(value)


def _controls(start, end, rng, profile, offset_range=None):
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance == 0:
        return (sx + dx / 3, sy + dy / 3), (sx + dx * 2 / 3, sy + dy * 2 / 3)
    px, py = -dy / distance, dx / distance
    offset_range = offset_range or profile.control_offset
    if getattr(profile, "correlated_controls", False):
        # 真人移动通常向一侧轻微偏移后再收回，不会每个控制点独立换边。
        side = rng.choice((-1, 1))
        first = rng.uniform(*offset_range)
        second = first * rng.uniform(0.55, 0.95)
        offset1 = distance * first * side
        offset2 = distance * second * side
    else:
        offset1 = distance * rng.uniform(*offset_range) * rng.choice((-1, 1))
        offset2 = distance * rng.uniform(*offset_range) * rng.choice((-1, 1))
    c1 = (sx + dx / 3 + px * offset1, sy + dy / 3 + py * offset1)
    c2 = (sx + dx * 2 / 3 + px * offset2, sy + dy * 2 / 3 + py * offset2)
    return c1, c2


def _bezier(start, c1, c2, end, t, skew=1.0):
    # skew < 1 会把峰值速度略微前移，留下更长的视觉收尾段。
    if skew > 0 and skew != 1.0:
        t = max(0.0, min(1.0, t)) ** skew
    s = 10 * t**3 - 15 * t**4 + 6 * t**5
    u3 = (1.0 - s) ** 3
    s3 = s**3
    middle = 3 * (1.0 - s) * s
    x = u3 * start[0] + middle * ((1.0 - s) * c1[0] + s * c2[0]) + s3 * end[0]
    y = u3 * start[1] + middle * ((1.0 - s) * c1[1] + s * c2[1]) + s3 * end[1]
    return x, y


def _durations(total_ms, rng, profile, sample_ms=None):
    low, high = sample_ms or profile.sample_ms
    if low <= 0 or high < low:
        raise ValueError("invalid sample interval")
    min_count = max(1, math.ceil(total_ms / high))
    max_count = max(1, total_ms // low)
    if min_count <= max_count:
        count = rng.randint(min_count, max_count)
    else:
        count = max(1, round(total_ms / ((low + high) / 2)))
    if count == 1:
        return [total_ms]
    result = []
    remaining = total_ms
    for index in range(count - 1):
        slots = count - index - 1
        lo = max(low, remaining - high * slots)
        hi = min(high, remaining - low * slots)
        value = rng.randint(lo, hi)
        result.append(value)
        remaining -= value
    result.append(remaining)
    return result


def _overshoot_point(end, start, distance, ratio):
    if distance == 0:
        return end
    dx, dy = (end[0] - start[0]) / distance, (end[1] - start[1]) / distance
    available = math.inf
    if dx > 0:
        available = min(available, (799 - end[0]) / dx)
    elif dx < 0:
        available = min(available, -end[0] / dx)
    if dy > 0:
        available = min(available, (599 - end[1]) / dy)
    elif dy < 0:
        available = min(available, -end[1] / dy)
    amount = min(distance * ratio, available)
    if amount < 1:
        return None
    return end[0] + dx * amount, end[1] + dy * amount


def plan_path(start, end, rng: random.Random, profile: Profile):
    start = _check_point(start)
    end = _check_point(end)
    if profile.target_width_px <= 0:
        raise ValueError("target width must be positive")
    distance = math.dist(start, end)
    if distance == 0:
        return [(end[0], end[1], 0)]
    a = _range_value(rng, profile.fitts_a_ms)
    b = _range_value(rng, profile.fitts_b_ms)
    duration = (a + b * math.log2(distance / profile.target_width_px + 1))
    duration *= 1 + rng.uniform(-profile.duration_jitter, profile.duration_jitter)
    total_ms = max(profile.sample_ms[0], int(round(duration)))
    durations = _durations(total_ms, rng, profile)
    count = len(durations)
    correction_start = count
    destination = end
    if distance > 150 and rng.random() < rng.uniform(*profile.overshoot_probability):
        ratio = rng.uniform(*profile.overshoot_ratio)
        overshoot = _overshoot_point(end, start, distance, ratio)
        if overshoot is not None:
            destination = overshoot
            correction_start = max(2, count - max(2, round(count * 0.2)))
            if correction_start >= count:
                correction_start = count - 1
    c1, c2 = _controls(start, destination, rng, profile)
    if correction_start < count:
        correction_c1, correction_c2 = _controls(destination, end, rng, profile)
    else:
        correction_c1 = correction_c2 = None
    points = [(start[0], start[1], 0)]
    pending = 0
    base_dx, base_dy = destination[0] - start[0], destination[1] - start[1]
    base_distance = math.hypot(base_dx, base_dy) or 1.0
    normal = (-base_dy / base_distance, base_dx / base_distance)
    noise_state = 0.0
    for index, dt in enumerate(durations, 1):
        pending += dt
        if index <= correction_start:
            t = index / correction_start
            x, y = _bezier(start, c1, c2, destination, t, profile.path_skew)
        else:
            t = (index - correction_start) / (count - correction_start)
            x, y = _bezier(destination, correction_c1, correction_c2, end, t, profile.path_skew)
        if index == count:
            x, y = end
        elif profile.noise_px:
            if getattr(profile, "correlated_noise", False):
                # 低频横向抖动：保留连续性，且在最后约四分之一段逐渐收掉。
                noise_state = noise_state * 0.72 + rng.uniform(
                    -profile.noise_px, profile.noise_px) * 0.28
                fade = min(1.0, max(0.0, (1.0 - t) / 0.28))
                x += normal[0] * noise_state * fade
                y += normal[1] * noise_state * fade
            else:
                x += rng.uniform(-profile.noise_px, profile.noise_px)
                y += rng.uniform(-profile.noise_px, profile.noise_px)
        point = _clamp(x, y)
        if index == count:
            point = end
        if point == points[-1][:2]:
            continue
        points.append((point[0], point[1], pending))
        pending = 0
    if points[-1][:2] != end:
        points.append((end[0], end[1], pending))
    elif pending:
        x, y, dt = points[-1]
        points[-1] = (x, y, dt + pending)
    return points


def plan_correction(start, end, rng: random.Random, profile: Profile):
    """看到落点有偏差后的小幅修正：近似直线、起步快收尾慢、短距离不抖动。

    plan_path 的控制点偏移（距离的 5%~20%）和每点 ±1px 噪声是为长距离设计的，
    用在几像素到几十像素的修正上会让指针来回抖、拐弯，不像人手的收尾动作。
    噪声只在距离超过 20px 后才逐渐加入，所以短修正沿着一个方向单调走完。
    """
    start = _check_point(start)
    end = _check_point(end)
    distance = math.dist(start, end)
    if distance == 0:
        return [(end[0], end[1], 0)]
    a = _range_value(rng, profile.correction_a_ms)
    b = _range_value(rng, profile.correction_b_ms)
    duration = a + b * math.log2(distance / profile.correction_target_px + 1)
    duration *= 1 + rng.uniform(-profile.duration_jitter, profile.duration_jitter)
    total_ms = max(profile.sample_ms[0], int(round(duration)))
    durations = _durations(total_ms, rng, profile, profile.correction_sample_ms)
    count = len(durations)
    c1, c2 = _controls(start, end, rng, profile, profile.correction_control_offset)
    noise = profile.noise_px * min(1.0, max(0.0, (distance - 20.0) / 60.0))
    points = [(start[0], start[1], 0)]
    pending = 0
    normal = (-(end[1] - start[1]) / distance,
              (end[0] - start[0]) / distance)
    noise_state = 0.0
    for index, dt in enumerate(durations, 1):
        pending += dt
        if index == count:
            point = end
        else:
            x, y = _bezier(start, c1, c2, end, (index / count) ** profile.correction_skew)
            if noise:
                if getattr(profile, "correlated_noise", False):
                    noise_state = noise_state * 0.72 + rng.uniform(-noise, noise) * 0.28
                    x += normal[0] * noise_state
                    y += normal[1] * noise_state
                else:
                    x += rng.uniform(-noise, noise)
                    y += rng.uniform(-noise, noise)
            point = _clamp(x, y)
        if point == points[-1][:2]:
            continue
        points.append((point[0], point[1], pending))
        pending = 0
    if points[-1][:2] != end:
        points.append((end[0], end[1], pending))
    elif pending:
        x, y, dt = points[-1]
        points[-1] = (x, y, dt + pending)
    return points


def click_point(center, rng: random.Random, box=None):
    x, y = _check_point(center)
    if box is None:
        return _clamp(x + rng.randint(-1, 1), y + rng.randint(-1, 1))
    width, height = box
    if width <= 0 or height <= 0:
        raise ValueError("box dimensions must be positive")
    xmin, xmax = max(0.0, x - width / 2), min(799.0, x + width / 2)
    ymin, ymax = max(0.0, y - height / 2), min(599.0, y + height / 2)
    while True:
        px = rng.gauss(x, width / 6)
        py = rng.gauss(y, height / 6)
        if xmin <= px <= xmax and ymin <= py <= ymax:
            return _clamp(px, py)


def think(rng: random.Random, kind):
    return rng.randint(*_THINK_RANGES.get(kind, _THINK_RANGES["default"]))


def plan_combo(modifiers, key, rng: random.Random, profile: Profile):
    if isinstance(modifiers, str):
        modifiers = (modifiers,)
    else:
        modifiers = tuple(modifiers)
    if not modifiers:
        raise ValueError("at least one modifier is required")
    events = [("down", modifier, 0) for modifier in modifiers]
    events.append(("down", key, rng.randint(*profile.modifier_delay_ms)))
    events.append(("up", key, rng.randint(*profile.key_hold_ms)))
    release_delay = rng.randint(*profile.modifier_release_delay_ms)
    for index, modifier in enumerate(reversed(modifiers)):
        events.append(("up", modifier, release_delay if index == 0 else 0))
    return events
