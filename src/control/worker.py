from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError

from common import estop, wincap
from common.config import runtime_dir
from control.actuator import Actuator
from control.channel import Channel
from control.cursor import CursorTracker
from control.dm_factory import make_dm
from control.viewport import Viewport

ACTIONS = {
    "move_to", "click", "double_click", "drag", "hover", "wheel",
    "key_press", "key_combo", "type_text", "release_all", "act_until",
}
# 开了箭头对齐时这些动作会反复修正，最长可达 align_max_s，等待上限要相应放宽。
ALIGN_ACTIONS = {"move_to", "click", "double_click", "drag", "hover", "wheel"}


class WorkerError(RuntimeError):
    pass


class _Runner:
    def __init__(self, owner, hwnd, input_scale, capture_scale, dm_factory):
        self.owner = owner
        self.hwnd = hwnd
        self.input_scale = input_scale
        self.capture_scale = capture_scale
        self.dm_factory = dm_factory
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self._run, name=f"input-{owner.slave}", daemon=True)
        self.ready = threading.Event()
        self.error = None
        self.actuator = None
        self.channel = None
        self.started_at = None
        self.current = None
        self.stopping = False

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            self.channel = Channel(self.dm_factory())
            self.channel.bind(self.hwnd)
            viewport = Viewport(input_scale=self.input_scale, capture_scale=self.capture_scale)

            def capture():
                width, height = viewport.game_size
                cap = wincap.capture_scaled(self.hwnd, width, height)
                if not cap.get("ok"):
                    return None
                return cap["rgb"], cap["w"], cap["h"]

            self.actuator = Actuator(self.channel, viewport, cursor=CursorTracker(),
                                     slave=self.owner.slave, capture=capture,
                                     visual_align=self.owner.visual_align, lan_mode=True)
            self.actuator.align_tolerance = self.owner.align_tolerance
            self.actuator.align_max_s = self.owner.align_max_s
        except BaseException as exc:
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        while not self.stopping:
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                self.owner._heartbeat(self)
                continue
            if job is None:
                break
            name, args, kwargs, future = job
            if not future.set_running_or_notify_cancel():
                continue
            self.current = name
            self.started_at = time.monotonic()
            try:
                if name == "snapshot":
                    result = self._snapshot()
                elif name == "frame":
                    result = self._frame()
                elif name == "region":
                    result = self._region(*args)
                elif name == "color":
                    result = self.channel.get_color(*self.actuator.vp.g_to_v(*args))
                elif name == "state":
                    result = self._state()
                elif name == "profile":
                    self._profile(kwargs)
                    result = None
                elif name == "calibrate":
                    result = self._calibrate()
                elif name == "clear_path":
                    self.actuator.last_path = []
                    self.actuator.last_click = None
                    self.actuator.last_align = None
                    self.actuator.last_align_error = None
                    self.actuator.last_align_path = []
                    self.actuator.last_send = None
                    self.actuator.last_gem = None
                    result = None
                elif name == "reset_cursor":
                    self.actuator._cursor.reset()
                    result = None
                elif name == "visual":
                    self.actuator.visual_align = bool(kwargs.get("enabled"))
                    result = None
                elif name == "align":
                    self.actuator.align_tolerance = kwargs["tolerance"]
                    self.actuator.align_max_s = kwargs["max_s"]
                    result = None
                elif name in ACTIONS:
                    result = getattr(self.actuator, name)(*args, **kwargs)
                else:
                    raise WorkerError(f"不支持的动作：{name}")
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                self.current = None
                self.started_at = None
                self.owner._heartbeat(self)
        if self.actuator is not None:
            try:
                self.actuator.release_all()
            except Exception:
                pass
        if self.channel is not None and self.channel.bound:
            try:
                self.channel.unbind()
            except Exception:
                pass

    def _snapshot(self):
        path = os.path.join(runtime_dir(), f"kmtool_{self.owner.slave_safe}_{threading.get_ident()}.png")
        width, height = self.channel.client_size()
        try:
            if not self.channel.capture_png(0, 0, width, height, path):
                raise WorkerError("后台抓图失败")
            with open(path, "rb") as handle:
                data = handle.read()
            return data, self.actuator.last_path, self.actuator.last_start_source, self.actuator.last_click
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def frame_direct(self):
        """预览帧只用 PrintWindow，不碰大漠，所以可以在调用线程里直接取，不必排在动作队列后面。

        对齐动作一次可能占着队列好几秒，排队取帧会让预览整段冻结。
        """
        actuator = self.actuator
        if actuator is None:
            return None
        width, height = actuator.vp.game_size
        cap = wincap.capture_scaled(self.hwnd, width, height)
        if not cap.get("ok"):
            return None
        data = wincap.to_ppm(cap["rgb"], cap["w"], cap["h"])
        return data, list(actuator.last_path), actuator.last_start_source, actuator.last_click

    def _frame(self):
        result = self.frame_direct()
        return result if result is not None else self._snapshot()

    def _region(self, x1, y1, x2, y2, path):
        vp = self.actuator.vp
        for x, y in ((x1, y1), (x2, y2)):
            vp.check_g(x, y)
        a = vp.g_to_v(min(x1, x2), min(y1, y2))
        b = vp.g_to_v(max(x1, x2), max(y1, y2))
        if not self.channel.capture_png(a[0], a[1], b[0], b[1], path):
            raise WorkerError("区域抓图失败")
        return path

    def _profile(self, values):
        for name, value in values.items():
            if not hasattr(self.actuator.profile, name):
                raise WorkerError(f"未知轨迹参数：{name}")
            setattr(self.actuator.profile, name, value)

    def _calibrate(self):
        import math
        from control.calib import NINE_POINTS, CalibrationRow, TemplatePointerDetector, assess_rows
        # 1:1 画面用原模板 mouse1~4.bmp。模板左上角比发送点偏 (-1,-1)：登录页 30 个随机点实测
        # 偏移范围 x -2~-1、y -1~0（runtime/v16_out.jsonl V16_E2），补回 (1,1) 后偏差不超过 1.5px
        detector = TemplatePointerDetector(self.channel, self.actuator.vp, hotspot=(1, 1))
        previous = self.actuator._cursor
        previous_visual = self.actuator.visual_align
        self.actuator._cursor = CursorTracker(detector, min_detect_interval=0.2)
        self.actuator.visual_align = False
        rows = []
        try:
            for index, point in enumerate(NINE_POINTS, 1):
                estop.check()
                self.actuator.move_to(*point)
                time.sleep(0.8)
                path = os.path.join(runtime_dir(), f"kmtool_calib_{index}.jpg")
                detector.refresh_to(path)
                detected = detector.detect(point)
                deviation = None if detected is None else math.dist(point, detected)
                rows.append(CalibrationRow(point, detected, deviation, detector.last_ms, path))
            passed, detail = assess_rows(rows)
            return rows, passed, detail
        finally:
            self.actuator.visual_align = previous_visual
            self.actuator._cursor = previous
            self.actuator._cursor.reset()

    def _state(self):
        target = self.channel.assert_target()
        cursor = self.actuator._cursor.cursor
        return {
            "target": target, "cursor": cursor,
            "start_source": self.actuator.last_start_source,
            "path": list(self.actuator.last_path), "click": self.actuator.last_click,
            "align": self.actuator.last_align, "align_error": self.actuator.last_align_error,
            "send": self.actuator.last_send, "gem": self.actuator.last_gem,
            "visual": self.actuator.visual_align,
            "align_ok": self.actuator.last_align_ok,
        }


class InputWorker:
    def __init__(self, hwnd: int, slave: str = "viewer", *, input_scale: float = 1.0,
                 capture_scale: float = 1.0, dm_factory=make_dm, action_timeout: float = 10.0):
        self.slave = slave
        self.slave_safe = "".join(c if c.isalnum() else "_" for c in slave)
        self.action_timeout = action_timeout
        self.visual_align = False
        self.align_tolerance = Actuator.ALIGN_DEADBAND
        self.align_max_s = Actuator.ALIGN_MAX_S
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._runner = _Runner(self, hwnd, input_scale, capture_scale, dm_factory)
        self._stopped = False
        self.last_heartbeat = time.monotonic()
        self.rebuild_count = 0
        self._runner.start()

    def wait_ready(self, timeout: float = 15.0):
        runner = self._runner
        if not runner.ready.wait(timeout):
            raise WorkerError("大漠绑定超时")
        if runner.error is not None:
            raise runner.error
        return self

    def submit(self, command: str, *args, **kwargs) -> Future:
        with self._lock:
            if self._stopped:
                raise WorkerError("输入工作者已停止")
            future = Future()
            self._runner.jobs.put((command, args, kwargs, future))
            return future

    def call(self, command: str, *args, timeout: float | None = None, **kwargs):
        future = self.submit(command, *args, **kwargs)
        limit = self.action_timeout if timeout is None else timeout
        if timeout is None and self.visual_align and command in ALIGN_ACTIONS:
            limit += self.align_max_s + 5
        try:
            return future.result(limit)
        except FutureTimeoutError:
            future.cancel()
            self._timed_out(command)
            raise WorkerError(f"动作 {command} 超过 {limit:g} 秒，已隔离并重建工作者") from None

    def snapshot(self, timeout: float = 8.0):
        return self.call("snapshot", timeout=timeout)

    def frame(self, timeout: float = 5.0):
        runner = self._runner
        if not runner.stopping:
            direct = runner.frame_direct()
            if direct is not None:
                return direct
        return self.call("frame", timeout=timeout)

    def state(self, timeout: float = 3.0):
        return self.call("state", timeout=timeout)

    def set_visual(self, enabled: bool) -> None:
        with self._lock:
            if self._stopped:
                return
            self.visual_align = bool(enabled)
        self.submit("visual", enabled=bool(enabled))

    def set_align(self, tolerance: int, max_s: float) -> None:
        tolerance = min(20, max(1, int(tolerance)))
        max_s = min(120.0, max(3.0, float(max_s)))
        with self._lock:
            if self._stopped:
                return
            self.align_tolerance = tolerance
            self.align_max_s = max_s
        self.submit("align", tolerance=tolerance, max_s=max_s)

    def _timed_out(self, command):
        with self._lock:
            if self._stopped:
                return
            old = self._runner
            if old.current != command:
                return
            old.stopping = True
            self._fail_pending(old, WorkerError("前一个动作超时，工作者已重建"))
            self._runner = _Runner(self, old.hwnd, old.input_scale, old.capture_scale, old.dm_factory)
            self.rebuild_count += 1
            self._runner.start()
            self._cv.notify_all()

    @staticmethod
    def _fail_pending(runner, exc):
        while True:
            try:
                job = runner.jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None and not job[3].done():
                job[3].set_exception(exc)

    def _heartbeat(self, runner):
        with self._lock:
            if runner is self._runner:
                self.last_heartbeat = time.monotonic()
                self._cv.notify_all()

    def estop(self):
        estop.trigger(f"{self.slave} 输入急停")

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            runner = self._runner
            runner.stopping = True
            runner.jobs.put(None)
            self._fail_pending(runner, WorkerError("输入工作者已停止"))
            self._cv.notify_all()
        runner.thread.join(timeout=3.0)
