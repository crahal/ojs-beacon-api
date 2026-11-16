from dataclasses import replace
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from contextlib import redirect_stderr

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import tune_workstation_mysql as tuning

GIB = tuning.GIB


class FakeBackend:
    def __init__(self, config):
        self.config = config
        self.service_value = tuning.Service("active", "running", config.invocation, "/user.slice/builder.service", 10)
        self.item = tuning.Candidate(20, 123, config.clean / "mysql-2026-07-01.building",
            Path("/tmp/ojs-api-mysql-test/mysql.sock"), Path("/tmp/ojs-api-mysql-test/mysql.pid"),
            self.service_value.cgroup, "2026-07-01")
        self.data = tuning.Database(self.item.datadir, self.item.socket, self.item.pid_file,
            "8.4.11", 1, 0, 1, "ON", 4 * GIB, 128 * 1024**2, 7, GIB // 4,
            16384, 0, 4 * GIB // 16384, GIB // 4)
        self.assignments, self.checks, self.sleeps = [], [], 0
        self.items = [self.item]
        self.on_sleep = None

    def service(self):
        return self.service_value

    def candidates(self, service):
        return self.items

    def candidate(self, pid, cgroup):
        return self.item

    def database(self, candidate):
        return self.data

    def resources(self, candidate, **kwargs):
        self.checks.append(kwargs)
        tuning.check_resource_numbers(available=50 * GIB, free_disk=100 * GIB,
                                     groups=[(5 * GIB, 32 * GIB, 36 * GIB)], **kwargs)

    def assign(self, candidate, name, value):
        self.assignments.append((candidate.pid, name, value))
        if name == "innodb_buffer_pool_size":
            self.data = replace(self.data, requested_pool=value, pool_pages=value // self.data.page_size)
        else:
            self.data = replace(self.data, requested_redo=value, applied_redo=value)

    def sleep(self):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.config = tuning.Config("ojs-workstation-history.service", "a" * 32,
            Path("/private/bin"), Path("/private/clean"), ("2026-07-01", "2026-10-01"), 24 * GIB, 2 * GIB)
        self.backend = FakeBackend(self.config)
        self.events = []
        self.controller = tuning.Controller(self.config, self.backend,
            lambda event, **values: self.events.append((event, values)))

    def test_aligns_pool_and_applies_only_two_fixed_growths(self):
        self.assertEqual(self.controller.run(), 0)
        self.assertEqual(self.backend.assignments, [
            (20, "innodb_buffer_pool_size", int(24.5 * GIB)),
            (20, "innodb_redo_log_capacity", 2 * GIB)])
        self.assertTrue(self.events[-1][1]["at_least_target"])
        self.assertEqual(self.backend.checks[1]["pool_growth"], 0)
        self.assertEqual(self.backend.checks[1]["pool_floor"], int(24.5 * GIB))

    def test_larger_existing_sizes_are_not_shrunk(self):
        self.backend.data = replace(self.backend.data, requested_pool=28 * GIB,
            pool_pages=28 * GIB // 16384, requested_redo=4 * GIB, applied_redo=4 * GIB)
        self.assertEqual(self.controller.run(), 0)
        self.assertEqual(self.backend.assignments, [])

    def test_pending_resize_is_not_reissued_and_reports_actual_capacity(self):
        self.backend.data = replace(self.backend.data, requested_pool=16 * GIB, resize_code=5,
                                   requested_redo=2 * GIB, applied_redo=2 * GIB)
        self.assertEqual(self.controller.run(), 0)
        self.assertEqual(self.backend.assignments, [])
        self.assertEqual(self.events[-1][1]["actual_pool_bytes"], 4 * GIB)
        self.assertFalse(self.events[-1][1]["at_least_target"])

    def test_failed_resize_is_not_retried(self):
        self.backend.data = replace(self.backend.data, resize_code=7)
        with self.assertRaises(tuning.TuningError):
            self.controller.run()
        self.assertEqual(self.backend.assignments, [])

    def test_check_only_checks_resources_but_never_assigns(self):
        self.controller.config = replace(self.config, check_only=True)
        self.assertEqual(self.controller.run(), 0)
        self.assertEqual(len(self.backend.checks), 1)
        self.assertEqual(self.backend.assignments, [])
        self.assertIn("check-only-passed", [event[0] for event in self.events])

    def test_unsafe_sql_identities_and_durability_are_rejected(self):
        original = self.backend.data
        for change in (dict(datadir=Path("/unrelated")), dict(socket=Path("/other.sock")),
                       dict(pid_file=Path("/other.pid")), dict(version="8.0.46"),
                       dict(skip_networking=0), dict(log_bin=1), dict(flush_at_commit=2),
                       dict(doublewrite="OFF"), dict(instances=0), dict(page_size=1)):
            with self.subTest(change=change):
                self.backend.data = replace(original, **change)
                with self.assertRaises(tuning.TuningError):
                    self.controller.run()
                self.assertEqual(self.backend.assignments, [])

    def test_identity_change_before_assignment_is_safe(self):
        with mock.patch.object(self.backend, "candidate", return_value=replace(self.backend.item, start_ticks=999)):
            self.assertEqual(self.controller.run(), 1)
        self.assertEqual(self.backend.assignments, [])

    def test_invocation_change_before_assignment_is_safe(self):
        original = self.backend.service_value
        with mock.patch.object(self.backend, "service", side_effect=[original, original, replace(original, invocation="b" * 32)]):
            self.assertEqual(self.controller.run(), 1)
        self.assertEqual(self.backend.assignments, [])

    def test_short_resource_floor_stops_before_any_assignment(self):
        with mock.patch.object(self.backend, "resources", side_effect=tuning.TuningError("reserve unavailable")):
            with self.assertRaises(tuning.TuningError):
                self.controller.run()
        self.assertEqual(self.backend.assignments, [])

    def test_no_builder_is_not_successful_one_shot(self):
        self.backend.items = []
        self.assertEqual(self.controller.run(), 1)
        self.assertEqual(self.backend.assignments, [])

    def test_watch_follows_october_once_and_exits_original_invocation(self):
        self.controller.config = replace(self.config, watch=True)
        original_data = self.backend.data

        def advance():
            if self.backend.sleeps == 1:
                return  # Same PID must not receive the same settings again.
            if self.backend.sleeps == 2:
                self.backend.item = replace(self.backend.item, pid=30, start_ticks=456,
                    datadir=self.config.clean / "mysql-2026-10-01.building", version="2026-10-01")
                self.backend.items = [self.backend.item]
                self.backend.data = replace(original_data, datadir=self.backend.item.datadir)
            else:
                self.backend.service_value = replace(self.backend.service_value, invocation="b" * 32)

        self.backend.on_sleep = advance
        self.assertEqual(self.controller.run(), 0)
        self.assertEqual([item[0] for item in self.backend.assignments], [20, 20, 30, 30])
        self.assertEqual(self.backend.sleeps, 3)

    def test_watch_can_wait_for_startup_or_changed_auth_without_credentials(self):
        self.controller.config = replace(self.config, watch=True)
        self.backend.on_sleep = lambda: setattr(self.backend, "service_value",
            replace(self.backend.service_value, active="inactive", main_pid=0))
        with mock.patch.object(self.backend, "database", side_effect=tuning.Unavailable("auth unavailable")):
            self.assertEqual(self.controller.run(), 0)
        self.assertEqual(self.backend.assignments, [])


class ResourceTests(unittest.TestCase):
    def numbers(self, **overrides):
        values = dict(available=40 * GIB, free_disk=100 * GIB,
            groups=[(5 * GIB, 32 * GIB, 36 * GIB)], pool_growth=int(7.875 * GIB),
            pool_floor=int(16.625 * GIB), redo_growth=0)
        values.update(overrides)
        return values

    def test_normal_growth_leaves_host_and_cgroup_reserves(self):
        tuning.check_resource_numbers(**self.numbers())

    def test_lazy_pool_commitment_counts_despite_small_current_rss(self):
        for override in (dict(available=25 * GIB), dict(groups=[(5 * GIB, 25 * GIB, 32 * GIB)]),
                         dict(groups=[(5 * GIB, 28 * GIB, 30 * GIB)])):
            with self.subTest(override=override), self.assertRaises(tuning.TuningError):
                tuning.check_resource_numbers(**self.numbers(**override))

    def test_parent_limits_and_service_high_and_max_required(self):
        for groups in ([], [(5 * GIB, None, 32 * GIB)], [(5 * GIB, 28 * GIB, None)],
                       [(5 * GIB, 28 * GIB, 32 * GIB), (20 * GIB, None, 32 * GIB)]):
            with self.subTest(groups=groups), self.assertRaises(tuning.TuningError):
                tuning.check_resource_numbers(**self.numbers(groups=groups))

    def test_even_no_growth_retains_host_reserve_and_redo_disk_allowance(self):
        for overrides in (dict(pool_growth=0, pool_floor=4 * GIB, available=11 * GIB),
                          dict(free_disk=42 * GIB, redo_growth=2 * GIB)):
            with self.subTest(overrides=overrides), self.assertRaises(tuning.TuningError):
                tuning.check_resource_numbers(**self.numbers(**overrides))

    def test_parent_accounts_existing_lazy_commitment_not_just_new_resize(self):
        # Parent 40GiB + child commitment ~19GiB + reserve6 exceeds max60;
        # adding only the 7.875GiB resize would incorrectly pass.
        groups = [(int(7.2 * GIB), 32 * GIB, 36 * GIB), (40 * GIB, None, 60 * GIB)]
        with self.assertRaises(tuning.TuningError):
            tuning.check_resource_numbers(**self.numbers(groups=groups))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools, self.clean, self.proc, self.cgroups = [self.root / name for name in ("bin", "clean", "proc", "cgroups")]
        for path in (self.tools, self.clean, self.proc, self.cgroups):
            path.mkdir()
        for name in ("mysql", "mysqld"):
            (self.tools / name).write_text("fixture executable")
            (self.tools / name).chmod(0o700)
        self.config = tuning.Config("ojs-workstation-history.service", "a" * 32, self.tools,
            self.clean, ("2026-07-01",), 24 * GIB, 2 * GIB)
        self.backend = tuning.Backend(self.config, proc=self.proc, cgroups=self.cgroups)
        self.runtime = self.root / "ojs-api-mysql-fixture"
        self.runtime.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.sock.close)
        self.sock.bind(str(self.runtime / "mysql.sock"))
        (self.runtime / "mysql.pid").write_text("123\n")
        self.datadir = self.clean / "mysql-2026-07-01.building"
        self.datadir.mkdir()
        self.process = self.proc / "123"
        self.process.mkdir()
        (self.process / "comm").write_text("mysqld\n")
        (self.process / "exe").symlink_to(self.tools / "mysqld")
        (self.process / "cgroup").write_text("0::/user.slice/test.service\n")
        (self.process / "stat").write_text("123 (mysqld) " + " ".join(["S"] + ["0"] * 18 + ["123456", "0"]))
        self.argv = [str(self.tools / "mysqld"), "--no-defaults", "--daemonize", f"--datadir={self.datadir}",
            f"--socket={self.runtime / 'mysql.sock'}", f"--pid-file={self.runtime / 'mysql.pid'}",
            "--skip-networking", "--mysqlx=0", "--skip-log-bin", "--local-infile=0", "--secure-file-priv=NULL"]
        self.write_argv()
        self.service = tuning.Service("active", "running", self.config.invocation, "/user.slice/test.service", 100)
        self.group = self.cgroups / "user.slice" / "test.service"
        self.group.mkdir(parents=True)
        (self.group / "cgroup.procs").write_text("123\n")

    def write_argv(self):
        (self.process / "cmdline").write_bytes(("\0".join(self.argv) + "\0").encode())

    def candidate(self):
        return self.backend.candidate(123, self.service.cgroup)

    def test_exact_scoped_native_builder_is_discovered(self):
        found = self.backend.candidates(self.service)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].start_ticks, 123456)
        self.assertEqual(found[0].datadir, self.datadir)

    def test_wrong_cgroup_binary_datadir_and_duplicate_options_are_ignored(self):
        original = self.argv[:]
        for extra in ("--skip-networking=0", "--init-file=/untrusted", "--datadir=/wrong"):
            with self.subTest(extra=extra):
                self.argv = original + [extra]
                self.write_argv()
                self.assertIsNone(self.candidate())
        self.argv = original
        self.write_argv()
        self.assertIsNone(self.backend.candidate(123, "/wrong.service"))
        (self.process / "exe").unlink()
        (self.process / "exe").symlink_to(self.tools / "mysql")
        self.assertIsNone(self.candidate())

    def test_missing_private_profile_is_not_tuned(self):
        original = self.argv[:]
        for option in ("--no-defaults", "--skip-networking", "--skip-log-bin", "--mysqlx=0"):
            self.argv = [item for item in original if item != option]
            self.write_argv()
            with self.subTest(option=option):
                self.assertIsNone(self.candidate())

    def test_socket_must_be_socket_and_runtime_private(self):
        self.runtime.chmod(0o755)
        with self.assertRaises(tuning.TuningError):
            self.candidate()
        self.runtime.chmod(0o700)
        self.sock.close()
        (self.runtime / "mysql.sock").unlink()
        (self.runtime / "mysql.sock").write_text("not a socket")
        with self.assertRaises(tuning.TuningError):
            self.candidate()

    def test_pid_mismatch_is_not_tuned(self):
        (self.runtime / "mysql.pid").write_text("999\n")
        with self.assertRaises(tuning.Unavailable):
            self.candidate()

    def test_symlinked_datadir_is_rejected(self):
        self.datadir.rmdir()
        other = self.clean / "unrelated"
        other.mkdir()
        self.datadir.symlink_to(other, target_is_directory=True)
        with self.assertRaises(tuning.TuningError):
            self.candidate()

    def test_mysql_credentials_options_and_environment_are_disabled(self):
        candidate = self.candidate()
        with mock.patch.dict(os.environ, MYSQL_PWD="never-log", MYSQL_TEST_LOGIN_FILE="never-open",
                             MYSQL_HOST="remote", OJS_DB_API_PASSWORD="also-never-log"), \
                mock.patch.object(tuning.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            self.assertEqual(self.backend.mysql(candidate, tuning.IDENTITY_SQL), "ok")
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[1:3], ["--no-defaults", "--no-login-paths"])
        self.assertIn("--skip-password", command)
        self.assertIn("--protocol=socket", command)
        self.assertNotIn("never-log", str(command))
        self.assertFalse(any(key.startswith(("MYSQL", "OJS_")) for key in kwargs["env"]))

    def test_sql_failure_does_not_expose_client_diagnostics(self):
        with mock.patch.object(tuning.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "secret payload")):
            with self.assertRaises(tuning.Unavailable) as error:
                self.backend.mysql(self.candidate(), tuning.IDENTITY_SQL)
        self.assertNotIn("secret", str(error.exception))

    def test_only_positive_fixed_set_global_assignments_are_possible(self):
        for name, value in (("innodb_flush_log_at_trx_commit", 2), ("innodb_buffer_pool_size; DROP TABLE x", 1),
                            ("innodb_buffer_pool_size", 0), ("innodb_buffer_pool_size", "123")):
            with self.subTest(name=name, value=value), self.assertRaises(tuning.TuningError), \
                    mock.patch.object(self.backend, "mysql") as mysql:
                self.backend.assign(self.candidate(), name, value)
                mysql.assert_not_called()

    def test_invalid_argument_values_are_not_echoed(self):
        output = io.StringIO()
        with redirect_stderr(output):
            result = tuning.main(["--unknown-secret", "never-print-this"])
        self.assertEqual(result, 1)
        self.assertNotIn("never-print-this", output.getvalue())


if __name__ == "__main__":
    unittest.main()
