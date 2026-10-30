import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))


class MapNamesTest(unittest.TestCase):
    def test_builtin_comes_from_scene_table(self):
        from nav import map_names
        data = {name: (hint, aliases) for name, hint, aliases in map_names.builtin()}
        self.assertEqual(data["长安城"][0], "主城")
        self.assertEqual(data["江南野外"][0], "野外")
        self.assertEqual(data["大雁塔一层"][0], "迷宫")
        self.assertIn("西牛贺洲", data["长寿郊外"][1])
        self.assertIn("花果山", data)

    def test_custom_add_persist_and_dedupe(self):
        from nav import map_names
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(map_names, "runtime_dir", lambda: tmp):
            self.assertTrue(map_names.add_custom("  新地图  "))
            self.assertEqual(map_names.load_custom(), ["新地图"])
            self.assertFalse(map_names.add_custom("新地图"))
            self.assertFalse(map_names.add_custom("长安城"))
            self.assertFalse(map_names.add_custom("   "))
            self.assertFalse(map_names.add_custom("长" * (map_names.MAX_NAME + 1)))
            self.assertEqual([name for name, hint, _ in map_names.all_entries() if hint == "自定义"],
                             ["新地图"])
            path = os.path.join(tmp, "nav", "custom_maps.json")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["新地图"])

    def test_broken_custom_file_is_ignored(self):
        from nav import map_names
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(map_names, "runtime_dir", lambda: tmp):
            folder = os.path.join(tmp, "nav")
            os.makedirs(folder)
            with open(os.path.join(folder, "custom_maps.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(map_names.load_custom(), [])
            self.assertTrue(any(name == "长安城" for name, _, _ in map_names.all_entries()))


if __name__ == "__main__":
    unittest.main()
