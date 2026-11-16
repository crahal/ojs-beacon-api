from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_api_history as checker

DATES = ["2026-01-01", "2026-07-01", "2026-10-01"]


class FakeClient:
    def __init__(self, *, bounds=False):
        self.calls = []
        self.snapshots = [
            {"snapshot_date": day, "event_count": 2, "active_article_count": 10,
             "source_sha256": str(index + 1) * 64, "build_sql_sha256": "a" * 64}
            for index, day in enumerate(DATES)
        ]
        if bounds:
            for index, snapshot in enumerate(self.snapshots):
                snapshot.update(first_event_id=index * 10 + 1, last_event_id=index * 10 + 3)
        self.metadata = {"latest_snapshot": self.snapshots[-1], "high_watermark_event_id": 23}
        self.events = {}
        for index, day in enumerate(DATES):
            for event_id in (index * 10 + 1, index * 10 + 3):
                self.events[event_id] = {"event": {"event_id": event_id, "event_snapshot_date": day},
                                         "article": {"status": "removed"}, "current_operation": "delete"}

    def get(self, endpoint, *, authenticated=True, expected=200):
        self.calls.append((endpoint, authenticated, expected))
        if not authenticated and endpoint != "/health":
            if expected != 401:
                raise AssertionError("Unauthorized check missing expected status")
            return {}
        if endpoint == "/health":
            return {"status": "ok"}
        if endpoint == "/meta":
            return copy.deepcopy(self.metadata)
        if endpoint == "/snapshots":
            return {"data": copy.deepcopy(self.snapshots)}
        if endpoint.startswith("/articles?"):
            return {"data": [{"article_id": 1, "status": "active"}]}
        after = int(endpoint.split("after_event_id=", 1)[1].split("&", 1)[0])
        event = next((self.events[key] for key in sorted(self.events) if key > after), None)
        return {"data": [copy.deepcopy(event)] if event else []}


def reports(directory, snapshots):
    for index, snapshot in enumerate(snapshots):
        report = {"snapshot": {"date": snapshot["snapshot_date"], "source_sha256": snapshot["source_sha256"],
                               "build_sql_sha256": snapshot["build_sql_sha256"]},
                  "article_events": {"total": 2, "first_event_id": index * 10 + 1, "last_event_id": index * 10 + 3}}
        path = directory / f"pkpbeacon-changes-{snapshot['snapshot_date']}.json"
        raw = json.dumps(report).encode()
        path.write_bytes(raw)
        path.with_name(path.name + ".sha256").write_text(f"{hashlib.sha256(raw).hexdigest()}  {path.name}\n")


class HistoryTests(unittest.TestCase):
    def test_all_three_snapshots_from_reports_checked_without_scanning_events(self):
        client = FakeClient()
        with TemporaryDirectory() as directory:
            reports(Path(directory), client.snapshots)
            result = checker.verify(client, DATES, Path(directory))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["snapshots"]), 3)
        changes = [endpoint for endpoint, auth, _ in client.calls if auth and endpoint.startswith("/changes")]
        self.assertEqual(len(changes), 6)
        self.assertTrue(all("limit=1" in endpoint and "structured_metadata=false" in endpoint for endpoint in changes))
        self.assertLess(len(client.calls), 25)
        self.assertIn("current state", result["article_payload"])

    def test_missing_bounds_is_explicitly_incomplete_without_history_scan(self):
        client = FakeClient()
        result = checker.verify(client, DATES)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_event_boundaries"], DATES)
        self.assertFalse(any(auth and endpoint.startswith("/changes") for endpoint, auth, _ in client.calls))

    def test_api_event_bounds_allow_check_without_local_reports(self):
        self.assertEqual(checker.verify(FakeClient(bounds=True), DATES)["status"], "passed")

    def test_incorrect_date_boundary_missing_event_and_operation_fail(self):
        for mutation in ("date", "missing", "operation", "id"):
            with self.subTest(mutation=mutation):
                client = FakeClient(bounds=True)
                if mutation == "date":
                    client.events[11]["event"]["event_snapshot_date"] = DATES[0]
                elif mutation == "missing":
                    del client.events[11]
                elif mutation == "operation":
                    client.events[11]["current_operation"] = "upsert"
                else:
                    client.events[11]["event"]["event_id"] = 12
                with self.assertRaises(checker.CheckError):
                    checker.verify(client, DATES)

    def test_missing_snapshot_wrong_latest_and_watermark_fail(self):
        for mutation in ("snapshots", "latest", "watermark"):
            with self.subTest(mutation=mutation):
                client = FakeClient(bounds=True)
                if mutation == "snapshots":
                    client.snapshots.pop(1)
                elif mutation == "latest":
                    client.metadata["latest_snapshot"] = client.snapshots[0]
                else:
                    client.metadata["high_watermark_event_id"] = 24
                with self.assertRaises(checker.CheckError):
                    checker.verify(client, DATES)

    def test_zero_events_valid_and_does_not_fabricate_event_probe(self):
        client = FakeClient(bounds=True)
        client.snapshots[1].update(event_count=0, first_event_id=None, last_event_id=None)
        client.events.pop(11)
        client.events.pop(13)
        result = checker.verify(client, DATES)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["snapshots"][1]["event_count"], 0)

    def test_duplicate_or_unbounded_expected_history_fails(self):
        for dates in ([], DATES + [DATES[0]], ["2026-01-01"] * 33):
            with self.assertRaises(checker.CheckError):
                checker.verify(FakeClient(), dates)

    def test_tampered_report_and_wrong_provenance_fail(self):
        client = FakeClient()
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            reports(directory, client.snapshots)
            path = directory / "pkpbeacon-changes-2026-07-01.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(checker.CheckError, "checksum"):
                checker.verify(client, DATES, directory)
            reports(directory, client.snapshots)
            client.snapshots[1]["source_sha256"] = "f" * 64
            with self.assertRaisesRegex(checker.CheckError, "provenance"):
                checker.verify(client, DATES, directory)

    def test_release_change_during_checks_is_detected(self):
        client = FakeClient(bounds=True)
        original = client.get
        meta_calls = 0

        def changed(endpoint, **kwargs):
            nonlocal meta_calls
            result = original(endpoint, **kwargs)
            if endpoint == "/meta" and kwargs.get("authenticated", True):
                meta_calls += 1
                if meta_calls == 2:
                    result["high_watermark_event_id"] += 1
            return result

        client.get = changed
        with self.assertRaisesRegex(checker.CheckError, "changed during"):
            checker.verify(client, DATES)


class SecurityTests(unittest.TestCase):
    def test_https_or_loopback_only_no_embedded_secrets_or_redirects(self):
        for value in ("https://203.0.113.10", "http://127.0.0.1:8000/", "http://[::1]:8000"):
            self.assertEqual(checker.origin(value), value.rstrip("/"))
        for value in ("http://203.0.113.10", "https://user:key@example.org", "https://example.org/a",
                      "https://example.org?key=secret", "https://example.org#fragment", "https://example.org:0",
                      "https://example.org\n", "https://example.org:bad"):
            with self.assertRaises(checker.CheckError):
                checker.origin(value)
        with self.assertRaises(checker.CheckError):
            checker.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_private_credential_parser_rejects_symlinks_permissions_and_duplicates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "client.env"
            path.write_text("OJS_API_USERNAME=test-user\nOJS_API_KEY=test-key\n")
            path.chmod(0o600)
            self.assertEqual(checker.credentials(path), ("test-user", "test-key"))
            path.chmod(0o644)
            with self.assertRaises(checker.CheckError):
                checker.credentials(path)
            path.chmod(0o600)
            alias = path.with_name("alias")
            alias.symlink_to(path)
            with self.assertRaises(checker.CheckError):
                checker.credentials(alias)
            with path.open("a") as target:
                target.write("OJS_API_KEY=another\n")
            with self.assertRaises(checker.CheckError):
                checker.credentials(path)

    def test_request_has_bounded_timeout_and_no_header_for_unauthorized_check(self):
        with mock.patch.object(checker, "build_opener") as factory:
            client = checker.Client("https://example.org", ("user", "key"), 12)
        response = factory.return_value.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"status":"ok"}'
        client.get("/health", authenticated=False)
        args, kwargs = client.opener.open.call_args
        self.assertNotIn("Authorization", args[0].headers)
        self.assertEqual(kwargs, {"timeout": 12})
        response.read.assert_called_once_with(checker.MAX_BYTES + 1)
        response.read.return_value = b"x" * (checker.MAX_BYTES + 1)
        with self.assertRaises(checker.CheckError):
            client.get("/meta")

    def test_unauthorized_expected_status_does_not_read_error_body(self):
        with mock.patch.object(checker, "build_opener"):
            client = checker.Client("https://example.org", ("user", "key"), 12)
        failure = HTTPError("https://example.org", 401, "private server detail", {}, io.BytesIO(b"private"))
        client.opener.open.side_effect = failure
        self.assertEqual(client.get("/meta", authenticated=False, expected=401), {})
        self.assertTrue(failure.fp.closed)

    def test_unexpected_error_details_and_secret_are_never_printed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "client.env"
            path.write_text("OJS_API_USERNAME=test-user\nOJS_API_KEY=test-key\n")
            path.chmod(0o600)
            errors = io.StringIO()
            with mock.patch.object(checker.Client, "get", side_effect=RuntimeError("test-key private")), contextlib.redirect_stderr(errors):
                status = checker.main(["--base-url", "https://example.org", "--credentials-file", str(path),
                                       "--snapshot", DATES[0]])
            self.assertEqual(status, 1)
            self.assertNotIn("test-key", errors.getvalue())
            self.assertNotIn("RuntimeError", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
