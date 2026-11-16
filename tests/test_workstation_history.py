from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import workstation_history as history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clean = self.root / "clean"
        self.clean.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.baseline = "2026-01-01"
        self.july = self.snapshot("2026-07-01")
        self.october = self.snapshot("2026-10-01")
        self.release(self.baseline, None)
        self.argv = ["--credentials-env", str(self.root / "local.env"),
                     "--api-password-file", str(self.root / "db-api-password"),
                     "--mysql-bin", str(self.root / "mysql-bin"),
                     "--baseline-version", self.baseline,
                     "--snapshot", str(self.july.path), "--snapshot", str(self.october.path),
                     "--clean-dir", str(self.clean), "--scratch-dir", str(self.scratch)]
        self.tools = history.pipeline.MySQLTools("/fixture/mysqld", "/fixture/mysql", "/fixture/mysqladmin",
                                                 "/fixture/mysqldump", "8.4.11")
        patches = [
            mock.patch.object(history.recovery, "matching_tools", return_value=self.tools),
            mock.patch.object(history.recovery, "disk_scratch", return_value=self.scratch),
            mock.patch.object(history.recovery, "host_available_memory", return_value=30 * history.recovery.GIB),
            mock.patch.object(history.shutil, "disk_usage", return_value=mock.Mock(free=300 * history.recovery.GIB)),
            mock.patch.object(history.recovery, "credentials", return_value={"MYSQL_ROOT_PASSWORD": "never-log-root-secret"}),
            mock.patch.object(history.recovery, "private_text", return_value="never-log-api-secret\n"),
            mock.patch.object(history.pipeline, "assert_database_stopped"),
            mock.patch.object(history.os, "environ", {}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def snapshot(self, selected):
        raw = self.root / selected
        raw.mkdir()
        path = raw / f"pkpbeacon-{selected}.sql"
        path.write_text(f"-- MySQL dump fixture\n-- Dump completed on {selected} 00:00:00\n")
        return history.pipeline.Snapshot(path, selected, datetime.fromisoformat(selected), path.stat().st_size)

    def release(self, selected, previous, snapshot=None):
        snapshot_info = {"date": selected, "previous_snapshot_date": previous,
                         "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64}
        if snapshot:
            snapshot_info.update(source_filename=snapshot.path.name, source_size_bytes=snapshot.size,
                                 completed_at=snapshot.completed_at.isoformat(),
                                 source_sha256=hashlib.sha256(snapshot.path.read_bytes()).hexdigest())
        report = {"schema_version": 1, "snapshot": snapshot_info,
                  "release_guard": {"status": "passed"}, "article_events": {"total": 3}}
        report_path = history.pipeline.change_report_path(self.clean, selected)
        report_path.write_text(json.dumps(report))
        export = history.pipeline.clean_export_path(self.clean, selected)
        export.write_bytes(b"fixture-clean-export")
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        export_hash = hashlib.sha256(export.read_bytes()).hexdigest()
        history.pipeline.write_checksum_sidecar(report_path, report_hash)
        history.pipeline.write_checksum_sidecar(export, export_hash)
        history.pipeline.publish_release_manifest(self.clean, version=selected,
            export_path=export, export_sha256=export_hash, report_path=report_path, report_sha256=report_hash)
        directory = self.clean / f"mysql-{selected}"
        directory.mkdir(exist_ok=True)
        marker = {"schema_version": 1, "snapshot_date": selected, "source_sha256": snapshot_info["source_sha256"],
                  "build_sql_sha256": "b" * 64, "clean_only": True, "data_directory_name": directory.name}
        (directory / "OJS_COMPACT_RELEASE.json").write_text(json.dumps(marker))
        return report

    def run_main(self, arguments=None):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = history.main(self.argv if arguments is None else arguments)
        for secret in ("never-log-root-secret", "never-log-api-secret"):
            self.assertNotIn(secret, output.getvalue() + error.getvalue())
        return result, output.getvalue(), error.getvalue()

    def test_runs_in_order_with_separate_raw_paths_and_preserves_history(self):
        calls = []

        def pipeline(argv):
            selected = argv[argv.index("--snapshot") + 1]
            snapshot = next(item for item in (self.july, self.october) if str(item.path) == selected)
            previous = self.baseline if snapshot == self.july else self.july.version
            self.assertTrue(history.pipeline.release_artifacts_complete(self.clean, previous))
            self.assertEqual(argv[argv.index("--raw-dir") + 1], str(snapshot.path.parent))
            self.assertIn("--skip-download", argv)
            self.assertIn("--compact-storage", argv)
            for forbidden in ("--force-rebuild", "--rebuild-history", "--recover-exact-keys", "--recover-touched-keys"):
                self.assertNotIn(forbidden, argv)
            self.assertEqual(argv[argv.index("--metadata-workers") + 1], "1")
            self.assertEqual(argv[argv.index("--mysql-buffer-pool-size") + 1], "4G")
            self.release(snapshot.version, previous, snapshot)
            calls.append(snapshot.version)
            return 0

        with mock.patch.object(history.pipeline, "main", side_effect=pipeline):
            result, output, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [self.july.version, self.october.version])
        self.assertIn("not deployed", output)
        self.assertTrue((self.clean / f"mysql-{self.baseline}").is_dir())
        self.assertEqual(history.os.environ["MYSQL_ROOT_PASSWORD"], "never-log-root-secret")
        self.assertNotIn("OJS_DB_API_PASSWORD", history.os.environ)
        self.assertEqual(history.os.environ["OJS_METADATA_MIGRATION_RESERVE_BYTES"], str(40 * 1024**3))

    def test_completed_prefix_is_skipped_without_rebuilding_july(self):
        self.release(self.july.version, self.baseline, self.july)

        def pipeline(argv):
            self.assertEqual(argv[argv.index("--snapshot") + 1], str(self.october.path))
            self.release(self.october.version, self.july.version, self.october)
            return 0

        with mock.patch.object(history.pipeline, "main", side_effect=pipeline) as invoke:
            result, output, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(invoke.call_count, 1)
        self.assertIn("already committed 2026-07-01", output)

    def test_all_completed_is_idempotent(self):
        self.release(self.july.version, self.baseline, self.july)
        self.release(self.october.version, self.july.version, self.october)
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 0)
        invoke.assert_not_called()

    def test_all_completed_still_rejects_missing_final_datadir_or_corrupt_export(self):
        self.release(self.july.version, self.baseline, self.july)
        self.release(self.october.version, self.july.version, self.october)
        datadir = self.clean / f"mysql-{self.october.version}"
        (datadir / "OJS_COMPACT_RELEASE.json").unlink()
        datadir.rmdir()
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()
        self.release(self.october.version, self.july.version, self.october)
        history.pipeline.clean_export_path(self.clean, self.october.version).write_text("corrupt")
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()

    def test_missing_baseline_and_out_of_order_inputs_fail_before_pipeline(self):
        with mock.patch.object(history.pipeline, "main") as invoke:
            argv = list(self.argv)
            first, second = argv.index(str(self.july.path)), argv.index(str(self.october.path))
            argv[first], argv[second] = argv[second], argv[first]
            self.assertEqual(self.run_main(argv)[0], 1)
            history.pipeline.release_manifest_path(self.clean, self.baseline).unlink()
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()

    def test_gap_or_wrong_previous_history_is_rejected(self):
        self.release(self.october.version, self.baseline, self.october)
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
            self.release(self.july.version, self.baseline, self.july)
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()

    def test_staging_finalizing_is_preserved_and_never_started(self):
        staging = self.clean / f"mysql-{self.july.version}.building"
        staging.mkdir()
        state = staging / "OJS_PIPELINE_STATE.json"
        state.write_text(json.dumps({"phase": "finalizing"}))
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()
        self.assertEqual(json.loads(state.read_text())["phase"], "finalizing")

    def test_only_matching_metadata_ready_checkpoint_can_resume(self):
        history.attempt_receipt(self.clean, self.july, self.baseline, required=False)
        staging = self.clean / f"mysql-{self.july.version}.building"
        staging.mkdir()
        checkpoint = {"phase": "metadata_ready", "pipeline_version": history.pipeline.PIPELINE_VERSION,
                      "snapshot_version": self.july.version, "source_filename": self.july.path.name,
                      "source_size_bytes": self.july.size, "source_sha256": "a" * 64,
                      "build_sql_sha256": history.pipeline.sha256_small_file(history.pipeline.DEFAULT_BUILD_SQL)}
        (staging / "OJS_PIPELINE_STATE.json").write_text(json.dumps(checkpoint))
        with mock.patch.object(history.pipeline, "assert_database_stopped") as stopped:
            self.assertEqual(history.resume_arguments(self.clean, self.july, self.baseline), ["--resume-building"])
        stopped.assert_called_once_with(staging)
        checkpoint["metadata_storage_profile"] = history.pipeline.METADATA_STORAGE_PROFILE
        (staging / "OJS_PIPELINE_STATE.json").write_text(json.dumps(checkpoint))
        self.assertEqual(history.resume_arguments(self.clean, self.july, self.baseline), ["--resume-building"])
        for unsupported in (None, "unknown-storage-profile"):
            checkpoint["metadata_storage_profile"] = unsupported
            (staging / "OJS_PIPELINE_STATE.json").write_text(json.dumps(checkpoint))
            with self.assertRaises(history.pipeline.PipelineError):
                history.resume_arguments(self.clean, self.july, self.baseline)
        checkpoint.pop("metadata_storage_profile")
        checkpoint["source_filename"] = "wrong.sql"
        (staging / "OJS_PIPELINE_STATE.json").write_text(json.dumps(checkpoint))
        with self.assertRaises(history.pipeline.PipelineError):
            history.resume_arguments(self.clean, self.july, self.baseline)

    def test_resume_requires_matching_predecessor_receipt(self):
        with self.assertRaises(history.pipeline.PipelineError):
            history.attempt_receipt(self.clean, self.july, self.baseline, required=True)
        history.attempt_receipt(self.clean, self.july, self.baseline, required=False)
        history.attempt_receipt(self.clean, self.july, self.baseline, required=True)
        sidecar = history.pipeline.clean_export_path(self.clean, self.baseline)
        history.pipeline.write_checksum_sidecar(sidecar, "f" * 64)
        with self.assertRaises(history.pipeline.PipelineError):
            history.attempt_receipt(self.clean, self.july, self.baseline, required=True)

    def test_future_incomplete_build_is_not_adopted_after_earlier_snapshot(self):
        (self.clean / f"mysql-{self.october.version}.building").mkdir()
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()

    def test_success_exit_without_manifest_is_not_completion(self):
        with mock.patch.object(history.pipeline, "main", return_value=0) as invoke:
            self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(invoke.call_count, 1)

    def test_failure_stops_before_later_snapshot_and_pruning(self):
        with mock.patch.object(history.pipeline, "main", return_value=130) as invoke, \
                mock.patch.object(history, "prune_superseded") as prune:
            self.assertEqual(self.run_main(self.argv + ["--prune-superseded-datadirs"])[0], 130)
        self.assertEqual(invoke.call_count, 1)
        prune.assert_not_called()

    def test_check_only_validates_credentials_without_scanning_gzip_starting_mysql_or_pruning(self):
        with mock.patch.object(history.pipeline, "main") as invoke, \
                mock.patch.object(history.recovery, "credentials", return_value={"MYSQL_ROOT_PASSWORD": "private"}) as credentials, \
                mock.patch.object(history.pipeline, "inspect_snapshot") as inspect, \
                mock.patch.object(history, "prune_superseded") as prune:
            self.assertEqual(self.run_main(self.argv + ["--check-only", "--prune-superseded-datadirs"])[0], 0)
        invoke.assert_not_called()
        credentials.assert_called_once()
        inspect.assert_not_called()
        prune.assert_not_called()

    def test_capacity_rechecked_and_changed_disk_floor_stops_next_stage(self):
        capacity_calls = []
        original_capacity = history.check_capacity

        def capacity(*args, **kwargs):
            capacity_calls.append(True)
            if len(capacity_calls) == 3:
                raise history.pipeline.PipelineError("fixture disk floor reached")
            return original_capacity(*args, **kwargs)

        def pipeline(argv):
            self.release(self.july.version, self.baseline, self.july)
            return 0

        with mock.patch.object(history, "check_capacity", side_effect=capacity), \
                mock.patch.object(history.pipeline, "main", side_effect=pipeline) as invoke:
            self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(invoke.call_count, 1)

    def test_low_memory_stops_before_pipeline(self):
        with mock.patch.object(history.recovery, "host_available_memory", return_value=10 * history.recovery.GIB), \
                mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main()[0], 1)
        invoke.assert_not_called()

    def test_workflow_lock_excludes_second_runner(self):
        with history.workflow_lock(self.clean):
            with self.assertRaises(history.pipeline.PipelineError):
                with history.workflow_lock(self.clean):
                    self.fail("second lock acquired")

    def test_prune_keeps_exports_and_inputs_only_removes_superseded_owned_directory(self):
        self.release(self.july.version, self.baseline, self.july)
        with mock.patch.object(history, "unmounted"), mock.patch.object(history.pipeline, "assert_database_stopped"):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(history.prune_superseded(self.clean, self.baseline, self.july.version,
                                                        {self.baseline, self.july.version}))
        self.assertFalse((self.clean / f"mysql-{self.baseline}").exists())
        self.assertTrue(history.pipeline.clean_export_path(self.clean, self.baseline).is_file())
        self.assertTrue(history.pipeline.change_report_path(self.clean, self.baseline).is_file())
        self.assertTrue((self.clean / f"mysql-{self.july.version}").is_dir())
        self.assertTrue(self.july.path.is_file())

    def test_prune_rejects_unselected_or_current_dates(self):
        with self.assertRaises(history.pipeline.PipelineError):
            history.prune_superseded(self.clean, self.baseline, self.july.version, {self.july.version})
        with self.assertRaises(history.pipeline.PipelineError):
            history.prune_superseded(self.clean, self.baseline, self.baseline, {self.baseline})

    def test_prune_rejects_failed_guard_corrupt_export_pointer_and_symlink(self):
        self.release(self.july.version, self.baseline, self.july)
        datadir = self.clean / f"mysql-{self.baseline}"
        pointer = self.clean / "mysql-live"
        pointer.symlink_to(datadir.name)
        with self.assertRaises(history.pipeline.PipelineError), redirect_stdout(io.StringIO()):
            history.prune_superseded(self.clean, self.baseline, self.july.version, {self.baseline, self.july.version})
        pointer.unlink()
        (datadir / "unexpected-link").symlink_to(self.root)
        with mock.patch.object(history, "unmounted"), mock.patch.object(history.pipeline, "assert_database_stopped"):
            with self.assertRaises(history.pipeline.PipelineError), redirect_stdout(io.StringIO()):
                history.prune_superseded(self.clean, self.baseline, self.july.version, {self.baseline, self.july.version})
        history.pipeline.clean_export_path(self.clean, self.baseline).write_text("corrupted")
        with self.assertRaises(history.PublishError), redirect_stdout(io.StringIO()):
            history.prune_superseded(self.clean, self.baseline, self.july.version, {self.baseline, self.july.version})
        self.assertTrue(datadir.is_dir())

    def test_native_mount_and_docker_mounts_prevent_prune(self):
        target = self.clean / f"mysql-{self.baseline}"
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text(f"1 0 0:1 / {target}/nested rw - ext4 /dev/example rw\n")
        with self.assertRaises(history.pipeline.PipelineError):
            history.unmounted(target, mountinfo=mountinfo)
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/example rw\n")
        with mock.patch.object(history.shutil, "which", return_value="/fixture/docker"), \
                mock.patch.object(history.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0, stdout="abc\n"),
                    mock.Mock(returncode=0, stdout=json.dumps([{"Mounts": [{"Source": str(target)}]}]))]):
            with self.assertRaises(history.pipeline.PipelineError):
                history.unmounted(target, mountinfo=mountinfo)

    def test_docker_inspection_failure_fails_closed(self):
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/example rw\n")
        with mock.patch.object(history.shutil, "which", return_value="/fixture/docker"), \
                mock.patch.object(history.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaises(history.pipeline.PipelineError):
                history.unmounted(self.clean / f"mysql-{self.baseline}", mountinfo=mountinfo)

    def test_explicit_native_cleanup_skips_docker_but_not_native_mount_checks(self):
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/example rw\n")
        target = self.clean / f"mysql-{self.baseline}"
        with mock.patch.object(history.subprocess, "run") as docker, redirect_stdout(io.StringIO()) as output:
            history.unmounted(target, mountinfo=mountinfo, native_only=True)
        docker.assert_not_called()
        self.assertIn("Docker container references are not inspected", output.getvalue())
        mountinfo.write_text(f"1 0 0:1 {target} /bind-target rw - ext4 /dev/example rw\n")
        with self.assertRaises(history.pipeline.PipelineError):
            history.unmounted(target, mountinfo=mountinfo, native_only=True)

    def test_native_cleanup_requires_explicit_prune_opt_in(self):
        with mock.patch.object(history.pipeline, "main") as invoke:
            self.assertEqual(self.run_main(self.argv + ["--native-only-cleanup"])[0], 1)
        invoke.assert_not_called()

    def test_separate_filesystem_bind_aliases_use_filesystem_relative_roots(self):
        mountinfo = self.root / "mountinfo"
        target = Path("/home/user/.local/ojs/clean/mysql-2026-01-01")
        fixtures = (
            "/.local/ojs/clean/mysql-2026-01-01",  # whole candidate
            "/.local/ojs/clean/mysql-2026-01-01/database",  # subtree
            "/.local/ojs",  # ancestor includes candidate
        )
        for root in fixtures:
            mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                                 "2 1 0:9 / /home/user rw - zfs pool/home rw\n"
                                 f"3 1 0:9 {root} /alias rw - zfs pool/home rw\n")
            with self.subTest(root=root), self.assertRaises(history.pipeline.PipelineError):
                history.unmounted(target, mountinfo=mountinfo, native_only=True)
        # Unrelated siblings on the same filesystem are not aliases of this DB.
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                             "2 1 0:9 / /home/user rw - zfs pool/home rw\n"
                             "3 1 0:9 /unrelated /alias rw - zfs pool/home rw\n")
        with redirect_stdout(io.StringIO()):
            history.unmounted(target, mountinfo=mountinfo, native_only=True)

    def test_nsfs_mount_namespace_handle_outside_target_is_not_a_data_alias(self):
        mountinfo = self.root / "mountinfo"
        target = self.clean / f"mysql-{self.baseline}"
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                             "2 1 0:4 mnt:[4026533713] /run/snapd/ns/docker.mnt rw - nsfs nsfs rw\n")
        with redirect_stdout(io.StringIO()):
            history.unmounted(target, mountinfo=mountinfo, native_only=True)

    def test_nsfs_handle_inside_target_is_still_a_protected_mount(self):
        mountinfo = self.root / "mountinfo"
        target = self.clean / f"mysql-{self.baseline}"
        mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                             f"2 1 0:4 mnt:[4026533713] {target}/namespace rw - nsfs nsfs rw\n")
        with self.assertRaisesRegex(history.pipeline.PipelineError, "mount reference"):
            history.unmounted(target, mountinfo=mountinfo, native_only=True)

    def test_namespace_exception_does_not_hide_malformed_filesystem_roots(self):
        mountinfo = self.root / "mountinfo"
        target = self.clean / f"mysql-{self.baseline}"
        for filesystem, root in (("ext4", "mnt:[4026533713]"), ("nsfs", "mnt:[invalid]"),
                                 ("zfs", "relative/path"), ("nsfs", "unrecognized")):
            mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                                 f"2 1 0:4 {root} /outside rw - {filesystem} fixture rw\n")
            with self.subTest(filesystem=filesystem, root=root), self.assertRaisesRegex(
                    history.pipeline.PipelineError, "coordinates"):
                history.unmounted(target, mountinfo=mountinfo, native_only=True)


if __name__ == "__main__":
    unittest.main()
