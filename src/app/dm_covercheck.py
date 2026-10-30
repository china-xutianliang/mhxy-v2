"""诊断：查看器被其他窗口盖住时，各显示模式抓到的是查看器内容还是上层窗口。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
import dm_selftest as st  # noqa: E402

threading.Timer(120, lambda: os._exit(3)).start()
st.user32.SetProcessDpiAwarenessContext(st.ctypes.c_void_p(-4))
dm = st.make_dm()
hwnd = st.user32.FindWindowW("tvnviewer-session-window", None)
foreground = st.user32.GetForegroundWindow()
out = os.path.join(st.OUT, "cover")
os.makedirs(out, exist_ok=True)
lines = [f"viewer={hwnd} foreground={foreground} iconic={st.user32.IsIconic(hwnd)}"]
for key, disp in (("normal", "normal"), ("gdi", "gdi"), ("gdi2", "gdi2")):
    dm.UnBindWindow()
    ret = dm.BindWindowEx(hwnd, disp, "windows3", "windows", "", 0)
    t0 = time.time()
    dm.CapturePng(0, 0, 1200, 700, os.path.join(out, f"{key}.png"))
    lines.append(f"{key}: bind={ret} capture {time.time() - t0:.1f}s")
dm.UnBindWindow()
open(os.path.join(out, "log.txt"), "w", encoding="utf-8").write("\n".join(lines))
os._exit(0)
