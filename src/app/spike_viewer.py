"""阶段0.5 实测脚本：探测大漠能否抓取并操作主控机上的远程查看器窗口。

只在主控机运行，不触碰副机上的任何进程。用 32 位 Python 运行（dm.dll 是 32 位）。

用法：
    python spike_viewer.py [--aware] [--tier safe|inject] [--class CLS] [--title TXT]

    --aware   让本进程声明 DPI 感知（per-monitor v2）。主控机缩放不是 100% 时，
              对比有无此参数下 GetClientSize 的结果，判断大漠看到的是不是物理像素。
    --tier    safe   : 只测不注入查看器进程的模式（display=normal，鼠标/键盘走窗口消息）
              inject : 另测 gdi/gdi2/dx2 等会向查看器进程注入的模式（有让查看器崩溃的风险）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.config import load_yaml, resources_path  # noqa: E402

OUT_DIR = os.path.join(tempfile.gettempdir(), "mhxy_spike")


def set_dpi_aware() -> None:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def make_dm():
    raw = load_yaml("config", "driver.yaml")
    dmreg = ctypes.WinDLL(resources_path("dll", raw["dll"]["dmreg_file"]))
    dmreg.SetDllPathW.argtypes = [wintypes.LPCWSTR, wintypes.LONG]
    dmreg.SetDllPathW.restype = wintypes.LONG
    if dmreg.SetDllPathW(resources_path("dll", raw["dll"]["dm_file"]), 0) != 1:
        raise RuntimeError("SetDllPathW 失败")
    dm = win32com.client.Dispatch("dm.dmsoft")
    ret = dm.Reg(raw["auth"]["reg_code"], raw["auth"]["ver_info"])
    if ret != 1:
        raise RuntimeError(f"大漠注册失败，返回码 {ret}（-2 表示需要管理员权限运行）")
    return dm


def snap(dm, name: str, x1: int, y1: int, x2: int, y2: int) -> str:
    path = os.path.join(OUT_DIR, name)
    ret = dm.CapturePng(x1, y1, x2, y2, path)
    return path if ret == 1 else ""


def file_sig(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def try_bind(dm, hwnd: int, display: str, mouse: str, keypad: str, public: str, mode: int) -> int:
    dm.UnBindWindow()
    return dm.BindWindowEx(hwnd, display, mouse, keypad, public, mode)


def run_case(dm, hwnd: int, label: str, display: str, mouse: str, keypad: str, public: str, mode: int, cw: int, ch: int):
    print(f"\n=== {label}: display={display} mouse={mouse} keypad={keypad} mode={mode}")
    ret = try_bind(dm, hwnd, display, mouse, keypad, public, mode)
    print(f"  bind ret={ret} lasterr={dm.GetLastError() if ret != 1 else 0}")
    if ret != 1:
        return
    dm.delay(500)

    region = (0, 0, min(cw, 600), min(ch, 400))
    base = snap(dm, f"{label}_base.png", *region)
    print(f"  capture base: {'OK ' + base if base else 'FAIL'}")

    # 鼠标移动探针：在查看器客户区里扫一小片网格，看画面是否因悬停高亮而变化。
    # 只做移动，不点击，对副机无副作用。
    base_bytes = file_sig(base) if base else b""
    changed = []
    for gy in range(40, 260, 40):
        for gx in range(20, 200, 40):
            dm.MoveTo(gx, gy)
            dm.delay(250)
            p = snap(dm, f"{label}_hover.png", *region)
            if p and file_sig(p) != base_bytes:
                changed.append((gx, gy))
    print(f"  hover probe: {len(changed)} of 30 points changed the picture {changed[:6]}")
    dm.MoveTo(5, 5)
    dm.UnBindWindow()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aware", action="store_true")
    ap.add_argument("--tier", default="safe", choices=["safe", "inject"])
    ap.add_argument("--class", dest="cls", default="tvnviewer-session-window")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    if args.aware:
        set_dpi_aware()
    os.makedirs(OUT_DIR, exist_ok=True)

    dm = make_dm()
    print("dm version:", dm.Ver(), "| aware:", args.aware)

    hwnd = dm.FindWindow(args.cls, args.title)
    if not hwnd:
        print("没找到查看器窗口，检查 --class/--title")
        return
    print("viewer hwnd:", hwnd)

    ret, cw, ch = dm.GetClientSize(hwnd)
    print(f"GetClientSize ret={ret} -> {cw}x{ch}")
    ret, x1, y1, x2, y2 = dm.GetWindowRect(hwnd)
    print(f"GetWindowRect ret={ret} -> ({x1},{y1})-({x2},{y2})  {x2 - x1}x{y2 - y1}")

    run_case(dm, hwnd, "A_normal_windows", "normal", "windows", "windows", "", 0, cw, ch)
    run_case(dm, hwnd, "B_normal_windows3", "normal", "windows3", "windows", "", 0, cw, ch)

    if args.tier == "inject":
        run_case(dm, hwnd, "C_gdi_windows", "gdi", "windows", "windows", "", 0, cw, ch)
        run_case(dm, hwnd, "D_gdi2_windows", "gdi2", "windows", "windows", "", 0, cw, ch)
        cfg = load_yaml("config", "driver.yaml")["bind"]
        run_case(dm, hwnd, "E_dx2_dxmouse", cfg["display"], cfg["mouse"], cfg["keypad"], cfg["public"], cfg["mode"], cw, ch)

    print("\n截图输出目录:", OUT_DIR)


if __name__ == "__main__":
    main()
