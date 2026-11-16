from __future__ import annotations

from collections import Counter
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import compact_update as update
from build_baseline import BaselineError
from mount_safety import MountSafetyError
from source_activity import source_activity
from test_compact_update import FakeCoordinator, FakeRunner, release


SECRET = "PRIVATE_REPORT_SENTINEL_123"


class ReadinessRunner(FakeRunner):
    def __init__(self, root):
        super().__init__(root)
        self.service_output = "LoadState=loaded\nRefuseManualStart=no\nActiveState=inactive\n"
        self.timer_output = "ActiveState=inactive\nUnitFileState=disabled\n"
        self.cron_output = "active\n"
        self.timezone_output = "UTC\n"

    def run(self, command, *, cwd, env, timeout):
        if command[:3] == ["systemctl", "show", "ojs-api-update.service"]:
            return self.service_output
        if command[:3] == ["systemctl", "show", "ojs-api-update.timer"]:
            return self.timer_output
        if command[:3] == ["systemctl", "show", "cron.service"]:
            return self.cron_output
        if command[:2] == ["timedatectl", "show"]:
            return self.timezone_output
        return super().run(command, cwd=cwd, env=env, timeout=timeout)


class AutomaticReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clean = self.root / "data" / "clean"
        self.raw = self.root / "data" / "raw"
        self.clean.mkdir(parents=True)
        self.raw.mkdir()
        self.args = update.parser().parse_args([
            "--project-root", str(self.root), "--readiness",
            "--min-free-gb", "0", "--reserve-gb", "0",
            "--url", "https://private.invalid/" + SECRET,
        ])
        self.runner = ReadinessRunner(self.root)
        self.runner.probe = {"needs_download": True, "known_snapshot": None,
                             "date_hint": "2026-11-01", "remote": {"size": update.GIB}}
        self.coordinator = FakeCoordinator(self.args, self.runner)
        self.coordinator.env = {"MYSQL_ROOT_PASSWORD": SECRET,
                                "OJS_BUILD_WORKSPACE_GB": "5",
                                "OJS_MIN_AVAILABLE_MEMORY_MB": "0"}
        self.free_bytes = 10 * update.GIB
        self.enterContext(mock.patch.object(update.shutil, "disk_usage",
                                            side_effect=lambda _: mock.Mock(free=self.free_bytes)))
        self.scheduler_patch = mock.patch.object(self.coordinator, "scheduler_status", return_value={
            "cron_installed": True, "service_loaded": True,
            "cron_matches_template": True, "cron_active": True, "timezone_utc": True,
            "manual_start_refused": False, "service_active": False, "timer_enabled": False})
        self.scheduler_patch.start()
        self.addCleanup(self.scheduler_patch.stop)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.output))

    def pointer(self, name, candidate):
        pointer = self.clean / name
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(candidate.datadir.name)

    def tree(self):
        return {str(path.relative_to(self.root)): (
            "symlink" if path.is_symlink() else "dir" if path.is_dir() else path.read_bytes())
            for path in self.root.rglob("*")}

    def automatic(self):
        self.args.readiness = False
        return self.coordinator.execute()

    @contextlib.contextmanager
    def real_scheduler(self, *, contents=None, symlink=False, root_owned=True, mode=0o644):
        """Exercise real bounded cron checks against isolated, read-only fixtures."""
        self.scheduler_patch.stop()
        expected = (Path(__file__).parents[1] / "deploy/ojs-api.cron").read_bytes()
        deploy = self.root / "deploy"
        deploy.mkdir(exist_ok=True)
        (deploy / "ojs-api.cron").write_bytes(expected)
        cron = self.root / "cron-fixture"
        actual = self.root / "cron-fixture-target" if symlink else cron
        actual.write_bytes(expected if contents is None else contents)
        actual.chmod(mode)
        if symlink:
            cron.symlink_to(actual)
        cron_identity = actual.stat().st_dev, actual.stat().st_ino
        original_open, original_fstat = os.open, os.fstat

        def open_fixture(path, *args, **kwargs):
            return original_open(cron if path == "/etc/cron.d/ojs-api" else path, *args, **kwargs)

        def owned_stat(descriptor):
            info = original_fstat(descriptor)
            if (info.st_dev, info.st_ino) == cron_identity:
                fields = list(info)
                fields[4] = 0 if root_owned else 12345
                return os.stat_result(fields)
            return info

        with mock.patch.object(update, "Path", side_effect=lambda value: (
                cron if value == "/etc/cron.d/ojs-api" else Path(value))), \
                mock.patch.object(update.os, "open", side_effect=open_fixture), \
                mock.patch.object(update.os, "fstat", side_effect=owned_stat):
            yield
        cron.unlink()
        if symlink:
            actual.unlink()

    def test_readiness_creates_no_artifacts_and_exposes_no_private_input(self):
        live = release(self.root, "2026-10-01")
        self.pointer("mysql-live", live)
        before = self.tree()
        result = self.coordinator.execute()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["live_snapshot"], "2026-10-01")
        self.assertEqual(result["baseline_snapshot"], "2026-10-01")
        self.assertTrue(result["ready_for_build"])
        self.assertTrue(result["automatic_updates_allowed"])
        self.assertEqual(before, self.tree())
        for private in (SECRET, str(self.root), "https://private.invalid"):
            self.assertNotIn(private, json.dumps(result) + self.output.getvalue())
        self.assertFalse(self.runner.children)
        self.assertFalse(self.coordinator.health_calls)

    def test_active_source_lease_blocks_readiness_without_writes(self):
        with source_activity(self.raw):
            before = self.tree()
            result = self.coordinator.execute()
            self.assertIn("source_activity_busy", result["build_blockers"])
            self.assertFalse(result["ready_for_build"])
            self.assertEqual(before, self.tree())
        self.assertFalse(self.runner.children)

    def test_pause_is_an_activation_blocker_not_missing_build_configuration(self):
        marker = self.root / "data" / update.PAUSE_MARKER
        marker.write_text("Retain operator pause.")
        result = self.coordinator.execute()
        self.assertTrue(result["ready_for_build"])
        self.assertFalse(result["automatic_updates_allowed"])
        self.assertEqual(result["status"], "prepared")
        self.assertIn("updates_paused", result["activation_blockers"])
        self.assertEqual(marker.read_text(), "Retain operator pause.")

    def test_unknown_service_or_timer_state_never_enables_automatic_updates(self):
        self.scheduler_patch.stop()
        for service, timer, blocker in (
            ("LoadState=loaded\nRefuseManualStart=no\nActiveState=unknown\n",
             "ActiveState=inactive\nUnitFileState=disabled\n", "service_not_confirmed_idle"),
            ("LoadState=loaded\nRefuseManualStart=no\nActiveState=inactive\n",
             "", "alternate_scheduler_requires_review"),
            ("LoadState=loaded\nRefuseManualStart=no\nActiveState=inactive\n",
             "ActiveState=unknown\nUnitFileState=unknown\n", "alternate_scheduler_requires_review"),
        ):
            with self.subTest(blocker=blocker):
                self.runner.service_output, self.runner.timer_output = service, timer
                result = self.coordinator.execute()
                self.assertFalse(result["automatic_updates_allowed"])
                self.assertIn(blocker, result["activation_blockers"])

    def test_manual_start_refusal_and_missing_cron_are_reported_separately(self):
        self.coordinator.scheduler_status.return_value.update(
            manual_start_refused=True, cron_installed=False)
        result = self.coordinator.execute()
        self.assertTrue(result["ready_for_build"])
        self.assertIn("manual_start_not_permitted", result["activation_blockers"])
        self.assertIn("daily_cron_not_installed", result["activation_blockers"])
        self.assertFalse(self.runner.children)

    def test_real_matching_cron_active_daemon_and_utc_allow_activation_without_writes(self):
        with self.real_scheduler():
            before = self.tree()
            result = self.coordinator.execute()
            self.assertTrue(result["ready_for_build"])
            self.assertTrue(result["automatic_updates_allowed"])
            self.assertEqual(result["status"], "ready")
            self.assertTrue(result["scheduler"]["cron_matches_template"])
            self.assertTrue(result["scheduler"]["cron_active"])
            self.assertTrue(result["scheduler"]["timezone_utc"])
            self.assertEqual(before, self.tree())
        self.assertFalse(self.runner.children)

    def test_empty_foreign_and_symlink_cron_cannot_mark_automatic_updates_ready(self):
        for settings in ({"contents": b""}, {"contents": b"* * * * * root /foreign/job\n"},
                         {"symlink": True}):
            with self.subTest(settings=settings), self.real_scheduler(**settings):
                before = self.tree()
                result = self.coordinator.execute()
                self.assertTrue(result["ready_for_build"])
                self.assertFalse(result["automatic_updates_allowed"])
                self.assertEqual(result["status"], "prepared")
                self.assertFalse(result["scheduler"]["cron_matches_template"])
                self.assertIn("daily_cron_requires_review", result["activation_blockers"])
                self.assertEqual(before, self.tree())
        self.assertFalse(self.runner.children)

    def test_nonroot_writable_or_oversized_cron_cannot_mark_ready(self):
        for settings in ({"root_owned": False}, {"mode": 0o666},
                         {"contents": b"#" * 16385}):
            with self.subTest(settings=settings), self.real_scheduler(**settings):
                result = self.coordinator.execute()
                self.assertFalse(result["automatic_updates_allowed"])
                self.assertFalse(result["scheduler"]["cron_matches_template"])
                self.assertIn("daily_cron_requires_review", result["activation_blockers"])

    def test_stopped_or_unverified_cron_daemon_cannot_mark_ready(self):
        for state in ("inactive", "failed", "activating", "deactivating", "unknown", ""):
            with self.subTest(state=state), self.real_scheduler():
                self.runner.cron_output = state + "\n"
                result = self.coordinator.execute()
                self.assertTrue(result["scheduler"]["cron_matches_template"])
                self.assertTrue(result["ready_for_build"])
                self.assertFalse(result["automatic_updates_allowed"])
                self.assertIn("cron_daemon_not_confirmed_active", result["activation_blockers"])
        self.assertFalse(self.runner.children)

    def test_non_utc_or_unverified_timezone_cannot_mark_ready(self):
        for timezone in ("Europe/London", "America/New_York", ""):
            with self.subTest(timezone=timezone), self.real_scheduler():
                self.runner.timezone_output = timezone + "\n"
                result = self.coordinator.execute()
                self.assertTrue(result["scheduler"]["cron_matches_template"])
                self.assertTrue(result["scheduler"]["cron_active"])
                self.assertFalse(result["automatic_updates_allowed"])
                self.assertIn("utc_schedule_not_confirmed", result["activation_blockers"])

    def test_etc_utc_timezone_is_supported(self):
        with self.real_scheduler():
            self.runner.timezone_output = "Etc/UTC\n"
            result = self.coordinator.execute()
            self.assertTrue(result["automatic_updates_allowed"])

    def test_malformed_source_metadata_is_rejected_without_leaking_input(self):
        for probe in (
            {"needs_download": True, "remote": None},
            {"needs_download": True, "remote": []},
            {"needs_download": True, "remote": {"size": SECRET}},
            {"needs_download": True, "remote": {"size": True}},
            {"needs_download": True, "remote": {"size": 1}, "date_hint": SECRET},
            {"needs_download": True, "remote": {"size": 1}, "date_hint": "2026-02-31"},
        ):
            with self.subTest(probe=probe):
                self.runner.probe = probe
                result = self.coordinator.execute()
                self.assertIn("source_or_capacity_check_unavailable", result["build_blockers"])
                self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())
                self.assertFalse(self.runner.children)

    def test_missing_live_baseline_blocks_before_download(self):
        live = release(self.root, "2026-10-01")
        self.pointer("mysql-live", live)
        live.export.unlink()
        with self.assertRaises(BaselineError):
            self.automatic()
        self.assertFalse(self.runner.children)
        self.assertFalse(self.coordinator.health_calls)
        self.assertEqual(update.live_directory(self.clean), live.datadir)

    def test_missing_invalid_or_insufficient_budget_prevents_download(self):
        for value, code in ((None, "build_workspace_gb_unset"), ("NaN", "build_workspace_gb_invalid"),
                            ("0", "build_workspace_gb_invalid"), ("-1", "build_workspace_gb_invalid"),
                            ("50", "insufficient_data_raw_space")):
            with self.subTest(value=value):
                if value is None:
                    self.coordinator.env.pop("OJS_BUILD_WORKSPACE_GB", None)
                else:
                    self.coordinator.env["OJS_BUILD_WORKSPACE_GB"] = value
                with self.assertRaisesRegex(update.UpdateError, code):
                    self.automatic()
                self.assertFalse(self.runner.children)
                self.assertFalse(self.coordinator.health_calls)

    def test_space_is_rechecked_after_download_before_build_and_publication(self):
        def download_finished(command):
            self.assertIn("download_beacon.py", " ".join(command))
            self.free_bytes = update.GIB
        self.runner.on_child = download_finished
        with self.assertRaisesRegex(update.UpdateError, "insufficient_data_raw_space"):
            self.automatic()
        self.assertEqual(len(self.runner.children), 1)
        self.assertFalse(self.coordinator.cold_checks)
        self.assertFalse(self.coordinator.health_calls)
        self.assertFalse((self.clean / "mysql-live").exists())

    def test_baseline_is_rechecked_after_download_before_build(self):
        live = release(self.root, "2026-10-01")
        self.pointer("mysql-live", live)
        self.runner.on_child = lambda _: live.export.unlink()
        with self.assertRaises(BaselineError):
            self.automatic()
        self.assertEqual(len(self.runner.children), 1)
        self.assertFalse(self.coordinator.cold_checks)
        self.assertFalse(self.coordinator.health_calls)
        self.assertEqual(update.live_directory(self.clean), live.datadir)

    def test_mount_safety_is_checked_while_planning_and_immediately_before_cleanup(self):
        old = release(self.root, "2026-01-01")
        new = release(self.root, "2026-10-01")
        self.pointer("mysql-live", new)
        self.runner.mount = new.datadir
        with mock.patch.object(update, "assert_cleanup_unmounted") as check:
            plan = self.coordinator.cleanup_plan(new)
            planned_counts = Counter(call.args[0] for call in check.call_args_list)
            self.assertEqual(planned_counts[old.datadir], 1)
            self.assertEqual(planned_counts[old.export], 1)
            self.coordinator.finish({"schema_version": 1, "phase": "verified", "cleanup": plan}, new)
            completed_counts = Counter(call.args[0] for call in check.call_args_list)
        self.assertEqual(completed_counts[old.datadir], 2)
        self.assertEqual(completed_counts[old.export], 2)
        self.assertFalse(old.datadir.exists())
        self.assertTrue(new.datadir.exists())

    def test_mount_rejection_during_planning_retains_all_old_files(self):
        old = release(self.root, "2026-01-01")
        new = release(self.root, "2026-10-01")
        with mock.patch.object(update, "assert_cleanup_unmounted",
                               side_effect=MountSafetyError("mounted; retain")) as check:
            self.assertEqual(self.coordinator.cleanup_plan(new), [])
        check.assert_called_once_with(old.datadir)
        self.assertTrue(old.datadir.exists())
        self.assertTrue(old.export.exists())

    def test_new_mount_between_planning_and_deletion_blocks_and_retains_work(self):
        old = release(self.root, "2026-01-01")
        new = release(self.root, "2026-10-01")
        self.pointer("mysql-live", new)
        self.runner.mount = new.datadir
        with mock.patch.object(update, "assert_cleanup_unmounted"):
            plan = self.coordinator.cleanup_plan(new)
        state = {"schema_version": 1, "phase": "verified", "cleanup": plan}
        update.atomic_json(self.coordinator.journal, state)
        with mock.patch.object(update, "assert_cleanup_unmounted",
                               side_effect=MountSafetyError("new mount; retain")) as check:
            with self.assertRaises(MountSafetyError):
                self.coordinator.finish(state, new)
        check.assert_called_once_with(old.datadir)
        self.assertTrue(old.datadir.exists())
        self.assertTrue(old.export.exists())
        self.assertTrue(self.coordinator.journal.exists())
        self.assertEqual(update.live_directory(self.clean), new.datadir)


if __name__ == "__main__":
    unittest.main()
