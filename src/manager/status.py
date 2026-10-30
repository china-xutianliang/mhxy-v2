"""把会话状态翻译成界面上看得懂的文字和颜色（纯函数，方便单元测试）。"""
from __future__ import annotations

from manager.session import CONNECTING, ERROR, IDLE, LOST, ONLINE, STATIC_WARN_S

LATE_FRAME_S = 6.0

# 状态灯颜色名 -> 十六进制
LIGHT = {"ok": "#1f883d", "warn": "#bc4c00", "bad": "#cf222e", "busy": "#9a6700", "idle": "#8c8c94"}


def describe(state: str, message: str, fps: float, frame_age: float | None, static_s: float) -> tuple[str, str, str]:
    """返回 (灯的级别, 状态简称, 一行说明)。"""
    if state == ONLINE:
        if frame_age is None:
            return "warn", "在线", "等待第一帧画面……"
        if frame_age > LATE_FRAME_S:
            return "warn", "画面延迟", f"已经 {frame_age:.0f} 秒没有收到新画面"
        if static_s >= STATIC_WARN_S:
            return "warn", "画面不动", f"画面已 {static_s:.0f} 秒没有变化（游戏卡住或网络断了？）"
        return "ok", "在线", f"{fps:.1f} 帧/秒　{frame_age:.1f} 秒前更新"
    if state == CONNECTING:
        return "busy", "连接中", message
    if state == LOST:
        return "bad", "已断开", message
    if state == ERROR:
        return "bad", "出错", message
    return "idle", "未连接", "点“连接”开始"


def action_label(state: str) -> str:
    return {IDLE: "连接", CONNECTING: "取消", ONLINE: "断开", LOST: "立即重连", ERROR: "重试"}.get(state, "连接")


def summarize(states: list[str], warns: int = 0) -> tuple[str, str]:
    """顶部横幅：(级别, 文字)。"""
    n = len(states)
    if n == 0:
        return "idle", "还没有添加设备"
    online = sum(1 for s in states if s == ONLINE)
    bad = sum(1 for s in states if s in (LOST, ERROR))
    busy = sum(1 for s in states if s == CONNECTING)
    parts = [f"{n} 台", f"在线 {online}"]
    if busy:
        parts.append(f"连接中 {busy}")
    if bad:
        parts.append(f"异常 {bad}")
    if warns:
        parts.append(f"画面异常 {warns}")
    level = "bad" if bad else ("warn" if warns or busy else ("ok" if online == n else "idle"))
    return level, " · ".join(parts)
