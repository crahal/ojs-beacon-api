from __future__ import annotations

import contextlib
import io
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import run_guarded_local_build as guard


class GuardedBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.argv = ["--working-directory", str(self.root), "--disk-reserve", ".", "20",
                     "--max-runtime-hours", "24", "--", "private-executable", "private-argument"]
        self.cgroups = mock.patch.object(guard, "cgroup_memory_directories", return_value=())
        self.cgroups.start()
        self.addCleanup(self.cgroups.stop)
        self.memory = mock.patch.object(guard, "host_available_memory", return_value=30 * guard.GIB)
        self.memory.start()
        self.addCleanup(self.memory.stop)
        self.disk = mock.patch.object(guard.os, "statvfs", return_value=mock.Mock(f_bavail=100 * guard.GIB, f_frsize=1))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def resource_guard(self, *, required=False):
        return guard.ResourceGuard((guard.DiskReserve(self.root, 20 * guard.GIB),),
                                   8 * guard.GIB, 2 * guard.GIB, require_cgroup_limit=required)

    def test_runs_explicit_command_after_preflight_and_keeps_secrets_out_of_logs(self):
        runner = mock.Mock()
        output = io.StringIO()
        with contextlib.redirect_stderr(output), mock.patch.dict(guard.os.environ, {"SECRET": "private-value"}):
            guard.execute(guard.parser().parse_args(self.argv), runner)
        call = runner.child.call_args
        self.assertEqual(call.args[0], ["private-executable", "private-argument"])
        self.assertEqual(call.kwargs["cwd"], self.root)
        self.assertEqual(call.kwargs["timeout"], 24 * 3600)
        self.assertEqual(call.kwargs["env"]["SECRET"], "private-value")
        for secret in ("private-executable", "private-argument", "private-value"):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn('"host_available_memory_bytes"', output.getvalue())

    def test_limits_must_be_finite_and_positive_before_spawn(self):
        for value in ("0", "-1", "nan", "inf", "1e309", "private-invalid-value"):
            with self.subTest(value=value), mock.patch.object(guard.Runner, "child") as child, \
                 contextlib.redirect_stderr(io.StringIO()) as output:
                argv = self.argv.copy()
                argv[6] = value
                self.assertEqual(guard.main(argv), 1)
                child.assert_not_called()
                self.assertNotIn(value, output.getvalue())

    def test_disk_limit_scale_overflow_fails_before_spawn(self):
        argv = self.argv.copy()
        argv[4] = "1e308"
        with mock.patch.object(guard.Runner, "child") as child, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(argv), 1)
        child.assert_not_called()

    def test_invalid_shutdown_progress_environment_fails_before_spawn(self):
        with mock.patch.dict(guard.os.environ, {"OJS_PROGRESS_SECONDS": "private-invalid"}), \
             mock.patch.object(guard.Runner, "child") as child, contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(guard.main(self.argv), 1)
        child.assert_not_called()
        self.assertNotIn("private-invalid", output.getvalue())

    def test_disk_and_host_memory_preflight_fail_closed(self):
        for measurement, value in ((guard.os.statvfs, mock.Mock(f_bavail=19 * guard.GIB, f_frsize=1)),
                                   (guard.host_available_memory, 7 * guard.GIB)):
            with self.subTest(value=value), mock.patch.object(guard.Runner, "child") as child:
                measurement.return_value = value
                with self.assertRaises(guard.GuardError):
                    guard.execute(guard.parser().parse_args(self.argv))
                child.assert_not_called()
                guard.os.statvfs.return_value = mock.Mock(f_bavail=100 * guard.GIB, f_frsize=1)

    def test_each_declared_disk_path_is_checked(self):
        second = self.root / "scratch"
        second.mkdir()
        watcher = guard.ResourceGuard((guard.DiskReserve(self.root, 20 * guard.GIB),
                                       guard.DiskReserve(second, 10 * guard.GIB)), 8 * guard.GIB, 2 * guard.GIB)
        guard.os.statvfs.side_effect = [mock.Mock(f_bavail=100 * guard.GIB, f_frsize=1),
                                      mock.Mock(f_bavail=9 * guard.GIB, f_frsize=1)]
        with self.assertRaisesRegex(guard.GuardError, "disk reserve 2"):
            watcher.check()

    def test_unreadable_measurements_fail_closed(self):
        guard.os.statvfs.side_effect = OSError("private-path")
        with self.assertRaisesRegex(guard.GuardError, "measurement is unavailable") as error:
            self.resource_guard().check()
        self.assertNotIn("private-path", str(error.exception))

    def test_requires_finite_cgroup_limit_when_requested(self):
        with self.assertRaisesRegex(guard.GuardError, "finite cgroup"):
            self.resource_guard(required=True).check()

    def test_cgroup_limit_includes_tighter_parent_and_is_read_each_time(self):
        child, parent = self.root / "child", self.root / "parent"
        for path, maximum, current in ((child, 28, 20), (parent, 32, 31)):
            path.mkdir()
            (path / "memory.max").write_text(str(maximum * guard.GIB))
            (path / "memory.current").write_text(str(current * guard.GIB))
        watcher = self.resource_guard(required=True)
        watcher.cgroups = (child, parent)
        with self.assertRaisesRegex(guard.GuardError, "cgroup memory reserve"):
            watcher.check()
        (parent / "memory.current").write_text(str(28 * guard.GIB))
        metrics = watcher.check()
        self.assertEqual(metrics["cgroup_limited_groups"], 2)
        self.assertEqual(metrics["cgroup_minimum_headroom_bytes"], 4 * guard.GIB)
        (child / "memory.current").write_text("invalid")
        with self.assertRaisesRegex(guard.GuardError, "measurement is unavailable"):
            watcher.check()

    def test_resource_failure_uses_existing_indefinite_graceful_shutdown(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("private-command", 30)] * 8 + [0]
        guard.host_available_memory.side_effect = [30 * guard.GIB, 7 * guard.GIB]
        with mock.patch("compact_update.subprocess.Popen", return_value=process) as popen, \
             mock.patch("compact_update.os.killpg") as kill, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(guard.GuardError, "host memory reserve"):
                guard.execute(guard.parser().parse_args(self.argv))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(1234, signal.SIGINT)
        self.assertEqual(process.wait.call_count, 9)

    def test_runtime_limit_interrupts_real_child_and_waits_for_cleanup(self):
        marker = self.root / "clean-shutdown"
        child = ("import time\nfrom pathlib import Path\n"
                 "try:\n    time.sleep(60)\n"
                 f"except KeyboardInterrupt:\n    Path({str(marker)!r}).write_text('cleaned')\n")
        argv = self.argv[:6] + ["0.0001", "--", sys.executable, "-c", child]
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(guard.UpdateError, "exceeded its configured runtime"):
                guard.execute(guard.parser().parse_args(argv))
        self.assertEqual(marker.read_text(), "cleaned")

    def test_signal_request_enters_existing_graceful_shutdown(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        checks = 0

        def memory_check():
            nonlocal checks
            checks += 1
            if checks == 2:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 30 * guard.GIB

        guard.host_available_memory.side_effect = memory_check
        with mock.patch("compact_update.subprocess.Popen", return_value=process), \
             mock.patch("compact_update.os.killpg") as kill, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(self.argv), 1)
        kill.assert_called_once_with(1234, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=30)

    def test_spawn_error_does_not_echo_arguments_or_paths(self):
        with mock.patch.object(guard.Runner, "child", side_effect=OSError("private-argument")), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(guard.main(self.argv), 1)
        self.assertNotIn("private-argument", output.getvalue())

    def test_parser_error_does_not_echo_unknown_values(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(guard.main(["--private-unknown-secret"]), 1)
        self.assertNotIn("private-unknown-secret", output.getvalue())

    def test_signal_handlers_are_restored(self):
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        with mock.patch.object(guard, "execute"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main(self.argv), 0)
        self.assertEqual(previous, {signum: signal.getsignal(signum) for signum in previous})


class LinuxMeasurementTests(unittest.TestCase):
    def test_host_memavailable_and_missing_field(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "meminfo"
            path.write_text("MemFree: 1 kB\nMemAvailable: 1234 kB\n")
            self.assertEqual(guard.host_available_memory(path), 1234 * 1024)
            for value in ("MemFree: 10 kB\n", "MemAvailable: -2 kB\n", "MemAvailable: 2 bytes\n"):
                path.write_text(value)
                with self.assertRaises(guard.GuardError):
                    guard.host_available_memory(path)

    def test_discovers_unlimited_and_limited_ancestors(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            leaf = root / "user.slice" / "build.service"
            leaf.mkdir(parents=True)
            (leaf / "memory.max").write_text("max")
            (leaf.parent / "memory.max").write_text(str(28 * guard.GIB))
            membership = root / "membership"
            membership.write_text("0::/user.slice/build.service\n")
            self.assertEqual(guard.cgroup_memory_directories(membership, root), (leaf, leaf.parent))


if __name__ == "__main__":
    unittest.main()
