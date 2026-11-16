"""Fail-closed retained-key proofs and real-MySQL bootstrap equivalence."""
from __future__ import annotations

import copy
import io
from pathlib import Path
import re
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import bootstrap_recovery as bootstrap
import recovery_checks
import run_pipeline
from test_recovery_checks import AuditServer
import test_pipeline as fixture
from test_compact_storage_equivalence import key_scenario_rows, logical_rows


BUILD_SQL = Path(__file__).parents[1] / "sql" / "01_build_ojs_tables.sql"


class KeyAuditServer(AuditServer):
    def __init__(self):
        super().__init__()
        self.tables.update({name: ["BASE TABLE", "InnoDB"] for name in (
            "ojs_stage_effective_keys", "ojs_stage_noisy_keys", "ojs_stage_touched_keys",
        )})
        enum = "enum('doi','fingerprint','oai','url')"
        schemas = {
            "ojs_stage_keys": [("source_record_id", "bigint unsigned", "NO"),
                               ("key_type", enum, "NO"), ("key_hash", "binary(32)", "NO")],
            "ojs_stage_effective_keys": [("source_record_id", "bigint unsigned", "NO"),
                ("key_type", enum, "NO"), ("key_hash", "binary(32)", "NO"),
                ("article_id", "bigint unsigned", "YES"), ("is_present", "tinyint(1)", "NO")],
            "ojs_stage_noisy_keys": [("key_type", enum, "NO"), ("key_hash", "binary(32)", "NO")],
        }
        self.answers.update({
            "recovery-key-columns": "\n".join("\t".join((table, *column))
                for table, columns in schemas.items() for column in columns),
            "recovery-trigger-inventory": "0", "recovery-noisy-count": "0",
            "recovery-noisy-range": "0\t0", "recovery-retained-key-count": "64",
        })
        indexes = []
        for table in schemas:
            primary = ("key_type", "key_hash") if table.endswith("noisy_keys") else ("source_record_id", "key_type")
            indexes += [f"{table}\tPRIMARY\t0\t{position}\t{column}\t0"
                        for position, column in enumerate(primary, 1)]
        indexes += [f"ojs_stage_effective_keys\tidx_ojs_effective_keys_lookup\t1\t{position}\t{column}\t0"
                    for position, column in enumerate(("key_type", "key_hash", "article_id", "source_record_id"), 1)]
        self.answers["recovery-key-indexes"] = "\n".join(indexes)

    def execute(self, sql, *, database, password, label):
        if label not in self.answers and label in ("recovery-expected-key-range", "recovery-retained-key-range"):
            self.calls.append((label, sql))
            lower, upper = map(int, re.search(r"BETWEEN (\d+) AND (\d+)", sql).groups())
            count = upper - lower + 1
            return str(count) if label == "recovery-expected-key-range" else f"{count}\t0\t0\t0\t0\t0"
        return super().execute(sql, database=database, password=password, label=label)


class RetainedKeyAuditTests(unittest.TestCase):
    def setUp(self):
        self.server = KeyAuditServer()
        self.checkpoint = {
            "pipeline_version": run_pipeline.PIPELINE_VERSION, "snapshot_version": "2026-01-01",
            "phase": "finalizing", "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64,
            "source_size_bytes": 123, "source_filename": "pkpbeacon-2026-01-01.sql.gz",
        }
        self.arguments = dict(database="fixture", password=None, snapshot_version="2026-01-01",
            source_sha256="a" * 64, build_sql_sha256="b" * 64,
            checkpoint=self.checkpoint, expected_checkpoint=copy.deepcopy(self.checkpoint))
        for module in (recovery_checks, bootstrap):
            patcher = mock.patch.object(module, "Progress")
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate(self):
        return bootstrap.validate_bootstrap_touched_keys(self.server, **self.arguments)

    def test_full_metadata_audit_is_mandatory_and_every_query_is_read_only(self):
        result = self.validate()
        self.assertEqual(result["recovery_boundary"], "bootstrap_touched_keys")
        self.assertEqual(result["original_build_sql_sha256"], "b" * 64)
        self.assertEqual(result["effective_key_rows"], 64)
        self.assertEqual(result["already_removed_noisy_memberships"], 0)
        self.assertEqual(sum(label == "recovery-metadata-range" for label, _ in self.server.calls), 32)
        self.assertEqual(sum(label == "recovery-source-coverage" for label, _ in self.server.calls), 32)
        self.assertEqual(sum(label == "recovery-noisy-range" for label, _ in self.server.calls), 64)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for _, sql in self.server.calls))
        for label, sql in self.server.calls:
            if label == "recovery-retained-key-range":
                self.assertIn("effective FORCE INDEX (PRIMARY)", sql)
            if label == "recovery-noisy-range":
                self.assertIn("FORCE INDEX (idx_ojs_effective_keys_lookup)", sql)
                self.assertNotIn("is_present", sql)
                self.assertIn("key_hash >= UNHEX(", sql)

    def test_touched_table_may_be_absent_but_completed_key_tables_are_required(self):
        del self.server.tables["ojs_stage_touched_keys"]
        self.validate()
        for table in ("ojs_stage_keys", "ojs_stage_effective_keys", "ojs_stage_noisy_keys"):
            with self.subTest(table=table):
                self.server = KeyAuditServer()
                del self.server.tables[table]
                with self.assertRaisesRegex(run_pipeline.PipelineError, "missing"):
                    self.validate()

    def test_original_exact_key_api_still_refuses_later_tables(self):
        with self.assertRaisesRegex(run_pipeline.PipelineError, "later finalization"):
            recovery_checks.validate_bootstrap_exact_keys(self.server, **self.arguments)

    def test_rejects_bad_provenance_and_later_work_before_key_reads(self):
        self.arguments["checkpoint"] = {**self.checkpoint, "build_sql_sha256": "c" * 64}
        with self.assertRaisesRegex(run_pipeline.PipelineError, "checkpoint"):
            self.validate()
        self.assertEqual(self.server.calls, [])
        self.arguments["checkpoint"] = self.checkpoint
        for table in ("ojs_stage_existing_candidates", "ojs_stage_assignments", "ojs_pipeline_raw_stats"):
            with self.subTest(table=table):
                self.server = KeyAuditServer()
                self.server.tables[table] = ["BASE TABLE", "InnoDB"]
                with self.assertRaisesRegex(run_pipeline.PipelineError, "later finalization"):
                    self.validate()
                self.assertFalse(any(label == "recovery-key-columns" for label, _ in self.server.calls))

    def test_rejects_unproved_metadata_and_bootstrap_state(self):
        for label, answer in (("recovery-clean-empty", "0\t0\t1\t0\t0"),
                              ("recovery-metadata-range", "2\t0\t0\t0\t0\t1\t0"),
                              ("recovery-other-clients", "1"),
                              ("recovery-routine-inventory", "1")):
            with self.subTest(label=label):
                self.server = KeyAuditServer()
                self.server.answers[label] = answer
                with self.assertRaises(run_pipeline.PipelineError):
                    self.validate()

    def test_rejects_missing_wrong_extra_or_nonbootstrap_key_memberships(self):
        for result in ("2\t1\t0\t0\t0\t0", "2\t0\t1\t0\t0\t0",
                       "2\t0\t0\t1\t0\t1", "2\t0\t0\t0\t1\t0",
                       "1\t0\t0\t0\t0\t0", "3\t0\t0\t0\t0\t0"):
            with self.subTest(result=result):
                self.server = KeyAuditServer()
                self.server.answers["recovery-retained-key-range"] = result
                with self.assertRaisesRegex(run_pipeline.PipelineError, "retained key range"):
                    self.validate()
        for counts in (["65", "64"], ["64", "65"]):
            self.server = KeyAuditServer()
            self.server.answers["recovery-retained-key-count"] = counts
            with self.assertRaises(run_pipeline.PipelineError):
                self.validate()

    def test_noisy_memberships_can_have_been_removed_but_noise_must_be_proved(self):
        self.server.answers["recovery-retained-key-count"] = ["63", "64"]
        self.server.answers["recovery-retained-key-range"] = ["2\t0\t0\t0\t0\t1"] + ["2\t0\t0\t0\t0\t0"] * 31
        self.server.answers["recovery-noisy-count"] = "1"
        self.server.answers["recovery-noisy-range"] = ["1\t0"] + ["0\t0"] * 63
        self.assertEqual(self.validate()["already_removed_noisy_memberships"], 1)
        for actual, expected in (("0", "1\t1"), ("1", "0\t0")):
            self.server = KeyAuditServer()
            self.server.answers["recovery-noisy-count"] = actual
            self.server.answers["recovery-noisy-range"] = expected
            with self.assertRaisesRegex(run_pipeline.PipelineError, "noisy keys"):
                self.validate()

    def test_rejects_schema_changes_prefix_indexes_and_triggers(self):
        mutations = (
            ("recovery-key-columns", lambda s: s.replace("binary(32)", "binary(16)")),
            ("recovery-key-indexes", lambda s: s.replace("\tPRIMARY\t0", "\tPRIMARY\t1")),
            ("recovery-key-indexes", lambda s: s.replace("idx_ojs_effective_keys_lookup", "other_lookup")),
            ("recovery-trigger-inventory", lambda s: "1"),
        )
        for label, mutate in mutations:
            self.server = KeyAuditServer()
            self.server.answers[label] = mutate(self.server.answers[label])
            with self.assertRaises(run_pipeline.PipelineError):
                self.validate()


class BootstrapRenderTests(unittest.TestCase):
    def test_render_preserves_original_provenance_and_skips_completed_work(self):
        original_hash = run_pipeline.sha256_small_file(BUILD_SQL)
        suffix = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True).suffix
        rendered = bootstrap.render_bootstrap_touched_recovery(suffix)
        self.assertTrue(rendered.startswith("SELECT 'OJS_PROGRESS_V1', 'stage', 'touched_keys';"))
        for statement in ("INSERT INTO ojs_stage_touched_keys", "INSERT INTO ojs_stage_existing_candidates",
                          "CREATE TABLE ojs_stage_keys", "CREATE TABLE ojs_stage_effective_keys"):
            self.assertNotIn(statement, rendered)
        self.assertIn("DELETE k\nFROM ojs_stage_keys", rendered)
        self.assertIn("HAVING COUNT(DISTINCT effective.article_id) = 1", rendered)
        self.assertIn("MIN(effective.article_id), @ojs_snapshot_date", rendered)
        self.assertEqual(original_hash, run_pipeline.sha256_small_file(BUILD_SQL))
        self.assertNotEqual(bootstrap.recovery_sql_sha256(rendered), original_hash)

    def test_unreviewed_sql_changes_and_standard_representation_are_refused(self):
        suffix = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True).suffix
        for changed in (suffix.replace("COUNT(*) > 25", "COUNT(*) > 26"),
                        suffix.replace("MIN(effective.article_id)", "MAX(effective.article_id)"),
                        run_pipeline.split_build_sql(BUILD_SQL, compact_storage=False).suffix,
                        suffix + "SELECT 1;", None):
            with self.assertRaises(run_pipeline.PipelineError):
                bootstrap.render_bootstrap_touched_recovery(changed)


@unittest.skipUnless(all(shutil.which(name) for name in ("mysqld", "mysql", "mysqladmin", "mysqldump")),
                     "MySQL server and clients required")
class BootstrapRecoveryMySQLTests(unittest.TestCase):
    def test_touched_interruptions_match_original_sql_with_noisy_colliding_and_null_keys(self):
        with TemporaryDirectory(prefix="ojs-touched-recovery-") as directory, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            tools = run_pipeline.MySQLTools.discover()
            server = run_pipeline.MySQLServer(tools, root / "mysql", "128M", compact_storage=True)
            rows = fixture.fixture_rows() + key_scenario_rows(1)
            rows.append(fixture.record_row(record_id=300, context_id=1, identifier=1300,
                metadata_xml=fixture.metadata(oai_identifier="", title="", creator="", published="", url=""),
                modified="2026-01-01 00:00:00"))
            path = root / "pkpbeacon-2026-01-01.sql"
            path.write_text(fixture.dump("2026-01-01", rows), encoding="utf-8")
            snapshot = run_pipeline.inspect_snapshot(path)
            parts = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True)
            build_hash = run_pipeline.sha256_small_file(BUILD_SQL)
            original_before_touched, touched_onward = parts.suffix.split(bootstrap._anchor("touched_keys"), 1)
            touched_only = bootstrap._anchor("touched_keys") + touched_onward.split(bootstrap._anchor("existing_candidates"), 1)[0]
            expected_tables = None
            expected_counts = None
            try:
                server.initialize()
                server.start(None)
                for mode in ("baseline", "touched_absent", "touched_partial", "after_noisy_delete"):
                    with self.subTest(mode=mode):
                        database = "fixture_" + mode
                        run_pipeline.create_database(server, database)
                        source_hash = server.import_snapshot(snapshot, database=database, password=None, progress_seconds=60)
                        prelude = run_pipeline.build_sql_prelude(snapshot=snapshot, source_sha256=source_hash,
                            build_sql_sha256=build_hash, mysql_version=tools.version, full_rescan=False)
                        for label, sql in (("fixture-prefix", parts.prefix), ("fixture-metadata", parts.metadata)):
                            server.run_sql_text(sql, label=label, database=database, password=None, prelude=prelude)
                        if mode == "baseline":
                            server.run_sql_text(parts.suffix, label="fixture-baseline", database=database, password=None, prelude=prelude)
                        else:
                            server.run_sql_text(original_before_touched, label="fixture-key-prefix", database=database,
                                                password=None, prelude=prelude)
                            self.assertGreater(int(server.execute("SELECT COUNT(*) FROM ojs_stage_noisy_keys;",
                                                                  database=database)), 0)
                            preserved = {table: logical_rows(server, database, table) for table in (
                                "records", "ojs_stage_source_index", "ojs_stage_sources", "ojs_stage_effective_keys",
                            )}
                            if mode == "touched_partial":
                                ddl = touched_only.split("INSERT INTO ojs_stage_touched_keys\n", 1)[0]
                                server.run_sql_text(ddl, label="fixture-touched-ddl", database=database, password=None)
                                server.execute("INSERT INTO ojs_stage_touched_keys SELECT key_type,key_hash,'discard me',"
                                    "NULL,NULL FROM ojs_stage_effective_keys LIMIT 1;", database=database)
                            elif mode == "after_noisy_delete":
                                server.run_sql_text(touched_only, label="fixture-touched-complete", database=database,
                                                    password=None, prelude=prelude)
                            checkpoint = dict(pipeline_version=run_pipeline.PIPELINE_VERSION,
                                snapshot_version=snapshot.version, phase="finalizing", source_sha256=source_hash,
                                build_sql_sha256=build_hash, source_size_bytes=snapshot.size, source_filename=path.name)
                            audit = bootstrap.validate_bootstrap_touched_keys(server, database=database, password=None,
                                snapshot_version=snapshot.version, source_sha256=source_hash, build_sql_sha256=build_hash,
                                checkpoint=checkpoint, expected_checkpoint=checkpoint.copy(), range_count=4, progress_seconds=60)
                            self.assertEqual(audit["original_build_sql_sha256"], build_hash)
                            self.assertGreater(audit["noisy_key_groups"], 0)
                            self.assertEqual(audit["already_removed_noisy_memberships"] > 0, mode == "after_noisy_delete")
                            for table, before in preserved.items():
                                self.assertEqual(logical_rows(server, database, table), before, table)
                            sql = bootstrap.render_bootstrap_touched_recovery(parts.suffix)
                            server.run_sql_text(sql, label="fixture-recovery", database=database, password=None, prelude=prelude)
                        counts, _ = run_pipeline.validate_database(server, database, None)
                        tables = {table: logical_rows(server, database, table) for table in run_pipeline.CLEAN_EXPORT_TABLES}
                        if expected_tables is None:
                            expected_tables, expected_counts = tables, counts
                        else:
                            self.assertEqual(counts, expected_counts)
                            self.assertEqual(tables, expected_tables)
            finally:
                server.shutdown(None)


if __name__ == "__main__":
    unittest.main()
