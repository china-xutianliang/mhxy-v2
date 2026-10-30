"""一台设备对应一个会话：启动 TightVNC 查看器、盯着它的状态、持续截取远程画面。

只在主控端运行，用 Win32 读取查看器窗口，不碰设备上的任何东西（设备零注入）。
查看器窗口连上后被调成"游戏画面刚好 800x600（按本机缩放）"，再停到屏幕外：
画面照常刷新、PrintWindow 照常能截，但不挡主机桌面，也不会被误点。
"""
from __future__ import annotations

import subprocess
import threading
import time
import zlib
from dataclasses import dataclass

from common import winapi, wincap
from control import viewer_link as vl
from manager.machines import Machine

IDLE, CONNECTING, ONLINE, LOST, ERROR = "idle", "connecting", "online", "lost", "error"
GAME_W, GAME_H = 800, 600
PARK_X, PARK_Y = -30000, 0
STATIC_WARN_S = 10.0           # 画面这么久完全没变，提示"画面不动"（游戏里人物待机也会动）
CONNECT_TIMEOUT_S = 40.0
BACKOFF = (3.0, 6.0, 12.0, 24.0, 30.0)


@dataclass(frozen=True)
class Frame:
    seq: int
    w: int
    h: int
    ppm: bytes
    ts: float
    blank: bool


def next_backoff(attempt: int) -> float:
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def sample_signature(rgb) -> int:
    """隔点抽样算校验和，用来判断两帧是否一样（比整帧比较快得多）。"""
    return zlib.crc32(bytes(rgb[::97]))


class Session:
    def __init__(self, machine: Machine, viewer_exe: str = vl.DEFAULT_VIEWER_EXE,
                 park: bool = True, auto_reconnect: bool = True) -> None:
        self.machine = machine
        self.viewer_exe = viewer_exe
        self.park = park
        self.auto_reconnect = auto_reconnect
        self.state = IDLE
        self.message = "未连接"
        self.frame: Frame | None = None
        self.fps = 0.0
        self.static_since = 0.0          # 画面最近一次变化的时刻
        self.retry_at = 0.0              # LOST 时下一次自动重连的时刻
        self.online_since = 0.0
        self.want = (0, 0, 0.0)          # 界面要的截图宽、高、每秒帧数；帧数为 0 表示不用截
        self.remote_title = ""
        self._attempt = 0
        self._want_connect = False
        self._quit = False
        self._proc: subprocess.Popen | None = None
        self._h: vl.ViewerHandles | None = None
        self._seq = 0
        self._sig = None
        self._last_cap = 0.0
        self._prev_ts = 0.0
        self._last_check = 0.0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"session-{machine.name}")
        self._thread.start()

    # ------------------------------------------------------------ 对外接口
    @property
    def target_client(self) -> tuple[int, int]:
        # 查看器 -scale=100：1 帧像素 = 1 物理像素，客户区恒为 800x600，与主机 DPI 无关；输入坐标 V = G
        return GAME_W, GAME_H

    def connect(self) -> None:
        self._attempt = 0
        self.retry_at = 0.0
        self._want_connect = True

    def disconnect(self) -> None:
        self._want_connect = False
        self._teardown()
        self._set(IDLE, "未连接")

    def close(self) -> None:
        self._quit = True
        self._want_connect = False
        self._teardown()

    def static_for(self) -> float:
        return max(0.0, time.monotonic() - self.static_since) if self.state == ONLINE and self.static_since else 0.0

    def frame_age(self) -> float | None:
        f = self.frame
        return None if f is None else time.monotonic() - f.ts

    def show_viewer(self) -> None:
        """把原始查看器窗口摆到屏幕上，排查问题用。"""
        h = self._h
        if h and h.alive():
            vl.ensure_restored(h)
            winapi.set_window_size(h.top, *self._outer_size(h), 80, 80)
            winapi.user32.SetForegroundWindow(h.top)

    def hide_viewer(self) -> None:
        self._park()

    def grab(self, w: int = GAME_W, h: int = GAME_H) -> dict | None:
        """立刻截一张指定尺寸的图（保存快照用）。没连上返回 None。"""
        hd = self._h
        if self.state != ONLINE or not hd or not hd.alive():
            return None
        cap = wincap.capture_scaled(hd.child, w, h)
        return cap if cap.get("ok") else None

    # ------------------------------------------------------------ 内部
    def _set(self, state: str, message: str) -> None:
        with self._lock:
            self.state, self.message = state, message

    def _outer_size(self, h: vl.ViewerHandles) -> tuple[int, int]:
        left, top, right, bottom = winapi.window_rect(h.top)
        return right - left, bottom - top

    def _teardown(self) -> None:
        proc, self._proc = self._proc, None
        self._h = None
        self.fps = 0.0
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _park(self) -> None:
        h = self._h
        if self.park and h and h.alive():
            winapi.hide_from_taskbar(h.top)
            w, hh = self._outer_size(h)
            winapi.set_window_size(h.top, w, hh, PARK_X, PARK_Y)

    def _run(self) -> None:
        while not self._quit:
            try:
                if self.state == ONLINE:
                    self._tick_online()
                elif self._want_connect and self.state in (IDLE, LOST, ERROR) and time.monotonic() >= self.retry_at:
                    if self.state == ERROR and not self.auto_reconnect:
                        time.sleep(0.2)
                        continue
                    self._do_connect()
                else:
                    time.sleep(0.15)
            except Exception as e:  # noqa: BLE001  一台机器出错不能拖垮别的
                self._teardown()
                self._lost(f"内部错误：{type(e).__name__}: {e}")

    def _lost(self, why: str) -> None:
        self._teardown()
        if self._want_connect and self.auto_reconnect:
            wait = next_backoff(self._attempt)
            self._attempt += 1
            self.retry_at = time.monotonic() + wait
            self._set(LOST, f"{why}，{wait:.0f} 秒后自动重连")
        else:
            self._want_connect = False
            self._set(LOST, why)

    def _handle_dialogs(self, pid: int) -> str | None:
        """处理查看器弹出的对话框。"未加密连接"确认框直接点掉；其他对话框当作错误，返回错误文字。"""
        self._dlg_seen = {k: v for k, v in getattr(self, "_dlg_seen", {}).items() if winapi.user32.IsWindow(k)}
        for top in winapi.enum_top_windows():
            if winapi.pid_of(top) != pid or winapi.class_name(top) != "#32770":
                continue
            kids = winapi.enum_children(top)
            texts = [winapi.title(k).strip() for k in kids if winapi.class_name(k) == "Static"]
            texts = [t for t in texts if t]
            buttons = [k for k in kids if winapi.class_name(k) == "Button"]
            if any(vl.CONFIRM_BUTTON_PATTERN.search(winapi.title(b)) for b in buttons):
                for b in buttons:
                    if vl.CONFIRM_BUTTON_PATTERN.search(winapi.title(b)):
                        winapi.click_button(b)
                        break
                continue
            seen = self._dlg_seen.setdefault(top, time.monotonic())
            if time.monotonic() - seen < 3.0:
                continue  # 对话框刚弹出，按钮可能还没建好，等一会儿再判断是不是未加密确认框
            if any(winapi.class_name(k) == "Edit" for k in kids):
                if self.machine.password:
                    return "密码不对（右键“修改……”重新填写）"
                return "设备要求密码，但这里没填（右键“修改……”填写，或在设备 TightVNC 里取消密码）"
            text = " ".join(texts) or winapi.title(top) or "查看器弹出了未知提示"
            return text[:80]
        return None

    def _find_handles(self, pid: int) -> vl.ViewerHandles | None:
        for h in vl.find_viewers():
            if winapi.pid_of(h.top) == pid and winapi.client_size(h.child)[0] > 0:
                return h
        return None

    def _do_connect(self) -> None:
        m = self.machine
        self.frame = None
        self._sig = None
        self._prev_ts = 0.0
        self._set(CONNECTING, "正在启动查看器…")
        try:
            self._proc = vl.launch_viewer(m.host, m.port, m.password, self.viewer_exe)
        except (OSError, FileNotFoundError) as e:
            self._want_connect = False
            self._set(ERROR, str(e))
            return
        pid = self._proc.pid
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while time.monotonic() < deadline and not self._quit and self._want_connect:
            if self._proc is None:
                return
            rc = self._proc.poll()
            err = self._handle_dialogs(pid)
            if err:
                self._teardown()
                self._fail(err)
                return
            if rc is not None:
                self._teardown()
                self._fail(f"查看器已退出（代码 {rc}），可能密码或地址不对")
                return
            h = self._find_handles(pid)
            if h:
                self._h = h
                self._set(CONNECTING, "正在调整画面大小…")
                vl.fit_window(h, self.target_client)
                self._park()
                self.remote_title = winapi.title(h.top)
                self.static_since = time.monotonic()
                self.online_since = time.monotonic()
                self._set(ONLINE, "在线")
                return
            self._set(CONNECTING, "正在连接设备…")
            time.sleep(0.3)
        if self._want_connect and not self._quit:
            self._teardown()
            self._fail("连接超时：设备没响应（检查设备上 TightVNC 是否在运行、网络是否通）")

    def _fail(self, why: str) -> None:
        # 密码/地址这类错误自动重试没有意义，直接停下让用户改；超时和掉线才自动重连
        self._want_connect = False
        self._set(ERROR, why)

    def _tick_online(self) -> None:
        h, proc = self._h, self._proc
        if h is None or proc is None:
            self._lost("连接已断开")
            return
        if proc.poll() is not None or not h.alive():
            self._lost("连接断开了")
            return
        now = time.monotonic()
        if now - self._last_check > 2.0:
            self._last_check = now
            err = self._handle_dialogs(proc.pid)
            if err:
                self._lost(f"设备断开：{err}")
                return
            if winapi.is_iconic(h.top):
                vl.ensure_restored(h)
                self._park()
            if winapi.client_size(h.child) != self.target_client:
                vl.fit_window(h, self.target_client)
                self._park()
            self.remote_title = winapi.title(h.top)
        w, hh, fps = self.want
        if fps <= 0 or w <= 0 or hh <= 0:
            time.sleep(0.1)
            return
        interval = 1.0 / fps
        if now - self._last_cap < interval:
            time.sleep(min(0.05, interval / 4))
            return
        t0 = time.monotonic()
        cap = wincap.capture_scaled(h.child, w, hh)
        self._last_cap = t0
        if not cap.get("ok"):
            time.sleep(0.2)
            return
        rgb = cap["rgb"]
        sig = sample_signature(rgb)
        if sig != self._sig:
            self._sig = sig
            self.static_since = time.monotonic()
        self._seq += 1
        done = time.monotonic()
        if self._prev_ts:
            inst = 1.0 / max(1e-3, done - self._prev_ts)
            self.fps = 0.7 * self.fps + 0.3 * inst if self.fps else inst
        self._prev_ts = done
        self.frame = Frame(self._seq, w, hh, wincap.to_ppm(rgb, w, hh), done, bool(cap.get("blank")))
