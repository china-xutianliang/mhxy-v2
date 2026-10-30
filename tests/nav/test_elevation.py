import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from common import winapi  # noqa: E402
from nav import elevation  # noqa: E402


class ProcessElevationTest(unittest.TestCase):
    def test_current_process_matches_is_admin(self):
        self.assertEqual(elevation.process_elevated(os.getpid()), bool(winapi.is_admin()))

    def test_zero_pid(self):
        self.assertFalse(elevation.process_elevated(0))


class NeedsElevationTest(unittest.TestCase):
    def test_only_when_viewer_is_higher(self):
        viewer = mock.Mock(top=123)
        with mock.patch.object(elevation, "viewer_elevated", return_value=True), \
                mock.patch.object(elevation.winapi, "is_admin", return_value=False):
            self.assertTrue(elevation.needs_elevation(viewer))
        with mock.patch.object(elevation, "viewer_elevated", return_value=True), \
                mock.patch.object(elevation.winapi, "is_admin", return_value=True):
            self.assertFalse(elevation.needs_elevation(viewer))
        with mock.patch.object(elevation, "viewer_elevated", return_value=False), \
                mock.patch.object(elevation.winapi, "is_admin", return_value=False):
            self.assertFalse(elevation.needs_elevation(viewer))
        self.assertFalse(elevation.needs_elevation(None))

    def test_viewer_elevated_survives_errors(self):
        viewer = mock.Mock(top=123)
        with mock.patch.object(elevation.winapi, "pid_of", side_effect=RuntimeError):
            self.assertFalse(elevation.viewer_elevated(viewer))


class RelaunchTest(unittest.TestCase):
    def test_dev_uses_module_entry(self):
        fake = types.SimpleNamespace(executable=r"C:\Python\python.exe", argv=["recorder_app.py"])
        with mock.patch.object(elevation, "sys", fake), \
                mock.patch.object(elevation, "_shell_execute_runas", return_value=42) as run:
            self.assertTrue(elevation.relaunch_as_admin())
        exe, args, workdir = run.call_args.args
        self.assertEqual(exe, fake.executable)
        self.assertIn("-m nav.recorder_app", args)
        self.assertTrue(workdir.endswith(os.sep + "src"))

    def test_frozen_uses_exe(self):
        fake = types.SimpleNamespace(frozen=True, executable=r"C:\Apps\recorder.exe", argv=["recorder.exe"])
        with mock.patch.object(elevation, "sys", fake), \
                mock.patch.object(elevation, "_shell_execute_runas", return_value=42) as run:
            self.assertTrue(elevation.relaunch_as_admin())
        exe, _args, workdir = run.call_args.args
        self.assertEqual(exe, fake.executable)
        self.assertEqual(workdir, r"C:\Apps")

    def test_user_cancels_uac(self):
        fake = types.SimpleNamespace(frozen=True, executable=r"C:\Apps\recorder.exe", argv=[])
        with mock.patch.object(elevation, "sys", fake), \
                mock.patch.object(elevation, "_shell_execute_runas", return_value=5):
            self.assertFalse(elevation.relaunch_as_admin())


if __name__ == "__main__":
    unittest.main()
