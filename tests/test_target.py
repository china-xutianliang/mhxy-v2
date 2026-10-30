import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.target import TargetError, resolve, validate_child


class FakeWinApi:
    def __init__(self):
        self.parents = {1: 0, 2: 1, 3: 2}
        self.classes = {1: "tvnviewer-session-window", 2: "tvnviewer-session-window", 3: "Static"}
        self.sizes = {1: (820, 700), 2: (1600, 1200), 3: (20, 20)}
        self.visible = {1: True, 2: True, 3: True}
        self.image = r"C:\Program Files\TightVNC\tvnviewer.exe"
        self.window_title = "100.64.0.10::5900 - TightVNC"

    def is_window(self, h): return h in self.classes
    def parent_of(self, h): return self.parents.get(h, 0)
    def class_name(self, h): return self.classes.get(h, "")
    def pid_of(self, h): return 123
    def image_path(self, pid): return self.image
    def enum_children(self, h): return [2, 3] if h == 1 else []
    def is_visible(self, h): return self.visible.get(h, False)
    def is_iconic(self, h): return False
    def client_size(self, h): return self.sizes.get(h, (0, 0))
    def title(self, h): return self.window_title


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWinApi()

    def test_resolve_from_descendant(self):
        result = resolve(3, self.api)
        self.assertEqual((result.top, result.child, result.pid), (1, 2, 123))
        self.assertEqual(result.address, "100.64.0.10:5900")

    def test_resolve_from_top_and_child(self):
        self.assertEqual(resolve(1, self.api).child, 2)
        self.assertEqual(resolve(2, self.api).top, 1)

    def test_validate_rejects_top(self):
        with self.assertRaisesRegex(TargetError, "只能发到画面子窗口"):
            validate_child(1, self.api)

    def test_rejects_other_process(self):
        self.api.image = r"C:\Windows\notepad.exe"
        with self.assertRaisesRegex(TargetError, "不是 tvnviewer.exe"):
            resolve(2, self.api)

    def test_rejects_zero_and_missing_child(self):
        with self.assertRaises(TargetError):
            resolve(0, self.api)
        self.api.classes[2] = "Other"
        with self.assertRaisesRegex(TargetError, "找不到画面子窗口"):
            resolve(1, self.api)

    def test_rejects_invisible_or_empty_child(self):
        self.api.visible[2] = False
        with self.assertRaisesRegex(TargetError, "不可见"):
            resolve(2, self.api)
        self.api.visible[2] = True
        self.api.sizes[2] = (0, 0)
        with self.assertRaisesRegex(TargetError, "客户区为 0"):
            resolve(2, self.api)


if __name__ == "__main__":
    unittest.main()
