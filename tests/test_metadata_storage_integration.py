"""Real MySQL regressions for payloads exceeding TEXT's byte capacity.

Only disposable, tiny fixture databases are used. The large title reproduces
the production failure's byte length without including any production record.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import metadata_storage
import run_pipeline
import test_pipeline as fixture


BUILD_SQL = Path(__file__).parents[1] / "sql" / "01_build_ojs_tables.sql"
MYSQL_AVAILABLE = all(shutil.which(name) for name in
                      ("mysqld", "mysql", "mysqladmin", "mysqldump"))
DATABASE = "pkpbeacon_db"
PASSWORD = "fixture-root-password"
DATES = ("2026-01-01", "2026-07-01", "2026-10-01")
PAYLOAD_FIELDS = ("title", "publisher", "published", "types", "formats",
                  "source_title", "languages", "coverage")


def large_payload(revision: int) -> tuple[str, dict[str, str]]:
    values = {
        # Exactly 983,983 bytes at revision 1, matching the observed overflow.
        "title": "é" * 491_991 + ("x" if revision == 1 else "y"),
        # These have fewer than 65,535 characters but more than 65,535 bytes.
        "first_creator": "界" * 22_000,
        "publisher": "Publisher " + "界" * 22_000,
        "published": "2025-01-15 " + "界" * 22_000,
        "types": "Article " + "界" * 22_000,
        "formats": "Format " + "界" * 22_000,
        "source_title": "Journal " + "界" * 22_000,
        "languages": "Language " + "界" * 22_000,
        "coverage": "Coverage " + "界" * 22_000,
    }
    xml = fixture.metadata(
        oai_identifier="oai:oversized:100", title=values["title"],
        creator=values["first_creator"], published="2025-01-15",
        url="https://one.example/article/100", doi="10.1234/oversized",
    )
    xml = xml.replace("<dc:date>2025-01-15</dc:date>",
                      f"<dc:date>{values['published']}</dc:date>")
    xml = xml.replace("<dc:type>info:eu-repo/semantics/article</dc:type>",
                      f"<dc:type>{values['types']}</dc:type>")
    tags = {"publisher": "publisher", "formats": "format",
            "source_title": "source", "languages": "language",
            "coverage": "coverage"}
    xml = xml.replace("</oai_dc:dc>", "".join(
        f"<dc:{tag}>{values[field]}</dc:{tag}>" for field, tag in tags.items()
    ) + "</oai_dc:dc>")
    return xml, values


def rows(index: int) -> list[str]:
    result = []
    for record_id in (1, 100, 101, 200):
        doi = "10.1234/oversized" if record_id in (100, 101) else f"10.1234/{record_id}"
        xml = fixture.metadata(
            oai_identifier=f"oai:fixture:{record_id}",
            title=f"Small title {record_id}" + (" revised" if record_id == 1 and index else ""),
            creator="Fixture Author", published="2025-01-15",
            url=f"https://one.example/article/{record_id}", doi=doi,
        )
        if record_id == 100 and index:
            xml, _ = large_payload(index)
        result.append(fixture.record_row(
            record_id=record_id, context_id=1, identifier=1000 + record_id,
            metadata_xml=xml, modified=DATES[index] + " 00:00:00",
            removed_at=DATES[1] + " 00:00:00" if record_id == 200 and index == 1 else None,
        ))
    return result


def snapshot(root: Path, index: int):
    path = root / f"pkpbeacon-{DATES[index]}.sql"
    path.write_text(fixture.dump(DATES[index], rows(index)), encoding="utf-8")
    return run_pipeline.inspect_snapshot(path)


def build_options(item, clean: Path) -> dict:
    return dict(
        snapshot=item, clean_dir=clean, build_sql=BUILD_SQL, database=DATABASE,
        root_password=PASSWORD, api_db_user="fixture_api",
        api_db_password="fixture-api-password",
        # The four-row adversarial fixture intentionally removes one article;
        # production thresholds are not changed by this test.
        release_thresholds=run_pipeline.ReleaseThresholds(1, 1, 1, 1),
        allow_anomalous_release=False, buffer_pool_size="128M",
        progress_seconds=60, force_rebuild=False, verify_existing=False,
        verify_source_checksum=False, full_rescan=False, publish_current=False,
        metadata_workers=1, resume_building=False, compact_storage=True,
    )


def sql_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@unittest.skipUnless(MYSQL_AVAILABLE, "MySQL 8.4 server and client tools required")
class MetadataStorageIntegrationTests(unittest.TestCase):
    def assert_payload(self, server, index: int, password=None):
        xml, expected = large_payload(index)
        for table, fields, predicate in (
            ("ojs_articles", PAYLOAD_FIELDS, "canonical_source_record_id=100"),
            ("ojs_article_sources", ("title", "first_creator"), "source_record_id=100"),
        ):
            for field in fields:
                with self.subTest(table=table, field=field, snapshot=DATES[index]):
                    actual = server.execute(
                        f"SELECT OCTET_LENGTH(`{field}`), SHA2(`{field}`,256) "
                        f"FROM `{table}` WHERE {predicate};",
                        database=DATABASE, password=password,
                    )
                    self.assertEqual(actual, f"{len(expected[field].encode())}\t{sql_hash(expected[field])}")
        self.assertEqual(server.execute(
            "SELECT SHA2(metadata_xml,256),SHA2(creators,256) FROM ojs_articles "
            "WHERE canonical_source_record_id=100;", database=DATABASE, password=password,
        ), f"{sql_hash(xml)}\t{sql_hash(expected['first_creator'])}")

    def test_legacy_checkpoint_resume_and_three_snapshot_history_preserve_large_payloads(self):
        with TemporaryDirectory(prefix="ojs-metadata-history-") as directory, \
                mock.patch.dict(os.environ, {"OJS_METADATA_MIGRATION_RESERVE_BYTES": "0"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            clean = root / "clean"
            tools = run_pipeline.MySQLTools.discover()
            items = [snapshot(root, index) for index in range(3)]

            # Produce a genuine old-schema January export, as exists on disk
            # before the lossless storage profile was introduced.
            with mock.patch.object(run_pipeline, "render_metadata_storage_sql", side_effect=lambda sql: sql), \
                    mock.patch.object(run_pipeline, "migrate_metadata_storage", return_value={}):
                january, _ = run_pipeline.build_snapshot_database(**build_options(items[0], clean))
            server = run_pipeline.MySQLServer(tools, january, "128M", compact_storage=True)
            try:
                server.start(PASSWORD)
                self.assertEqual(server.execute(
                    "SELECT DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='ojs_articles' AND COLUMN_NAME='title';",
                    database=DATABASE, password=PASSWORD,
                ), "text")
                original_ids = server.execute(
                    "SELECT source_record_id,article_id FROM ojs_article_sources ORDER BY source_record_id;",
                    database=DATABASE, password=PASSWORD,
                )
            finally:
                server.shutdown(PASSWORD)

            # Recreate the actual failed boundary: old imported history, raw
            # July data, completed source index, partial generated metadata.
            staging = clean / "mysql-2026-07-01.building"
            server = run_pipeline.MySQLServer(tools, staging, "128M", compact_storage=True)
            old_parts = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True, lossless_metadata=False)
            source_hash = ""
            try:
                server.initialize()
                server.start(None)
                run_pipeline.create_database(server, DATABASE)
                source_hash = server.import_snapshot(items[1], database=DATABASE, password=None,
                                                     progress_seconds=60)
                previous = run_pipeline.previous_clean_export(clean, DATES[1])
                self.assertIsNotNone(previous)
                server.import_clean_export(previous, database=DATABASE, password=None, progress_seconds=60)
                prelude = run_pipeline.build_sql_prelude(
                    snapshot=items[1], source_sha256=source_hash,
                    build_sql_sha256=run_pipeline.sha256_small_file(BUILD_SQL),
                    mysql_version=tools.version, full_rescan=False,
                )
                server.run_sql_text(old_parts.prefix, label="old-source-index", database=DATABASE,
                                    password=None, prelude=prelude)
                server.run_sql_text(old_parts.metadata, label="old-partial-metadata", database=DATABASE,
                                    password=None, prelude=prelude + "\nSET @ojs_metadata_max_id=1;")
                self.assertEqual(server.execute("SELECT COUNT(*) FROM ojs_stage_sources;",
                                               database=DATABASE), "1")
                # Reproduce the original failure before applying the fix;
                # MySQL rolls back the failed INSERT, retaining the small row.
                with self.assertRaisesRegex(run_pipeline.PipelineError, "mysql_error=1406"):
                    server.run_sql_text(
                        old_parts.metadata, label="legacy-width-overflow", database=DATABASE,
                        password=None,
                        prelude=prelude + "\nSET @ojs_metadata_min_id=100;\nSET @ojs_metadata_max_id=100;",
                    )
                self.assertEqual(server.execute("SELECT COUNT(*) FROM ojs_stage_sources;",
                                               database=DATABASE), "1")
                # Simulate a previously interrupted widening. The migrator
                # must preserve this column and finish all remaining columns.
                server.execute("ALTER TABLE ojs_article_sources MODIFY COLUMN title MEDIUMTEXT NULL, "
                               "ALGORITHM=COPY, LOCK=EXCLUSIVE;", database=DATABASE)
                run_pipeline.write_build_state(
                    staging, snapshot=items[1], source_sha256=source_hash,
                    build_sql_sha256=run_pipeline.sha256_small_file(BUILD_SQL), phase="metadata_ready",
                )
                source_index = server.execute(
                    "SELECT source_record_id,needs_metadata_parse,is_active FROM ojs_stage_source_index "
                    "ORDER BY source_record_id;", database=DATABASE,
                )
            finally:
                server.shutdown(None)

            inodes = {name: (staging / DATABASE / f"{name}.ibd").stat().st_ino
                      for name in ("records", "ojs_stage_source_index")}
            original_start = run_pipeline.MySQLServer.start
            reused = []

            def checked_start(resumed, password):
                for name, inode in inodes.items():
                    self.assertEqual((resumed.datadir / DATABASE / f"{name}.ibd").stat().st_ino, inode)
                original_start(resumed, password)
                self.assertEqual(resumed.execute(
                    "SELECT source_record_id,needs_metadata_parse,is_active FROM ojs_stage_source_index "
                    "ORDER BY source_record_id;", database=DATABASE,
                ), source_index)
                reused.append(True)

            with mock.patch.object(run_pipeline.MySQLServer, "initialize", side_effect=AssertionError("must reuse raw database")), \
                    mock.patch.object(run_pipeline.MySQLServer, "import_snapshot", side_effect=AssertionError("must not repeat raw import")), \
                    mock.patch.object(run_pipeline.MySQLServer, "import_clean_export", side_effect=AssertionError("must not repeat history import")), \
                    mock.patch.object(run_pipeline.MySQLServer, "start", new=checked_start):
                july, _ = run_pipeline.build_snapshot_database(
                    **{**build_options(items[1], clean), "resume_building": True})
            self.assertEqual(reused, [True])
            server = run_pipeline.MySQLServer(tools, july, "128M", compact_storage=True)
            try:
                server.start(PASSWORD)
                self.assert_payload(server, 1, PASSWORD)
                self.assertEqual(server.execute(
                    "SELECT source_record_id,article_id FROM ojs_article_sources ORDER BY source_record_id;",
                    database=DATABASE, password=PASSWORD,
                ), original_ids)
                events_before = server.execute(
                    "SELECT event_id,snapshot_date,article_id,event_type,HEX(data_hash) "
                    "FROM ojs_article_events ORDER BY event_id;", database=DATABASE, password=PASSWORD,
                ).splitlines()
                # Reapplying a completed migration must issue no ALTER and
                # preserve both metadata and row/event identities.
                with mock.patch.object(server, "run_sql_text", side_effect=AssertionError("already widened")):
                    audit = metadata_storage.migrate_metadata_storage(
                        server, database=DATABASE, password=PASSWORD, reserve_bytes=0,
                        required_tables=("ojs_articles", "ojs_article_sources"),
                    )
                self.assertTrue(all(not item["changed_columns"] for item in audit["tables"]))
            finally:
                server.shutdown(PASSWORD)

            october, _ = run_pipeline.build_snapshot_database(**build_options(items[2], clean))
            server = run_pipeline.MySQLServer(tools, october, "128M", compact_storage=True)
            try:
                server.start(PASSWORD)
                self.assert_payload(server, 2, PASSWORD)
                self.assertEqual(server.execute(
                    "SELECT source_record_id,article_id FROM ojs_article_sources ORDER BY source_record_id;",
                    database=DATABASE, password=PASSWORD,
                ), original_ids)
                self.assertEqual(server.execute(
                    "SELECT COUNT(DISTINCT article_id) FROM ojs_article_sources WHERE source_record_id IN(100,101);",
                    database=DATABASE, password=PASSWORD,
                ), "1", "cross-source DOI deduplication must survive widening")
                self.assertEqual(server.execute("SELECT snapshot_date FROM ojs_snapshots ORDER BY snapshot_date;",
                                                database=DATABASE, password=PASSWORD).splitlines(), list(DATES))
                events_after = server.execute(
                    "SELECT event_id,snapshot_date,article_id,event_type,HEX(data_hash) "
                    "FROM ojs_article_events ORDER BY event_id;", database=DATABASE, password=PASSWORD,
                ).splitlines()
                self.assertEqual(events_after[:len(events_before)], events_before)
                self.assertEqual(server.execute(
                    "SELECT COUNT(*) FROM ojs_article_events WHERE snapshot_date='2026-07-01' AND event_type='removed';",
                    database=DATABASE, password=PASSWORD,
                ), "1")
                self.assertEqual(server.execute(
                    "SELECT COUNT(*) FROM ojs_article_events WHERE snapshot_date='2026-10-01' AND event_type='restored';",
                    database=DATABASE, password=PASSWORD,
                ), "1")
                self.assertEqual(server.execute(
                    "SELECT COUNT(*) FROM ojs_article_events WHERE snapshot_date='2026-10-01' "
                    "AND event_type='modified' AND article_id=(SELECT article_id FROM ojs_article_sources WHERE source_record_id=100);",
                    database=DATABASE, password=PASSWORD,
                ), "1")
            finally:
                server.shutdown(PASSWORD)
            report = json.loads(run_pipeline.change_report_path(clean, DATES[1]).read_text())
            self.assertEqual(report["snapshot"]["source_sha256"], source_hash)
            self.assertEqual(report["snapshot"]["build_sql_sha256"], run_pipeline.sha256_small_file(BUILD_SQL))
            self.assertTrue(all(run_pipeline.release_artifacts_complete(clean, date) for date in DATES))

    def test_fresh_standard_and_compact_storage_preserve_identical_oversized_values(self):
        with TemporaryDirectory(prefix="ojs-metadata-width-") as directory, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            tools = run_pipeline.MySQLTools.discover()
            item = snapshot(root, 1)
            server = run_pipeline.MySQLServer(tools, root / "mysql", "128M")
            try:
                server.initialize()
                server.start(None)
                for compact in (False, True):
                    with self.subTest(compact=compact):
                        server.compact_storage = compact
                        run_pipeline.create_database(server, DATABASE)
                        source_hash = server.import_snapshot(item, database=DATABASE, password=None,
                                                             progress_seconds=60)
                        parts = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=compact,
                                                             lossless_metadata=True)
                        prelude = run_pipeline.build_sql_prelude(
                            snapshot=item, source_sha256=source_hash,
                            build_sql_sha256=run_pipeline.sha256_small_file(BUILD_SQL),
                            mysql_version=tools.version, full_rescan=False,
                        )
                        for label, sql in (("index", parts.prefix), ("metadata", parts.metadata),
                                           ("finalization", parts.suffix)):
                            server.run_sql_text(sql, label=label, database=DATABASE, password=None,
                                                prelude=prelude)
                        self.assert_payload(server, 1)
                        self.assertEqual(server.execute(
                            "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                            "AND TABLE_NAME IN ('ojs_articles','ojs_article_sources','ojs_stage_sources',"
                            "'ojs_stage_canonical_payload') AND DATA_TYPE='text';", database=DATABASE,
                        ), "0")
                        server.execute(f"DROP DATABASE `{DATABASE}`;")
            finally:
                server.shutdown(None)


if __name__ == "__main__":
    unittest.main()
