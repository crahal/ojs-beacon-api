"""Small, synthetic checks for exact-file export retirement and durable resume."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import export_handoff as handoff
import run_pipeline


OLD = "2026-01-01"
NEW = "2026-07-01"
SOURCE_SHA = "a" * 64
SQL_SHA = "b" * 64
PAYLOAD_PROFILE = run_pipeline.DEFERRED_PAYLOAD_PROFILE


def write_document(path, value):
    path.write_bytes(handoff.canonical_bytes(value))


def checksum(path):
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(f"{value}  {path.name}\n")
    return value


def fixture(root):
    clean = root / "clean"
    clean.mkdir()
    export = clean / f"pkpbeacon-clean-{OLD}.sql.gz"
    export.write_bytes(b"small synthetic predecessor archive\n")
    export_sha = checksum(export)
    report_path = clean / f"pkpbeacon-changes-{OLD}.json"
    report = {"snapshot": {"date": OLD, "source_sha256": SOURCE_SHA,
                           "build_sql_sha256": SQL_SHA},
              "release_guard": {"status": "passed"}}
    write_document(report_path, report)
    report_sha = checksum(report_path)
    write_document(clean / f"pkpbeacon-release-{OLD}.json", {
        "schema_version": 1, "snapshot_date": OLD,
        "clean_export_filename": export.name, "clean_export_sha256": export_sha,
        "change_report_filename": report_path.name, "change_report_sha256": report_sha,
    })
    rollback = clean / f"mysql-{OLD}"
    rollback.mkdir()
    (rollback / "must-survive.ibd").write_bytes(b"synthetic rollback database")
    write_document(rollback / "OJS_COMPACT_RELEASE.json", {
        "schema_version": 1, "snapshot_date": OLD, "clean_only": True,
        "data_directory_name": rollback.name,
        "source_sha256": SOURCE_SHA, "build_sql_sha256": SQL_SHA,
    })
    (clean / "mysql-live").symlink_to(rollback.name)
    receipt = root / "private-backup-receipt.json"
    write_document(receipt, {
        "schema_version": 1, "snapshot_date": OLD,
        "export_sha256": export_sha, "export_size_bytes": export.stat().st_size,
        "verified_elsewhere": True, "verified_at": "2026-11-16T00:00:00+00:00",
        "backup_location": "synthetic://separate-backup",
    })
    receipt.chmod(0o600)
    env = {"OJS_BASELINE_EXPORT_BACKUP_RECEIPT": str(receipt)}
    baseline = handoff.preflight_retirement(clean, OLD, env=env)
    staging = clean / f"mysql-{NEW}.building"
    staging.mkdir()
    (staging / "must-survive.ibd").write_bytes(b"complete hydrated candidate")
    state = {"schema_version": 1, "pipeline_version": run_pipeline.PIPELINE_VERSION,
             "phase": "finalizing", "snapshot_version": NEW,
             "source_filename": f"pkpbeacon-{NEW}.sql", "source_size_bytes": 42,
             "source_sha256": SOURCE_SHA, "build_sql_sha256": SQL_SHA,
             "metadata_storage_profile": run_pipeline.METADATA_STORAGE_PROFILE,
             "storage_layout_profile": None,
             "event_history_profile": "innodb-events-compressed-8k-v1",
             "baseline_payload_profile": PAYLOAD_PROFILE,
             "baseline_export_sha256": export_sha}
    report = {"schema_version": 1, "pipeline_version": run_pipeline.PIPELINE_VERSION,
        "snapshot": {"date": NEW, "previous_snapshot_date": OLD,
        **{key: state[key] for key in (
        "source_filename", "source_size_bytes", "source_sha256", "build_sql_sha256")}},
        "release_guard": {"status": "passed"},
        "source_rows": {"retained_total": 3, "active": 2},
        "articles": {"retained_total": 3, "active": 2,
                     "removed_tombstones": 1, "merged_tombstones": 0},
        "article_events": {"total": 1},
        "storage": {"profile": run_pipeline.COMPACT_STORAGE_PROFILE,
                    "rendered_build_sql_sha256": SQL_SHA,
                    "storage_layout_profile": state["storage_layout_profile"],
                    "event_history_profile": state["event_history_profile"],
                    "deferred_baseline_payload": {
                        "profile": PAYLOAD_PROFILE, "baseline_export_sha256": export_sha,
                        "baseline_export_filename": export.name,
                        "baseline_articles": 3, "hydrated_articles": 2}}}
    state = handoff.prepare_export_ready(staging, state, report=report,
                                         baseline=baseline, mysql_version="8.4.11",
                                         event_auto_increment=2)
    return clean, export, staging, state, receipt, env


def contents(root):
    """Only used for the tiny fixture tree, never a real database or archive."""
    return {str(path.relative_to(root)): (
        path.lstat().st_ino,
        os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
    ) for path in root.rglob("*")}


class ExportHandoffTests(unittest.TestCase):
    def test_retirement_opt_in_is_explicit(self):
        self.assertFalse(handoff.retirement_enabled({}))
        self.assertFalse(handoff.retirement_enabled({"OJS_RETIRE_BASELINE_EXPORT": "0"}))
        self.assertTrue(handoff.retirement_enabled({"OJS_RETIRE_BASELINE_EXPORT": "1"}))
        for value in ("true", "yes", "", "2"):
            with self.subTest(value=value), self.assertRaises(handoff.ExportHandoffError):
                handoff.retirement_enabled({"OJS_RETIRE_BASELINE_EXPORT": value})

    def test_preflight_does_not_hash_large_archive(self):
        with TemporaryDirectory(prefix="ojs-handoff-small-") as directory:
            clean, export, _, state, _, env = fixture(Path(directory))
            original = handoff._file_sha

            def small_files_only(path):
                self.assertNotEqual(path, export, "admission must not re-read a large archive")
                return original(path)

            with mock.patch.object(handoff, "_file_sha", side_effect=small_files_only):
                self.assertEqual(handoff.preflight_retirement(clean, OLD, env=env),
                                 state["export_handoff"]["baseline"])

    def test_retirement_deletes_only_archive_and_is_idempotent(self):
        with TemporaryDirectory(prefix="ojs-handoff-exact-file-") as directory:
            root = Path(directory)
            clean, export, staging, state, _, _ = fixture(root)
            before = contents(root)
            with mock.patch.object(handoff, "_file_sha", wraps=handoff._file_sha) as digest:
                receipt = handoff.retire_baseline_export(clean, state)
                self.assertEqual(sum(call.args[0] == export for call in digest.call_args_list), 1)
            self.assertFalse(export.exists())
            self.assertEqual(handoff.validate_export_ready(staging), state)
            self.assertEqual(handoff.validate_resume_handoff(clean, NEW, OLD), state)
            self.assertEqual(handoff.validate_retired_baseline(clean, OLD, NEW), receipt)
            with mock.patch.object(handoff, "_file_sha", wraps=handoff._file_sha) as digest:
                self.assertEqual(handoff.retire_baseline_export(clean, state), receipt)
                self.assertFalse(any(call.args[0] == export for call in digest.call_args_list))
            after = contents(root)
            removed = {str(export.relative_to(root))}
            for name, value in before.items():
                if name not in removed:
                    self.assertEqual(after[name], value, name)
            self.assertEqual(set(before) - set(after), removed)
            self.assertEqual(set(after) - set(before), {
                str(handoff.retirement_receipt_path(clean, OLD).relative_to(root))})

    def test_changed_archive_fails_checksum_before_deletion(self):
        with TemporaryDirectory(prefix="ojs-handoff-corrupt-archive-") as directory:
            root = Path(directory)
            clean, export, _, state, _, _ = fixture(root)
            export.write_bytes(b"x" * export.stat().st_size)
            before = contents(root)
            with self.assertRaisesRegex(handoff.ExportHandoffError, "changed baseline export"):
                handoff.retire_baseline_export(clean, state)
            self.assertEqual(contents(root), before)

    def test_invalid_backup_receipts_fail_before_any_mutation(self):
        for fault in ("checksum", "size", "snapshot", "not-verified", "public", "symlink"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="ojs-handoff-receipt-") as directory:
                root = Path(directory)
                clean, _, _, _, receipt, env = fixture(root)
                value = json.loads(receipt.read_text())
                if fault == "public":
                    receipt.chmod(0o644)
                elif fault == "symlink":
                    alternate = root / "receipt-target.json"
                    receipt.rename(alternate)
                    receipt.symlink_to(alternate)
                else:
                    key, replacement = {
                        "checksum": ("export_sha256", "c" * 64),
                        "size": ("export_size_bytes", 1),
                        "snapshot": ("snapshot_date", NEW),
                        "not-verified": ("verified_elsewhere", False),
                    }[fault]
                    value[key] = replacement
                    write_document(receipt, value)
                before = contents(root)
                with self.assertRaises(handoff.ExportHandoffError):
                    handoff.preflight_retirement(clean, OLD, env=env)
                self.assertEqual(contents(root), before)

    def test_invalid_checkpoint_bindings_fail_before_deletion(self):
        for fault in ("source", "sql", "layout", "event-layout", "report-checksum", "archive-checksum"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="ojs-handoff-binding-") as directory:
                root = Path(directory)
                clean, export, staging, state, _, _ = fixture(root)
                invalid = copy.deepcopy(state)
                if fault == "archive-checksum":
                    export.with_name(export.name + ".sha256").write_text("malformed checksum\n")
                elif fault == "report-checksum":
                    invalid["export_handoff"]["report_sha256"] = "c" * 64
                else:
                    key = {"source": "source_sha256", "sql": "build_sql_sha256",
                           "layout": "storage_layout_profile",
                           "event-layout": "event_history_profile"}[fault]
                    invalid[key] = "c" * 64 if fault in ("source", "sql") else "changed-layout"
                write_document(staging / handoff.STATE_NAME, invalid)
                before = contents(root)
                with self.assertRaises(handoff.ExportHandoffError):
                    handoff.retire_baseline_export(clean, invalid)
                self.assertEqual(contents(root), before)

    def test_changed_live_target_and_missing_rollback_fail_before_deletion(self):
        for fault in ("live", "rollback-marker", "rollback-symlink"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="ojs-handoff-rollback-") as directory:
                root = Path(directory)
                clean, _, _, state, _, _ = fixture(root)
                rollback = clean / f"mysql-{OLD}"
                if fault == "live":
                    live = clean / "mysql-live"
                    live.unlink()
                    live.symlink_to(f"mysql-{NEW}.building")
                elif fault == "rollback-marker":
                    marker = rollback / "OJS_COMPACT_RELEASE.json"
                    value = json.loads(marker.read_text())
                    value["source_sha256"] = "c" * 64
                    write_document(marker, value)
                else:
                    alternate = clean / "unowned-rollback"
                    rollback.rename(alternate)
                    rollback.symlink_to(alternate.name)
                before = contents(root)
                with self.assertRaises(handoff.ExportHandoffError):
                    handoff.retire_baseline_export(clean, state)
                self.assertEqual(contents(root), before)

    def test_missing_archive_requires_matching_durable_receipt(self):
        with TemporaryDirectory(prefix="ojs-handoff-missing-receipt-") as directory:
            root = Path(directory)
            clean, export, _, _, _, _ = fixture(root)
            export.unlink()
            before = contents(root)
            with self.assertRaises(FileNotFoundError):
                handoff.validate_resume_handoff(clean, NEW, OLD)
            self.assertEqual(contents(root), before)

    def test_retirement_receipt_rejects_changed_successor(self):
        for key in ("snapshot_date", "source_sha256", "build_sql_sha256", "report_sha256"):
            with self.subTest(key=key), TemporaryDirectory(prefix="ojs-handoff-successor-") as directory:
                root = Path(directory)
                clean, _, _, state, _, _ = fixture(root)
                handoff.retire_baseline_export(clean, state)
                receipt_path = handoff.retirement_receipt_path(clean, OLD)
                receipt = json.loads(receipt_path.read_text())
                receipt["successor"][key] = "2026-11-01" if key == "snapshot_date" else "c" * 64
                write_document(receipt_path, receipt)
                before = contents(root)
                with self.assertRaises(handoff.ExportHandoffError):
                    handoff.validate_resume_handoff(clean, NEW, OLD)
                self.assertEqual(contents(root), before)

    def test_unsafe_state_files_fail_without_mutation(self):
        for fault in ("symlink", "hardlink", "oversize", "missing-report"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="ojs-handoff-file-") as directory:
                root = Path(directory)
                clean, _, staging, state, _, _ = fixture(root)
                path = staging / handoff.STATE_NAME
                if fault == "symlink":
                    target = root / "state-target.json"
                    path.rename(target)
                    path.symlink_to(target)
                elif fault == "hardlink":
                    os.link(path, root / "second-state-link.json")
                elif fault == "oversize":
                    path.write_bytes(b" " * (1024 * 1024 + 1))
                else:
                    del state["export_handoff"]["report"]
                    write_document(path, state)
                before = contents(root)
                with self.assertRaises((handoff.ExportHandoffError, OSError)):
                    handoff.retire_baseline_export(clean, state)
                self.assertEqual(contents(root), before)

    def test_archive_mount_rejection_precedes_hash_receipt_and_deletion(self):
        with TemporaryDirectory(prefix="ojs-handoff-mounted-") as directory:
            root = Path(directory)
            clean, export, _, state, _, _ = fixture(root)
            before = contents(root)
            original = handoff._file_sha

            def no_archive_hash(path):
                self.assertNotEqual(path, export)
                return original(path)

            with mock.patch.object(handoff, "assert_cleanup_unmounted",
                                   side_effect=ValueError("synthetic mount rejection")) as mounted, \
                    mock.patch.object(handoff, "_file_sha", side_effect=no_archive_hash), \
                    self.assertRaisesRegex(ValueError, "mount rejection"):
                handoff.retire_baseline_export(clean, state)
            mounted.assert_called_once_with(export)
            self.assertEqual(contents(root), before)

    def test_capacity_failure_retains_ready_candidate_and_rollback(self):
        with TemporaryDirectory(prefix="ojs-handoff-capacity-") as directory:
            root = Path(directory)
            clean, _, staging, state, _, _ = fixture(root)
            before = contents(root)
            with mock.patch.object(handoff.shutil, "disk_usage", return_value=mock.Mock(free=0)), \
                    self.assertRaisesRegex(handoff.ExportHandoffError, "candidate retained"):
                handoff.require_export_capacity(clean, {"OJS_BUILD_EXPORT_GB": "1",
                                                       "OJS_DISK_RESERVE_GB": "0"})
            self.assertEqual(handoff.validate_export_ready(staging), state)
            self.assertEqual(contents(root), before)


class ExportHandoffPipelineAdmissionTests(unittest.TestCase):
    def candidate(self, root):
        clean, export, staging, state, receipt, _ = fixture(root)
        source = root / state["source_filename"]
        source.write_bytes(b"synthetic raw snapshot\n")
        build_sql = Path(__file__).parents[1] / "sql" / "01_build_ojs_tables.sql"
        state.update(source_size_bytes=source.stat().st_size,
                     source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                     build_sql_sha256=run_pipeline.sha256_small_file(build_sql))
        report = state["export_handoff"]["report"]
        for key in ("source_size_bytes", "source_sha256", "build_sql_sha256"):
            report["snapshot"][key] = state[key]
        state["export_handoff"]["report_sha256"] = handoff.document_sha256(report)
        write_document(staging / handoff.STATE_NAME, state)
        self.assertEqual(handoff.validate_export_ready(staging), state)
        snapshot = run_pipeline.Snapshot(source, NEW, datetime(2026, 7, 1), source.stat().st_size)
        options = dict(
            snapshot=snapshot, clean_dir=clean, build_sql=build_sql,
            database="synthetic_handoff", root_password="synthetic-password",
            api_db_user="fixture_api", api_db_password="synthetic-api-password",
            release_thresholds=run_pipeline.ReleaseThresholds(1, 1, 1, 1),
            allow_anomalous_release=False, buffer_pool_size="128M", progress_seconds=60,
            force_rebuild=False, verify_existing=False, verify_source_checksum=False,
            full_rescan=False, publish_current=False, metadata_workers=1,
            resume_building=True, compact_storage=True,
        )
        env = {"OJS_RETIRE_BASELINE_EXPORT": "1", "OJS_DEFER_BASELINE_PAYLOAD": "1",
               "OJS_COMPRESS_EVENT_HISTORY": "1", "OJS_MYSQL_COMPRESSED_PAGE_KB": "8",
               "OJS_BASELINE_EXPORT_BACKUP_RECEIPT": str(receipt),
               "OJS_BUILD_EXPORT_GB": "0.01", "OJS_DISK_RESERVE_GB": "0"}
        return clean, export, staging, state, options, env

    def test_force_and_changed_source_layout_fail_before_cleanup_or_database_start(self):
        for fault in ("force", "not-resuming", "not-opted-in", "source-size", "source-name",
                      "sql", "layout", "event-layout"):
            with self.subTest(fault=fault), TemporaryDirectory(prefix="ojs-handoff-admission-") as directory:
                root = Path(directory)
                _, _, _, _, options, env = self.candidate(root)
                if fault == "force":
                    options["force_rebuild"] = True
                elif fault == "not-resuming":
                    options["resume_building"] = False
                elif fault == "not-opted-in":
                    env["OJS_RETIRE_BASELINE_EXPORT"] = "0"
                elif fault in ("source-name", "source-size"):
                    snapshot = options["snapshot"]
                    options["snapshot"] = run_pipeline.Snapshot(
                        snapshot.path.with_name("changed.sql") if fault == "source-name" else snapshot.path,
                        snapshot.version, snapshot.completed_at,
                        snapshot.size + int(fault == "source-size"),
                    )
                elif fault == "sql":
                    different_sql = root / "different-build.sql"
                    different_sql.write_text("SELECT 1;\n")
                    options["build_sql"] = different_sql
                elif fault == "layout":
                    env["OJS_MYSQL_COMPRESSED_PAGE_KB"] = "4"
                else:
                    env["OJS_COMPRESS_EVENT_HISTORY"] = "0"
                before = contents(root)
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(run_pipeline.MySQLTools, "discover",
                                          return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        mock.patch.object(run_pipeline.shutil, "rmtree") as remove, \
                        self.assertRaisesRegex(run_pipeline.PipelineError, "export-ready"):
                    run_pipeline.build_snapshot_database(**options)
                server.assert_not_called()
                remove.assert_not_called()
                self.assertEqual(contents(root), before)


if __name__ == "__main__":
    unittest.main()
