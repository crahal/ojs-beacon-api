#!/usr/bin/env python3
"""Root-only revocation of a gateway client, with validated graceful reload."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import subprocess
import sys


DEFAULT_USERS = "/etc/caddy/ojs-api-users.caddy"
DEFAULT_CONFIG = "/etc/caddy/Caddyfile"
DEFAULT_ADMIN = "unix//run/ojs-caddy-admin/admin.sock"
CADDY_BINARY = "/usr/bin/caddy"
MAX_USERS_BYTES = 128 * 1024
MAX_ACCOUNTS = 257
COMMAND_TIMEOUT = 30
MAX_ADAPTED_BYTES = 2 * 1024 * 1024
MARKER = "# internal-monitoring-username: "
USERNAME = re.compile(r"[A-Za-z0-9_.@+-]{1,64}")
BCRYPT = r"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}"
ACCOUNT = re.compile(r"[ \t]*([A-Za-z0-9_.@+-]{1,64})[ \t]+(" + BCRYPT + r")[ \t]*")


class GatewayError(ValueError):
    """Fixed messages only: never expose account hashes, paths, or child output."""


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or not path.name or ".." in path.parts
            or any(ord(character) < 32 for character in str(path))):
        raise GatewayError("Use absolute, non-symlink configuration paths.")
    return path


@contextmanager
def secure_parent(value):
    path = _path(value)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:-1]):
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            info = os.fstat(descriptor)
            final = index == len(path.parts[1:-1]) - 1
            sticky_ancestor = not final and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid != 0 or (info.st_mode & 0o022 and not sticky_ancestor):
                raise GatewayError("Configuration parents must be root-owned and not writable by others.")
        info = os.fstat(descriptor)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise GatewayError("Configuration parents must be root-owned and not writable by others.")
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _users_info(info, caddy_gid):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != caddy_gid
            or stat.S_IMODE(info.st_mode) != 0o640 or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_USERS_BYTES):
        raise GatewayError("The account file must be a bounded root:caddy mode 0640 regular file.")


def read_users(directory, name, caddy_gid):
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    _users_info(before, caddy_gid)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        _users_info(opened, caddy_gid)
        if _identity(opened) != _identity(before):
            raise GatewayError("The account file changed; inspect it before retrying.")
        content = bytearray()
        while len(content) <= MAX_USERS_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_USERS_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if (len(content) != opened.st_size or len(content) > MAX_USERS_BYTES
                or _identity(os.fstat(descriptor)) != _identity(opened)
                or _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(opened)):
            raise GatewayError("The account file changed; inspect it before retrying.")
        return bytes(content), _identity(opened)
    finally:
        os.close(descriptor)


def parse_users(payload):
    try:
        text = payload.decode("ascii")
    except UnicodeError:
        raise GatewayError("The account file has an unsupported format.") from None
    if not text.endswith("\n") or len(payload) > MAX_USERS_BYTES or "\r" in text:
        raise GatewayError("The account file has an unsupported format.")
    lines = text.splitlines(keepends=True)
    operator, accounts = None, {}
    hashes = set()
    for index, line in enumerate(lines):
        body = line.removesuffix("\n")
        if not body.strip(" \t"):
            continue
        if body.startswith(MARKER):
            candidate = body[len(MARKER):]
            if operator is not None or accounts or not USERNAME.fullmatch(candidate):
                raise GatewayError("Exactly one internal monitoring marker must precede all accounts.")
            operator = candidate
            continue
        match = ACCOUNT.fullmatch(body)
        if (operator is None or match is None or match[1] in accounts
                or match[2] in hashes or len(accounts) >= MAX_ACCOUNTS):
            raise GatewayError("The account file has invalid or duplicate entries.")
        accounts[match[1]] = index
        hashes.add(match[2])
    if operator is None or operator not in accounts:
        raise GatewayError("The internal monitoring account must be present.")
    return operator, accounts, lines


def revoked_payload(payload, username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise GatewayError("The requested username is invalid.")
    operator, accounts, lines = parse_users(payload)
    if username == operator:
        raise GatewayError("The internal monitoring account cannot be revoked by this command.")
    if username not in accounts:
        raise GatewayError("The requested client is absent; no changes were made.")
    del lines[accounts[username]]
    updated = "".join(lines).encode("ascii")
    parse_users(updated)
    return updated


@contextmanager
def account_lock(directory, name):
    lock = name + ".lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 0
                or _identity(os.stat(lock, dir_fd=directory, follow_symlinks=False)) != _identity(info)):
            raise GatewayError("The private gateway lock is unsafe.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GatewayError("Another gateway change is active; retry later.") from None
        yield
    finally:
        os.close(descriptor)


def replace_users(directory, name, payload, expected, caddy_gid):
    temporary = ".gateway-users-" + secrets.token_hex(12) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        os.fchown(descriptor, 0, caddy_gid)
        os.fchmod(descriptor, 0o640)
        remaining = memoryview(payload)
        while remaining:
            count = os.write(descriptor, remaining)
            if count <= 0:
                raise OSError("Private write failed")
            remaining = remaining[count:]
        os.fsync(descriptor)
        if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected:
            raise GatewayError("The account file changed; no replacement was made.")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return _identity(os.fstat(descriptor))
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


def validate_config_path(config, caddy_gid):
    with secure_parent(config) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_gid != caddy_gid or stat.S_IMODE(info.st_mode) != 0o640
                    or not 0 < info.st_size <= 1024 * 1024
                    or _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(info)):
                raise GatewayError("The gateway configuration must be a bounded, protected root-owned file.")
            return _identity(info)
        finally:
            os.close(descriptor)


def validate_admin_socket(admin, caddy_uid):
    if (not isinstance(admin, str) or not admin.startswith("unix//")
            or any(character.isspace() for character in admin)
            or any(character in admin for character in ("?", "#", "\x00"))):
        raise GatewayError("Use the trusted local Unix admin socket.")
    path = _path(admin[5:])
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            info = os.fstat(descriptor)
            if info.st_uid not in (0, caddy_uid) or info.st_mode & 0o022:
                raise GatewayError("The admin socket has untrusted parent directories.")
        parent = os.fstat(descriptor)
        info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if (parent.st_uid not in (0, caddy_uid) or stat.S_IMODE(parent.st_mode) != 0o700
                or not stat.S_ISSOCK(info.st_mode) or info.st_uid not in (0, caddy_uid)
                or info.st_nlink != 1):
            raise GatewayError("The admin socket must be trusted and inside an owner-private directory.")
        return (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


def caddy_command(operation, config, admin, caddy_account):
    arguments = [CADDY_BINARY, operation, "--config", str(config), "--adapter", "caddyfile"]
    if operation == "reload":
        arguments.extend(["--address", admin])
    options = {"env": {"PATH": "/usr/bin:/bin", "HOME": caddy_account.pw_dir,
                       "USER": "caddy", "LOGNAME": "caddy"}}
    if operation in {"adapt", "validate"}:
        options.update(user=caddy_account.pw_uid, group=caddy_account.pw_gid, extra_groups=[])
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=COMMAND_TIMEOUT, check=False, cwd="/", **options)
    if result.returncode:
        raise GatewayError("Gateway validation or reload failed.")
    return result.stdout


def validate_effective_accounts(adapted, payload):
    """Check expanded configuration privately; never print or persist its secrets."""
    if not isinstance(adapted, bytes) or len(adapted) > MAX_ADAPTED_BYTES:
        raise GatewayError("The expanded gateway configuration is unavailable or too large.")
    document = json.loads(adapted)
    providers, remaining = [], [document]
    while remaining:
        current = remaining.pop()
        if isinstance(current, dict):
            if current.get("handler") == "authentication":
                configured = current.get("providers")
                if not isinstance(configured, dict) or set(configured) != {"http_basic"}:
                    raise GatewayError("The gateway authentication configuration is unsupported.")
                providers.append(configured["http_basic"])
            remaining.extend(current.values())
        elif isinstance(current, list):
            remaining.extend(current)
    if len(providers) != 1 or not isinstance(providers[0], dict):
        raise GatewayError("Exactly one gateway Basic authentication provider is required.")
    provider = providers[0]
    if provider.get("hash", {"algorithm": "bcrypt"}) != {"algorithm": "bcrypt"}:
        raise GatewayError("The gateway must use bcrypt authentication.")
    actual = {}
    accounts = provider.get("accounts")
    if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
        raise GatewayError("The effective gateway accounts are invalid.")
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {"username", "password"}:
            raise GatewayError("The effective gateway accounts are invalid.")
        username, password = account["username"], account["password"]
        if (not isinstance(username, str) or not USERNAME.fullmatch(username)
                or username in actual or not isinstance(password, str)):
            raise GatewayError("The effective gateway accounts are invalid.")
        if not password.startswith("$"):
            password = base64.b64decode(password, validate=True).decode("ascii")
        actual[username] = password
    _, names, lines = parse_users(payload)
    expected = {username: ACCOUNT.fullmatch(lines[index].removesuffix("\n"))[2]
                for username, index in names.items()}
    if actual != expected:
        raise GatewayError("The account file does not match the effective gateway configuration.")


def assert_effective_accounts(config, admin, caddy_account, payload):
    validate_effective_accounts(caddy_command("adapt", config, admin, caddy_account), payload)


def revoke_client(username, *, users_file=DEFAULT_USERS, config=DEFAULT_CONFIG, admin=DEFAULT_ADMIN):
    if os.geteuid() != 0:
        raise GatewayError("Only root may run this gateway management command.")
    caddy_gid = grp.getgrnam("caddy").gr_gid
    caddy_account = pwd.getpwnam("caddy")
    if caddy_account.pw_gid != caddy_gid:
        raise GatewayError("The gateway service account has an unexpected primary group.")
    admin_identity = validate_admin_socket(admin, caddy_account.pw_uid)
    config = _path(config)
    config_identity = validate_config_path(config, caddy_gid)
    with secure_parent(users_file) as (directory, name), account_lock(directory, name):
        original, previous = read_users(directory, name, caddy_gid)
        updated = revoked_payload(original, username)
        assert_effective_accounts(config, admin, caddy_account, original)
        attempted_reload = False
        try:
            replace_users(directory, name, updated, previous, caddy_gid)
            if validate_config_path(config, caddy_gid) != config_identity:
                raise GatewayError("The gateway configuration changed; inspect it before retrying.")
            current, _ = read_users(directory, name, caddy_gid)
            if current != updated:
                raise GatewayError("The account file changed; inspect it before retrying.")
            assert_effective_accounts(config, admin, caddy_account, updated)
            caddy_command("validate", config, admin, caddy_account)
            if (validate_admin_socket(admin, caddy_account.pw_uid) != admin_identity
                    or validate_config_path(config, caddy_gid) != config_identity):
                raise GatewayError("The gateway configuration or admin socket changed.")
            attempted_reload = True
            caddy_command("reload", config, admin, caddy_account)
            if read_users(directory, name, caddy_gid)[0] != updated:
                raise GatewayError("The account file changed; inspect it before retrying.")
        except BaseException:
            try:
                current, current_identity = read_users(directory, name, caddy_gid)
                if current == updated:
                    replace_users(directory, name, original, current_identity, caddy_gid)
                    restored = True
                elif current == original:
                    restored = False
                else:
                    raise GatewayError("The account file changed during recovery.")
                if restored or attempted_reload:
                    if (validate_config_path(config, caddy_gid) != config_identity
                            or validate_admin_socket(admin, caddy_account.pw_uid) != admin_identity):
                        raise GatewayError("The gateway configuration changed during recovery.")
                    assert_effective_accounts(config, admin, caddy_account, original)
                    caddy_command("validate", config, admin, caddy_account)
                    caddy_command("reload", config, admin, caddy_account)
                    if read_users(directory, name, caddy_gid)[0] != original:
                        raise GatewayError("The account file changed during recovery.")
            except BaseException:
                raise GatewayError("Revocation failed and rollback is not confirmed; inspect gateway state before retrying.") from None
            raise GatewayError("Revocation failed; the original account file was preserved or restored.") from None


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise GatewayError("Invalid arguments; use --help for command syntax.")


def main(argv=None):
    parser = PrivateArgumentParser(prog="manage-gateway-clients", description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--users-file", default=DEFAULT_USERS)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--admin", default=DEFAULT_ADMIN)
    try:
        args = parser.parse_args(argv)
        revoke_client(args.username, users_file=args.users_file, config=args.config, admin=args.admin)
        print("Client revoked; gateway configuration validated and gracefully reloaded.")
        return 0
    except GatewayError as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("error: Gateway operation failed; inspect gateway state before retrying.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
