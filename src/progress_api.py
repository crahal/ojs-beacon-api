"""Bounded, read-only public API observations for operational reports.

Only closed status values, validated dates and integer catalogue counters leave
the disposable worker. Response bodies, URLs, credentials and article contents
are never returned or logged. This is a small sample, not a full data audit.
"""
from __future__ import annotations

import base64
from datetime import date
import http.client
import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import re
import ssl
import stat
import struct
import time
from urllib.parse import urlsplit


REQUEST_SECONDS = 25.0
SOCKET_SECONDS = 8.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CREDENTIAL_BYTES = 4096
MAX_MESSAGE_BYTES = 16 * 1024
MAX_SNAPSHOTS = 512
_INTEGER_MAX = (1 << 64) - 1
_VALUE = re.compile(r"[A-Za-z0-9_.@+-]{1,1024}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_COUNTERS = {
    "active_articles": "active_article_count",
    "total_articles": "article_count",
    "removed_articles": "removed_article_count",
    "merged_articles": "merged_article_count",
    "active_sources": "active_source_count",
    "source_records": "source_record_count",
}
_STATUSES = frozenset({"serving", "unavailable", "auth_failed", "unverified", "not_configured"})
_HEALTH = frozenset({"ok", "failed", "unknown"})


class ProbeError(Exception):
    """An exception whose text is deliberately limited to fixed vocabulary."""

    def __init__(self, status: str = "unverified", *, health: str = "unknown"):
        self.status = status if status in _STATUSES else "unverified"
        self.health = health if health in _HEALTH else "unknown"
        super().__init__(self.status)


def _origin(raw: str) -> tuple[str, int | None]:
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise ProbeError()
    if any(ord(char) <= 32 or ord(char) >= 127 for char in raw):
        raise ProbeError()
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or "?" in raw or "#" in raw or "\\" in raw
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        if ":" in parsed.hostname:
            ipaddress.IPv6Address(parsed.hostname)
            if "%" in parsed.hostname or not re.fullmatch(r"\[[0-9A-Fa-f: .]+\](?::[0-9]+)?", parsed.netloc):
                raise ValueError
        else:
            if (len(parsed.hostname) > 253
                    or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
                    or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                           for label in parsed.hostname.rstrip(".").split("."))):
                raise ValueError
        return parsed.hostname, parsed.port
    except ValueError:
        raise ProbeError() from None


def validated_origin(raw: str) -> str:
    """Validate an HTTPS origin for private configuration; errors contain no URL."""
    _origin(raw)
    return raw.rstrip("/")


def _systemd_acl(descriptor: int, permissions: int) -> bool:
    """Recognize only systemd's root + one service-user read-only Linux ACL.

    An ACL's mask occupies stat's group bits; a systemd-provided 0440 file
    therefore need not grant its owning group any read access. Do not relax
    normal private-file checks for arbitrary group-readable credentials.
    """
    try:
        raw = os.getxattr(descriptor, "system.posix_acl_access")
    except OSError:
        return False
    if len(raw) != 44 or struct.unpack_from("<I", raw)[0] != 2:
        return False
    entries = [struct.unpack_from("<HHI", raw, offset) for offset in range(4, len(raw), 8)]
    undefined = 0xFFFFFFFF
    expected = [(1, permissions, undefined), (2, permissions, os.geteuid()),
                (4, 0, undefined), (16, permissions, undefined), (32, 0, undefined)]
    return sorted(entries) == sorted(expected)


def _systemd_private_credential(path: Path, info, descriptor: int) -> bool:
    """Accept the tightly scoped ACL-backed LoadCredential runtime layout."""
    if (info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o440
            or len(path.parts) != 5 or path.parts[:3] != ("/", "run", "credentials")
            or path.name != "api-client.env" or path.parent.name in (".", "..")):
        return False
    # /run/credentials is system-manager owned. Check each component rather
    # than trusting an environment variable or following a symbolic link.
    for ancestor in (Path("/"), Path("/run"), Path("/run/credentials")):
        state = ancestor.lstat()
        if not stat.S_ISDIR(state.st_mode) or state.st_uid != 0 or state.st_mode & 0o022:
            return False
    parent = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        state = os.fstat(parent)
        if (state.st_uid != 0 or state.st_gid != 0 or stat.S_IMODE(state.st_mode) != 0o550
                or not _systemd_acl(parent, 5) or not _systemd_acl(descriptor, 4)):
            return False
        opened = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        return (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino)
    finally:
        os.close(parent)


def _credentials(directory: str) -> str:
    if not directory or not Path(directory).is_absolute():
        raise ProbeError()
    path = Path(directory) / "api-client.env"
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CREDENTIAL_BYTES:
            raise ProbeError()
        private = not (info.st_mode & 0o077) and info.st_uid in (0, os.geteuid())
        if not private and not _systemd_private_credential(path, info, descriptor):
            raise ProbeError()
        raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise ProbeError()
    values = {}
    for line in raw.decode("ascii").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (not separator or key not in {"OJS_API_USERNAME", "OJS_API_KEY"}
                or key in values or _VALUE.fullmatch(value) is None):
            raise ProbeError()
        values[key] = value
    if set(values) != {"OJS_API_USERNAME", "OJS_API_KEY"}:
        raise ProbeError()
    pair = values["OJS_API_USERNAME"] + ":" + values["OJS_API_KEY"]
    return "Basic " + base64.b64encode(pair.encode("ascii")).decode("ascii")


def _object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ProbeError()
        output[key] = value
    return output


def _bad_constant(_):
    raise ProbeError()


def _get(origin: tuple[str, int | None], endpoint: str, authorization: str | None = None) -> dict:
    # http.client ignores proxy environment variables. Do not follow redirects
    # or turn off TLS verification, even when a server has changed address.
    connection = http.client.HTTPSConnection(
        origin[0], port=origin[1], timeout=SOCKET_SECONDS, context=ssl.create_default_context())
    try:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if authorization is not None:
            headers["Authorization"] = authorization
        connection.request("GET", endpoint, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            if authorization is not None and response.status in (401, 403):
                status = "auth_failed"
            elif response.status >= 500 or response.status == 429:
                status = "unavailable"
            else:
                status = "unverified"
            raise ProbeError(status, health="failed")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ProbeError()
        if response.getheader("Content-Encoding", "identity").strip().lower() != "identity":
            raise ProbeError()
        length = response.getheader("Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length)
                                   or int(length) > MAX_RESPONSE_BYTES):
            raise ProbeError()
        encoded = response.read(MAX_RESPONSE_BYTES + 1)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ProbeError()
        result = json.loads(encoded.decode("utf-8"), object_pairs_hook=_object, parse_constant=_bad_constant)
        if not isinstance(result, dict):
            raise ProbeError()
        return result
    except ProbeError:
        raise
    except (OSError, http.client.HTTPException):
        raise ProbeError("unavailable") from None
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError() from None
    finally:
        connection.close()


def _number(value, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _INTEGER_MAX:
        raise ProbeError()
    return value


def _snapshot_date(value) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ProbeError()
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ProbeError() from None
    return value


def _catalogue(meta: dict, snapshots: dict, articles: dict) -> dict:
    latest = meta.get("latest_snapshot")
    entries = snapshots.get("data")
    if not isinstance(latest, dict) or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SNAPSHOTS:
        raise ProbeError()
    latest_date = _snapshot_date(latest.get("snapshot_date"))
    dates = [_snapshot_date(row.get("snapshot_date")) if isinstance(row, dict) else None for row in entries]
    if None in dates or dates != sorted(set(dates)) or latest_date != dates[-1]:
        raise ProbeError()
    watermark = _number(meta.get("high_watermark_event_id"))
    output = {"latest_snapshot": latest_date, "snapshot_dates": dates,
              "high_watermark_event_id": watermark}
    for public, field in _COUNTERS.items():
        if field in latest:
            output[public] = _number(latest[field])
            if _number(entries[-1].get(field)) != output[public]:
                raise ProbeError()
    if all(key in output for key in ("total_articles", "active_articles", "removed_articles", "merged_articles")):
        if output["total_articles"] != output["active_articles"] + output["removed_articles"] + output["merged_articles"]:
            raise ProbeError()
    if "source_records" in output and "active_sources" in output and output["active_sources"] > output["source_records"]:
        raise ProbeError()
    # Metadata appears in the article response as well: publication changing
    # under these requests is inconclusive, never a false availability claim.
    article_meta = articles.get("latest_snapshot")
    if (not isinstance(article_meta, dict) or article_meta != latest
            or _number(articles.get("high_watermark_event_id")) != watermark):
        raise ProbeError()
    records = articles.get("data")
    if not isinstance(records, list) or len(records) > 1 or "next_after_id" not in articles:
        raise ProbeError()
    cursor = articles["next_after_id"]
    if records:
        record = records[0]
        if not isinstance(record, dict) or record.get("status") != "active":
            raise ProbeError()
        identifier = _number(record.get("article_id"), minimum=1)
        if output.get("active_articles") == 0:
            raise ProbeError()
        if cursor is not None and _number(cursor, minimum=1) != identifier:
            raise ProbeError()
    elif output.get("active_articles") != 0 or cursor is not None:
        raise ProbeError()
    return output


def _collect(origin, directory, observe) -> dict:
    result = {"status": "unverified", "health": "unknown"}
    try:
        health = _get(origin, "/health")
    except ProbeError as error:
        return {"status": error.status, "health": error.health}
    if health.get("status") != "ok":
        return {"status": "unverified", "health": "failed"}
    result["health"] = "ok"
    observe(result)
    try:
        authorization = _credentials(directory)
    except Exception:
        return result
    try:
        meta = _get(origin, "/meta", authorization)
        snapshots = _get(origin, "/snapshots", authorization)
        articles = _get(origin, "/articles?limit=1&structured_metadata=false", authorization)
        result.update(_catalogue(meta, snapshots, articles))
        result["status"] = "serving"
    except ProbeError as error:
        result["status"] = error.status
    except Exception:
        result["status"] = "unverified"
    return result


def _network_worker(sender, origin, directory) -> None:
    def send(result, *, done=False):
        sender.send_bytes((b"F" if done else b"P") + json.dumps(result, separators=(",", ":")).encode("ascii"))
    try:
        send(_collect(origin, directory, send), done=True)
    except BaseException:
        try:
            send({"status": "unverified", "health": "unknown"}, done=True)
        except BaseException:
            pass
    finally:
        sender.close()


def _sanitize(result) -> dict:
    """Reapply the output allowlist at the worker boundary."""
    if not isinstance(result, dict) or result.get("status") not in _STATUSES or result.get("health") not in _HEALTH:
        raise ProbeError()
    output = {"status": result["status"], "health": result["health"]}
    if result["status"] == "serving":
        if result["health"] != "ok":
            raise ProbeError()
        output["latest_snapshot"] = _snapshot_date(result.get("latest_snapshot"))
        dates = result.get("snapshot_dates")
        if not isinstance(dates, list) or not 1 <= len(dates) <= MAX_SNAPSHOTS:
            raise ProbeError()
        dates = [_snapshot_date(value) for value in dates]
        if dates != sorted(set(dates)) or dates[-1] != output["latest_snapshot"]:
            raise ProbeError()
        output["snapshot_dates"] = dates
        output["high_watermark_event_id"] = _number(result.get("high_watermark_event_id"))
        for key in _COUNTERS:
            if key in result:
                output[key] = _number(result[key])
    return output


def sanitize_api(raw) -> dict:
    """Strict report/LLM allowlist, including for loaded or malformed state."""
    try:
        return _sanitize(raw)
    except Exception:
        return {"status": "unverified", "health": "unknown"}


def _bounded_probe(origin, directory) -> dict:
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_network_worker, args=(sender, origin, directory), daemon=True)
    result = {"status": "unavailable", "health": "unknown"}
    started = False
    deadline = time.monotonic() + REQUEST_SECONDS
    try:
        process.start()
        started = True
        sender.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                return {"status": "unavailable", "health": result["health"]}
            encoded = receiver.recv_bytes(MAX_MESSAGE_BYTES)
            if time.monotonic() > deadline:
                return {"status": "unavailable", "health": result["health"]}
            if encoded[:1] not in (b"P", b"F"):
                raise ProbeError()
            result = _sanitize(json.loads(encoded[1:]))
            if encoded.startswith(b"F"):
                return result
    except Exception:
        return {"status": "unverified", "health": result["health"]}
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.kill()
            process.join(timeout=1)
            if not process.is_alive():
                process.close()


def collect_api() -> dict:
    """Return a bounded public-HTTPS observation; never raise a remote error."""
    raw = os.environ.get("OJS_PROGRESS_API_BASE_URL", "")
    if not raw:
        return {"status": "not_configured", "health": "unknown"}
    try:
        origin = _origin(raw)
        return _bounded_probe(origin, os.environ.get("CREDENTIALS_DIRECTORY", ""))
    except Exception:
        return {"status": "unverified", "health": "unknown"}
