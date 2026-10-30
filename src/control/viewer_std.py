"""查看器标准启动参数：生成、判定、对已开着的查看器核对并关闭不合标准的。

依据（2026-10-29 用假 RFB 服务器在 TightVNC 4.0 上实测线上编码）：
- 命令行是唯一有效的通道。注册表 History / Settings 对"命令行带 -host 启动"的查看器完全没有影响，
  所以不做注册表预写。
- `-jpegimagequality=-1` 是 4.0 的未公开用法：帮助文档只写 0..9，实测 -1 = 不发 JPEG 质量伪编码（JPEG 关闭）。
- `-scale=100` 配合客户区恒为 800x600 物理像素：1 帧像素 = 1 物理像素，输入坐标 V = G，与主机 DPI 无关，截图无插值。
  客户区不是 800x600 时画面会居中留黑、坐标整体偏移（1600x1200 时偏 (400,300)），所以调用方必须把客户区调成 800x600。
  `-scale=auto` 随客户区缩放，客户区一变（比例不对时还会留边）坐标就偏几到十几个像素，不适合做精确鼠标定位。
- `-encoding` 的值区分大小写：写成 ZRLE 会退化成 Raw；选项名不区分大小写。
- 编码选 tight：JPEG 关闭后 Tight 仍是无损（zlib），而且分块陆续出画面，真机慢链路上 5~7 秒就有内容，整屏约 11~21 秒；
  ZRLE 要等整屏一次到齐，约 17~36 秒才出画面，没有更快（2026-10-29 真机多轮交替对比）。压缩级别 1/9 与默认无明显差别，不设。
- `-unencrypted=allow` 免掉"未加密连接"确认框；缺省或 ask 会弹框。
密码只出现在命令行里，任何日志都要先经 redact()。
"""
from __future__ import annotations

import ctypes
import re
import socket
import struct
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

from common import winapi
from common.log import log

VIEWER_PROCESS_NAME = "tvnviewer.exe"

# 唯一的标准参数表：(选项名, 值)。改这里即改全部调用方。
STANDARD_OPTIONS: tuple[tuple[str, str], ...] = (
    ("jpegimagequality", "-1"),
    ("scale", "100"),
    ("encoding", "tight"),
    ("unencrypted", "allow"),
)
# 这些选项出现且为 yes 就不合标准（只看会让后台输入失效，全屏会破坏窗口尺寸）。
FORBIDDEN_YES = ("viewonly", "fullscreen", "mouseswap")
# 这些选项存在就无法判断连接选项来自哪里，不合标准。
FORBIDDEN_PRESENT = ("optionsfile", "listen")

_PASSWORD_RE = re.compile(r'(-password=)(?:"[^"]*"|\S*)', re.I)


class ViewerSettingsError(OSError):
    """查看器按标准参数启动后仍核对不过。继承 OSError，旧调用方的 except OSError 能接住。"""


def standard_args(host: str, port: int, password: str = "") -> list[str]:
    """不含 exe 的标准参数列表。"""
    args = [f"-host={host}", f"-port={port}"]
    args += [f"-{k}={v}" for k, v in STANDARD_OPTIONS]
    if password:
        args.append(f"-password={password}")
    return args


def redact(text: str) -> str:
    """把命令行文本里的密码换成占位符，写日志前必须过一遍。"""
    return _PASSWORD_RE.sub(r"\1<hidden>", text)


def split_cmdline(cmdline: str) -> list[str]:
    """按 Windows 规则拆命令行（含第一个 exe 路径）。"""
    n = ctypes.c_int(0)
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    ptr = shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    if not ptr:
        return cmdline.split()
    try:
        return [ptr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ptr)


@dataclass
class ViewerCmd:
    host: str = ""
    port: int = 0                       # 0 = 命令行没给
    opts: dict[str, str] = field(default_factory=dict)  # 选项名小写 -> 值（无值的开关为 ""）


def _split_host_port(text: str) -> tuple[str, int]:
    """HOST、HOST::PORT、HOST:显示号（TightVNC 的 :N 表示端口 5900+N，N>=100 时当作端口）。"""
    if "::" in text:
        h, _, p = text.rpartition("::")
        return h, int(p) if p.isdigit() else 0
    if text.count(":") == 1:
        h, _, p = text.partition(":")
        if p.isdigit():
            n = int(p)
            return h, (5900 + n if n < 100 else n)
    return text, 0


def parse_viewer_cmd(cmdline: str) -> ViewerCmd:
    argv = split_cmdline(cmdline)[1:]
    cmd = ViewerCmd()
    for tok in argv:
        if tok.startswith(("-", "/")) and len(tok) > 1:
            name, eq, val = tok[1:].partition("=")
            name = name.lower()
            if name == "host":
                cmd.host, p = _split_host_port(val)
                cmd.port = p or cmd.port
            elif name == "port" and val.isdigit():
                cmd.port = int(val)
            else:
                cmd.opts[name] = val if eq else ""
        elif not cmd.host:
            cmd.host, cmd.port = _split_host_port(tok)
    return cmd


def check_standard(cmd: ViewerCmd) -> list[str]:
    """返回不合标准的项（中文短语）；空列表 = 标准。选项名不分大小写，值必须与标准完全一致。"""
    bad: list[str] = []
    for name, want in STANDARD_OPTIONS:
        got = cmd.opts.get(name)
        if got is None:
            bad.append(f"缺 -{name}={want}")
        elif got != want:
            bad.append(f"-{name}={got}（应为 {want}）")
    for name in FORBIDDEN_YES:
        if cmd.opts.get(name, "").lower() == "yes":
            bad.append(f"-{name}=yes 不允许")
    for name in FORBIDDEN_PRESENT:
        if name in cmd.opts:
            bad.append(f"-{name} 不允许")
    return bad


# ---------------------------------------------------------------- 进程与网络（系统调用集中在这里，测试时整体替换）

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
_ntdll.NtQueryInformationProcess.restype = ctypes.c_long
winapi.kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
winapi.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_PROCESS_COMMAND_LINE_INFORMATION = 60
_STATUS_LENGTH_ERRORS = (0xC0000004, 0xC0000023, 0x80000005)
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


def process_command_line(pid: int) -> str | None:
    """读别的进程的完整命令行（同一用户无需管理员）；读不到（权限、进程已退出）返回 None。"""
    h = winapi.kernel32.OpenProcess(winapi.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = 2048
        for _ in range(3):
            buf = ctypes.create_string_buffer(size)
            need = ctypes.c_ulong(0)
            st = _ntdll.NtQueryInformationProcess(h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(need))
            if st == 0:
                us = _UnicodeString.from_buffer(buf)
                return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
            if (st & 0xFFFFFFFF) in _STATUS_LENGTH_ERRORS:
                size = max(need.value, size * 2)
                continue
            return None
        return None
    finally:
        winapi.kernel32.CloseHandle(h)


def list_pids(exe_name: str = VIEWER_PROCESS_NAME) -> list[int]:
    k = winapi.kernel32
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == winapi.INVALID_HANDLE:
        return []
    out: list[int] = []
    try:
        pe = winapi._ProcEntry()
        pe.dwSize = ctypes.sizeof(winapi._ProcEntry)
        ok = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == exe_name.lower():
                out.append(pe.th32ProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def kill_process(pid: int, wait_s: float = 3.0) -> bool:
    """结束进程并等它真正退出（窗口随之销毁）。"""
    k = winapi.kernel32
    h = k.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        if not k.TerminateProcess(h, 1):
            return False
        return k.WaitForSingleObject(h, int(wait_s * 1000)) == 0
    finally:
        k.CloseHandle(h)


class _TcpRow(ctypes.Structure):
    _fields_ = [("state", wintypes.DWORD), ("local_addr", wintypes.DWORD), ("local_port", wintypes.DWORD),
                ("remote_addr", wintypes.DWORD), ("remote_port", wintypes.DWORD), ("pid", wintypes.DWORD)]


def tcp_remote_endpoints(pid: int) -> list[tuple[str, int]]:
    """该进程的 IPv4 TCP 连接对端 (IP, 端口)，含正在连接的。"""
    iph = ctypes.WinDLL("iphlpapi")
    size = wintypes.DWORD(0)
    iph.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)  # AF_INET, TCP_TABLE_OWNER_PID_ALL
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        if iph.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) == 0:
            break
    else:
        return []
    n = struct.unpack_from("<I", buf, 0)[0]
    out: list[tuple[str, int]] = []
    for i in range(n):
        row = _TcpRow.from_buffer(buf, 4 + i * ctypes.sizeof(_TcpRow))
        if row.pid != pid or row.remote_addr == 0:
            continue
        ip = socket.inet_ntoa(struct.pack("<I", row.remote_addr))
        port = ((row.remote_port & 0xFF) << 8) | ((row.remote_port >> 8) & 0xFF)
        out.append((ip, port))
    return out


@dataclass
class SysOps:
    """系统调用集合，单元测试里整体换成假的。"""
    list_pids: Callable[[], list[int]] = list_pids
    command_line: Callable[[int], str | None] = process_command_line
    kill: Callable[[int], bool] = kill_process
    endpoints: Callable[[int], list[tuple[str, int]]] = tcp_remote_endpoints


def _is_ip(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


def _resolve(name: str) -> str:
    if _is_ip(name):
        return name
    try:
        return socket.gethostbyname(name)
    except OSError:
        return name.lower()


def same_host(a: str, b: str) -> bool:
    if a.lower() == b.lower():
        return True
    if _is_ip(a) and _is_ip(b):
        return False
    return _resolve(a) == _resolve(b)


def _targets(cmd: ViewerCmd, pid: int, host: str, port: int, ops: SysOps) -> bool:
    """这个查看器是不是连着 host:port。命令行里有 -host 就以它为准，否则看它的实际 TCP 连接。"""
    if cmd.host:
        return same_host(cmd.host, host) and (cmd.port or 5900) == port
    ip = _resolve(host)
    return any(r_ip == ip and r_port == port for r_ip, r_port in ops.endpoints(pid))


def ensure_standard(host: str, port: int, ops: SysOps | None = None) -> list[int]:
    """关闭所有连着 host:port 却不是标准参数启动的 tvnviewer。返回被关闭的 pid 列表。

    只动 tvnviewer.exe；命令行读不到的（权限不够）跳过；连着别的地址的不碰。
    """
    ops = ops or SysOps()
    killed: list[int] = []
    for pid in ops.list_pids():
        line = ops.command_line(pid)
        if line is None:
            continue
        cmd = parse_viewer_cmd(line)
        if not _targets(cmd, pid, host, port, ops):
            continue
        bad = check_standard(cmd)
        if not bad:
            continue
        log(f"关闭非标准查看器 {host}::{port} pid={pid}：{'；'.join(bad)}")
        if ops.kill(pid):
            killed.append(pid)
        else:
            log(f"结束查看器 pid={pid} 失败")
    return killed


def verify_process(pid: int, host: str, port: int, ops: SysOps | None = None) -> list[str]:
    """核对刚启动的查看器：命令行必须是标准参数且地址匹配。命令行读不到时无从核对，返回空（沿用我们自己传的参数）。"""
    ops = ops or SysOps()
    line = ops.command_line(pid)
    if line is None:
        return []
    cmd = parse_viewer_cmd(line)
    bad = check_standard(cmd)
    if not cmd.host or not same_host(cmd.host, host) or (cmd.port or 5900) != port:
        bad.append(f"地址不对（{cmd.host}::{cmd.port}）")
    return bad


def prepare(exe: str) -> None:
    """启动前的一次性准备：补上 DPI 兼容设置（只写当前用户注册表，对下一次启动生效）。"""
    from common import env_check
    try:
        if not env_check.viewer_dpi_flag_set(exe):
            env_check.fix_viewer_dpi(exe)
            log("已补上查看器 DPI 兼容设置")
    except OSError as e:
        log(f"补查看器 DPI 兼容设置失败：{e}")
