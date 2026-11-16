#!/usr/bin/env python3
"""Adopt, publish and smoke-test one already transferred release; never build.

Run as the pipeline user under a durable systemd unit, with its EnvironmentFile
set to the project's existing .env. Automatic work must remain paused. Existing
adoption/publication helpers retain their locks, validation and rollback logic.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import compact_update as update
import check_api_history as api_check


class HandoffError(RuntimeError):
    pass


def validate_plan(args) -> tuple[Path, list[str]]:
    root = args.project_root.expanduser().absolute()
    if root.resolve(strict=True) != root:
        raise HandoffError("Project root must be an existing non-symlinked path.")
    data = root / "data"
    if data.is_symlink() or not data.is_mount():
        raise HandoffError("The project data directory must be its own mounted filesystem.")
    pause = data / update.PAUSE_MARKER
    if pause.is_symlink() or not pause.is_file():
        raise HandoffError("Keep automatic updates paused before this handoff.")
    dates = args.history_snapshot
    if not dates or dates != sorted(set(dates)) or dates[-1] != args.snapshot:
        raise HandoffError("Specify the full ordered history, ending at the selected release.")
    origin = api_check.origin(args.public_base_url)
    if not origin.startswith("https://"):
        raise HandoffError("The public API check requires trusted HTTPS.")
    for name in ("adopt_candidate_credentials.py", "check_api_history.py", "automatic_update.sh"):
        path = root / "scripts" / name
        if path.is_symlink() or not path.is_file():
            raise HandoffError("A required handoff helper is missing or symlinked.")
    api_check.credentials(root / ".secrets" / "api-client.env")
    port = os.environ.get("OJS_API_PORT", "8000")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise HandoffError("The API port is invalid.")
    return root, [f"http://127.0.0.1:{port}", origin]


def run_stage(label: str, command: list[str], root: Path) -> None:
    print(json.dumps({"stage": label, "event": "started"}), flush=True)
    process = subprocess.Popen(command, cwd=root)
    previous = {}

    def stop(signum, frame):
        # Signal the coordinator, not all its MySQL children. It must retain
        # its locks while waiting for a graceful database shutdown/rollback.
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, signal.SIG_IGN)
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.wait()
        raise HandoffError("Handoff interrupted; files retained and automatic work remains paused.")

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)
        if process.wait() != 0:
            raise HandoffError(f"The {label} step failed; later steps were not run.")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(json.dumps({"stage": label, "event": "completed"}), flush=True)


def handoff(args) -> dict:
    root, origins = validate_plan(args)
    clean = root / "data" / "clean"
    candidate = clean / f"mysql-{args.snapshot}"
    if update.live_directory(clean) != candidate:
        run_stage("adopt-credentials", [sys.executable, "-B",
                  str(root / "scripts" / "adopt_candidate_credentials.py"),
                  "--project-root", str(root), "--snapshot", args.snapshot,
                  "--temporary-root-password-file", str(args.temporary_root_password_file)], root)
    # Always use an explicit date, including retries after the live switch.
    # Never use the generic updater: it could download/build the next snapshot.
    run_stage("publish-release", ["/bin/bash", str(root / "scripts" / "automatic_update.sh"),
              "--publish-release", args.snapshot, "--no-prune", "--reserve-gb", "20",
              "--health-timeout", "600"], root)
    if update.live_directory(clean) != candidate:
        raise HandoffError("Publication did not select the requested release; inspect the job/locks.")
    for index, base in enumerate(origins):
        command = [sys.executable, "-B", str(root / "scripts" / "check_api_history.py"),
                   "--base-url", base, "--credentials-file",
                   str(root / ".secrets" / "api-client.env"), "--reports-dir", str(clean),
                   "--timeout", "120"]
        for date in args.history_snapshot:
            command.extend(("--snapshot", date))
        run_stage("verify-loopback" if index == 0 else "verify-public-https", command, root)
    return {"status": "passed", "snapshot": args.snapshot, "history": args.history_snapshot,
            "public_base_url": origins[-1], "updates_paused": True,
            "note": "Automatic builds remain paused; no old files or credentials were deleted."}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-root", type=Path, default=ROOT)
    result.add_argument("--snapshot", type=update.snapshot_date, required=True)
    result.add_argument("--history-snapshot", type=update.snapshot_date, action="append", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument("--temporary-root-password-file", type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        print(json.dumps(handoff(args), sort_keys=True), flush=True)
        return 0
    except (HandoffError, api_check.CheckError) as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("error: Handoff failed; inspect the journal. No cleanup or automatic build was requested.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
