"""环境检查：每项返回 (名称, 是否通过, 详情, 修复提示)。"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
from dataclasses import dataclass

from common import winapi
from common.config import resources_path

VIEWER_EXE = r"C:\Program Files\TightVNC\tvnviewer.exe"
LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


def python_bits() -> int:
    return struct.calcsize("P") * 8


def process_dpi_awareness() -> int:
    """0=不识别 1=系统 2=每显示器。"""
    val = ctypes.c_int(-1)
    try:
        ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(val))
    except OSError:
        return -1
    return val.value


def system_scale_percent() -> int:
    try:
        return round(ctypes.windll.user32.GetDpiForSystem() * 100 / 96)
    except OSError:
        return 100


def viewer_dpi_flag_set(exe: str = VIEWER_EXE) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY) as k:
            val, _ = winreg.QueryValueEx(k, exe)
            return "HIGHDPIAWARE" in str(val).upper()
    except OSError:
        return False


def fix_viewer_dpi(exe: str = VIEWER_EXE) -> None:
    """让 tvnviewer 按物理像素渲染（只写当前用户的兼容性设置，需重启查看器生效）。"""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY) as k:
        winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, "~ HIGHDPIAWARE")


def run_checks() -> list[Check]:
    out: list[Check] = []
    bits = python_bits()
    out.append(Check("Python 位数", bits == 32, f"{bits} 位 ({sys.version.split()[0]})",
                     "大漠 dm.dll 是 32 位，必须用 32 位 Python"))
    admin = winapi.is_admin()
    out.append(Check("管理员权限", admin, "是" if admin else "否", "大漠注册需要管理员，请以管理员身份运行"))
    aw = process_dpi_awareness()
    out.append(Check("本进程 DPI 感知", aw == 2, {0: "不识别", 1: "系统", 2: "每显示器"}.get(aw, "未知"),
                     "窗口尺寸会按缩放比例错位，必须在创建窗口前调用 set_dpi_aware"))
    out.append(Check("系统缩放", True, f"{system_scale_percent()}%（主控机可以不是 100%）"))
    for name in ("dm.dll", "DmReg.dll"):
        p = resources_path("dll", name)
        out.append(Check(f"resources/dll/{name}", os.path.exists(p), p if os.path.exists(p) else "缺失"))
    exists = os.path.exists(VIEWER_EXE)
    out.append(Check("TightVNC 查看器", exists, VIEWER_EXE if exists else "未找到", "请安装 TightVNC Viewer"))
    if exists:
        flag = viewer_dpi_flag_set()
        out.append(Check("查看器 DPI 兼容设置", flag, "已设置 (HIGHDPIAWARE)" if flag else "未设置",
                         "主控机缩放不是 100% 时必须设置；点“修复查看器 DPI”后重启查看器"))
    return out
