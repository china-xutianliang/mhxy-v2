from __future__ import annotations

import os
import re
from dataclasses import dataclass

from common import winapi

VIEWER_CLASS = "tvnviewer-session-window"
VIEWER_EXE = "tvnviewer.exe"
_ADDRESS = re.compile(r"(?P<host>\[[0-9a-fA-F:]+\]|[0-9a-zA-Z_.-]+)::(?P<port>\d+)")


class TargetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ViewerTarget:
    top: int
    child: int
    pid: int
    title: str
    address: str = ""


def resolve(hwnd: int, api=winapi) -> ViewerTarget:
    if not hwnd or not api.is_window(hwnd):
        raise TargetError("目标窗口不存在或句柄为 0")
    top = hwnd
    while api.parent_of(top):
        top = api.parent_of(top)
    if api.class_name(top) != VIEWER_CLASS:
        raise TargetError("目标不是 TightVNC 查看器会话窗口")
    pid = api.pid_of(top)
    image = os.path.basename(api.image_path(pid)).lower()
    if image != VIEWER_EXE:
        raise TargetError(f"目标进程不是 {VIEWER_EXE}，已拒绝绑定")
    children = [h for h in api.enum_children(top)
                if api.class_name(h) == VIEWER_CLASS and h != top]
    if not children:
        raise TargetError("查看器里找不到画面子窗口 tvnviewer-session-window")
    child = children[0]
    if not api.is_window(child) or not api.is_visible(child):
        raise TargetError("画面子窗口不可见或已经失效")
    if api.is_iconic(top):
        raise TargetError("查看器处于最小化状态，请先不激活还原")
    width, height = api.client_size(child)
    if width <= 0 or height <= 0:
        raise TargetError("画面子窗口客户区为 0，拒绝发送输入")
    title = api.title(top)
    match = _ADDRESS.search(title)
    address = f"{match.group('host')}:{match.group('port')}" if match else ""
    return ViewerTarget(top, child, pid, title, address)


def validate_child(hwnd: int, api=winapi) -> ViewerTarget:
    target = resolve(hwnd, api)
    if target.child != hwnd:
        raise TargetError("输入只能发到画面子窗口，不能发到查看器顶层或其他窗口")
    return target
