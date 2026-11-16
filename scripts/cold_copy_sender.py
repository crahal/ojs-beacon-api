#!/usr/bin/env python3
"""Read-only rsync sender for one stopped, paused pipeline candidate.

Run as the pipeline account through rsync --rsync-path. The two existing locks
are held for the whole transfer. No live database, credentials directory, or
arbitrary filesystem path can be selected by the rsync arguments.
"""
from pathlib import Path
import argparse
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from source_activity import source_activity
from run_pipeline import pipeline_lock, assert_database_stopped, read_build_state


def sender_arguments(command: list[str], target: Path) -> list[str]:
    if command[:1] == ["--"]:
        command = command[1:]
    if (command[:2] != ["--server", "--sender"] or len(command) < 5
            or command[-2] != "." or command[-1].rstrip("/") != str(target)):
        raise ValueError("only a read-only sender for the selected candidate is allowed")
    # Rsync's server archive/sparse options and compatibility suffix. Do not
    # accept arbitrary sender options: --log-file alone can write elsewhere.
    for option in command[2:-2]:
        if (re.fullmatch(r"-[logDtprS]+e\.[iLsfxCIvu]+", option) is None
                and option != "--numeric-ids"):
            raise ValueError("unsupported rsync sender option")
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("rsync_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.snapshot):
        parser.error("invalid snapshot date")
    clean, raw = ROOT / "data/clean", ROOT / "data/raw"
    target = clean / f"mysql-{args.snapshot}.building"
    try:
        command = sender_arguments(args.rsync_args, target)
    except ValueError as exc:
        parser.error(str(exc))
    if any(path.is_symlink() for path in (ROOT / "data", clean, raw)):
        parser.error("pipeline data directories must not be symlinks")
    pause = ROOT / "data/.updates-paused"
    if pause.is_symlink() or not pause.is_file():
        parser.error("automatic updates must remain paused")
    if target.is_symlink() or not target.is_dir():
        parser.error("candidate must be a regular directory")
    with source_activity(raw), pipeline_lock(clean):
        # Check before opening the checkpoint or system tablespace, too.
        if any(path.is_symlink() for path in target.rglob("*")):
            parser.error("candidate contains a symlink; inspect manually")
        assert_database_stopped(target)
        state = read_build_state(target)
        if not state or state.get("snapshot_version") != args.snapshot or state.get("phase") != "finalizing":
            parser.error("candidate must have a matching finalizing checkpoint")
        return subprocess.run(["rsync", *command], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
