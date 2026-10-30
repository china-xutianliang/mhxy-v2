"""P0 命令行自测：绑定现有查看器，按步骤文件执行后台点击/按键/抓图。

用法（32 位 Python，管理员）：
  python p0_selftest.py steps.txt
步骤文件每行一条，# 开头为注释：
  origin X Y        设置 G→V 偏移（默认 0 0，即 G 坐标等于查看器画面坐标）
  click X Y [right] 在 G 坐标后台点击
  move X Y          只移动鼠标
  key NAME          按键
  wait SEC          等待
  shot NAME         抓整个画面到 runtime/p0_NAME.png
  color X Y         打印 V 坐标处的颜色
结果同时写到 runtime/p0_out.txt。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import winapi  # noqa: E402
from common.config import runtime_dir  # noqa: E402
from control import viewer_link  # noqa: E402
from control.actuator import Actuator  # noqa: E402
from control.channel import Channel  # noqa: E402
from control.dm_factory import make_dm  # noqa: E402
from control.viewport import Viewport  # noqa: E402


def main() -> None:
    winapi.set_dpi_aware()
    out_path = os.path.join(runtime_dir(), "p0_out.txt")
    out = open(out_path, "w", encoding="utf-8")

    def say(msg: str) -> None:
        print(msg)
        out.write(msg + "\n")
        out.flush()

    try:
        vs = viewer_link.find_viewers(None)
        if not vs:
            say("没找到查看器")
            return
        h = vs[0]
        viewer_link.ensure_restored(h)
        time.sleep(0.5)
        w, hh = winapi.client_size(h.child)
        say(f"admin={winapi.is_admin()} 子窗口客户区 {w}x{hh}")
        dm = make_dm()
        ch = Channel(dm)
        ch.bind(h.child)
        vp = Viewport(origin=(0, 0), game_size=(w, hh))
        act = Actuator(ch, vp, sleep=time.sleep)
        steps = sys.argv[1] if len(sys.argv) > 1 else None
        lines = open(steps, encoding="utf-8-sig").read().splitlines() if steps else ["shot default"]
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()
            cmd = p[0]
            if cmd == "origin":
                vp.origin = (int(p[1]), int(p[2]))
            elif cmd == "click":
                act.click(int(p[1]), int(p[2]), p[3] if len(p) > 3 else "left")
                say(f"{time.strftime('%H:%M:%S')} {line}")
            elif cmd == "move":
                act.move_to(int(p[1]), int(p[2]))
            elif cmd == "key":
                act.key_press(p[1])
                say(f"key {p[1]}")
            elif cmd == "wait":
                time.sleep(float(p[1]))
            elif cmd == "shot":
                path = os.path.join(runtime_dir(), f"p0_{p[1]}.png")
                say(f"{time.strftime('%H:%M:%S')} shot {p[1]} -> {ch.capture_png(0, 0, w, hh, path)}")
            elif cmd == "drag":
                # drag X1 Y1 X2 Y2 HOLD_SEC NAME：按下拖到终点，保持 HOLD 秒后抓图再抬起（看橡皮筋框）
                x1, y1, x2, y2 = (vp.g_to_v(int(p[1]), int(p[2])) + vp.g_to_v(int(p[3]), int(p[4])))
                ch.move_to(x1, y1)
                time.sleep(0.4)
                ch.down("left")
                time.sleep(0.3)
                for k in range(1, 11):
                    ch.move_to(x1 + (x2 - x1) * k // 10, y1 + (y2 - y1) * k // 10)
                    time.sleep(0.1)
                time.sleep(float(p[5]))
                path = os.path.join(runtime_dir(), f"p0_{p[6]}.png")
                say(f"{time.strftime('%H:%M:%S')} drag {x1},{y1}->{x2},{y2} shot {p[6]} -> {ch.capture_png(0, 0, w, hh, path)}")
                ch.up("left")
            elif cmd == "glide":
                # glide X Y [STEP_PX] [STEP_MS]：从上一个位置分小步移动到目标（模拟人手）
                tx, ty = vp.g_to_v(int(p[1]), int(p[2]))
                step = int(p[3]) if len(p) > 3 else 12
                ms = int(p[4]) if len(p) > 4 else 12
                cx, cy = getattr(ch, "_last", (tx, ty))
                import math
                dist = math.hypot(tx - cx, ty - cy)
                n = max(1, int(dist // step))
                for k in range(1, n + 1):
                    ch.move_to(cx + (tx - cx) * k // n, cy + (ty - cy) * k // n)
                    time.sleep(ms / 1000.0)
                ch.move_to(tx, ty)
                ch._last = (tx, ty)
                say(f"glide -> {tx},{ty} 共{n}步")
            elif cmd == "combo":
                act.key_combo(p[1], p[2])
                say(f"{time.strftime('%H:%M:%S')} combo {p[1]}+{p[2]}")
            elif cmd == "timing":
                act.pre_press_ms = (int(p[1]), int(p[1]))
                act.hold_ms = (int(p[2]), int(p[2]))
                say(f"timing 移动后停 {p[1]}ms 按住 {p[2]}ms")
            elif cmd == "watch":
                # watch SEC X1 Y1 X2 Y2：每 0.25s 取样一次，打印画面变化的时刻
                from control import guard
                region = tuple(int(v) for v in p[2:6])
                t_end = time.monotonic() + float(p[1])
                last = guard.grid_signature(ch, region, 3)
                changes = []
                t_start = time.monotonic()
                while time.monotonic() < t_end:
                    time.sleep(0.25)
                    cur = guard.grid_signature(ch, region, 3)
                    if cur != last:
                        changes.append(round(time.monotonic() - t_start, 1))
                        last = cur
                say(f"watch {p[1]}s 区域变化发生在(秒): {changes}")
            elif cmd == "front":
                winapi.user32.ShowWindow(h.top, 9)
                winapi.user32.SetForegroundWindow(h.top)
                say(f"front 前台窗口={winapi.foreground()} 查看器={h.top}")
            elif cmd == "color":
                say(f"color {p[1]},{p[2]} = {ch.get_color(int(p[1]), int(p[2]))}")
            else:
                say(f"未知步骤: {line}")
        ch.unbind()
        say("完成")
    except Exception as e:  # noqa: BLE001
        say(f"失败: {type(e).__name__}: {e}")
    finally:
        out.close()


if __name__ == "__main__":
    main()
