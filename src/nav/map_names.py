"""录制器用的地图名：内置场景表 + 用户自定义地图。

自定义地图保存在 runtime/nav/custom_maps.json，重新打开程序仍然可选；
内置名字来自 resources/nav/world/scenes.yaml，不在这里重复维护。
"""
from __future__ import annotations

import json
import os

from common.config import load_yaml, runtime_dir

KIND_LABELS = {
    "city": "主城", "field": "野外", "sect": "门派", "sect_hall": "门派室内",
    "maze": "迷宫", "interior": "室内", "guild": "帮派", "instance": "副本",
}
CUSTOM_KIND = "自定义"
MAX_NAME = 24


def builtin() -> list[tuple[str, str, tuple[str, ...]]]:
    scenes = (load_yaml("nav", "world", "scenes.yaml").get("scenes") or {})
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for name, data in scenes.items():
        data = data or {}
        kind = KIND_LABELS.get(str(data.get("kind") or ""), "")
        aliases = tuple(str(a).strip() for a in (data.get("alias") or ()) if str(a).strip())
        out.append((str(name), kind, aliases))
    return out


def _path() -> str:
    folder = os.path.join(runtime_dir(), "nav")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "custom_maps.json")


def load_custom() -> list[str]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        name = " ".join(item.split())
        if name and name not in out:
            out.append(name)
    return out


def save_custom(names) -> None:
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(list(names), f, ensure_ascii=False, indent=2)


def all_entries() -> list[tuple[str, str, tuple[str, ...]]]:
    """自定义在前，内置在后；重名的内置项优先，不会出现两条。"""
    known = {name for name, _, _ in builtin()}
    custom = [(name, CUSTOM_KIND, ()) for name in load_custom() if name not in known]
    return custom + builtin()


def add_custom(name: str) -> bool:
    name = " ".join(str(name).split())
    if not name or len(name) > MAX_NAME:
        return False
    if name in {n for n, _, _ in builtin()}:
        return False
    names = load_custom()
    if name in names:
        return False
    names.append(name)
    try:
        save_custom(names)
    except OSError:
        return False
    return True
