from __future__ import annotations

import copy
import json
import multiprocessing
import os
from pathlib import Path
import ssl
import stat
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import progress_api as api


SECRET = "private-do-not-display"
ORIGIN = ("catalogue.example.org", None)
LATEST = {
    "snapshot_date": "2026-10-01", "article_count": 4,
    "active_article_count": 2, "removed_article_count": 1,
    "merged_article_count": 1, "source_record_count": 7,
    "active_source_count": 5, "source_filename": SECRET,
}


def responses():
    meta = {"latest_snapshot": copy.deepcopy(LATEST), "high_watermark_event_id": 19}
    return {
        "/health": {"status": "ok"},
        "/meta": meta,
        "/snapshots": {"data": [{"snapshot_date": "2026-01-01"}, copy.deepcopy(LATEST)]},
        "/articles?limit=1&structured_metadata=false": {
            **copy.deepcopy(meta), "next_after_id": 8,
            "data": [{"article_id": 8, "status": "active", "title": SECRET,
                      "creators": SECRET, "endpoint_oai_url": "https://private.example.org/"}],
        },
    }


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "api-client.env"
        self.path.write_text("OJS_API_USERNAME=monitor\nOJS_API_KEY=" + SECRET + "\n")
        self.path.chmod(0o600)

    def collect(self, values=None, *, directory=None):
        fixtures = responses() if values is None else values
        observed = []

        def get(_origin, endpoint, authorization=None):
            self.assertEqual(_origin, ORIGIN)
            if endpoint == "/health":
                self.assertIsNone(authorization)
            else:
                self.assertEqual(authorization, api._credentials(self.directory.name))
            value = fixtures[endpoint]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)

        with mock.patch.object(api, "_get", side_effect=get) as request:
            result = api._collect(ORIGIN, self.directory.name if directory is None else directory,
                                  lambda value: observed.append(copy.deepcopy(value)))
        return result, observed, request.call_count

    def test_serving_observation_only_contains_validated_allowlisted_facts(self):
        result, observed, count = self.collect()
        self.assertEqual(count, 4)
        self.assertEqual(observed, [{"status": "unverified", "health": "ok"}])
        self.assertEqual(result, {
            "status": "serving", "health": "ok", "latest_snapshot": "2026-10-01",
            "snapshot_dates": ["2026-01-01", "2026-10-01"], "high_watermark_event_id": 19,
            "active_articles": 2, "total_articles": 4, "removed_articles": 1,
            "merged_articles": 1, "active_sources": 5, "source_records": 7,
        })
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("example.org", json.dumps(result))

    def test_missing_credentials_still_observes_health(self):
        result, _, count = self.collect(directory="")
        self.assertEqual(result, {"status": "unverified", "health": "ok"})
        self.assertEqual(count, 1)

    def test_invalid_credentials_are_private_and_not_sent(self):
        for raw in ("", "OJS_API_KEY=" + SECRET, "OJS_API_USERNAME=x\nOJS_API_USERNAME=y\nOJS_API_KEY=x",
                    "OJS_API_USERNAME=x\nOJS_API_KEY=x\nEXTRA=" + SECRET,
                    "OJS_API_USERNAME=x\nOJS_API_KEY=x\r\nInjected: " + SECRET,
                    "x" * (api.MAX_CREDENTIAL_BYTES + 1)):
            with self.subTest(length=len(raw)):
                self.path.write_text(raw)
                result, _, count = self.collect()
                self.assertEqual(result, {"status": "unverified", "health": "ok"})
                self.assertEqual(count, 1)

    def test_credential_permissions_symlink_and_fifo_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(api.ProbeError):
            api._credentials(self.directory.name)
        self.path.unlink()
        target = Path(self.directory.name) / "other"
        target.write_text(SECRET)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            api._credentials(self.directory.name)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(api.ProbeError):
            api._credentials(self.directory.name)

    def test_authentication_failure_does_not_claim_api_outage(self):
        values = responses()
        values["/meta"] = api.ProbeError("auth_failed")
        result, _, count = self.collect(values)
        self.assertEqual(result, {"status": "auth_failed", "health": "ok"})
        self.assertEqual(count, 2)

    def test_health_failure_stops_authenticated_requests(self):
        for health, expected in (({"status": SECRET}, {"status": "unverified", "health": "failed"}),
                                 (api.ProbeError("unavailable", health="failed"),
                                  {"status": "unavailable", "health": "failed"}),
                                 (api.ProbeError("unavailable"),
                                  {"status": "unavailable", "health": "unknown"})):
            with self.subTest(expected=expected):
                values = responses()
                values["/health"] = health
                result, _, count = self.collect(values)
                self.assertEqual(result, expected)
                self.assertEqual(count, 1)

    def test_empty_catalogue_requires_explicit_zero_active(self):
        for active, expected in ((0, "serving"), (2, "unverified"), (None, "unverified")):
            values = responses()
            latest = values["/meta"]["latest_snapshot"]
            latest["article_count"] = 2 + (active or 0)
            if active is None:
                latest.pop("active_article_count")
            else:
                latest["active_article_count"] = active
            values["/snapshots"]["data"][-1] = copy.deepcopy(latest)
            values["/articles?limit=1&structured_metadata=false"].update(
                latest_snapshot=copy.deepcopy(latest), data=[], next_after_id=None)
            self.assertEqual(self.collect(values)[0]["status"], expected)

    def test_malformed_or_contradictory_responses_are_unverified(self):
        mutations = [
            lambda v: v["/meta"].update(high_watermark_event_id=True),
            lambda v: v["/meta"]["latest_snapshot"].update(article_count=-1),
            lambda v: v["/meta"]["latest_snapshot"].update(active_article_count="2"),
            lambda v: v["/meta"]["latest_snapshot"].update(snapshot_date="2026-02-30"),
            lambda v: v["/snapshots"]["data"].reverse(),
            lambda v: v["/snapshots"]["data"].append(copy.deepcopy(LATEST)),
            lambda v: v["/snapshots"]["data"].append({"snapshot_date": "2026-11-01"}),
            lambda v: v["/snapshots"]["data"].append(None),
            lambda v: v["/articles?limit=1&structured_metadata=false"].update(high_watermark_event_id=20),
            lambda v: v["/articles?limit=1&structured_metadata=false"].update(data=[{"article_id": True, "status": "active"}]),
            lambda v: v["/articles?limit=1&structured_metadata=false"].update(data=[{"article_id": 8, "status": "removed"}]),
            lambda v: v["/articles?limit=1&structured_metadata=false"].update(next_after_id=99),
        ]
        for mutation in mutations:
            values = responses()
            mutation(values)
            result = self.collect(values)[0]
            self.assertEqual(result, {"status": "unverified", "health": "ok"})

    def test_inconsistent_snapshot_counters_are_rejected(self):
        for changes in ({"article_count": 99}, {"active_source_count": 8},
                        {"article_count": 1 << 64}, {"active_article_count": 0}):
            values = responses()
            for latest in (values["/meta"]["latest_snapshot"], values["/snapshots"]["data"][-1],
                           values["/articles?limit=1&structured_metadata=false"]["latest_snapshot"]):
                latest.update(changes)
            self.assertEqual(self.collect(values)[0]["status"], "unverified")

    def test_configuration_disabled_does_not_read_credentials_or_connect(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(api, "_bounded_probe") as network:
            self.assertEqual(api.collect_api(), {"status": "not_configured", "health": "unknown"})
        network.assert_not_called()

    def test_invalid_configuration_is_sanitized_without_network(self):
        with mock.patch.dict(os.environ, {"OJS_PROGRESS_API_BASE_URL": "http://" + SECRET}), \
                mock.patch.object(api, "_bounded_probe") as network:
            self.assertEqual(api.collect_api(), {"status": "unverified", "health": "unknown"})
        network.assert_not_called()

    def test_collect_uses_only_private_systemd_credentials_directory(self):
        with mock.patch.dict(os.environ, {"OJS_PROGRESS_API_BASE_URL": "https://catalogue.example.org/",
                                         "CREDENTIALS_DIRECTORY": self.directory.name,
                                         "OJS_API_KEY": SECRET}), \
                mock.patch.object(api, "_bounded_probe", return_value={"status": "unverified", "health": "ok"}) as probe:
            api.collect_api()
        probe.assert_called_once_with(ORIGIN, self.directory.name)


class NetworkTests(unittest.TestCase):
    def request(self, *, status=200, body=b'{"status":"ok"}', headers=None, authorization=None):
        response = mock.Mock(status=status)
        metadata = {"Content-Type": "application/json"} if headers is None else headers
        response.getheader.side_effect = lambda key, default=None: metadata.get(key, default)
        response.read.return_value = body
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(api.http.client, "HTTPSConnection", return_value=connection) as factory:
            try:
                result = api._get(ORIGIN, "/health", authorization)
            finally:
                connection.close.assert_called_once()
        return result, connection, factory

    def test_tls_verification_bounded_read_and_no_health_authorization(self):
        result, connection, factory = self.request()
        context = factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(factory.call_args.kwargs["timeout"], api.SOCKET_SECONDS)
        connection.request.assert_called_once_with("GET", "/health", headers={
            "Accept": "application/json", "Accept-Encoding": "identity"})
        connection.getresponse.return_value.read.assert_called_once_with(api.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(result, {"status": "ok"})

    def test_authenticated_http_failure_categories_and_redirect_refusal(self):
        for status, expected in ((301, "unverified"), (302, "unverified"), (307, "unverified"),
                                 (401, "auth_failed"), (403, "auth_failed"), (500, "unavailable"),
                                 (503, "unavailable"), (429, "unavailable"), (404, "unverified")):
            with self.subTest(status=status), self.assertRaises(api.ProbeError) as raised:
                self.request(status=status, authorization=SECRET)
            self.assertEqual(str(raised.exception), expected)

    def test_malformed_or_oversized_body_and_headers_refused(self):
        for body in (b"x", b"[]", b'{"status":"ok","status":"other"}', b'{"status":NaN}',
                     b"\xff", b"x" * (api.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(body)), self.assertRaises(api.ProbeError):
                self.request(body=body)
        for headers in ({}, {"Content-Type": "text/html"},
                        {"Content-Type": "application/json", "Content-Length": str(api.MAX_RESPONSE_BYTES + 1)},
                        {"Content-Type": "application/json", "Content-Length": "-1"},
                        {"Content-Type": "application/json", "Content-Encoding": "gzip"}):
            with self.subTest(headers=headers), self.assertRaises(api.ProbeError):
                self.request(headers=headers)

    def test_network_exception_detail_cannot_escape(self):
        connection = mock.Mock()
        connection.request.side_effect = OSError(SECRET)
        with mock.patch.object(api.http.client, "HTTPSConnection", return_value=connection), \
                self.assertRaises(api.ProbeError) as raised:
            api._get(ORIGIN, "/health")
        self.assertEqual(str(raised.exception), "unavailable")
        connection.close.assert_called_once()

    def test_origins_are_https_only_and_safe_for_environment_file(self):
        for raw in ("https://example.org", "https://127.0.0.1:443/", "https://[::1]:8443/",
                    "https://[2001:db8::1]", "https://api.example.org./"):
            self.assertEqual(api.validated_origin(raw), raw.rstrip("/"))
        for raw in ("http://127.0.0.1", "https://user:secret@example.org", "https://example.org/path",
                    "https://example.org?", "https://example.org#", "https://example.org\n",
                    "https://example.org:0", "https://example.org:65536", "https://exa$mple.org",
                    "https://example.org;evil", "https://exa'mple.org", "https://example.org\\evil",
                    "https://-example.org", "https://example..org", "https://[2001:db8::1%25eth0]",
                    "https://example.org:", "https://", SECRET):
            with self.subTest(raw=raw), self.assertRaises(api.ProbeError) as raised:
                api.validated_origin(raw)
            self.assertNotIn(raw, str(raised.exception))

    def test_sanitizer_discards_private_fields_and_rejects_malformed_observations(self):
        for raw in (None, [], {"status": SECRET, "health": "ok"},
                    {"status": [], "health": "ok"}, {"status": "serving", "health": "ok"}):
            self.assertEqual(api.sanitize_api(raw), {"status": "unverified", "health": "unknown"})
        self.assertEqual(api.sanitize_api({"status": "auth_failed", "health": "ok", "error": SECRET,
                                         "latest_snapshot": SECRET}), {"status": "auth_failed", "health": "ok"})
        good = {"status": "serving", "health": "ok", "latest_snapshot": "2026-10-01",
                "snapshot_dates": ["2026-10-01"], "high_watermark_event_id": 1, "private": SECRET}
        self.assertNotIn(SECRET, json.dumps(api.sanitize_api(good)))
        for value in (True, -1, "123", 1 << 64):
            self.assertEqual(api.sanitize_api({**good, "active_articles": value})["status"], "unverified")


class SystemdCredentialTests(unittest.TestCase):
    @staticmethod
    def state(mode, uid=0, gid=0):
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_dev=1, st_ino=2, st_size=109)

    @staticmethod
    def acl(permissions, uid=1001, *, group=0, other=0):
        undefined = 0xFFFFFFFF
        entries = [(1, permissions, undefined), (2, permissions, uid), (4, group, undefined),
                   (16, permissions, undefined), (32, other, undefined)]
        return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)

    def valid_layout(self, *, file=None, parent=None, ancestor=None, file_acl=None, parent_acl=None,
                     path="/run/credentials/ojs-api-progress-email.service/api-client.env"):
        file = self.state(stat.S_IFREG | 0o440) if file is None else file
        parent = self.state(stat.S_IFDIR | 0o550) if parent is None else parent
        ancestor = self.state(stat.S_IFDIR | 0o755) if ancestor is None else ancestor
        file_acl = self.acl(4) if file_acl is None else file_acl
        parent_acl = self.acl(5) if parent_acl is None else parent_acl
        with mock.patch.object(api.os, "geteuid", return_value=1001), \
                mock.patch.object(api.Path, "lstat", return_value=ancestor), \
                mock.patch.object(api.os, "open", return_value=8), \
                mock.patch.object(api.os, "fstat", return_value=parent), \
                mock.patch.object(api.os, "stat", return_value=file), \
                mock.patch.object(api.os, "getxattr", side_effect=lambda descriptor, key:
                                  file_acl if descriptor == 7 else parent_acl), \
                mock.patch.object(api.os, "close"):
            return api._systemd_private_credential(Path(path), file, 7)

    def test_exact_systemd_named_user_readonly_acl_is_private(self):
        # These are the real systemd v255 LoadCredential modes and ACL tuples;
        # group mask bits are visible as 0440/0550, but owning-group access is 0.
        self.assertTrue(self.valid_layout())

    def test_group_read_exception_is_not_available_outside_runtime_credentials(self):
        for path in ("/tmp/ojs-api-progress-email.service/api-client.env",
                     "/run/credentials/nested/unit/api-client.env",
                     "/run/credentials/ojs-api-progress-email.service/wrong.env",
                     "/run/credentials/../api-client.env"):
            with self.subTest(path=path):
                self.assertFalse(self.valid_layout(path=path))

    def test_writable_public_or_non_root_files_and_parents_are_rejected(self):
        for mode in (0o444, 0o460, 0o640, 0o441, 0o540):
            self.assertFalse(self.valid_layout(file=self.state(stat.S_IFREG | mode)))
        for mode in (0o750, 0o570, 0o555, 0o770):
            self.assertFalse(self.valid_layout(parent=self.state(stat.S_IFDIR | mode)))
        for uid, gid in ((1001, 0), (0, 1001)):
            self.assertFalse(self.valid_layout(file=self.state(stat.S_IFREG | 0o440, uid, gid)))
            self.assertFalse(self.valid_layout(parent=self.state(stat.S_IFDIR | 0o550, uid, gid)))

    def test_writable_nonroot_or_symlink_ancestors_are_rejected(self):
        for ancestor in (self.state(stat.S_IFDIR | 0o775), self.state(stat.S_IFDIR | 0o777),
                         self.state(stat.S_IFDIR | 0o755, uid=1001), self.state(stat.S_IFLNK | 0o777)):
            self.assertFalse(self.valid_layout(ancestor=ancestor))

    def test_other_users_groups_or_excess_acl_access_are_rejected(self):
        for acl in (self.acl(4, uid=2000), self.acl(4, group=4), self.acl(4, other=4),
                    self.acl(6), self.acl(4) + struct.pack("<HHI", 2, 4, 2000), b"invalid"):
            with self.subTest(length=len(acl)):
                self.assertFalse(self.valid_layout(file_acl=acl))
        self.assertFalse(self.valid_layout(parent_acl=self.acl(5, group=5)))
        self.assertFalse(self.valid_layout(parent_acl=self.acl(7)))

    def test_missing_acl_is_not_treated_as_a_private_systemd_file(self):
        with mock.patch.object(api.os, "getxattr", side_effect=OSError("not supported")):
            self.assertFalse(api._systemd_acl(7, 4))


@unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "Linux fork worker required")
class WallClockTests(unittest.TestCase):
    def test_worker_deadline_bounds_dns_and_retains_prior_health(self):
        for health in (False, True):
            def slow(origin, directory, observe):
                if health:
                    observe({"status": "unverified", "health": "ok"})
                time.sleep(5)
                return {"status": "unverified", "health": "unknown"}

            start = time.monotonic()
            with mock.patch.object(api, "REQUEST_SECONDS", 0.15), mock.patch.object(api, "_collect", side_effect=slow):
                result = api._bounded_probe(ORIGIN, "")
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(result, {"status": "unavailable", "health": "ok" if health else "unknown"})

    def test_worker_errors_return_no_exception_text(self):
        with mock.patch.object(api, "_collect", side_effect=RuntimeError(SECRET)):
            result = api._bounded_probe(ORIGIN, "")
        self.assertEqual(result, {"status": "unverified", "health": "unknown"})

    def test_successful_worker_round_trip(self):
        observation = {"status": "serving", "health": "ok", "latest_snapshot": "2026-10-01",
                       "snapshot_dates": ["2026-10-01"], "high_watermark_event_id": 19, "active_articles": 2}
        with mock.patch.object(api, "_collect", return_value=observation):
            self.assertEqual(api._bounded_probe(ORIGIN, ""), observation)


if __name__ == "__main__":
    unittest.main()
