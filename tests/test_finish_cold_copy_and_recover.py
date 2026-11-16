from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import finish_cold_copy_and_recover as handoff
import run_pipeline as pipeline
import source_activity
import cold_copy_sender


class ExecObserved(BaseException):
    pass


class ColdCopyHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clean, self.raw = self.root / "clean", self.root / "raw"
        self.clean.mkdir()
        self.raw.mkdir()
        self.snapshot = self.raw / "pkpbeacon-2026-01-01.sql.gz"
        self.snapshot.write_bytes(b"fixture raw archive")
        self.key = self.root / "identity with spaces"
        self.key.write_text("fixture identity")
        self.key.chmod(0o600)
        self.destination = self.clean / "mysql-2026-01-01.building"
        self.args = handoff.parser().parse_args([
            "--remote", "ubuntu@example.invalid", "--remote-project", "/srv/ojs_api",
            "--ssh-key", str(self.key), "--snapshot-date", "2026-01-01",
            "--credentials-env", str(self.root / "local.env"),
            "--api-password-file", str(self.root / "db-api-password"),
            "--mysql-bin", str(self.root / "mysql/bin"), "--snapshot", str(self.snapshot),
            "--clean-dir", str(self.clean), "--scratch-dir", str(self.root / "scratch"),
        ])
        self.records = []
        self.statuses = [(0, "loaded", "inactive")]
        self.copy_status = 0
        self.on_copy = self.completed_copy
        self.on_status = None
        self.run_patch = mock.patch.object(handoff.subprocess, "run", side_effect=self.run_command)
        self.run = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.stopped_patch = mock.patch.object(handoff, "assert_database_stopped")
        self.stopped = self.stopped_patch.start()
        self.addCleanup(self.stopped_patch.stop)
        self.exec_patch = mock.patch.object(handoff.os, "execve", side_effect=self.exec_child)
        self.exec = self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)
        self.sleep_patch = mock.patch.object(handoff.time, "sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def completed_copy(self):
        self.destination.mkdir(exist_ok=True)
        for name in ("ibdata1", "mysql.ibd"):
            (self.destination / name).write_bytes(b"fixture tablespace")
        checkpoint = {
            "pipeline_version": pipeline.PIPELINE_VERSION, "snapshot_version": "2026-01-01",
            "source_filename": self.snapshot.name, "phase": "finalizing",
            "source_size_bytes": 100, "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64,
        }
        (self.destination / pipeline.BUILD_STATE_NAME).write_text(json.dumps(checkpoint))

    def run_command(self, command, **kwargs):
        self.records.append(command[0])
        if command[0] == "systemctl":
            status, load, active = self.statuses.pop(0)
            if self.on_status:
                self.on_status()
            return subprocess.CompletedProcess(command, status,
                stdout=f"LoadState={load}\nActiveState={active}\n", stderr="")
        self.assertEqual(command[0], "rsync")
        if self.on_copy:
            self.on_copy()
        return subprocess.CompletedProcess(command, self.copy_status)

    def exec_child(self, executable, arguments, env):
        self.records.append("exec")
        descriptor = int(env[source_activity.INHERITED_FD_ENV])
        self.assertTrue(os.get_inheritable(descriptor))
        with (self.raw / source_activity.LOCK_NAME).open("r") as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(pipeline.PipelineError):
            with pipeline.pipeline_lock(self.clean):
                self.fail("pipeline lock was released before exec")
        self.assertEqual(executable, sys.executable)
        self.assertEqual(arguments[2], str(handoff.ROOT / "scripts/workstation_recover.py"))
        self.assertEqual(arguments[arguments.index("--snapshot") + 1], str(self.snapshot))
        self.assertEqual(arguments[arguments.index("--buffer-pool-size") + 1], "8G")
        raise ExecObserved

    def test_waits_for_old_copy_then_always_resyncs_and_hands_off_under_locks(self):
        self.args.wait_for_unit = "ojs-candidate-copy.service"
        self.statuses = [(0, "loaded", "active"), (0, "loaded", "deactivating"),
                         (0, "loaded", "inactive")]
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(ExecObserved):
            handoff.execute(self.args)
        self.assertEqual(self.records, ["systemctl", "systemctl", "systemctl", "rsync", "exec"])
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.stopped.call_count, 3)
        events = [json.loads(line.removeprefix("[progress] ")) for line in output.getvalue().splitlines()
                  if line.startswith("[progress] ")]
        copy_events = [event for event in events if event["stage"] == "workstation-cold-copy"]
        self.assertEqual([event["event"] for event in copy_events], ["started", "completed"])
        self.assertTrue(all("disk_free_bytes" in event for event in copy_events))
        self.assertNotIn(str(self.key), output.getvalue())

    def test_failed_or_disappeared_old_unit_can_only_proceed_after_successful_final_copy(self):
        self.args.wait_for_unit = "ojs-candidate-copy.service"
        for status in ((0, "loaded", "failed"), (1, "not-found", "inactive")):
            with self.subTest(status=status):
                self.records.clear()
                self.statuses = [status]
                with self.assertRaises(ExecObserved):
                    handoff.execute(self.args)
                self.assertEqual(self.records, ["systemctl", "rsync", "exec"])

    def test_final_transfer_failure_never_executes_even_if_old_unit_disappeared(self):
        self.args.wait_for_unit = "ojs-candidate-copy.service"
        self.statuses = [(1, "not-found", "inactive")]
        self.copy_status = 23
        with self.assertRaisesRegex(handoff.HandoffError, "transfer failed"):
            handoff.execute(self.args)
        self.exec.assert_not_called()
        self.assertEqual(self.records, ["systemctl", "rsync"])
        self.assertTrue(self.destination.is_dir())

    def test_no_old_unit_still_requires_successful_transfer(self):
        with self.assertRaises(ExecObserved):
            handoff.execute(self.args)
        self.assertEqual(self.records, ["rsync", "exec"])

    def test_success_status_without_complete_files_or_checkpoint_is_not_sufficient(self):
        for missing in ("ibdata1", "mysql.ibd", pipeline.BUILD_STATE_NAME):
            with self.subTest(missing=missing):
                def incomplete_copy():
                    self.completed_copy()
                    (self.destination / missing).unlink()
                self.on_copy = incomplete_copy
                with self.assertRaises(handoff.HandoffError):
                    handoff.execute(self.args)
        self.exec.assert_not_called()

    def test_copied_checkpoint_must_match_original_snapshot_and_finalizing_state(self):
        for key, invalid in (("snapshot_version", "2026-07-01"), ("source_filename", "other.sql"),
                             ("phase", "metadata_ready"), ("pipeline_version", "unknown"),
                             ("source_sha256", "invalid"), ("source_size_bytes", 0)):
            with self.subTest(key=key):
                def invalid_checkpoint():
                    self.completed_copy()
                    path = self.destination / pipeline.BUILD_STATE_NAME
                    values = json.loads(path.read_text())
                    values[key] = invalid
                    path.write_text(json.dumps(values))
                self.on_copy = invalid_checkpoint
                with self.assertRaisesRegex(handoff.HandoffError, "checkpoint"):
                    handoff.execute(self.args)
        self.exec.assert_not_called()

    def test_new_release_or_symlink_appearing_during_wait_prevents_resync(self):
        self.args.wait_for_unit = "ojs-candidate-copy.service"
        final = self.clean / "mysql-2026-01-01"
        for change in (lambda: final.mkdir(), lambda: self.destination.symlink_to(self.raw)):
            with self.subTest(change=change):
                self.statuses = [(0, "loaded", "inactive")]
                self.on_status = change
                with self.assertRaisesRegex(handoff.HandoffError, "release or a symlink"):
                    handoff.execute(self.args)
                if final.exists():
                    final.rmdir()
                self.destination.unlink(missing_ok=True)
        self.assertNotIn("rsync", self.records)
        self.exec.assert_not_called()

    def test_nested_symlink_appearing_during_transfer_prevents_exec(self):
        def symlink_copy():
            self.completed_copy()
            (self.destination / "unexpected-link").symlink_to(self.snapshot)
        self.on_copy = symlink_copy
        with self.assertRaisesRegex(handoff.HandoffError, "contains a symlink"):
            handoff.execute(self.args)
        self.exec.assert_not_called()
        self.assertEqual(self.stopped.call_count, 2)

    def test_existing_serving_pointer_or_publication_state_prevents_transfer(self):
        for name in ("mysql-live", "mysql-current", ".compact-update.json"):
            with self.subTest(name=name):
                pointer = self.clean / name
                pointer.symlink_to("unavailable-target")
                with self.assertRaisesRegex(handoff.HandoffError, "unpublished"):
                    handoff.execute(self.args)
                pointer.unlink()
        self.run.assert_not_called()
        self.exec.assert_not_called()

    def test_running_database_or_symlinked_clean_ancestor_is_rejected(self):
        self.stopped.side_effect = pipeline.PipelineError("database is still running")
        with self.assertRaises(pipeline.PipelineError):
            handoff.execute(self.args)
        self.stopped.side_effect = None
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.args.clean_dir = alias / "clean"
        with self.assertRaisesRegex(handoff.HandoffError, "regular local clean"):
            handoff.execute(self.args)
        self.run.assert_not_called()
        self.exec.assert_not_called()

    def test_pipeline_and_source_locks_exclude_transfer(self):
        with pipeline.pipeline_lock(self.clean), self.assertRaises(pipeline.PipelineError):
            handoff.execute(self.args)
        lock_path = self.raw / source_activity.LOCK_NAME
        with lock_path.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(source_activity.SourceActivityBusy):
                handoff.execute(self.args)
        self.run.assert_not_called()
        self.exec.assert_not_called()

    def test_snapshot_filename_mismatch_is_rejected_before_wait_or_copy(self):
        self.args.snapshot_date = "2026-07-01"
        self.args.wait_for_unit = "ojs-candidate-copy.service"
        with self.assertRaisesRegex(handoff.HandoffError, "filename"):
            handoff.execute(self.args)
        self.run.assert_not_called()

    def test_remote_command_uses_only_authenticated_read_only_sender(self):
        command = handoff.transfer_command(self.args, self.destination)
        self.assertIn("--partial", command)
        self.assertIn("--sparse", command)
        self.assertFalse(any(arg.startswith(("--delete", "--remove", "--inplace")) for arg in command))
        ssh = shlex.split(command[command.index("-e") + 1])
        self.assertEqual(ssh[ssh.index("-i") + 1], str(self.key))
        self.assertIn("StrictHostKeyChecking=yes", ssh)
        self.assertIn("IdentitiesOnly=yes", ssh)
        sender = shlex.split(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--rsync-path=")))
        self.assertEqual(sender, ["sudo", "-n", "-u", "ojs", "python3", "-B",
            "/srv/ojs_api/scripts/cold_copy_sender.py", "--snapshot", "2026-01-01", "--"])
        self.assertEqual(command[-2:], ["ubuntu@example.invalid:/srv/ojs_api/data/clean/mysql-2026-01-01.building/",
                                      str(self.destination) + "/"])

    def test_invalid_remote_inputs_or_date_are_rejected(self):
        for key, value in (("remote", "host;command"), ("remote_project", "/srv/../../etc"),
                           ("pipeline_user", "user;command"), ("snapshot_date", "2026-02-30")):
            previous = getattr(self.args, key)
            with self.subTest(key=key), self.assertRaises(handoff.HandoffError):
                setattr(self.args, key, value)
                handoff.transfer_command(self.args, self.destination)
            setattr(self.args, key, previous)

    def test_uninspectable_or_unknown_unit_state_never_proves_completion(self):
        for status in ((1, "loaded", "inactive"), (0, "error", "inactive"), (0, "loaded", "unknown")):
            with self.subTest(status=status):
                self.statuses = [status]
                with self.assertRaises(handoff.HandoffError):
                    handoff.wait_for_copy("ojs-candidate-copy.service", 1)
        self.assertNotIn("rsync", self.records)

    def test_wait_deadline_does_not_start_a_copy(self):
        self.statuses = [(0, "loaded", "active")]
        with mock.patch.object(handoff, "Progress"), \
                mock.patch.object(handoff.time, "monotonic", side_effect=[0, 3601]), \
                self.assertRaisesRegex(handoff.HandoffError, "deadline"):
            handoff.wait_for_copy("ojs-candidate-copy.service", 1)
        self.assertEqual(self.records, ["systemctl"])


@unittest.skipUnless(shutil.which("rsync"), "rsync required")
class HandoffRsyncContractTests(unittest.TestCase):
    def test_real_rsync_sender_arguments_match_read_only_gate_without_network(self):
        with TemporaryDirectory(prefix="ojs-rsync-contract-") as temporary:
            root = Path(temporary)
            key = root / "fixture-key"
            key.write_text("unused fixture key")
            args = SimpleNamespace(snapshot_date="2026-01-01", remote="ubuntu@example.invalid",
                                   remote_project="/srv/ojs_api", pipeline_user="ojs", ssh_key=key)
            command = handoff.transfer_command(args, root / "candidate")
            # Replace SSH completely: this process prints argv and exits before
            # any connection or file transfer can occur.
            probe = "import json,sys; print(json.dumps(sys.argv[1:]), file=sys.stderr); sys.exit(1)"
            command[command.index("-e") + 1] = shlex.join([sys.executable, "-c", probe])
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            arguments = json.loads(result.stderr.splitlines()[0])
            sender_args = arguments[arguments.index("--server"):]
            target = Path("/srv/ojs_api/data/clean/mysql-2026-01-01.building")
            self.assertEqual(cold_copy_sender.sender_arguments(sender_args, target), sender_args)


if __name__ == "__main__":
    unittest.main()
