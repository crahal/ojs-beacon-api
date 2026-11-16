from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import adopt_candidate_credentials as adoption
import run_pipeline as pipeline
from test_compact_update import release
import test_pipeline as fixture

PRODUCTION_ROOT = "production-root-secret"
TEMPORARY_ROOT = "temporary-root-secret"
PRODUCTION_API = "production-api-secret"


def credentials(root, version="8.4.11"):
    secrets = root / ".secrets"
    secrets.mkdir()
    environment = root / ".env"
    environment.write_text(
        f"MYSQL_ROOT_PASSWORD='{PRODUCTION_ROOT}'\nOJS_MYSQL_IMAGE=mysql:{version}\n"
        "OJS_DB_NAME=pkpbeacon_db\nOJS_DB_API_USER=ojs_api\n"
        "OJS_DB_API_PASSWORD_FILE=./.secrets/db-api-password\n", encoding="utf-8")
    api_password = secrets / "db-api-password"
    api_password.write_text(PRODUCTION_API + "\n")
    temporary_root = secrets / "incoming-root-password"
    temporary_root.write_text(TEMPORARY_ROOT + "\n")
    for path in (environment, api_password, temporary_root):
        path.chmod(0o600)
    pause = root / "data" / adoption.update.PAUSE_MARKER
    pause.write_text("Keep automatic updates paused.\n")
    return temporary_root


class FakeServer:
    def __init__(self, state, candidate):
        self.state, self.candidate = state, candidate
        self.running = False
        self.shutdown_password = None
        self.auth_attempts = []

    def start(self, password):
        self.running = True  # Like mysqladmin ping, readiness is not authentication.

    def execute(self, sql, *, password=None, database=None, label="statement"):
        if label == "credential-adoption-authentication":
            self.auth_attempts.append(password)
        if password != self.state["root"]:
            raise pipeline.PipelineError("authentication refused")
        if sql == "SELECT 1;":
            return "1"
        if label == "credential-adoption-tables":
            return "\n".join((*pipeline.CLEAN_EXPORT_TABLES, "ojs_pipeline_metadata"))
        if label == "credential-adoption-provenance":
            values = [self.candidate.date, self.candidate.source_sha256, self.candidate.build_sql_sha256]
            if self.state.get("bad_provenance"):
                values[0] = "1999-01-01"
            return "\t".join(values + ["8.4.11"]) + "\n" + "\t".join(values)
        if "CREATE USER IF NOT EXISTS" in sql:
            self.state["api"] = PRODUCTION_API
            return ""
        if "ALTER USER 'root'@'localhost'" in sql:
            self.state["root"] = PRODUCTION_ROOT
            if self.state.pop("fail_after_root_change", False):
                raise pipeline.PipelineError("simulated failure after ALTER USER committed")
            return ""
        raise AssertionError("Unexpected SQL")

    def shutdown(self, password):
        self.shutdown_password = password
        self.running = False
        if password is not None and password != self.state["root"]:
            raise AssertionError("Shutdown used an obsolete credential")


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate = release(self.root, "2026-01-01")
        self.raw = self.root / "data" / "raw"
        self.raw.mkdir()
        marker_path = self.candidate.datadir / adoption.update.MARKER
        marker = json.loads(marker_path.read_text())
        marker["mysql_version"] = "8.4.11"
        marker_path.write_text(json.dumps(marker))
        self.temporary_password = credentials(self.root)
        self.argv = ["--project-root", str(self.root), "--snapshot", self.candidate.date,
                     "--temporary-root-password-file", str(self.temporary_password)]
        self.args = adoption.parser().parse_args(self.argv)
        self.state = {"root": TEMPORARY_ROOT, "api": "temporary-api"}
        self.servers = []
        tools = pipeline.MySQLTools("mysqld", "mysql", "mysqladmin", "mysqldump", "8.4.11")
        self.tools = mock.patch.object(pipeline.MySQLTools, "discover", return_value=tools).start()
        self.addCleanup(mock.patch.stopall)
        self.unmounted = mock.patch.object(adoption.update.Coordinator, "require_unmounted").start()
        self.rollback = mock.patch.object(adoption, "wait_innodb_rollback").start()
        self.factory = mock.patch.object(pipeline, "MySQLServer", side_effect=self.server).start()

    def server(self, *args, **kwargs):
        server = FakeServer(self.state, self.candidate)
        self.servers.append(server)
        return server

    def test_success_and_repeat_keep_secrets_pause_exports_and_serving_state(self):
        original = {path: path.read_bytes() for path in (
            self.root / ".env", self.temporary_password, self.root / ".secrets/db-api-password",
            self.candidate.export, self.root / "data" / adoption.update.PAUSE_MARKER,
        )}
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(adoption.adopt(self.args), {
                    "status": "credentials_adopted", "release": self.candidate.date,
                    "published": False, "updates_paused": True,
                })
        self.assertEqual(self.state, {"root": PRODUCTION_ROOT, "api": PRODUCTION_API})
        self.assertEqual(self.servers[0].auth_attempts[:2], [PRODUCTION_ROOT, TEMPORARY_ROOT])
        self.assertEqual(self.servers[1].auth_attempts[0], PRODUCTION_ROOT)
        self.assertNotIn(TEMPORARY_ROOT, self.servers[1].auth_attempts)
        self.assertTrue(all(server.shutdown_password == PRODUCTION_ROOT for server in self.servers))
        self.assertTrue(all(not server.running for server in self.servers))
        self.assertFalse((self.root / "data/clean/mysql-live").exists())
        self.assertFalse((self.root / "data/clean/mysql-current").exists())
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        receipt = (self.candidate.datadir / adoption.ADOPTION_MARKER).read_text()
        self.assertEqual(json.loads(receipt)["mysql_version"], "8.4.11")
        for secret in (PRODUCTION_ROOT, TEMPORARY_ROOT, PRODUCTION_API):
            self.assertNotIn(secret, receipt)

    def test_failure_after_root_commit_shuts_down_with_new_password_and_retries(self):
        self.state["fail_after_root_change"] = True
        with self.assertRaises(pipeline.PipelineError):
            adoption.adopt(self.args)
        self.assertEqual(self.servers[0].shutdown_password, PRODUCTION_ROOT)
        self.assertFalse((self.candidate.datadir / adoption.ADOPTION_MARKER).exists())
        self.assertEqual(adoption.adopt(self.args)["status"], "credentials_adopted")

    def test_failure_before_rekey_shuts_down_without_changing_credentials(self):
        self.state["bad_provenance"] = True
        with self.assertRaisesRegex(adoption.AdoptionError, "provenance"):
            adoption.adopt(self.args)
        self.assertEqual(self.state["root"], TEMPORARY_ROOT)
        self.assertEqual(self.state["api"], "temporary-api")
        self.assertEqual(self.servers[0].shutdown_password, TEMPORARY_ROOT)
        self.assertFalse((self.candidate.datadir / adoption.ADOPTION_MARKER).exists())

    def test_wrong_known_passwords_stop_without_mutation(self):
        self.state["root"] = "unrecognized-root"
        with self.assertRaisesRegex(adoption.AdoptionError, "Neither"):
            adoption.adopt(self.args)
        self.assertFalse(self.servers[0].running)
        self.assertEqual(self.state["api"], "temporary-api")
        self.assertEqual(set(self.servers[0].auth_attempts), {PRODUCTION_ROOT, TEMPORARY_ROOT})

    def test_live_or_mounted_candidate_is_rejected_before_start(self):
        live = self.root / "data/clean/mysql-live"
        live.symlink_to(self.candidate.datadir.name)
        with self.assertRaisesRegex(adoption.AdoptionError, "serving database"):
            adoption.adopt(self.args)
        live.unlink()
        self.unmounted.side_effect = adoption.update.UpdateError("mounted")
        with self.assertRaises(adoption.update.UpdateError):
            adoption.adopt(self.args)
        self.factory.assert_not_called()

    def test_patch_mismatch_or_missing_evidence_is_rejected_before_start(self):
        path = self.candidate.datadir / adoption.update.MARKER
        marker = json.loads(path.read_text())
        for version in ("8.4.0", None):
            with self.subTest(version=version):
                marker["mysql_version"] = version
                path.write_text(json.dumps(marker))
                with self.assertRaisesRegex(adoption.AdoptionError, "same exact patch"):
                    adoption.adopt(self.args)
        self.factory.assert_not_called()

    def test_pause_required_and_pending_publication_rejected(self):
        pause = self.root / "data" / adoption.update.PAUSE_MARKER
        pause.unlink()
        with self.assertRaisesRegex(adoption.AdoptionError, "paused"):
            adoption.adopt(self.args)
        pause.touch()
        (self.root / "data/clean/.compact-update.json").write_text("{}")
        with self.assertRaisesRegex(adoption.AdoptionError, "journal"):
            adoption.adopt(self.args)
        self.factory.assert_not_called()

    def test_private_file_symlinks_or_open_permissions_are_rejected(self):
        self.temporary_password.chmod(0o644)
        with self.assertRaisesRegex(adoption.AdoptionError, "0600"):
            adoption.adopt(self.args)
        self.temporary_password.chmod(0o600)
        alias = self.root / ".secrets/password-alias"
        alias.symlink_to(self.temporary_password)
        self.args.temporary_root_password_file = alias
        with self.assertRaisesRegex(adoption.AdoptionError, "symlinks"):
            adoption.adopt(self.args)
        self.factory.assert_not_called()

    def test_configuration_is_not_executed_and_rejects_shell_expansion(self):
        environment = self.root / ".env"
        original = environment.read_text()
        for value in ("$(touch should-not-exist)", "`touch should-not-exist`", "$OTHER", "first; touch should-not-exist"):
            with self.subTest(value=value):
                environment.write_text(original.replace(PRODUCTION_ROOT, value).replace("'", ""))
                with self.assertRaises(adoption.AdoptionError):
                    adoption.adopt(self.args)
        self.assertFalse((self.root / "should-not-exist").exists())
        self.factory.assert_not_called()

    def test_all_locks_exclude_credential_mutation(self):
        clean = self.root / "data/clean"
        for name, lock in (
            ("automatic", lambda: adoption.update.automatic_lock(clean)),
            ("pipeline", lambda: pipeline.pipeline_lock(clean)),
            ("publisher", lambda: adoption.update.publish_live.publisher_lock(self.root)),
        ):
            with self.subTest(lock=name), lock(), self.assertRaises(RuntimeError):
                adoption.adopt(self.args)
        source_lock = self.raw / ".source-activity.lock"
        with source_lock.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(adoption.update.SourceActivityBusy):
                adoption.adopt(self.args)
        self.factory.assert_not_called()

    def test_driver_errors_are_redacted(self):
        with mock.patch.object(adoption, "adopt", side_effect=RuntimeError(PRODUCTION_ROOT)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(adoption.main(self.argv), 1)
        self.assertNotIn(PRODUCTION_ROOT, stdout.getvalue() + stderr.getvalue())


@unittest.skipUnless(all(shutil.which(name) for name in ("mysqld", "mysql", "mysqladmin", "mysqldump")),
                     "MySQL 8.4 tools required")
class AdoptionMySQLTests(unittest.TestCase):
    def test_real_candidate_root_and_select_only_api_credentials_are_adopted_and_retryable(self):
        with TemporaryDirectory(prefix="ojs-adopt-credentials-") as temporary, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"OJS_MYSQL_TMP_ROOT": ""}):
            root = Path(temporary)
            raw, clean = root / "data/raw", root / "data/clean"
            raw.mkdir(parents=True)
            clean.mkdir()
            snapshot_path = raw / "pkpbeacon-2026-01-01.sql"
            snapshot_path.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
            tools = pipeline.MySQLTools.discover()
            snapshot = pipeline.inspect_snapshot(snapshot_path)
            candidate, _ = pipeline.build_snapshot_database(
                snapshot=snapshot, clean_dir=clean,
                build_sql=Path(__file__).parents[1] / "sql/01_build_ojs_tables.sql",
                database="pkpbeacon_db", root_password=TEMPORARY_ROOT,
                api_db_user="ojs_api", api_db_password="temporary-api-password",
                release_thresholds=pipeline.ReleaseThresholds(.1, .1, .1, .1),
                allow_anomalous_release=False, buffer_pool_size="128M", progress_seconds=60,
                force_rebuild=False, verify_existing=False, verify_source_checksum=False,
                full_rescan=False, publish_current=False, metadata_workers=1,
                resume_building=False, compact_storage=True,
            )
            temporary_password = credentials(root, tools.version)
            args = adoption.parser().parse_args([
                "--project-root", str(root), "--snapshot", snapshot.version,
                "--temporary-root-password-file", str(temporary_password),
            ])
            # The isolated fresh fixture cannot be mounted in a container.
            # Real process/POSIX locks and all other checks remain in force.
            with mock.patch.object(adoption.update.Coordinator, "protected_paths", return_value=[]):
                self.assertEqual(adoption.adopt(args)["status"], "credentials_adopted")
                self.assertEqual(adoption.adopt(args)["status"], "credentials_adopted")
            server = pipeline.MySQLServer(tools, candidate, "128M")
            try:
                server.start(PRODUCTION_ROOT)
                self.assertEqual(server.execute("SELECT 1;", password=PRODUCTION_ROOT), "1")
                with self.assertRaises(pipeline.PipelineError):
                    server.execute("SELECT 1;", password=TEMPORARY_ROOT)
                grants = server.execute("SHOW GRANTS FOR 'ojs_api'@'%';", password=PRODUCTION_ROOT)
                self.assertEqual(grants.count("GRANT SELECT ON"), len(pipeline.API_READ_TABLES))
                self.assertNotIn("GRANT OPTION", grants)
                self.assertNotIn("INSERT", grants)
                api = subprocess.run([
                    tools.mysql, "--no-defaults", "--protocol=socket", f"--socket={server.socket}",
                    "--user=ojs_api", "--batch", "--skip-column-names", "pkpbeacon_db",
                ], input="SELECT COUNT(*) FROM ojs_snapshots;", text=True, capture_output=True,
                    env=server._password_env(PRODUCTION_API), timeout=30)
                self.assertEqual(api.returncode, 0)
                self.assertEqual(api.stdout.strip(), "1")
            finally:
                server.shutdown(PRODUCTION_ROOT)
            self.assertFalse((clean / "mysql-live").exists())
            self.assertTrue((root / "data" / adoption.update.PAUSE_MARKER).is_file())


if __name__ == "__main__":
    unittest.main()
