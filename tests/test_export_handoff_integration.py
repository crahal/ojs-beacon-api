"""Synthetic crash/restart coverage for retiring a validated predecessor export.

Only isolated fixture databases and temporary backup archives are used. No live
database, real dump, cloud resource, or private operator receipt is accessed.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import export_handoff
import run_pipeline
import test_deferred_payload_integration as deferred
import test_dense_storage_integration as dense


def copy_serving_baseline(reference: Path, candidate: Path):
    deferred.copy_baseline(reference, candidate)
    directory_name = f"mysql-{deferred.DATES[0]}"
    shutil.copytree(reference / directory_name, candidate / directory_name)
    (candidate / "mysql-live").symlink_to(directory_name)
    return candidate / directory_name


def backup_receipt(root: Path, clean: Path, version=deferred.DATES[0]):
    source = run_pipeline.clean_export_path(clean, version)
    backup = root / f"{clean.name}-{version}-offhost-backup.sql.gz"
    shutil.copy2(source, backup)
    checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert checksum == run_pipeline._declared_checksum(source)
    receipt = root / f"{clean.name}-{version}-backup-receipt.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "snapshot_date": version,
        "export_sha256": checksum, "export_size_bytes": backup.stat().st_size,
        "verified_elsewhere": True, "verified_at": "2026-11-16T00:00:00+00:00",
        "backup_location": str(backup),
    }), encoding="utf-8")
    receipt.chmod(0o600)
    return receipt, backup


def build(snapshot, candidate: Path, receipt: Path, **options):
    with mock.patch.dict(os.environ, {
        "OJS_RETIRE_BASELINE_EXPORT": "1",
        "OJS_BASELINE_EXPORT_BACKUP_RECEIPT": str(receipt),
        "OJS_BUILD_EXPORT_GB": "0.01", "OJS_DISK_RESERVE_GB": "0",
    }):
        return deferred.build(snapshot, candidate, True, event_compression=True, **options)


@unittest.skipUnless(dense.MYSQL_AVAILABLE, "MySQL 8.4 server and client tools required")
class ExportHandoffIntegrationTests(unittest.TestCase):
    def test_three_healthy_snapshots_match_every_logical_row_and_report(self):
        with TemporaryDirectory(prefix="ojs-handoff-parity-") as directory, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": "",
                                             "OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            snapshots = [deferred.make_snapshot(root, index) for index in range(3)]
            reference = root / "reference"
            helper = dense.DenseStorageIntegrationTests()
            expected = []
            for index, snapshot in enumerate(snapshots):
                datadir, counts = deferred.build(snapshot, reference, False)
                expected.append((helper.catalogue(datadir, 8, index), counts,
                                 helper.report(reference, snapshot)))

            candidate = root / "candidate"
            live = copy_serving_baseline(reference, candidate)
            for index in (1, 2):
                with self.subTest(snapshot=snapshots[index].version):
                    receipt, backup = backup_receipt(root, candidate, snapshots[index - 1].version)
                    live_before = deferred.file_metadata(live)
                    datadir, counts = build(snapshots[index], candidate, receipt)
                    self.assertEqual((candidate / "mysql-live").resolve(), live)
                    self.assertEqual(deferred.file_metadata(live), live_before)
                    rows, expected_counts, report = expected[index]
                    self.assertEqual(counts, expected_counts)
                    self.assertEqual(helper.catalogue(datadir, 8, index), rows)
                    self.assertEqual(helper.report(candidate, snapshots[index]), report)
                    self.assertTrue(backup.is_file())
                    self.assertFalse(run_pipeline.clean_export_path(
                        candidate, snapshots[index - 1].version).exists())
                    export_handoff.validate_retired_baseline(
                        candidate, snapshots[index - 1].version, snapshots[index].version)
                    # Simulate the ordinary promotion between independent builds.
                    (candidate / "mysql-live").unlink()
                    (candidate / "mysql-live").symlink_to(datadir.name)
                    live = datadir

    def test_export_ready_crashes_resume_without_replaying_wrangling(self):
        self.assert_crashes_resume(("before-retirement", "after-retirement", "during-export"))

    def test_capacity_and_publication_crashes_resume_without_replaying_wrangling(self):
        self.assert_crashes_resume(("insufficient-capacity", "during-publication"))

    def assert_crashes_resume(self, faults):
        with TemporaryDirectory(prefix="ojs-handoff-crash-") as directory, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": "",
                                             "OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            snapshots = [deferred.make_snapshot(root, index) for index in (0, 1)]
            reference = root / "reference"
            deferred.build(snapshots[0], reference, False)
            expected_directory, expected_counts = deferred.build(snapshots[1], reference, False)
            helper = dense.DenseStorageIntegrationTests()
            expected_rows = helper.catalogue(expected_directory, 8, 1)
            expected_report = helper.report(reference, snapshots[1])

            for fault in faults:
                with self.subTest(fault=fault):
                    candidate = root / fault
                    live = copy_serving_baseline(reference, candidate)
                    receipt, backup = backup_receipt(root, candidate)
                    live_before = deferred.file_metadata(live)
                    baseline = run_pipeline.clean_export_path(candidate, deferred.DATES[0])
                    baseline_report = run_pipeline.change_report_path(candidate, deferred.DATES[0])
                    baseline_manifest = run_pipeline.release_manifest_path(candidate, deferred.DATES[0])
                    preserved = {path: path.read_bytes() for path in (
                        baseline_report, baseline_manifest,
                        run_pipeline.checksum_sidecar_path(baseline),
                        run_pipeline.checksum_sidecar_path(baseline_report),
                    )}
                    original_retire = run_pipeline.retire_baseline_export

                    def fail_retirement(*args, **kwargs):
                        if fault == "after-retirement":
                            original_retire(*args, **kwargs)
                        raise run_pipeline.PipelineError("synthetic retirement interruption")

                    def fail_export(server, target, **kwargs):
                        target.write_bytes(b"synthetic interrupted gzip stream")
                        raise run_pipeline.PipelineError("synthetic export interruption")

                    if fault == "during-export":
                        patcher = mock.patch.object(run_pipeline.MySQLServer, "export_clean_tables", new=fail_export)
                    elif fault == "during-publication":
                        patcher = mock.patch.object(run_pipeline, "publish_release_manifest", side_effect=
                                                   run_pipeline.PipelineError("synthetic publication interruption"))
                    elif fault == "insufficient-capacity":
                        patcher = mock.patch.object(run_pipeline, "require_export_capacity", side_effect=
                                                   export_handoff.ExportHandoffError("synthetic capacity interruption"))
                    else:
                        patcher = mock.patch.object(run_pipeline, "retire_baseline_export", new=fail_retirement)
                    with patcher, self.assertRaisesRegex(run_pipeline.PipelineError, "synthetic.*interruption"):
                        build(snapshots[1], candidate, receipt)
                    staging = candidate / (f"mysql-{deferred.DATES[1]}" if fault == "during-publication"
                                           else f"mysql-{deferred.DATES[1]}.building")
                    checkpoint = export_handoff.validate_export_ready(staging)
                    self.assertIsNotNone(checkpoint)
                    self.assertEqual(checkpoint["phase"], "export_ready")
                    self.assertEqual(baseline.exists(), fault == "before-retirement")
                    self.assertEqual((candidate / "mysql-live").resolve(), live)
                    self.assertEqual(deferred.file_metadata(live), live_before)
                    self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
                    self.assertTrue(backup.is_file())
                    for artifact in (run_pipeline.clean_export_path, run_pipeline.change_report_path,
                                     run_pipeline.release_manifest_path):
                        self.assertEqual(artifact(candidate, deferred.DATES[1]).exists(),
                                         fault == "during-publication" and
                                         artifact != run_pipeline.release_manifest_path)

                    # An export-ready database is already hydrated and pruned.
                    # Replaying any earlier phase is both wasteful and unsafe.
                    with ExitStack() as stack:
                        forbidden = [stack.enter_context(mock.patch.object(
                            run_pipeline.MySQLServer, method,
                            side_effect=AssertionError(f"export resume replayed {method}"),
                        )) for method in ("initialize", "import_snapshot", "import_clean_export",
                                          "hydrate_baseline_payload", "run_sql_text")]
                        forbidden.append(stack.enter_context(mock.patch.object(
                            run_pipeline, "extract_metadata_in_parallel",
                            side_effect=AssertionError("export resume replayed metadata"),
                        )))
                        if fault == "during-publication":
                            forbidden.append(stack.enter_context(mock.patch.object(
                                run_pipeline.MySQLServer, "export_clean_tables",
                                side_effect=AssertionError("publication resume duplicated the completed export"),
                            )))
                        actual_directory, counts = build(snapshots[1], candidate, receipt, resume_building=True)
                    for method in forbidden:
                        method.assert_not_called()
                    self.assertFalse(baseline.exists())
                    self.assertEqual((candidate / "mysql-live").resolve(), live)
                    self.assertEqual(deferred.file_metadata(live), live_before)
                    self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
                    self.assertEqual(counts, expected_counts)
                    self.assertEqual(helper.report(candidate, snapshots[1]), expected_report)
                    self.assertEqual(helper.catalogue(actual_directory, 8, 1), expected_rows)
                    export_handoff.validate_retired_baseline(candidate, deferred.DATES[0], deferred.DATES[1])


if __name__ == "__main__":
    unittest.main()
