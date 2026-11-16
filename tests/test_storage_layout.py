from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import run_pipeline
from storage_layout import (
    CLEAN_TABLES, COMPACT_TABLES, CleanExportDDL, StorageLayoutError,
    audit_storage_layout, compression_options, compression_page_kb, layout_profile,
    event_history_enabled, event_history_profile, recorded_layout_audit_options,
    render_event_history_sql,
)


ROOT = Path(__file__).parents[1]
ENV = "OJS_MYSQL_COMPRESSED_PAGE_KB"


def clean_dump(*, options=b" DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci", payload=b"'value'"):
    tables = []
    for table in sorted(CLEAN_TABLES):
        name = table.encode("ascii")
        tables.append(
            b"DROP TABLE IF EXISTS `" + name + b"`;\n"
            b"CREATE TABLE `" + name + b"` (\n  `id` bigint NOT NULL\n"
            b") ENGINE=InnoDB" + options + b";\n"
            b"INSERT INTO `" + name + b"` VALUES (" + payload + b");\n"
        )
    return b"/*!40101 SET NAMES utf8mb4 */;\n" + b"".join(tables)


def transform(data, page_kb=4, limit=65536, **options):
    parser = CleanExportDDL(page_kb, **options)
    source = io.BytesIO(data)
    output = []
    while chunk := source.readline(limit):
        output.append(parser.transform(chunk))
    parser.finish()
    return b"".join(output)


class StorageLayoutTests(unittest.TestCase):
    def test_event_compression_configuration_is_strict_and_opt_in(self):
        self.assertFalse(event_history_enabled({}))
        self.assertFalse(event_history_enabled({"OJS_COMPRESS_EVENT_HISTORY": "0"}))
        self.assertTrue(event_history_enabled({"OJS_COMPRESS_EVENT_HISTORY": "1"}))
        for value in ("", "true", "yes", "2", " 1", "1\n", 1, True, None):
            with self.subTest(value=value), self.assertRaises(StorageLayoutError):
                event_history_enabled({"OJS_COMPRESS_EVENT_HISTORY": value})

    def test_recorded_audit_profiles_ignore_current_environment(self):
        with mock.patch.dict(os.environ, {ENV: "4", "OJS_COMPRESS_EVENT_HISTORY": "1"}):
            self.assertIsNone(recorded_layout_audit_options({}))
            self.assertEqual(recorded_layout_audit_options({"storage_layout_profile": layout_profile(4)}),
                             {"page_kb": 4, "include_event_history": False})
            self.assertEqual(recorded_layout_audit_options({"event_history_profile": event_history_profile(8)}),
                             {"page_kb": 8, "include_event_history": True})
            self.assertEqual(recorded_layout_audit_options({
                "storage_layout_profile": layout_profile(4),
                "event_history_profile": event_history_profile(4),
            }), {"page_kb": 4, "include_event_history": True})
        for storage in (None, [], {"storage_layout_profile": []},
                        {"event_history_profile": "unknown"},
                        {"event_history_profile": event_history_profile(4)},
                        {"storage_layout_profile": layout_profile(4),
                         "event_history_profile": event_history_profile(8)}):
            with self.subTest(storage=storage), self.assertRaises(StorageLayoutError):
                recorded_layout_audit_options(storage)

    def test_event_physical_audit_requires_matching_actual_tablespace(self):
        server = mock.Mock()
        original = "\n".join(f"{table}\tCompressed\t8192" for table in sorted(COMPACT_TABLES))
        server.execute.return_value = original + "\nojs_article_events\tCompressed\t8192"
        audit = audit_storage_layout(server, database="fixture", password=None, page_kb=8,
                                     include_event_history=True)
        self.assertEqual(set(audit["tables"]), COMPACT_TABLES | {"ojs_article_events"})
        for event_row in ("", "\nojs_article_events\tCompressed\t4096",
                          "\nojs_article_events\tDynamic\t0"):
            server.execute.return_value = original + event_row
            with self.subTest(event_row=event_row), self.assertRaises(StorageLayoutError):
                audit_storage_layout(server, database="fixture", password=None, page_kb=8,
                                     include_event_history=True)

    def test_event_build_ddl_changes_create_only_and_rejects_unknown_shape(self):
        original = (ROOT / "sql/01_build_ojs_tables.sql").read_text()
        output = render_event_history_sql(original, 4)
        self.assertEqual(output.replace("ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4 ", "", 1), original)
        self.assertEqual(output.count("ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4"), 1)
        event_start = output.index("CREATE TABLE IF NOT EXISTS ojs_article_events (")
        self.assertIn("ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4", output[event_start:].split(";", 1)[0])
        for invalid in (output, original.replace("CREATE TABLE IF NOT EXISTS ojs_article_events", "CREATE TABLE events"),
                        original + original):
            with self.subTest(invalid=invalid[:50]), self.assertRaises(StorageLayoutError):
                render_event_history_sql(invalid, 4)

    def test_event_export_ddl_is_opt_in_and_can_restore_dynamic_successor(self):
        payload = b"'ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8; INSERT INTO fake VALUES (1)'"
        original = clean_dump(payload=payload)
        compressed = transform(original, compress_events=True)
        self.assertEqual(compressed.count(b"KEY_BLOCK_SIZE=4"), 4)
        self.assertEqual(compressed.count(payload), len(CLEAN_TABLES))
        event = compressed.split(b"CREATE TABLE `ojs_article_events`", 1)[1].split(b";\n", 1)[0]
        self.assertIn(b"ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4", event)
        restored = transform(compressed, 8, restore_uncompressed_events=True)
        event = restored.split(b"CREATE TABLE `ojs_article_events`", 1)[1].split(b";\n", 1)[0]
        self.assertIn(b"ROW_FORMAT=DYNAMIC", event)
        self.assertNotIn(b"KEY_BLOCK_SIZE", event)
        self.assertEqual(restored.count(payload), len(CLEAN_TABLES))
        for options in ({"compress_events": 1}, {"restore_uncompressed_events": "1"},
                        {"compress_events": True, "restore_uncompressed_events": True}):
            with self.subTest(options=options), self.assertRaises(StorageLayoutError):
                CleanExportDDL(4, **options)

    def test_physical_audit_records_actual_tablespace_metadata_without_scanning_data(self):
        server = mock.Mock()
        server.execute.return_value = "\n".join(
            f"{table}\tCompressed\t4096" for table in sorted(COMPACT_TABLES)
        )
        audit = audit_storage_layout(server, database="fixture", password="private", page_kb=4)
        self.assertEqual(audit["profile"], layout_profile(4))
        self.assertEqual(audit["tables"], {
            table: {"row_format": "Compressed", "zip_page_size": 4096}
            for table in COMPACT_TABLES
        })
        query = server.execute.call_args.args[0]
        self.assertIn("FROM information_schema.INNODB_TABLESPACES", query)
        self.assertIn("WHERE NAME IN (", query)
        self.assertNotIn("SELECT *", query)
        self.assertNotIn("COUNT(", query)
        self.assertNotIn("private", query)

    def test_physical_audit_rejects_wrong_size_format_missing_and_duplicate_tablespaces(self):
        original = "\n".join(f"{table}\tCompressed\t4096" for table in sorted(COMPACT_TABLES))
        for output in (
            original.replace("4096", "8192", 1),
            original.replace("Compressed\t4096", "Dynamic\t0", 1),
            "\n".join(original.splitlines()[1:]),
            original + "\n" + original.splitlines()[0],
            original.replace("4096", "invalid", 1),
            "",
        ):
            with self.subTest(output=output):
                server = mock.Mock()
                server.execute.return_value = output
                with self.assertRaises(StorageLayoutError):
                    audit_storage_layout(server, database="fixture", password=None, page_kb=4)

    def test_page_size_configuration_is_strict_and_default_is_unchanged(self):
        self.assertEqual(compression_page_kb({}), 8)
        for size in (8, 4):
            self.assertEqual(compression_page_kb({ENV: str(size)}), size)
            self.assertEqual(layout_profile(size), f"innodb-compressed-{size}k-v1")
        for value in ("", "1", "2", "16", "0", "4.0", " 4", "4\n", "bad", 4, None):
            with self.subTest(value=value), self.assertRaises(StorageLayoutError):
                compression_page_kb({ENV: value})
        for value in (True, 4.0, "4", 1, 2, 16):
            with self.subTest(value=value), self.assertRaises(StorageLayoutError):
                compression_options(value)

    def test_build_sql_changes_only_compact_marker_page_size(self):
        with mock.patch.dict(os.environ, {ENV: "8"}):
            default = run_pipeline.split_build_sql(ROOT / "sql/01_build_ojs_tables.sql", True)
        original = default.prefix + default.metadata + default.suffix
        for size in (4,):
            with mock.patch.dict(os.environ, {ENV: str(size)}):
                parts = run_pipeline.split_build_sql(ROOT / "sql/01_build_ojs_tables.sql", True)
            dense = parts.prefix + parts.metadata + parts.suffix
            self.assertEqual(dense, original.replace("KEY_BLOCK_SIZE=8", f"KEY_BLOCK_SIZE={size}"))
        with mock.patch.dict(os.environ, {ENV: "4"}):
            explicit = run_pipeline.split_build_sql(
                ROOT / "sql/01_build_ojs_tables.sql", True, compressed_page_kb=8,
            )
        self.assertEqual(explicit, default)

    def test_raw_ddl_changes_storage_only_and_preserves_insert_bytes(self):
        data = (b"CREATE TABLE `records` (\n  `id` bigint NOT NULL\n"
                b") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
                b"INSERT INTO `records` VALUES ('ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8');\n")
        parser = run_pipeline.RawRecordsDDL(4)
        result = b"".join(parser.transform(line) for line in data.splitlines(keepends=True))
        parser.finish()
        self.assertEqual(result, data.replace(
            b") ENGINE=InnoDB", b") ENGINE=InnoDB ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4", 1,
        ))

    def test_clean_ddl_replaces_only_three_table_options(self):
        options = (b" DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
                   b" ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8")
        data = clean_dump(options=options)
        expected = data
        for table in COMPACT_TABLES:
            header = b"CREATE TABLE `" + table.encode() + b"` (\n  `id` bigint NOT NULL\n"
            old = header + b") ENGINE=InnoDB" + options + b";\n"
            expected = expected.replace(old, old.replace(b"KEY_BLOCK_SIZE=8", b"KEY_BLOCK_SIZE=4"))
        self.assertEqual(transform(data), expected)
        self.assertEqual(transform(transform(data), 8).count(b"KEY_BLOCK_SIZE=8"), 5)

    def test_clean_ddl_accepts_generated_option_order_and_uncompressed_history(self):
        for options in (
            b"", b" DEFAULT CHARSET=utf8mb4",
            b" ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4",
            b" AUTO_INCREMENT=123 DEFAULT CHARSET=utf8mb4 ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8",
            b" KEY_BLOCK_SIZE=8 ROW_FORMAT=COMPRESSED DEFAULT CHARSET=utf8mb4",
        ):
            with self.subTest(options=options):
                output = transform(clean_dump(options=options))
                self.assertEqual(output.count(b"KEY_BLOCK_SIZE=4"), 3)
                self.assertEqual(output.count(b"INSERT INTO"), 5)
                self.assertEqual(output.count(b"AUTO_INCREMENT=123"), 5 if b"AUTO_INCREMENT" in options else 0)
        self.assertEqual(transform(clean_dump().replace(b"\n", b"\r\n")).count(b"KEY_BLOCK_SIZE=4;\r\n"), 3)

    def test_extended_insert_with_embedded_ddl_is_never_rewritten(self):
        # Real newlines, escaped quotes, doubled quotes, semicolons, comments,
        # and a chunk-boundary escape must all remain inside their SQL value.
        payload = (b"'" + b"x" * 70000 + b"\\' quoted; '' doubled\n"
                   b"CREATE TABLE `ojs_articles` (\n"
                   b") ENGINE=InnoDB ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8;\n"
                   b"/* quoted */ -- quoted\n' ")
        data = clean_dump(payload=payload)
        output = transform(data)
        self.assertEqual(output.count(payload), 5)
        self.assertEqual(output.count(b"KEY_BLOCK_SIZE=4"), 3)

    def test_comments_with_ddl_text_do_not_create_tables(self):
        comment = (b"/* quoted-looking comment ' ;\nCREATE TABLE `ojs_articles` (\n"
                   b") ENGINE=InnoDB;\n*/\n")
        output = transform(comment + clean_dump() + b"-- trailing comment\n")
        self.assertTrue(output.startswith(comment))
        self.assertEqual(output.count(b"KEY_BLOCK_SIZE=4"), 3)
        for ending in (b"-- trailing comment", b"# trailing comment", b"-- trailing comment -"):
            self.assertTrue(transform(clean_dump() + ending).endswith(ending))

    def test_comment_and_quote_boundaries_across_input_fragments(self):
        from storage_layout import _SQLBoundary
        for content in (b"INSERT INTO t VALUES ('x\\\';\nCREATE TABLE');\n",
                        b"/* comment */ SELECT 'a''b'; -- comment\n",
                        b"# comment\nSELECT `a;b`;\n"):
            for width in (1, 2, 3, 7):
                with self.subTest(content=content, width=width):
                    state = _SQLBoundary()
                    for offset in range(0, len(content), width):
                        state.feed(content[offset:offset + width])
                    self.assertTrue(state.start)

    def test_unsupported_ddl_fails_closed(self):
        source = clean_dump()
        variants = (
            source.replace(b"CREATE TABLE `ojs_articles` (", b"CREATE TABLE `unknown` ("),
            source.replace(b"CREATE TABLE `ojs_articles` (", b"CREATE TABLE IF NOT EXISTS `ojs_articles` ("),
            source.replace(b"ENGINE=InnoDB", b"ENGINE=MyISAM", 1),
            source.replace(b"ENGINE=InnoDB", b"ENGINE=InnoDB COMPRESSION='zlib'", 1),
            source.replace(b"ENGINE=InnoDB", b"ENGINE=InnoDB KEY_BLOCK_SIZE=4 KEY_BLOCK_SIZE=8", 1),
            source.replace(b"ENGINE=InnoDB", b"ENGINE=InnoDB KEY_BLOCK_SIZE=32", 1),
            source + source,
            source[:source.index(b"CREATE TABLE")],
            source + b"SELECT 'unterminated",
            source + b"/* unterminated",
            source.replace(b"  `id` bigint NOT NULL", b" " * (256 * 1024), 1),
        )
        for index, data in enumerate(variants):
            with self.subTest(variant=index), self.assertRaises(StorageLayoutError):
                transform(data)

    def test_stream_import_digest_is_for_original_bytes(self):
        class QuietProgress:
            def update(self, **kwargs):
                pass

        output = io.BytesIO()
        server = object.__new__(run_pipeline.MySQLServer)
        server.compressed_page_kb = 4
        server._run_mysql = lambda feed, **kwargs: feed(output, QuietProgress())
        data = clean_dump()
        digest = server._stream_into_mysql(
            io.BytesIO(data), label="fixture", total=len(data), database="fixture",
            password=None, progress_seconds=60, calculate_sha256=True, compact_clean_export=True,
        )
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(output.getvalue(), transform(data, 4))

    def test_default_export_import_is_byte_identical_without_ddl_requirements(self):
        for size, compact in ((8, True), (4, False)):
            with self.subTest(size=size, compact=compact), TemporaryDirectory() as directory:
                path = Path(directory) / "pkpbeacon-clean-2026-01-01.sql.gz"
                path.write_bytes(gzip.compress(b"SELECT 'unchanged';\n"))
                server = object.__new__(run_pipeline.MySQLServer)
                server.compressed_page_kb = size
                server.compact_storage = compact
                server._stream_into_mysql = mock.Mock()
                with mock.patch.object(run_pipeline, "verify_checksum_sidecar"):
                    server.import_clean_export(run_pipeline.CleanExport(path, "2026-01-01"),
                                               database="fixture", password=None, progress_seconds=60)
                self.assertFalse(server._stream_into_mysql.call_args.kwargs["compact_clean_export"])

    def test_default_profile_rewrites_a_dense_predecessor_back_to_8k(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pkpbeacon-clean-2026-01-01.sql.gz"
            path.write_bytes(gzip.compress(clean_dump()))
            run_pipeline.change_report_path(path.parent, "2026-01-01").write_text(json.dumps({
                "storage": {"storage_layout_profile": layout_profile(4)},
            }))
            server = object.__new__(run_pipeline.MySQLServer)
            server.compressed_page_kb = 8
            server.compact_storage = True
            server._stream_into_mysql = mock.Mock()
            with mock.patch.object(run_pipeline, "verify_checksum_sidecar"):
                server.import_clean_export(run_pipeline.CleanExport(path, "2026-01-01"),
                                           database="fixture", password=None, progress_seconds=60)
            self.assertTrue(server._stream_into_mysql.call_args.kwargs["compact_clean_export"])

    def test_profile_mismatch_cannot_invalidate_existing_staging_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = run_pipeline.Snapshot(root / "pkpbeacon-2026-01-01.sql", "2026-01-01",
                                             datetime(2026, 1, 1), 123)
            marker = root / "PIPELINE_FAILED.txt"
            marker.write_bytes(b"retain original failure evidence")
            for recorded, requested in ((None, layout_profile(4)), ("innodb-compressed-2k-v1", layout_profile(4)),
                                        (layout_profile(4), None)):
                run_pipeline.write_build_state(
                    root, snapshot=snapshot, source_sha256="a" * 64, build_sql_sha256="b" * 64,
                    phase="metadata_ready", storage_layout_profile=recorded,
                )
                original = run_pipeline.build_state_path(root).read_bytes()
                with self.subTest(recorded=recorded, requested=requested):
                    with self.assertRaisesRegex(run_pipeline.PipelineError, "existing files retained"):
                        run_pipeline.validate_staging_layout(root, requested)
                    self.assertEqual(run_pipeline.build_state_path(root).read_bytes(), original)
                    self.assertEqual(marker.read_bytes(), b"retain original failure evidence")

    def test_resume_checkpoint_binds_physical_layout_in_both_directions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = run_pipeline.Snapshot(root / "pkpbeacon-2026-01-01.sql", "2026-01-01",
                                             datetime(2026, 1, 1), 123)
            server = mock.Mock()
            server.execute.return_value = "0"
            for recorded in (None, layout_profile(4), "innodb-compressed-2k-v1"):
                run_pipeline.write_build_state(
                    root, snapshot=snapshot, source_sha256="a" * 64, build_sql_sha256="b" * 64,
                    phase="metadata_ready", storage_layout_profile=recorded,
                )
                for requested in (None, layout_profile(4)):
                    kwargs = dict(staging_dir=root, snapshot=snapshot, database="fixture", password=None,
                                  build_sql_sha256="b" * 64, storage_layout_profile=requested)
                    with self.subTest(recorded=recorded, requested=requested):
                        if requested == recorded:
                            self.assertEqual(run_pipeline.validate_resume_state(server, **kwargs), "a" * 64)
                        else:
                            with self.assertRaisesRegex(run_pipeline.PipelineError, "different physical storage"):
                                run_pipeline.validate_resume_state(server, **kwargs)
            run_pipeline.build_state_path(root).unlink()
            with self.assertRaisesRegex(run_pipeline.PipelineError, "matching staging checkpoint"):
                run_pipeline.validate_resume_state(server, **kwargs)


if __name__ == "__main__":
    unittest.main()
