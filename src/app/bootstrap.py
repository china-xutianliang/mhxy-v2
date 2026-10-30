"""启动入口：组装驱动配置 → 注册驱动 → 绑定窗口 → 返回可用的 Context。

Context 目前只持有 driver（阶段0）；阶段1/2 会在这里补上 state/nav/interrupt，
业务代码届时统一从 Context 取用，不单独初始化任何东西。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import load_yaml, resources_path
from core.driver import DmDriver, IDriver


@dataclass
class Context:
    driver: IDriver


def _build_driver_config() -> dict:
    """把 driver.yaml 里的相对文件名，组装成 DmDriver 需要的绝对路径配置。"""
    raw = load_yaml("config", "driver.yaml")
    return {
        "auth": raw["auth"],
        "dll": {
            "dmreg_path": resources_path("dll", raw["dll"]["dmreg_file"]),
            "dm_path": resources_path("dll", raw["dll"]["dm_file"]),
        },
        "dict_path": resources_path("dict"),
        "dicts": raw.get("dicts", []),
        "ai": {
            "module_path": resources_path("ai", raw["ai"]["module_file"]),
            "yolo": {
                "version": raw["ai"]["yolo"]["version"],
                "models": [
                    {
                        "index": m["index"],
                        "path": resources_path("ai", m["file"]),
                        "password": m["password"],
                    }
                    for m in raw["ai"]["yolo"].get("models", [])
                ],
            },
        },
        "game_window": raw["game_window"],
        "bind": raw["bind"],
    }


def bootstrap(auto_bind: bool = True) -> Context:
    """初始化驱动：注册插件 → 字库/AI → 查找并绑定游戏窗口。

    :param auto_bind: 是否自动查找并绑定第一个匹配的游戏窗口；
                       多开场景下想自己挑选句柄时传 False，自行调用
                       driver.find_windows()/driver.bind(hwnd)。
    """
    config = _build_driver_config()
    driver = DmDriver(config, templates_dir=resources_path("templates"))
    driver.register()

    if auto_bind:
        hwnds = driver.find_windows()
        if not hwnds:
            raise RuntimeError("未找到游戏窗口，请先启动游戏客户端后再运行")
        driver.bind(next(iter(hwnds)))

    return Context(driver=driver)
