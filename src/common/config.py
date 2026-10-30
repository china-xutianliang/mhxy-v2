"""配置与路径：resources/ 下 yaml 的统一读取。

所有坐标、场景名、模板图名只应出现在 resources/ 的 yaml 里，代码里不硬编码。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache

import yaml

if hasattr(sys, "_MEIPASS"):  # PyInstaller 单文件
    PROJECT_ROOT = sys._MEIPASS  # type: ignore[attr-defined]
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESOURCES_DIR = os.path.join(PROJECT_ROOT, "resources")


def resources_path(*parts: str) -> str:
    return os.path.join(RESOURCES_DIR, *parts)


@lru_cache(maxsize=None)
def load_yaml(*parts: str) -> dict:
    with open(resources_path(*parts), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def runtime_dir() -> str:
    """运行时产物（日志、截图、状态）目录，不入版本库。"""
    d = os.path.join(PROJECT_ROOT if not hasattr(sys, "_MEIPASS") else os.path.dirname(sys.executable), "runtime")
    os.makedirs(d, exist_ok=True)
    return d
