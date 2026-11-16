"""Bounded, read-only checks that automatic builds preserve published history.

This is a metadata gate, not a full release verification. The pipeline still
verifies the large export while importing it. No data, lock or pointer is
created or changed here, and no exception includes private paths or content.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date
from pathlib import Path


MAX_METADATA_BYTES = 1024 * 1024
MARKER = "OJS_COMPACT_RELEASE.json"
_DATE = r"\d{4}-\d{2}-\d{2}"
_DATADIR = re.compile(rf"mysql-({_DATE})")
_ARTIFACT = re.compile(
    rf"(?:pkpbeacon-release-({_DATE})\.json|"
    rf"pkpbeacon-clean-({_DATE})\.sql\.gz(?:\.sha256)?|"
    rf"pkpbeacon-changes-({_DATE})\.json(?:\.sha256)?)"
)
_HASH = re.compile(r"[0-9a-f]{64}")
ERROR_MESSAGE = (
    "automatic build baseline is unsafe or incomplete; preserve existing "
    "history and review the published release before building"
)


class BaselineError(RuntimeError):
    """The published history cannot safely seed another automatic build."""


def _reject() -> None:
    raise BaselineError(ERROR_MESSAGE)


def _version(value: str) -> str:
    date.fromisoformat(value)
    return value


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_regular(path: Path, *, metadata: bool = True) -> bytes | tuple:
    """Reject links/nonfiles and avoid opening FIFOs as blocking streams."""
    named = path.lstat()
    if not stat.S_ISREG(named.st_mode) or named.st_size <= 0:
        _reject()
    if metadata and named.st_size > MAX_METADATA_BYTES:
        _reject()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(named) != _identity(opened):
            _reject()
        if metadata:
            with os.fdopen(os.dup(descriptor), "rb") as source:
                content = source.read(MAX_METADATA_BYTES + 1)
            if len(content) > MAX_METADATA_BYTES:
                _reject()
        else:
            # Opening verifies readability, but no byte of the export is read.
            content = _identity(opened)
        if _identity(opened) != _identity(os.fstat(descriptor)):
            _reject()
        if _identity(opened) != _identity(path.lstat()):
            _reject()
        return content
    finally:
        os.close(descriptor)


def _object(path: Path) -> tuple[dict, bytes]:
    content = _read_regular(path)
    value = json.loads(content)
    if not isinstance(value, dict):
        _reject()
    return value, content


def _checksum(path: Path) -> str:
    # Match the repository's sha256sum-compatible sidecar convention exactly.
    content = _read_regular(path.with_name(path.name + ".sha256")).decode("ascii").strip()
    match = re.fullmatch(r"([0-9a-f]{64})[ \t]+(?:\*)?([^\r\n]+)", content)
    if match is None or match.group(2) != path.name:
        _reject()
    return match.group(1)


def assert_automatic_baseline(clean: Path, live: Path | None) -> str | None:
    """Return the safe live snapshot, or None only for an empty-history bootstrap.

    The caller must hold the source/pipeline exclusion appropriate to its
    workflow and repeat this check immediately before launching the builder.
    A newer artifact is deliberately not auto-adopted: it may be an unpublished
    candidate requiring review. Older retained audit reports are permitted.
    Metadata files are bounded at 1 MiB each. Export contents are not hashed;
    their declared checksum must match the committed manifest. Every failure
    raises BaselineError with one fixed, non-sensitive message.
    """
    try:
        clean = Path(clean)
        if clean.is_symlink():
            _reject()
        if not clean.exists():
            if live is None:
                return None
            _reject()
        if not clean.is_dir():
            _reject()
        canonical = clean.resolve(strict=True)
        version = None
        if live is not None:
            live = Path(live)
            match = _DATADIR.fullmatch(live.name)
            if (match is None or live.is_symlink() or not live.is_dir()
                    or live.parent.resolve(strict=True) != canonical):
                _reject()
            version = _version(match.group(1))

        for entry in clean.iterdir():
            artifact = _ARTIFACT.fullmatch(entry.name)
            directory = _DATADIR.fullmatch(entry.name)
            if artifact is None and directory is None:
                continue
            discovered = _version(next(value for value in
                                       (artifact.groups() if artifact else directory.groups())
                                       if value is not None))
            if version is None or discovered > version:
                _reject()
        if version is None:
            return None

        export = clean / f"pkpbeacon-clean-{version}.sql.gz"
        report_path = clean / f"pkpbeacon-changes-{version}.json"
        manifest_path = clean / f"pkpbeacon-release-{version}.json"
        export_identity = _read_regular(export, metadata=False)
        export_hash = _checksum(export)
        report, report_bytes = _object(report_path)
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        if _checksum(report_path) != report_hash:
            _reject()
        manifest, _ = _object(manifest_path)
        expected = {
            "schema_version": 1,
            "snapshot_date": version,
            "clean_export_filename": export.name,
            "clean_export_sha256": export_hash,
            "change_report_filename": report_path.name,
            "change_report_sha256": report_hash,
        }
        if type(manifest.get("schema_version")) is not int or any(
                manifest.get(key) != value for key, value in expected.items()):
            _reject()
        marker, _ = _object(live / MARKER)
        if (type(marker.get("schema_version")) is not int
                or marker.get("schema_version") != 1
                or marker.get("snapshot_date") != version
                or marker.get("data_directory_name") != live.name
                or marker.get("clean_only") is not True):
            _reject()
        snapshot = report.get("snapshot")
        guard = report.get("release_guard")
        if (type(report.get("schema_version")) is not int
                or report.get("schema_version") != 1
                or not isinstance(snapshot, dict) or snapshot.get("date") != version
                or not isinstance(guard, dict)
                or guard.get("status") not in ("passed", "overridden")):
            _reject()
        for key in ("source_sha256", "build_sql_sha256"):
            value = marker.get(key)
            if not isinstance(value, str) or _HASH.fullmatch(value) is None or snapshot.get(key) != value:
                _reject()
        if export_identity != _read_regular(export, metadata=False):
            _reject()
        return version
    except (OSError, ValueError, TypeError, RuntimeError, StopIteration):
        raise BaselineError(ERROR_MESSAGE) from None
