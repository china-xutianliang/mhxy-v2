import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control import viewer_link
from control.viewer_link import ViewerHandles, restore_maximized, reveal


class FakeWinApi:
    def __init__(self, zoomed_after_restore):
        self.zoomed = True
        self.zoomed_after_restore = zoomed_after_restore
        self.restored = 0

    def is_zoomed(self, h):
        return self.zoomed

    def restore_noactivate(self, h):
        self.restored += 1
        self.zoomed = self.zoomed_after_restore
        return True


class RestoreMaximizedTests(unittest.TestCase):
    def run_restore(self, api, wait_s=1.5):
        handles = ViewerHandles(top=1, child=2)
        with mock.patch.object(viewer_link, "winapi", api), \
                mock.patch.object(viewer_link.time, "sleep", lambda _s: None):
            return restore_maximized(handles, wait_s=wait_s)

    def test_skips_when_not_maximized(self):
        api = FakeWinApi(zoomed_after_restore=False)
        api.zoomed = False
        self.assertFalse(self.run_restore(api))
        self.assertEqual(api.restored, 0)

    def test_restores_and_reports_success(self):
        api = FakeWinApi(zoomed_after_restore=False)
        self.assertTrue(self.run_restore(api))
        self.assertEqual(api.restored, 1)

    def test_reports_failure_when_still_maximized(self):
        api = FakeWinApi(zoomed_after_restore=True)
        self.assertFalse(self.run_restore(api, wait_s=0))
        self.assertEqual(api.restored, 1)


class FakeIconicApi:
    def __init__(self, show_works=False, restore_works=False, iconic=True):
        self.iconic = iconic
        self.show_works = show_works
        self.restore_works = restore_works
        self.calls = []

    def is_iconic(self, h):
        return self.iconic

    def show_noactivate(self, h):
        self.calls.append("show")
        if self.show_works:
            self.iconic = False

    def restore_noactivate(self, h):
        self.calls.append("restore")
        if self.restore_works:
            self.iconic = False
        return True

    def client_size(self, h):
        return (1600, 1200)


class RevealTests(unittest.TestCase):
    def run_reveal(self, api):
        handles = ViewerHandles(top=1, child=2)
        with mock.patch.object(viewer_link, "winapi", api), \
                mock.patch.object(viewer_link.time, "sleep", lambda _s: None):
            return reveal(handles, wait_s=0)

    def test_skips_when_not_iconic(self):
        api = FakeIconicApi(iconic=False)
        self.assertTrue(self.run_reveal(api))
        self.assertEqual(api.calls, [])

    def test_show_noactivate_restores(self):
        api = FakeIconicApi(show_works=True)
        self.assertTrue(self.run_reveal(api))
        self.assertEqual(api.calls, ["show"])

    def test_falls_back_to_restore_noactivate(self):
        api = FakeIconicApi(restore_works=True)
        self.assertTrue(self.run_reveal(api))
        self.assertEqual(api.calls, ["show", "restore"])

    def test_reports_failure_when_both_fail(self):
        api = FakeIconicApi()
        self.assertFalse(self.run_reveal(api))
        self.assertEqual(api.calls, ["show", "restore"])


if __name__ == "__main__":
    unittest.main()
