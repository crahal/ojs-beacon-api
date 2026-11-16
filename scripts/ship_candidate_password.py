#!/usr/bin/env python3
"""Send only a builder root password over verified SSH to an offline release.

Secrets travel over SSH stdin, never in command arguments, shell expansion,
environment variables, or diagnostics. This does not adopt or publish a release.
The dated destination is retained for explicit credential adoption/recovery.
"""
from __future__ import annotations

import argparse
from datetime import date
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import run_pipeline as pipeline
import workstation_recover as recovery

LIMIT = 4096
REMOTE_ROOT = Path("/srv/ojs_api")


class TransferError(RuntimeError):
    """Diagnostics are fixed strings and never include supplied file contents."""


def snapshot_date(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Snapshot must be a valid YYYY-MM-DD date.") from None
    return value


def regular_path(path: Path, *, private: bool = False) -> Path:
    path = path.expanduser().absolute()
    if path.resolve(strict=True) != path:
        raise TransferError("Input paths must not contain symlinks.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise TransferError("A regular input file is required.")
    if private and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
        raise TransferError("Private inputs must be owner-private and owned by the invoking account.")
    return path


def read_small(path: Path, limit: int = 65536, *, private: bool = False) -> bytes:
    path = regular_path(path, private=private)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and (
                info.st_mode & 0o077 or info.st_uid != os.geteuid())):
            raise TransferError("Input file changed or has unsafe permissions.")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise TransferError("Input exceeds the bounded size limit.")
    return data


def password_payload(value: str) -> bytes:
    if not value or any(character in value for character in ("\n", "\r", "\0")):
        raise TransferError("The builder password must be one nonempty line.")
    payload = value.encode("utf-8") + b"\n"
    if len(payload) > LIMIT:
        raise TransferError("The builder password exceeds the bounded size limit.")
    return payload


def remote_command(args) -> list[str]:
    # Fixed remote root, no shell metacharacters in the login target, and explicit
    # strict host verification: a newly provisioned/changed key must be reviewed.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", args.host):
        raise TransferError("Use an explicit SSH user@host without SSH options or shell syntax.")
    identity = regular_path(args.identity_file, private=True)
    command = ["sudo", "-n", "-u", "ojs", "python3", "-B",
               str(REMOTE_ROOT / "scripts" / "ship_candidate_password.py"),
               "receive", "--snapshot", args.snapshot]
    return ["ssh", "-F", "/dev/null", "-i", str(identity),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            args.host, shlex.join(command)]


def send(args) -> dict:
    command = remote_command(args)
    values = recovery.credentials(args.credentials_env)
    payload = password_payload(values["MYSQL_ROOT_PASSWORD"])
    result = subprocess.run(command, input=payload, capture_output=True,
                            timeout=120, check=False)
    if result.returncode:
        raise TransferError("Private credential transfer failed; check SSH access and the paused offline candidate.")
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise TransferError("The receiver did not return a valid transfer receipt.") from None
    expected = {"status": "credential_stored", "snapshot": args.snapshot,
                "path": str(REMOTE_ROOT / ".secrets" / f"incoming-root-password-{args.snapshot}"),
                "published": False}
    if receipt != expected:
        raise TransferError("The receiver returned an unexpected transfer receipt.")
    return receipt


def receive_preflight(root: Path, snapshot: str) -> Path:
    # root is fixed for the command-line receiver. The argument is injectable
    # only for isolated unit tests; no arbitrary remote destination is accepted.
    root = root.absolute()
    for directory in (root, root / "data", root / "data" / "clean", root / ".secrets"):
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise TransferError("Existing nonsymlinked deployment directories are required.")
    if not os.path.ismount(root / "data"):
        raise TransferError("The project data directory must be a mounted data filesystem.")
    regular_path(root / "data" / ".updates-paused")
    secrets = root / ".secrets"
    info = secrets.stat()
    if info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise TransferError("The secrets directory must be owner-private and owned by the pipeline account.")
    clean = root / "data" / "clean"
    candidate = clean / f"mysql-{snapshot}"
    if candidate.resolve(strict=True) != candidate or not candidate.is_dir():
        raise TransferError("The completed candidate directory is missing or unsafe.")
    live = clean / "mysql-live"
    if live.exists() or live.is_symlink():
        if not live.is_symlink() or live.resolve(strict=True) == candidate:
            raise TransferError("The receiving candidate must not be the live database.")
    if (clean / ".compact-update.json").exists() or (clean / ".compact-update.json").is_symlink():
        raise TransferError("Resolve the pending publication journal before credential transfer.")
    marker = json.loads(read_small(candidate / "OJS_COMPACT_RELEASE.json"))
    expected = {"schema_version": 1, "snapshot_date": snapshot,
                "data_directory_name": candidate.name, "clean_only": True}
    if any(marker.get(key) != value for key, value in expected.items()):
        raise TransferError("The candidate is not a completed clean release for this snapshot.")
    manifest = json.loads(read_small(clean / f"pkpbeacon-release-{snapshot}.json"))
    if (manifest.get("schema_version") != 1 or manifest.get("snapshot_date") != snapshot
            or manifest.get("clean_export_filename") != f"pkpbeacon-clean-{snapshot}.sql.gz"):
        raise TransferError("The candidate completion manifest is missing or inconsistent.")
    # Adoption/publication authenticate artifact checksums and database contents.
    # Here only confirm transfer has supplied the expected recovery artifacts.
    for name in (f"pkpbeacon-clean-{snapshot}.sql.gz", f"pkpbeacon-clean-{snapshot}.sql.gz.sha256",
                 f"pkpbeacon-changes-{snapshot}.json", f"pkpbeacon-changes-{snapshot}.json.sha256"):
        if regular_path(clean / name).stat().st_size == 0:
            raise TransferError("The completed candidate recovery artifacts are incomplete.")
    pipeline.assert_database_stopped(candidate)
    return secrets / f"incoming-root-password-{snapshot}"


def store_private(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        if not hmac.compare_digest(read_small(path, LIMIT, private=True), payload):
            raise TransferError("A different incoming credential already exists; refusing to overwrite it.")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-password-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        # Atomic no-overwrite publication; a concurrent writer cannot be replaced.
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def receive(args, *, stream=None, root: Path = REMOTE_ROOT) -> dict:
    target = receive_preflight(root, args.snapshot)
    stream = sys.stdin.buffer if stream is None else stream
    data = stream.read(LIMIT + 1)
    if len(data) > LIMIT or not data.endswith(b"\n"):
        raise TransferError("The received credential has an invalid size or framing.")
    payload = password_payload(data[:-1].decode("utf-8"))
    store_private(target, payload)
    return {"status": "credential_stored", "snapshot": args.snapshot,
            "path": str(target), "published": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    sender = modes.add_parser("send", help="run on the desktop; sends only the temporary builder root password")
    sender.add_argument("--credentials-env", type=Path, required=True)
    sender.add_argument("--host", required=True, help="Explicit SSH destination, for example ubuntu@api.example.org")
    sender.add_argument("--identity-file", type=Path, required=True)
    sender.add_argument("--snapshot", type=snapshot_date, required=True)
    receiver = modes.add_parser("receive", help="internal: run as ojs on the receiving server")
    receiver.add_argument("--snapshot", type=snapshot_date, required=True)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        result = send(args) if args.mode == "send" else receive(args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except TransferError as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        # Exceptions, SSH output, input text, and subprocess argv are not logged.
        print("error: Private credential transfer failed; no publication was attempted.", file=sys.stderr)
    finally:
        os.umask(previous_umask)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
