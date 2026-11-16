"""Small, nonsecret processing-start receipts shared by reporting and its watcher.

The watcher owns the marker; each recipient's normal SMTP-success state owns the
receipt. A polling restart must never turn one recipient's failure into another
recipient's duplicate email.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import stat


NOTICE_ENV = "OJS_PROGRESS_START_NOTICE_FILE"
RECEIPT_FIELD = "processing_start_notice"
MAX_NOTICE_BYTES = 4096
INVOCATION = re.compile(r"[0-9a-f]{32}\Z")


def snapshot_date(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def sanitize_notice(value: object) -> dict | None:
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1:
        return None
    snapshot, invocation = snapshot_date(value.get("snapshot_date")), value.get("invocation")
    started = timestamp(value.get("started_at"))
    if (snapshot is None or not isinstance(invocation, str)
            or not INVOCATION.fullmatch(invocation) or invocation == "0" * 32 or started is None):
        return None
    return {"schema": 1, "snapshot_date": snapshot, "invocation": invocation,
            "started_at": started.isoformat(timespec="seconds")}


def notice_token(notice: object) -> str | None:
    safe = sanitize_notice(notice)
    return f"{safe['snapshot_date']}:{safe['invocation']}" if safe else None


def valid_receipt(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    snapshot, separator, invocation = value.partition(":")
    if (not separator or snapshot_date(snapshot) is None or not INVOCATION.fullmatch(invocation)
            or invocation == "0" * 32):
        return None
    return value


def pending_notice(notice: object, previous: dict | None) -> bool:
    token = notice_token(notice)
    return bool(token and (previous or {}).get(RECEIPT_FIELD) != token)


def read_notice(path: Path, *, expected_uid: int = 0) -> dict | None:
    """Accept only a bounded, root-owned marker beneath a root-owned directory.

    The containing directory is deliberately 0755: the marker has no secrets,
    and the unprivileged email unit needs to read it without a new credential.
    It may not be writable by that unit or by another local account.
    """
    descriptor = None
    try:
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != expected_uid
                or parent.st_mode & 0o022):
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
                or info.st_mode & 0o022 or info.st_size > MAX_NOTICE_BYTES):
            return None
        raw = os.read(descriptor, MAX_NOTICE_BYTES + 1)
        if len(raw) > MAX_NOTICE_BYTES:
            return None
        return sanitize_notice(json.loads(raw))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def configured_notice() -> dict | None:
    value = os.environ.get(NOTICE_ENV)
    return read_notice(Path(value)) if value and Path(value).is_absolute() else None


def notice_line(notice: object) -> str | None:
    safe = sanitize_notice(notice)
    if not safe:
        return None
    return (f"Processing-start notice: snapshot {safe['snapshot_date']} began SQL import "
            f"at {safe['started_at']}. The current API and processing status is reported below.")
