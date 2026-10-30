"""大漠后台绑定自测（无交互）：对远程查看器窗口依次测试各绑定模式的 抓图 / 右键 / 左键 / 键盘，
并在"查看器的取样区域被另一个窗口盖住"的条件下重复，判断是否真的是后台可用。

判定依据（对副机桌面空白处，无副作用）：
  1. 右键 -> 弹出系统右键菜单 -> 取样区域大面积变化  => 右键事件到达副机
  2. 左键点远处空白 -> 菜单被点掉 -> 画面恢复基线      => 左键事件到达副机
  3. 再右键弹出菜单，按 Esc -> 菜单消失 -> 画面恢复基线  => 键盘事件到达副机
  副机壁纸若是动态的，取样会有噪声：脚本先连续取 3 次基线，只拿"3 次完全一致"的像素做比较，
  稳定像素太少（<15%）就直接判定"区域太吵，无法判断"，不会再给出假阳性。

安全设计（上一版的教训：全屏置顶遮挡窗在脚本卡住时无法关闭，会把人锁在屏幕外）：
  - 遮挡窗是独立子进程，只盖住取样区域，不是全屏；
  - 点击它或按任意键即可关闭，并且有 TTL 自动关闭，不依赖本脚本是否存活；
  - 本脚本有总时长看门狗，超时自杀；遮挡窗子进程随之自行退出；
  - 默认只测 A、B 两档（不向查看器注入）。C/D/E 会向查看器进程注入，需显式指定 --modes。

必须 32 位 Python + 管理员权限。结果写入 %TEMP%\\mhxy_selftest\\result.log
用法: python dm_selftest.py [--modes AB] [--no-occlude] [--max-seconds 240] [--unaware]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

import win32com.client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.config import load_yaml, resources_path  # noqa: E402

OUT = os.path.join(tempfile.gettempdir(), "mhxy_selftest")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "result.log")

MODES = {
    "A": ("normal", "windows", "windows", "", 0),
    "B": ("normal", "windows3", "windows", "", 0),
    "C": ("gdi", "windows", "windows", "", 0),
    "F": ("gdi", "windows3", "windows", "", 0),
    "G": ("gdi2", "windows3", "windows", "", 0),
    "D": ("gdi2", "windows2", "windows", "", 0),
    "E": ("dx2", "dx.mouse.state.api|dx.mouse.api|dx.mouse.cursor",
          "dx.keypad.state.api|dx.keypad.api",
          "dx.public.active.api|dx.public.active.message|dx.public.hide.dll|"
          "dx.public.graphic.protect|dx.public.anti.api|dx.public.km.protect|dx.public.down.cpu", 101),
}

# 遮挡窗：独立进程，自带关闭手段（点击/按键）和 TTL，不依赖主脚本。
OCCLUDER_CODE = r"""
import sys, ctypes, tkinter as tk
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
x, y, w, h, ttl = (int(v) for v in sys.argv[1:6])
root = tk.Tk()
root.title("遮挡测试窗 - 点击或按任意键关闭")
root.geometry(f"{w}x{h}+{x}+{y}")
root.configure(bg="#00ff00")
root.attributes("-topmost", True)
tk.Label(root, text="遮挡测试窗\n点击或按任意键关闭\n(%d 秒后自动关闭)" % ttl, bg="#00ff00", font=("Segoe UI", 12)).pack(expand=True)
root.bind("<Button>", lambda e: root.destroy())
root.bind("<Key>", lambda e: root.destroy())
root.after(ttl * 1000, root.destroy)
root.mainloop()
"""

user32 = ctypes.windll.user32


class RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


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
        raise RuntimeError(f"大漠 Reg 失败 ret={ret}（-2 需管理员权限）")
    return dm


# 取样区域（查看器客户区坐标）与动作坐标，均落在副机桌面右侧空白壁纸上
REGION = (1500, 600, 2000, 1300)
RIGHT_CLICK_AT = (1600, 700)
LEFT_CLICK_AWAY = (1000, 1000)
STEP = 50


class Ctx:
    def __init__(self, dm, hwnd: int):
        self.dm = dm
        self.hwnd = hwnd
        self.cw = 0
        self.ch = 0
        self.occ: subprocess.Popen | None = None
        self.region = REGION

    def sample(self) -> list[str]:
        x1, y1, x2, y2 = self.region
        return [self.dm.GetColor(x, y) for y in range(y1, y2, STEP) for x in range(x1, x2, STEP)]


def masked_diff(a: list[str], b: list[str], idx: list[int]) -> float:
    if not idx:
        return -1.0
    return sum(1 for i in idx if a[i] != b[i]) / len(idx)


def ensure_restored(ctx: Ctx) -> None:
    """最小化的查看器不刷新画面、客户区为 0，先还原（不激活，不抢焦点）。"""
    for _ in range(3):
        if user32.IsIconic(ctx.hwnd):
            log("  查看器处于最小化，还原中...")
            user32.ShowWindow(ctx.hwnd, 4)  # SW_SHOWNOACTIVATE
            time.sleep(3.0)
        else:
            break


def start_occluder(ctx: Ctx, ttl: int = 90) -> None:
    """只盖住取样区域，位置换算成屏幕坐标。"""
    r = RECT()
    user32.GetWindowRect(ctx.hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(ctx.hwnd, ctypes.byref(pt))
    x1, y1, x2, y2 = ctx.region
    x, y, w, h = pt.x + x1 - 40, pt.y + y1 - 40, (x2 - x1) + 80, (y2 - y1) + 80
    ctx.occ = subprocess.Popen(
        [sys.executable, "-c", OCCLUDER_CODE, str(x), str(y), str(w), str(h), str(ttl)],
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    time.sleep(1.5)


def stop_occluder(ctx: Ctx) -> None:
    if ctx.occ is not None:
        try:
            ctx.occ.terminate()
        except OSError:
            pass
        ctx.occ = None
        time.sleep(0.8)


def suite(ctx: Ctx, tag: str) -> dict:
    dm = ctx.dm
    res = {"capture": False, "reads_screen": False, "usable": False,
           "right": False, "left": False, "key": False, "stable": 0.0}

    b1 = ctx.sample()
    time.sleep(0.4)
    b2 = ctx.sample()
    time.sleep(0.4)
    b3 = ctx.sample()
    stable = [i for i in range(len(b1)) if b1[i] == b2[i] == b3[i]]
    res["stable"] = round(len(stable) / max(1, len(b1)), 2)
    # 取样区在纯色壁纸上颜色种类天然很少，抓图是否可用要看整个客户区
    whole = {dm.GetColor(x, y) for y in range(20, ctx.ch, 90) for x in range(20, ctx.cw, 90)}
    distinct = len(whole)
    dm.CapturePng(0, 0, ctx.cw, ctx.ch, os.path.join(OUT, f"{tag}_base.png"))
    nonblack = sum(1 for c in b1 if c not in ("000000", "")) / max(1, len(b1))
    green = sum(1 for c in b1 if c.lower() == "00ff00") / max(1, len(b1))
    res["capture"] = distinct > 8 and green < 0.5
    res["reads_screen"] = green > 0.5
    log(f"  [{tag}] 抓图: distinct={distinct} nonblack={nonblack:.2f} green={green:.2f} 稳定像素={res['stable']}")
    if not res["capture"]:
        log(f"  [{tag}] 抓图不可用，跳过输入测试")
        return res
    if res["stable"] < 0.15:
        log(f"  [{tag}] 取样区域太吵（壁纸在动），无法判断输入是否到达。请把副机壁纸改成纯色后重测")
        return res
    res["usable"] = True
    base = b1

    n_rc = [0]

    def poll(want_changed: bool, timeout: float = 8.0) -> tuple[float, float]:
        """轮询取样直到与基线的差异满足条件，返回 (差异, 耗时)。远程画面有延迟，不能只看固定等待后的一帧。"""
        t0 = time.time()
        d = masked_diff(base, ctx.sample(), stable)
        while time.time() - t0 < timeout:
            if (d > 0.05) == want_changed:
                break
            time.sleep(0.4)
            d = masked_diff(base, ctx.sample(), stable)
        return d, time.time() - t0

    def right_click() -> None:
        n_rc[0] += 1
        dm.MoveTo(RIGHT_CLICK_AT[0] + 40 * n_rc[0], RIGHT_CLICK_AT[1] + 30 * n_rc[0])
        time.sleep(0.4)
        dm.RightClick()

    def clean() -> None:
        dm.KeyPressChar("esc")
        time.sleep(0.4)
        dm.MoveTo(*LEFT_CLICK_AWAY)
        time.sleep(0.2)
        dm.LeftClick()
        poll(False, 4.0)

    try:
        right_click()
        d1, t1 = poll(True)
        res["right"] = d1 > 0.05
        log(f"  [{tag}] 右键: 变化率={d1:.3f} 耗时{t1:.1f}s -> {'到达' if res['right'] else '无反应'}")
        if res["right"]:
            dm.CapturePng(0, 0, ctx.cw, ctx.ch, os.path.join(OUT, f"{tag}_menu.png"))
            dm.MoveTo(*LEFT_CLICK_AWAY)
            time.sleep(0.2)
            dm.LeftClick()
            d2, t2 = poll(False)
            res["left"] = d2 <= 0.05
            log(f"  [{tag}] 左键点别处关菜单: 与基线差异={d2:.3f} 耗时{t2:.1f}s -> {'到达' if res['left'] else '菜单未关闭'}")

            right_click()
            d3, t3 = poll(True)
            if d3 > 0.05:
                dm.KeyPressChar("esc")
                d4, t4 = poll(False)
                res["key"] = d4 <= 0.05
                log(f"  [{tag}] Esc 关菜单: 与基线差异={d4:.3f} 耗时{t4:.1f}s -> {'到达' if res['key'] else '键盘未生效'}")
            else:
                log(f"  [{tag}] 第二次右键没弹出菜单（{t3:.1f}s），键盘未测")
    finally:
        clean()
    return res


def run_mode(ctx: Ctx, key: str, occlude: bool) -> dict:
    dm = ctx.dm
    display, mouse, keypad, public, mode = MODES[key]
    log(f"===== 模式 {key}: display={display} mouse={mouse} keypad={keypad} mode={mode}")
    dm.UnBindWindow()
    ensure_restored(ctx)
    t0 = time.time()
    ret = dm.BindWindowEx(ctx.hwnd, display, mouse, keypad, public, mode)
    if ret != 1:
        log(f"  Bind 失败 ret={ret} err={dm.GetLastError()}")
        return {"bind": False}
    log(f"  Bind 成功 ({time.time() - t0:.1f}s)")
    dm.EnableRealKeypad(1)
    _, cw, ch = dm.GetClientSize(ctx.hwnd)
    ctx.cw, ctx.ch = cw, ch
    log(f"  GetClientSize -> {cw}x{ch}")
    if cw <= 0 or ch <= 0:
        log("  客户区为 0，本模式无法测试（窗口仍是最小化？）")
        dm.UnBindWindow()
        return {"bind": False, "reason": "client 0x0"}
    out = {"bind": True, "client": f"{cw}x{ch}"}
    time.sleep(0.8)
    user32.SwitchToThisWindow(ctx.hwnd, True)
    time.sleep(1.0)
    log(f"  前台窗口==查看器: {user32.GetForegroundWindow() == ctx.hwnd}")
    out["visible"] = suite(ctx, f"{key}-可见")
    if occlude:
        start_occluder(ctx)
        try:
            log(f"  遮挡窗进程存活={ctx.occ.poll() is None} 前台==查看器: {user32.GetForegroundWindow() == ctx.hwnd}")
            out["occluded"] = suite(ctx, f"{key}-被遮挡")
        finally:
            stop_occluder(ctx)
    dm.UnBindWindow()
    time.sleep(1.0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="AB")
    ap.add_argument("--unaware", action="store_true")
    ap.add_argument("--no-occlude", action="store_true")
    ap.add_argument("--max-seconds", type=int, default=240)
    ap.add_argument("--cls", default="tvnviewer-session-window")
    ap.add_argument("--child", action="store_true", help="绑定查看器内部的画面子窗口而不是顶层窗口")
    args = ap.parse_args()

    open(LOG, "w", encoding="utf-8").close()
    # 看门狗：无论卡在哪个 COM 调用里，到点强制退出，遮挡窗子进程自带 TTL 也会自行消失
    threading.Timer(args.max_seconds, lambda: (log("!! 总时长看门狗触发，强制退出"), os._exit(3))).start()

    if not args.unaware:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    log(f"自测开始 dpi_aware={not args.unaware} modes={args.modes} occlude={not args.no_occlude}")
    dm = make_dm()
    log(f"dm ver={dm.Ver()}")
    hwnd = dm.FindWindow(args.cls, "")
    if not hwnd:
        log("没找到查看器窗口")
        os._exit(1)
    ctx = Ctx(dm, hwnd)
    ensure_restored(ctx)
    if args.child:
        ch = user32.FindWindowExW(wintypes.HWND(hwnd), None, args.cls, None)
        log(f"子窗口 {ch}（顶层 {hwnd}）")
        if ch:
            ctx.hwnd = ch
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    log(f"viewer hwnd={hwnd} window={r.r - r.l}x{r.b - r.t} at ({r.l},{r.t})")

    summary = {}
    for k in args.modes.upper():
        if k not in MODES:
            continue
        try:
            summary[k] = run_mode(ctx, k, occlude=not args.no_occlude)
        except Exception as e:  # noqa: BLE001
            log(f"  !! 模式 {k} 异常: {type(e).__name__}: {e}")
            stop_occluder(ctx)
            summary[k] = {"error": str(e)}

    log("===== 汇总 =====")
    for k, v in summary.items():
        if not v.get("bind", False):
            log(f"  {k}: 绑定失败/异常 {v}")
            continue
        for ph in ("visible", "occluded"):
            s = v.get(ph)
            if not s:
                continue
            verdict = "" if s["usable"] else "  (无结论: " + ("抓图失败" if not s["capture"] else "区域太吵") + ")"
            log(f"  {k} {ph:<8}: capture={s['capture']} right={s['right']} left={s['left']} key={s['key']}"
                f"{'  (读的是屏幕)' if s['reads_screen'] else ''}{verdict}")
    log("完成")
    os._exit(0)


if __name__ == "__main__":
    main()
