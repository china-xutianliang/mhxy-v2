"""启动期冒烟：KmTool 引用的每个 self.xxx 必须在类里定义或赋值过。

之前 `_capture_tick` 被调用却漏了定义，应用一启动就 AttributeError。
这条静态检查能拦住同类问题。
"""
import ast
import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parents[1] / "src" / "kmtool" / "app.py"


def self_attributes(node):
    return {n.attr for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"}


def assigned_attributes(node):
    names = set()
    for sub in ast.walk(node):
        targets = []
        if isinstance(sub, ast.Assign):
            targets = sub.targets
        elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
            targets = [sub.target]
        for target in targets:
            for item in ast.walk(target):
                if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name) and item.value.id == "self":
                    names.add(item.attr)
    return names


class KmToolAttributeTest(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(APP.read_text(encoding="utf-8"))
        self.cls = next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == "KmTool")

    def test_all_self_attributes_are_defined(self):
        methods = {n.name for n in self.cls.body if isinstance(n, ast.FunctionDef)}
        assigned = assigned_attributes(self.cls)
        missing = {name for name in self_attributes(self.cls) if name not in methods | assigned}
        self.assertEqual(set(), missing)

    def test_capture_tick_defined_and_scheduled(self):
        names = {n.name for n in self.cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("_capture_tick", names)
        self.assertIn("_capture_once", names)
        self.assertIn("_refresh_soon", names)


if __name__ == "__main__":
    unittest.main()
