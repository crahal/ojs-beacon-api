"""Independent tester credentials, revocation and private-file failure modes."""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import api_clients
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from test_api import api, FakeBackend


KEY_ONE = "synthetic-test-key-one-" + "a" * 43
KEY_TWO = "synthetic-test-key-two-" + "b" * 43
KEY_THREE = "synthetic-test-key-three-" + "c" * 43


def client(username="tester01", key=KEY_ONE, **changes):
    return {"username": username, "key_sha256": hashlib.sha256(key.encode("ascii")).hexdigest(),
            "enabled": True, "expires_at": None, **changes}


def document():
    return {"version": 1, "clients": [client(), client("tester02", KEY_TWO)]}


def write_registry(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def api_config(path=None):
    return api.APIConfig(
        db_host="localhost", db_port=3306, db_name="fixture", db_user="fixture",
        db_password="synthetic-db-password", api_username="monitor", api_password="monitor-key",
        article_table="ojs_articles", source_table="ojs_article_sources",
        event_table="ojs_article_events", snapshot_table="ojs_snapshots", oai_page_size=100,
        api_clients_file=path,
    )


def authentication(config):
    with mock.patch.object(api, "SQLBackend", FakeBackend):
        app = api.create_app(config)
    route = next(item for item in app.routes if item.path == "/")
    return next(item.call for item in route.dependant.dependencies
                if item.call.__name__ == "require_auth")


def credentials(username, password):
    return HTTPBasicCredentials(username=username, password=password)


class RegistryTests(unittest.TestCase):
    def test_distinct_clients_keys_cannot_be_swapped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clients.json"
            write_registry(path, document())
            self.assertEqual(api_clients.read_registry(path), document())
            for username, key, accepted in (("tester01", KEY_ONE, True), ("tester02", KEY_TWO, True),
                                            ("tester01", KEY_TWO, False), ("tester02", KEY_ONE, False),
                                            ("unknown", KEY_ONE, False), ("tester01", "wrong", False)):
                with self.subTest(username=username, accepted=accepted):
                    self.assertEqual(api_clients.authenticate_client(path, username, key), accepted)

    def test_revoked_and_expired_clients_are_rejected_at_the_exact_boundary(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for changes, accepted in (({"enabled": False}, False),
                                  ({"expires_at": "2030-01-01T00:00:00Z"}, False),
                                  ({"expires_at": "2029-12-31T23:59:59+00:00"}, False),
                                  ({"expires_at": "2030-01-01T00:00:00.000001Z"}, True)):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                path = Path(directory) / "clients.json"
                write_registry(path, {"version": 1, "clients": [client(**changes)]})
                self.assertEqual(api_clients.authenticate_client(path, "tester01", KEY_ONE, now=now), accepted)

    def test_schema_username_expiry_hash_and_duplicate_constraints_are_strict(self):
        invalid = [None, [], {}, {"version": True, "clients": []}, {"version": 2, "clients": []},
                   {"version": 1, "clients": [], "extra": True},
                   {"version": 1, "clients": [client(), client()]},
                   {"version": 1, "clients": [client(), client("tester03")]},
                   {"version": 1, "clients": [client("monitor")]},
                   {"version": 1, "clients": [client(f"user{i}") for i in range(257)]}]
        for changes in ({"username": ""}, {"username": "x" * 65}, {"username": "tést"},
                        {"username": "tester:name"}, {"username": "tester\n"},
                        {"enabled": 1}, {"enabled": "true"}, {"key_sha256": "A" * 64},
                        {"key_sha256": "b" * 63}, {"key_sha256": KEY_ONE},
                        {"expires_at": "2030-01-01"}, {"expires_at": "2030-01-01T00:00:00"},
                        {"expires_at": "2030-01-01T00:00:00+01:00"},
                        {"expires_at": "2030-02-30T00:00:00Z"}, {"expires_at": 123},
                        {"raw_key": KEY_ONE}):
            invalid.append({"version": 1, "clients": [client(**changes)]})
        for index, value in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(api_clients.RegistryError) as raised:
                api_clients.validate_registry(value, legacy_username="monitor")
            self.assertEqual(str(raised.exception), api_clients.REGISTRY_ERROR)
        valid = {"version": 1, "clients": [client(f"user{i}", f"synthetic-key-{i}") for i in range(256)]}
        self.assertEqual(api_clients.validate_registry(valid), valid)

    def test_unsafe_files_and_json_are_rejected_with_fixed_redacted_error(self):
        faults = ("missing", "public", "symlink", "hardlink", "directory", "fifo", "oversized",
                  "invalid-json", "duplicate-json", "nonfinite-json", "unknown-owner")
        for fault in faults:
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "private-sensitive-registry.json"
                write_registry(path, document())
                if fault in ("missing", "directory", "fifo", "symlink"):
                    path.unlink()
                if fault == "public":
                    path.chmod(0o644)
                elif fault == "symlink":
                    target = root / "target.json"
                    write_registry(target, document())
                    path.symlink_to(target)
                elif fault == "hardlink":
                    os.link(path, root / "second.json")
                elif fault == "directory":
                    path.mkdir(mode=0o700)
                elif fault == "fifo":
                    os.mkfifo(path, 0o600)
                elif fault == "oversized":
                    path.write_bytes(b" " * (api_clients.MAX_REGISTRY_BYTES + 1))
                elif fault == "invalid-json":
                    path.write_text(KEY_ONE)
                elif fault == "duplicate-json":
                    path.write_text('{"version":1,"version":1,"clients":[]}')
                elif fault == "nonfinite-json":
                    path.write_text('{"version":1,"clients":[],"extra":NaN}')
                original_fstat = os.fstat

                def checked_stat(descriptor):
                    info = original_fstat(descriptor)
                    if fault == "unknown-owner":
                        values = list(info)
                        values[4] = os.geteuid() + 12345
                        return os.stat_result(values)
                    return info

                with mock.patch.object(api_clients.os, "fstat", side_effect=checked_stat):
                    with self.assertRaises(api_clients.RegistryError) as raised:
                        api_clients.read_registry(path)
                self.assertEqual(str(raised.exception), api_clients.REGISTRY_ERROR)

    def test_credentials_are_bounded_before_reading_or_hashing(self):
        for username, key in (("x" * 65, KEY_ONE), ("tést", KEY_ONE), ("tester01", "x" * 513),
                              ("tester01", "sécret"), ("tester01", "line\nbreak"), ("tester01", "")):
            with self.subTest(username=username[:12], length=len(key)), \
                    mock.patch.object(api_clients, "read_registry") as read, \
                    mock.patch.object(api_clients.hashlib, "sha256") as digest:
                self.assertFalse(api_clients.authenticate_client(Path("unused"), username, key))
            read.assert_not_called()
            digest.assert_not_called()

    def test_symlink_ancestors_and_parent_traversal_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            path = actual / "clients.json"
            write_registry(path, document())
            link = root / "alias"
            link.symlink_to(actual, target_is_directory=True)
            for selected in (link / path.name, root / "actual" / ".." / "actual" / path.name):
                with self.subTest(selected=selected), self.assertRaises(api_clients.RegistryError):
                    api_clients.read_registry(selected)
            with mock.patch.object(api_clients.Path, "cwd", return_value=actual):
                self.assertEqual(api_clients.read_registry(Path("clients.json")), document())

    def test_atomic_replacement_during_read_is_rejected_and_next_read_observes_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clients.json"
            write_registry(path, document())
            changed = document()
            changed["clients"][0]["enabled"] = False
            replacement = path.with_name("replacement.json")
            write_registry(replacement, changed)
            original_read = os.read
            swapped = False

            def read_then_replace(descriptor, count):
                nonlocal swapped
                chunk = original_read(descriptor, count)
                if chunk and not swapped:
                    os.replace(replacement, path)
                    swapped = True
                return chunk

            with mock.patch.object(api_clients.os, "read", side_effect=read_then_replace), \
                    self.assertRaises(api_clients.RegistryError):
                api_clients.read_registry(path)
            self.assertEqual(api_clients.read_registry(path), changed)


class APIClientAuthenticationTests(unittest.TestCase):
    def test_optional_registry_preserves_legacy_authentication(self):
        auth = authentication(api_config())
        self.assertEqual(auth(credentials("monitor", "monitor-key")), "monitor")
        for username, key in (("monitor", "wrong"), ("tester01", KEY_ONE), ("tést", "sécret")):
            with self.subTest(username=username), self.assertRaises(HTTPException) as raised:
                auth(credentials(username, key))
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(raised.exception.detail, "invalid credentials")
            self.assertEqual(raised.exception.headers, {"WWW-Authenticate": "Basic"})

    def test_startup_rejects_missing_bad_registry_and_legacy_username_collision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clients.json"
            for value in (None, {}, {"version": 1, "clients": [client("monitor")]}):
                if value is not None:
                    write_registry(path, value)
                with self.assertRaises(api_clients.RegistryError):
                    authentication(api_config(path))

    def test_hot_reload_atomic_replacement_revocation_and_rotation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clients.json"
            write_registry(path, document())
            auth = authentication(api_config(path))
            self.assertEqual(auth(credentials("tester01", KEY_ONE)), "tester01")
            self.assertEqual(auth(credentials("tester02", KEY_TWO)), "tester02")
            replacement = path.with_name("replacement.json")
            changed = document()
            changed["clients"][0]["enabled"] = False
            write_registry(replacement, changed)
            os.replace(replacement, path)
            with self.assertRaises(HTTPException) as raised:
                auth(credentials("tester01", KEY_ONE))
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(auth(credentials("tester02", KEY_TWO)), "tester02")
            changed["clients"][0] = client(key=KEY_THREE)
            write_registry(replacement, changed)
            os.replace(replacement, path)
            self.assertEqual(auth(credentials("tester01", KEY_THREE)), "tester01")
            with self.assertRaises(HTTPException):
                auth(credentials("tester01", KEY_ONE))

    def test_registry_failure_after_startup_returns_generic_503_without_cached_acceptance(self):
        for fault in ("missing", "malformed", "public", "collision"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                path = Path(directory) / "clients.json"
                write_registry(path, document())
                auth = authentication(api_config(path))
                self.assertEqual(auth(credentials("tester01", KEY_ONE)), "tester01")
                if fault == "missing":
                    path.unlink()
                elif fault == "malformed":
                    path.write_text(KEY_ONE)
                elif fault == "public":
                    path.chmod(0o644)
                else:
                    write_registry(path, {"version": 1, "clients": [client("monitor")]})
                with self.assertRaises(HTTPException) as raised:
                    auth(credentials("tester01", KEY_ONE))
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.detail, "service unavailable")
                self.assertEqual(auth(credentials("monitor", "monitor-key")), "monitor")

    def test_configuration_resolves_optional_registry_environment(self):
        args = Namespace(db_host="localhost", db_port=3306, db_name="fixture", db_user="fixture",
                         db_password="fixture", oai_page_size=100)
        env = {"OJS_API_USERNAME": "monitor", "OJS_API_KEY": "monitor-key"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(api.resolve_config(args).api_clients_file)
        with mock.patch.dict(os.environ, {**env, "OJS_API_CLIENTS_FILE": "/private/clients.json"}, clear=True):
            self.assertEqual(api.resolve_config(args).api_clients_file, Path("/private/clients.json"))


if __name__ == "__main__":
    unittest.main()
