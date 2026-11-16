#!/usr/bin/env python3
"""Continue explicit dated snapshots on a guarded workstation, in order.

Run beneath run_guarded_local_build.py in a bounded service. The existing
baseline is never rebuilt. Each snapshot imports its predecessor's clean
export, preserving identities and event history. A completed release is an
atomic manifest, not a successful-looking heartbeat or process exit alone.

Interrupted imports/finalization are deliberately not retried destructively:
only an unchanged metadata_ready checkpoint supports automatic resume.
Optional pruning removes superseded stopped datadirs, never raw inputs or
clean exports. Publication on a serving host is a separate operation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import date
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import workstation_recover as recovery

pipeline = recovery.run_pipeline
from compact_update import UpdateError, validate_release
from publish_live import PublishError
from source_activity import SourceActivityError, source_activity


def version(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("snapshot dates must use YYYY-MM-DD")
    return value


def regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise pipeline.PipelineError("history artifacts must be regular non-symlinked files")


def report_for(clean: Path, selected: str) -> dict:
    """Check small provenance documents; the importer checks the full export."""
    if not pipeline.release_artifacts_complete(clean, selected):
        raise pipeline.PipelineError("history requires a committed baseline and completed predecessor")
    export = pipeline.clean_export_path(clean, selected)
    report = pipeline.change_report_path(clean, selected)
    for path in (export, report, pipeline.release_manifest_path(clean, selected),
                 export.with_name(export.name + ".sha256"), report.with_name(report.name + ".sha256")):
        regular(path)
    if report.stat().st_size > 1024 * 1024:
        raise pipeline.PipelineError("history change report is unexpectedly large")
    pipeline.verify_checksum_sidecar(report)
    document = json.loads(report.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or document.get("snapshot", {}).get("date") != selected
            or document.get("release_guard", {}).get("status") != "passed"):
        raise pipeline.PipelineError("history release guard and report must have passed")
    return document


def confirm_source(snapshot: pipeline.Snapshot, report: dict, previous: str) -> None:
    recorded = report.get("snapshot", {})
    expected = {"date": snapshot.version, "previous_snapshot_date": previous,
                "source_filename": snapshot.path.name, "source_size_bytes": snapshot.size,
                "completed_at": snapshot.completed_at.isoformat()}
    if any(recorded.get(key) != value for key, value in expected.items()):
        raise pipeline.PipelineError("completed snapshot does not match the requested chronological chain")
    if snapshot.path.name.endswith(".gz"):
        metadata = pipeline.read_validated_metadata(snapshot.path)
        if metadata is None or metadata.get("uncompressed_sha256") != recorded.get("source_sha256"):
            raise pipeline.PipelineError("completed snapshot does not match validated source bytes")
    else:
        if pipeline.snapshot_sha256(snapshot) != recorded.get("source_sha256"):
            raise pipeline.PipelineError("completed snapshot does not match source bytes")


def attempt_receipt(clean: Path, snapshot: pipeline.Snapshot, previous: str, *, required: bool) -> None:
    """Bind metadata resume to the predecessor that was actually selected."""
    expected = {"schema_version": 1, "snapshot_version": snapshot.version,
                "source_filename": snapshot.path.name, "source_size_bytes": snapshot.size,
                "previous_snapshot_date": previous,
                "previous_export_sha256": pipeline._declared_checksum(pipeline.clean_export_path(clean, previous)),
                "build_sql_sha256": pipeline.sha256_small_file(pipeline.DEFAULT_BUILD_SQL)}
    if expected["previous_export_sha256"] is None:
        raise pipeline.PipelineError("attempt predecessor has no valid clean export checksum")
    path = clean / f".workstation-history-{snapshot.version}.json"
    if path.exists() or path.is_symlink():
        regular(path)
        if path.stat().st_size > 65536 or json.loads(path.read_text(encoding="utf-8")) != expected:
            raise pipeline.PipelineError("attempt receipt differs from the selected predecessor or build")
    elif required:
        raise pipeline.PipelineError("existing staging lacks a workstation predecessor receipt; retained for inspection")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(expected, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        descriptor = os.open(clean, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def resume_arguments(clean: Path, snapshot: pipeline.Snapshot, previous: str) -> list[str]:
    staging = clean / f"mysql-{snapshot.version}.building"
    final = clean / f"mysql-{snapshot.version}"
    if final.exists() or final.is_symlink():
        raise pipeline.PipelineError("uncommitted final database requires manual inspection")
    if not staging.exists() and not staging.is_symlink():
        attempt_receipt(clean, snapshot, previous, required=False)
        return []
    if staging.is_symlink() or not staging.is_dir():
        raise pipeline.PipelineError("refusing an invalid staging database")
    attempt_receipt(clean, snapshot, previous, required=True)
    regular(pipeline.build_state_path(staging))
    checkpoint = pipeline.read_build_state(staging)
    if (checkpoint is not None and "metadata_storage_profile" in checkpoint
            and checkpoint["metadata_storage_profile"] != pipeline.METADATA_STORAGE_PROFILE):
        raise pipeline.PipelineError("staging metadata storage profile is unsupported; retained for inspection")
    expected = {"pipeline_version": pipeline.PIPELINE_VERSION,
                "snapshot_version": snapshot.version, "source_filename": snapshot.path.name,
                "source_size_bytes": snapshot.size, "phase": "metadata_ready",
                "build_sql_sha256": pipeline.sha256_small_file(pipeline.DEFAULT_BUILD_SQL)}
    if (checkpoint is None or any(checkpoint.get(key) != value for key, value in expected.items())
            or re.fullmatch(r"[0-9a-f]{64}", str(checkpoint.get("source_sha256", ""))) is None):
        raise pipeline.PipelineError("staging is not an unchanged metadata_ready checkpoint; retained for inspection")
    pipeline.assert_database_stopped(staging)
    return ["--resume-building"]


def chain_state(clean: Path, baseline: str, snapshots: list[pipeline.Snapshot]) -> list[str]:
    completed = chain_versions(clean, baseline, [item.version for item in snapshots])
    previous = baseline
    for snapshot in snapshots:
        if snapshot.version in completed:
            confirm_source(snapshot, report_for(clean, snapshot.version), previous)
        previous = snapshot.version
    return completed


def chain_versions(clean: Path, baseline: str, versions: list[str]) -> list[str]:
    report_for(clean, baseline)
    if versions != sorted(set(versions)) or not versions or versions[0] <= baseline:
        raise pipeline.PipelineError("snapshots must follow the baseline in strictly increasing order")
    completed = [item.version for item in pipeline.discover_clean_exports(clean, require_complete=True)
                 if item.version > baseline]
    if completed != versions[:len(completed)]:
        raise pipeline.PipelineError("existing releases are not a completed prefix of the requested chain")
    pending = versions[len(completed):]
    for staging in clean.glob("mysql-*.building"):
        if not pending or staging.name != f"mysql-{pending[0]}.building":
            raise pipeline.PipelineError("unrelated or out-of-order staging database requires manual inspection")
    previous = baseline
    for selected in completed:
        if report_for(clean, selected)["snapshot"].get("previous_snapshot_date") != previous:
            raise pipeline.PipelineError("completed history has an unexpected predecessor")
        previous = selected
    return completed


@contextmanager
def workflow_lock(clean: Path):
    path = clean / ".workstation-history.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, 0o444)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise pipeline.PipelineError("history workflow lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise pipeline.PipelineError("another workstation history workflow is running") from None
        yield
    finally:
        os.close(descriptor)


def check_capacity(clean: Path, scratch: Path, *, memory: int, reserve: int, disk: int) -> None:
    available = recovery.host_available_memory()
    if available - memory < reserve:
        raise pipeline.PipelineError("requested buffer pool would breach the host memory reserve")
    measurements = {"host_available_bytes": available}
    for name, path in (("clean", clean), ("scratch", scratch)):
        free = shutil.disk_usage(path).free
        measurements[name + "_free_bytes"] = free
        if free <= disk:
            raise pipeline.PipelineError("workstation disk reserve reached; existing artifacts retained")
    print("[history-capacity] " + json.dumps(measurements, sort_keys=True), flush=True)


def unmounted(datadir: Path, *, mountinfo: Path = Path("/proc/self/mountinfo"), native_only: bool = False) -> None:
    """Reject mountpoints, submounts, bind sources, and Docker mount references."""
    mounts = []
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        fields, details = before.split(), after.split()
        if not separator or len(fields) < 5 or not details:
            raise pipeline.PipelineError("mount measurement is unavailable")
        root, mountpoint = (Path(re.sub(r"\\([0-7]{3})", lambda item: chr(int(item.group(1), 8)), field))
                            for field in (fields[3], fields[4]))
        if not mountpoint.is_absolute():
            raise pipeline.PipelineError("mount coordinates are unavailable")
        if mountpoint == datadir or mountpoint.is_relative_to(datadir):
            raise pipeline.PipelineError("superseded database has a mount reference")
        # snapd may bind a mount-namespace handle at /run/snapd/ns/*.mnt.
        # Such nsfs roots are kernel handles, not paths into a data filesystem.
        # Only recognize this precise form after checking its mountpoint.
        if details[0] == "nsfs" and re.fullmatch(r"mnt:\[[0-9]+\]", fields[3]):
            continue
        if not root.is_absolute():
            raise pipeline.PipelineError("mount coordinates are unavailable")
        mounts.append((fields[0], fields[2], root, mountpoint))
    containing = [item for item in mounts if datadir.is_relative_to(item[3])]
    if not containing:
        raise pipeline.PipelineError("database filesystem coordinates are unavailable")
    own = max(containing, key=lambda item: len(item[3].parts))
    # mountinfo roots are relative to the filesystem, not the visible host
    # root. On a home ZFS dataset /home/user/path may therefore be /path.
    # Compare coordinates on the same device to find both subtree and ancestor
    # aliases, not only mounts whose root happens to equal the absolute path.
    filesystem_path = own[2] / datadir.relative_to(own[3])
    for identifier, device, root, _ in mounts:
        if identifier != own[0] and device == own[1] and (
                root == filesystem_path or root.is_relative_to(filesystem_path)
                or filesystem_path.is_relative_to(root)):
            raise pipeline.PipelineError("superseded database has a same-filesystem mount alias")
    if native_only:
        # Explicit operator assertion, never an automatic fallback for a failed
        # Docker inspection. Only use for locally built directories that have
        # never been assigned to a container. assert_database_stopped also
        # tests InnoDB's file lock, including containerized mysqld ownership.
        print("[history-cleanup] explicit native-only scope; Docker container references are not inspected", flush=True)
        return
    docker = shutil.which("docker")
    if docker:
        result = subprocess.run([docker, "ps", "--all", "--quiet"], capture_output=True,
                                text=True, timeout=30)
        if result.returncode:
            raise pipeline.PipelineError("cannot inspect Docker mounts; cleanup stopped safely")
        identifiers = result.stdout.split()
        if identifiers:
            result = subprocess.run([docker, "inspect", *identifiers], capture_output=True,
                                    text=True, timeout=30)
            if result.returncode:
                raise pipeline.PipelineError("cannot inspect Docker mounts; cleanup stopped safely")
            for container in json.loads(result.stdout):
                for mount in container.get("Mounts", []):
                    source = mount.get("Source")
                    if source:
                        path = Path(source).resolve()
                        if path == datadir or path.is_relative_to(datadir) or datadir.is_relative_to(path):
                            raise pipeline.PipelineError("superseded database is referenced by Docker")


def prune_superseded(clean: Path, previous: str, successor: str, allowed: set[str], *, native_only: bool = False) -> bool:
    """Remove only one owned stopped datadir after validating both exports."""
    if previous not in allowed or successor not in allowed or previous >= successor:
        raise pipeline.PipelineError("cleanup is restricted to the explicitly selected history chain")
    datadir = clean / f"mysql-{previous}"
    if not datadir.exists() and not datadir.is_symlink():
        return False
    if datadir.is_symlink() or not datadir.is_dir() or datadir.parent.resolve() != clean:
        raise pipeline.PipelineError("cleanup requires a direct non-symlinked managed database")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise pipeline.PipelineError("safe datadir cleanup is unavailable on this platform")
    for selected in (previous, successor):
        report_for(clean, selected)
        release = validate_release(clean, selected)
        if selected == successor and release.report["snapshot"].get("previous_snapshot_date") != previous:
            raise pipeline.PipelineError("cleanup successor does not preserve the predecessor history")
        marker = release.datadir / "OJS_COMPACT_RELEASE.json"
        regular(marker)
    for pointer in clean.iterdir():
        if pointer.is_symlink():
            target = pointer.resolve()
            if target == datadir or target.is_relative_to(datadir):
                raise pipeline.PipelineError("cleanup target is referenced by a release pointer")
    pipeline.assert_database_stopped(datadir)
    unmounted(datadir, native_only=native_only)
    original = datadir.stat()
    if original.st_uid != os.geteuid():
        raise pipeline.PipelineError("cleanup target must be owned by the workstation user")
    # Walk without following links; foreign mounts and unexpected symlinks are
    # rejected before rmtree rather than silently crossing ownership boundaries.
    for directory, names, files in os.walk(datadir, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            metadata = path.lstat()
            if path.is_symlink() or metadata.st_dev != original.st_dev or metadata.st_uid != os.geteuid():
                raise pipeline.PipelineError("cleanup target contains a symlink, foreign filesystem, or foreign-owned file")
    pipeline.assert_database_stopped(datadir)
    latest = datadir.stat()
    if (latest.st_dev, latest.st_ino) != (original.st_dev, original.st_ino):
        raise pipeline.PipelineError("cleanup target changed during validation")
    shutil.rmtree(datadir)
    print(f"[history-cleanup] removed {datadir.name}; checksummed clean export retained for recovery", flush=True)
    return True


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--credentials-env", type=Path, required=True)
    result.add_argument("--api-password-file", type=Path, required=True)
    result.add_argument("--mysql-bin", type=Path, required=True)
    result.add_argument("--mysql-version", default="8.4.11")
    result.add_argument("--baseline-version", required=True)
    result.add_argument("--snapshot", type=Path, action="append", required=True)
    result.add_argument("--clean-dir", type=Path, required=True)
    result.add_argument("--scratch-dir", type=Path, required=True)
    result.add_argument("--buffer-pool-size", default="4G")
    result.add_argument("--metadata-workers", type=int, default=1)
    result.add_argument("--min-available-memory-gib", default="8")
    result.add_argument("--disk-reserve-gib", default="40")
    result.add_argument("--prune-superseded-datadirs", action="store_true")
    result.add_argument("--native-only-cleanup", action="store_true",
                        help="explicit assertion that selected generated datadirs were never container-managed; checks native processes, InnoDB locks and mounts but not Docker metadata")
    result.add_argument("--check-only", "--plan-only", action="store_true",
                        help="check private files, tools, resources and named chronology without scanning large gzip files, starting MySQL or pruning")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        baseline = version(args.baseline_version)
        clean = args.clean_dir.expanduser().absolute()
        if clean.resolve(strict=True) != clean or not clean.is_dir() or clean == Path(clean.anchor):
            raise pipeline.PipelineError("clean directory must be an existing non-symlinked workspace directory")
        if args.metadata_workers < 1:
            raise pipeline.PipelineError("metadata workers must be positive")
        if args.native_only_cleanup and not args.prune_superseded_datadirs:
            raise pipeline.PipelineError("native-only cleanup requires explicit datadir pruning")
        memory = recovery.buffer_pool_bytes(args.buffer_pool_size)
        reserve = recovery.scaled_limit(args.min_available_memory_gib, recovery.GIB)
        disk = recovery.scaled_limit(args.disk_reserve_gib, recovery.GIB)
        scratch = recovery.disk_scratch(args.scratch_dir)
        tools = recovery.matching_tools(args.mysql_bin, args.mysql_version)
        paths = [path.expanduser().absolute() for path in args.snapshot]
        versions = []
        for path in paths:
            regular(path)
            if path.resolve(strict=True) != path:
                raise pipeline.PipelineError("raw inputs must not contain symlinked path components")
            match = pipeline.SNAPSHOT_NAME_RE.fullmatch(path.name)
            if match is None:
                raise pipeline.PipelineError("raw inputs require dated snapshot filenames")
            versions.append(version(match.group(1)))
        settings = recovery.credentials(args.credentials_env)
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", settings.get("OJS_DB_API_USER", "ojs_api")) is None:
            raise pipeline.PipelineError("API database user is invalid")
        api_password = recovery.private_text(args.api_password_file, limit=4096).rstrip("\n")
        if not api_password or "\n" in api_password or "\r" in api_password:
            raise pipeline.PipelineError("API password must contain exactly one nonempty line")
        with ExitStack() as stack:
            stack.enter_context(workflow_lock(clean))
            for raw in sorted({path.parent for path in paths}):
                stack.enter_context(source_activity(raw))
            with pipeline.pipeline_lock(clean):
                completed = chain_versions(clean, baseline, versions)
            print("[history-plan] " + json.dumps({"baseline": baseline,
                  "snapshots": versions, "already_completed": completed,
                  "remaining": len(versions) - len(completed),
                  "prune_superseded_datadirs": args.prune_superseded_datadirs,
                  "native_only_cleanup": args.native_only_cleanup}), flush=True)
            check_capacity(clean, scratch, memory=memory, reserve=reserve, disk=disk)
            if args.check_only:
                print("[history-preflight] private files, tools, named chronology and resource floors passed; full raw/source validation deferred to the guarded build", flush=True)
                return 0
            snapshots = [pipeline.inspect_snapshot(path) for path in paths]
            with pipeline.pipeline_lock(clean):
                completed = chain_state(clean, baseline, snapshots)
            os.environ.update(settings)
            os.environ.pop("OJS_DB_API_PASSWORD", None)
            os.environ["OJS_DB_API_PASSWORD_FILE"] = str(args.api_password_file.resolve())
            os.environ["PATH"] = str(Path(tools.mysqld).parent) + os.pathsep + os.environ.get("PATH", "")
            os.environ["OJS_MYSQL_TMP_ROOT"] = str(scratch)
            os.environ["OJS_BUILDER_MYSQL_TEMPTABLE_MAX_RAM"] = "256M"
            os.environ["OJS_BUILDER_MYSQL_MAX_CONNECTIONS"] = "8"
            os.environ["OJS_PROGRESS_SECONDS"] = "30"
            # Schema widening may copy a large InnoDB table. Its preflight
            # must retain the same reserve as the outer resource supervisor.
            os.environ["OJS_METADATA_MIGRATION_RESERVE_BYTES"] = str(disk)
            previous = baseline
            allowed = {baseline, *(item.version for item in snapshots)}
            for snapshot in snapshots:
                if snapshot.version not in completed:
                    check_capacity(clean, scratch, memory=memory, reserve=reserve, disk=disk)
                    with pipeline.pipeline_lock(clean):
                        chain_state(clean, baseline, snapshots)
                        resume = resume_arguments(clean, snapshot, previous)
                    print(f"[history-stage] building {snapshot.version} from {previous}", flush=True)
                    result = pipeline.main([
                        "--snapshot", str(snapshot.path), "--raw-dir", str(snapshot.path.parent),
                        "--clean-dir", str(clean), "--api-db-user", settings.get("OJS_DB_API_USER", "ojs_api"),
                        "--mysql-buffer-pool-size", args.buffer_pool_size,
                        "--metadata-workers", str(args.metadata_workers),
                        "--skip-download", "--compact-storage", *resume,
                    ])
                    if result:
                        return result
                    # A busy-source skip can return 0; require the actual commit.
                    confirm_source(snapshot, report_for(clean, snapshot.version), previous)
                    print(f"[history-stage] committed {snapshot.version}", flush=True)
                else:
                    print(f"[history-stage] already committed {snapshot.version}; no rebuild", flush=True)
                if args.prune_superseded_datadirs:
                    with pipeline.pipeline_lock(clean):
                        prune_superseded(clean, previous, snapshot.version, allowed,
                                         native_only=args.native_only_cleanup)
                previous = snapshot.version
            # Idempotent skips must still leave a usable stopped final release;
            # an old manifest alone is not proof that the export/datadir remain.
            with pipeline.pipeline_lock(clean):
                final_release = validate_release(clean, previous)
                pipeline.assert_database_stopped(final_release.datadir)
            print(f"[history-complete] latest={previous}; chronological exports and change reports retained; not deployed", flush=True)
        return 0
    except (OSError, ValueError, TypeError, AttributeError, KeyError, recovery.GuardError,
            subprocess.SubprocessError, SourceActivityError, pipeline.PipelineError,
            UpdateError, PublishError):
        # Config/parser exceptions may embed secret contents. Only our own
        # bounded static diagnosis is printed; detailed pipeline failures log
        # themselves before returning a nonzero status.
        print("error: workstation history stopped safely; check chronological committed releases, resumable checkpoint, private files, resource reserves, and matching tools; existing artifacts retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
