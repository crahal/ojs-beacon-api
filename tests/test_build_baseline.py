from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import build_baseline as baseline


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clean = Path(self.temporary.name) / "clean"
        self.clean.mkdir()
        self.version = "2026-10-01"
        self.live = self.clean / f"mysql-{self.version}"
        self.live.mkdir()
        self.export = self.clean / f"pkpbeacon-clean-{self.version}.sql.gz"
        self.report_path = self.clean / f"pkpbeacon-changes-{self.version}.json"
        self.manifest_path = self.clean / f"pkpbeacon-release-{self.version}.json"
        self.marker_path = self.live / baseline.MARKER
        self.report = {"schema_version": 1, "snapshot": {
            "date": self.version, "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64,
        }, "release_guard": {"status": "passed"}}
        self.marker = {"schema_version": 1, "snapshot_date": self.version,
                       "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64,
                       "clean_only": True, "data_directory_name": self.live.name}
        self.export.write_bytes(b"test export; contents intentionally not decoded or read")
        self.export_hash = hashlib.sha256(self.export.read_bytes()).hexdigest()
        self.manifest = {"schema_version": 1, "snapshot_date": self.version,
                         "clean_export_filename": self.export.name,
                         "clean_export_sha256": self.export_hash,
                         "change_report_filename": self.report_path.name}
        self.write_documents()

    def write_documents(self):
        self.report_path.write_text(json.dumps(self.report))
        report_hash = hashlib.sha256(self.report_path.read_bytes()).hexdigest()
        self.manifest["change_report_sha256"] = report_hash
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.marker_path.write_text(json.dumps(self.marker))
        for path, digest in ((self.export, self.export_hash), (self.report_path, report_hash)):
            path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n")

    def assert_blocked(self, clean=None, live=...):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.assert_automatic_baseline(clean or self.clean, self.live if live is ... else live)
        self.assertEqual(str(caught.exception), baseline.ERROR_MESSAGE)
        self.assertIsNone(caught.exception.__cause__)

    def test_valid_baseline_is_read_only_and_returns_live_date(self):
        before = {path.relative_to(self.clean): path.read_bytes()
                  for path in self.clean.rglob("*") if path.is_file()}
        self.assertEqual(baseline.assert_automatic_baseline(self.clean, self.live), self.version)
        after = {path.relative_to(self.clean): path.read_bytes()
                 for path in self.clean.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_missing_or_empty_history_directory_is_genuine_bootstrap(self):
        for name in ("missing", "empty"):
            clean = Path(self.temporary.name) / name
            if name == "empty":
                clean.mkdir()
                (clean / ".pipeline.lock").touch()
                (clean / "mysql-2026-11-01.building").mkdir()
            self.assertIsNone(baseline.assert_automatic_baseline(clean, None))
        self.assertFalse((Path(self.temporary.name) / "missing").exists())

    def test_existing_history_without_live_pointer_needs_review(self):
        self.assert_blocked(live=None)

    def test_any_retained_manifest_or_incomplete_export_prevents_bootstrap(self):
        for name in ("pkpbeacon-release-2026-01-01.json", "pkpbeacon-clean-2026-01-01.sql.gz",
                     "pkpbeacon-changes-2026-01-01.json.sha256", "mysql-2026-01-01"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                clean = Path(directory)
                (clean / name).touch()
                self.assert_blocked(clean, live=None)

    def test_missing_each_required_artifact_blocks(self):
        for path in (self.export, self.report_path, self.manifest_path, self.marker_path,
                     self.export.with_name(self.export.name + ".sha256"),
                     self.report_path.with_name(self.report_path.name + ".sha256")):
            with self.subTest(name=path.name):
                content = path.read_bytes()
                path.unlink()
                self.assert_blocked()
                path.write_bytes(content)

    def test_symlink_for_each_required_file_is_rejected(self):
        for path in (self.export, self.report_path, self.manifest_path, self.marker_path,
                     self.export.with_name(self.export.name + ".sha256"),
                     self.report_path.with_name(self.report_path.name + ".sha256")):
            with self.subTest(name=path.name):
                original = path.with_name(path.name + ".saved")
                path.rename(original)
                path.symlink_to(original.name)
                self.assert_blocked()
                path.unlink()
                original.rename(path)

    def test_newer_artifacts_require_review_even_if_incomplete(self):
        for name in ("pkpbeacon-release-2026-11-01.json", "pkpbeacon-clean-2026-11-01.sql.gz",
                     "pkpbeacon-changes-2026-11-01.json", "mysql-2026-11-01"):
            with self.subTest(name=name):
                path = self.clean / name
                path.touch()
                self.assert_blocked()
                path.unlink()

    def test_older_audit_reports_without_large_exports_are_allowed(self):
        (self.clean / "pkpbeacon-release-2026-01-01.json").write_text("retained audit")
        (self.clean / "pkpbeacon-changes-2026-01-01.json").write_text("retained audit")
        self.assertEqual(baseline.assert_automatic_baseline(self.clean, self.live), self.version)

    def test_tampered_report_and_sidecar_are_rejected(self):
        self.report_path.write_bytes(self.report_path.read_bytes() + b" ")
        self.assert_blocked()
        self.write_documents()
        self.report_path.with_name(self.report_path.name + ".sha256").write_text("f" * 64)
        self.assert_blocked()

    def test_wrong_export_sidecar_or_filename_is_rejected(self):
        path = self.export.with_name(self.export.name + ".sha256")
        for content in (f"{'c' * 64}  {self.export.name}\n", f"{self.export_hash}  wrong.sql.gz\n",
                        f"{self.export_hash}  ../{self.export.name}\n"):
            with self.subTest(content=content):
                path.write_text(content)
                self.assert_blocked()

    def test_manifest_identity_mismatches_block(self):
        for key, value in (("schema_version", True), ("snapshot_date", "2026-09-01"),
                           ("clean_export_filename", "wrong.gz"), ("clean_export_sha256", "c" * 64),
                           ("change_report_filename", "wrong.json")):
            with self.subTest(key=key):
                old = self.manifest[key]
                self.manifest[key] = value
                self.write_documents()
                self.assert_blocked()
                self.manifest[key] = old

    def test_marker_identity_and_source_hash_mismatches_block(self):
        for key, value in (("schema_version", True), ("snapshot_date", "2026-09-01"),
                           ("data_directory_name", "mysql-2026-09-01"), ("clean_only", 1),
                           ("source_sha256", "c" * 64), ("build_sql_sha256", "invalid")):
            with self.subTest(key=key):
                old = self.marker[key]
                self.marker[key] = value
                self.write_documents()
                self.assert_blocked()
                self.marker[key] = old

    def test_checksumming_report_does_not_approve_bad_identity(self):
        for key, value in (("date", "2026-09-01"), ("source_sha256", "c" * 64),
                           ("build_sql_sha256", "d" * 64)):
            with self.subTest(key=key):
                old = self.report["snapshot"][key]
                self.report["snapshot"][key] = value
                self.write_documents()
                self.assert_blocked()
                self.report["snapshot"][key] = old

    def test_failed_release_guard_blocks_but_audited_override_is_supported(self):
        self.report["release_guard"]["status"] = "failed"
        self.write_documents()
        self.assert_blocked()
        self.report["release_guard"]["status"] = "overridden"
        self.write_documents()
        self.assertEqual(baseline.assert_automatic_baseline(self.clean, self.live), self.version)

    def test_metadata_read_limit_applies_to_all_documents(self):
        for path in (self.report_path, self.marker_path, self.manifest_path,
                     self.export.with_name(self.export.name + ".sha256")):
            with self.subTest(name=path.name):
                content = path.read_bytes()
                path.write_bytes(b" " * (baseline.MAX_METADATA_BYTES + 1))
                self.assert_blocked()
                path.write_bytes(content)

    def test_export_is_not_read_or_hashed_and_can_be_larger_than_metadata_limit(self):
        with self.export.open("ab") as output:
            output.truncate(baseline.MAX_METADATA_BYTES * 4)
        real_read = baseline._read_regular
        def guarded(path, *, metadata=True):
            if path == self.export:
                self.assertFalse(metadata, "huge export must not be read by the readiness gate")
            return real_read(path, metadata=metadata)
        with mock.patch.object(baseline, "_read_regular", side_effect=guarded):
            self.assertEqual(baseline.assert_automatic_baseline(self.clean, self.live), self.version)

    def test_export_replacement_during_check_is_rejected(self):
        real_read = baseline._read_regular
        def replaced(path, *, metadata=True):
            result = real_read(path, metadata=metadata)
            if path == self.marker_path:
                self.export.write_bytes(b"replaced")
            return result
        with mock.patch.object(baseline, "_read_regular", side_effect=replaced):
            self.assert_blocked()

    def test_named_pipe_is_rejected_without_opening(self):
        self.marker_path.unlink()
        os.mkfifo(self.marker_path)
        self.assert_blocked()

    def test_malformed_json_and_nonobjects_have_fixed_error(self):
        for content in (b"not json containing private information", b"[]", b"null", b"\xff"):
            with self.subTest(content=content):
                self.marker_path.write_bytes(content)
                self.assert_blocked()

    def test_bad_live_paths_and_symlinked_clean_are_rejected(self):
        for live in (self.live.with_name("mysql-2026-13-01"), self.live.parent,
                     Path(self.temporary.name) / self.live.name):
            self.assert_blocked(live=live)
        link = Path(self.temporary.name) / "clean-link"
        link.symlink_to(self.clean, target_is_directory=True)
        self.assert_blocked(clean=link)


if __name__ == "__main__":
    unittest.main()
