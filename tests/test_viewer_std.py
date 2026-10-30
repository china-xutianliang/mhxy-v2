import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control import viewer_link, viewer_std
from control.viewer_std import (STANDARD_OPTIONS, SysOps, ViewerSettingsError, check_standard, ensure_standard,
                                parse_viewer_cmd, redact, standard_args, verify_process)

EXE = r'"C:\Program Files\TightVNC\tvnviewer.exe"'
STD = f"{EXE} -host=10.0.0.5 -port=5900 -jpegimagequality=-1 -scale=100 -encoding=tight -unencrypted=allow"


class StandardArgsTests(unittest.TestCase):
    def test_contains_every_standard_option(self):
        args = standard_args("10.0.0.5", 5900)
        self.assertIn("-host=10.0.0.5", args)
        self.assertIn("-port=5900", args)
        for k, v in STANDARD_OPTIONS:
            self.assertIn(f"-{k}={v}", args)

    def test_password_only_when_given(self):
        self.assertFalse(any(a.startswith("-password") for a in standard_args("h", 1)))
        self.assertIn("-password=abc", standard_args("h", 1, "abc"))

    def test_own_args_are_judged_standard(self):
        line = EXE + " " + " ".join(standard_args("10.0.0.5", 5900, "pw"))
        self.assertEqual(check_standard(parse_viewer_cmd(line)), [])


class RedactTests(unittest.TestCase):
    def test_password_hidden(self):
        out = redact(STD + " -password=s3cr3t!")
        self.assertNotIn("s3cr3t", out)
        self.assertIn("-password=<hidden>", out)

    def test_quoted_password_and_case(self):
        out = redact('x.exe -Password="my pass word" -scale=100')
        self.assertNotIn("my pass", out)
        self.assertIn("-scale=100", out)

    def test_no_password_unchanged(self):
        self.assertEqual(redact(STD), STD)


class ParseTests(unittest.TestCase):
    def test_host_port_forms(self):
        for line, want in [
            (f"{EXE} -host=1.2.3.4::5901", ("1.2.3.4", 5901)),
            (f"{EXE} -host=1.2.3.4 -port=5902", ("1.2.3.4", 5902)),
            (f"{EXE} -port=5902 -host=1.2.3.4", ("1.2.3.4", 5902)),
            (f"{EXE} 1.2.3.4::5903", ("1.2.3.4", 5903)),
            (f"{EXE} 1.2.3.4:1", ("1.2.3.4", 5901)),
            (f"{EXE} -host=1.2.3.4", ("1.2.3.4", 0)),
        ]:
            c = parse_viewer_cmd(line)
            self.assertEqual((c.host, c.port), want, line)

    def test_option_names_case_insensitive(self):
        c = parse_viewer_cmd(f"{EXE} -HOST=1.2.3.4 -JpegImageQuality=-1")
        self.assertEqual(c.host, "1.2.3.4")
        self.assertEqual(c.opts["jpegimagequality"], "-1")


class CheckStandardTests(unittest.TestCase):
    def bad(self, line):
        return check_standard(parse_viewer_cmd(line))

    def test_standard(self):
        self.assertEqual(self.bad(STD), [])

    def test_missing_jpeg_option(self):
        line = STD.replace(" -jpegimagequality=-1", "")
        self.assertTrue(any("jpegimagequality" in b for b in self.bad(line)))

    def test_wrong_scale(self):
        self.assertTrue(any("scale" in b for b in self.bad(STD.replace("-scale=100", "-scale=150"))))
        self.assertTrue(any("scale" in b for b in self.bad(STD.replace("-scale=100", "-scale=auto"))))

    def test_option_name_case_ok_but_value_case_not(self):
        self.assertEqual(self.bad(STD.replace("-encoding", "-ENCODING")), [])
        # 4.0 实测：-encoding 的值大小写敏感（ZRLE 大写会退化成 Raw），必须判不合标准
        self.assertTrue(self.bad(STD.replace("tight", "Tight")))

    def test_unencrypted_must_be_allow(self):
        self.assertTrue(self.bad(STD.replace("-unencrypted=allow", "-unencrypted=ask")))
        self.assertTrue(self.bad(STD.replace(" -unencrypted=allow", "")))

    def test_forbidden_options(self):
        self.assertTrue(self.bad(STD + " -viewonly=yes"))
        self.assertEqual(self.bad(STD + " -viewonly=no"), [])
        self.assertTrue(self.bad(STD + " -fullscreen=yes"))
        self.assertTrue(self.bad(STD + " -optionsfile=a.vnc"))

    def test_bare_host_is_not_standard(self):
        self.assertTrue(self.bad(f"{EXE} -host=10.0.0.5::5900"))


class FakeSys:
    """假系统：pid -> 命令行，记录被结束的 pid 和读 TCP 的次数。"""

    def __init__(self, procs, endpoints=None, kill_ok=True):
        self.procs, self.eps, self.kill_ok = dict(procs), endpoints or {}, kill_ok
        self.killed, self.ep_calls = [], 0

    def ops(self):
        return SysOps(list_pids=lambda: list(self.procs), command_line=lambda p: self.procs.get(p),
                      kill=self._kill, endpoints=self._eps)

    def _kill(self, pid):
        if self.kill_ok:
            self.killed.append(pid)
            self.procs.pop(pid, None)
        return self.kill_ok

    def _eps(self, pid):
        self.ep_calls += 1
        return self.eps.get(pid, [])


class EnsureStandardTests(unittest.TestCase):
    def test_nonstandard_same_host_is_killed(self):
        fs = FakeSys({1: STD.replace("-scale=100", "-scale=150")})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [1])
        self.assertEqual(fs.killed, [1])

    def test_standard_is_left_alone(self):
        fs = FakeSys({1: STD})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [])
        self.assertEqual(fs.killed, [])

    def test_other_host_or_port_untouched(self):
        fs = FakeSys({1: f"{EXE} -host=10.0.0.6 -port=5900 -scale=150", 2: f"{EXE} -host=10.0.0.5 -port=5901 -scale=150"})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [])
        self.assertEqual(fs.killed, [])

    def test_default_port_is_5900(self):
        fs = FakeSys({1: f"{EXE} -host=10.0.0.5"})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [1])

    def test_unreadable_commandline_skipped(self):
        fs = FakeSys({1: None})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [])

    def test_only_nonstandard_among_several(self):
        fs = FakeSys({1: STD, 2: f"{EXE} -host=10.0.0.5 -port=5900 -jpegimagequality=6", 3: f"{EXE} -host=9.9.9.9"})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [2])

    def test_manually_opened_viewer_found_by_tcp_endpoint(self):
        fs = FakeSys({1: EXE, 2: EXE}, endpoints={1: [("10.0.0.5", 5900)], 2: [("10.0.0.9", 5900)]})
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [1])

    def test_tcp_not_consulted_when_commandline_has_host(self):
        fs = FakeSys({1: STD})
        ensure_standard("10.0.0.5", 5900, fs.ops())
        self.assertEqual(fs.ep_calls, 0)

    def test_kill_failure_not_reported_as_killed(self):
        fs = FakeSys({1: f"{EXE} -host=10.0.0.5"}, kill_ok=False)
        self.assertEqual(ensure_standard("10.0.0.5", 5900, fs.ops()), [])


class VerifyTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(verify_process(1, "10.0.0.5", 5900, FakeSys({1: STD}).ops()), [])

    def test_reports_missing_option(self):
        bad = verify_process(1, "10.0.0.5", 5900, FakeSys({1: STD.replace(" -scale=100", "")}).ops())
        self.assertTrue(any("scale" in b for b in bad))

    def test_wrong_address(self):
        self.assertTrue(verify_process(1, "10.0.0.7", 5900, FakeSys({1: STD}).ops()))

    def test_unreadable_is_not_a_failure(self):
        self.assertEqual(verify_process(1, "10.0.0.5", 5900, FakeSys({1: None}).ops()), [])


class FakeProc:
    def __init__(self, pid):
        self.pid, self.killed = pid, False

    def kill(self):
        self.killed = True

    def wait(self):
        pass


class LaunchTests(unittest.TestCase):
    def launch(self, verify_results, killed=()):
        procs = []

        def popen(args):
            p = FakeProc(100 + len(procs))
            procs.append((p, args))
            return p

        with mock.patch.object(viewer_link.os.path, "exists", return_value=True), \
                mock.patch.object(viewer_std, "prepare"), \
                mock.patch.object(viewer_std, "ensure_standard", return_value=list(killed)), \
                mock.patch.object(viewer_std, "verify_process", side_effect=list(verify_results)), \
                mock.patch.object(viewer_link.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(viewer_link, "log") as lg:
            try:
                got = viewer_link.launch_viewer("10.0.0.5", 5900, "pw", "x.exe")
            except ViewerSettingsError as e:
                return procs, e, lg
        return procs, got, lg

    def test_happy_path_uses_standard_args_and_never_logs_password(self):
        procs, got, lg = self.launch([[]])
        self.assertEqual(len(procs), 1)
        self.assertIs(got, procs[0][0])
        self.assertEqual(procs[0][1], ["x.exe", *standard_args("10.0.0.5", 5900, "pw")])
        self.assertFalse(any("pw" in str(c) for c in lg.call_args_list))

    def test_retry_once_then_ok(self):
        procs, got, _ = self.launch([["缺 -scale=100"], []])
        self.assertEqual(len(procs), 2)
        self.assertTrue(procs[0][0].killed)
        self.assertIs(got, procs[1][0])

    def test_gives_clear_error_after_two_failures(self):
        procs, err, _ = self.launch([["缺 -scale=100"], ["缺 -scale=100"]])
        self.assertEqual(len(procs), 2)
        self.assertIsInstance(err, ViewerSettingsError)
        self.assertIn("查看器设置不对", str(err))
        self.assertIn("scale", str(err))
        self.assertIsInstance(err, OSError)

    def test_reopen_is_logged_when_old_viewer_killed(self):
        _, _, lg = self.launch([[]], killed=[7])
        self.assertTrue(any("已按标准参数重开" in str(c) for c in lg.call_args_list))

    def test_missing_exe_raises(self):
        with mock.patch.object(viewer_link.os.path, "exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                viewer_link.launch_viewer("h", 1, "")


class SessionTargetTests(unittest.TestCase):
    def test_client_is_fixed_800x600_regardless_of_dpi(self):
        from manager.session import Session
        # -scale=100 下客户区恒为 800x600（物理像素），输入坐标 V = G；目标尺寸不得再随主机 DPI 变化
        self.assertEqual(Session.target_client.fget(object()), (800, 600))


class SystemCallTests(unittest.TestCase):
    def test_reads_own_command_line(self):
        line = viewer_std.process_command_line(os.getpid())
        self.assertIsInstance(line, str)
        self.assertIn(os.path.basename(sys.executable).lower(), line.lower())

    def test_unreadable_pid_returns_none(self):
        self.assertIsNone(viewer_std.process_command_line(0xFFFFFFF0))


if __name__ == "__main__":
    unittest.main()
