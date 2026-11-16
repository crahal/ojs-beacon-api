from __future__ import annotations

import contextlib
import fcntl
import io
import itertools
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import compact_update as update
from test_compact_update import FakeCoordinator, FakeRunner, release


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clean = self.root / "data" / "clean"
        self.raw = self.root / "data" / "raw"
        self.clean.mkdir(parents=True)
        self.raw.mkdir()
        self.paused = self.root / "data" / update.PAUSE_MARKER
        self.paused.write_text("Retain operator pause.\n")
        self.args = update.parser().parse_args([
            "--project-root", str(self.root), "--publish-release", "2026-01-01",
            "--min-free-gb", "0", "--reserve-gb", "0",
        ])
        self.runner = FakeRunner(self.root)
        self.coordinator = FakeCoordinator(self.args, self.runner)

    def pointer(self, name, candidate):
        path = self.clean / name
        path.unlink(missing_ok=True)
        path.symlink_to(candidate.datadir.name)

    def old_and_new(self):
        old = release(self.root, "2025-12-01")
        candidate = release(self.root, self.args.publish_release)
        self.pointer("mysql-live", old)
        self.runner.mount = old.datadir
        return old, candidate

    def test_paused_bootstrap_publishes_exact_date_without_current_or_building(self):
        candidate = release(self.root, self.args.publish_release)
        future = release(self.root, "2026-07-01")
        staging = self.clean / "mysql-2026-10-01.building"
        staging.mkdir()
        (staging / "expensive-work").write_bytes(b"retained")
        raw = self.raw / "pkpbeacon-2026-07-01.sql.gz"
        raw.write_bytes(b"retained archive")

        result = self.coordinator.execute()

        self.assertEqual(result, {"status": "published", "release": candidate.date,
                                  "published": True, "updates_paused": True})
        self.assertEqual(update.live_directory(self.clean), candidate.datadir)
        self.assertFalse((self.clean / "mysql-current").exists())
        self.assertEqual(self.coordinator.cold_checks, [candidate.date])
        self.assertEqual(self.coordinator.health_calls, [(candidate.date, candidate.datadir)])
        self.assertEqual(self.paused.read_text(), "Retain operator pause.\n")
        self.assertFalse(self.runner.children)
        self.assertFalse(any("--check" in command for command, _ in self.runner.commands))
        self.assertTrue(future.datadir.exists())
        self.assertTrue(future.export.exists())
        self.assertEqual((staging / "expensive-work").read_bytes(), b"retained")
        self.assertEqual(raw.read_bytes(), b"retained archive")
        self.assertFalse(self.coordinator.journal.exists())

    def test_selected_release_ignores_current_pointer_and_preserves_old_artifacts(self):
        old, candidate = self.old_and_new()
        future = release(self.root, "2026-07-01")
        self.pointer("mysql-current", future)

        self.coordinator.execute()

        self.assertEqual(update.live_directory(self.clean), candidate.datadir)
        self.assertEqual(update.live_directory(self.clean, "mysql-current"), future.datadir)
        self.assertTrue(old.datadir.exists())
        self.assertTrue(old.export.exists())
        self.assertFalse((self.clean / f"pkpbeacon-cleanup-{candidate.date}.json").exists())

    def test_explicit_release_also_works_without_pause(self):
        release(self.root, self.args.publish_release)
        self.paused.unlink()

        result = self.coordinator.execute()

        self.assertTrue(result["published"])
        self.assertFalse(result["updates_paused"])
        self.assertFalse(self.paused.exists())

    def test_health_failure_rolls_back_while_pause_and_artifacts_remain(self):
        old, candidate = self.old_and_new()
        self.coordinator.fail_health = True

        with self.assertRaisesRegex(update.UpdateError, "API mismatch"):
            self.coordinator.execute()

        self.assertEqual(update.live_directory(self.clean), old.datadir)
        self.assertEqual(self.runner.mount, old.datadir)
        self.assertTrue(self.paused.is_file())
        self.assertTrue(candidate.datadir.exists())
        self.assertTrue(candidate.export.exists())
        self.assertTrue(old.export.exists())
        self.assertFalse(self.coordinator.journal.exists())

    def test_missing_manifest_and_corrupt_export_cannot_stop_live_database(self):
        old, candidate = self.old_and_new()
        manifest = self.clean / f"pkpbeacon-release-{candidate.date}.json"
        manifest_bytes = manifest.read_bytes()
        manifest.unlink()

        with self.assertRaisesRegex(update.UpdateError, "incomplete release"):
            self.coordinator.execute()
        manifest.write_bytes(manifest_bytes)
        candidate.export.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            self.coordinator.execute()

        self.assertEqual(self.runner.mount, old.datadir)
        self.assertFalse(any("stop" in command for command, _ in self.runner.commands))
        self.assertFalse(self.coordinator.cold_checks)

    def test_mounted_candidate_cannot_be_cold_started(self):
        candidate = release(self.root, self.args.publish_release)
        self.runner.other_mounts = [candidate.datadir]

        with self.assertRaisesRegex(update.UpdateError, "mounted or used"):
            self.coordinator.execute()

        self.assertIsNone(update.live_directory(self.clean))
        self.assertFalse(self.coordinator.cold_checks)
        self.assertFalse(any("compose" in command for command, _ in self.runner.commands))

    def test_mysql_patch_mismatch_blocks_publication(self):
        release(self.root, self.args.publish_release)
        coordinator = update.Coordinator(self.args, self.runner)
        coordinator.env["OJS_MYSQL_IMAGE"] = "mysql:8.4.11"
        tools = update.run_pipeline.MySQLTools("mysqld", "mysql", "mysqladmin", "mysqldump", "8.4.0")

        with mock.patch.object(update.run_pipeline.MySQLTools, "discover", return_value=tools), \
             self.assertRaisesRegex(update.UpdateError, "does not match"):
            coordinator.execute()

        self.assertIsNone(update.live_directory(self.clean))
        self.assertFalse(self.runner.commands)

    def test_older_than_live_release_is_rejected(self):
        old, candidate = self.old_and_new()
        self.pointer("mysql-live", candidate)
        self.runner.mount = candidate.datadir
        self.args.publish_release = old.date

        with self.assertRaisesRegex(update.UpdateError, "older release"):
            self.coordinator.execute()

        self.assertEqual(update.live_directory(self.clean), candidate.datadir)
        self.assertFalse(self.coordinator.cold_checks)

    def test_verified_journal_recovery_does_not_prune(self):
        old, candidate = self.old_and_new()
        self.pointer("mysql-live", candidate)
        self.runner.mount = candidate.datadir
        update.atomic_json(self.coordinator.journal, {
            "schema_version": 1, "phase": "verified", "candidate": candidate.datadir.name,
            "previous": old.datadir.name, "snapshot_date": candidate.date,
            "cleanup": self.coordinator.cleanup_plan(candidate),
        })

        self.coordinator.execute()

        self.assertTrue(old.datadir.exists())
        self.assertTrue(old.export.exists())
        self.assertFalse(self.coordinator.journal.exists())
        self.assertTrue(self.paused.is_file())

    def test_source_activity_lock_prevents_publication(self):
        release(self.root, self.args.publish_release)
        path = self.raw / ".source-activity.lock"
        path.touch()
        with path.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(update.SourceActivityBusy):
                self.coordinator.execute()
        self.assertFalse(self.runner.commands)
        self.assertFalse(self.coordinator.cold_checks)

    def test_all_coordinator_locks_prevent_publication(self):
        release(self.root, self.args.publish_release)
        locks = (
            ("automatic", lambda: update.automatic_lock(self.clean)),
            ("pipeline", lambda: update.run_pipeline.pipeline_lock(self.clean)),
            ("publisher", lambda: update.publish_live.publisher_lock(self.root)),
        )
        for name, lock in locks:
            with self.subTest(lock=name), lock(), self.assertRaisesRegex(RuntimeError, "already running"):
                self.coordinator.execute()
        self.assertFalse(self.runner.commands)
        self.assertFalse(self.coordinator.cold_checks)

    def test_cli_modes_are_mutually_exclusive(self):
        modes = (["--check"], ["--publish-only"], ["--publish-release", "2026-01-01"],
                 ["--recover-exact-keys", "2026-01-01"])
        for first, second in itertools.combinations(modes, 2):
            with self.subTest(first=first, second=second), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as raised:
                update.parser().parse_args(first + second)
            self.assertEqual(raised.exception.code, 2)

    def test_invalid_publication_and_recovery_dates_fail_before_coordinator(self):
        for option, value in itertools.product(
            ("--publish-release", "--recover-exact-keys"),
            ("../2026-01-01", "2026-1-1", "2026-02-30", "2026-13-01"),
        ):
            with self.subTest(option=option, value=value), \
                 contextlib.redirect_stderr(io.StringIO()), mock.patch.object(update, "Coordinator") as coordinator, \
                 self.assertRaises(SystemExit) as raised:
                update.main([option, value])
            self.assertEqual(raised.exception.code, 2)
            coordinator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
