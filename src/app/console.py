"""交互测试台：手动验证骨架——绑定窗口、查看状态。（导航命令随旧 nav 一并移除，第三层见 src/nav）

用法（游戏需要已打开，且终端需要以管理员身份运行，用32位Python执行）：
    cd src
    python -m app.console

命令：
    windows                   列出找到的游戏窗口句柄
    bind <序号>                绑定第N个窗口（序号从1开始）
    state                      打印当前 GameState（场景名/坐标）
    quit                       退出
"""
from __future__ import annotations

from app.bootstrap import _build_driver_config
from core.config import resources_path
from core.driver import DmDriver
from core.state import StateReader


def main() -> None:
    config = _build_driver_config()
    driver = DmDriver(config, templates_dir=resources_path("templates"))

    print("正在注册驱动...")
    driver.register()
    print("驱动注册成功，输入命令开始测试（help 查看命令列表，quit 退出）。")

    windows: list[str] = []
    state_reader = StateReader()

    while True:
        try:
            line = input(">> ").strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0]

        try:
            if cmd in ("quit", "exit"):
                break

            elif cmd == "help":
                print(__doc__)

            elif cmd == "windows":
                windows = sorted(driver.find_windows())
                if not windows:
                    print("未找到游戏窗口，请先启动梦幻西游客户端")
                for i, h in enumerate(windows, start=1):
                    print(f"  {i}: hwnd={h}")

            elif cmd == "bind":
                idx = int(parts[1])
                if not windows:
                    windows = sorted(driver.find_windows())
                hwnd = windows[idx - 1]
                driver.bind(hwnd)
                print(f"已绑定窗口 {idx} (hwnd={hwnd})")

            elif cmd == "state":
                s = state_reader.snapshot(driver)
                print(f"场景: {s.scene}  坐标: ({s.x}, {s.y})")

            else:
                print(f"未知命令: {cmd}，输入 help 查看命令列表")

        except Exception as e:  # noqa: BLE001 - 测试台需要吞掉异常继续交互，不能直接崩掉
            print(f"[出错] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
