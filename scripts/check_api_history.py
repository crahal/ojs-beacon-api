#!/usr/bin/env python3
"""Read-only, bounded smoke check of a published catalogue and snapshot history.

Credentials are read privately, never placed in argv or printed. This checks
history boundaries, not every event: full database validation precedes publish.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 1024 * 1024
KEYS = {"OJS_API_USERNAME", "OJS_API_KEY"}
VALUE = re.compile(r"[A-Za-z0-9_.@+-]+\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class CheckError(RuntimeError):
    """Messages contain fixed diagnostics only, never remote/private content."""


def snapshot_date(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a valid YYYY-MM-DD date") from None
    return value


def origin(value: str) -> str:
    try:
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or "?" in value or "#" in value):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError
    except ValueError:
        raise CheckError("Use an HTTPS origin, or HTTP on literal loopback/localhost, without path or credentials.") from None
    return value.rstrip("/")


def read_file(path: Path, *, private: bool = False, limit: int = MAX_BYTES) -> bytes:
    path = path.absolute()
    if path.resolve(strict=True) != path:
        raise CheckError("Input files and their ancestors must not be symlinks.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CheckError("Inputs must be regular files.")
        if private and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
            raise CheckError("Credentials must be owner-private (0600 or stricter).")
        value = source.read(limit + 1)
    if len(value) > limit:
        raise CheckError("An input exceeds the bounded size limit.")
    return value


def credentials(path: Path) -> tuple[str, str]:
    values = {}
    for raw in read_file(path, private=True, limit=65536).decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in KEYS or key in values or not VALUE.fullmatch(value):
            raise CheckError("API credential file is invalid.")
        values[key] = value
    if values.keys() != KEYS:
        raise CheckError("API credential file is incomplete.")
    return values["OJS_API_USERNAME"], values["OJS_API_KEY"]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CheckError("The API check refused an HTTP redirect.")


class Client:
    def __init__(self, base: str, identity: tuple[str, str], timeout: float):
        self.base = origin(base)
        self.timeout = timeout
        self.authorization = "Basic " + base64.b64encode(":".join(identity).encode("ascii")).decode("ascii")
        # Normal HTTPS certificate verification remains enabled. Do not pass
        # private credentials through an ambient HTTP(S)_PROXY configuration.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(self, endpoint: str, *, authenticated: bool = True, expected: int = 200) -> dict:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = self.authorization
        request = Request(self.base + endpoint, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != expected:
                    raise CheckError("API returned an unexpected HTTP status.")
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as error:
            try:
                if error.code == expected and expected != 200:
                    return {}
            finally:
                error.close()
            raise CheckError("API returned an unexpected HTTP status.") from None
        if len(raw) > MAX_BYTES:
            raise CheckError("API response exceeds the bounded size limit.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise CheckError("API returned an invalid JSON object.")
        return result


def number(value, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CheckError("API/report contains an invalid numeric field.")
    return value


def event_bounds(snapshot: dict, reports: Path | None) -> tuple[int | None, int | None] | None:
    count = number(snapshot["event_count"])
    if reports is not None:
        path = reports / f"pkpbeacon-changes-{snapshot['snapshot_date']}.json"
        raw = read_file(path)
        sidecar = read_file(path.with_name(path.name + ".sha256"), limit=4096).decode("ascii").split()
        if (len(sidecar) != 2 or not HASH.fullmatch(sidecar[0])
                or sidecar[1].lstrip("*") != path.name
                or hashlib.sha256(raw).hexdigest() != sidecar[0]):
            raise CheckError("Change-report checksum validation failed.")
        report = json.loads(raw)
        provenance = report["snapshot"]
        if (provenance["date"] != snapshot["snapshot_date"] or any(
            provenance[key] != snapshot[key] for key in ("source_sha256", "build_sql_sha256")
        )):
            raise CheckError("Change-report provenance differs from the published snapshot.")
        events = report["article_events"]
        if number(events["total"]) != count:
            raise CheckError("Change-report event count differs from the published snapshot.")
    else:
        events = snapshot
        if "first_event_id" not in events or "last_event_id" not in events:
            return None
    first, last = events["first_event_id"], events["last_event_id"]
    if count == 0:
        if first is not None or last is not None:
            raise CheckError("An empty snapshot has nonempty event boundaries.")
    elif number(first, minimum=1) > number(last, minimum=1) or last - first + 1 < count:
        raise CheckError("Snapshot event boundaries are inconsistent.")
    return first, last


def verify(client: Client, expected_dates: list[str], reports: Path | None = None) -> dict:
    if not 1 <= len(expected_dates) <= 32 or len(set(expected_dates)) != len(expected_dates):
        raise CheckError("Specify between one and 32 distinct expected snapshot dates.")
    expected_dates = sorted(expected_dates)
    if client.get("/health", authenticated=False).get("status") != "ok":
        raise CheckError("Public health check did not report ready.")
    for endpoint in ("/", "/meta", "/snapshots", "/articles", "/changes", "/oai", "/articles/1", "/articles/1/sources"):
        client.get(endpoint, authenticated=False, expected=401)
    metadata = client.get("/meta")
    rows = client.get("/snapshots").get("data")
    if not isinstance(rows, list) or [row.get("snapshot_date") for row in rows] != expected_dates:
        raise CheckError("Published snapshot history does not exactly match the requested dates.")
    latest = metadata["latest_snapshot"]
    if latest != rows[-1]:
        raise CheckError("Latest snapshot metadata disagrees with retained snapshot history.")
    watermark = number(metadata["high_watermark_event_id"])
    article_page = client.get("/articles?limit=1&structured_metadata=false&include_metadata_xml=false")
    articles = article_page.get("data")
    if (not isinstance(articles, list) or len(articles) > 1
            or bool(articles) != bool(number(latest["active_article_count"]))
            or any(article.get("status") != "active" for article in articles)):
        raise CheckError("Latest active article sample is inconsistent with snapshot metadata.")
    checked, missing, previous_last = [], [], 0
    for snapshot in rows:
        bounds = event_bounds(snapshot, reports)
        if bounds is None:
            missing.append(snapshot["snapshot_date"])
            continue
        first, last = bounds
        if first is not None:
            if first <= previous_last or last > watermark:
                raise CheckError("Snapshot event ranges overlap or exceed the API watermark.")
            for event_id in sorted({first, last}):
                payload = client.get(f"/changes?after_event_id={event_id - 1}&limit=1&structured_metadata=false&include_metadata_xml=false")
                events = payload.get("data")
                if not isinstance(events, list) or len(events) != 1:
                    raise CheckError("An expected history boundary event is missing.")
                item = events[0]
                event = item["event"]
                if (event["event_id"] != event_id or event["event_snapshot_date"] != snapshot["snapshot_date"]):
                    raise CheckError("A history boundary event has the wrong ID or snapshot date.")
                article_status = item["article"]["status"]
                if (article_status not in ("active", "removed", "merged") or item["current_operation"] != (
                    "upsert" if article_status == "active" else "delete"
                )):
                    raise CheckError("A change event's current-state operation is inconsistent.")
            previous_last = last
        checked.append({"snapshot_date": snapshot["snapshot_date"], "event_count": snapshot["event_count"],
                        "first_event_id": first, "last_event_id": last})
    if not missing and previous_last != watermark:
        raise CheckError("The final history boundary differs from the API watermark.")
    if client.get("/meta") != metadata:
        raise CheckError("The published release changed during checking; repeat against a stable release.")
    return {"status": "incomplete" if missing else "passed", "latest_snapshot": expected_dates[-1],
            "snapshots": checked, "missing_event_boundaries": missing,
            "high_watermark_event_id": watermark,
            "scope": "bounded API smoke test; not an exhaustive event audit",
            "article_payload": "current state, not historical before/after field values"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", required=True)
    result.add_argument("--credentials-file", type=Path, required=True)
    result.add_argument("--snapshot", action="append", type=snapshot_date, required=True)
    result.add_argument("--reports-dir", type=Path,
                        help="directory of checksum-verified per-snapshot change reports; required when API snapshot rows lack event boundaries")
    result.add_argument("--timeout", type=float, default=15)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 120:
            raise CheckError("Timeout must be between one and 120 seconds.")
        base = origin(args.base_url)
        client = Client(base, credentials(args.credentials_file), args.timeout)
        report = verify(client, args.snapshot, args.reports_dir)
        print(json.dumps(report, sort_keys=True))
        return 2 if report["status"] == "incomplete" else 0
    except CheckError as error:
        print(f"error: {error}", file=sys.stderr)
    except Exception:
        # URLs, remote bodies and exception details can contain private data.
        print("error: API history check failed; inspect connectivity, private credentials, report files and release readiness.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
