"""No network/databases: export recovery routing and retired-baseline cleanup."""
import json
import os
from pathlib import Path
import sys
from unittest import mock
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import export_handoff as handoff
import test_compact_update as fixtures


class HandoffCoordinatorTests(unittest.TestCase):
    setUp = fixtures.CompactUpdateTests.setUp
    pointer = fixtures.CompactUpdateTests.pointer
    old_and_new = fixtures.CompactUpdateTests.old_and_new
    def ready_fixture(self):
        self.coordinator.env["OJS_BUILD_EXPORT_GB"] = "0.01"
        self.coordinator.env.update({"OJS_RETIRE_BASELINE_EXPORT": "1",
                                     "OJS_DEFER_BASELINE_PAYLOAD": "1",
                                     "OJS_MYSQL_COMPRESSED_PAGE_KB": "8",
                                     "OJS_COMPRESS_EVENT_HISTORY": "0"})
        old = fixtures.release(self.root, "2026-01-01")
        self.pointer("mysql-live", old)
        self.runner.mount = old.datadir
        version = "2026-02-01"
        staged = self.clean / f"mysql-{version}.building"
        staged.mkdir()
        (staged / "ibdata1").write_bytes(b"complete synthetic candidate")
        raw = self.raw / f"pkpbeacon-{version}.sql"
        raw.write_bytes(b"synthetic raw source")
        baseline = handoff.preflight_retirement(self.clean, old.date, env={})
        report = dict(old.report)
        report["pipeline_version"] = "3"
        report["snapshot"] = {"date": version, "source_filename": raw.name,
                              "previous_snapshot_date": old.date,
                              "source_size_bytes": raw.stat().st_size,
                              "source_sha256": "c" * 64, "build_sql_sha256": "b" * 64}
        report["storage"] = {"profile": handoff.COMPACT_STORAGE_PROFILE,
            "rendered_build_sql_sha256": "b" * 64,
            "deferred_baseline_payload": {
            "profile": handoff.DEFERRED_PAYLOAD_PROFILE,
            "baseline_export_sha256": baseline["export_sha256"],
            "baseline_export_filename": baseline["export_filename"],
            "baseline_articles": 2, "hydrated_articles": 1}}
        state = {"pipeline_version": "3", "snapshot_version": version,
                 "source_filename": raw.name, "source_size_bytes": raw.stat().st_size,
                 "source_sha256": "c" * 64, "build_sql_sha256": "b" * 64,
                 "metadata_storage_profile": handoff.METADATA_STORAGE_PROFILE,
                 "baseline_payload_profile": handoff.DEFERRED_PAYLOAD_PROFILE,
                 "baseline_export_sha256": baseline["export_sha256"]}
        ready = handoff.prepare_export_ready(staged, state, report=report,
                                             baseline=baseline, mysql_version="8.4.11",
                                             event_auto_increment=4)
        return old, staged, raw, ready

    def completed_fixture(self, *, keep_checkpoint=True):
        old, staged, raw, state = self.ready_fixture()
        handoff.retire_baseline_export(self.clean, state)
        final = staged.with_name(staged.name.removesuffix(".building"))
        staged.rename(final)
        current = fixtures.release(self.root, state["snapshot_version"])
        report_path = self.clean / f"pkpbeacon-changes-{current.date}.json"
        report_hash = fixtures.artifact(report_path, handoff.canonical_bytes(state["export_handoff"]["report"]))
        manifest_path = self.clean / f"pkpbeacon-release-{current.date}.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["change_report_sha256"] = report_hash
        manifest_path.write_text(json.dumps(manifest))
        marker_path = final / fixtures.update.MARKER
        marker = json.loads(marker_path.read_text())
        marker["source_sha256"] = state["source_sha256"]
        marker_path.write_text(json.dumps(marker))
        if not keep_checkpoint:
            (final / handoff.STATE_NAME).unlink()
        self.pointer("mysql-current", old)
        self.args.publish_only = False
        return old, final, raw, state

    def assert_completed_publication_resumes(self, *, keep_checkpoint):
        old, final, raw, state = self.completed_fixture(keep_checkpoint=keep_checkpoint)
        raw_before = raw.read_bytes()
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                mock.patch.object(self.coordinator, "child", side_effect=AssertionError("no rebuild or download")):
            result = self.coordinator.execute()
        self.assertEqual(result, {"status": "published", "release": state["snapshot_version"]})
        self.assertEqual((self.clean / "mysql-live").resolve(), final)
        self.assertEqual((self.clean / "mysql-current").resolve(), final)
        self.assertEqual(self.coordinator.cold_checks, [state["snapshot_version"]])
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertFalse(old.datadir.exists())

    def test_committed_manifest_resumes_publication_with_checkpoint(self):
        self.assert_completed_publication_resumes(keep_checkpoint=True)

    def test_committed_manifest_resumes_publication_after_checkpoint_removal(self):
        self.assert_completed_publication_resumes(keep_checkpoint=False)

    def test_completed_publication_readiness_needs_no_rebuild_budget_or_source_probe(self):
        _, _, _, state = self.completed_fixture(keep_checkpoint=False)
        self.coordinator.env["MYSQL_ROOT_PASSWORD"] = "synthetic-password"
        self.coordinator.env.pop("OJS_BUILD_WORKSPACE_GB", None)
        self.coordinator.env.pop("OJS_BUILD_EXPORT_GB", None)
        before = fixtures.update.live_directory(self.clean)
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                mock.patch.object(self.coordinator, "scheduler_status", return_value={
                    "service_loaded": True, "manual_start_refused": False,
                    "service_active": False, "timer_enabled": False, "cron_installed": False}), \
                mock.patch.object(fixtures.update.publish_live, "verify_export_checksum",
                                  side_effect=AssertionError("readiness must not hash the large archive")):
            report = self.coordinator.readiness()
        self.assertTrue(report["ready_for_build"], report["build_blockers"])
        self.assertEqual(report["resume_phase"], "publication_ready")
        self.assertEqual(report["source"]["date_hint"], state["snapshot_version"])
        self.assertEqual(fixtures.update.live_directory(self.clean), before)
        self.assertFalse(self.runner.children)

    def test_completed_handoff_refuses_competing_candidates_before_publication(self):
        old, _, _, _ = self.completed_fixture()
        fixtures.release(self.root, "2026-03-01")
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                self.assertRaisesRegex(fixtures.update.UpdateError, "competing"):
            self.coordinator.execute()
        self.assertEqual(fixtures.update.live_directory(self.clean), old.datadir)
        self.assertFalse(self.coordinator.cold_checks)
        self.assertFalse(self.runner.children)

    def test_completed_handoff_refuses_receipt_report_disagreement(self):
        old, _, _, _ = self.completed_fixture(keep_checkpoint=False)
        receipt_path = handoff.retirement_receipt_path(self.clean, old.date)
        receipt = json.loads(receipt_path.read_text())
        receipt["successor"]["report_sha256"] = "e" * 64
        receipt_path.write_text(json.dumps(receipt))
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                self.assertRaisesRegex(fixtures.update.UpdateError, "not bound"):
            self.coordinator.execute()
        self.assertEqual(fixtures.update.live_directory(self.clean), old.datadir)
        self.assertFalse(self.coordinator.cold_checks)

    def test_completed_handoff_still_hashes_export_before_service_changes(self):
        old, final, _, state = self.completed_fixture(keep_checkpoint=False)
        export = self.clean / f"pkpbeacon-clean-{state['snapshot_version']}.sql.gz"
        export.write_bytes(b"x" * export.stat().st_size)
        with self.assertRaisesRegex(fixtures.update.publish_live.PublishError, "checksum mismatch"):
            self.coordinator.execute()
        self.assertEqual(fixtures.update.live_directory(self.clean), old.datadir)
        self.assertTrue(final.exists())
        self.assertFalse(self.coordinator.cold_checks)
        self.assertFalse(self.runner.commands)

    def test_completed_handoff_cold_verification_failure_keeps_live_rollback(self):
        old, final, _, _ = self.completed_fixture()
        with mock.patch.object(self.coordinator, "verify_database",
                               side_effect=fixtures.update.UpdateError("synthetic cold check failure")), \
                self.assertRaisesRegex(fixtures.update.UpdateError, "cold check failure"):
            self.coordinator.execute()
        self.assertEqual(fixtures.update.live_directory(self.clean), old.datadir)
        self.assertTrue((final / handoff.STATE_NAME).is_file())
        self.assertTrue(old.datadir.exists())
        self.assertFalse(self.runner.commands)

    def test_selected_coordinator_reserve_is_propagated_to_pipeline_environment(self):
        for selected in (0.0, 8.0, 17.5):
            with self.subTest(selected=selected), mock.patch.dict(os.environ, {"OJS_DISK_RESERVE_GB": "99"}):
                self.args.reserve_gb = selected
                coordinator = fixtures.FakeCoordinator(self.args, self.runner)
                self.assertEqual(float(coordinator.env["OJS_DISK_RESERVE_GB"]), selected)
                requirements = handoff.export_capacity_requirements(
                    dict(coordinator.env, OJS_BUILD_EXPORT_GB="1"))
                self.assertEqual(requirements["reserve_bytes"], int(selected * fixtures.update.GIB))

    def test_cleanup_only_constructor_preserves_environment_reserve_without_cli_option(self):
        del self.args.reserve_gb
        with mock.patch.dict(os.environ, {"OJS_DISK_RESERVE_GB": "99"}):
            coordinator = fixtures.FakeCoordinator(self.args, self.runner)
        self.assertEqual(coordinator.env["OJS_DISK_RESERVE_GB"], "99")

    def test_existing_successor_export_removes_only_its_conditional_working_allocation(self):
        _, _, _, state = self.completed_fixture()
        (self.clean / f"pkpbeacon-release-{state['snapshot_version']}.json").unlink()
        scratch = self.root / "scratch"
        scratch.mkdir()
        self.coordinator.env.update({"OJS_MYSQL_TMP_ROOT": str(scratch),
                                     "OJS_BUILD_SCRATCH_GB": "0.001"})
        with mock.patch.object(fixtures.update.shutil, "disk_usage", return_value=mock.Mock(free=4 * 1024**2)), \
                mock.patch.object(handoff, "_file_sha", side_effect=AssertionError("no large gzip read")):
            report = self.coordinator.export_resume_capacity(state["snapshot_version"])
            self.assertTrue(report["approved"], report["blockers"])
            self.assertTrue(report["conditional_successor_export_reuse"])
            group = next(group for group in report["filesystems"] if "data" in group["roles"])
            self.assertEqual(group["conditional_retirement_bytes"], 0)
            self.assertEqual(group["conditional_export_reuse_bytes"], 10737419)
            self.assertEqual(group["working_bytes"], 1073742)
            self.coordinator.env["OJS_BUILD_SCRATCH_GB"] = "0.01"
            self.assertFalse(self.coordinator.export_resume_capacity(state["snapshot_version"])["approved"])

    def test_missing_successor_metadata_retains_the_full_export_budget(self):
        _, _, _, state = self.completed_fixture()
        (self.clean / f"pkpbeacon-release-{state['snapshot_version']}.json").unlink()
        (self.clean / f"pkpbeacon-clean-{state['snapshot_version']}.sql.gz.sha256").unlink()
        with mock.patch.object(fixtures.update.shutil, "disk_usage", return_value=mock.Mock(free=4 * 1024**2)):
            report = self.coordinator.export_resume_capacity(state["snapshot_version"])
        self.assertFalse(report["approved"])
        self.assertFalse(report["conditional_successor_export_reuse"])
        self.assertGreater(report["filesystems"][0]["working_bytes"], 4 * 1024**2)

    def test_conditional_reuse_still_requires_runner_hash_and_consistent_manifest(self):
        _, _, _, state = self.completed_fixture()
        export = self.clean / f"pkpbeacon-clean-{state['snapshot_version']}.sql.gz"
        export.write_bytes(b"x" * export.stat().st_size)
        with mock.patch.object(handoff, "_file_sha", side_effect=AssertionError("no gzip read in metadata check")):
            self.assertIsNotNone(handoff.successor_export_reuse_metadata(self.clean, state))
        with self.assertRaisesRegex(handoff.ExportHandoffError, "does not match"):
            handoff.reusable_successor_export(self.clean, state)
        manifest_path = self.clean / f"pkpbeacon-release-{state['snapshot_version']}.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["clean_export_sha256"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(handoff.ExportHandoffError, "manifest"):
            self.coordinator.export_resume_capacity(state["snapshot_version"])

    def test_incompatible_recovery_flags_block_readiness_and_child_without_upstream_probe(self):
        _, _, _, state = self.ready_fixture()
        self.coordinator.env["MYSQL_ROOT_PASSWORD"] = "synthetic-password"
        valid = dict(self.coordinator.env)
        cases = (("OJS_RETIRE_BASELINE_EXPORT", "0", "export_retirement_not_enabled"),
                 ("OJS_DEFER_BASELINE_PAYLOAD", "0", "export_deferred_payload_not_enabled"),
                 ("OJS_MYSQL_COMPRESSED_PAGE_KB", "4", "export_storage_profile_mismatch"),
                 ("OJS_COMPRESS_EVENT_HISTORY", "1", "export_storage_profile_mismatch"),
                 ("OJS_RETIRE_BASELINE_EXPORT", "yes", "export_recovery_configuration_invalid"),
                 ("OJS_MYSQL_COMPRESSED_PAGE_KB", "2", "export_recovery_configuration_invalid"))
        for name, value, blocker in cases:
            with self.subTest(setting=name, value=value):
                self.coordinator.env = dict(valid, **{name: value})
                budget = self.coordinator.export_resume_capacity(state["snapshot_version"])
                self.assertFalse(budget["approved"])
                self.assertIn(blocker, budget["blockers"])
                with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                        mock.patch.object(self.coordinator, "child") as child, \
                        self.assertRaisesRegex(fixtures.update.UpdateError, "not admitted"):
                    self.coordinator.build_or_resume()
                child.assert_not_called()
                with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no upstream probe")), \
                        mock.patch.object(self.coordinator, "scheduler_status", return_value={
                            "service_loaded": True, "manual_start_refused": False,
                            "service_active": False, "timer_enabled": False, "cron_installed": False}):
                    readiness = self.coordinator.readiness()
                self.assertFalse(readiness["ready_for_build"])
                self.assertIn(blocker, readiness["build_blockers"])

    def test_export_recovery_finds_renamed_candidate_before_manifest_publication(self):
        _, staged, raw, state = self.ready_fixture()
        final = staged.with_name(staged.name.removesuffix(".building"))
        staged.rename(final)
        handoff.retire_baseline_export(self.clean, state)
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no HEAD")), \
                mock.patch.object(self.coordinator, "child") as child:
            self.coordinator.incomplete_builds()
            self.assertEqual(self.coordinator.export_resume(), state["snapshot_version"])
            self.coordinator.build_or_resume()
        self.assertIn(str(raw), child.call_args.args[0])
        self.assertEqual(child.call_args.kwargs["stage"], "update-export-resume")

    def test_export_recovery_does_not_probe_or_download_a_newer_snapshot(self):
        old, staged, raw, state = self.ready_fixture()
        handoff.retire_baseline_export(self.clean, state)
        self.assertFalse(old.export.exists())
        with mock.patch.object(self.coordinator, "probe", side_effect=AssertionError("no HEAD")), \
                mock.patch.object(self.coordinator, "require_build_capacity", side_effect=AssertionError("not a full build")), \
                mock.patch.object(self.coordinator, "child") as child:
            self.coordinator.incomplete_builds()
            self.assertIsNone(self.coordinator.build_or_resume())
        command = child.call_args.args[0]
        self.assertIn("--snapshot", command)
        self.assertIn(str(raw), command)
        self.assertEqual(child.call_args.kwargs["stage"], "update-export-resume")
        self.assertTrue(old.datadir.is_dir())

    def test_missing_source_blocks_export_recovery_without_download(self):
        _, _, raw, _ = self.ready_fixture()
        raw.unlink()
        with mock.patch.object(self.coordinator, "probe") as probe, \
                self.assertRaisesRegex(fixtures.update.UpdateError, "retained source"):
            self.coordinator.build_or_resume()
        probe.assert_not_called()
        self.assertFalse(self.runner.children)

    def test_missing_export_budget_blocks_recovery_before_start(self):
        self.ready_fixture()
        self.coordinator.env.pop("OJS_BUILD_EXPORT_GB")
        with self.assertRaisesRegex(fixtures.update.UpdateError, "budgets"):
            self.coordinator.build_or_resume()
        self.assertFalse(self.runner.children)

    def test_export_budget_shows_conditional_credit_without_deleting(self):
        old, _, _, state = self.ready_fixture()
        report = self.coordinator.export_resume_capacity(state["snapshot_version"])
        self.assertTrue(report["approved"])
        data = report["filesystems"][0]
        self.assertEqual(data["conditional_retirement_bytes"], old.export.stat().st_size)
        self.assertTrue(old.export.exists())
        handoff.retire_baseline_export(self.clean, state)
        report = self.coordinator.export_resume_capacity(state["snapshot_version"])
        self.assertEqual(report["filesystems"][0]["conditional_retirement_bytes"], 0)

    def test_changed_live_pointer_blocks_export_recovery(self):
        old, _, _, _ = self.ready_fixture()
        other = fixtures.release(self.root, "2025-12-01")
        self.pointer("mysql-live", other)
        with self.assertRaises(ValueError):
            self.coordinator.incomplete_builds()
        self.assertTrue(old.export.exists())
        self.assertFalse(self.runner.children)

    def test_unreceipted_missing_old_export_is_never_eligible_for_cleanup(self):
        old, new = self.old_and_new()
        old.export.unlink()
        self.assertEqual(self.coordinator.cleanup_plan(new), [])
        self.assertTrue(old.datadir.exists())

    def test_retirement_cannot_weaken_normal_candidate_publication_checks(self):
        old, _, _, state = self.ready_fixture()
        handoff.retire_baseline_export(self.clean, state)
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            fixtures.update.validate_release(self.clean, old.date)
        self.assertTrue(old.datadir.exists())

    def test_retirement_receipt_must_match_the_actual_published_successor(self):
        old, _, _, state = self.ready_fixture()
        handoff.retire_baseline_export(self.clean, state)
        different = fixtures.release(self.root, "2026-02-01")
        self.assertEqual(self.coordinator.cleanup_plan(different), [])
        self.assertTrue(old.datadir.exists())

    def test_receipted_old_database_is_cleaned_only_after_matching_publication(self):
        old, _, _, state = self.ready_fixture()
        handoff.retire_baseline_export(self.clean, state)
        current = fixtures.release(self.root, "2026-02-01")
        # Bind the synthetic published report exactly to the export checkpoint.
        report = state["export_handoff"]["report"]
        current = fixtures.update.Release(current.date, current.datadir, current.export,
                                         state["source_sha256"], state["build_sql_sha256"], report)
        self.runner.mount = current.datadir
        self.pointer("mysql-live", current)
        plan = self.coordinator.cleanup_plan(current)
        self.assertEqual({item["name"] for item in plan},
                         {old.datadir.name, old.export.name + ".sha256"})
        # Planning never deletes; mounted predecessors are always retained.
        self.assertTrue(old.datadir.exists())
        self.runner.other_mounts = [old.datadir]
        self.assertEqual(self.coordinator.cleanup_plan(current), [])


if __name__ == "__main__":
    unittest.main()
