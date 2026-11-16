#!/usr/bin/env python3
"""Transfer a completed desktop release and queue durable publication on its VM.

Run this coordinator in a desktop user systemd service. Keep the desktop awake
until it reports publication_started; that status is NOT an API health result.
The remote publisher then runs independently of this desktop and verifies the
API. Existing helpers provide data validation, workflow locks and safe staging.

Automatic downloads/builds stay paused. Failure stops the chain. A completed or
adopted transfer is never silently retried: inspect the dated remote publication
unit and incoming state before using the individual recovery helpers. The only
transfer retry override is --finish-only for interrupted transfer installation.
SIGINT/SIGTERM stop and wait for the current local child process group; use
KillMode=mixed and a generous stop timeout on the desktop service. A disconnect
while launching publication can leave the remote job running; inspect its named
unit instead of starting another deployment blindly.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_api_history as api_check
import ship_candidate_password as ship

REMOTE_ROOT = Path("/srv/ojs_api")


class DeploymentError(RuntimeError):
    """Safe diagnostics never contain command arguments or credential contents."""


def validate_plan(args) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", args.host):
        raise DeploymentError("Use an explicit SSH user@host without options or shell syntax.")
    if args.remote_project != REMOTE_ROOT:
        # Credential receiving is deliberately fixed to this deployment root.
        raise DeploymentError("The credential receiver requires /srv/ojs_api as the remote project.")
    if args.reserve_gib < 20:
        raise DeploymentError("Keep at least 20 GiB reserved on the receiving data disk.")
    history = args.history_snapshot
    if not history or history != sorted(set(history)) or history[-1] != args.snapshot:
        raise DeploymentError("Specify ordered, unique history dates ending at the selected snapshot.")
    public_origin = api_check.origin(args.public_base_url)
    if not public_origin.startswith("https://"):
        raise DeploymentError("The public API origin must use trusted HTTPS.")
    args.identity_file = ship.regular_path(args.identity_file, private=True)
    args.credentials_env = ship.regular_path(args.credentials_env, private=True)
    try:
        ship.password_payload(ship.recovery.credentials(args.credentials_env)["MYSQL_ROOT_PASSWORD"])
    except Exception:
        raise DeploymentError("The private builder credential is invalid; no transfer was started.") from None
    for attribute in ("source_clean", "source_raw"):
        paths = [getattr(args, attribute)] if attribute == "source_clean" else getattr(args, attribute)
        for path in paths:
            absolute = path.expanduser().absolute()
            if absolute.resolve(strict=True) != absolute or not absolute.is_dir():
                raise DeploymentError("Source directories must exist and must not contain symlinks.")
        setattr(args, attribute, paths[0].expanduser().absolute() if attribute == "source_clean"
                else [path.expanduser().absolute() for path in paths])
    for name in ("transfer_completed_release.py", "ship_candidate_password.py",
                 "publish_imported_release.py"):
        ship.regular_path(ROOT / "scripts" / name)
    return public_origin


def remote_publication(args, public_origin: str) -> tuple[str, list[str]]:
    unit = f"ojs-api-publish-{args.snapshot}"
    root = args.remote_project
    command = ["sudo", "-n", "systemd-run", f"--unit={unit}",
               "--description=Adopt and publish completed OJS catalogue",
               "--uid=ojs", "--gid=ojs", f"--working-directory={root}"]
    for value in ("Type=exec", f"EnvironmentFile={root}/.env",
                  f"RequiresMountsFor={root}/data", "MemoryHigh=1792M", "MemoryMax=2048M",
                  "MemorySwapMax=1024M", "OOMScoreAdjust=500", "UMask=0077",
                  "KillMode=mixed", "TimeoutStopSec=infinity"):
        command.append(f"--property={value}")
    command += ["--setenv=PYTHONDONTWRITEBYTECODE=1", "--setenv=PYTHONUNBUFFERED=1",
                "/usr/bin/python3", "-B", str(root / "scripts/publish_imported_release.py"),
                "--project-root", str(root), "--snapshot", args.snapshot]
    for version in args.history_snapshot:
        command += ["--history-snapshot", version]
    command += ["--public-base-url", public_origin, "--temporary-root-password-file",
                str(root / ".secrets" / f"incoming-root-password-{args.snapshot}")]
    ssh = ["ssh", "-F", "/dev/null", "-i", str(args.identity_file),
           "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=6", args.host, shlex.join(command)]
    return unit + ".service", ssh


def run_stage(label: str, command: list[str]) -> None:
    print(json.dumps({"stage": label, "event": "started"}), flush=True)
    process = None
    cancelled = False
    previous = {}

    def terminate_group():
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
        terminate_group()

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)
        if cancelled:
            raise DeploymentError("Deployment interrupted before its next step.")
        process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
        if cancelled:
            terminate_group()
        status = process.wait()
        if cancelled:
            raise DeploymentError("Deployment interrupted; inspect remote state before retrying.")
        if status != 0:
            raise DeploymentError(f"The {label} step failed; later steps were not run.")
    finally:
        if process is not None and process.poll() is None:
            terminate_group()
            process.wait()
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(json.dumps({"stage": label, "event": "completed"}), flush=True)


def deploy(args) -> dict:
    public_origin = validate_plan(args)
    unit, remote = remote_publication(args, public_origin)
    transfer = [sys.executable, "-B", str(ROOT / "scripts/transfer_completed_release.py"), "send",
                "--source-clean", str(args.source_clean), "--snapshot", args.snapshot,
                "--host", args.host, "--identity", str(args.identity_file),
                "--remote-project", str(args.remote_project), "--reserve-gib", str(args.reserve_gib)]
    for raw in args.source_raw:
        transfer += ["--source-raw", str(raw)]
    for version in args.history_snapshot:
        transfer += ["--history", version]
    if args.native_only_source:
        transfer.append("--native-only-source")
    if args.finish_only:
        transfer.append("--finish-only")
    run_stage("transfer-release", transfer)
    run_stage("ship-private-credential", [sys.executable, "-B",
              str(ROOT / "scripts/ship_candidate_password.py"), "send", "--credentials-env",
              str(args.credentials_env), "--identity-file", str(args.identity_file),
              "--host", args.host, "--snapshot", args.snapshot])
    run_stage("queue-remote-publication", remote)
    return {"status": "publication_started", "snapshot": args.snapshot,
            "history": args.history_snapshot, "remote_unit": unit,
            "public_base_url": public_origin, "api_verified": False, "updates_paused": True,
            "note": "Remote publication is running independently; check its journal for verification results."}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-clean", type=Path, required=True)
    result.add_argument("--source-raw", type=Path, action="append", required=True)
    result.add_argument("--credentials-env", type=Path, required=True)
    result.add_argument("--identity-file", "--identity", type=Path, required=True)
    result.add_argument("--host", required=True)
    result.add_argument("--remote-project", type=Path, default=REMOTE_ROOT)
    result.add_argument("--snapshot", type=api_check.snapshot_date, required=True)
    result.add_argument("--history-snapshot", "--history", type=api_check.snapshot_date,
                        action="append", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument("--native-only-source", action="store_true")
    result.add_argument("--reserve-gib", type=int, default=20)
    result.add_argument("--finish-only", action="store_true",
                        help="finish interrupted transfer installation; does not skip transfer validation")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        print(json.dumps(deploy(args), sort_keys=True), flush=True)
        return 0
    except (DeploymentError, api_check.CheckError, ship.TransferError) as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("error: Deployment stopped; inspect the named remote publication unit before retrying.",
              file=sys.stderr)
    finally:
        os.umask(previous_umask)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
