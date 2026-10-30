"""ViewportGuard：证明"这个窗口里就是 800x600、1:1、原点 (0,0) 的游戏"。

现行 L0（第一层 v1.6，查看器按 -scale=100 标准参数启动）：查看器未最小化，且画面子窗口客户区 == 800x600
物理像素，与主机缩放率无关，见 run_l0_fit。

下面的 evaluate_l0 / sample_bands / run_l0 / detect_share_mode 是已被取代的旧方案（客户区 816x616 + 右/下
16px 纯色边带），只剩旧测试台 bench 在用，v1.6 起不再适用，调用时 scale 恒为 1.0。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from common import winapi
from control.viewport import Viewport


@dataclass
class GuardResult:
    level: str
    ok: bool
    detail: str
    ms: int = 0
    data: dict = field(default_factory=dict)
    skipped: bool = False

    def __str__(self) -> str:
        tag = "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")
        return f"[{self.level}] {tag} {self.detail} ({self.ms}ms)"


def evaluate_l0(client: tuple[int, int], expect_client: tuple[int, int], iconic: bool,
                right_band: list[str], bottom_band: list[str], bg: str | None = None) -> GuardResult:
    """纯逻辑：根据已采集的数据判定 L0，便于单元测试。"""
    if iconic:
        return GuardResult("L0", False, "查看器处于最小化")
    if client != expect_client:
        return GuardResult("L0", False, f"子窗口客户区 {client[0]}x{client[1]}，期望 {expect_client[0]}x{expect_client[1]}")
    bands = right_band + bottom_band
    if not bands or any(not c for c in bands):
        return GuardResult("L0", False, "边带取色失败")
    colors = set(c.lower() for c in bands)
    if len(colors) != 1:
        return GuardResult("L0", False, f"边带不是纯色（{len(colors)} 种颜色）：帧缓冲可能大于 800x600 或出现了滚动条",
                           data={"colors": sorted(colors)[:6]})
    only = colors.pop()
    if bg is not None and only != bg.lower():
        return GuardResult("L0", False, f"边带颜色 {only} 不是已知背景色 {bg}", data={"band": only})
    return GuardResult("L0", True, f"客户区 {client[0]}x{client[1]}，边带纯色 {only}", data={"band": only})


def sample_bands(channel, game_size: tuple[int, int], client: tuple[int, int],
                 scale: float = 1.0) -> tuple[list[str], list[str]]:
    gw, gh = round(game_size[0] * scale), round(game_size[1] * scale)
    cw, ch = client
    offs = [round(o * scale) for o in (2, 8, 14)]
    step = round(60 * scale)
    right_x = [min(cw - 1, gw + off) for off in offs]
    bottom_y = [min(ch - 1, gh + off) for off in offs]
    right = [channel.get_color(x, y) for x in right_x for y in range(0, min(ch, gh + 1), step)]
    bottom = [channel.get_color(x, y) for y in bottom_y for x in range(0, min(cw, gw + 1), step)]
    return right, bottom


def run_l0(channel, handles, expect_client: tuple[int, int], game_size: tuple[int, int],
           bg: str | None = None, scale: float = 1.0) -> GuardResult:
    t0 = time.perf_counter()
    iconic = winapi.is_iconic(handles.top)
    client = winapi.client_size(handles.child)
    if iconic or client != expect_client:
        res = evaluate_l0(client, expect_client, iconic, [], [], bg)
    else:
        right, bottom = sample_bands(channel, game_size, client, scale)
        res = evaluate_l0(client, expect_client, iconic, right, bottom, bg)
    res.ms = int((time.perf_counter() - t0) * 1000)
    return res


def run_anchors(channel, vp: Viewport, anchors: list[dict], tol_px: int = 1, search_px: int = 40) -> GuardResult:
    """L1/L2：每个锚点在预期 G 位置附近找图，偏差必须 <= tol_px。无锚点配置时跳过。"""
    t0 = time.perf_counter()
    if not anchors:
        return GuardResult("L2", True, "未配置锚点，跳过", skipped=True)
    worst = 0
    for a in anchors:
        ex, ey = vp.g_to_capture(a["gx"], a["gy"])
        hit = channel.find_pic(ex - search_px, ey - search_px, ex + search_px + a.get("w", 0),
                               ey + search_px + a.get("h", 0), a["template"],
                               a.get("delta", "202020"), a.get("sim", 0.9))
        if hit is None:
            return GuardResult("L1", False, f"锚点 {a['template']} 未找到（游戏未在场或位置偏离超过 {search_px}px）",
                               int((time.perf_counter() - t0) * 1000))
        dev = max(abs(hit[0] - ex), abs(hit[1] - ey))
        worst = max(worst, dev)
        if dev > tol_px:
            return GuardResult("L2", False, f"锚点 {a['template']} 偏移 {dev}px（容差 {tol_px}px），origin 或缩放有问题",
                               int((time.perf_counter() - t0) * 1000), data={"dev": dev})
    return GuardResult("L2", True, f"{len(anchors)} 个锚点最大偏差 {worst}px",
                       int((time.perf_counter() - t0) * 1000), data={"dev": worst})


def grid_signature(channel, region: tuple[int, int, int, int], step: int = 20) -> tuple[str, ...]:
    x1, y1, x2, y2 = region
    return tuple(channel.get_color(x, y) for y in range(y1, y2, step) for x in range(x1, x2, step))


def run_l4(channel, region: tuple[int, int, int, int], wait_s: float = 3.0,
           sleep=time.sleep) -> GuardResult:
    """画面新鲜度：同一区域隔 wait_s 秒两次取样，应有变化（时钟、光标闪烁、小地图等）。

    静止画面会得到 FAIL，所以区域要选游戏里会动的地方；失败只说明"这段时间没变"，
    由上层结合是否有断线提示判断是否真的卡死。
    """
    t0 = time.perf_counter()
    a = grid_signature(channel, region)
    sleep(wait_s)
    b = grid_signature(channel, region)
    changed = sum(1 for x, y in zip(a, b) if x != y)
    ok = changed > 0
    return GuardResult("L4", ok, f"{wait_s:.0f}s 内 {changed}/{len(a)} 个取样点变化",
                       int((time.perf_counter() - t0) * 1000), data={"changed": changed, "total": len(a)})


@dataclass
class ModeResult:
    mode: str          # "shared"：副机只共享游戏区域；"desktop"：副机共享整桌面（备用）
    l0: GuardResult
    reason: str


def run_l0_fit(handles, expect_client: tuple[int, int]) -> GuardResult:
    """日常 L0：查看器没最小化，且画面子窗口客户区 == expect_client（现行标准是 800x600）。"""
    t0 = time.perf_counter()
    iconic = winapi.is_iconic(handles.top)
    client = winapi.client_size(handles.child)
    if iconic:
        res = GuardResult("L0", False, "查看器处于最小化")
    elif client != expect_client:
        res = GuardResult("L0", False, f"子窗口客户区 {client[0]}x{client[1]}，期望 {expect_client[0]}x{expect_client[1]}")
    else:
        res = GuardResult("L0", True, f"客户区 {client[0]}x{client[1]} 与游戏大小一致")
    res.ms = int((time.perf_counter() - t0) * 1000)
    return res


def detect_share_mode(channel, handles, target_client: tuple[int, int], game_size: tuple[int, int],
                      fit, sleep=time.sleep, clock=time.monotonic,
                      timeout_s: float = 10.0, poll_s: float = 1.0, scale: float = 1.0) -> ModeResult:
    """旧方案：把查看器调成 target_client（800x600 + 16px 边带）后轮询 L0：通过说明副机共享的就是 800x600 游戏区域。

    画面从副机传到查看器有 0.4~1.2s 延迟，所以要轮询到超时才能判定失败。
    失败时把窗口尺寸还原，让后续整桌面模式继续可用。
    """
    before = winapi.client_size(handles.child)
    fit(handles, target_client)
    deadline = clock() + timeout_s
    res = run_l0(channel, handles, target_client, game_size, None, scale)
    while not res.ok and clock() < deadline:
        sleep(poll_s)
        res = run_l0(channel, handles, target_client, game_size, None, scale)
    if res.ok:
        return ModeResult("shared", res, "副机已只共享游戏区域，画面坐标就是游戏坐标")
    if before[0] > 0 and before[1] > 0 and before != winapi.client_size(handles.child):
        fit(handles, before)
    return ModeResult("desktop", res, f"副机仍在共享整桌面（{res.detail}）。请确认副机上 game_share 在运行且游戏已打开")