"""连接流程里不依赖界面的部分：先保证查看器是标准参数，再等窗口出现，最后等画面出齐。

分两步是因为慢链路上"窗口出现"（2~3 秒）远早于"画面出齐"（11~21 秒，多会话共用一条链路可到 30~110 秒）。
画面没出齐就绑定、校准或取色，读到的是黑块。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from common.log import log
from control import viewer_std

STANDARD_CLIENT = (800, 600)
WINDOW_TIMEOUT_S = 40.0
FRAME_TIMEOUT_S = 120.0
_EDGE_PX = 8
_DARK_SUM = 30


class Cancelled(Exception):
    pass


class FrameNotReady(RuntimeError):
    pass


def _lit(rgb, offset: int) -> bool:
    return rgb[offset] + rgb[offset + 1] + rgb[offset + 2] > _DARK_SUM


def frame_filled(rgb, w: int, h: int) -> bool:
    """画面出齐：高度 1/4、1/2、3/4 三行，非黑像素都从最左 8 像素内铺到最右 8 像素内。

    分块陆续到达的画面先出现在左上，所以只看一部分有内容不算数；整幅黑也不算。
    """
    if w <= 2 * _EDGE_PX or h <= 0 or len(rgb) < w * h * 3:
        return False
    for y in (h // 4, h // 2, h * 3 // 4):
        row = y * w * 3
        if not any(_lit(rgb, row + x * 3) for x in range(_EDGE_PX + 1)):
            return False
        if not any(_lit(rgb, row + x * 3) for x in range(w - _EDGE_PX - 1, w)):
            return False
    return True


def wait_frame_ready(grab: Callable[[], tuple | None], *, cancel: threading.Event | None = None,
                     progress: Callable[[float], None] | None = None, timeout_s: float = FRAME_TIMEOUT_S,
                     poll_s: float = 0.5, clock=time.monotonic, sleep=time.sleep) -> float:
    """轮询到画面出齐，返回用时；用户取消抛 Cancelled，超时抛 FrameNotReady。

    grab() 返回 (rgb, w, h) 或 None（抓图失败），必须是 800x600 的全尺寸画面，不能用缩略图。
    """
    start = clock()
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        cap = grab()
        if cap is not None and frame_filled(*cap):
            return clock() - start
        elapsed = clock() - start
        if progress is not None:
            progress(elapsed)
        if elapsed >= timeout_s:
            raise FrameNotReady(f"等了 {elapsed:.0f} 秒，远程画面还没传完整。链路很慢，或副机游戏没有打开；"
                                "确认副机状态后可再点一次连接。")
        sleep(poll_s)


def standard_problems(pid: int, command_line=viewer_std.process_command_line) -> list[str]:
    """已开着的查看器是否按标准参数启动；命令行读不到时无从核对，按没问题处理。"""
    line = command_line(pid)
    if line is None:
        return []
    return viewer_std.check_standard(viewer_std.parse_viewer_cmd(line))


def viewer_targets(pid: int, host: str, port: int, command_line=viewer_std.process_command_line,
                   endpoints=viewer_std.tcp_remote_endpoints) -> bool:
    """这个查看器进程是不是连着 host:port。读不到命令行（权限不够）按"不是"处理，宁可新开也不复用看不清的。"""
    line = command_line(pid)
    if line is None:
        return False
    cmd = viewer_std.parse_viewer_cmd(line)
    if cmd.host:
        return viewer_std.same_host(cmd.host, host) and (cmd.port or 5900) == port
    return any(ip == host and p == port for ip, p in endpoints(pid))


def address_viewers(host: str, port: int, find_all, pid_of, targets=viewer_targets) -> list:
    """连着 host:port 的查看器会话窗口。

    不能用窗口标题找：连上之后标题是服务端的机器名（如"desktop-xxx – TightVNC Viewer"），不含 IP，
    按标题找永远找不到，每次连接都会白开一个新查看器。
    """
    return [h for h in find_all() if targets(pid_of(h.top), host, port)]


@dataclass
class Ops:
    """连接流程用到的外部动作，单元测试里整体换成假的。"""
    ensure_standard: Callable
    find_viewers: Callable
    launch_viewer: Callable
    confirm_unencrypted: Callable
    wait_target: Callable           # (children, timeout_s) -> ViewerTarget
    pid_of: Callable
    client_width: Callable
    fit: Callable                   # ViewerTarget -> ViewerTarget，客户区调到 800x600
    grab: Callable                  # hwnd -> (rgb, w, h) | None
    clock: Callable = time.monotonic
    sleep: Callable = time.sleep
    address_viewers: Callable | None = None   # (host, port) -> 连着该地址的查看器；缺省退回按标题找（仅测试用）


def acquire_target(host: str, port: int, password: str, ops: Ops, window_timeout: float = WINDOW_TIMEOUT_S):
    """拿到连着 host:port 的标准查看器的画面窗口（只保证窗口出现，画面是否出齐见 prepare_target）。

    ensure_standard 必须先于 find_viewers：否则会把别人用非标准参数开的旧查看器当成可用的复用。
    ViewerSettingsError 等错误原样抛出，文字可直接给用户看。
    """
    killed = ops.ensure_standard(host, port)
    existing = ops.address_viewers(host, port) if ops.address_viewers else ops.find_viewers(host)
    if existing:
        ordered = sorted(existing, key=lambda h: ops.client_width(h.child), reverse=True)
        return ops.wait_target([h.child for h in ordered], 8.0)
    proc = ops.launch_viewer(host, port, password)
    if killed:
        # launch_viewer 内部的 ensure_standard 这时已无非标准查看器可关，不会再写这句，所以在这里补上
        log(f"已按标准参数重开查看器 {host}::{port}")
    # 标准参数带 -unencrypted=allow，正常不会出现确认框；放在后台只当兜底，不能让它占住 12 秒
    threading.Thread(target=ops.confirm_unencrypted, args=(proc.pid, 12), daemon=True).start()
    deadline = ops.clock() + window_timeout
    while ops.clock() < deadline:
        found = [h for h in ops.find_viewers() if ops.pid_of(h.top) == proc.pid]
        if found:
            return ops.wait_target([h.child for h in found], max(1.0, deadline - ops.clock()))
        ops.sleep(0.4)
    raise RuntimeError(f"{window_timeout:g} 秒内未出现已连接的画面窗口")


def prepare_target(target, ops: Ops, cancel: threading.Event | None = None,
                   progress: Callable[[float], None] | None = None, timeout_s: float = FRAME_TIMEOUT_S):
    """窗口出现之后：客户区调到 800x600，再等画面出齐。新启动的和复用的查看器都要走这一步。"""
    target = ops.fit(target)
    wait_frame_ready(lambda: ops.grab(target.child), cancel=cancel, progress=progress, timeout_s=timeout_s,
                     clock=ops.clock, sleep=ops.sleep)
    return target
