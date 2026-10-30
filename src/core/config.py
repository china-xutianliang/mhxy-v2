"""配置与路径加载工具：统一管理 resources/ 目录下的 yaml 配置读取。

所有具体坐标/场景名/路由数据只应该出现在 resources/ 的 yaml 文件里，
逻辑代码里不应该硬编码任何这类数据——这是整套骨架的基本约束。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache

import yaml

# 打包成 PyInstaller 单文件 exe 时，资源被解包到 sys._MEIPASS；开发态仍用源码树。
if hasattr(sys, "_MEIPASS"):
    _PROJECT_ROOT = sys._MEIPASS  # type: ignore[attr-defined]
else:
    _SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../src
    _PROJECT_ROOT = os.path.dirname(_SRC_DIR)
_RESOURCES_DIR = os.path.join(_PROJECT_ROOT, "resources")


def resources_path(*parts: str) -> str:
    """拼接 resources/ 目录下的路径。

    例如 resources_path('config', 'driver.yaml') -> resources/config/driver.yaml
    """
    return os.path.join(_RESOURCES_DIR, *parts)


@lru_cache(maxsize=None)
def load_yaml(*parts: str) -> dict:
    """读取并缓存 resources/ 目录下的某个 yaml 配置文件。"""
    path = resources_path(*parts)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
