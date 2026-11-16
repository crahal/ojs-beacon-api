#!/usr/bin/env python3
"""Build and switch a compact Beacon release, retaining rollback until verified.

The live pointer is independent of the pipeline's candidate pointer. A durable
journal makes an interrupted switch roll back before another build can start.
Only authenticated, generated artifacts are eligible for bounded retention.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import download_beacon
import publish_live
import run_pipeline
from build_baseline import assert_automatic_baseline
from build_budget import build_budget, existing_lock_busy, scratch_reserve_gib
from export_handoff import (validate_retired_baseline, validate_resume_handoff,
                            document_sha256, retirement_receipt_path,
                            read_checkpoint_document, successor_export_reuse_metadata)
from mount_safety import assert_cleanup_unmounted
from progress_logging import Progress, progress_interval
from source_activity import SourceActivityBusy, source_activity, source_activity_child_options

ROOT = Path(__file__).resolve().parents[1]
DATE = r"\d{4}-\d{2}-\d{2}"
DATADIR_RE = re.compile(rf"mysql-({DATE})$")
MARKER = "OJS_COMPACT_RELEASE.json"
GIB = 1024 ** 3
COMMAND_TIMEOUT_MESSAGE = "command timed out; check service availability and timeout settings"
PAUSE_MARKER = ".updates-paused"


class UpdateError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise UpdateError(f"expected a regular JSON file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UpdateError(f"expected a JSON object: {path}")
    return value


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise UpdateError(f"refusing symlinked state file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(value, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def automatic_lock(clean: Path):
    clean.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(clean / ".automatic-update.lock",
                         os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another automatic update is already running") from exc
        yield
    finally:
        os.close(descriptor)


def safe_child(directory: Path, name: str) -> Path:
    if Path(name).name != name or name in ("", ".", ".."):
        raise UpdateError("state contains an unsafe artifact name")
    path = directory / name
    if path.is_symlink() or path.parent.resolve() != directory.resolve():
        raise UpdateError(f"refusing symlinked artifact: {path}")
    return path


def live_directory(clean: Path, name: str = "mysql-live") -> Path | None:
    link = clean / name
    if not link.is_symlink():
        if link.exists():
            raise UpdateError(f"expected a managed symlink: {link}")
        return None
    target = link.resolve(strict=True)
    if target.parent != clean or not DATADIR_RE.fullmatch(target.name):
        raise UpdateError(f"unexpected database pointer target: {link}")
    if not target.is_dir():
        raise UpdateError(f"database pointer is not a directory: {link}")
    return target


def set_live(clean: Path, target: Path | None) -> None:
    link = clean / "mysql-live"
    if link.exists() and not link.is_symlink():
        raise UpdateError(f"refusing to replace unmanaged live directory: {link}")
    if target is None:
        link.unlink(missing_ok=True)
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=".mysql-live.", dir=clean)
        os.close(descriptor)
        Path(temporary).unlink()
        try:
            Path(temporary).symlink_to(target.name)
            os.replace(temporary, link)
        finally:
            Path(temporary).unlink(missing_ok=True)
    sync_directory(clean)


@dataclass(frozen=True)
class Release:
    date: str
    datadir: Path
    export: Path
    source_sha256: str
    build_sql_sha256: str
    report: dict


def validate_release(clean: Path, date: str, *, require_directory: bool = True,
                     retired_successor: str | None = None) -> Release:
    if not re.fullmatch(DATE, date):
        raise UpdateError("invalid release date")
    export = safe_child(clean, f"pkpbeacon-clean-{date}.sql.gz")
    for name in (export.name + ".sha256", f"pkpbeacon-changes-{date}.json",
                 f"pkpbeacon-changes-{date}.json.sha256", f"pkpbeacon-release-{date}.json"):
        path = safe_child(clean, name)
        if not path.is_file():
            raise UpdateError(f"incomplete release: {path}")
    if not export.exists() and retired_successor is not None:
        # This exception is for OLD-artifact cleanup only. A candidate being
        # published must always possess its real checksum-verified export.
        validate_retired_baseline(clean, date, retired_successor)
        authenticated_report = read_json(clean / f"pkpbeacon-changes-{date}.json")
        source_hash = authenticated_report["snapshot"]["source_sha256"]
        build_hash = authenticated_report["snapshot"]["build_sql_sha256"]
    else:
        with Progress("update-release-checksums", disk_path=clean,
                      step="clean-export") as progress:
            checksum = publish_live.checksum_for(export)
            publish_live.verify_export_checksum(export, checksum)
            progress.event("export-verified", step="release-provenance")
            source_hash, build_hash = publish_live.candidate_provenance(export, date, checksum)
    report = read_json(clean / f"pkpbeacon-changes-{date}.json")
    if report.get("release_guard", {}).get("status") not in ("passed", "overridden"):
        raise UpdateError(f"release guard has not passed for {date}")
    datadir = safe_child(clean, f"mysql-{date}")
    if require_directory:
        marker = read_json(datadir / MARKER)
        expected = {"schema_version": 1, "snapshot_date": date,
                    "source_sha256": source_hash, "build_sql_sha256": build_hash,
                    "clean_only": True, "data_directory_name": datadir.name}
        if any(marker.get(key) != value for key, value in expected.items()):
            raise UpdateError(f"invalid compact database ownership marker: {datadir}")
    return Release(date, datadir, export, source_hash, build_hash, report)


class Runner:
    """Commands never include secret values, and failures do not echo stderr."""
    def run(self, command: list[str], *, cwd: Path, env: dict,
            timeout: float = 180) -> str:
        try:
            result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # TimeoutExpired contains argv and captured output; neither is
            # suitable for coordinator logs or exception chaining.
            raise UpdateError(COMMAND_TIMEOUT_MESSAGE) from None
        if result.returncode:
            if "Beacon authentication failed" in result.stderr:
                raise UpdateError("Beacon authentication failed; check the configured credentials file")
            raise UpdateError(f"{Path(command[0]).name} command failed (exit {result.returncode})")
        return result.stdout

    def child(self, command: list[str], *, cwd: Path, env: dict,
              reserve_check, timeout: float, pass_fds: tuple[int, ...] = ()) -> None:
        # Interrupt Python first so its finally block shuts down local mysqld;
        # client subprocesses are interrupted with the same process group.
        process = subprocess.Popen(command, cwd=cwd, env=env, pass_fds=pass_fds,
                                   start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                reserve_check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateError("update exceeded its configured runtime")
                try:
                    process.wait(timeout=min(15, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise UpdateError(f"update child failed (exit {process.returncode})")
        except BaseException:
            # A cancelled multi-day INSERT can take hours to roll back. Killing
            # the Python supervisor after 180 seconds used to strand mysqld and
            # let systemd SIGKILL it. Keep the lease and wait for safe shutdown.
            previous_signals = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous_signals[signum] = signal.signal(signum, signal.SIG_IGN)
            try:
                try:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                with Progress("update-graceful-shutdown", step="wait-for-child-rollback"):
                    while True:
                        try:
                            process.wait(timeout=30)
                            break
                        except subprocess.TimeoutExpired:
                            pass
            finally:
                for signum, previous in previous_signals.items():
                    signal.signal(signum, previous)
            raise


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise UpdateError("API health check refused an HTTP redirect")


class Coordinator:
    def __init__(self, args: argparse.Namespace, runner: Runner | None = None):
        self.args = args
        self.root = args.project_root.expanduser().resolve()
        self.clean = self.root / "data" / "clean"
        self.raw = self.root / "data" / "raw"
        for directory in (self.root / "data", self.clean, self.raw):
            if directory.is_symlink():
                raise UpdateError(f"refusing symlinked data directory: {directory}")
        self.env = os.environ.copy()
        # The parent and pipeline must enforce the same selected reserve,
        # including an explicit command-line override of the environment.
        if hasattr(args, "reserve_gb"):
            self.env["OJS_DISK_RESERVE_GB"] = str(args.reserve_gb)
        self.runner = runner or Runner()
        self.journal = self.clean / ".compact-update.json"
        self.deadline = time.monotonic() + args.max_runtime_hours * 3600
        self._compatible_tools: run_pipeline.MySQLTools | None = None

    def command(self, command, *, env=None, timeout=180):
        return self.runner.run(command, cwd=self.root, env=env or self.env, timeout=timeout)

    def capacity(self, minimum: float, *, separate_scratch_reserve: bool = False) -> None:
        path = self.clean if self.clean.exists() else self.root
        paths = [("data", path)]
        if self.raw.exists() and self.raw.stat().st_dev != path.stat().st_dev:
            paths.append(("raw", self.raw))
        scratch = run_pipeline.mysql_scratch_root(
            configured=self.env.get("OJS_MYSQL_TMP_ROOT", ""),
        )
        if scratch is not None:
            paths.append(("MySQL scratch", scratch))
        # Never let a smaller scratch reserve weaken the data reserve when
        # both directories are on the same filesystem.
        data_devices = {directory.stat().st_dev for label, directory in paths
                        if label != "MySQL scratch"}
        for label, directory in paths:
            required = minimum
            if separate_scratch_reserve and label == "MySQL scratch":
                required = scratch_reserve_gib(self.env, minimum)
                if directory.stat().st_dev in data_devices:
                    required = max(required, minimum)
            free = shutil.disk_usage(directory).free
            if free < required * GIB:
                raise UpdateError(f"disk reserve reached on {label}: {free / GIB:.1f} GiB free; "
                                  f"{required:g} GiB required; existing database files retained")

    def memory_capacity(self) -> None:
        minimum_mb = int(self.env.get("OJS_MIN_AVAILABLE_MEMORY_MB", "512"))
        if minimum_mb < 0:
            raise UpdateError("OJS_MIN_AVAILABLE_MEMORY_MB must be nonnegative")
        memory = Path("/proc/meminfo").read_text()
        available = re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.MULTILINE)
        if available is None:
            raise UpdateError("host available memory measurement is unavailable")
        if available and int(available.group(1)) < minimum_mb * 1024:
            raise UpdateError(f"less than {minimum_mb} MiB memory is available for the build")

    def build_capacity(self, probe: dict, *, downloaded: bool = False) -> dict:
        scratch = run_pipeline.mysql_scratch_root(
            configured=self.env.get("OJS_MYSQL_TMP_ROOT", ""))
        return build_budget(
            clean=self.clean, raw=self.raw, scratch=scratch, env=self.env,
            needs_download=probe["needs_download"] and not downloaded,
            remote_size=probe.get("remote", {}).get("size"),
            reserve_gib=self.args.reserve_gb, minimum_gib=self.args.min_free_gb)

    def require_build_capacity(self, probe: dict, *, downloaded: bool = False) -> None:
        report = self.build_capacity(probe, downloaded=downloaded)
        print(json.dumps({"event": "build-capacity", **report}, sort_keys=True))
        if not report["approved"]:
            raise UpdateError("automatic build not admitted: " + ", ".join(report["blockers"]) +
                              "; review the working-space budget; live database retained")

    def export_resume_capacity(self, version: str) -> dict:
        """Read-only export admission; reclamation remains conditional on validation."""
        live = live_directory(self.clean)
        if live is None:
            raise UpdateError("export recovery has no verified rollback catalogue")
        state = validate_resume_handoff(self.clean, version, live.name[6:])
        configuration_blockers = []
        try:
            if not run_pipeline.retirement_enabled(self.env):
                configuration_blockers.append("export_retirement_not_enabled")
            if not run_pipeline.deferred_payload_enabled(self.env):
                configuration_blockers.append("export_deferred_payload_not_enabled")
            page_kb = run_pipeline.compression_page_kb(self.env)
            events = run_pipeline.event_history_enabled(self.env)
            expected_profiles = {
                "storage_layout_profile": run_pipeline.layout_profile(page_kb) if page_kb != 8 else None,
                "event_history_profile": run_pipeline.event_layout_profile(page_kb) if events else None,
            }
            if any(state.get(key) != value for key, value in expected_profiles.items()):
                configuration_blockers.append("export_storage_profile_mismatch")
        except ValueError:
            configuration_blockers.append("export_recovery_configuration_invalid")
        baseline = state["export_handoff"]["baseline"]
        export = safe_child(self.clean, baseline["export_filename"])
        credit = baseline["export_size_bytes"] if export.exists() else 0
        env = dict(self.env, OJS_BUILD_WORKSPACE_GB=self.env.get("OJS_BUILD_EXPORT_GB", ""))
        scratch = run_pipeline.mysql_scratch_root(configured=self.env.get("OJS_MYSQL_TMP_ROOT", ""))
        report = build_budget(clean=self.clean, raw=self.raw, scratch=scratch, env=env,
                              needs_download=False, remote_size=None,
                              reserve_gib=self.args.reserve_gb, minimum_gib=self.args.min_free_gb)
        reuse = successor_export_reuse_metadata(self.clean, state)
        export_budget_valid = not any(code.startswith("build_workspace_gb_") for code in report["blockers"])
        export_working_bytes = (math.ceil(float(env["OJS_BUILD_WORKSPACE_GB"]) * GIB)
                                if export_budget_valid else 0)
        for group in report["filesystems"]:
            if "data" not in group["roles"]:
                continue
            if reuse is not None and export_budget_valid:
                # Metadata supplies conditional admission only. The runner
                # authenticates every gzip byte before waiving this allocation.
                group["conditional_export_reuse_bytes"] = export_working_bytes
                group["working_bytes"] -= export_working_bytes
                group["required_free_bytes"] = max(
                    math.ceil(self.args.min_free_gb * GIB),
                    group["working_bytes"] + group["download_bytes"] + group["reserve_bytes"])
            group["conditional_retirement_bytes"] = credit
            group["available_after_retirement_bytes"] = group["available_bytes"] + credit
            group["enough_space"] = group["available_after_retirement_bytes"] >= group["required_free_bytes"]
            blocker = "insufficient_" + "_".join(group["roles"]) + "_space"
            if group["enough_space"] and blocker in report["blockers"]:
                report["blockers"].remove(blocker)
        report["blockers"].extend(configuration_blockers)
        report["approved"] = not report["blockers"]
        report["resume_phase"] = "export_ready"
        report["conditional_successor_export_reuse"] = reuse is not None
        report["note"] = ("Retirement and successor reuse credits are conditional; the runner revalidates "
                          "the database, fully hashes any reused export, and checks actual free space before exporting.")
        return report

    def incomplete_builds(self) -> None:
        for staging in self.clean.glob("mysql-????-??-??.building"):
            safe_child(self.clean, staging.name)
            state = run_pipeline.read_build_state(staging)
            if state is not None and state.get("phase") == "export_ready":
                live = live_directory(self.clean)
                if live is None:
                    raise UpdateError("export recovery has no verified rollback catalogue")
                validate_resume_handoff(self.clean, staging.name[6:-9], live.name[6:])
                continue
            if state is None or state.get("phase") != "metadata_ready":
                raise UpdateError("incomplete build requires manual recovery; candidate retained; no download started")

    def export_resume(self) -> str | None:
        """Select one validated export checkpoint before any upstream download."""
        candidates = []
        for staging in self.clean.glob("mysql-????-??-??*"):
            match = re.fullmatch(rf"mysql-({DATE})(\.building)?", staging.name)
            if match is None:
                continue
            safe_child(self.clean, staging.name)
            if (run_pipeline.read_build_state(staging) or {}).get("phase") != "export_ready":
                continue
            version = match.group(1)
            if not match.group(2) and run_pipeline.release_artifacts_complete(self.clean, version):
                continue
            candidates.append(version)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise UpdateError("multiple export checkpoints require operator review; nothing downloaded")
        live = live_directory(self.clean)
        if live is None:
            raise UpdateError("export recovery has no verified rollback catalogue")
        validate_resume_handoff(self.clean, candidates[0], live.name[6:])
        return candidates[0]

    def completed_handoff_version(self) -> str | None:
        """Identify a committed, receipt-bound successor without reading its gzip.

        A crash can leave either the durable checkpoint or only the completed
        release before mysql-current/publication have caught up. The receipt
        permits only the exact successor of the current live predecessor.
        Full export and cold database checks still precede promotion.
        """
        live = live_directory(self.clean)
        if live is None:
            return None
        predecessor = live.name[6:]
        receipt_path = retirement_receipt_path(self.clean, predecessor)
        safe_child(self.clean, receipt_path.name)
        if not receipt_path.exists():
            return None
        receipt = validate_retired_baseline(self.clean, predecessor)
        version = receipt["successor"]["snapshot_date"]
        candidate = safe_child(self.clean, f"mysql-{version}")
        if not candidate.is_dir() or not run_pipeline.release_artifacts_complete(self.clean, version):
            return None
        newer = []
        for path in self.clean.glob("mysql-????-??-??*"):
            match = re.fullmatch(rf"mysql-({DATE})(\.building)?", path.name)
            if match is not None and match.group(1) > predecessor:
                safe_child(self.clean, path.name)
                newer.append(path)
        if newer != [candidate]:
            raise UpdateError("completed handoff has competing candidates; publication requires review")
        assert_automatic_baseline(self.clean, candidate)
        report = read_checkpoint_document(self.clean / f"pkpbeacon-changes-{version}.json")
        snapshot = report["snapshot"]
        expected = {"snapshot_date": version,
                    "source_sha256": snapshot["source_sha256"],
                    "build_sql_sha256": snapshot["build_sql_sha256"],
                    "report_sha256": document_sha256(report)}
        if (snapshot.get("previous_snapshot_date") != predecessor
                or receipt["successor"] != expected):
            raise UpdateError("completed handoff is not bound to the live predecessor and published report")
        checkpoint = run_pipeline.build_state_path(candidate)
        if checkpoint.exists() or checkpoint.is_symlink():
            state = validate_resume_handoff(self.clean, version, predecessor)
            if state["export_handoff"]["report"] != report:
                raise UpdateError("completed handoff checkpoint and published report disagree")
        if live_directory(self.clean) != live:
            raise UpdateError("live predecessor changed during completed handoff validation")
        return version

    def build_or_resume(self) -> dict | None:
        ready = self.export_resume()
        if ready is not None:
            # No HEAD/download or metadata/temporal replay. The runner cold-
            # verifies this complete candidate, then enforces its separate
            # export budget after any authorized predecessor retirement.
            matches = [safe_child(self.raw, f"pkpbeacon-{ready}{suffix}")
                       for suffix in (".sql.gz", ".sql")
                       if (self.raw / f"pkpbeacon-{ready}{suffix}").exists()]
            if len(matches) != 1:
                raise UpdateError("export recovery requires exactly one retained source archive")
            self.compatible_mysql_tools()
            self.capacity(self.args.reserve_gb, separate_scratch_reserve=True)
            self.memory_capacity()
            budget = self.export_resume_capacity(ready)
            if not budget["approved"]:
                raise UpdateError("export recovery is not admitted by its export/scratch budgets; candidate retained")
            self.guard_build_directories()
            self.child([sys.executable, str(self.root / "src" / "run_pipeline.py"),
                        "--raw-dir", str(self.raw), "--clean-dir", str(self.clean),
                        "--snapshot", str(matches[0]), "--skip-download",
                        "--resume-building", "--compact-storage"],
                       stage="update-export-resume")
            return None
        probe = self.probe()
        current = live_directory(self.clean)
        known = probe.get("known_snapshot")
        known_name = Path(known).name if isinstance(known, str) else ""
        known_match = re.fullmatch(rf"pkpbeacon-({DATE})\.sql\.gz", known_name)
        if not probe["needs_download"] and current is not None and known_match and current.name == f"mysql-{known_match.group(1)}":
            metadata = download_beacon.read_validated_metadata(safe_child(self.raw, known_name))
            marker = read_json(current / MARKER)
            if metadata and metadata.get("uncompressed_sha256") == marker.get("source_sha256"):
                return {"status": "unchanged", "release": known_match.group(1)}
        self.compatible_mysql_tools()
        self.capacity(self.args.min_free_gb)
        self.memory_capacity()
        assert_automatic_baseline(self.clean, current)
        self.require_build_capacity(probe)
        self.guard_build_directories()
        if probe["needs_download"]:
            self.child(self.download_command(), stage="update-download")
        # Downloads may take hours; repeat the baseline and capacity checks.
        assert_automatic_baseline(self.clean, current)
        self.require_build_capacity(probe, downloaded=True)
        self.guard_build_directories()
        self.child([sys.executable, str(self.root / "src" / "run_pipeline.py"),
                    "--raw-dir", str(self.raw), "--clean-dir", str(self.clean),
                    "--skip-download", "--resume-building", "--compact-storage"],
                   stage="update-build")
        return None

    def scheduler_status(self) -> dict:
        """Small allowlisted systemd observations; never read unit environments."""
        status = {"cron_installed": Path("/etc/cron.d/ojs-api").is_file(),
                  "cron_matches_template": False, "cron_active": None,
                  "timezone_utc": None,
                  "service_loaded": None, "manual_start_refused": None,
                  "service_active": None, "timer_enabled": None}
        descriptor = None
        try:
            descriptor = os.open("/etc/cron.d/ojs-api", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and info.st_size <= 16384:
                with os.fdopen(descriptor, "rb") as source:
                    descriptor = None
                    actual = source.read(16385)
                with (self.root / "deploy/ojs-api.cron").open("rb") as source:
                    expected = source.read(16385)
                status["cron_matches_template"] = bool(expected) and len(expected) <= 16384 and actual == expected
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            output = self.command(["systemctl", "show", "ojs-api-update.service",
                                   "-p", "LoadState", "-p", "RefuseManualStart",
                                   "-p", "ActiveState"], timeout=5)
            fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            status["service_loaded"] = fields.get("LoadState") == "loaded"
            status["manual_start_refused"] = (
                fields["RefuseManualStart"] == "yes" if fields.get("RefuseManualStart") in ("yes", "no") else None)
            state = fields.get("ActiveState")
            if state in ("active", "activating", "deactivating", "reloading", "refreshing", "maintenance"):
                status["service_active"] = True
            elif state in ("inactive", "failed"):
                status["service_active"] = False
            timer = self.command(["systemctl", "show", "ojs-api-update.timer",
                                  "-p", "ActiveState", "-p", "UnitFileState"], timeout=5)
            timer_fields = dict(line.split("=", 1) for line in timer.splitlines() if "=" in line)
            timer_active = timer_fields.get("ActiveState")
            timer_file = timer_fields.get("UnitFileState")
            if (timer_active in ("active", "activating", "deactivating", "reloading", "refreshing", "maintenance") or
                    timer_file in ("enabled", "enabled-runtime", "linked", "linked-runtime")):
                status["timer_enabled"] = True
            elif timer_active in ("inactive", "failed") and timer_file in ("", "disabled", "static", "masked", "masked-runtime"):
                status["timer_enabled"] = False
            cron = self.command(["systemctl", "show", "cron.service", "-p", "ActiveState", "--value"], timeout=5).strip()
            status["cron_active"] = cron == "active" if cron in ("active", "inactive", "failed", "activating", "deactivating") else None
            timezone = self.command(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=5).strip()
            status["timezone_utc"] = timezone in ("UTC", "Etc/UTC") if timezone else None
        except (OSError, RuntimeError, ValueError):
            pass
        return status

    def readiness(self) -> dict:
        """No files, databases, downloads, service starts, mail or paid API calls."""
        blockers = []
        report = {"schema_version": 1, "live_snapshot": None, "baseline_snapshot": None,
                  "updates_paused": (self.root / "data" / PAUSE_MARKER).exists() or
                                    (self.root / "data" / PAUSE_MARKER).is_symlink(),
                  "recovery_pending": self.journal.exists() or self.journal.is_symlink()}
        live = None
        ready = None
        publication_ready = None
        try:
            live = live_directory(self.clean)
            report["live_snapshot"] = live.name.removeprefix("mysql-") if live else None
            publication_ready = self.completed_handoff_version()
            ready = None if publication_ready else self.export_resume()
            if publication_ready is not None:
                report["baseline_snapshot"] = report["live_snapshot"]
                report["resume_phase"] = "publication_ready"
            elif ready is not None:
                report["baseline_snapshot"] = report["live_snapshot"]
                report["resume_phase"] = "export_ready"
            else:
                report["baseline_snapshot"] = assert_automatic_baseline(self.clean, live)
        except (OSError, RuntimeError, ValueError):
            blockers.append("history_baseline_unavailable")
        if report["recovery_pending"]:
            blockers.append("publication_recovery_pending")
        try:
            self.incomplete_builds()
        except (OSError, RuntimeError, ValueError):
            blockers.append("incomplete_build_requires_review")
        for code, path in (
            ("source_activity", self.raw / ".source-activity.lock"),
            ("automatic_update", self.clean / ".automatic-update.lock"),
            ("pipeline", self.clean / ".pipeline.lock"),
            ("publication", self.clean / ".publish-live.lock"),
        ):
            try:
                if existing_lock_busy(path):
                    blockers.append(code + "_busy")
            except OSError:
                blockers.append(code + "_unavailable")
        for code, check in (("mysql_tools_incompatible", self.compatible_mysql_tools),
                            ("api_credentials_unavailable", self.credentials),
                            ("host_memory_unavailable", self.memory_capacity)):
            try:
                check()
            except (OSError, RuntimeError, ValueError):
                blockers.append(code)
        if not self.env.get("MYSQL_ROOT_PASSWORD"):
            blockers.append("database_credentials_unset")
        try:
            if publication_ready is not None:
                self.capacity(self.args.reserve_gb, separate_scratch_reserve=True)
                report["source"] = {"needs_download": False, "date_hint": publication_ready,
                                    "reason": "publish_completed_export_handoff"}
                report["capacity"] = {"approved": True, "blockers": [],
                                      "resume_phase": "publication_ready",
                                      "reserve_bytes": math.ceil(self.args.reserve_gb * GIB)}
            elif ready is not None:
                report["source"] = {"needs_download": False, "date_hint": ready,
                                    "reason": "resume_retained_export_checkpoint"}
                report["capacity"] = self.export_resume_capacity(ready)
            else:
                probe = self.probe()
                report["source"] = {"needs_download": probe["needs_download"],
                                    "date_hint": probe.get("date_hint"),
                                    "compressed_bytes": probe.get("remote", {}).get("size")}
                report["capacity"] = self.build_capacity(probe)
            blockers.extend(report["capacity"]["blockers"])
        except (OSError, RuntimeError, ValueError, TypeError):
            blockers.append("source_or_capacity_check_unavailable")
        report["build_blockers"] = list(dict.fromkeys(blockers))
        report["ready_for_build"] = not blockers
        scheduler = self.scheduler_status()
        report["scheduler"] = scheduler
        activation = []
        if report["updates_paused"]:
            activation.append("updates_paused")
        if scheduler["service_loaded"] is not True:
            activation.append("service_not_confirmed_loaded")
        if scheduler["manual_start_refused"] is not False:
            activation.append("manual_start_not_permitted")
        if scheduler["service_active"] is not False:
            activation.append("service_not_confirmed_idle")
        if scheduler["timer_enabled"] is not False:
            activation.append("alternate_scheduler_requires_review")
        if not scheduler["cron_installed"]:
            activation.append("daily_cron_not_installed")
        elif scheduler.get("cron_matches_template") is not True:
            activation.append("daily_cron_requires_review")
        if scheduler.get("cron_active") is not True:
            activation.append("cron_daemon_not_confirmed_active")
        if scheduler.get("timezone_utc") is not True:
            activation.append("utc_schedule_not_confirmed")
        report["activation_blockers"] = activation
        report["automatic_updates_allowed"] = not blockers and not activation
        report["status"] = "blocked" if blockers else "prepared" if activation else "ready"
        report["note"] = "Point-in-time admission checks only; not a measured peak, API health test or backup verification."
        return report

    def child(self, command, *, stage: str = "update-child") -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateError("update exceeded its configured runtime")
        # The child reports data progress. Parent heartbeats only show that
        # supervision continues; reserve checks must not reset data-idle time.
        with Progress(stage, disk_path=self.clean, step="supervise-child"):
            self.runner.child(command, cwd=self.root,
                              **source_activity_child_options(self.raw, self.env),
                              reserve_check=lambda: self.capacity(self.args.reserve_gb, separate_scratch_reserve=True),
                              timeout=remaining)

    def download_command(self) -> list[str]:
        credentials = self.args.credentials_file
        if not credentials.is_absolute():
            credentials = self.root / credentials
        return [sys.executable, str(self.root / "src" / "download_beacon.py"),
                "--raw-dir", str(self.raw), "--url", self.args.url,
                "--credentials-file", str(credentials)]

    def probe(self) -> dict:
        with Progress("update-source-check", disk_path=self.root):
            output = self.command(self.download_command() + ["--check", "--json"])
            try:
                probe = json.loads(output)
                if not isinstance(probe, dict) or type(probe.get("needs_download")) is not bool:
                    raise ValueError
                remote = probe.get("remote", {})
                if not isinstance(remote, dict):
                    raise ValueError
                size = remote.get("size")
                if size is not None and (type(size) is not int or not 0 < size < 2**63):
                    raise ValueError
                date_hint = probe.get("date_hint")
                if date_hint is not None:
                    if not isinstance(date_hint, str):
                        raise ValueError
                    snapshot_date(date_hint)
                return probe
            except (ValueError, TypeError, AttributeError, argparse.ArgumentTypeError) as exc:
                raise UpdateError("downloader returned invalid check JSON") from exc

    def compose(self, datadir: Path, *arguments: str) -> str:
        self.compatible_mysql_tools()
        env = dict(self.env, OJS_LIVE_DATA_DIR=str(datadir.resolve(strict=True)))
        return self.command(["docker", "compose", "--project-directory", str(self.root),
                             *arguments], env=env, timeout=max(180, self.args.health_timeout))

    def compatible_mysql_tools(self) -> run_pipeline.MySQLTools:
        """Reject physical-datadir handover between different MySQL patches.

        This is deliberately lazy: unchanged HEAD checks need no MySQL tools.
        A coordinator invocation uses one fixed environment and tool version.
        """
        if self._compatible_tools is not None:
            return self._compatible_tools
        image = self.env.get("OJS_MYSQL_IMAGE", "mysql:8.4.0")
        match = re.fullmatch(r"[^\s@]+:(8\.4\.\d+)", image)
        if match is None:
            raise UpdateError("OJS_MYSQL_IMAGE must use an exact numeric MySQL 8.4 patch tag")
        tools = run_pipeline.MySQLTools.discover()
        if tools.version != match.group(1):
            raise UpdateError(f"host MySQL {tools.version} does not match serving image patch "
                              f"{match.group(1)}; update both together before publication")
        self._compatible_tools = tools
        return tools

    def protected_paths(self) -> list[Path]:
        identifiers = self.command(["docker", "ps", "--all", "--quiet"]).split()
        protected = []
        if identifiers:
            # All containers, including stopped containers and other projects.
            payload = json.loads(self.command(["docker", "inspect", *identifiers]))
            for container in payload:
                protected.extend(Path(mount["Source"]).resolve()
                                 for mount in container.get("Mounts", []) if mount.get("Source"))
        for process in Path("/proc").glob("[0-9]*"):
            try:
                if process.joinpath("comm").read_text().strip() != "mysqld":
                    continue
                arguments = process.joinpath("cmdline").read_bytes().decode().split("\0")
                for index, value in enumerate(arguments):
                    if value.startswith("--datadir="):
                        protected.append(Path(value.split("=", 1)[1]).resolve())
                    elif value == "--datadir" and index + 1 < len(arguments):
                        protected.append(Path(arguments[index + 1]).resolve())
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise UpdateError("cannot inspect running database processes safely") from exc
        return protected

    def require_unmounted(self, path: Path) -> None:
        canonical = path.resolve()
        for protected in self.protected_paths():
            if canonical == protected or canonical in protected.parents or protected in canonical.parents:
                raise UpdateError(f"artifact is mounted or used by MySQL: {path}")

    def guard_build_directories(self) -> None:
        live = live_directory(self.clean)
        for path in self.clean.glob("mysql-????-??-??*"):
            if not re.fullmatch(rf"mysql-{DATE}(?:\.building)?", path.name):
                continue
            safe_child(self.clean, path.name)
            if path != live:
                self.require_unmounted(path)

    def verify_database(self, release: Release) -> None:
        tools = self.compatible_mysql_tools()
        self.require_unmounted(release.datadir)
        password = self.env.get("MYSQL_ROOT_PASSWORD")
        if not password:
            raise UpdateError("MYSQL_ROOT_PASSWORD must be set")
        server = run_pipeline.MySQLServer(tools, release.datadir,
                                         self.env.get("OJS_MYSQL_BUFFER_POOL_SIZE", "768M"))
        database = self.env.get("OJS_DB_NAME", "pkpbeacon_db")
        try:
            server.start(password)
            counts, _ = run_pipeline.validate_database(server, database, password, require_raw=False)
            tables = server.execute("SELECT table_name FROM information_schema.tables "
                                    "WHERE table_schema = DATABASE() ORDER BY table_name;",
                                    database=database, password=password).splitlines()
            if set(tables) != set(run_pipeline.CLEAN_EXPORT_TABLES) | {"ojs_pipeline_metadata"}:
                raise UpdateError("candidate database is not clean-only")
            provenance = server.execute("SELECT DATE_FORMAT(snapshot_date, '%Y-%m-%d'), "
                                        "source_sha256, build_sql_sha256 FROM ojs_snapshots "
                                        "ORDER BY snapshot_date DESC LIMIT 1;",
                                        database=database, password=password).split("\t")
            if provenance != [release.date, release.source_sha256, release.build_sql_sha256]:
                raise UpdateError("candidate database provenance does not match release")
            report = release.report
            expected_counts = (report["source_rows"]["retained_total"], report["source_rows"]["active"],
                               report["articles"]["retained_total"], report["articles"]["active"],
                               report["articles"]["removed_tombstones"], report["articles"]["merged_tombstones"],
                               report["article_events"]["total"])
            if tuple(vars(counts).values()) != expected_counts:
                raise UpdateError("candidate database counts do not match release report")
        finally:
            server.shutdown(password)

    def credentials(self) -> tuple[str, str]:
        path = Path(self.env.get("OJS_API_CREDENTIALS_HOST_FILE", ".secrets/api-client.env"))
        if not path.is_absolute():
            path = self.root / path
        if path.stat().st_mode & 0o077:
            raise UpdateError("API credentials file permissions must be private")
        values = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, separator, value = line.partition("=")
                if not separator or key not in ("OJS_API_USERNAME", "OJS_API_KEY"):
                    raise UpdateError("invalid API credentials file")
                values[key] = value.strip()
        if not values.get("OJS_API_USERNAME") or not values.get("OJS_API_KEY"):
            raise UpdateError("API credentials file is incomplete")
        return values["OJS_API_USERNAME"], values["OJS_API_KEY"]

    def check_health(self, release: Release | None, datadir: Path) -> None:
        with Progress("update-api-health", disk_path=self.clean):
            self._check_health(release, datadir)

    def _check_health(self, release: Release | None, datadir: Path) -> None:
        username, password = self.credentials()
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        base = self.args.api_url.rstrip("/")
        parsed = urlsplit(base)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UpdateError("API check URL must not contain credentials or query parameters")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in
                                              ("127.0.0.1", "localhost", "::1")):
            raise UpdateError("API credentials require HTTPS or a loopback URL")
        deadline = time.monotonic() + self.args.health_timeout
        while True:
            try:
                ids = self.compose(datadir, "ps", "--quiet", "mysql", "ojs-api").split()
                if len(ids) != 2:
                    raise UpdateError("Compose services are not running")
                states = json.loads(self.command(["docker", "inspect", *ids]))
                if any(item.get("State", {}).get("Health", {}).get("Status") != "healthy" for item in states):
                    raise UpdateError("Compose services are not healthy")
                mysql = next((item for item in states if item.get("Config", {}).get("Labels", {}).get(
                    "com.docker.compose.service") == "mysql"), None)
                if mysql is None or not any(mount.get("Destination") == "/var/lib/mysql" and
                                            Path(mount.get("Source", "")).resolve() == datadir
                                            for mount in mysql.get("Mounts", [])):
                    raise UpdateError("serving MySQL mount does not match selected database")
                opener = build_opener(NoRedirect())
                for endpoint in ("/health", "/meta"):
                    request = Request(base + endpoint, headers={"Authorization": "Basic " + token})
                    with opener.open(request, timeout=5) as response:
                        payload = json.load(response)
                if release is not None:
                    snapshot = payload.get("latest_snapshot", {})
                    expected = {"snapshot_date": release.date, "source_sha256": release.source_sha256,
                                "build_sql_sha256": release.build_sql_sha256}
                    if any(snapshot.get(key) != value for key, value in expected.items()):
                        raise UpdateError("authenticated API release provenance does not match candidate")
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise UpdateError("API release verification timed out; no old artifacts were deleted") from None
                time.sleep(min(3, max(0, deadline - time.monotonic())))

    def rollback(self, state: dict) -> None:
        with Progress("update-rollback", disk_path=self.clean):
            self._rollback(state)

    def _rollback(self, state: dict) -> None:
        candidate = safe_child(self.clean, state["candidate"])
        previous = safe_child(self.clean, state["previous"]) if state.get("previous") else None
        self.compose(candidate, "stop", "ojs-api", "mysql")
        set_live(self.clean, previous)
        if previous is not None:
            self.compose(previous, "up", "--detach", "--force-recreate", "mysql", "ojs-api")
            self.check_health(None, previous)
        else:
            # Remove stopped first-install containers, whose bind mounts would
            # otherwise prevent a safe cold verification on the next attempt.
            self.compose(candidate, "down")
        self.journal.unlink()
        sync_directory(self.clean)

    def recover(self) -> None:
        if not self.journal.exists():
            return
        state = read_json(self.journal)
        if state.get("schema_version") != 1 or not DATADIR_RE.fullmatch(state.get("candidate", "")):
            raise UpdateError("unrecognized update recovery journal")
        if state.get("previous") is not None and not DATADIR_RE.fullmatch(state["previous"]):
            raise UpdateError("invalid previous database in recovery journal")
        self.compatible_mysql_tools()
        if state.get("phase") == "verified":
            release = validate_release(self.clean, state["snapshot_date"])
            if live_directory(self.clean) != release.datadir:
                raise UpdateError("verified journal and live pointer disagree")
            self.check_health(release, release.datadir)
            self.finish(state, release)
        elif state.get("phase") in ("prepared", "switching"):
            print("[recover] restoring the previous live database before continuing")
            self.rollback(state)
        else:
            raise UpdateError("unknown update recovery phase")

    def promote(self, release: Release) -> None:
        with Progress("update-promotion", disk_path=self.clean):
            self._promote(release)

    def _promote(self, release: Release) -> None:
        self.compatible_mysql_tools()
        previous = live_directory(self.clean)
        if previous == release.datadir:
            self.compose(release.datadir, "up", "--detach", "mysql", "ojs-api")
            self.check_health(release, release.datadir)
            self.finish({"schema_version": 1, "phase": "verified", "candidate": release.datadir.name,
                         "previous": None, "snapshot_date": release.date}, release)
            return
        if previous and previous.name > release.datadir.name:
            raise UpdateError("refusing to replace live database with an older release")
        self.capacity(self.args.reserve_gb, separate_scratch_reserve=True)
        self.credentials()  # Fail before stopping services if the health check cannot authenticate.
        with Progress("update-cold-verification", disk_path=self.clean):
            self.verify_database(release)
        state = {"schema_version": 1, "phase": "prepared", "candidate": release.datadir.name,
                 "previous": previous.name if previous else None, "snapshot_date": release.date,
                 "started_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(self.journal, state)
        try:
            self.compose(previous or release.datadir, "stop", "ojs-api", "mysql")
            state["phase"] = "switching"
            atomic_json(self.journal, state)
            set_live(self.clean, release.datadir)
            self.compose(release.datadir, "up", "--detach", "--force-recreate", "mysql", "ojs-api")
            self.check_health(release, release.datadir)
        except BaseException:
            self.rollback(state)
            raise
        state["phase"] = "verified"
        atomic_json(self.journal, state)
        self.finish(state, release)

    def cleanup_plan(self, current: Release) -> list[dict]:
        plan = []
        for manifest in sorted(self.clean.glob("pkpbeacon-release-????-??-??.json")):
            date = manifest.name.removeprefix("pkpbeacon-release-").removesuffix(".json")
            if date >= current.date:
                continue
            try:
                old = validate_release(self.clean, date, require_directory=False,
                                       retired_successor=current.date)
                if not old.export.exists():
                    receipt = validate_retired_baseline(self.clean, date, current.date)
                    expected_successor = {"snapshot_date": current.date,
                                          "source_sha256": current.source_sha256,
                                          "build_sql_sha256": current.build_sql_sha256,
                                          "report_sha256": document_sha256(current.report)}
                    if receipt["successor"] != expected_successor:
                        raise UpdateError("retirement receipt is not bound to the published successor")
                paths = []
                if old.datadir.exists():
                    validate_release(self.clean, date, retired_successor=current.date)
                    paths.append(old.datadir)
                raw = safe_child(self.raw, f"pkpbeacon-{date}.sql.gz")
                if raw.exists():
                    metadata = download_beacon.read_validated_metadata(raw)
                    if metadata and metadata.get("source_url") == self.args.url and metadata.get("uncompressed_sha256") == old.source_sha256:
                        paths.extend((raw, safe_child(self.raw, raw.name + ".metadata.json")))
                # Early retirement removes only the old gzip; its checksum,
                # report, manifest and receipt authenticate normal later cleanup.
                if old.export.exists():
                    paths.append(old.export)
                paths.append(old.export.with_name(old.export.name + ".sha256"))
                for path in paths:
                    self.require_unmounted(path)
                    assert_cleanup_unmounted(path)
                    if path.is_dir():
                        for base, directories, files in os.walk(path, followlinks=False):
                            if any((Path(base) / name).is_symlink() for name in directories + files):
                                raise UpdateError(f"refusing cleanup of a tree containing symlinks: {path}")
                    info = path.stat(follow_symlinks=False)
                    size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.is_dir() else info.st_size
                    plan.append({"parent": "raw" if path.parent == self.raw else "clean", "name": path.name,
                                 "device": info.st_dev, "inode": info.st_ino, "bytes": size,
                                 "directory": path.is_dir(), "deleted": False})
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"[retain] {date}: artifacts not eligible for automatic cleanup ({type(exc).__name__})")
        return plan

    def finish(self, state: dict, release: Release) -> None:
        with Progress("update-cleanup", disk_path=self.clean,
                      step="plan-cleanup", deleted_artifacts=0, deleted_bytes=0) as progress:
            self._finish(state, release, progress)

    def _finish(self, state: dict, release: Release, progress: Progress) -> None:
        if self.args.no_prune or self.args.publish_release:
            progress.update(step="cleanup-disabled")
            self.journal.unlink(missing_ok=True)
            sync_directory(self.clean)
            return
        if "cleanup" not in state:
            state["cleanup"] = self.cleanup_plan(release)
            atomic_json(self.journal, state)
        progress.update(step="remove-owned-artifacts", total_artifacts=len(state["cleanup"]))
        deleted_artifacts = 0
        deleted_bytes = 0
        for entry in state["cleanup"]:
            directory = self.raw if entry["parent"] == "raw" else self.clean
            path = safe_child(directory, entry["name"])
            # The journal is a deletion authorization only for precisely named
            # generated artifacts validated before the first deletion.
            allowed = (re.fullmatch(rf"pkpbeacon-{DATE}\.sql\.gz(?:\.metadata\.json)?", path.name) if directory == self.raw else
                       DATADIR_RE.fullmatch(path.name) or re.fullmatch(rf"pkpbeacon-clean-{DATE}\.sql\.gz(?:\.sha256)?", path.name))
            if not allowed or path == release.datadir or path == release.export:
                raise UpdateError("unsafe artifact in cleanup journal")
            artifact_date = re.search(DATE, path.name).group(0)
            if artifact_date >= release.date:
                raise UpdateError("cleanup journal contains a current or future artifact")
            if path.exists():
                info = path.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (entry["device"], entry["inode"]):
                    raise UpdateError(f"cleanup target changed since validation: {path}")
                self.require_unmounted(path)
                assert_cleanup_unmounted(path)
                if entry["directory"]:
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise UpdateError("safe directory cleanup is unavailable on this platform")
                    shutil.rmtree(path)
                else:
                    path.unlink()
            entry["deleted"] = True
            atomic_json(self.journal, state)
            deleted_artifacts += 1
            deleted_bytes += entry["bytes"]
            progress.update(deleted_artifacts=deleted_artifacts, deleted_bytes=deleted_bytes)
        audit_path = self.clean / f"pkpbeacon-cleanup-{release.date}.json"
        prior_entries = read_json(audit_path).get("artifacts", []) if audit_path.exists() else []
        entries = {(entry["parent"], entry["name"], entry["device"], entry["inode"]): entry
                   for entry in prior_entries + state["cleanup"]}
        audit = {"schema_version": 1, "release": release.date,
                 "completed_at": datetime.now(timezone.utc).isoformat(),
                 "deleted_count": sum(entry["deleted"] for entry in entries.values()),
                 "deleted_bytes": sum(entry["bytes"] for entry in entries.values() if entry["deleted"]),
                 "artifacts": list(entries.values())}
        atomic_json(audit_path, audit)
        self.journal.unlink(missing_ok=True)
        sync_directory(self.clean)
        print(f"[cleanup] removed {audit['deleted_count']} generated artifacts ({audit['deleted_bytes']} bytes)")

    def execute(self) -> dict:
        if self.args.readiness:
            return self.readiness()
        if self.args.check:
            return {"probe": self.probe(), "live": str(live_directory(self.clean)) if self.clean.exists() and live_directory(self.clean) else None,
                    "recovery_pending": self.journal.exists()}
        # Keep source exclusion through download, wrangling and publication.
        # Only the intended downloader/pipeline children inherit this lease.
        with source_activity(self.raw), automatic_lock(self.clean):
            paused = self.root / "data" / PAUSE_MARKER
            if self.args.recover_exact_keys:
                # Explicit, supervised one-snapshot recovery; never probe,
                # download, promote, prune sources, or start later snapshots.
                if not paused.is_file() or paused.is_symlink():
                    raise UpdateError("create a regular data/.updates-paused marker before explicit recovery")
                if self.args.publish_only or self.journal.exists():
                    raise UpdateError("exact-key recovery cannot be combined with publication/release recovery")
                version = self.args.recover_exact_keys
                matches = [safe_child(self.raw, f"pkpbeacon-{version}{suffix}")
                           for suffix in (".sql.gz", ".sql")
                           if (self.raw / f"pkpbeacon-{version}{suffix}").exists()]
                if len(matches) != 1:
                    raise UpdateError("exact-key recovery requires exactly one raw archive for the requested date")
                self.compatible_mysql_tools()
                self.capacity(self.args.min_free_gb)
                self.memory_capacity()
                self.guard_build_directories()
                self.child([sys.executable, str(self.root / "src" / "run_pipeline.py"),
                            "--raw-dir", str(self.raw), "--clean-dir", str(self.clean),
                            "--snapshot", str(matches[0]), "--skip-download",
                            "--resume-building", "--compact-storage", "--recover-exact-keys"],
                           stage="update-recover-exact-keys")
                return {"status": "recovered", "release": version, "published": False,
                        "updates_paused": True}
            if self.args.publish_release:
                # A reviewed dated release may go live while automatic work
                # remains paused. Do not select a different current candidate,
                # start a build/download, or prune any retained artifacts.
                with run_pipeline.pipeline_lock(self.clean), publish_live.publisher_lock(self.root):
                    self.recover()
                    release = validate_release(self.clean, self.args.publish_release)
                    self.promote(release)
                    return {"status": "published", "release": release.date, "published": True,
                            "updates_paused": paused.exists() or paused.is_symlink()}
            if paused.exists() or paused.is_symlink():
                return {"status": "paused", "reason": "operator_pause", "marker": str(paused)}
            with run_pipeline.pipeline_lock(self.clean), publish_live.publisher_lock(self.root):
                self.recover()
                if not self.args.publish_only:
                    committed = self.completed_handoff_version()
                    if committed is not None:
                        release = validate_release(self.clean, committed)
                        run_pipeline.repair_current_release_pointers(self.clean, committed)
                        self.promote(release)
                        return {"status": "published", "release": release.date}
            if not self.args.publish_only:
                # Refuse a failed finalization BEFORE even a new download. Only
                # an explicit, audited recovery or rebuild may dispose of work.
                self.incomplete_builds()
                outcome = self.build_or_resume()
                if outcome is not None:
                    return outcome
                # Builds can last days; recheck after possible host package changes.
                self._compatible_tools = None
            with run_pipeline.pipeline_lock(self.clean), publish_live.publisher_lock(self.root):
                candidate = live_directory(self.clean, "mysql-current")
                if candidate is None:
                    raise UpdateError("no completed pipeline candidate is available")
                release = validate_release(self.clean, DATADIR_RE.fullmatch(candidate.name).group(1))
                self.promote(release)
                return {"status": "published", "release": release.date}


def snapshot_date(value: str) -> str:
    try:
        if not re.fullmatch(DATE, value):
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a valid YYYY-MM-DD date") from exc
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-root", type=Path, default=ROOT)
    result.add_argument("--url", default=os.getenv("OJS_BEACON_URL", download_beacon.DEFAULT_URL))
    result.add_argument("--credentials-file", type=Path, default=Path(os.getenv("OJS_BEACON_CREDENTIALS_FILE", ".secrets/beacon.ini")))
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--readiness", action="store_true", help="read-only automatic-build admission report; never download, publish or unpause")
    modes.add_argument("--check", action="store_true", help="read-only HEAD and local status; no locks or writes")
    modes.add_argument("--publish-only", action="store_true", help="promote the completed pipeline candidate without downloading or building")
    modes.add_argument("--publish-release", type=snapshot_date, metavar="YYYY-MM-DD",
                       help="promote this complete dated release even while updates are paused; no download, build or pruning")
    modes.add_argument("--recover-exact-keys", type=snapshot_date, metavar="YYYY-MM-DD", help="explicit paused bootstrap recovery only; no download, publication or pruning")
    result.add_argument("--no-prune", action="store_true", help="keep prior generated releases after successful promotion")
    result.add_argument("--min-free-gb", type=float, default=float(os.getenv("OJS_MIN_FREE_GB", "20")))
    result.add_argument("--reserve-gb", type=float, default=float(os.getenv("OJS_DISK_RESERVE_GB", "8")))
    result.add_argument("--max-runtime-hours", type=float, default=float(os.getenv("OJS_MAX_RUNTIME_HOURS", "720")))
    result.add_argument("--health-timeout", type=float, default=300)
    result.add_argument("--api-url", default=f"http://127.0.0.1:{os.getenv('OJS_API_PORT', '8000')}")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    limits = (args.min_free_gb, args.reserve_gb, args.max_runtime_hours, args.health_timeout)
    if not all(math.isfinite(value) for value in limits) or min(args.min_free_gb, args.reserve_gb) < 0 or args.max_runtime_hours <= 0 or args.health_timeout <= 0:
        print("error: invalid capacity or timeout setting", file=sys.stderr)
        return 2
    def interrupt(signum, frame):
        raise UpdateError("update interrupted; recovery state retained")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        progress_interval()
        outcome = Coordinator(args).execute()
        print(json.dumps(outcome, sort_keys=True))
        return 3 if args.readiness and not outcome["ready_for_build"] else 0
    except SourceActivityBusy:
        print(json.dumps({"status": "skipped", "reason": "source_activity_busy",
                          "message": "Download or wrangling already in progress; no new download started."}))
        return 0
    except subprocess.TimeoutExpired:
        # Also protect timeouts raised by nested pipeline/control operations.
        print(f"error: {COMMAND_TIMEOUT_MESSAGE}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: update interrupted; recovery state retained", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
