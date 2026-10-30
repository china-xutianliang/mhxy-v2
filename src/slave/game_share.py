"""副机共享助手（图形界面版）：让 TightVNC 只共享游戏客户区。

为什么要有它：副机桌面是 2560x1440，游戏只占 800x600。整桌面共享既费带宽，
主控机还得额外算偏移。这个工具把共享范围收到游戏客户区，主控机拿到的画面
就是 800x600 的游戏画面，坐标一一对应（见 控制层方案.md 第 4 节）。

它只做两件事：
  1. 用 Win32 只读接口找游戏窗口，得到客户区在屏幕上的位置（不读内存、不发消息）；
  2. 调用 TightVNC 自带的命令行
         tvnserver.exe -controlservice -sharerect 800x600+X+Y
不注入、不装驱动、不碰游戏进程，符合副机"零注入"约束。

用法（测试阶段，手动启动）：
  双击 game_share.exe → UAC 点"是" → 界面里点"开始共享"。
  用完了点"停止共享（恢复整桌面）"，或者直接关掉窗口。

命令行模式（调试或以后做自动化时用）：
  game_share.exe --once --dry-run     只打印将要执行的事，不下发
  game_share.exe --console            无界面的守护循环
  game_share.exe --passfile C:\\tvn_pass.txt
"""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 本目录自带 common/，不依赖主项目

from common import winapi  # noqa: E402
from common.inspector import InspectorFrame  # noqa: E402

TVN_CANDIDATES = (r"C:\Program Files\TightVNC\tvnserver.exe", r"C:\Program Files (x86)\TightVNC\tvnserver.exe")
DEFAULT_TVN = ""
GAME_SIZE = (800, 600)
RETRY_AFTER = 10.0  # 下发失败后,同一个矩形至少隔这么多秒再试
DATA_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "mhxy")
TVN_PATH_FILE = os.path.join(DATA_DIR, "tvn_path.txt")
DEFAULT_PASSFILE = os.path.join(DATA_DIR, "tvn_pass.txt")
LOG_PATH = os.path.join(DATA_DIR, "game_share.log")
LOG_MAX_BYTES = 512 * 1024
CREATE_NO_WINDOW = 0x08000000
COLOR_OK, COLOR_BAD, COLOR_WARN, COLOR_INFO = "#1e8449", "#c0392b", "#b9770e", "#3f4a5a"


# ---------------------------------------------------------------- 公共小工具
def now() -> str:
    return time.strftime("%H:%M:%S")


def say(msg: str) -> None:
    """无界面模式用：打印并写日志。"""
    if sys.stdout is not None:
        try:
            print(msg, flush=True)
        except (OSError, ValueError):
            pass
    write_log(msg)


def write_log(msg: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _exe_from_command_line(cmd: str) -> str:
    """服务注册表里的 ImagePath 可能带引号和参数，取出 exe 路径。"""
    cmd = os.path.expandvars(cmd.strip())
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    low = cmd.lower()
    i = low.find(".exe")
    return cmd[:i + 4] if i >= 0 else cmd


def _registry_tvn_paths() -> list[str]:
    """从服务项和卸载信息里找 TightVNC 的安装位置（不管装在哪个盘都能找到）。"""
    try:
        import winreg
    except ImportError:
        return []
    found: list[str] = []
    for name in ("tvnserver", "TightVNC Server"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}") as k:
                found.append(_exe_from_command_line(str(winreg.QueryValueEx(k, "ImagePath")[0])))
        except OSError:
            pass
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                0, winreg.KEY_READ | view) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                            disp = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if "tightvnc" not in disp.lower():
                                continue
                            loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                            if loc.strip():
                                found.append(os.path.join(loc, "tvnserver.exe"))
                    except OSError:
                        continue
        except OSError:
            pass
    return found


def load_saved_tvn() -> str:
    try:
        with open(TVN_PATH_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_tvn(path: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TVN_PATH_FILE, "w", encoding="utf-8") as f:
            f.write(path)
    except OSError:
        pass


def find_tvnserver(preferred: str = "") -> str:
    """找 tvnserver.exe：用户指定/保存的 → 默认路径 → 正在运行的进程 → 服务注册表 → 卸载信息。
    都找不到时返回空字符串。"""
    cands = [preferred, load_saved_tvn(), *TVN_CANDIDATES]
    try:
        cands += winapi.process_paths("tvnserver.exe")
    except Exception:  # noqa: BLE001
        pass
    cands += _registry_tvn_paths()
    for p in cands:
        if p and os.path.isabs(p) and os.path.isfile(p):
            return p
    return ""


def screen_size() -> tuple[int, int]:
    u = ctypes.windll.user32
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def fmt_rect(rect: tuple[int, int, int, int] | None) -> str:
    if not rect:
        return "—"
    x, y, w, h = rect
    return f"{w}x{h}+{x}+{y}"


# ---------------------------------------------------------------- 找游戏窗口
TITLE_KEYWORD = "梦幻西游"
FRAME_CLASS = "MHXYMainFrame"  # 游戏主窗口（顶层，进程是 mhtab.exe）；画面在它里面的 WSGAME 子窗口
GAME_EXES = ("mhmain.exe", "mhtab.exe")


def _exe_of(h: int) -> str:
    return os.path.basename(winapi.image_path(winapi.pid_of(h))).lower()


def find_game_window(cls: str, exe_name: str = "", title_kw: str = TITLE_KEYWORD) -> int:
    """找游戏画面窗口；找不到返回 0。全程只读窗口信息，不进入游戏进程。

    实测窗口树：顶层 MHXYMainFrame（进程 mhtab.exe）里面有子窗口 WSGAME，画面在 WSGAME 里。
    所以对每个可见的顶层窗口，连同它的可见子窗口一起看：类名等于 cls 得 4 分；
    标题含 title_kw 且客户区正好 800x600 也得 4 分（排除浏览器标签页）；进程名是游戏进程再加 2 分；
    客户区正好 800x600 再加 1 分。至少 4 分才算命中，取分最高的。
    """
    exes = {e.lower() for e in GAME_EXES}
    if exe_name:
        exes.add(exe_name.lower())
    best, best_score = 0, 0

    def consider(h: int) -> None:
        nonlocal best, best_score
        w, hgt = winapi.client_size(h)
        if w <= 0 or hgt <= 0:
            return
        score = 0
        if cls and winapi.class_name(h) == cls:
            score += 4
        if title_kw and title_kw in winapi.title(h) and (w, hgt) == GAME_SIZE:
            score += 4
        if not score:
            return
        if _exe_of(h) in exes:
            score += 2
        if (w, hgt) == GAME_SIZE:
            score += 1
        if score > best_score:
            best, best_score = h, score

    for top in winapi.enum_top_windows():
        if not winapi.user32.IsWindowVisible(top):
            continue
        consider(top)
        for ch in winapi.enum_children(top):
            if winapi.user32.IsWindowVisible(ch):
                consider(ch)
    return best


def list_candidates(limit: int = 25) -> list[str]:
    """诊断用：列出可见的较大窗口（客户区至少 400x300）和它们的子窗口，按面积从大到小。"""
    rows = []

    def add(h: int, prefix: str) -> None:
        w, hgt = winapi.client_size(h)
        if w < 400 or hgt < 300:
            return
        rows.append((w * hgt, f"{prefix}句柄={h}　类名={winapi.class_name(h)}　进程={_exe_of(h) or '读不到'}　"
                              f"标题={winapi.title(h)[:30] or '（空）'}　客户区={w}x{hgt}"))

    for h in winapi.enum_top_windows():
        if not winapi.user32.IsWindowVisible(h):
            continue
        add(h, "")
        for ch in winapi.enum_children(h):
            if winapi.user32.IsWindowVisible(ch):
                add(ch, "　└子窗口 ")
    rows.sort(key=lambda r: -r[0])
    return [r[1] for r in rows[:limit]]


def client_rect_on_screen(h: int) -> tuple[int, int, int, int] | None:
    """(x, y, w, h)；窗口最小化或尺寸为 0 时返回 None。"""
    if winapi.is_iconic(h):
        return None
    w, hgt = winapi.client_size(h)
    if w <= 0 or hgt <= 0:
        return None
    x, y = winapi.client_origin_on_screen(h)
    return x, y, w, hgt


# ---------------------------------------------------------------- 下发命令
def build_command(tvn: str, rect: tuple[int, int, int, int], passfile: str | None,
                  mode: str = "service") -> list[str]:
    """mode: service=TightVNC 以服务方式运行(-controlservice);app=以普通程序运行(-controlapp)。"""
    x, y, w, h = rect
    cmd = [tvn, "-controlapp" if mode == "app" else "-controlservice", "-sharerect", f"{w}x{h}+{x}+{y}"]
    if passfile:
        cmd += ["-passfile", passfile]
    return cmd


def run_command(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """执行 tvnserver 命令，返回 (退出码, 输出)。进程启动失败时退出码为 -1。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def file_version(path: str) -> tuple[int, int, int, int] | None:
    """读 exe 的文件版本号;读不到返回 None。"""
    try:
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr, ln = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)) or ln.value < 52:
            return None
        info = (wintypes.DWORD * 13).from_address(ptr.value)
        ms, ls = info[4], info[5]
        return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF
    except (OSError, ValueError, AttributeError):
        return None


def tvn_lacks_sharerect(path: str) -> str:
    """TightVNC 4.x 删除了 -sharerect。是 4.x 返回版本字符串,否则返回空串。"""
    v = file_version(path)
    return ".".join(map(str, v)) if v and v[0] >= 4 else ""


def service_installed() -> bool:
    try:
        import winreg
        for name in ("tvnserver", "TightVNC Server"):
            try:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                               rf"SYSTEM\CurrentControlSet\Services\{name}"))
                return True
            except OSError:
                continue
    except ImportError:
        pass
    return False


def kill_stuck_control() -> None:
    """超时的控制命令会留下卡住的 tvnserver 子进程和 "Failed to connect" 弹窗,逐次累积。
    只杀命令行带 controlservice/controlapp 的进程,不碰真正的服务(-service)和桌面进程(-desktopserver)。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='tvnserver.exe'\" | "
          "Where-Object { $_.CommandLine -match 'control(service|app)' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                       creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


_last_good: dict = {}


def apply_rect(tvn: str, rect: tuple[int, int, int, int], passfile: str | None,
               prefer: str = "", timeout: float = 5.0) -> tuple[int, str, str]:
    """下发共享区域。TightVNC 以服务或程序方式运行,命令不同且不回报结果,所以两种都试;
    有管理密码文件时先试"不带密码文件"(服务端没开控制密码时,带着密码文件反而连不上),不行再带上。
    返回 (退出码, 说明, 成功的模式)。失败时说明里列出每种方式的结果。"""
    first = prefer or ("service" if service_installed() else "app")
    order = [first, "app" if first == "service" else "service"]
    combos = [(m, pf) for m in order for pf in ([None, passfile] if passfile else [None])]
    good = _last_good.get("combo")
    if good in combos:
        combos.remove(good)
        combos.insert(0, good)
    notes = []
    rc = -1
    for mode, pf in combos:
        rc, out = run_command(build_command(tvn, rect, pf, mode), timeout=timeout)
        if rc == 0:
            _last_good["combo"] = (mode, pf)
            return 0, "", mode
        notes.append(f"{mode}方式{'(带密码文件)' if pf else ''}:退出码{rc}" + (f"({out})" if out else ""))
        kill_stuck_control()
    return rc, "；".join(notes), ""


def diagnose_tvn(tvn: str | None) -> list[str]:
    """控制命令连不上 TightVNC 时,收集足够判断原因的信息:服务状态、运行中的 tvnserver 进程、控制管道。"""
    out: list[str] = [f"本程序{'是' if winapi.is_admin() else '不是'}管理员身份;选用的 tvnserver = {tvn or '没找到'}"]
    for name in ("tvnserver", "TightVNC Server"):
        try:
            r = subprocess.run(["sc", "query", name], capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=8,
                               creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            state = next((ln.strip() for ln in r.stdout.splitlines() if "STATE" in ln), "?")
            out.append(f"服务 {name}: {state}")
            break
    else:
        out.append("没有注册 TightVNC 服务(说明它是以普通程序方式在运行,或根本没在运行)")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='tvnserver.exe'\" | ForEach-Object "
          "{ \"pid=$($_.ProcessId) 会话=$($_.SessionId) 路径=$($_.ExecutablePath) 参数=$($_.CommandLine)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           encoding="mbcs", errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        out += [f"运行中的 tvnserver: {ln}" for ln in lines] or ["当前没有 tvnserver.exe 进程在运行"]
    except (OSError, subprocess.TimeoutExpired):
        out.append("读取 tvnserver 进程失败")
    try:
        pipes = [n for n in os.listdir("\\\\.\\pipe\\") if "tight" in n.lower() or "tvn" in n.lower()]
    except OSError:
        pipes = []
    out.append("控制管道: " + (", ".join(pipes) if pipes else "没有找到(服务端的控制接口没开)"))
    return out


def save_passfile(text: str) -> str | None:
    """把管理密码写成 TightVNC 要的 ASCII 文件，并限制成仅管理员可读。返回错误说明或 None。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DEFAULT_PASSFILE, "w", encoding="ascii", newline="") as f:
            f.write(text)
    except (OSError, UnicodeEncodeError) as e:
        return f"写密码文件失败：{e}"
    try:
        subprocess.run(["icacls", DEFAULT_PASSFILE, "/inheritance:r",
                        "/grant:r", "*S-1-5-32-544:(R,W)", "*S-1-5-18:(R)"],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass  # 限制失败也还能用，只是别的账户可能读得到，界面上会提示
    return None


def clear_passfile() -> None:
    try:
        os.remove(DEFAULT_PASSFILE)
    except OSError:
        pass


# ---------------------------------------------------------------- 提权
def relaunch_as_admin() -> bool:
    """以管理员身份重新启动自己（弹一次 UAC）。成功返回 True。"""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
        workdir = os.path.dirname(sys.executable)
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}"'
        workdir = os.path.dirname(os.path.abspath(__file__))
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
    except OSError:
        return False
    return rc > 32


# ---------------------------------------------------------------- 界面
class SlaveApp:
    """副机上的小工具界面：状态一眼看懂，两个大按钮就够用。"""

    def __init__(self, cls: str = "WSGAME", exe: str = "mhmain.exe",
                 tvn: str = DEFAULT_TVN, interval: float = 1.0, reapply: float = 30.0) -> None:
        self.cls, self.exe = cls, exe
        self.interval, self.reapply = interval, reapply
        self.tvn = find_tvnserver(tvn)
        self.sharing = False
        self.follow = True
        self.game_hwnd = 0
        self.rect: tuple[int, int, int, int] | None = None
        self.last_rect: tuple[int, int, int, int] | None = None
        self.last_apply = 0.0
        self.fail_until = 0.0  # 失败退避:这个时刻之前,自动流程不再重复下发
        self.last_err = ""
        self.mode = ""  # 上次成功的下发方式:service / app
        self.applying = False
        self.last_dump = 0.0
        self.pinned = 0  # 手动指定的游戏画面窗口句柄；0 表示自动查找
        self.q: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("梦幻西游 副机共享助手")
        self.ui = max(1.0, winapi.user32.GetDpiForSystem() / 96.0) if hasattr(winapi.user32, "GetDpiForSystem") else 1.0
        self.root.tk.call("tk", "scaling", self.ui * 96 / 72)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(int(1040 * self.ui), int(sw * 0.92)), min(int(720 * self.ui), int(sh * 0.9))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(int(600 * self.ui), int(480 * self.ui))
        self._fonts()
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(400, self.tick)

    # ---- 外观
    def _fonts(self) -> None:
        fam = "Microsoft YaHei UI"
        try:
            if fam not in tkfont.families(self.root):
                fam = tkfont.nametofont("TkDefaultFont").cget("family")
        except tk.TclError:
            fam = "Segoe UI"
        for name, size, weight in (("TkDefaultFont", 11, "normal"), ("TkTextFont", 11, "normal"),
                                   ("TkMenuFont", 11, "normal"), ("TkHeadingFont", 12, "bold"),
                                   ("TkCaptionFont", 12, "bold"), ("TkSmallCaptionFont", 10, "normal")):
            try:
                tkfont.nametofont(name).configure(family=fam, size=size, weight=weight)
            except tk.TclError:
                pass
        self.f_base = (fam, 11)
        self.f_small = (fam, 10)
        self.f_bold = (fam, 11, "bold")
        self.f_banner = (fam, 14, "bold")
        self.f_big = (fam, 15, "bold")

    def _style(self) -> None:
        st = ttk.Style(self.root)
        st.configure("TButton", padding=(12, 8))
        st.configure("TLabelframe.Label", font=self.f_bold)

    def _big_button(self, parent, text, command, bg="#2d6cdf") -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=self.f_big, bg=bg, fg="white",
                         activebackground="#1b4fa8", activeforeground="white", relief="flat",
                         cursor="hand2", padx=int(16 * self.ui), pady=int(10 * self.ui))

    # ---- 布局
    def _build(self) -> None:
        self.v_state = tk.StringVar(value="准备中……")
        self.banner = tk.Label(self.root, textvariable=self.v_state, font=self.f_banner, bg=COLOR_INFO, fg="white",
                               anchor="w", justify="left", padx=int(14 * self.ui), pady=int(12 * self.ui),
                               wraplength=int(680 * self.ui))
        self.banner.pack(fill="x")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        body = ttk.Frame(nb, padding=12)
        nb.add(body, text="共享")
        self.inspector = InspectorFrame(nb, on_pin=self.pin_window, on_unpin=self.unpin_window)
        nb.add(self.inspector, text="窗口检查器")

        btns = ttk.Frame(body)
        btns.pack(fill="x")
        self.btn_start = self._big_button(btns, "开始共享", self.do_start, bg="#1e8449")
        self.btn_start.pack(side="left")
        self.btn_stop = self._big_button(btns, "停止共享（恢复整桌面）", self.do_stop, bg="#7f8c8d")
        self.btn_stop.pack(side="left", padx=10)
        ttk.Button(btns, text="立即重新下发", command=self.do_apply_now).pack(side="left", padx=6)
        ttk.Button(btns, text="诊断：列出窗口", command=self.do_diagnose).pack(side="left")
        ttk.Button(btns, text="选择 tvnserver.exe…", command=self.choose_tvn).pack(side="left", padx=6)
        ttk.Button(btns, text="诊断：TightVNC", command=self.do_diag_tvn).pack(side="left")

        info = ttk.LabelFrame(body, text="状态", padding=10)
        info.pack(fill="x", pady=10)
        self.v_window = tk.StringVar(value="—")
        self.v_client = tk.StringVar(value="—")
        self.v_share = tk.StringVar(value="—")
        self.v_last = tk.StringVar(value="还没有下发过")
        self.v_pass = tk.StringVar(value="未设置（通常不需要）")
        self.v_tvn = tk.StringVar(value=self.tvn or "没找到 tvnserver.exe，请点“选择 tvnserver.exe…”")
        rows = (("游戏窗口", self.v_window), ("游戏客户区", self.v_client),
                ("当前共享区域", self.v_share), ("上次下发", self.v_last), ("管理密码", self.v_pass),
                ("TightVNC", self.v_tvn))
        for i, (lab, var) in enumerate(rows):
            ttk.Label(info, text=lab, font=self.f_bold, width=12).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(info, textvariable=var, font=self.f_base).grid(row=i, column=1, sticky="w", pady=2)

        opts = ttk.Frame(body)
        opts.pack(fill="x")
        self.v_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="自动跟随游戏窗口（挪窗口、切场景后自动重新下发）",
                        variable=self.v_follow, command=self.on_follow).pack(side="left")
        ttk.Button(opts, text="设置 TightVNC 密码…", command=self.open_pass_dialog).pack(side="right")
        ttk.Button(opts, text="打开日志文件夹", command=self.open_log_dir).pack(side="right", padx=8)

        logf = ttk.LabelFrame(body, text="日志", padding=8)
        logf.pack(fill="both", expand=True, pady=10)
        self.log_text = tk.Text(logf, height=10, font=self.f_small, state="disabled", wrap="word")
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(body, text="用完之后点“停止共享（恢复整桌面）”或直接关掉窗口。本工具不安装任何服务、不开机自启。",
                  font=self.f_small, foreground="#555555", wraplength=int(660 * self.ui),
                  justify="left").pack(anchor="w")

        self.log(f"共享助手已启动　tvnserver={self.tvn or '没找到'}")
        if not winapi.is_admin():
            self.log("警告：当前不是管理员，下发共享范围大概率会失败。")

    # ---- 小操作
    def log(self, msg: str) -> None:
        line = f"[{now()}] {msg}"
        write_log(line)
        try:
            self.log_text.config(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            pass

    def set_banner(self, text: str, level: str = "info") -> None:
        self.v_state.set(text)
        self.banner.config(bg={"ok": COLOR_OK, "bad": COLOR_BAD, "warn": COLOR_WARN}.get(level, COLOR_INFO))

    def on_follow(self) -> None:
        self.follow = self.v_follow.get()
        self.log(f"自动跟随：{'开' if self.follow else '关'}")

    def open_log_dir(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            os.startfile(DATA_DIR)  # noqa: S606  （副机本地打开资源管理器）
        except OSError as e:
            self.log(f"打不开日志文件夹：{e}")

    def open_pass_dialog(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("设置 TightVNC 管理密码")
        top.transient(self.root)
        top.resizable(False, False)
        frm = ttk.Frame(top, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="只有在“下发被拒绝 / 认证失败”时才需要设置。\n密码只写在本机，不进仓库、不上传。",
                  font=self.f_small, foreground="#555555", justify="left").pack(anchor="w")
        v = tk.StringVar()
        e = ttk.Entry(frm, textvariable=v, show="*", width=32, font=self.f_base)
        e.pack(fill="x", pady=(10, 6))
        e.focus_set()

        def on_save() -> None:
            text = v.get()
            if not text:
                messagebox.showwarning("密码为空", "请先输入密码。", parent=top)
                return
            err = save_passfile(text)
            if err:
                messagebox.showerror("保存失败", err, parent=top)
                return
            self.log("管理密码已保存到本机（仅管理员可读）")
            self.v_pass.set("已设置")
            top.destroy()

        def on_clear() -> None:
            clear_passfile()
            self.log("已删除本机保存的管理密码")
            self.v_pass.set("未设置（通常不需要）")
            top.destroy()

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="保存", command=on_save).pack(side="left")
        ttk.Button(row, text="删除已保存的密码", command=on_clear).pack(side="left", padx=8)
        ttk.Button(row, text="取消", command=top.destroy).pack(side="right")

    # ---- 下发
    def choose_tvn(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 TightVNC 服务端程序 tvnserver.exe", filetypes=[("tvnserver.exe", "tvnserver.exe"), ("程序", "*.exe")])
        if not path:
            return
        path = os.path.normpath(path)
        if os.path.basename(path).lower() != "tvnserver.exe":
            messagebox.showwarning("选错了", "请选名字叫 tvnserver.exe 的那个文件。")
            return
        self.tvn = path
        save_tvn(path)
        self.v_tvn.set(path)
        self.fail_until = 0.0
        self.last_rect = None
        self.log(f"已指定 tvnserver：{path}")

    def _ensure_tvn(self) -> bool:
        if self.tvn and os.path.isfile(self.tvn):
            return True
        self.tvn = find_tvnserver()
        self.v_tvn.set(self.tvn or "没找到 tvnserver.exe，请点“选择 tvnserver.exe…”")
        if self.tvn:
            save_tvn(self.tvn)
            self.log(f"找到 tvnserver：{self.tvn}")
            return True
        return False

    def _log_once(self, msg: str) -> None:
        if msg != self.last_err:
            self.last_err = msg
            self.log(msg)

    def _submit(self, rect: tuple[int, int, int, int], why: str) -> None:
        if self.applying:
            return
        if not self._ensure_tvn():
            self.fail_until = time.monotonic() + RETRY_AFTER
            self.v_last.set(f"{now()}  没找到 tvnserver.exe")
            self.set_banner("找不到 TightVNC 服务端（tvnserver.exe）。点“选择 tvnserver.exe…”告诉我它在哪。", "bad")
            self._log_once("找不到 tvnserver.exe。副机上到 TightVNC 安装目录里找到它，点“选择 tvnserver.exe…”。")
            return
        v4 = tvn_lacks_sharerect(self.tvn)
        if v4:
            self.fail_until = time.monotonic() + RETRY_AFTER
            self.v_last.set(f"{now()}  TightVNC {v4} 不支持")
            self.set_banner(f"TightVNC {v4} 不支持只共享游戏区域。请换成 TightVNC 2.8.x(见使用说明)。", "bad")
            self._log_once(f"TightVNC {v4} 已删除 -sharerect 命令,发过去只会弹出帮助窗口。请卸载后安装 TightVNC 2.8.90。")
            return
        self.applying = True
        self.log(f"下发共享区域 {fmt_rect(rect)}（{why}）")

        def work() -> None:
            passfile = DEFAULT_PASSFILE if os.path.exists(DEFAULT_PASSFILE) else None
            t0 = time.monotonic()
            rc, out, mode = apply_rect(self.tvn, rect, passfile, self.mode)
            self.q.put((rect, rc, out, time.monotonic() - t0, mode))

        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self) -> None:
        while True:
            try:
                rect, rc, out, dt, mode = self.q.get_nowait()
            except queue.Empty:
                return
            self.applying = False
            self.last_apply = time.monotonic()
            if rc != 0:
                self.fail_until = self.last_apply + RETRY_AFTER
            if rc == 0:
                self.last_err = ""
                self.last_rect = rect
                self.mode = mode
                self.v_last.set(f"{now()}  成功（{dt * 1000:.0f}ms,{ '服务' if mode == 'service' else '程序' }方式）")
                self.log(f"成功：{fmt_rect(rect)}（TightVNC 以{ '服务' if mode == 'service' else '程序' }方式运行）")
                if out:
                    self.log("  注意：" + out)
            elif rc == -1 and "程序" in out:
                self.v_last.set(f"{now()}  执行失败")
                self._log_once(f"执行失败：{out}")
                self.set_banner("找不到 tvnserver.exe，或没有权限执行它。", "bad")
            else:
                self.v_last.set(f"{now()}  失败（退出码 {rc}）")
                first = out != self.last_err
                self._log_once(f"失败:{out}　（{RETRY_AFTER:.0f} 秒后自动重试）")
                if first:
                    self.do_diag_tvn()
                self.set_banner("下发失败。常见原因：TightVNC 服务端没在运行、需要管理密码、或没用管理员身份运行。", "bad")

    # ---- 按钮行为
    def do_start(self) -> None:
        if not self.rect:
            self.log("还没找到游戏窗口，先打开游戏再点一次")
            self.set_banner("还没找到游戏窗口。把游戏打开，等 1 秒会自动开始。", "warn")
            self.sharing = True
            return
        self.sharing = True
        self.fail_until = 0.0
        self._submit(self.rect, "开始共享")
        self.set_banner("正在共享游戏区域。可以回主机连接了。", "ok")

    def do_stop(self) -> None:
        self.sharing = False
        sw, sh = screen_size()
        self.last_rect = None
        self._submit((0, 0, sw, sh), "恢复整桌面")
        self.set_banner("正在恢复共享整桌面。", "warn")

    def pin_window(self, h: int) -> None:
        self.pinned = h
        self.last_rect = None
        self.log(f"手动指定游戏窗口：{h}（类名 {winapi.class_name(h)}）")

    def unpin_window(self) -> None:
        self.pinned = 0
        self.last_rect = None
        self.log("改回自动查找游戏窗口")

    def do_diagnose(self) -> None:
        self.log(f"找窗口的条件：类名={self.cls}　进程={self.exe}　或标题含“{TITLE_KEYWORD}”且客户区 800x600")
        rows = list_candidates()
        if not rows:
            self.log("没有找到任何较大的可见窗口（客户区至少 400x300）")
        for r in rows:
            self.log("  " + r)
        self.log("把上面带“梦幻”或像游戏的那一行发给我，我就能改对匹配条件。")

    def do_diag_tvn(self) -> None:
        self.log("正在检查 TightVNC 状态……")

        def work() -> None:
            lines = diagnose_tvn(self.tvn)
            for ln in lines:
                self.root.after(0, self.log, "  " + ln)
        threading.Thread(target=work, daemon=True).start()

    def do_apply_now(self) -> None:
        if not self.rect:
            self.log("还没找到游戏窗口，没法下发")
            return
        self.fail_until = 0.0
        self.last_err = ""
        self._submit(self.rect, "手动重新下发")

    # ---- 主循环
    def tick(self) -> None:
        try:
            self._poll_queue()
            if self.pinned and not winapi.is_window(self.pinned):
                self.log(f"指定的窗口 {self.pinned} 已不存在，改回自动查找")
                self.pinned = 0
            self.game_hwnd = self.pinned or find_game_window(self.cls, self.exe)
            self.rect = client_rect_on_screen(self.game_hwnd) if self.game_hwnd else None
            self._update_view()
            if self.sharing and self.rect and self.follow:
                need = self.rect != self.last_rect or time.monotonic() - self.last_apply >= self.reapply
                if need and time.monotonic() >= self.fail_until:
                    why = "窗口位置变了" if self.rect != self.last_rect else "定期重发"
                    self._submit(self.rect, why)
        except Exception as e:  # noqa: BLE001  界面不能因为一次异常就停
            self.log(f"循环出错：{type(e).__name__}: {e}")
        self.root.after(int(self.interval * 1000), self.tick)

    def _update_view(self) -> None:
        if not self.game_hwnd:
            if time.monotonic() - self.last_dump > 20:
                self.last_dump = time.monotonic()
                self.do_diagnose()
            self.v_window.set("没找到（游戏没开？）")
            self.v_client.set("—")
            if self.sharing:
                self.set_banner("没找到游戏窗口，正在等游戏打开……", "warn")
            elif not self.applying:
                self.set_banner("没找到游戏窗口。先打开游戏，再点“开始共享”。", "warn")
            return
        rect = self.rect
        if rect is None:
            self.v_window.set(f"已找到（{self.game_hwnd}），但窗口最小化或尺寸为 0")
            self.v_client.set("—")
            self.set_banner("游戏窗口最小化了。把它还原，共享会自动跟上。", "warn")
            return
        x, y, w, h = rect
        self.v_window.set(f"已找到（句柄 {self.game_hwnd}）" + ("　手动指定" if self.pinned else ""))
        if (w, h) == GAME_SIZE:
            self.v_client.set(f"{w}x{h}　位置 ({x},{y})")
        else:
            self.v_client.set(f"{w}x{h}　位置 ({x},{y})　⚠ 不是 {GAME_SIZE[0]}x{GAME_SIZE[1]}")
        if self.sharing:
            self.v_share.set(fmt_rect(self.last_rect or rect) + ("" if self.applying else ""))
            if self.applying:
                pass
            elif self.last_err:
                self.set_banner("下发失败,稍后自动重试。原因看下面的日志(常见:没找到 tvnserver、需要管理密码)。", "bad")
            else:
                self.set_banner(f"正在共享游戏区域 {fmt_rect(self.last_rect or rect)}。可以回主机连接了。", "ok")
        else:
            self.v_share.set("未共享（副机共享整桌面）")
            if not self.applying:
                self.set_banner("未共享。点“开始共享”后，副机就只共享游戏区域。", "info")
        self.v_pass.set("已设置" if os.path.exists(DEFAULT_PASSFILE) else "未设置（通常不需要）")

    # ---- 退出
    def on_close(self) -> None:
        if self.sharing and self.tvn:
            try:
                sw, sh = screen_size()
                apply_rect(self.tvn, (0, 0, sw, sh),
                           DEFAULT_PASSFILE if os.path.exists(DEFAULT_PASSFILE) else None, self.mode, timeout=4)
                write_log(f"[{now()}] 退出前已恢复共享整桌面")
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------- 无界面模式
def console_loop(cls: str, exe: str, tvn: str, passfile: str | None, interval: float,
                 reapply: float, once: bool, dry_run: bool) -> int:
    winapi.set_dpi_aware()
    tvn = find_tvnserver(tvn)
    if not tvn:
        say(f"[{now()}] 找不到 tvnserver.exe,请用 --tvn 指定完整路径")
        return 2
    say(f"[{now()}] game_share 启动 tvnserver={tvn} passfile={'有' if passfile else '无'}")
    last_rect, last_apply, warned = None, 0.0, False
    while True:
        h = find_game_window(cls, exe)
        rect = client_rect_on_screen(h) if h else None
        if rect is None:
            if not warned:
                say(f"[{now()}] 没找到可见的游戏窗口（类名 {cls}），等待中...")
                warned = True
        else:
            warned = False
            if (rect[2], rect[3]) != GAME_SIZE:
                say(f"[{now()}] 警告: 游戏客户区 {rect[2]}x{rect[3]}，应为 {GAME_SIZE[0]}x{GAME_SIZE[1]}")
            if rect != last_rect or time.monotonic() - last_apply >= reapply:
                say(f"[{now()}] 下发: {fmt_rect(rect)}")
                if not dry_run:
                    rc, out, mode = apply_rect(tvn, rect, passfile)
                    say(f"[{now()}]   退出码 {rc} 方式={mode or '失败'}{('，输出: ' + out) if out else ''}")
                last_rect, last_apply = rect, time.monotonic()
        if once:
            return 0 if rect else 1
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="副机共享助手")
    ap.add_argument("--class", dest="cls", default="WSGAME")
    ap.add_argument("--exe", default="mhmain.exe")
    ap.add_argument("--tvn", default=DEFAULT_TVN)
    ap.add_argument("--passfile", default=DEFAULT_PASSFILE if os.path.exists(DEFAULT_PASSFILE) else None)
    ap.add_argument("--interval", type=float, default=1.0, help="检查窗口位置的间隔（秒）")
    ap.add_argument("--reapply", type=float, default=30.0, help="位置没变时的重发间隔（秒）")
    ap.add_argument("--console", action="store_true", help="不用界面，直接跑守护循环")
    ap.add_argument("--once", action="store_true", help="只做一轮（调试）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不带参数真正下发（调试）")
    ap.add_argument("--smoke", action="store_true", help="界面自检：打开 3 秒后自动关闭（开发用）")
    args = ap.parse_args()

    if args.console or args.once or args.dry_run:
        return console_loop(args.cls, args.exe, args.tvn, args.passfile, args.interval,
                            args.reapply, args.once, args.dry_run)

    winapi.set_dpi_aware()  # 必须早于 Tk()，否则缩放 150% 时坐标会偏
    if not winapi.is_admin():
        if relaunch_as_admin():
            return 0
        # 提权被拒绝：仍然打开界面，但会把问题写在日志里
    app = SlaveApp(args.cls, args.exe, args.tvn, args.interval, args.reapply)
    if args.smoke:
        app.root.after(3000, app.root.destroy)
        app.run()
        print("SMOKE OK")
        return 0
    app.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        write_log(f"[{now()}] 异常退出: {type(exc).__name__}: {exc}")
        raise
