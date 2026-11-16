"""Synthetic MySQL parity for smaller physical compressed pages.

No existing application datadir or real dump is opened. The tests import a
genuine 8 KiB predecessor export into a fresh 4 KiB candidate, then advance it
again, checking the complete logical catalogue and oversized Unicode payloads.
Unsupported layouts and mismatched checkpoints must fail before any mutation.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
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
import run_pipeline
import test_pipeline as fixture
from test_compact_storage_equivalence import logical_rows, snapshot_rows
from test_metadata_storage_integration import large_payload, PAYLOAD_FIELDS


BUILD_SQL = Path(__file__).parents[1] / "sql" / "01_build_ojs_tables.sql"
DATABASE = "pkpbeacon_db"
PASSWORD = "synthetic-dense-root-password"
DATES = ("2026-01-01", "2026-07-01", "2026-11-01")
MYSQL_AVAILABLE = all(shutil.which(name) for name in
                      ("mysqld", "mysql", "mysqladmin", "mysqldump"))


def dense_snapshot(root: Path, index: int):
    xml, _ = large_payload(index)
    rows = snapshot_rows(index)
    rows.append(fixture.record_row(
        record_id=5000, context_id=1, identifier=9000, metadata_xml=xml,
        # Hash-based changes must work even when publisher timestamps do not.
        modified="2026-01-01 00:00:00",
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


def build_candidate(snapshot, clean: Path, page_kb: int, **overrides):
    options = dict(
        snapshot=snapshot, clean_dir=clean, build_sql=BUILD_SQL,
        database=DATABASE, root_password=PASSWORD,
        api_db_user="fixture_api", api_db_password="synthetic-api-password",
        release_thresholds=run_pipeline.ReleaseThresholds(1, 1, 1, 1),
        allow_anomalous_release=False, buffer_pool_size="128M", progress_seconds=60,
        force_rebuild=False, verify_existing=False, verify_source_checksum=False,
        full_rescan=False, publish_current=False, metadata_workers=1,
        resume_building=False, compact_storage=True,
    )
    options.update(overrides)
    with mock.patch.dict(os.environ, {"OJS_MYSQL_COMPRESSED_PAGE_KB": str(page_kb)}):
        return run_pipeline.build_snapshot_database(**options)


def tree_contents(root: Path):
    return {
        str(path.relative_to(root)): (
            path.stat().st_ino, path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


class DenseStoragePreflightTests(unittest.TestCase):
    def test_2k_is_rejected_before_discovery_or_database_files_are_created(self):
        with TemporaryDirectory(prefix="ojs-layout-refusal-") as directory:
            root = Path(directory)
            source = root / "pkpbeacon-2026-01-01.sql"
            source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
            snapshot = run_pipeline.inspect_snapshot(source)
            before = tree_contents(root)
            with mock.patch.object(run_pipeline.MySQLTools, "discover") as discover, \
                    mock.patch.object(run_pipeline, "MySQLServer") as server, \
                    self.assertRaisesRegex(run_pipeline.PipelineError, "exactly 8 or 4"):
                build_candidate(snapshot, root / "clean", 2)
            discover.assert_not_called()
            server.assert_not_called()
            self.assertFalse((root / "clean").exists())
            self.assertEqual(tree_contents(root), before)

    def test_mismatched_layout_preserves_every_artifact_even_with_force_rebuild(self):
        cases = (
            (4, None, True),
            (4, None, False),
            (8, "innodb-compressed-4k-v1", True),
            (4, "unknown-layout", True),
        )
        for selected, checkpoint_profile, has_checkpoint in cases:
            with self.subTest(selected=selected, profile=checkpoint_profile,
                              checkpoint=has_checkpoint), \
                    TemporaryDirectory(prefix="ojs-layout-preserve-") as directory:
                root = Path(directory)
                source = root / "pkpbeacon-2026-01-01.sql"
                source.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
                snapshot = run_pipeline.inspect_snapshot(source)
                clean = root / "clean"
                staging = clean / f"mysql-{snapshot.version}.building"
                staging.mkdir(parents=True)
                (staging / "keep.ibd").write_bytes(b"synthetic interrupted database")
                final = clean / f"mysql-{snapshot.version}"
                final.mkdir()
                (final / "keep.ibd").write_bytes(b"synthetic published database")
                for artifact_path in (
                    run_pipeline.clean_export_path,
                    run_pipeline.change_report_path,
                    run_pipeline.release_manifest_path,
                ):
                    artifact = artifact_path(clean, snapshot.version)
                    artifact.write_bytes(b"existing publication artifact")
                    (clean / f".{artifact.name}.tmp").write_bytes(b"existing candidate artifact")
                if has_checkpoint:
                    run_pipeline.write_build_state(
                        staging, snapshot=snapshot,
                        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        build_sql_sha256=run_pipeline.sha256_small_file(BUILD_SQL),
                        phase="importing", storage_layout_profile=checkpoint_profile,
                    )
                before = tree_contents(root)
                with mock.patch.object(run_pipeline.MySQLTools, "discover",
                                       return_value=mock.Mock(version="8.4.11")), \
                        mock.patch.object(run_pipeline, "MySQLServer") as server, \
                        mock.patch.object(run_pipeline.shutil, "rmtree") as remove, \
                        self.assertRaisesRegex(run_pipeline.PipelineError,
                                               "different physical storage layout"):
                    build_candidate(snapshot, clean, selected,
                                    force_rebuild=True, resume_building=True)
                server.assert_not_called()
                remove.assert_not_called()
                self.assertEqual(tree_contents(root), before)

    def test_matching_checkpoint_validation_is_read_only(self):
        for profile in (None, "innodb-compressed-4k-v1"):
            with self.subTest(profile=profile), \
                    TemporaryDirectory(prefix="ojs-layout-match-") as directory:
                staging = Path(directory)
                (staging / "keep.ibd").write_bytes(b"synthetic existing database")
                state = {} if profile is None else {"storage_layout_profile": profile}
                run_pipeline.build_state_path(staging).write_text(json.dumps(state))
                before = tree_contents(staging)
                run_pipeline.validate_staging_layout(staging, profile)
                self.assertEqual(tree_contents(staging), before)


@unittest.skipUnless(MYSQL_AVAILABLE, "MySQL 8.4 server and client tools required")
class DenseStorageIntegrationTests(unittest.TestCase):
    def build(self, snapshot, clean: Path, page_kb: int):
        return build_candidate(snapshot, clean, page_kb)

    def catalogue(self, datadir: Path, page_kb: int, index: int):
        with mock.patch.dict(os.environ, {"OJS_MYSQL_COMPRESSED_PAGE_KB": str(page_kb)}):
            server = run_pipeline.MySQLServer(
                run_pipeline.MySQLTools.discover(), datadir, "128M", compact_storage=True,
            )
        try:
            server.start(PASSWORD)
            for table in ("ojs_articles", "ojs_article_sources", "ojs_article_keys"):
                with self.subTest(table=table):
                    options = server.execute(
                        "SELECT ROW_FORMAT,CREATE_OPTIONS FROM information_schema.TABLES "
                        f"WHERE TABLE_SCHEMA='{DATABASE}' AND TABLE_NAME='{table}';",
                        password=PASSWORD,
                    )
                    self.assertTrue(options.startswith("Compressed\t"), options)
                    self.assertIn(f"key_block_size={page_kb}", options.lower())

            xml, expected = large_payload(index)
            for field in PAYLOAD_FIELDS:
                actual = server.execute(
                    f"SELECT OCTET_LENGTH(`{field}`),SHA2(`{field}`,256) "
                    "FROM ojs_articles WHERE canonical_source_record_id=5000;",
                    database=DATABASE, password=PASSWORD,
                )
                self.assertEqual(actual, f"{len(expected[field].encode())}\t"
                                 + hashlib.sha256(expected[field].encode()).hexdigest())
            self.assertEqual(server.execute(
                "SELECT SHA2(metadata_xml,256) FROM ojs_articles "
                "WHERE canonical_source_record_id=5000;", database=DATABASE, password=PASSWORD,
            ), hashlib.sha256(xml.encode()).hexdigest())
            return {
                table: logical_rows(server, DATABASE, table, password=PASSWORD)
                for table in run_pipeline.CLEAN_EXPORT_TABLES
            }
        finally:
            server.shutdown(PASSWORD)

    def report(self, clean: Path, snapshot):
        report = json.loads(run_pipeline.change_report_path(clean, snapshot.version).read_text())
        self.assertEqual(report["snapshot"]["source_sha256"],
                         hashlib.sha256(snapshot.path.read_bytes()).hexdigest())
        self.assertEqual(report["snapshot"]["build_sql_sha256"],
                         run_pipeline.sha256_small_file(BUILD_SQL))
        self.assertTrue(run_pipeline.release_artifacts_complete(clean, snapshot.version))
        storage = report.pop("storage")
        if storage.get("compressed_page_kb") == 4:
            tables = ["ojs_articles", "ojs_article_sources", "ojs_article_keys"]
            if storage.get("event_history_profile"):
                tables.append("ojs_article_events")
            self.assertEqual(storage["physical_layout_audit"], {
                "profile": "innodb-compressed-4k-v1",
                "tables": {
                    table: {"row_format": "Compressed", "zip_page_size": 4096}
                    for table in tables
                },
            })
        return report

    def test_8k_predecessor_export_advances_with_dense_pages_without_logical_changes(self):
        with TemporaryDirectory(prefix="ojs-dense-history-") as directory, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": "",
                                             "OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            snapshots = [dense_snapshot(root, index) for index in range(3)]
            reference = root / "reference"
            expected = []
            for index, snapshot in enumerate(snapshots):
                datadir, counts = self.build(snapshot, reference, 8)
                expected.append((self.catalogue(datadir, 8, index), counts,
                                 self.report(reference, snapshot)))

            # Both candidates start from a genuine old 8 KiB export. One stays
            # dense; the other returns to 8 KiB on the final snapshot.
            for pages in ((4, 4), (4, 8)):
                with self.subTest(compressed_page_sequence=pages):
                    clean = root / f"dense-{pages[0]}-{pages[1]}"
                    clean.mkdir()
                    for path in reference.iterdir():
                        if path.is_file() and DATES[0] in path.name:
                            shutil.copy2(path, clean / path.name)
                    for index, page_kb in enumerate(pages, 1):
                        snapshot = snapshots[index]
                        datadir, counts = self.build(snapshot, clean, page_kb)
                        actual = self.catalogue(datadir, page_kb, index)
                        expected_rows, expected_counts, expected_report = expected[index]
                        self.assertEqual(counts, expected_counts)
                        self.assertEqual(self.report(clean, snapshot), expected_report)
                        for table in run_pipeline.CLEAN_EXPORT_TABLES:
                            with self.subTest(snapshot=snapshot.version, table=table):
                                self.assertEqual(actual[table], expected_rows[table])
                    with mock.patch.dict(os.environ, {"OJS_MYSQL_COMPRESSED_PAGE_KB": "8",
                                                     "OJS_COMPRESS_EVENT_HISTORY": "1"}):
                        verified = run_pipeline.verify_existing_database(
                            snapshot=snapshots[2], datadir=datadir, build_sql=BUILD_SQL,
                            database=DATABASE, root_password=PASSWORD, buffer_pool_size="128M",
                            progress_seconds=60, verify_source_checksum=False,
                        )
                    self.assertEqual(verified, expected[2][1])


if __name__ == "__main__":
    unittest.main()
