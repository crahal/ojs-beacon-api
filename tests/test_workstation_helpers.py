from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import cold_copy_sender as sender
import create_local_build_credentials as create
import workstation_recover as recovery


class CredentialCreationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "private"

    def test_generated_private_multiline_config_is_readable_by_recovery(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(create.main(["--directory", str(self.directory)]), 0)
        settings = recovery.credentials(self.directory / "local.env")
        password = recovery.private_text(self.directory / "db-api-password").strip()
        self.assertRegex(settings["MYSQL_ROOT_PASSWORD"], r"^[a-f0-9]{64}$")
        self.assertRegex(password, r"^[a-f0-9]{64}$")
        self.assertNotEqual(settings["MYSQL_ROOT_PASSWORD"], password)
        self.assertEqual(settings["OJS_DB_API_USER"], "ojs_api")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(settings["MYSQL_ROOT_PASSWORD"], output.getvalue())

    def test_existing_files_are_never_read_or_overwritten(self):
        create.create(self.directory)
        originals = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with mock.patch.object(create.secrets, "token_hex") as generate:
            with self.assertRaisesRegex(ValueError, "overwrite"):
                create.create(self.directory)
        generate.assert_not_called()
        self.assertEqual(originals, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_nonprivate_directory_and_wrong_owner_are_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            create.create(self.directory)
        self.directory.chmod(0o700)
        with mock.patch.object(create.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned"):
                create.create(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_directory_symlink_and_symlink_ancestor_are_rejected(self):
        actual = self.root / "actual"
        actual.mkdir(mode=0o700)
        self.directory.symlink_to(actual, target_is_directory=True)
        for target in (self.directory, self.directory / "nested"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "symlink"):
                create.create(target)
        self.assertEqual(list(actual.iterdir()), [])

    def test_preexisting_credential_symlink_or_fifo_is_not_followed(self):
        self.directory.mkdir(mode=0o700)
        target = self.directory / "db-api-password"
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(self.root / "missing-private-file")
                else:
                    os.mkfifo(target, 0o600)
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    create.create(self.directory)
                self.assertFalse((self.directory / "local.env").exists())
                target.unlink()

    def test_second_file_failure_removes_only_files_created_this_call(self):
        original_open = os.open

        def reject_second(path, *args, **kwargs):
            if path == "db-api-password":
                raise OSError("fixture failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(create.os, "open", side_effect=reject_second):
            with self.assertRaises(OSError):
                create.create(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])


class WorkstationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        create.create(self.private)
        self.config = self.private / "local.env"
        self.api = self.private / "db-api-password"
        self.mysql_bin = self.root / "mysql-bin"
        self.mysql_bin.mkdir()
        for name in ("mysqld", "mysql", "mysqladmin", "mysqldump"):
            executable = self.mysql_bin / name
            executable.write_text("fixture only; never execute")
            executable.chmod(0o700)
        self.scratch = self.root / "mysql-scratch"
        self.scratch.mkdir()
        self.snapshot = self.root / "pkpbeacon-2026-01-01.sql.gz"
        self.argv = ["--credentials-env", str(self.config), "--api-password-file", str(self.api),
                     "--mysql-bin", str(self.mysql_bin), "--mysql-version", "8.4.11",
                     "--snapshot", str(self.snapshot), "--clean-dir", str(self.root / "clean"),
                     "--scratch-dir", str(self.scratch), "--buffer-pool-size", "20G"]
        self.memory = mock.patch.object(recovery, "host_available_memory", return_value=40 * recovery.GIB)
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def test_private_reader_rejects_symlinks_fifo_permissions_and_oversize(self):
        link = self.private / "linked"
        link.symlink_to(self.config)
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.private_text(link)
        fifo = self.private / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.private_text(fifo)
        self.config.chmod(0o644)
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.private_text(self.config)
        self.config.chmod(0o600)
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.private_text(self.config, limit=1)
        with mock.patch.object(recovery.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(recovery.run_pipeline.PipelineError):
                recovery.private_text(self.config)

    def test_credentials_are_literal_data_and_selected_values_must_be_unique(self):
        self.config.write_text("MYSQL_ROOT_PASSWORD='$(never-run)'\nOJS_DB_API_USER=ojs_api\nIGNORED=anything\n")
        self.assertEqual(recovery.credentials(self.config),
                         {"MYSQL_ROOT_PASSWORD": "$(never-run)", "OJS_DB_API_USER": "ojs_api"})
        for text in ("MYSQL_ROOT_PASSWORD=one\nMYSQL_ROOT_PASSWORD=two\n",
                     "MYSQL_ROOT_PASSWORD=\n", "OJS_DB_API_USER=ojs_api\n"):
            self.config.write_text(text)
            with self.assertRaises(recovery.run_pipeline.PipelineError):
                recovery.credentials(self.config)

    def test_matching_tools_checks_all_four_explicit_local_executables(self):
        with mock.patch.object(recovery.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout="fixture Ver 8.4.11 for Linux", stderr="")) as version:
            tools = recovery.matching_tools(self.mysql_bin, "8.4.11")
        self.assertEqual(tools.version, "8.4.11")
        self.assertEqual(version.call_count, 4)
        for call in version.call_args_list:
            self.assertEqual(Path(call.args[0][0]).parent, self.mysql_bin)
            self.assertEqual(call.args[0][1:], ["--version"])
            self.assertEqual(call.kwargs["timeout"], 30)

    def test_missing_local_tool_never_falls_back_to_path(self):
        (self.mysql_bin / "mysqld").unlink()
        with mock.patch.object(recovery.subprocess, "run") as version:
            with self.assertRaisesRegex(recovery.run_pipeline.PipelineError, "all four local"):
                recovery.matching_tools(self.mysql_bin, "8.4.11")
        version.assert_not_called()

    def test_external_tool_symlink_and_mismatched_client_version_are_rejected(self):
        executable = self.mysql_bin / "mysqld"
        executable.unlink()
        outside = self.root / "other-mysqld"
        outside.write_text("fixture")
        outside.chmod(0o700)
        executable.symlink_to(outside)
        with self.assertRaisesRegex(recovery.run_pipeline.PipelineError, "all four local"):
            recovery.matching_tools(self.mysql_bin, "8.4.11")
        executable.unlink()
        executable.write_text("fixture")
        executable.chmod(0o700)
        results = [mock.Mock(returncode=0, stdout="fixture Ver 8.4.11", stderr=""),
                   mock.Mock(returncode=0, stdout="fixture Ver 8.4.10", stderr="")]
        with mock.patch.object(recovery.subprocess, "run", side_effect=results):
            with self.assertRaisesRegex(recovery.run_pipeline.PipelineError, "patch version"):
                recovery.matching_tools(self.mysql_bin, "8.4.11")

    def test_main_passes_recovery_flags_and_private_settings_without_shell_or_publish(self):
        settings = recovery.credentials(self.config)
        environment = {}

        def pipeline_main(argv):
            environment.update(os.environ)
            self.assertIn("--recover-touched-keys", argv)
            self.assertIn("--resume-building", argv)
            self.assertIn("--skip-download", argv)
            self.assertIn("--compact-storage", argv)
            self.assertNotIn("--publish", argv)
            self.assertEqual(argv[argv.index("--mysql-buffer-pool-size") + 1], "20G")
            return 7

        with mock.patch.dict(os.environ, {"OJS_DB_API_PASSWORD": "stale-fixture-secret"}), \
             mock.patch.object(recovery, "disk_scratch", return_value=self.scratch), \
             mock.patch.object(recovery.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout="fixture Ver 8.4.11", stderr="")), \
             mock.patch.object(recovery.run_pipeline, "main", side_effect=pipeline_main), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(output):
            self.assertEqual(recovery.main(self.argv), 7)
        self.assertEqual(environment["MYSQL_ROOT_PASSWORD"], settings["MYSQL_ROOT_PASSWORD"])
        self.assertEqual(environment["OJS_DB_API_PASSWORD_FILE"], str(self.api))
        self.assertNotIn("OJS_DB_API_PASSWORD", environment)
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(self.mysql_bin))
        self.assertEqual(environment["OJS_MYSQL_TMP_ROOT"], str(self.scratch))
        self.assertNotIn(settings["MYSQL_ROOT_PASSWORD"], output.getvalue())

    def test_bad_api_secret_or_version_failure_never_starts_pipeline_or_echoes_secret(self):
        for value in ("private-api\nsecond-line", "private-api\x00"):
            self.api.write_text(value)
            with mock.patch.object(recovery.run_pipeline, "main") as run, \
                 mock.patch.object(recovery, "disk_scratch", return_value=self.scratch), \
                 redirect_stderr(io.StringIO()) as output:
                self.assertEqual(recovery.main(self.argv), 1)
            run.assert_not_called()
            self.assertNotIn("private-api", output.getvalue())
        self.api.write_text("private-api")
        with mock.patch.object(recovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-command", 30)), \
             mock.patch.object(recovery, "disk_scratch", return_value=self.scratch), \
             mock.patch.object(recovery.run_pipeline, "main") as run, redirect_stderr(io.StringIO()) as output:
            self.assertEqual(recovery.main(self.argv), 1)
        run.assert_not_called()
        self.assertNotIn("private-command", output.getvalue())

    def test_buffer_pool_default_is_eight_gib_and_preflight_reserves_host_memory(self):
        recovery.host_available_memory.return_value = 15 * recovery.GIB
        with mock.patch.object(recovery.run_pipeline, "main") as run, \
             mock.patch.object(recovery, "credentials") as secrets, redirect_stderr(io.StringIO()):
            self.assertEqual(recovery.main(self.argv[:-2]), 1)
        secrets.assert_not_called()
        run.assert_not_called()
        recovery.host_available_memory.return_value = 16 * recovery.GIB
        with mock.patch.object(recovery.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout="fixture Ver 8.4.11", stderr="")), \
             mock.patch.object(recovery, "disk_scratch", return_value=self.scratch), \
             mock.patch.object(recovery.run_pipeline, "main", return_value=0) as run, \
             mock.patch.dict(os.environ), redirect_stderr(io.StringIO()):
            self.assertEqual(recovery.main(self.argv[:-2]), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--mysql-buffer-pool-size") + 1], "8G")

    def test_buffer_pool_limit_is_positive_and_has_known_units(self):
        for value in ("0", "-1G", "nan", "inf", "1.5G", "1T", "5G private"):
            with self.subTest(value=value), self.assertRaises(recovery.run_pipeline.PipelineError):
                recovery.buffer_pool_bytes(value)
        self.assertEqual(recovery.buffer_pool_bytes("1024M"), recovery.GIB)

    def test_scratch_rejects_ram_filesystems_including_nested_mount(self):
        mountinfo = self.root / "mountinfo"
        for filesystem in ("tmpfs", "ramfs", "devtmpfs", "hugetlbfs"):
            with self.subTest(filesystem=filesystem):
                mountinfo.write_text("1 0 8:1 / / rw - ext4 /dev/root rw\n"
                                    f"2 1 0:5 / {self.scratch} rw - {filesystem} memory rw\n")
                with self.assertRaisesRegex(recovery.run_pipeline.PipelineError, "disk-backed"):
                    recovery.disk_scratch(self.scratch, mountinfo=mountinfo)
        mountinfo.write_text("1 0 8:1 / / rw - ext4 /dev/root rw\n")
        self.assertEqual(recovery.disk_scratch(self.scratch, mountinfo=mountinfo), self.scratch)

    def test_scratch_mount_escapes_and_unreadable_measurement_are_handled(self):
        scratch = self.root / "scratch space"
        scratch.mkdir()
        mountinfo = self.root / "mountinfo"
        escaped = str(scratch).replace(" ", "\\040")
        mountinfo.write_text("1 0 8:1 / / rw - ext4 /dev/root rw\n"
                            f"2 1 0:5 / {escaped} rw - tmpfs memory rw\n")
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.disk_scratch(scratch, mountinfo=mountinfo)
        mountinfo.write_text("invalid")
        with self.assertRaises(recovery.run_pipeline.PipelineError):
            recovery.disk_scratch(scratch, mountinfo=mountinfo)


class ColdCopySenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "data/clean/mysql-2026-01-01.building"
        self.target.mkdir(parents=True)
        (self.root / "data/raw").mkdir()
        (self.root / "data/.updates-paused").touch()
        self.command = ["--server", "--sender", "-lDtprSe.iLsfxCIvu", ".", str(self.target) + "/"]
        self.argv = ["--snapshot", "2026-01-01", "--", *self.command]

    def test_only_expected_read_only_sender_options_and_exact_target_are_allowed(self):
        self.assertEqual(sender.sender_arguments(self.command, self.target), self.command)
        for option in ("--log-file=/private/path", "--remove-source-files", "--delete", "--files-from=/private",
                       "--daemon", "--copy-devices", "--config=/private", "--sender", "another-source"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                sender.sender_arguments(self.command[:3] + [option] + self.command[3:], self.target)
        for command in (self.command[:-1] + ["/private"], self.command[:1] + self.command[2:],
                        self.command[:-2] + ["/private", str(self.target)]):
            with self.assertRaises(ValueError):
                sender.sender_arguments(command, self.target)

    def test_both_locks_stay_held_through_stopped_check_state_read_and_transfer(self):
        held = set()

        @contextmanager
        def lock(name):
            held.add(name)
            try:
                yield
            finally:
                held.remove(name)

        def check(*args, **kwargs):
            self.assertEqual(held, {"source", "pipeline"})

        def read_state(*args):
            check()
            return {"snapshot_version": "2026-01-01", "phase": "finalizing"}

        def transfer(command, **kwargs):
            check()
            self.assertEqual(command, ["rsync", *self.command])
            self.assertEqual(kwargs, {"check": False})
            return mock.Mock(returncode=23)

        with mock.patch.object(sender, "ROOT", self.root), \
             mock.patch.object(sender, "source_activity", side_effect=lambda _: lock("source")), \
             mock.patch.object(sender, "pipeline_lock", side_effect=lambda _: lock("pipeline")), \
             mock.patch.object(sender, "assert_database_stopped", side_effect=check), \
             mock.patch.object(sender, "read_build_state", side_effect=read_state), \
             mock.patch.object(sender.subprocess, "run", side_effect=transfer):
            self.assertEqual(sender.main(self.argv), 23)
        self.assertEqual(held, set())

    def test_unpaused_live_mismatched_or_symlinked_candidates_never_transfer(self):
        for failure in ("unpaused", "live", "mismatch", "symlink"):
            with self.subTest(failure=failure), mock.patch.object(sender, "ROOT", self.root), \
                 mock.patch.object(sender, "source_activity"), mock.patch.object(sender, "pipeline_lock"), \
                 mock.patch.object(sender, "assert_database_stopped") as stopped, \
                 mock.patch.object(sender, "read_build_state", return_value={"snapshot_version": "2026-01-01", "phase": "finalizing"}) as state, \
                 mock.patch.object(sender.subprocess, "run") as transfer, redirect_stderr(io.StringIO()):
                pause = self.root / "data/.updates-paused"
                if failure == "unpaused":
                    pause.unlink()
                elif failure == "live":
                    stopped.side_effect = RuntimeError("fixture database is running")
                elif failure == "mismatch":
                    state.return_value = {"snapshot_version": "2026-01-02", "phase": "finalizing"}
                else:
                    (self.target / "checkpoint-link").symlink_to(self.root / "private")
                with self.assertRaises((SystemExit, RuntimeError)):
                    sender.main(self.argv)
                transfer.assert_not_called()
                if failure == "symlink":
                    state.assert_not_called()
                    stopped.assert_not_called()
                    (self.target / "checkpoint-link").unlink()
                pause.touch()


if __name__ == "__main__":
    unittest.main()
