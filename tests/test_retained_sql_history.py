"""Retired exports must not turn unchanged plain SQL history into a backfill."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import run_pipeline


def fixture(root):
    raw, clean = root / "raw", root / "clean"
    raw.mkdir()
    clean.mkdir()
    snapshots = []
    for version in ("2026-01-01", "2026-07-01", "2026-11-01"):
        source = raw / f"pkpbeacon-{version}.sql"
        source.write_text("-- MySQL dump 10.13\nCREATE TABLE example (id INTEGER);\n"
                          f"-- Dump completed on {version} 12:00:00\n")
        snapshot = run_pipeline.inspect_snapshot(source)
        snapshots.append(snapshot)
        if version == "2026-11-01":
            continue
        export = run_pipeline.clean_export_path(clean, version)
        export.write_bytes(b"synthetic clean export")
        report = run_pipeline.change_report_path(clean, version)
        report.write_text(json.dumps({"schema_version": 1, "snapshot": {
            "date": version, "completed_at": snapshot.completed_at.isoformat(),
            "source_filename": source.name, "source_size_bytes": snapshot.size,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }}) + "\n")
        export_sha = hashlib.sha256(export.read_bytes()).hexdigest()
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        run_pipeline.write_checksum_sidecar(export, export_sha)
        run_pipeline.write_checksum_sidecar(report, report_sha)
        run_pipeline.publish_release_manifest(
            clean, version=version, export_path=export, export_sha256=export_sha,
            report_path=report, report_sha256=report_sha,
        )
        if version == "2026-01-01":
            export.unlink()
    return raw, clean, snapshots


class RetainedSQLHistoryTests(unittest.TestCase):
    def test_unchanged_plain_sql_remains_processed_after_export_retirement(self):
        with TemporaryDirectory(prefix="ojs-retained-sql-") as directory:
            raw, clean, snapshots = fixture(Path(directory))
            self.assertTrue(run_pipeline.retained_snapshot_matches_release(snapshots[0], clean))
            self.assertEqual(run_pipeline.snapshots_to_process(
                target=snapshots[2], raw_dir=raw, clean_dir=clean,
                explicit_snapshot=False, rebuild_history=False,
            ), [snapshots[2]])

    def test_same_size_changed_input_is_rejected_as_unprocessed_history(self):
        with TemporaryDirectory(prefix="ojs-retained-sql-") as directory:
            raw, clean, snapshots = fixture(Path(directory))
            source = snapshots[0].path
            source.write_bytes(source.read_bytes().replace(b"example", b"altered"))
            self.assertFalse(run_pipeline.retained_snapshot_matches_release(snapshots[0], clean))
            with self.assertRaisesRegex(run_pipeline.PipelineError, "arrived behind"):
                run_pipeline.snapshots_to_process(
                    target=snapshots[2], raw_dir=raw, clean_dir=clean,
                    explicit_snapshot=False, rebuild_history=False,
                )

    def test_symlink_and_hardlink_sources_are_rejected(self):
        for link in ("symbolic", "hard"):
            with self.subTest(link=link), TemporaryDirectory(prefix="ojs-retained-sql-") as directory:
                root = Path(directory)
                _, clean, snapshots = fixture(root)
                source = snapshots[0].path
                other = root / "other.sql"
                if link == "symbolic":
                    source.rename(other)
                    source.symlink_to(other)
                else:
                    os.link(source, other)
                self.assertFalse(run_pipeline.retained_snapshot_matches_release(snapshots[0], clean))

    def test_missing_commit_metadata_rejects_without_hashing_sql(self):
        with TemporaryDirectory(prefix="ojs-retained-sql-") as directory:
            _, clean, snapshots = fixture(Path(directory))
            run_pipeline.release_manifest_path(clean, snapshots[0].version).unlink()
            with mock.patch.object(run_pipeline, "regular_file_sha256") as digest:
                self.assertFalse(run_pipeline.retained_snapshot_matches_release(snapshots[0], clean))
            digest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
