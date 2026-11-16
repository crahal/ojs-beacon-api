from __future__ import annotations

import copy
import hashlib
import re
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import metadata_storage as storage
from compact_storage import render_compact_sql


class SchemaServer:
    def __init__(self, datadir: Path):
        self.datadir = datadir
        self.scratch_root = None
        self.calls = []
        self.alters = []
        self.isolation = "0\t0"
        self.fail_table = None
        self.extra_columns = []
        self.tables = {
            table: {"kind": "BASE TABLE", "engine": "InnoDB",
                    "collation": storage._COLLATION, "data": 1000, "indexes": 100,
                    "columns": {field: ["text", "YES", "utf8mb4", storage._COLLATION, "1", "", ""]
                                for field in fields}}
            for table, fields in storage._TABLE_COLUMNS.items()
        }
        del self.tables["ojs_stage_canonical_payload"]

    def execute(self, sql, *, database, password, label):
        self.calls.append((label, sql))
        if label == "metadata-storage-isolation":
            return self.isolation
        if label == "metadata-storage-tables":
            return "\n".join("\t".join(map(str, [name, item["kind"], item["engine"],
                            item["collation"], item["data"], item["indexes"]]))
                             for name, item in self.tables.items())
        if label == "metadata-storage-columns":
            rows = ["\t".join([name, field, *schema])
                    for name, item in self.tables.items() for field, schema in item["columns"].items()]
            return "\n".join(rows + self.extra_columns).strip()
        raise AssertionError(f"unexpected query {label}")

    def run_sql_text(self, sql, **arguments):
        table = re.search(r"ALTER TABLE `([^`]+)`", sql)[1]
        self.alters.append((sql, arguments))
        if table == self.fail_table:
            raise RuntimeError("simulated interrupted atomic DDL")
        for field in re.findall(r"MODIFY COLUMN `([^`]+)`", sql):
            self.tables[table]["columns"][field][0] = "mediumtext"


class MetadataStorageRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (PROJECT_ROOT / "sql" / "01_build_ojs_tables.sql").read_text(encoding="utf-8")

    def test_only_declared_storage_width_changes_in_normal_and_compact_programs(self):
        for original in (self.sql, render_compact_sql(self.sql)):
            with self.subTest(compact=original != self.sql):
                expected = original
                for field in set(field for fields in storage._TABLE_COLUMNS.values() for field in fields):
                    expected = expected.replace(f"    {field} TEXT NULL,", f"    {field} MEDIUMTEXT NULL,")
                result = storage.render_metadata_storage_sql(original)
                self.assertEqual(result, expected)
                self.assertNotEqual(result, original)
                self.assertEqual(result, storage.render_metadata_storage_sql(result))
                self.assertEqual(result.count("fingerprint_key_value VARCHAR(2048) NULL,"), 1)
                self.assertEqual(result.count("UNHEX(SHA2(normalized.metadata_xml, 256))"), 1)

    def test_all_expected_declarations_are_present_and_unbounded(self):
        result = storage.render_metadata_storage_sql(self.sql)
        for table, fields in storage._TABLE_COLUMNS.items():
            schema = re.search(rf"CREATE TABLE(?: IF NOT EXISTS)? {table} \(\n(.*?)\n\) ENGINE", result, re.S)[1]
            for field in fields:
                self.assertIn(f"    {field} MEDIUMTEXT NULL,", schema)
            self.assertNotRegex(schema, r"\sTEXT\s")

    def test_refuses_changed_declaration_missing_schema_and_unreviewed_text_field(self):
        for changed in (
            self.sql.replace("    title TEXT NULL,", "    title VARCHAR(2048) NULL,", 1),
            self.sql.replace("    title TEXT NULL,", "    title TEXT NOT NULL,", 1),
            self.sql.replace("    title TEXT NULL,", "    title TEXT DEFAULT 'x',", 1),
            self.sql.replace("    title TEXT NULL,", "    title TEXT NULL,\n    added TEXT NULL,", 1),
            self.sql.replace("CREATE TABLE IF NOT EXISTS ojs_articles", "CREATE TABLE IF NOT EXISTS changed", 1),
            self.sql + self.sql,
            1,
        ):
            with self.subTest(changed=str(changed)[:60]):
                with self.assertRaises(storage.MetadataStorageError):
                    storage.render_metadata_storage_sql(changed)

    def test_original_build_file_remains_unchanged(self):
        path = PROJECT_ROOT / "sql" / "01_build_ojs_tables.sql"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        storage.render_metadata_storage_sql(render_compact_sql(self.sql))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


class MetadataStorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="ojs-metadata-schema-test-")
        self.addCleanup(self.temporary.cleanup)
        self.server = SchemaServer(Path(self.temporary.name))
        patcher = mock.patch.object(storage.shutil, "disk_usage", return_value=types.SimpleNamespace(free=512 * 1024**3))
        self.disk = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(storage, "Progress")
        self.progress = patcher.start()
        self.addCleanup(patcher.stop)

    def estimate(self, **kwargs):
        return storage.estimate_metadata_storage_migration(
            self.server, database="fixture", password=None, reserve_bytes=40 * 1024**3, **kwargs,
        )

    def migrate(self, **kwargs):
        return storage.migrate_metadata_storage(
            self.server, database="fixture", password=None, reserve_bytes=40 * 1024**3, **kwargs,
        )

    def test_read_only_estimate_has_copy_budget_reserve_and_before_after_schema(self):
        plan = self.estimate()
        self.assertTrue(plan["capacity_ok"])
        self.assertEqual(plan["profile"], storage.METADATA_STORAGE_PROFILE)
        self.assertEqual(plan["copy_budget_factor"], 1.5)
        self.assertEqual([item["table"] for item in plan["tables"]],
                         ["ojs_stage_sources", "ojs_article_sources", "ojs_articles"])
        self.assertEqual(self.server.alters, [])
        self.assertTrue(all(sql.strip().startswith("SELECT") for _, sql in self.server.calls))
        self.assertEqual(plan["tables"][0]["copy_budget_bytes"], 64 * 1024**2)
        self.assertEqual(plan["tables"][0]["columns_before"]["title"], "text")
        self.assertEqual(plan["tables"][0]["columns_after"]["title"], "mediumtext")

    def test_combines_columns_in_one_explicit_lossless_copy_per_table_with_progress(self):
        report = self.migrate()
        self.assertTrue(report["completed"])
        self.assertEqual(report["changed_table_count"], 3)
        self.assertRegex(report["implementation_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(len(self.server.alters), 3)
        for sql, arguments in self.server.alters:
            self.assertIn("ALGORITHM=COPY, LOCK=EXCLUSIVE", sql)
            self.assertNotIn("TRUNCATE", sql)
            self.assertNotIn("DROP", sql)
            self.assertNotIn("LEFT(", sql)
            self.assertIn("STRICT_ALL_TABLES", arguments["prelude"])
            self.assertEqual(arguments["label"], "metadata-storage-table-copy")
            self.assertEqual(sql.count("ALTER TABLE"), 1)
        self.assertEqual(self.progress.return_value.__enter__.return_value.event.call_count, 6)

    def test_already_widened_schema_is_noop(self):
        self.migrate()
        self.server.alters.clear()
        report = self.migrate()
        self.assertEqual(report["changed_table_count"], 0)
        self.assertEqual(self.server.alters, [])
        self.assertTrue(all(not row["changed_columns"] for row in report["tables"]))

    def test_retry_after_partial_atomic_table_migration_keeps_completed_tables(self):
        self.server.fail_table = "ojs_article_sources"
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.migrate()
        self.assertTrue(all(value[0] == "mediumtext" for value in self.server.tables["ojs_stage_sources"]["columns"].values()))
        self.server.fail_table = None
        self.server.alters.clear()
        report = self.migrate()
        self.assertEqual(report["changed_table_count"], 2)
        self.assertNotIn("ojs_stage_sources", " ".join(sql for sql, _ in self.server.alters))

    def test_mixed_old_and_new_columns_only_alter_remaining_old_fields(self):
        self.server.tables["ojs_article_sources"]["columns"]["title"][0] = "mediumtext"
        self.migrate()
        sql = next(sql for sql, args in self.server.alters if args["table"] == "ojs_article_sources")
        self.assertNotIn("MODIFY COLUMN `title`", sql)
        self.assertIn("MODIFY COLUMN `first_creator`", sql)

    def test_all_tables_are_validated_before_any_alter(self):
        for index, invalid in ((0, "tinytext"), (0, "longtext"), (0, "varchar(255)"),
                               (1, "NO"), (2, "latin1"), (3, "utf8mb4_bin"),
                               (4, "0"), (5, "INVISIBLE"), (6, "title")):
            with self.subTest(index=index, invalid=invalid):
                original = copy.deepcopy(self.server.tables)
                self.server.tables["ojs_articles"]["columns"]["title"][index] = invalid
                with self.assertRaises(storage.MetadataStorageError):
                    self.migrate()
                self.assertEqual(self.server.alters, [])
                self.server.tables = original

    def test_rejects_wrong_engine_view_table_collation_and_missing_columns(self):
        original = copy.deepcopy(self.server.tables)
        for field, value in (("engine", "MyISAM"), ("kind", "VIEW"), ("collation", "latin1_swedish_ci")):
            with self.subTest(field=field):
                self.server.tables["ojs_articles"][field] = value
                with self.assertRaises(storage.MetadataStorageError):
                    self.migrate()
                self.server.tables = copy.deepcopy(original)
        del self.server.tables["ojs_articles"]["columns"]["title"]
        with self.assertRaises(storage.MetadataStorageError):
            self.migrate()
        self.assertEqual(self.server.alters, [])

    def test_refuses_unreviewed_text_column(self):
        self.server.extra_columns = ["ojs_articles\tnew_column\ttext\tYES\tutf8mb4\tutf8mb4_0900_ai_ci\t1\t\t"]
        with self.assertRaisesRegex(storage.MetadataStorageError, "unreviewed"):
            self.migrate()

    def test_missing_required_table_refused_but_history_only_required_set_supported(self):
        del self.server.tables["ojs_stage_sources"]
        with self.assertRaisesRegex(storage.MetadataStorageError, "required tables"):
            self.migrate()
        self.assertEqual(self.server.alters, [])
        report = self.migrate(required_tables=("ojs_articles", "ojs_article_sources"))
        self.assertEqual(report["changed_table_count"], 2)

    def test_insufficient_capacity_refused_before_any_ddl(self):
        self.disk.return_value.free = 40 * 1024**3
        self.assertFalse(self.estimate()["capacity_ok"])
        with self.assertRaisesRegex(storage.MetadataStorageError, "headroom"):
            self.migrate()
        self.assertEqual(self.server.alters, [])

    def test_space_is_checked_again_before_each_table(self):
        initial = storage._space
        calls = 0

        def falling_space(server, copy_bytes, reserve_bytes):
            nonlocal calls
            calls += 1
            result = initial(server, copy_bytes, reserve_bytes)
            if calls > 4:
                result["capacity_ok"] = False
            return result

        with mock.patch.object(storage, "_space", side_effect=falling_space):
            with self.assertRaisesRegex(storage.MetadataStorageError, "fell below"):
                self.migrate()
        self.assertEqual(len(self.server.alters), 1)

    def test_uses_larger_physical_or_reported_table_size_without_recursive_scan(self):
        table_dir = self.server.datadir / "fixture"
        table_dir.mkdir()
        with (table_dir / "ojs_articles.ibd").open("wb") as output:
            output.truncate(100 * 1024**2)
        result = self.estimate()
        article = next(row for row in result["tables"] if row["table"] == "ojs_articles")
        self.assertEqual(article["table_bytes_estimate"], 100 * 1024**2)
        self.assertEqual(article["copy_budget_bytes"], 150 * 1024**2)
        self.server.tables["ojs_articles"]["data"] = 200 * 1024**2
        self.server.tables["ojs_articles"]["indexes"] = 0
        article = next(row for row in self.estimate()["tables"] if row["table"] == "ojs_articles")
        self.assertEqual(article["table_bytes_estimate"], 200 * 1024**2)

    def test_symlinked_table_file_is_refused(self):
        table_dir = self.server.datadir / "fixture"
        table_dir.mkdir()
        (table_dir / "ojs_articles.ibd").symlink_to(table_dir / "absent.ibd")
        with self.assertRaisesRegex(storage.MetadataStorageError, "symlink"):
            self.estimate()

    def test_other_clients_or_transactions_block_migration_not_read_only_plan(self):
        for result in ("1\t0", "0\t1", "nonsense"):
            self.server.isolation = result
            self.assertTrue(self.estimate()["capacity_ok"])
            with self.assertRaisesRegex(storage.MetadataStorageError, "client or active transaction"):
                self.migrate()
            self.assertEqual(self.server.alters, [])

    def test_rejects_invalid_parameters_before_any_query(self):
        for parameters in ({"database": "fixture;bad"}, {"reserve_bytes": -1},
                           {"reserve_bytes": True}, {"required_tables": ("unknown",)},
                           {"required_tables": ("ojs_articles", "ojs_articles")}):
            options = {"database": "fixture", "password": None, "reserve_bytes": 0, **parameters}
            with self.assertRaises(storage.MetadataStorageError):
                storage.estimate_metadata_storage_migration(self.server, **options)
            self.assertEqual(self.server.calls, [])


if __name__ == "__main__":
    unittest.main()
