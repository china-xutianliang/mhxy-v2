"""创建并注册大漠对象。必须在 32 位 Python、管理员权限下调用，且只在创建它的线程里使用。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from common.config import load_yaml, resources_path


class DmError(RuntimeError):
    pass


def make_dm():
    import pythoncom
    import win32com.client  # 延迟导入：单元测试环境不需要

    pythoncom.CoInitialize()
    raw = load_yaml("config", "driver.yaml")
    dmreg = ctypes.WinDLL(resources_path("dll", raw["dll"]["dmreg_file"]))
    dmreg.SetDllPathW.argtypes = [wintypes.LPCWSTR, wintypes.LONG]
    dmreg.SetDllPathW.restype = wintypes.LONG
    if dmreg.SetDllPathW(resources_path("dll", raw["dll"]["dm_file"]), 0) != 1:
        raise DmError("SetDllPathW 失败（dm.dll 路径或位数不对，需要 32 位 Python）")
    dm = win32com.client.Dispatch("dm.dmsoft")
    ret = dm.Reg(raw["auth"]["reg_code"], raw["auth"]["ver_info"])
    if ret != 1:
        hint = "需要管理员权限" if ret == -2 else ""
        raise DmError(f"大漠 Reg 失败 ret={ret} {hint}".strip())
    return dm
