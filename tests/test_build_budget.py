import fcntl
import math
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import build_budget


GIB = build_budget.GIB


class BuildBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clean = self.location("clean", device=1)
        self.raw = self.location("raw", device=1)
        self.scratch = self.location("scratch", device=2)
        self.free = {self.clean: 300 * GIB, self.raw: 300 * GIB, self.scratch: 100 * GIB}
        patch = mock.patch.object(build_budget.shutil, "disk_usage", side_effect=self.disk_usage)
        patch.start()
        self.addCleanup(patch.stop)
        self.env = {"OJS_BUILD_WORKSPACE_GB": "200", "OJS_BUILD_SCRATCH_GB": "40"}

    def location(self, name, *, device):
        path = mock.Mock(spec=Path, name=name)
        path.is_dir.return_value = True
        path.is_symlink.return_value = False
        path.stat.return_value = mock.Mock(st_dev=device)
        return path

    def disk_usage(self, path):
        return mock.Mock(free=self.free[path])

    def budget(self, **changes):
        settings = dict(clean=self.clean, raw=self.raw, scratch=self.scratch,
                        env=self.env, needs_download=True, remote_size=30 * GIB,
                        reserve_gib=20, minimum_gib=20)
        settings.update(changes)
        return build_budget.build_budget(**settings)

    def test_reviewed_positive_budgets_approve_with_per_filesystem_details(self):
        report = self.budget()
        self.assertTrue(report["approved"])
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["budget_is_operator_estimate"])
        data, scratch = report["filesystems"]
        self.assertEqual(data, {"roles": ["data", "raw"], "available_bytes": 300 * GIB,
                                "working_bytes": 200 * GIB, "download_bytes": 30 * GIB,
                                "reserve_bytes": 20 * GIB, "required_free_bytes": 250 * GIB,
                                "enough_space": True})
        self.assertEqual(scratch["required_free_bytes"], 60 * GIB)

    def test_missing_and_blank_workspace_budgets_block(self):
        for value in (None, "", "   "):
            env = self.env.copy()
            if value is None:
                del env["OJS_BUILD_WORKSPACE_GB"]
            else:
                env["OJS_BUILD_WORKSPACE_GB"] = value
            with self.subTest(value=value):
                report = self.budget(env=env)
                self.assertFalse(report["approved"])
                self.assertIn("build_workspace_gb_unset", report["blockers"])

    def test_explicit_scratch_requires_its_own_budget(self):
        report = self.budget(env={"OJS_BUILD_WORKSPACE_GB": "200"})
        self.assertFalse(report["approved"])
        self.assertIn("build_scratch_gb_unset", report["blockers"])

    def test_default_datadir_scratch_does_not_require_separate_budget(self):
        report = self.budget(scratch=None, env={"OJS_BUILD_WORKSPACE_GB": "200"})
        self.assertTrue(report["approved"])
        self.assertEqual(len(report["filesystems"]), 1)

    def test_nonpositive_nonfinite_and_huge_budgets_block_without_echoing_values(self):
        for setting in self.env:
            for value in ("0", "-1", "nan", "NaN", "inf", "-inf", "1e309",
                          "1e308", str(2**33), "private-invalid-value", None, object()):
                env = self.env.copy()
                env[setting] = value
                with self.subTest(setting=setting, value=value):
                    report = self.budget(env=env)
                    self.assertFalse(report["approved"])
                    self.assertIn(setting.lower().removeprefix("ojs_") + "_invalid", report["blockers"])
                    self.assertNotIn("private-invalid-value", str(report))

    def test_fractional_budgets_round_up_to_whole_bytes(self):
        report = self.budget(scratch=None, needs_download=False, reserve_gib=0, minimum_gib=0,
                             env={"OJS_BUILD_WORKSPACE_GB": "0.0000000001"})
        self.assertTrue(report["approved"])
        self.assertEqual(report["filesystems"][0]["working_bytes"], 1)

    def test_unknown_nonpositive_or_noninteger_remote_size_blocks_download(self):
        for size in (None, 0, -1, True, False, "123", 1.5, math.nan, math.inf, 2**63, object()):
            with self.subTest(size=size):
                report = self.budget(remote_size=size)
                self.assertFalse(report["approved"])
                self.assertIn("source_size_unknown", report["blockers"])

    def test_existing_validated_download_needs_no_new_download_bytes(self):
        for size in (None, 0, "unknown", 30 * GIB):
            with self.subTest(size=size):
                report = self.budget(needs_download=False, remote_size=size)
                self.assertTrue(report["approved"])
                self.assertEqual(report["filesystems"][0]["download_bytes"], 0)
                self.assertEqual(report["filesystems"][0]["required_free_bytes"], 220 * GIB)

    def test_shared_filesystem_sums_workspace_scratch_download_and_single_reserve(self):
        self.scratch.stat.return_value.st_dev = 1
        self.free[self.scratch] = 300 * GIB
        report = self.budget()
        self.assertTrue(report["approved"])
        self.assertEqual(len(report["filesystems"]), 1)
        group = report["filesystems"][0]
        self.assertEqual(group["roles"], ["data", "raw", "scratch"])
        self.assertEqual(group["working_bytes"], 240 * GIB)
        self.assertEqual(group["download_bytes"], 30 * GIB)
        self.assertEqual(group["reserve_bytes"], 20 * GIB)
        self.assertEqual(group["required_free_bytes"], 290 * GIB)

    def test_shared_filesystem_uses_lowest_observed_free_bytes(self):
        self.free[self.raw] = 249 * GIB
        report = self.budget()
        self.assertFalse(report["approved"])
        self.assertEqual(report["filesystems"][0]["available_bytes"], 249 * GIB)
        self.assertIn("insufficient_data_raw_space", report["blockers"])

    def test_separate_filesystems_cannot_pool_free_space(self):
        self.free[self.clean] = self.free[self.raw] = 249 * GIB
        self.free[self.scratch] = 2000 * GIB
        report = self.budget()
        self.assertFalse(report["approved"])
        self.assertIn("insufficient_data_raw_space", report["blockers"])
        self.assertTrue(report["filesystems"][1]["enough_space"])

    def test_separate_raw_filesystem_bears_download_plus_its_own_reserve(self):
        self.raw.stat.return_value.st_dev = 3
        self.free[self.raw] = 49 * GIB
        report = self.budget()
        self.assertFalse(report["approved"])
        groups = {tuple(group["roles"]): group for group in report["filesystems"]}
        self.assertEqual(groups[("data",)]["required_free_bytes"], 220 * GIB)
        self.assertEqual(groups[("raw",)]["required_free_bytes"], 50 * GIB)
        self.assertIn("insufficient_raw_space", report["blockers"])

    def test_minimum_starting_floor_is_not_added_twice(self):
        report = self.budget(minimum_gib=280)
        self.assertEqual(report["filesystems"][0]["required_free_bytes"], 280 * GIB)
        self.assertEqual(report["filesystems"][1]["required_free_bytes"], 280 * GIB)
        self.assertIn("insufficient_scratch_space", report["blockers"])

    def test_exact_space_boundary_is_accepted(self):
        self.free[self.clean] = self.free[self.raw] = 250 * GIB
        self.free[self.scratch] = 60 * GIB
        self.assertTrue(self.budget()["approved"])

    def test_reserve_and_floor_round_up_fractional_bytes(self):
        report = self.budget(scratch=None, needs_download=False, reserve_gib=0.0000000001,
                             minimum_gib=0, env={"OJS_BUILD_WORKSPACE_GB": "1"})
        self.assertEqual(report["filesystems"][0]["required_free_bytes"], GIB + 1)

    def test_invalid_reserves_and_floors_fail_closed(self):
        for setting in ("reserve_gib", "minimum_gib"):
            for value in (-1, math.nan, math.inf, -math.inf, 1e308, 2**33):
                with self.subTest(setting=setting, value=value):
                    with self.assertRaisesRegex(ValueError, "invalid build budget reserve"):
                        self.budget(**{setting: value})

    def test_unavailable_missing_symlink_and_negative_free_paths_block(self):
        for role, location in (("data", self.clean), ("raw", self.raw), ("scratch", self.scratch)):
            for failure in ("missing", "symlink", "stat", "negative"):
                with self.subTest(role=role, failure=failure):
                    if failure == "missing":
                        location.is_dir.return_value = False
                    elif failure == "symlink":
                        location.is_symlink.return_value = True
                    elif failure == "stat":
                        location.stat.side_effect = PermissionError("private path")
                    else:
                        self.free[location] = -1
                    report = self.budget()
                    self.assertFalse(report["approved"])
                    self.assertIn(role + "_filesystem_unavailable", report["blockers"])
                    self.assertNotIn("private path", str(report))
                    location.is_dir.return_value = True
                    location.is_symlink.return_value = False
                    location.stat.side_effect = None
                    self.free[location] = 300 * GIB

    def test_disk_usage_failure_blocks_without_exposing_private_paths(self):
        with mock.patch.object(build_budget.shutil, "disk_usage", side_effect=PermissionError("private path")):
            report = self.budget()
        self.assertFalse(report["approved"])
        self.assertEqual(set(report["blockers"]),
                         {"data_filesystem_unavailable", "raw_filesystem_unavailable", "scratch_filesystem_unavailable"})
        self.assertEqual(report["filesystems"], [])
        self.assertNotIn("private path", str(report))


class ExistingLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = self.root / ".pipeline.lock"

    def test_absent_lock_does_not_create_anything(self):
        self.assertFalse(build_budget.existing_lock_busy(self.lock))
        self.assertFalse(self.lock.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unlocked_regular_file_is_preserved_and_lock_released(self):
        self.lock.write_text("private fixture content")
        before = self.lock.stat()
        self.assertFalse(build_budget.existing_lock_busy(self.lock))
        after = self.lock.stat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_size),
                         (after.st_dev, after.st_ino, after.st_size))
        self.assertEqual(self.lock.read_text(), "private fixture content")
        with self.lock.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_existing_exclusive_flock_is_busy_and_not_unlocked(self):
        self.lock.touch()
        with self.lock.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(build_budget.existing_lock_busy(self.lock))
            with self.lock.open("rb") as observer:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(observer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(build_budget.existing_lock_busy(self.lock))

    def test_existing_shared_flock_is_busy(self):
        self.lock.touch()
        with self.lock.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertTrue(build_budget.existing_lock_busy(self.lock))

    def test_symlink_and_dangling_symlink_fail_without_modifying_target(self):
        target = self.root / "target"
        for present in (False, True):
            if present:
                target.write_text("preserve me")
            self.lock.symlink_to(target)
            with self.subTest(present=present):
                with self.assertRaises(OSError):
                    build_budget.existing_lock_busy(self.lock)
                self.assertTrue(self.lock.is_symlink())
                if present:
                    self.assertEqual(target.read_text(), "preserve me")
            self.lock.unlink()

    def test_directory_is_rejected(self):
        self.lock.mkdir()
        with self.assertRaisesRegex(OSError, "not a regular file"):
            build_budget.existing_lock_busy(self.lock)
        self.assertTrue(self.lock.is_dir())

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(self.lock)
        original_open = os.open

        def checked_open(path, flags):
            # Fail rather than hanging the test if the nonblocking protection
            # is accidentally removed from a future version of the helper.
            self.assertTrue(flags & os.O_NONBLOCK)
            self.assertFalse(flags & os.O_CREAT)
            self.assertTrue(flags & os.O_NOFOLLOW)
            return original_open(path, flags)

        with mock.patch.object(build_budget.os, "open", side_effect=checked_open):
            with self.assertRaisesRegex(OSError, "not a regular file"):
                build_budget.existing_lock_busy(self.lock)
        self.assertTrue(stat.S_ISFIFO(self.lock.stat().st_mode))

    def test_changed_inode_during_observation_is_rejected(self):
        self.lock.touch()
        actual = self.lock.stat()
        with mock.patch.object(Path, "stat", return_value=mock.Mock(st_dev=actual.st_dev, st_ino=actual.st_ino + 1)):
            with self.assertRaisesRegex(OSError, "changed during observation"):
                build_budget.existing_lock_busy(self.lock)
        self.assertFalse(build_budget.existing_lock_busy(self.lock))

    def test_disappeared_lock_after_open_does_not_become_unlocked_result(self):
        self.lock.touch()
        with mock.patch.object(Path, "stat", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                build_budget.existing_lock_busy(self.lock)
        self.assertFalse(build_budget.existing_lock_busy(self.lock))

    def test_open_permission_failure_is_not_treated_as_missing(self):
        with mock.patch.object(build_budget.os, "open", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                build_budget.existing_lock_busy(self.lock)

    def test_lock_error_closes_descriptor(self):
        self.lock.touch()
        original_close = os.close
        with mock.patch.object(build_budget.fcntl, "flock", side_effect=OSError("flock failed")), \
                mock.patch.object(build_budget.os, "close", wraps=original_close) as close:
            with self.assertRaisesRegex(OSError, "flock failed"):
                build_budget.existing_lock_busy(self.lock)
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
