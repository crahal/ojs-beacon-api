"""Exercise the touched-recovery CLI against isolated MySQL databases."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import bootstrap_recovery
import run_pipeline
import test_pipeline as fixture
import test_pipeline_safety as safety
from test_compact_storage_equivalence import key_scenario_rows, logical_rows


BUILD_SQL = Path(__file__).parents[1] / "sql" / "01_build_ojs_tables.sql"
MYSQL_AVAILABLE = all(shutil.which(name) for name in ("mysqld", "mysql", "mysqladmin", "mysqldump"))


class TouchedRecoveryPreflightTests(safety.ExactKeysRecoveryFilesystemTests):
    """Apply every existing preservation/preflight contract to the new flag."""

    def recovery_fixture(self, root):
        staging, options = super().recovery_fixture(root)
        options.update(recover_exact_keys=False, recover_touched_keys=True)
        return staging, options

    def test_cli_rejects_conflicting_or_unsafe_recovery_flags_before_startup(self):
        base = ["--recover-touched-keys", "--snapshot", "/tmp/pkpbeacon-2026-01-01.sql",
                "--skip-download", "--resume-building", "--compact-storage"]
        with redirect_stderr(io.StringIO()), mock.patch.object(run_pipeline, "MySQLServer",
                side_effect=AssertionError("must not start a database")):
            for additional in (["--recover-exact-keys"], ["--force-rebuild"], ["--rebuild-history"],
                               ["--verify-existing"], ["--verify-source-checksum"], ["--download-only"]):
                with self.subTest(flags=additional):
                    self.assertEqual(run_pipeline.main(base + additional), 2)


@unittest.skipUnless(MYSQL_AVAILABLE, "MySQL server and clients required")
class TouchedRecoveryPipelineTests(unittest.TestCase):
    def make_interrupted_build(self, root: Path, *, staging_password=None):
        raw = root / "raw"
        raw.mkdir()
        path = raw / "pkpbeacon-2026-01-01.sql"
        path.write_text(fixture.dump("2026-01-01", fixture.fixture_rows() + key_scenario_rows(1)), encoding="utf-8")
        snapshot = run_pipeline.inspect_snapshot(path)
        clean = root / "clean"
        options = dict(
            snapshot=snapshot, clean_dir=clean, build_sql=BUILD_SQL, database="pkpbeacon_db",
            root_password="fixture-initial-not-yet-installed", api_db_user="fixture_api",
            api_db_password="fixture-initial-api", release_thresholds=run_pipeline.ReleaseThresholds(.1, .1, .1, .1),
            allow_anomalous_release=False, buffer_pool_size="128M", progress_seconds=60,
            force_rebuild=False, verify_existing=False, verify_source_checksum=False,
            full_rescan=False, publish_current=False, metadata_workers=1,
            resume_building=False, compact_storage=True,
        )
        original_run = run_pipeline.MySQLServer.run_sql_text
        original_write_state = run_pipeline.write_build_state
        suffix = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True).suffix
        before_touched = suffix.split("INSERT INTO ojs_stage_touched_keys\n", 1)[0]
        preserved = {}
        inodes = {}
        tables = ("records", "ojs_stage_source_index", "ojs_stage_sources",
                  "ojs_stage_keys", "ojs_stage_effective_keys", "ojs_stage_noisy_keys")

        def interrupt_after_completed_keys(server, sql, **kwargs):
            if kwargs["label"] != "temporal finalization phase":
                return original_run(server, sql, **kwargs)
            original_run(server, before_touched, **kwargs)
            server.execute("INSERT INTO ojs_stage_touched_keys SELECT key_type,key_hash,'discard partial',"
                           "NULL,NULL FROM ojs_stage_effective_keys LIMIT 1;", database=options["database"])
            for table in tables:
                preserved[table] = logical_rows(server, options["database"], table)
                inodes[table] = (server.datadir / options["database"] / f"{table}.ibd").stat().st_ino
            if staging_password:
                run_pipeline.set_root_password(server, None, staging_password)
            raise run_pipeline.PipelineError("fixture interrupted during touched keys")

        def write_legacy_state(*args, **kwargs):
            kwargs.pop("metadata_storage_profile", None)
            return original_write_state(*args, **kwargs)

        # Reproduce an actual legacy interrupted build: narrow payload schemas,
        # no widening migration, and no newer storage-profile checkpoint marker.
        with mock.patch.object(run_pipeline.MySQLServer, "run_sql_text", new=interrupt_after_completed_keys), \
                mock.patch.object(run_pipeline, "render_metadata_storage_sql", side_effect=lambda sql: sql), \
                mock.patch.object(run_pipeline, "migrate_metadata_storage", return_value={}), \
                mock.patch.object(run_pipeline, "write_build_state", side_effect=write_legacy_state), \
                self.assertRaisesRegex(run_pipeline.PipelineError, "fixture interrupted"):
            run_pipeline.build_snapshot_database(**options)
        staging = clean / "mysql-2026-01-01.building"
        checkpoint = run_pipeline.read_build_state(staging)
        self.assertEqual(checkpoint["phase"], "finalizing")
        self.assertNotIn("metadata_storage_profile", checkpoint)
        return options, preserved, inodes

    def test_cli_recovery_preserves_completed_work_provenance_and_installs_new_credentials(self):
        with TemporaryDirectory(prefix="ojs-touched-cli-") as directory, \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors, \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": ""}):
            root = Path(directory)
            options, preserved, inodes = self.make_interrupted_build(root)
            clean = options["clean_dir"]
            snapshot = options["snapshot"]
            staging = clean / "mysql-2026-01-01.building"
            staging_inode = staging.stat().st_ino
            original_checkpoint = run_pipeline.build_state_path(staging).read_bytes()
            original_source = snapshot.path.read_text(encoding="utf-8")
            # Same filename and byte size are insufficient: original source
            # content must match before the database audit or recovery writes.
            changed_source = original_source.replace("Alpha Study", "Alpho Study", 1)
            self.assertNotEqual(changed_source, original_source)
            self.assertEqual(len(changed_source.encode()), snapshot.size)
            snapshot.path.write_text(changed_source, encoding="utf-8")
            with mock.patch.object(run_pipeline, "validate_bootstrap_touched_keys",
                    side_effect=AssertionError("changed source must fail before key audit")), \
                    self.assertRaisesRegex(run_pipeline.PipelineError, "raw SQL checksum"):
                run_pipeline.build_snapshot_database(**{**options, "resume_building": True, "recover_touched_keys": True})
            self.assertEqual(run_pipeline.build_state_path(staging).read_bytes(), original_checkpoint)
            self.assertEqual(staging.stat().st_ino, staging_inode)
            snapshot.path.write_text(original_source, encoding="utf-8")

            original_start = run_pipeline.MySQLServer.start
            original_run = run_pipeline.MySQLServer.run_sql_text
            starts = []
            executed = []

            def inspect_start(server, password):
                starts.append(password)
                self.assertIsNone(password, "unfinished bootstrap has not installed the final root credential")
                original_start(server, password)
                self.assertEqual(server.datadir.stat().st_ino, staging_inode)
                for table, rows in preserved.items():
                    self.assertEqual((server.datadir / options["database"] / f"{table}.ibd").stat().st_ino, inodes[table])
                    self.assertEqual(logical_rows(server, options["database"], table), rows)

            def inspect_sql(server, sql, **kwargs):
                self.assertNotIn(kwargs["label"], ("source-index phase", "metadata-shard"))
                if kwargs["label"] == "temporal finalization phase":
                    self.assertTrue(sql.startswith("SELECT 'OJS_PROGRESS_V1', 'stage', 'touched_keys';"))
                    self.assertNotIn("CREATE TABLE ojs_stage_effective_keys", sql)
                    self.assertEqual(run_pipeline.build_state_path(server.datadir).read_bytes(), original_checkpoint)
                    executed.append(sql)
                return original_run(server, sql, **kwargs)

            new_root_password = "fixture-new-final-root"
            new_api_password = "fixture-new-final-api"
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {
                    "MYSQL_ROOT_PASSWORD": new_root_password, "OJS_DB_API_PASSWORD": new_api_password,
                    "MYSQL_PWD": "fixture-unrelated-inherited-password",
                }))
                for name in ("initialize", "import_snapshot"):
                    stack.enter_context(mock.patch.object(run_pipeline.MySQLServer, name,
                        side_effect=AssertionError(f"recovery must not {name}")))
                stack.enter_context(mock.patch.object(run_pipeline, "extract_metadata_in_parallel",
                    side_effect=AssertionError("recovery must not parse metadata")))
                stack.enter_context(mock.patch.object(run_pipeline.MySQLServer, "start", new=inspect_start))
                stack.enter_context(mock.patch.object(run_pipeline.MySQLServer, "run_sql_text", new=inspect_sql))
                result = run_pipeline.main([
                    "--snapshot", str(snapshot.path), "--raw-dir", str(snapshot.path.parent),
                    "--clean-dir", str(clean), "--build-sql", str(BUILD_SQL),
                    "--database", options["database"], "--api-db-user", "fixture_api",
                    "--skip-download", "--resume-building", "--compact-storage", "--recover-touched-keys",
                    "--mysql-buffer-pool-size", "128M", "--progress-seconds", "60",
                ])
            self.assertEqual(result, 0, errors.getvalue())
            self.assertEqual(starts, [None])
            self.assertEqual(len(executed), 1)
            final = clean / "mysql-2026-01-01"
            self.assertEqual(final.stat().st_ino, staging_inode)
            self.assertFalse(staging.exists())
            self.assertFalse((final / run_pipeline.BUILD_STATE_NAME).exists())
            self.assertFalse((final / "PIPELINE_FAILED.txt").exists())
            for name in ("mysql-current", "mysql-live", "pkpbeacon-clean-latest.sql.gz", "pkpbeacon-changes-latest.json"):
                self.assertFalse((clean / name).exists(), name)
                self.assertFalse((clean / name).is_symlink(), name)
            self.assertTrue(run_pipeline.release_artifacts_complete(clean, snapshot.version))

            report = json.loads(run_pipeline.change_report_path(clean, snapshot.version).read_text())
            recovery = report["storage"]["recovery"]
            original_hash = run_pipeline.sha256_small_file(BUILD_SQL)
            self.assertEqual(recovery["original_build_sql_sha256"], original_hash)
            self.assertEqual(recovery["build_sql_sha256"], original_hash)
            self.assertEqual(recovery["source_sha256"], run_pipeline.sha256_small_file(snapshot.path))
            self.assertEqual(recovery["recovery_sql_sha256"], hashlib.sha256(executed[0].encode()).hexdigest())
            self.assertEqual(recovery["recovery_profile"], bootstrap_recovery.RECOVERY_PROFILE)
            parts = run_pipeline.split_build_sql(BUILD_SQL, compact_storage=True)
            self.assertEqual(report["storage"]["rendered_build_sql_sha256"],
                hashlib.sha256((parts.prefix + parts.metadata + parts.suffix).encode()).hexdigest())
            self.assertEqual(report["release_guard"]["status"], "passed")
            for secret in (new_root_password, new_api_password):
                self.assertNotIn(secret, json.dumps(report))
                self.assertNotIn(secret, output.getvalue() + errors.getvalue())

            # Cold restart with the final credential verifies password rotation,
            # the original source/build provenance, and all release artifacts.
            verified = run_pipeline.verify_existing_database(
                snapshot=snapshot, datadir=final, build_sql=BUILD_SQL, database=options["database"],
                root_password=new_root_password, buffer_pool_size="128M", progress_seconds=60,
                verify_source_checksum=True,
            )
            self.assertGreater(verified.articles, 0)
            reader = run_pipeline.MySQLServer(run_pipeline.MySQLTools.discover(), final, "128M")
            try:
                reader.start(new_root_password)
                with self.assertRaises(run_pipeline.PipelineError):
                    reader.execute("SELECT 1;", password=None)
                self.assertEqual(reader.execute("SELECT build_sql_sha256 FROM ojs_pipeline_metadata WHERE id=1;",
                    database=options["database"], password=new_root_password), original_hash)
            finally:
                reader.shutdown(new_root_password)

    def test_unexpected_existing_root_password_is_refused_without_bypassing_grants(self):
        with TemporaryDirectory(prefix="ojs-touched-auth-") as directory, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": ""}):
            options, preserved, _ = self.make_interrupted_build(Path(directory), staging_password="fixture-unexpected-old-root")
            staging = options["clean_dir"] / "mysql-2026-01-01.building"
            checkpoint = run_pipeline.build_state_path(staging).read_bytes()
            with mock.patch.object(run_pipeline, "validate_bootstrap_touched_keys",
                    side_effect=AssertionError("authentication must precede audit")), \
                    self.assertRaises(run_pipeline.PipelineError):
                run_pipeline.build_snapshot_database(**{**options, "resume_building": True, "recover_touched_keys": True})
            self.assertEqual(run_pipeline.build_state_path(staging).read_bytes(), checkpoint)
            server = run_pipeline.MySQLServer(run_pipeline.MySQLTools.discover(), staging, "128M")
            try:
                server.start("fixture-unexpected-old-root")
                for table, rows in preserved.items():
                    self.assertEqual(logical_rows(server, options["database"], table,
                        password="fixture-unexpected-old-root"), rows)
            finally:
                server.shutdown("fixture-unexpected-old-root")


if __name__ == "__main__":
    unittest.main()
