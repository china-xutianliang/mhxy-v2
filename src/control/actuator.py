from __future__ import annotations

import math
import random
import time
import zlib
from contextlib import contextmanager
from typing import Callable

from common import estop
from common.log import log
from control.crystal import CURSOR_SEARCH_HALF, GEM_TO_TIP, find_cursor
from control.cursor import CursorTracker
from control.humanize import Profile, click_point, plan_correction, plan_path, think
from control.viewport import Viewport

MODIFIERS = ("alt", "ctrl", "shift", "win")


class Actuator:
    # 箭尖相对发送点的预期偏移。登录页 1:1 真机九点实测（2026-10-29，runtime/v16_out.jsonl V16_E1E2）箭尖就在
    # 发送点上；旧的 (10,11) 是 2× 时代的数值，v1.6 起作废。游戏内光标是游戏自己画的，首发落点会差几十像素，
    # 这个预期值不再起作用，由 _align_arrow 的闭环修正。
    ARROW_OFFSET = (0, 0)
    # 容差和最长时间的默认值；实例上的 align_tolerance / align_max_s 才是实际生效的（界面可调）。
    ALIGN_DEADBAND = 3
    ALIGN_MAX_S = 30.0
    # 只防写错导致的死循环，正常由 align_max_s 收口。
    ALIGN_HARD_CHECKS = 80
    # 真机（2026-10-29）：首发落点偏差可达 ±60px，一般 2~3 次修正收敛；画面刷新时快时慢，
    # 中转链路（往返 0.5~0.9 s）上新位置要隔一秒以上才出现，读到旧画面时要继续等。
    ALIGN_MIN_WAIT_S = 0.6
    ALIGN_POLL_S = 0.06
    ALIGN_STABLE_POLLS = 4
    ALIGN_TIMEOUT_S = 5.0
    ALIGN_STALE_RETRIES = 1
    ALIGN_LOST_RETRIES = 3
    ALIGN_WAIT_GROW_S = 0.6
    ALIGN_WAIT_MAX_S = 3.0
    # 指令位移小于它时分不清“没动”和抖动，不判旧画面。
    ALIGN_FRESH_MIN_MOVE = 4
    # 单次修正的最大幅度，防止认错光标时一步跳很远。
    ALIGN_STEP_CAP = 90
    # 实测画面延迟很小（局域网）时缩短“停下来看一眼”的等待，节奏更像人；延迟变大或读到旧画面就恢复保守值。
    ALIGN_FAST_LAG_S = 0.25
    ALIGN_MIN_WAIT_FAST_S = 0.3
    ALIGN_STABLE_POLLS_FAST = 3
    # 本项目只连接局域网副机：先用快速档开始观察，但仍保留旧画面保护。
    LAN_ALIGN_MIN_WAIT_S = 0.12
    LAN_ALIGN_POLL_S = 0.016
    LAN_ALIGN_STABLE_POLLS = 2
    LAN_ALIGN_FAST_LAG_S = 0.12
    LAN_ALIGN_MIN_WAIT_FAST_S = 0.05
    # 位移小于它时看不出“新位置第一次出现”，不用来估计延迟。
    ALIGN_LAG_MIN_MOVE = 12

    def __init__(self, channel, viewport: Viewport, latency_s: float = 0.8,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None,
                 cursor: CursorTracker | None = None,
                 profile: Profile | None = None, slave: str = "-",
                 capture: Callable[[], tuple[bytes, int, int] | None] | None = None,
                 visual_align: bool = False, lan_mode: bool = False):
        self.ch = channel
        self.vp = viewport
        self.latency_s = latency_s
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self.lan_mode = bool(lan_mode)
        self.profile = profile or (Profile.lan() if self.lan_mode else Profile())
        self._cursor = cursor or CursorTracker(clock=clock)
        self._capture = capture
        self.visual_align = visual_align
        self.align_tolerance = self.ALIGN_DEADBAND
        self.align_max_s = self.ALIGN_MAX_S
        self.last_align_ok: bool | None = None
        self._display_lag_s: float | None = None
        self._lag_samples = 0
        self._seen_s: float | None = None
        self.slave = slave
        self.last_path: list[tuple[int, int, int]] = []
        self.last_start_source = "unknown"
        self.last_click: tuple[int, int] | None = None
        self.last_align: tuple[int, int] | None = None
        self.last_align_error: tuple[int, int] | None = None
        self.last_align_path: list[tuple[int, int, int]] = []
        self.last_send: tuple[int, int] | None = None
        self.last_gem: tuple[int, int, int] | None = None
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()

    @contextmanager
    def _protected(self):
        estop.check()
        validate = getattr(self.ch, "assert_target", None)
        if validate:
            validate()
        try:
            yield
        except BaseException:
            self.release_all()
            raise

    def _wait(self, lo_ms: int, hi_ms: int) -> None:
        estop.check()
        self._sleep(self._rng.uniform(lo_ms, hi_ms) / 1000.0)
        estop.check()

    def _traverse(self, gx: int, gy: int, speed: float = 1.0, detect: bool = True,
                  *, align: bool = False, correction: bool = False) -> None:
        self.vp.check_g(gx, gy)
        if speed <= 0:
            raise ValueError("速度倍率必须大于 0")
        start = self._cursor.start_for(gx, gy) if detect else self._cursor.cursor.pos
        if start is None:
            start = self._cursor.start_for(gx, gy)
        source = self._cursor.cursor.source
        if not align:
            self.last_start_source = source
        width, height = self.vp.game_size
        inside = (min(width - 1, max(0, start[0])), min(height - 1, max(0, start[1])))
        if start != inside:
            self.ch.move_to(*self.vp.g_to_v(*start))
            log(f"指针位置未知，从画面外 {start} 进入", slave=self.slave)
        planner = plan_correction if correction else plan_path
        path = planner(inside, (gx, gy), self._rng, self.profile)
        if align:
            self.last_align_path = path
        else:
            self.last_path = path
        started = self._clock()
        for x, y, dt_ms in path:
            estop.check()
            self.ch.move_to(*self.vp.g_to_v(x, y))
            if dt_ms:
                self._sleep(dt_ms / 1000.0 / speed)
        self._cursor.mark_moved(gx, gy)
        self.last_send = (gx, gy)
        elapsed_ms = round((self._clock() - started) * 1000)
        if align:
            log(f"箭头修正到 G=({gx},{gy}) 轨迹={len(path)}点 {elapsed_ms}ms", slave=self.slave)
        else:
            log(f"移动到 G=({gx},{gy}) 起点={source} 轨迹={len(path)}点 {elapsed_ms}ms",
                slave=self.slave)

    def _read_cursor(self, center: tuple[int, int], last_crc: int | None
                     ) -> tuple[tuple[int, int, int] | None, int | None, bool]:
        """抓一帧找水晶光标。返回 (宝石, 帧校验值, 本次是否真的抓到了画面)。

        画面和上次完全相同时不再重复识别，调用方沿用上次结果。
        """
        if self._capture is None:
            return None, last_crc, False
        try:
            capture = self._capture()
        except Exception as exc:
            log(f"箭头对齐：抓图失败 {exc}", slave=self.slave)
            return None, last_crc, False
        if not capture:
            return None, last_crc, False
        rgb, width, height = capture
        crc = zlib.crc32(rgb)
        if crc == last_crc:
            return self.last_gem, crc, True
        found = find_cursor(rgb, width, height, center, half=CURSOR_SEARCH_HALF)
        self.last_gem = found
        return found, crc, True

    def _is_stale(self, found: tuple[int, int, int], before: tuple[int, int, int] | None,
                  cmd: tuple[int, int] | None, first: bool) -> bool:
        """读数还是发送前的光标位置：指针明明挪过了，画面却没跟上（旧画面或残影）。

        第一次发送的位移大，用 12px 门槛；修正的位移小，用 ALIGN_FRESH_MIN_MOVE。
        游戏光标落点本身不规则（历史相关），所以只判“没动”，不判“动得对不对”。
        """
        if before is None or cmd is None:
            return False
        size = max(abs(cmd[0]), abs(cmd[1]))
        shift = max(abs(found[0] - before[0]), abs(found[1] - before[1]))
        if first:
            return size > 12 and shift <= 3
        return size >= self.ALIGN_FRESH_MIN_MOVE and shift <= 2

    def _settle_cursor(self, sent: tuple[int, int], before: tuple[int, int, int] | None,
                       cmd: tuple[int, int] | None, first: bool, min_wait: float,
                       timeout: float, stable_polls: int | None = None
                       ) -> tuple[tuple[int, int, int] | None, str]:
        """发送后等水晶光标画面稳定，返回 (宝石位置, 状态)。状态：ok / stale / lost。

        远程画面有延迟：副机刷新是轮询的，光标的新位置要隔几百毫秒甚至几秒才出现，
        期间画面里还留着旧位置的光标（残影）。所以只在发送满 min_wait 之后、
        同一个位置连续 ALIGN_STABLE_POLLS 次认出才采信；指针明显移动过（cmd）
        读数却没离开发送前的位置（before）就当成旧画面继续等，超时仍如此返回 stale。
        一直没认出光标返回 lost。
        稳定位置第一次被看到的时刻（≈画面延迟）记在 _seen_s，没采信时为 None。
        """
        need = stable_polls or self.ALIGN_STABLE_POLLS
        center = (sent[0] + GEM_TO_TIP[0], sent[1] + GEM_TO_TIP[1])
        started = self._clock()
        streak: list[tuple[int, int, int]] = []
        since = 0.0
        last_crc: int | None = None
        self._seen_s = None
        while True:
            estop.check()
            found, last_crc, grabbed = self._read_cursor(center, last_crc)
            elapsed = self._clock() - started
            if grabbed:
                if found is None:
                    streak = []
                elif not streak or max(abs(found[0] - streak[-1][0]),
                                       abs(found[1] - streak[-1][1])) > 1:
                    streak = [found]
                    since = elapsed
                else:
                    streak.append(found)
            stale = bool(streak) and self._is_stale(streak[-1], before, cmd, first)
            if elapsed >= min_wait and len(streak) >= need and not stale:
                self._seen_s = since
                return streak[-1], "ok"
            if elapsed >= timeout:
                if stale:
                    return None, "stale"
                return (streak[-1], "ok") if streak else (None, "lost")
            self._sleep(self.LAN_ALIGN_POLL_S if self.lan_mode else self.ALIGN_POLL_S)

    def _near_clip_edge(self, tx: int, ty: int, width: int, height: int) -> bool:
        """目标靠右/靠下时，游戏光标的落点偏差（最大约 +68/+55）加上宝石在箭尖右下 (13,14)，
        宝石可能被画面边缘截掉，识别不到；靠左/靠上箭尖会被夹在 0，宝石仍在画面内。"""
        return tx > width - 100 or ty > height - 80

    def _initial_approach(self, start: tuple[int, int], target: tuple[int, int]
                          ) -> tuple[int, int]:
        """LAN 首次移动只落到目标附近，后续交给视觉闭环逐轮收近。"""
        low, high = self.profile.initial_approach_gap_px
        if high <= 0 or high < low:
            return target
        sx, sy = start
        tx, ty = target
        dx, dy = tx - sx, ty - sy
        distance = math.hypot(dx, dy)
        if distance <= 1:
            return target
        gap = min(self._rng.uniform(low, high), distance * 0.35)
        if gap < 1:
            return target
        x = tx - dx / distance * gap
        y = ty - dy / distance * gap
        width, height = self.vp.game_size
        return (min(width - 1, max(0, int(round(x)))),
                min(height - 1, max(0, int(round(y)))))

    def _recovery_seed(self, tx: int, ty: int, attempt: int, width: int, height: int
                       ) -> tuple[int, int]:
        """认不出光标时先把指针移到向画面内收的位置再找，找到后再从里面慢慢修正回目标。

        每次重试收得更深；加 1~3px 随机量，免得连续落在同一个像素上。
        """
        margin = 40 + 50 * attempt
        x = min(tx, width - 1 - margin) - self._rng.randint(1, 3)
        y = min(ty, height - 1 - margin) - self._rng.randint(1, 3)
        return max(0, x), max(0, y)

    def _wait_plan(self) -> tuple[float, int]:
        """发送后至少等多久、同一位置连续认出几次才采信。

        量过的画面延迟很小（局域网）就用快速档，停顿短，像人扫一眼就动手；
        还没量过、延迟大、或刚读到过旧画面，用保守档。
        """
        lag = self._display_lag_s
        if self.lan_mode:
            if lag is None:
                return self.LAN_ALIGN_MIN_WAIT_S, self.LAN_ALIGN_STABLE_POLLS
            if lag <= self.LAN_ALIGN_FAST_LAG_S:
                return max(self.LAN_ALIGN_MIN_WAIT_FAST_S, lag + 0.035), self.LAN_ALIGN_STABLE_POLLS
        if lag is not None and lag <= self.ALIGN_FAST_LAG_S and self._lag_samples >= 2:
            return max(self.ALIGN_MIN_WAIT_FAST_S, lag + 0.2), self.ALIGN_STABLE_POLLS_FAST
        return self.ALIGN_MIN_WAIT_S, self.ALIGN_STABLE_POLLS

    def _lag_text(self) -> str:
        return "" if self._display_lag_s is None else f" 画面延迟≈{self._display_lag_s:.2f}s"

    def _note_lag(self, seen: float | None, cmd: tuple[int, int] | None,
                  before: tuple[int, int, int] | None, found: tuple[int, int, int]) -> None:
        """用“新位置第一次被看到”的时刻更新画面延迟估计；变慢时立刻跟上，变快时平滑。

        只在指针明显动过、且读数确实离开了发送前的位置时采样：读数没动可能是旧画面，
        那种“从一开始就稳定”的读数不能算成延迟为 0。
        """
        if seen is None or cmd is None or before is None:
            return
        if max(abs(cmd[0]), abs(cmd[1])) < self.ALIGN_LAG_MIN_MOVE:
            return
        if max(abs(found[0] - before[0]), abs(found[1] - before[1])) <= 3:
            return
        lag = self._display_lag_s
        self._display_lag_s = seen if lag is None or seen > lag else 0.5 * lag + 0.5 * seen
        self._lag_samples += 1

    def _align_arrow(self, tx: int, ty: int, *, speed: float = 1.0) -> None:
        """先把水晶箭头尖对到目标，再执行后续动作；宝石只用来定位水晶。

        游戏里的光标是游戏自己画的，落点和发送点会差几十像素且随位置、移动历史变化，
        所以闭环：发送 → 等光标画面稳定（见 _settle_cursor）→ 算箭尖和目标的偏差 →
        把指针往反方向挪同样的量，反复直到偏差进入 align_tolerance，或用满 align_max_s。
        最近三次偏差没有缩小就把步子减半，防止画面延迟造成来回震荡。
        第一次就认不出水晶（登录页、普通按钮、光标变了形态）时退回原目标点，只有目标靠右/靠下
        （宝石可能被画面边缘截掉）才先向画面内收着找；
        中途认不出时先把指针移到向内收的位置再找，几次都找不到就退回目标点，
        没动过位置只是多等几次的则保持当前指针；
        指针挪了画面里的光标却始终不动（画面卡住或认错了物体）时清空结果并退回目标点；
        到画面边缘时按当前指针继续，都不阻断动作，结果记在 last_align_ok。
        """
        self.last_align = None
        self.last_align_error = None
        self.last_align_path = []
        self.last_align_ok = None
        if not self.visual_align or self._capture is None:
            self._traverse(tx, ty, speed=speed)
            return
        width, height = self.vp.game_size
        sx = min(width - 1, max(0, tx - self.ARROW_OFFSET[0]))
        sy = min(height - 1, max(0, ty - self.ARROW_OFFSET[1]))
        tolerance = max(1, int(self.align_tolerance))
        started = self._clock()
        deadline = started + max(1.0, float(self.align_max_s))
        previous = self._cursor.cursor.pos
        anchor = previous if previous is not None else (tx, ty)
        before, _crc, _grabbed = self._read_cursor(
            (anchor[0] + GEM_TO_TIP[0], anchor[1] + GEM_TO_TIP[1]), None)
        initial_target = (sx, sy)
        if self.lan_mode:
            approach_start = previous or (sx, sy)
            initial_target = self._initial_approach(approach_start, (sx, sy))
        self._traverse(initial_target[0], initial_target[1], speed=speed)
        position = self._cursor.cursor.pos
        cmd = (None if previous is None or position is None
               else (position[0] - previous[0], position[1] - previous[1]))
        corrections = 0
        lost = stale = 0
        reseeded = False
        grown = 0.0
        norms: list[int] = []
        for _ in range(self.ALIGN_HARD_CHECKS):
            estop.check()
            position = self._cursor.cursor.pos
            timeout = min(self.ALIGN_TIMEOUT_S, max(1.0, deadline - self._clock()))
            plan_wait, stable = self._wait_plan()
            min_wait = max(plan_wait, grown)
            found, status = self._settle_cursor(position or (sx, sy), before, cmd,
                                                corrections == 0, min_wait, timeout, stable)
            if status == "lost":
                lost += 1
                can_try = lost <= self.ALIGN_LOST_RETRIES and self._clock() < deadline
                seed = None
                if can_try and (corrections or self._near_clip_edge(tx, ty, width, height)):
                    seed = self._recovery_seed(tx, ty, lost, width, height)
                    if position is not None and max(abs(seed[0] - position[0]),
                                                    abs(seed[1] - position[1])) < 8:
                        seed = None
                if seed is not None:
                    log(f"箭头对齐：认不出水晶，先移到 {seed} 再找", slave=self.slave)
                    self._traverse(seed[0], seed[1], speed=speed, detect=False, align=True)
                    reseeded = True
                    before = cmd = None
                    norms = []
                    continue
                if can_try and corrections:
                    continue
                if corrections == 0 or reseeded:
                    if position != (tx, ty):
                        self._traverse(tx, ty, speed=speed, detect=False)
                    log("箭头对齐：未识别到水晶，按目标点执行", slave=self.slave)
                else:
                    self.last_align_ok = False
                    log(f"箭头对齐：第 {corrections} 次修正后认不出水晶，保持当前指针", slave=self.slave)
                return
            if status == "stale":
                stale += 1
                grown = min(self.ALIGN_WAIT_MAX_S, min_wait + self.ALIGN_WAIT_GROW_S)
                self._display_lag_s = None
                self._lag_samples = 0
                if stale <= self.ALIGN_STALE_RETRIES and self._clock() < deadline:
                    continue
                self.last_align = None
                self.last_align_error = None
                self.last_align_ok = False
                if position != (tx, ty):
                    self._traverse(tx, ty, speed=speed, detect=False)
                log("箭头对齐：指针挪了画面里的光标却没动，回退目标点", slave=self.slave)
                return
            lost = stale = 0
            self._note_lag(self._seen_s, cmd, before, found)
            gx, gy, area = found
            ax, ay = gx - GEM_TO_TIP[0], gy - GEM_TO_TIP[1]
            dx, dy = ax - tx, ay - ty
            self.last_align = (ax, ay)
            self.last_align_error = (dx, dy)
            norm = max(abs(dx), abs(dy))
            norms.append(norm)
            if norm <= tolerance:
                self.last_align_ok = True
                # 只有真正经历过闭环修正才加收尾停顿；首发准确时保持原有节奏。
                if corrections and any(self.profile.terminal_hold_ms):
                    self._wait(*self.profile.terminal_hold_ms)
                log(f"箭头对齐完成：箭尖=({ax},{ay}) 目标=({tx},{ty}) "
                    f"偏差=({dx:+d},{dy:+d}) 指针={position} 宝石面积={area} 修正={corrections}次 "
                    f"用时={self._clock() - started:.1f}s{self._lag_text()}", slave=self.slave)
                return
            if self._clock() >= deadline or corrections >= self.ALIGN_HARD_CHECKS - 1:
                self.last_align_ok = False
                log(f"箭头对齐停止：{corrections} 次修正、{self._clock() - started:.0f}s 后"
                    f"偏差=({dx:+d},{dy:+d})px，超出容差 ±{tolerance}", slave=self.slave)
                return
            if position is None:
                return
            cap = self.ALIGN_STEP_CAP
            # 靠近目标时故意留一点误差，让下一次画面确认承担最后一小段收尾。
            # 这比每次都把观测误差完整反向发送更接近手眼操作，也不会牺牲最终容差。
            terminal = norm <= max(tolerance + 1, self.profile.terminal_radius_px)
            gain_range = self.profile.terminal_gain if terminal else self.profile.correction_gain
            gain = (self._rng.uniform(*gain_range)
                    if gain_range != (1.0, 1.0) else 1.0)
            step_x = int(round(dx * gain))
            step_y = int(round(dy * gain))
            if terminal and gain < 1.0:
                # 每个非零分量至少保留 1px，避免最后一段又退化成全量修正。
                if dx and abs(step_x) >= abs(dx):
                    step_x = int(math.copysign(max(0, abs(dx) - 1), dx))
                if dy and abs(step_y) >= abs(dy):
                    step_y = int(math.copysign(max(0, abs(dy) - 1), dy))
            step_x = max(-cap, min(cap, step_x))
            step_y = max(-cap, min(cap, step_y))
            if len(norms) >= 3 and norm >= norms[-3] * 0.9:
                step_x, step_y = int(step_x / 2), int(step_y / 2)
                if step_x == 0 and step_y == 0:
                    step_x, step_y = (1 if dx > 0 else -1 if dx < 0 else 0,
                                      1 if dy > 0 else -1 if dy < 0 else 0)
            nx = min(width - 1, max(0, position[0] - step_x))
            ny = min(height - 1, max(0, position[1] - step_y))
            if (nx, ny) == position:
                self.last_align_ok = False
                log(f"箭头对齐停止：已到画面边缘，偏差=({dx:+d},{dy:+d})px", slave=self.slave)
                return
            before, cmd = found, (nx - position[0], ny - position[1])
            corrections += 1
            reaction = self.profile.correction_reaction_ms or self.profile.reaction_ms
            self._wait(*reaction)
            self._traverse(nx, ny, speed=speed, detect=False, align=True, correction=True)

    def move_to(self, gx: int, gy: int, *, speed: float = 1.0) -> None:
        self.vp.check_g(gx, gy)
        with self._protected():
            self._align_arrow(gx, gy, speed=speed)

    def _down_up(self, button: str) -> None:
        self.ch.down(button)
        self._pressed_buttons.add(button)
        try:
            self._wait(*self.profile.hold_ms)
        finally:
            self.ch.up(button)
            self._pressed_buttons.discard(button)

    def click(self, gx: int, gy: int, button: str = "left", *, box=None, speed: float = 1.0) -> None:
        if button not in ("left", "right"):
            raise ValueError("鼠标键只支持 left/right")
        self.vp.check_g(gx, gy)
        with self._protected():
            point = click_point((gx, gy), self._rng, box)
            self.last_click = point
            self._align_arrow(*point, speed=speed)
            self._wait(*self.profile.pre_press_ms)
            self._down_up(button)
            if self.visual_align and self._capture is not None:
                if self.last_align is None:
                    log(f"{button} 点击 G={point} 箭头未对齐，按目标点点击", slave=self.slave)
                else:
                    log(f"{button} 点击 G={point} 箭尖={self.last_align} 指针={self.cursor()}",
                        slave=self.slave)
            else:
                log(f"{button} 点击 G={point}", slave=self.slave)

    def double_click(self, gx: int, gy: int, *, box=None, speed: float = 1.0) -> None:
        self.vp.check_g(gx, gy)
        with self._protected():
            point = click_point((gx, gy), self._rng, box)
            self.last_click = point
            self._align_arrow(*point, speed=speed)
            self._wait(*self.profile.pre_press_ms)
            self._down_up("left")
            self._wait(*self.profile.double_interval_ms)
            self._down_up("left")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        if button not in ("left", "right"):
            raise ValueError("鼠标键只支持 left/right")
        self.vp.check_g(x1, y1)
        self.vp.check_g(x2, y2)
        with self._protected():
            self._align_arrow(x1, y1)
            self._wait(*self.profile.pre_press_ms)
            self.ch.down(button)
            self._pressed_buttons.add(button)
            try:
                end = (x2, y2)
                position = self._cursor.cursor.pos
                if self.visual_align and self.last_align is not None and position is not None:
                    offset = (self.last_align[0] - position[0], self.last_align[1] - position[1])
                    width, height = self.vp.game_size
                    end = (min(width - 1, max(0, x2 - offset[0])),
                           min(height - 1, max(0, y2 - offset[1])))
                self._traverse(*end, speed=0.65, detect=False)
                self._wait(*self.profile.pre_press_ms)
            finally:
                self.ch.up(button)
                self._pressed_buttons.discard(button)

    def hover(self, gx: int, gy: int, dwell_ms=(300, 800), *, speed: float = 1.0) -> None:
        self.vp.check_g(gx, gy)
        with self._protected():
            self._align_arrow(gx, gy, speed=speed)
            self._wait(*dwell_ms)

    def wheel(self, gx: int, gy: int, notches: int, *, speed: float = 1.0) -> None:
        if not notches:
            return
        self.vp.check_g(gx, gy)
        with self._protected():
            self._align_arrow(gx, gy, speed=speed)
            for _ in range(abs(notches)):
                estop.check()
                self.ch.wheel(1 if notches > 0 else -1)
                self._wait(*self.profile.wheel_ms)

    def key_press(self, key: str, hold_ms: int | None = None) -> None:
        if not key:
            raise ValueError("按键不能为空")
        key = key.lower()
        with self._protected():
            self.ch.key_down(key)
            self._pressed_keys.add(key)
            try:
                if hold_ms is None:
                    self._wait(*self.profile.key_hold_ms)
                else:
                    self._sleep(hold_ms / 1000.0)
                    estop.check()
            finally:
                self.ch.key_up(key)
                self._pressed_keys.discard(key)

    def key_combo(self, *keys: str) -> None:
        if len(keys) < 2 or any(not key for key in keys):
            raise ValueError("组合键至少包含一个修饰键和一个主键")
        keys = tuple(key.lower() for key in keys)
        with self._protected():
            pressed = []
            try:
                for key in keys[:-1]:
                    estop.check()
                    self.ch.key_down(key)
                    self._pressed_keys.add(key)
                    pressed.append(key)
                    self._wait(*self.profile.modifier_delay_ms)
                main = keys[-1]
                self.ch.key_down(main)
                self._pressed_keys.add(main)
                pressed.append(main)
                self._wait(*self.profile.key_hold_ms)
                self.ch.key_up(main)
                self._pressed_keys.discard(main)
                pressed.pop()
                self._wait(*self.profile.modifier_release_delay_ms)
            finally:
                for key in reversed(pressed):
                    try:
                        self.ch.key_up(key)
                    finally:
                        self._pressed_keys.discard(key)

    def type_text(self, text: str) -> None:
        if any(ord(char) > 127 for char in text):
            raise ValueError("后台文字输入只支持 ASCII")
        with self._protected():
            for char in text:
                estop.check()
                self.ch.press(char)
                self._sleep(think(self._rng, "text") / 1000.0)

    def release_all(self) -> None:
        keys = list(MODIFIERS) + sorted(self._pressed_keys - set(MODIFIERS))
        for key in keys:
            try:
                self.ch.key_up(key)
            except Exception:
                pass
        self._pressed_keys.clear()
        for button in ("left", "right"):
            try:
                self.ch.up(button)
            except Exception:
                pass
        self._pressed_buttons.clear()
        self._cursor.invalidate()

    def release_modifiers(self) -> None:
        self.release_all()

    def cursor(self) -> tuple[int, int] | None:
        return self._cursor.cursor.pos

    def default_timeout(self) -> float:
        return max(2.0, 3.0 * self.latency_s)

    def default_poll(self) -> float:
        return min(0.5, max(0.2, self.latency_s / 3))

    def wait_until(self, done: Callable[[], bool], timeout_s: float, poll_s: float | None = None) -> bool:
        poll = poll_s if poll_s is not None else self.default_poll()
        deadline = self._clock() + timeout_s
        while True:
            estop.check()
            if done():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(poll)

    def act_until(self, action: Callable[[], None], done: Callable[[], bool],
                  timeout_s: float | None = None, retries: int = 2,
                  poll_s: float | None = None) -> bool:
        timeout = timeout_s if timeout_s is not None else self.default_timeout()
        for _ in range(retries + 1):
            action()
            if self.wait_until(done, timeout, poll_s):
                return True
        return False
