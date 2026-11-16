#!/usr/bin/env python3
"""Cold-copy a complete release to a paused server; never publish or delete data.

Run send on the workstation. Install this same helper in the destination clone
first. Only the latest database/export and the requested small history reports
travel. Incomplete transfers stay in a dated incoming directory and are safely
resumable; --finish-only resumes interrupted installation without recopying.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import compact_update as update
import run_pipeline as pipeline
from source_activity import SourceActivityError, source_activity
from workstation_history import unmounted

GIB = 1024 ** 3


class TransferError(RuntimeError):
    pass


def snapshot(value):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("snapshot must be YYYY-MM-DD") from None
    return value


def regular_directory(path):
    path = path.absolute()
    if path.resolve(strict=True) != path or not path.is_dir():
        raise TransferError("existing directories without symlink components are required")
    return path


def document(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise TransferError("invalid or oversized release document")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise TransferError("release documents must be JSON objects")
    return value


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def declared_checksum(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise TransferError("invalid checksum file")
    fields = path.read_text(encoding="ascii").split()
    if not fields or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
        raise TransferError("invalid checksum declaration")
    return fields[0]


@contextmanager
def held_lock(path):
    """Use repository lock names; children inherit leases if the parent dies."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o444)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TransferError("unsafe transfer lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TransferError("another build, transfer or publication holds a required lock") from None
        yield descriptor
    finally:
        # Do not LOCK_UN: inherited open descriptions must protect a child that
        # outlives this Python process. Never remove a repository lock file.
        os.close(descriptor)


@contextmanager
def transfer_locks(clean, raw_dirs, *, receiver=False):
    with ExitStack() as stack:
        descriptors = []
        if not receiver:
            descriptors.append(stack.enter_context(held_lock(clean / ".workstation-history.lock")))
        for raw in sorted(set(raw_dirs)):
            lease = stack.enter_context(source_activity(raw))
            descriptors.extend(lease.child_options()["pass_fds"])
        names = [".automatic-update.lock", ".pipeline.lock", ".publish-live.lock"] if receiver else [".pipeline.lock"]
        for name in names:
            descriptors.append(stack.enter_context(held_lock(clean / name)))
        yield tuple(descriptors)


@contextmanager
def offline_database(datadir):
    """Keep MySQL from opening a stopped source during the long cold copy."""
    regular_directory(datadir)
    pipeline.assert_database_stopped(datadir)
    descriptor = os.open(datadir / "ibdata1", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TransferError("invalid InnoDB system tablespace")
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TransferError("MySQL opened the database before cold copying could start") from None
        yield
    finally:
        os.close(descriptor)


def artifact_names(selected, history):
    if not history or history != sorted(set(history)) or history[-1] != selected:
        raise TransferError("history must be unique, chronological and end with the selected snapshot")
    names = [f"mysql-{selected}", f"pkpbeacon-clean-{selected}.sql.gz",
             f"pkpbeacon-clean-{selected}.sql.gz.sha256"]
    for version in history:
        names += [f"pkpbeacon-changes-{version}.json",
                  f"pkpbeacon-changes-{version}.json.sha256",
                  f"pkpbeacon-release-{version}.json"]
    return names


def inventory(paths):
    """Apparent file sizes, not ZFS-compressed disk allocation, bound capacity."""
    rows = []
    for name, root in sorted(paths.items()):
        entries = [root, *sorted(root.rglob("*"))] if root.is_dir() and not root.is_symlink() else [root]
        for path in entries:
            info = path.lstat()
            relative = name if path == root else f"{name}/{path.relative_to(root).as_posix()}"
            if stat.S_ISREG(info.st_mode):
                rows.append([relative, "file", info.st_size])
            elif stat.S_ISDIR(info.st_mode):
                rows.append([relative, "directory", 0])
            else:
                raise TransferError("package contains a symlink or special file")
    rows.sort()
    return rows


def inventory_digest(rows):
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def verify_release(paths, selected, history, *, verify_export):
    def file(name):
        return paths[name]
    marker = document(file(f"mysql-{selected}") / update.MARKER)
    if any(marker.get(key) != value for key, value in {
        "schema_version": 1, "snapshot_date": selected, "clean_only": True,
        "data_directory_name": f"mysql-{selected}", "mysql_version": "8.4.11",
        "pipeline_version": "3",
    }.items()):
        raise TransferError("expected a complete, clean-only MySQL 8.4.11 release")
    previous = None
    for version in history:
        report_name = f"pkpbeacon-changes-{version}.json"
        export_name = f"pkpbeacon-clean-{version}.sql.gz"
        manifest = document(file(f"pkpbeacon-release-{version}.json"))
        report = document(file(report_name))
        report_hash = sha256(file(report_name))
        if any(manifest.get(key) != value for key, value in {
            "schema_version": 1, "snapshot_date": version,
            "change_report_filename": report_name, "change_report_sha256": report_hash,
            "clean_export_filename": export_name,
        }.items()) or declared_checksum(file(report_name + ".sha256")) != report_hash:
            raise TransferError("history report checksum or manifest mismatch")
        if not isinstance(manifest.get("clean_export_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["clean_export_sha256"]):
            raise TransferError("history manifest lacks its original export checksum")
        details = report.get("snapshot", {})
        if (report.get("schema_version") != 1 or details.get("date") != version
                or details.get("previous_snapshot_date") != previous
                or report.get("release_guard", {}).get("status") != "passed"):
            raise TransferError("history lineage or release guard mismatch")
        if version == selected:
            export = file(export_name)
            declared = declared_checksum(file(export_name + ".sha256"))
            if not declared or manifest.get("clean_export_sha256") != declared:
                raise TransferError("latest export checksum declaration mismatch")
            if verify_export and sha256(export) != declared:
                raise TransferError("latest export content checksum mismatch")
            if any(marker.get(key) != details.get(key) for key in ("source_sha256", "build_sql_sha256")):
                raise TransferError("database marker provenance mismatch")
        previous = version


def target_paths(directory, names):
    return {name: directory / name for name in names}


def receiver_arguments(command, target):
    if command[:1] == ["--"]:
        command = command[1:]
    if len(command) < 4 or command[0] != "--server" or command[-2:] != [".", str(target) + "/"]:
        raise TransferError("only rsync receiving into the selected incoming directory is allowed")
    # Rsync 3.x archive/relative/checksum compatibility bundle; no sender,
    # deletion, backup, log-file, or arbitrary path-bearing options are accepted.
    for option in command[1:-2]:
        if (not re.fullmatch(r"-[rtcR]+e\.[iLsfxCIvu]+", option)
                and option not in {"--inplace", "--partial", "--checksum", "--relative", "--fsync"}):
            raise TransferError("unsupported rsync receiver option")
    return command


def remote_layout(args):
    project = regular_directory(args.project_root)
    data = regular_directory(project / "data")
    clean = regular_directory(data / "clean")
    raw = regular_directory(data / "raw")
    if not os.path.ismount(data):
        raise TransferError("project data must be a mounted data filesystem")
    pause = data / ".updates-paused"
    if pause.is_symlink() or not pause.is_file():
        raise TransferError("automatic updates must remain paused")
    live = clean / "mysql-live"
    if live.exists() and not live.is_symlink():
        raise TransferError("mysql-live must be a managed symlink, not a regular path")
    if live.is_symlink() and live.resolve() in {clean / f"mysql-{args.snapshot}", clean / f".incoming-{args.snapshot}" / "files" / f"mysql-{args.snapshot}"}:
        raise TransferError("the selected incoming date must not be the live database")
    for name in (".compact-update.json",):
        if (clean / name).exists() or (clean / name).is_symlink():
            raise TransferError("resolve the pending publication journal first")
    return project, clean, raw


def plan(args):
    return {"schema_version": 1, "snapshot": args.snapshot, "history": args.history,
            "inventory_sha256": args.inventory_sha256, "total_bytes": args.total_bytes}


def combined_paths(clean, payload, names, installing):
    result = {}
    for name in names:
        staged, final = payload / name, clean / name
        if final.exists() or final.is_symlink():
            if not installing or staged.exists() or staged.is_symlink():
                raise TransferError("refusing to overwrite an existing destination artifact")
            result[name] = final
        else:
            result[name] = staged
    return result


def finish(args, clean, incoming, payload, names):
    receipt = document(incoming / "transfer.json")
    if any(receipt.get(key) != value for key, value in plan(args).items()):
        raise TransferError("incoming transfer identity does not match this source")
    if receipt.get("installed") is True:
        raise TransferError("this transfer is already installed; proceed to credential adoption and publication")
    if {path.name for path in payload.iterdir()} - set(names):
        raise TransferError("incoming directory contains unexpected artifacts")
    paths = combined_paths(clean, payload, names, receipt.get("installing") is True)
    rows = inventory(paths)
    if inventory_digest(rows) != args.inventory_sha256 or sum(row[2] for row in rows) != args.total_bytes:
        raise TransferError("received package inventory differs from the source")
    pipeline.assert_database_stopped(paths[f"mysql-{args.snapshot}"])
    unmounted(paths[f"mysql-{args.snapshot}"])
    print("[transfer] verifying export SHA-256 and historical reports; this can take time", file=sys.stderr, flush=True)
    verify_release(paths, args.snapshot, args.history, verify_export=True)
    receipt["installing"] = True
    update.atomic_json(incoming / "transfer.json", receipt)
    # Commit manifests last. On interruption --finish-only verifies the joined
    # staged/final package under the same journal before continuing the moves.
    ordered = sorted(names, key=lambda name: name.startswith("pkpbeacon-release-"))
    for name in ordered:
        if paths[name].parent == payload:
            if (clean / name).exists() or (clean / name).is_symlink():
                raise TransferError("destination changed during installation")
            os.rename(paths[name], clean / name)
            update.sync_directory(clean)
            update.sync_directory(payload)
    receipt["installed"] = True
    update.atomic_json(incoming / "transfer.json", receipt)
    print(f"[transfer] installed {args.snapshot}; updates remain paused; NOT published", file=sys.stderr, flush=True)


def receive(args):
    project, clean, raw = remote_layout(args)
    names = artifact_names(args.snapshot, args.history)
    incoming = clean / f".incoming-{args.snapshot}"
    payload = incoming / "files"
    with transfer_locks(clean, [raw], receiver=True) as descriptors:
        remote_layout(args)
        if args.mode == "finish":
            regular_directory(incoming)
            regular_directory(payload)
            finish(args, clean, incoming, payload, names)
            return 0
        combined_paths(clean, payload, names, False)
        if incoming.exists() or incoming.is_symlink():
            regular_directory(incoming)
            if document(incoming / "transfer.json") != plan(args):
                raise TransferError("incoming transfer differs or installation began; use --finish-only")
            regular_directory(payload)
        else:
            incoming.mkdir(mode=0o700)
            payload.mkdir(mode=0o700)
            update.atomic_json(incoming / "transfer.json", plan(args))
        existing = {path.name: path for path in payload.iterdir()}
        if set(existing) - set(names):
            raise TransferError("incoming directory contains unexpected artifacts")
        inventory(existing)  # reject existing symlinks/special files before rsync
        pipeline.assert_database_stopped(payload / f"mysql-{args.snapshot}")
        unmounted(payload / f"mysql-{args.snapshot}")
        allocated = sum(min(path.stat().st_size, path.stat().st_blocks * 512)
                        for root in existing.values() for path in ([root] if root.is_file() else root.rglob("*"))
                        if path.is_file())
        reserve = args.reserve_gib * GIB
        if shutil.disk_usage(clean).free < max(0, args.total_bytes - allocated) + reserve:
            raise TransferError("insufficient free data-disk space for apparent package size plus reserve")
        command = receiver_arguments(args.rsync_args, payload)
        process = subprocess.Popen(["rsync", *command], pass_fds=descriptors)
        try:
            while process.poll() is None:
                if shutil.disk_usage(clean).free < reserve:
                    raise TransferError("disk reserve reached; partial incoming copy retained")
                time.sleep(1)
            if process.returncode:
                return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finish(args, clean, incoming, payload, names)
        return 0


def send(args):
    clean = regular_directory(args.source_clean)
    names = artifact_names(args.snapshot, args.history)
    paths = target_paths(clean, names)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", args.host):
        raise TransferError("host must be an explicit user@hostname or user@IPv4 address")
    remote = args.remote_project
    if not remote.is_absolute() or ".." in remote.parts or str(remote) == "/":
        raise TransferError("remote project must be an absolute project path")
    with ExitStack() as stack:
        descriptors = stack.enter_context(transfer_locks(clean, [regular_directory(path) for path in args.source_raw]))
        rows = inventory(paths)
        stack.enter_context(offline_database(paths[f"mysql-{args.snapshot}"]))
        with redirect_stdout(sys.stderr):
            unmounted(paths[f"mysql-{args.snapshot}"], native_only=args.native_only_source)
        verify_release(paths, args.snapshot, args.history, verify_export=False)
        total = sum(row[2] for row in rows)
        print(f"[transfer] stopped source validated; {total / GIB:.1f} GiB apparent data; destination reserve {args.reserve_gib} GiB", flush=True)
        ssh = ["ssh", "-F", "/dev/null", "-i", str(args.identity.absolute()),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"]
        helper = ["sudo", "-n", "-u", "ojs", "python3", "-B",
                  str(remote / "scripts/transfer_completed_release.py"),
                  "finish" if args.finish_only else "receive", "--project-root", str(remote),
                  "--snapshot", args.snapshot, "--inventory-sha256", inventory_digest(rows),
                  "--total-bytes", str(total), "--reserve-gib", str(args.reserve_gib)]
        for version in args.history:
            helper.extend(["--history", version])
        if args.finish_only:
            return subprocess.run([*ssh, args.host, shlex.join(helper)], check=False, pass_fds=descriptors).returncode
        target = remote / "data/clean" / f".incoming-{args.snapshot}" / "files"
        command = ["rsync", "-rtR", "--checksum", "--inplace", "--partial", "--fsync", "--info=progress2",
                   "-e", shlex.join(ssh), "--rsync-path", shlex.join(helper + ["--"]),
                   *[str(clean) + "/./" + name for name in names], f"{args.host}:{target}/"]
        return subprocess.run(command, check=False, pass_fds=descriptors).returncode


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    for mode in ("send", "receive", "finish"):
        sub = modes.add_parser(mode)
        sub.add_argument("--snapshot", type=snapshot, required=True)
        sub.add_argument("--history", type=snapshot, action="append", required=True)
        sub.add_argument("--reserve-gib", type=int, default=20)
        if mode == "send":
            sub.add_argument("--source-clean", type=Path, required=True)
            sub.add_argument("--source-raw", type=Path, action="append", required=True)
            sub.add_argument("--host", required=True)
            sub.add_argument("--identity", type=Path, required=True)
            sub.add_argument("--remote-project", type=Path, default=Path("/srv/ojs_api"))
            sub.add_argument("--finish-only", action="store_true")
            sub.add_argument("--native-only-source", action="store_true",
                             help="explicit assertion that this desktop-built source has never been container-managed; mount and InnoDB lock checks still apply")
        else:
            sub.add_argument("--project-root", type=Path, default=ROOT)
            sub.add_argument("--inventory-sha256", required=True)
            sub.add_argument("--total-bytes", type=int, required=True)
            if mode == "receive":
                sub.add_argument("rsync_args", nargs=argparse.REMAINDER)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.reserve_gib < 20:
        raise SystemExit("error: reserve must be at least 20 GiB")
    if args.mode != "send" and (args.total_bytes <= 0 or not re.fullmatch(r"[0-9a-f]{64}", args.inventory_sha256)):
        raise SystemExit("error: invalid transfer identity")
    old_umask = os.umask(0o077)
    def interrupted(signum, frame):
        raise TransferError("transfer interrupted; incoming files retained, not published")
    old_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        return send(args) if args.mode == "send" else receive(args)
    except (TransferError, pipeline.PipelineError, SourceActivityError, update.UpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyboardInterrupt):
        print("error: transfer failed; inspect paths, permissions and incoming state; no publication attempted", file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        os.umask(old_umask)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
