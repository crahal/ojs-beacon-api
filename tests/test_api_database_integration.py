"""Optional, synthetic API/driver integration against disposable MySQL 8.4.

Requires the four MySQL command-line tools on PATH. The fixture binds only an
ephemeral loopback port, uses private temporary directories and never opens an
existing application datadir. No real credentials or upstream data are needed.
"""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import pymysql

from test_api import api
import test_pipeline as fixture
import run_pipeline as pipeline


@unittest.skipUnless(
    all(shutil.which(name) for name in ("mysqld", "mysql", "mysqladmin", "mysqldump")),
    "MySQL 8.4 tools required",
)
class APIDatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory(prefix="ojs-api-database-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.environment = mock.patch.dict(os.environ, {
            "OJS_MYSQL_TMP_ROOT": "",
            "OJS_BUILDER_MYSQL_MAX_CONNECTIONS": "8",
            "OJS_BUILDER_MYSQL_TEMPTABLE_MAX_RAM": "64M",
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        root = Path(cls.temporary.name)
        raw, clean = root / "data/raw", root / "data/clean"
        raw.mkdir(parents=True)
        clean.mkdir()
        snapshot_path = raw / "pkpbeacon-2026-01-01.sql"
        snapshot_path.write_text(fixture.JANUARY_DUMP, encoding="utf-8")
        tools = pipeline.MySQLTools.discover()
        cls.root_password = "synthetic-root-password"
        cls.api_password = "synthetic-api-password"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            candidate, _ = pipeline.build_snapshot_database(
                snapshot=pipeline.inspect_snapshot(snapshot_path), clean_dir=clean,
                build_sql=Path(__file__).parents[1] / "sql/01_build_ojs_tables.sql",
                database="pkpbeacon_db", root_password=cls.root_password,
                api_db_user="ojs_api", api_db_password=cls.api_password,
                release_thresholds=pipeline.ReleaseThresholds(.1, .1, .1, .1),
                allow_anomalous_release=False, buffer_pool_size="128M", progress_seconds=60,
                force_rebuild=False, verify_existing=False, verify_source_checksum=False,
                full_rescan=False, publish_current=False, metadata_workers=1,
                resume_building=False, compact_storage=True,
            )
        cls.server = pipeline.MySQLServer(tools, candidate, "128M", progress_seconds=60)
        cls.addClassCleanup(cls.server.shutdown, cls.root_password)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cls.port = reservation.getsockname()[1]
        original_control = cls.server._run_control

        def start_on_loopback(args, *, label, **kwargs):
            if label == "mysql-start":
                args = [arg for arg in args if arg != "--skip-networking"]
                args.extend(["--bind-address=127.0.0.1", f"--port={cls.port}"])
            return original_control(args, label=label, **kwargs)

        with mock.patch.object(cls.server, "_run_control", side_effect=start_on_loopback):
            cls.server.start(cls.root_password)
        cls.config = api.APIConfig(
            db_host="127.0.0.1", db_port=cls.port, db_name="pkpbeacon_db",
            db_user="ojs_api", db_password=cls.api_password,
            api_username="synthetic-client", api_password="synthetic-http-password",
            article_table="ojs_articles", source_table="ojs_article_sources",
            event_table="ojs_article_events", snapshot_table="ojs_snapshots",
            oai_page_size=50, public_base_url="https://api.example.org",
        )

    def setUp(self):
        self.backend = api.SQLBackend(self.config)

    def test_tcp_default_tls_and_caching_sha2_authentication(self):
        # No ssl options or CA overrides: exercise exactly SQLBackend's default
        # TCP connection, including PyMySQL 1.2's automatic TLS negotiation.
        self.backend.assert_ready()
        with self.backend._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                self.assertTrue(cursor.fetchone()["Value"])
                cursor.execute("SELECT CURRENT_USER() AS account")
                self.assertEqual(cursor.fetchone()["account"], "ojs_api@%")
        plugin = self.server.execute(
            "SELECT plugin FROM mysql.user WHERE User='ojs_api' AND Host='%';",
            password=self.root_password,
        )
        self.assertEqual(plugin, "caching_sha2_password")

    def test_passwordless_mysqladmin_ping_is_only_a_liveness_check(self):
        with self.assertRaises(pymysql.err.OperationalError) as caught:
            pymysql.connect(
                host="127.0.0.1", port=self.port, user="root", password="", connect_timeout=5,
            )
        self.assertEqual(caught.exception.args[0], 1045)
        environment = os.environ.copy()
        environment.pop("MYSQL_PWD", None)
        command = [
            self.server.tools.mysqladmin,
            "--no-defaults", "--no-login-paths", "ping",
            "--host=127.0.0.1", "--user=root", "--silent", f"--port={self.port}",
        ]
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=10,
        )
        # mysqladmin ping deliberately returns success for an authentication
        # rejection: a responding server is alive, but readiness is unproven.
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertNotIn("MYSQL_PWD", environment)
        for secret in (self.root_password, self.api_password):
            self.assertNotIn(secret, " ".join(command))
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_rest_and_oai_queries_use_real_clean_schema(self):
        self.assertEqual(str(self.backend.snapshots()[0]["snapshot_date"]), "2026-01-01")
        self.assertGreater(self.backend.meta()["high_watermark_event_id"], 0)
        options = dict(
            limit=1, article_status="active", changed_since=None, issn=None, doi=None,
            ids=None, structured_metadata=True, include_metadata_xml=False,
        )
        first = self.backend.list_articles(after_id=0, **options)
        self.assertEqual(len(first["data"]), 1)
        article_id = first["data"][0]["article_id"]
        self.assertEqual(first["next_after_id"], article_id)
        second = self.backend.list_articles(after_id=first["next_after_id"], **options)
        self.assertGreater(second["data"][0]["article_id"], article_id)
        article = self.backend.article(article_id)
        self.assertEqual(article["article_id"], article_id)
        self.assertNotIn("metadata_xml", article)
        self.assertTrue(article["metadata"]["title"])
        self.assertIsNone(self.backend.article(2**63))
        sources = self.backend.sources(article_id, after_id=0, limit=100)
        self.assertTrue(sources["data"])
        self.assertIsInstance(sources["data"][0]["is_active"], bool)
        changes = self.backend.changes(
            after_event_id=0, limit=1, structured_metadata=True, include_metadata_xml=False,
        )
        self.assertEqual(changes["data"][0]["current_operation"], "upsert")
        self.assertEqual(changes["data"][0]["article"]["article_id"], article_id)
        self.assertGreater(changes["next_after_event_id"], 0)
        records, more = self.backend.oai_records(
            from_date="2026-01-01", until_date="2026-01-01", issn=None,
            after_date=None, after_id=0, limit=1,
        )
        self.assertEqual(len(records), 1)
        self.assertTrue(more)
        self.assertTrue(records[0]["metadata_xml"])
        sets, more_sets = self.backend.oai_sets(after_issn="", limit=100)
        self.assertTrue(sets)
        self.assertFalse(more_sets)
        self.assertIsInstance(sets[0]["article_count"], int)

    def test_api_database_account_cannot_write_or_read_mysql_users(self):
        with self.backend._connect() as connection:
            with connection.cursor() as cursor:
                for statement in (
                    "DELETE FROM ojs_articles WHERE article_id=0",
                    "SELECT User FROM mysql.user",
                ):
                    with self.subTest(statement=statement), self.assertRaises(pymysql.err.OperationalError) as caught:
                        cursor.execute(statement)
                    self.assertEqual(caught.exception.args[0], 1142)
        with self.assertRaises(pymysql.err.OperationalError) as caught:
            pymysql.connect(
                host="127.0.0.1", port=self.port, user="ojs_api",
                password="incorrect-synthetic-password", connect_timeout=5,
            )
        self.assertEqual(caught.exception.args[0], 1045)


if __name__ == "__main__":
    unittest.main()
