"""Private, reloadable credentials for high-entropy issued API client keys."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from datetime import datetime, timezone


MAX_REGISTRY_BYTES = 128 * 1024
MAX_CLIENTS = 256
MAX_USERNAME_LENGTH = 64
MAX_KEY_LENGTH = 512
USERNAME_RE = re.compile(r"[A-Za-z0-9_.@+-]{1,64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
EXPIRY_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")
REGISTRY_ERROR = "API client registry unavailable"


class RegistryError(ValueError):
    """A fixed diagnostic which never includes file paths or registry content."""


def _expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not EXPIRY_RE.fullmatch(value):
        raise RegistryError(REGISTRY_ERROR)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RegistryError(REGISTRY_ERROR) from None


def validate_registry(document: object, *, legacy_username: str | None = None) -> dict:
    """Validate the exact version-one format without retaining any raw keys."""
    if (not isinstance(document, dict) or set(document) != {"version", "clients"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["clients"], list)
            or len(document["clients"]) > MAX_CLIENTS):
        raise RegistryError(REGISTRY_ERROR)
    names = set()
    digests = set()
    for client in document["clients"]:
        if (not isinstance(client, dict)
                or set(client) != {"username", "key_sha256", "enabled", "expires_at"}):
            raise RegistryError(REGISTRY_ERROR)
        username, digest = client["username"], client["key_sha256"]
        if (not isinstance(username, str) or not USERNAME_RE.fullmatch(username)
                or username in names or username == legacy_username
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
                or digest in digests
                or type(client["enabled"]) is not bool):
            raise RegistryError(REGISTRY_ERROR)
        _expiry(client["expires_at"])
        names.add(username)
        digests.add(digest)
    return document


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError(REGISTRY_ERROR)
        value[key] = item
    return value


def _reject_constant(value):
    raise RegistryError(REGISTRY_ERROR)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_parent(path: Path) -> tuple[int, str]:
    """Anchor the complete path without following directory symlinks."""
    if ".." in path.parts or not path.name:
        raise RegistryError(REGISTRY_ERROR)
    selected = path if path.is_absolute() else Path.cwd() / path
    descriptor = os.open(selected.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in selected.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor, selected.name
    except BaseException:
        os.close(descriptor)
        raise


def read_registry(path: Path, *, legacy_username: str | None = None) -> dict:
    """Read one bounded, private file; reopen each time to observe replacement."""
    descriptor = None
    directory = None
    try:
        path = Path(path)
        directory, name = _open_parent(path)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o077 or before.st_uid not in (0, os.geteuid())
                or not 0 < before.st_size <= MAX_REGISTRY_BYTES):
            raise RegistryError(REGISTRY_ERROR)
        payload = bytearray()
        while chunk := os.read(descriptor, MAX_REGISTRY_BYTES + 1 - len(payload)):
            payload.extend(chunk)
            if len(payload) > MAX_REGISTRY_BYTES:
                raise RegistryError(REGISTRY_ERROR)
        if (_identity(before) != _identity(os.fstat(descriptor))
                or _identity(before) != _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))):
            raise RegistryError(REGISTRY_ERROR)
        document = json.loads(payload, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        return validate_registry(document, legacy_username=legacy_username)
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        raise RegistryError(REGISTRY_ERROR) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def authenticate_client(path: Path, username: str, key: str, *,
                        legacy_username: str | None = None,
                        now: datetime | None = None) -> bool:
    """Check a bounded ASCII credential against a freshly read registry."""
    if (not isinstance(username, str) or not isinstance(key, str)
            or not USERNAME_RE.fullmatch(username)
            or not 1 <= len(key) <= MAX_KEY_LENGTH or not key.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in key)):
        return False
    registry = read_registry(path, legacy_username=legacy_username)
    current = datetime.now(timezone.utc) if now is None else now
    supplied = hashlib.sha256(key.encode("ascii")).hexdigest()
    accepted = False
    for client in registry["clients"]:
        matches_name = secrets.compare_digest(username, client["username"])
        matches_key = secrets.compare_digest(supplied, client["key_sha256"])
        expires = _expiry(client["expires_at"])
        allowed = client["enabled"] and (expires is None or current < expires)
        accepted = (matches_name and matches_key and allowed) or accepted
    return accepted
