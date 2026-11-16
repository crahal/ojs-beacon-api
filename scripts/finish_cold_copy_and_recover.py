#!/usr/bin/env python3
"""Finish a stopped-candidate transfer, then exec the guarded recovery child.

Run inside run_guarded_local_build.py, never on its own. An optional existing
copy unit is allowed to finish first. A final resumable rsync must itself exit
successfully before MySQL can open the local copy; disappearance of the old
unit is not treated as proof of a completed transfer. The remote read-only
sender holds the source/pipeline locks and requires paused updates throughout.
"""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from progress_logging import Progress
from run_pipeline import PIPELINE_VERSION, assert_database_stopped, pipeline_lock, read_build_state
from source_activity import source_activity, source_activity_child_options


class HandoffError(RuntimeError):
    pass


def wait_for_copy(unit: str, timeout_hours: float) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.service", unit):
        raise HandoffError("invalid existing copy unit")
    deadline = time.monotonic() + timeout_hours * 3600
    with Progress("workstation-copy-wait", interval=30, step="existing-copy"):
        while True:
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState"],
                capture_output=True, text=True, timeout=15,
            )
            states = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if states.get("LoadState") == "not-found":
                return  # The final rsync still has to prove successful completion.
            if result.returncode != 0 or states.get("LoadState") != "loaded":
                raise HandoffError("cannot inspect the existing copy unit")
            if states.get("ActiveState") in ("inactive", "failed"):
                return
            if states.get("ActiveState") not in ("active", "activating", "deactivating", "reloading"):
                raise HandoffError("unrecognized existing copy state")
            if time.monotonic() >= deadline:
                raise HandoffError("existing copy wait exceeded its deadline; no database opened")
            time.sleep(15)


def transfer_command(args, destination: Path) -> list[str]:
    try:
        if date.fromisoformat(args.snapshot_date).isoformat() != args.snapshot_date:
            raise ValueError
    except ValueError:
        raise HandoffError("invalid snapshot date") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+@[A-Za-z0-9][A-Za-z0-9.-]*", args.remote):
        raise HandoffError("remote must be an explicit SSH user and host")
    remote_root = PurePosixPath(args.remote_project)
    if (not remote_root.is_absolute() or ".." in remote_root.parts
            or not re.fullmatch(r"/[A-Za-z0-9_/.-]+", args.remote_project)
            or str(remote_root) == "/"):
        raise HandoffError("remote project must be a specific absolute path")
    key = args.ssh_key.expanduser().resolve(strict=True)
    if not key.is_file():
        raise HandoffError("an existing SSH identity file is required")
    ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
           "-o", "ServerAliveCountMax=4", "-o", "IdentitiesOnly=yes", "-i", str(key)]
    sender = ["sudo", "-n", "-u", args.pipeline_user, "python3", "-B",
              str(remote_root / "scripts/cold_copy_sender.py"), "--snapshot", args.snapshot_date, "--"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", args.pipeline_user):
        raise HandoffError("invalid remote pipeline account")
    source = remote_root / "data/clean" / f"mysql-{args.snapshot_date}.building"
    return ["rsync", "-a", "--no-owner", "--no-group", "--partial", "--sparse", "--stats",
            "-e", shlex.join(ssh), "--rsync-path=" + shlex.join(sender),
            f"{args.remote}:{source}/", str(destination) + "/"]


def check_destination(clean: Path, snapshot_date: str) -> Path:
    if clean.resolve(strict=True) != clean or not clean.is_dir():
        raise HandoffError("an existing regular local clean directory is required")
    destination = clean / f"mysql-{snapshot_date}.building"
    final = clean / f"mysql-{snapshot_date}"
    if destination.is_symlink() or final.exists() or final.is_symlink():
        raise HandoffError("refusing to copy over a release or a symlink")
    if any((clean / name).exists() or (clean / name).is_symlink()
           for name in ("mysql-live", "mysql-current", ".compact-update.json")):
        raise HandoffError("cold-copy recovery requires an isolated unpublished clean directory")
    if destination.exists() and not destination.is_dir():
        raise HandoffError("the local candidate is not a regular directory")
    if any(path.is_symlink() for path in destination.rglob("*")):
        raise HandoffError("local candidate contains a symlink")
    # Inspect only after rejecting symlinks, including all table files.
    assert_database_stopped(destination)
    return destination


def check_completed_copy(destination: Path, snapshot: Path, snapshot_date: str) -> None:
    if not all((destination / name).is_file() for name in ("ibdata1", "mysql.ibd")):
        raise HandoffError("completed copy is missing required MySQL data files")
    checkpoint = read_build_state(destination)
    expected = {"pipeline_version": PIPELINE_VERSION, "snapshot_version": snapshot_date,
                "source_filename": snapshot.name, "phase": "finalizing"}
    if (checkpoint is None or any(checkpoint.get(key) != value for key, value in expected.items())
            or type(checkpoint.get("source_size_bytes")) is not int or checkpoint["source_size_bytes"] <= 0
            or any(not isinstance(checkpoint.get(key), str)
                   or re.fullmatch(r"[0-9a-f]{64}", checkpoint[key]) is None
                   for key in ("source_sha256", "build_sql_sha256"))):
        raise HandoffError("completed copy lacks the matching original finalizing checkpoint")


def execute(args) -> None:
    snapshot = args.snapshot.expanduser().resolve(strict=True)
    if (not snapshot.is_file()
            or snapshot.name not in (f"pkpbeacon-{args.snapshot_date}.sql",
                                     f"pkpbeacon-{args.snapshot_date}.sql.gz")):
        raise HandoffError("the selected raw snapshot filename must match the requested date")
    clean = args.clean_dir.expanduser().absolute()
    # Validate arguments before a potentially long wait, then recheck the local
    # destination under both locks after the old copy stops and after rsync.
    destination = check_destination(clean, args.snapshot_date)
    command = transfer_command(args, destination)
    if args.wait_for_unit:
        wait_for_copy(args.wait_for_unit, 48)
    with source_activity(snapshot.parent), pipeline_lock(clean):
        check_destination(clean, args.snapshot_date)
        with Progress("workstation-cold-copy", interval=30, step="resume-and-confirm", disk_path=clean):
            result = subprocess.run(command, check=False)
            if result.returncode:
                raise HandoffError("candidate transfer failed; no database opened")
        check_destination(clean, args.snapshot_date)
        check_completed_copy(destination, snapshot, args.snapshot_date)
        print("[handoff] cold transfer completed; starting audited local recovery", flush=True)
        child = [sys.executable, "-B", str(ROOT / "scripts/workstation_recover.py"),
                 "--credentials-env", str(args.credentials_env),
                 "--api-password-file", str(args.api_password_file),
                 "--mysql-bin", str(args.mysql_bin), "--mysql-version", args.mysql_version,
                 "--snapshot", str(snapshot), "--clean-dir", str(clean),
                 "--scratch-dir", str(args.scratch_dir), "--buffer-pool-size", args.buffer_pool_size]
        # Exec closes the CLOEXEC pipeline descriptor before the child acquires
        # its own pipeline lock. Preserve the source lease across that boundary
        # so no downloader or second pipeline can enter during the handoff.
        options = source_activity_child_options(snapshot.parent)
        descriptors = options["pass_fds"]
        try:
            for descriptor in descriptors:
                os.set_inheritable(descriptor, True)
            os.execve(sys.executable, child, options["env"])
            raise HandoffError("recovery exec unexpectedly returned")
        finally:
            for descriptor in descriptors:
                os.set_inheritable(descriptor, False)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--remote", required=True)
    result.add_argument("--remote-project", required=True)
    result.add_argument("--pipeline-user", default="ojs")
    result.add_argument("--ssh-key", type=Path, required=True)
    result.add_argument("--snapshot-date", required=True)
    result.add_argument("--wait-for-unit")
    for name in ("credentials-env", "api-password-file", "mysql-bin", "snapshot", "clean-dir", "scratch-dir"):
        result.add_argument("--" + name, type=Path, required=True)
    result.add_argument("--mysql-version", default="8.4.11")
    result.add_argument("--buffer-pool-size", default="8G")
    return result


def main(argv=None) -> int:
    os.umask(0o077)
    try:
        execute(parser().parse_args(argv))
    except HandoffError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
    except (Exception, KeyboardInterrupt):
        print("error: workstation handoff stopped; inspect copy and recovery progress", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
