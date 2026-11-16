#!/usr/bin/env python3
"""Manage a private, hash-only API client registry offline; never print keys."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from api_clients import MAX_REGISTRY_BYTES, validate_registry


MAX_INVITATION_BYTES = 128 * 1024
KEY_PLACEHOLDER = "[PRIVATE_KEY_SHARING_LINK]"


class ManagementError(ValueError):
    """A deliberately non-sensitive operator error."""


class CommittedError(ManagementError):
    """A rename succeeded but subsequent durability confirmation failed."""

    def __init__(self, identity):
        super().__init__("An update may have committed; inspect private files before retrying.")
        self.identity = identity


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private_file(info, limit):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > limit):
        raise ManagementError("A private file is unsafe; require owner-only mode 0600 and one regular file.")


@contextmanager
def private_parent(path):
    """Anchor operations to an owner-private parent, following no symlinks.

    Ancestors need not be private (for example a shared workstation directory),
    but the immediate parent must be owned by this user and mode 0700.
    """
    path = Path(path)
    if ".." in path.parts or not path.name or path.name in {".", ".."}:
        raise ManagementError("A file path is invalid.")
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ManagementError("Private file parents must be owner-owned mode 0700 directories.")
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def read_private(directory, name, limit):
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    _private_file(before, limit)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        _private_file(opened, limit)
        if _identity(opened) != _identity(before):
            raise ManagementError("A private file changed during verification.")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (size > limit or size != after.st_size or _identity(after) != _identity(opened)
                or _identity(current) != _identity(opened)):
            raise ManagementError("A private file changed during verification.")
        return b"".join(chunks), _identity(opened)
    finally:
        os.close(descriptor)


def _current_identity(directory, name):
    try:
        return _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _remove_owned(directory, name, identity):
    if identity is not None and _current_identity(directory, name) == identity:
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)


def _write_all(descriptor, payload):
    remaining = memoryview(payload)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise OSError("Private write failed")
        remaining = remaining[count:]


def exclusive_key_output(directory, name, payload):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(directory)
        return _identity(os.fstat(descriptor))
    except BaseException:
        _remove_owned(directory, name, _identity(os.fstat(descriptor)))
        raise
    finally:
        os.close(descriptor)


@contextmanager
def registry_lock(directory, name):
    lock_name = name + ".lock"
    descriptor = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        _private_file(info, 0)
        if _current_identity(directory, lock_name) != _identity(info):
            raise ManagementError("The registry lock changed; no update was made.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagementError("Another registry operation is active; retry later.") from None
        yield
    finally:
        os.close(descriptor)


def atomic_store(directory, name, payload, expected):
    """Publish a fully written private file, preserving prior files on failure.

    A post-commit failure is distinguished so callers never discard a key whose
    registry entry may already be active. Initial publication never overwrites.
    """
    temporary = ".api-clients-" + secrets.token_hex(12) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    committed = False
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if _current_identity(directory, name) != expected:
            raise ManagementError("A private file changed; no replacement was made.")
        if expected is None:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            committed = True
            os.unlink(temporary, dir_fd=directory)
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            committed = True
        os.fsync(directory)
        return _identity(os.fstat(descriptor))
    except BaseException:
        # A signal can arrive after rename/link succeeds but before the Python
        # assignment above. Inspect the named inode before classifying failure.
        current = _current_identity(directory, name)
        own = _identity(os.fstat(descriptor))
        if committed or (current is not None and current[:2] == own[:2]):
            raise CommittedError(_identity(os.fstat(descriptor))) from None
        raise
    finally:
        try:
            current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            own = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (own.st_dev, own.st_ino):
                os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManagementError("The registry is invalid.")
        result[key] = value
    return result


def _registry(directory, name, reserved_username):
    payload, identity = read_private(directory, name, MAX_REGISTRY_BYTES)
    document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    return validate_registry(document, legacy_username=reserved_username), identity


def _registry_bytes(document, reserved_username):
    validate_registry(document, legacy_username=reserved_username)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_REGISTRY_BYTES:
        raise ManagementError("The registry has reached its size limit.")
    return payload


def initialize_registry(registry):
    with private_parent(registry) as (directory, name), registry_lock(directory, name):
        atomic_store(directory, name, _registry_bytes({"version": 1, "clients": []}, None), None)


def issue_client(registry, username, *, output=None, invitation=None,
                 expires_at=None, reserved_username=None):
    if (output is None) == (invitation is None):
        raise ManagementError("Choose exactly one private key output or invitation.")
    entry = {"username": username, "key_sha256": "0" * 64,
             "enabled": True, "expires_at": expires_at}
    validate_registry({"version": 1, "clients": [entry]}, legacy_username=reserved_username)
    if expires_at is not None:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc):
            raise ManagementError("Expiry must be a future UTC timestamp.")
    with private_parent(registry) as (directory, name), registry_lock(directory, name), \
            private_parent(invitation if invitation is not None else output) as (target_dir, target_name):
        if ((os.fstat(directory).st_dev, os.fstat(directory).st_ino)
                == (os.fstat(target_dir).st_dev, os.fstat(target_dir).st_ino)
                and target_name in {name, name + ".lock"}):
            raise ManagementError("The key destination must be separate from the registry and lock.")
        document, previous = _registry(directory, name, reserved_username)
        if any(client["username"] == username for client in document["clients"]):
            raise ManagementError("The username already exists; no key was replaced.")
        original, original_identity = None, None
        if invitation is not None:
            original, original_identity = read_private(target_dir, target_name, MAX_INVITATION_BYTES)
            text = original.decode("utf-8")
            username_placeholder = "[CONFIRMED_USERNAME_FOR_" + username.upper() + "]"
            if (text.count(KEY_PLACEHOLDER) != 1 or text.count(username_placeholder) != 1
                    or text.count("[CONFIRMED_USERNAME_FOR_") != 1):
                raise ManagementError("The invitation must contain exactly the matching username and key placeholders.")
        elif _current_identity(target_dir, target_name) is not None:
            raise ManagementError("The key output already exists; refusing to overwrite it.")
        key = secrets.token_hex(32)
        entry["key_sha256"] = hashlib.sha256(key.encode("ascii")).hexdigest()
        document["clients"].append(entry)
        payload = _registry_bytes(document, reserved_username)
        target_payload = ((text.replace(username_placeholder, username).replace(KEY_PLACEHOLDER, key)
                           if invitation is not None else key + "\n").encode("utf-8"))
        target_identity = None
        registry_committed = False
        try:
            try:
                if invitation is None:
                    target_identity = exclusive_key_output(target_dir, target_name, target_payload)
                else:
                    target_identity = atomic_store(target_dir, target_name, target_payload, original_identity)
            except CommittedError as error:
                target_identity = error.identity
                raise
            try:
                atomic_store(directory, name, payload, previous)
                registry_committed = True
            except CommittedError:
                registry_committed = True
                raise
        except BaseException:
            # An interruption immediately after a successful registry rename
            # must never discard the only copy of an already-authorized key.
            if (target_identity is not None and not registry_committed
                    and _current_identity(directory, name) == previous):
                if original is None:
                    _remove_owned(target_dir, target_name, target_identity)
                elif _current_identity(target_dir, target_name) == target_identity:
                    atomic_store(target_dir, target_name, original, target_identity)
            raise


def revoke_client(registry, username, *, reserved_username=None):
    with private_parent(registry) as (directory, name), registry_lock(directory, name):
        document, previous = _registry(directory, name, reserved_username)
        matches = [client for client in document["clients"] if client["username"] == username]
        if not matches:
            raise ManagementError("The username is not present; no registry changes were made.")
        if not matches[0]["enabled"]:
            return
        matches[0]["enabled"] = False
        atomic_store(directory, name, _registry_bytes(document, reserved_username), previous)


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ManagementError("Invalid arguments; use --help for command syntax.")


def main(argv=None):
    parser = PrivateArgumentParser(prog="manage-api-clients", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in ("init", "issue", "revoke"):
        command = commands.add_parser(operation)
        command.add_argument("--registry", required=True)
        if operation != "init":
            command.add_argument("--username", required=True)
            command.add_argument("--reserved-username")
        if operation == "issue":
            destinations = command.add_mutually_exclusive_group(required=True)
            destinations.add_argument("--output", help="New private file for the raw key only.")
            destinations.add_argument("--invitation", help="Existing private invitation with matching placeholders.")
            command.add_argument("--expires-at", help="Future ISO 8601 UTC expiry; otherwise no expiry.")
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            initialize_registry(args.registry)
        elif args.command == "issue":
            issue_client(args.registry, args.username, output=args.output, invitation=args.invitation,
                         expires_at=args.expires_at, reserved_username=args.reserved_username)
        else:
            revoke_client(args.registry, args.username, reserved_username=args.reserved_username)
        print("Private client registry operation completed. No services were changed.")
        return 0
    except ManagementError as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("error: Private client operation failed; inspect private files before retrying.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
