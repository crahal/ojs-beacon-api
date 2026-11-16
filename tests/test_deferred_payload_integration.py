"""Lossless temporal parity and publication safety for deferred baseline data.

All databases and dump files are synthetic and live in temporary directories.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
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
import deferred_payload
import run_pipeline
import test_dense_storage_integration as dense
import test_pipeline as fixture
from test_compact_storage_equivalence import snapshot_rows
from test_metadata_storage_integration import large_payload


ENV = "OJS_DEFER_BASELINE_PAYLOAD"
DATES = dense.DATES
DATABASE = dense.DATABASE
PASSWORD = dense.PASSWORD


def make_snapshot(root: Path, index: int):
    rows = snapshot_rows(index)
    xml, _ = large_payload(index)
    rows.append(fixture.record_row(
        record_id=5000, context_id=1, identifier=9000, metadata_xml=xml,
        modified="2026-01-01 00:00:00",
    ))
    for record_id in (7000, 7001, 7002, 7003, 7004, 7100, 7101):
        if record_id == 7002 and index:
            continue  # Absent aliases retain their old canonical payload.
        entity = 7100 if record_id in (7100, 7101) else record_id
        title = f"Stable payload {entity} – café 界"
        if record_id == 7003:
            title += " ' quoted \\ slash &amp; XML\nINSERT INTO `ojs_articles` VALUES ('fake');"
        xml = fixture.metadata(
            oai_identifier=f"oai:deferred:{entity}", title=title,
            creator="O'Brien \\ Researcher", published="2025-04-15",
            url=f"https://one.example/deferred/{entity}", doi=f"10.1234/deferred-{entity}",
        )
        if record_id == 7004 and index:
            # Legitimately cleared payload must not regain stale baseline text.
            xml = xml.replace(f"<dc:title>{title}</dc:title>", "")
            xml = xml.replace("<dc:creator>O'Brien \\ Researcher</dc:creator>", "")
        removed = record_id == 7001 and index or record_id == 7100 and index == 1
        rows.append(fixture.record_row(
            record_id=record_id, context_id=1 if record_id != 7101 else 2,
            identifier=record_id + 10000, metadata_xml=xml,
            # Identical XML on 7100/7101 exercises a canonical switch with an
            # unchanged canonical metadata hash (and unchanged publisher time).
            modified="2026-01-01 00:00:00",
            removed_at=DATES[1] + " 00:00:00" if removed else None,
        ))
    content = fixture.dump(DATES[index], rows).replace(
        "INSERT INTO `records` VALUES",
        "INSERT INTO `endpoints` VALUES (3,'ojs','https://excluded.example/oai');\n"
        "INSERT INTO `contexts` VALUES (3,3,'Journal without an ISSN');\n"
        "INSERT INTO `records` VALUES", 1,
    )
    path = root / f"pkpbeacon-{DATES[index]}.sql"
    path.write_text(content, encoding="utf-8")
    return run_pipeline.inspect_snapshot(path)


def copy_baseline(source: Path, destination: Path):
    destination.mkdir()
    for path in source.iterdir():
        if path.is_file() and DATES[0] in path.name:
            shutil.copy2(path, destination / path.name)


def file_metadata(root: Path):
    return {
        str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_size,
                                    path.stat().st_mtime_ns)
        for path in root.rglob("*")
    }


def build(snapshot, clean: Path, enabled: bool, *, event_compression=False, page_kb=8, **options):
    with mock.patch.dict(os.environ, {ENV: str(int(enabled)),
                                     "OJS_COMPRESS_EVENT_HISTORY": str(int(event_compression))}):
        return dense.build_candidate(snapshot, clean, page_kb, **options)


class DeferredPayloadCheckpointSafetyTests(unittest.TestCase):
    def test_opt_in_requires_compact_mode_and_an_existing_baseline_without_mutation(self):
        for compact, message in ((False, "compact-storage"), (True, "prior clean export")):
            with self.subTest(compact=compact), \
                    TemporaryDirectory(prefix="ojs-deferred-config-") as directory:
                root = Path(directory)
                source = root / "pkpbeacon-2026-01-01.sql"
                source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
                snapshot = run_pipeline.inspect_snapshot(source)
                before = dense.tree_contents(root)
                with mock.patch.object(run_pipeline.MySQLTools, "discover",
                                       return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        self.assertRaisesRegex(run_pipeline.PipelineError, message):
                    build(snapshot, root / "clean", True, compact_storage=compact)
                server.assert_not_called()
                self.assertEqual(dense.tree_contents(root), before)

    def test_profile_and_predecessor_binding_reject_changes_before_force_deletion(self):
        profile = deferred_payload.DEFERRED_PAYLOAD_PROFILE
        cases = ((False, profile, "a" * 64), (True, None, None),
                 (True, profile, "b" * 64))
        for enabled, stored_profile, stored_checksum in cases:
            with self.subTest(enabled=enabled, profile=stored_profile, sha=stored_checksum), \
                    TemporaryDirectory(prefix="ojs-deferred-checkpoint-") as directory:
                root = Path(directory)
                source = root / "pkpbeacon-2026-01-01.sql"
                source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
                snapshot = run_pipeline.inspect_snapshot(source)
                clean = root / "clean"
                staging = clean / f"mysql-{snapshot.version}.building"
                staging.mkdir(parents=True)
                (staging / "keep.ibd").write_bytes(b"synthetic candidate")
                state = {"phase": "metadata_ready"}
                if stored_profile is not None:
                    state.update(baseline_payload_profile=stored_profile,
                                 baseline_export_sha256=stored_checksum)
                run_pipeline.build_state_path(staging).write_text(json.dumps(state))
                for artifact in (run_pipeline.clean_export_path, run_pipeline.change_report_path,
                                 run_pipeline.release_manifest_path):
                    artifact(clean, snapshot.version).write_bytes(b"must not be invalidated")
                before = dense.tree_contents(root)
                previous = run_pipeline.CleanExport(clean / "synthetic-prior.sql.gz", "2025-12-01")
                with mock.patch.object(run_pipeline.MySQLTools, "discover",
                                       return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "previous_clean_export", return_value=previous), \
                        mock.patch.object(run_pipeline, "_declared_checksum", return_value="a" * 64), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        mock.patch.object(run_pipeline.shutil, "rmtree") as remove, \
                        self.assertRaisesRegex(run_pipeline.PipelineError,
                                               "baseline payload profile or export checksum"):
                    build(snapshot, clean, enabled, force_rebuild=True, resume_building=True)
                server.assert_not_called()
                remove.assert_not_called()
                self.assertEqual(dense.tree_contents(root), before)

    def test_metadata_resume_requires_the_bound_profile_and_original_export_checksum(self):
        with TemporaryDirectory(prefix="ojs-deferred-resume-") as directory:
            root = Path(directory)
            source = root / "pkpbeacon-2026-01-01.sql"
            source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
            snapshot = run_pipeline.inspect_snapshot(source)
            staging = root / "candidate"
            staging.mkdir()
            run_pipeline.write_build_state(
                staging, snapshot=snapshot, source_sha256="c" * 64,
                build_sql_sha256="d" * 64, phase="metadata_ready",
                baseline_payload_profile=deferred_payload.DEFERRED_PAYLOAD_PROFILE,
                baseline_export_sha256="a" * 64,
            )
            server = mock.Mock()
            server.execute.return_value = "0"
            options = dict(server=server, staging_dir=staging, snapshot=snapshot,
                           database=DATABASE, password=None, build_sql_sha256="d" * 64,
                           baseline_payload_profile=deferred_payload.DEFERRED_PAYLOAD_PROFILE,
                           baseline_export_sha256="a" * 64)
            before = dense.tree_contents(root)
            self.assertEqual(run_pipeline.validate_resume_state(**options), "c" * 64)
            for changed in ({"baseline_payload_profile": None},
                            {"baseline_export_sha256": "b" * 64}):
                with self.subTest(changed=changed), self.assertRaisesRegex(
                        run_pipeline.PipelineError, "baseline payload profile or export checksum"):
                    run_pipeline.validate_resume_state(**(options | changed))
            self.assertEqual(dense.tree_contents(root), before)

    def test_interrupted_import_or_finalization_cannot_resume_or_mutate_files(self):
        for phase in ("importing", "finalizing"):
            with self.subTest(phase=phase), \
                    TemporaryDirectory(prefix="ojs-deferred-phase-") as directory:
                root = Path(directory)
                source = root / "pkpbeacon-2026-01-01.sql"
                source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
                snapshot = run_pipeline.inspect_snapshot(source)
                clean = root / "clean"
                staging = clean / f"mysql-{snapshot.version}.building"
                staging.mkdir(parents=True)
                (staging / "keep.ibd").write_bytes(b"synthetic interrupted database")
                run_pipeline.write_build_state(
                    staging, snapshot=snapshot, source_sha256="c" * 64,
                    build_sql_sha256=run_pipeline.sha256_small_file(dense.BUILD_SQL), phase=phase,
                    baseline_payload_profile=deferred_payload.DEFERRED_PAYLOAD_PROFILE,
                    baseline_export_sha256="a" * 64,
                )
                before = dense.tree_contents(root)
                previous = run_pipeline.CleanExport(clean / "synthetic-prior.sql.gz", "2025-12-01")
                with mock.patch.object(run_pipeline.MySQLTools, "discover",
                                       return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "previous_clean_export", return_value=previous), \
                        mock.patch.object(run_pipeline, "_declared_checksum", return_value="a" * 64), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        mock.patch.object(run_pipeline.shutil, "rmtree") as remove, \
                        self.assertRaisesRegex(run_pipeline.PipelineError, "resume only at metadata_ready"):
                    build(snapshot, clean, True, resume_building=True)
                server.assert_not_called()
                remove.assert_not_called()
                self.assertEqual(dense.tree_contents(root), before)

    def test_event_layout_change_is_rejected_before_force_cleanup(self):
        for selected, recorded in ((True, None), (False, "innodb-events-compressed-8k-v1")):
            with self.subTest(selected=selected, recorded=recorded), \
                    TemporaryDirectory(prefix="ojs-event-profile-") as directory:
                root = Path(directory)
                source = root / "pkpbeacon-2026-01-01.sql"
                source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
                snapshot = run_pipeline.inspect_snapshot(source)
                clean = root / "clean"
                staging = clean / f"mysql-{snapshot.version}.building"
                staging.mkdir(parents=True)
                (staging / "keep.ibd").write_bytes(b"synthetic interrupted database")
                checkpoint = {} if recorded is None else {"event_history_profile": recorded}
                run_pipeline.build_state_path(staging).write_text(json.dumps(checkpoint))
                before = dense.tree_contents(root)
                with mock.patch.object(run_pipeline.MySQLTools, "discover",
                                       return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        mock.patch.object(run_pipeline.shutil, "rmtree") as remove, \
                        self.assertRaisesRegex(run_pipeline.PipelineError, "event.*profile|event.*layout"):
                    build(snapshot, clean, False, event_compression=selected,
                          force_rebuild=True, resume_building=True)
                server.assert_not_called()
                remove.assert_not_called()
                self.assertEqual(dense.tree_contents(root), before)


@unittest.skipUnless(dense.MYSQL_AVAILABLE, "MySQL 8.4 server and client tools required")
class DeferredPayloadIntegrationTests(unittest.TestCase):
    def inspect_cases(self, datadir, index, *, event_compression=False, page_kb=8):
        server = run_pipeline.MySQLServer(
            run_pipeline.MySQLTools.discover(), datadir, "128M", compact_storage=True,
        )
        try:
            server.start(PASSWORD)
            self.assertEqual(server.execute(
                "SELECT ROW_FORMAT,ZIP_PAGE_SIZE FROM information_schema.INNODB_TABLESPACES "
                "WHERE NAME=CONCAT(DATABASE(),'/ojs_article_events');",
                database=DATABASE, password=PASSWORD,
            ), f"Compressed\t{page_kb * 1024}" if event_compression else "Dynamic\t0")
            self.assertEqual(server.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
                f"AND TABLE_NAME IN ('{deferred_payload.TRACKING_TABLE}',"
                f"'{deferred_payload.HYDRATION_TABLE}');",
                database=DATABASE, password=PASSWORD,
            ), "0", "private hydration state must not survive publication")
            switch = server.execute(
                "SELECT canonical_source_record_id,HEX(canonical_metadata_hash) FROM ojs_articles "
                "WHERE canonical_source_record_id IN (7100,7101);",
                database=DATABASE, password=PASSWORD,
            )
            self.assertEqual(switch.split("\t")[0], "7101" if index == 1 else "7100")
            if index:
                self.assertEqual(server.execute(
                    "SELECT COUNT(*) FROM ojs_articles WHERE canonical_source_record_id IN (7001,7002) "
                    "AND status='removed' AND title IS NOT NULL AND metadata_xml IS NOT NULL;",
                    database=DATABASE, password=PASSWORD,
                ), "2")
                self.assertEqual(server.execute(
                    "SELECT title IS NULL,creators IS NULL FROM ojs_articles "
                    "WHERE canonical_source_record_id=7004;",
                    database=DATABASE, password=PASSWORD,
                ), "1\t1")
            return switch.split("\t")[1]
        finally:
            server.shutdown(PASSWORD)

    def test_three_snapshots_preserve_every_column_hash_event_and_delta(self):
        with TemporaryDirectory(prefix="ojs-deferred-parity-") as directory, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": "",
                                             "OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            snapshots = [make_snapshot(root, index) for index in range(3)]
            reference = root / "reference"
            helper = dense.DenseStorageIntegrationTests()
            expected = []
            canonical_hashes = []
            for index, snapshot in enumerate(snapshots):
                datadir, counts = build(snapshot, reference, False)
                expected.append((helper.catalogue(datadir, 8, index), counts,
                                 helper.report(reference, snapshot)))
                canonical_hashes.append(self.inspect_cases(datadir, index))
            self.assertEqual(len(set(canonical_hashes)), 1)

            for page_kb, event_modes in ((8, (True, True)), (8, (True, False)), (4, (True, True))):
                with self.subTest(page_kb=page_kb, event_modes=event_modes):
                    candidate = root / f"deferred-{page_kb}k-events-{int(event_modes[0])}-{int(event_modes[1])}"
                    copy_baseline(reference, candidate)
                    for index, event_compression in enumerate(event_modes, 1):
                        if event_modes == (True, True) and index == 1:
                            with mock.patch.object(run_pipeline, "extract_metadata_in_parallel",
                                                   side_effect=run_pipeline.PipelineError("synthetic metadata interruption")), \
                                    self.assertRaisesRegex(run_pipeline.PipelineError, "metadata interruption"):
                                build(snapshots[index], candidate, True,
                                      event_compression=event_compression, page_kb=page_kb)
                            staging = candidate / f"mysql-{DATES[index]}.building"
                            self.assertEqual(run_pipeline.read_build_state(staging)["phase"], "metadata_ready")
                            with mock.patch.object(run_pipeline.MySQLServer, "import_snapshot") as raw_import, \
                                    mock.patch.object(run_pipeline.MySQLServer, "import_clean_export") as history_import:
                                datadir, counts = build(snapshots[index], candidate, True,
                                                       event_compression=event_compression, page_kb=page_kb,
                                                       resume_building=True)
                            raw_import.assert_not_called()
                            history_import.assert_not_called()
                        else:
                            datadir, counts = build(snapshots[index], candidate, True,
                                                   event_compression=event_compression, page_kb=page_kb)
                        rows = helper.catalogue(datadir, page_kb, index)
                        self.assertEqual(self.inspect_cases(datadir, index, event_compression=event_compression,
                                                            page_kb=page_kb),
                                         canonical_hashes[index])
                        expected_rows, expected_counts, expected_report = expected[index]
                        self.assertEqual(counts, expected_counts)
                        self.assertEqual(helper.report(candidate, snapshots[index]), expected_report)
                        for table in run_pipeline.CLEAN_EXPORT_TABLES:
                            with self.subTest(snapshot=DATES[index], table=table):
                                self.assertEqual(rows[table], expected_rows[table])
                        report = json.loads(run_pipeline.change_report_path(candidate, DATES[index]).read_text())
                        if event_compression:
                            self.assertEqual(report["storage"]["event_history_profile"],
                                             f"innodb-events-compressed-{page_kb}k-v1")
                            self.assertEqual(report["storage"]["physical_layout_audit"]["tables"], {
                                table: {"row_format": "Compressed", "zip_page_size": page_kb * 1024}
                                for table in ("ojs_articles", "ojs_article_sources",
                                              "ojs_article_keys", "ojs_article_events")
                            })
                        else:
                            self.assertIsNone(report["storage"].get("event_history_profile"))
                        audit = report["storage"]["deferred_baseline_payload"]
                        self.assertEqual(audit["profile"], deferred_payload.DEFERRED_PAYLOAD_PROFILE)
                        self.assertGreater(audit["baseline_articles"], 0)
                        self.assertGreater(audit["hydrated_articles"], 0)
                        previous = run_pipeline.previous_clean_export(candidate, DATES[index])
                        self.assertEqual(audit["baseline_export_filename"], previous.path.name)
                        self.assertEqual(audit["baseline_export_sha256"],
                                         run_pipeline._declared_checksum(previous.path))

                    # Successful retention can remove the predecessor. A
                    # completed, hydrated release remains independently usable.
                    previous = run_pipeline.previous_clean_export(candidate, DATES[2])
                    previous.path.unlink()
                    run_pipeline.checksum_sidecar_path(previous.path).unlink()
                    with mock.patch.object(run_pipeline.MySQLServer, "hydrate_baseline_payload") as hydrate:
                        reused, reused_counts = build(snapshots[2], candidate, True,
                                                      event_compression=event_modes[1], page_kb=page_kb)
                    hydrate.assert_not_called()
                    self.assertEqual(reused, datadir)
                    self.assertEqual(reused_counts, expected[2][1])
                    self.assertEqual(helper.report(candidate, snapshots[2]), expected[2][2])
                    with mock.patch.object(run_pipeline.MySQLServer, "hydrate_baseline_payload") as hydrate:
                        verified_dir, verified_counts = build(
                            snapshots[2], candidate, False, event_compression=False,
                            page_kb=8, verify_existing=True,
                        )
                    hydrate.assert_not_called()
                    self.assertEqual(verified_dir, datadir)
                    self.assertEqual(verified_counts, expected[2][1])
                    # Direct verification authenticates the recorded physical
                    # layout, rather than guessing from a later operator env.
                    with mock.patch.dict(os.environ, {
                        "OJS_MYSQL_COMPRESSED_PAGE_KB": "4",
                        "OJS_COMPRESS_EVENT_HISTORY": str(int(not event_modes[1])),
                    }):
                        verified = run_pipeline.verify_existing_database(
                            snapshot=snapshots[2], datadir=datadir, build_sql=dense.BUILD_SQL,
                            database=DATABASE, root_password=PASSWORD, buffer_pool_size="128M",
                            progress_seconds=60, verify_source_checksum=False,
                        )
                    self.assertEqual(verified, expected[2][1])

    def test_interrupted_or_wrong_checksum_hydration_never_publishes(self):
        with TemporaryDirectory(prefix="ojs-deferred-crash-") as directory, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": "",
                                             "OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            snapshots = [make_snapshot(root, index) for index in (0, 1)]
            reference = root / "reference"
            original, _ = build(snapshots[0], reference, False)
            original_hydrate = run_pipeline.MySQLServer.hydrate_baseline_payload
            for fault in ("interrupted", "checksum", "unfinished", "row-count", "payload-mismatch"):
                with self.subTest(fault=fault):
                    candidate = root / fault
                    copy_baseline(reference, candidate)
                    pointer = candidate / "mysql-current"
                    pointer.symlink_to(original)
                    baseline_before = file_metadata(reference)
                    calls = []

                    def fail_hydration(server, clean_export, **options):
                        calls.append(True)
                        self.assertGreater(int(server.execute(
                            f"SELECT COUNT(*) FROM {deferred_payload.TRACKING_TABLE};",
                            database=DATABASE, password=options["password"],
                        )), 0)
                        if fault == "interrupted":
                            raise run_pipeline.PipelineError("synthetic hydration interruption")
                        if fault == "unfinished":
                            return 0
                        if fault == "row-count":
                            original_hydrate(server, clean_export, **options)
                            return 0
                        if fault == "payload-mismatch":
                            server.execute(
                                "CREATE TRIGGER fixture_corrupt_payload BEFORE UPDATE ON ojs_articles "
                                "FOR EACH ROW SET NEW.title='synthetic wrong hydrated payload';",
                                database=DATABASE, password=options["password"],
                            )
                            return original_hydrate(server, clean_export, **options)
                        options["expected_sha256"] = "0" * 64
                        return original_hydrate(server, clean_export, **options)

                    with mock.patch.object(run_pipeline.MySQLServer, "hydrate_baseline_payload",
                                           new=fail_hydration), \
                            self.assertRaisesRegex(run_pipeline.PipelineError, "hydration|checksum"):
                        build(snapshots[1], candidate, True, publish_current=True)
                    self.assertEqual(calls, [True])
                    self.assertEqual(pointer.resolve(), original)
                    self.assertEqual(file_metadata(reference), baseline_before)
                    self.assertFalse((candidate / f"mysql-{DATES[1]}").exists())
                    for artifact in (run_pipeline.clean_export_path, run_pipeline.change_report_path,
                                     run_pipeline.release_manifest_path):
                        self.assertFalse(artifact(candidate, DATES[1]).exists())
                    staging = candidate / f"mysql-{DATES[1]}.building"
                    checkpoint = run_pipeline.read_build_state(staging)
                    self.assertEqual(checkpoint["baseline_payload_profile"],
                                     deferred_payload.DEFERRED_PAYLOAD_PROFILE)
                    previous = run_pipeline.previous_clean_export(candidate, DATES[1])
                    self.assertEqual(checkpoint["baseline_export_sha256"],
                                     run_pipeline._declared_checksum(previous.path))


if __name__ == "__main__":
    unittest.main()
