#!/usr/bin/env python3
"""Opt-in, grow-only live tuning of one guarded workstation history invocation.

Only a same-user, private native builder in the named systemd user service may
be tuned. This never starts/stops MySQL, changes a cgroup, persists SQL settings,
opens credentials, or modifies catalogue contents. --watch carries explicitly
requested sizes to subsequent dated builders in the SAME service invocation.
The operator must raise the service's memory limits separately, if necessary.

MySQL 8.4 reports requested buffer size separately from asynchronous resize
progress: https://dev.mysql.com/doc/refman/8.4/en/server-status-variables.html
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time

GIB = 1024 ** 3
HOST_RESERVE = 12 * GIB
CGROUP_RESERVE = 6 * GIB
CGROUP_HIGH_RESERVE = 2 * GIB
DISK_RESERVE = 40 * GIB
NON_POOL_ALLOWANCE = 2 * GIB  # Engineering floor, not a proof of all future SQL allocations.
SYSTEMD_PROPERTIES = ("ActiveState", "SubState", "InvocationID", "ControlGroup", "MainPID")
IDENTITY_SQL = """SELECT @@version, @@datadir, @@socket, @@pid_file,
@@skip_networking, @@log_bin, @@innodb_flush_log_at_trx_commit,
@@innodb_doublewrite, @@innodb_buffer_pool_size,
@@innodb_buffer_pool_chunk_size, @@innodb_buffer_pool_instances,
@@innodb_redo_log_capacity, @@innodb_page_size,
(SELECT VARIABLE_VALUE FROM performance_schema.global_status
 WHERE VARIABLE_NAME='Innodb_buffer_pool_resize_status_code'),
(SELECT VARIABLE_VALUE FROM performance_schema.global_status
 WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_total'),
(SELECT VARIABLE_VALUE FROM performance_schema.global_status
 WHERE VARIABLE_NAME='Innodb_redo_log_capacity_resized');"""


class TuningError(RuntimeError):
    """Static, non-sensitive diagnostics only."""


class Unavailable(TuningError):
    """A private server is not ready, or has already changed authentication."""


class InvocationEnded(TuningError):
    """The explicitly selected service invocation is no longer running."""


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise TuningError("invalid tuning arguments; use --help for syntax")


@dataclass(frozen=True)
class Config:
    unit: str
    invocation: str
    mysql_bin: Path
    clean: Path
    versions: tuple[str, ...]
    pool_bytes: int
    redo_bytes: int
    watch: bool = False
    check_only: bool = False


@dataclass(frozen=True)
class Service:
    active: str
    substate: str
    invocation: str
    cgroup: str
    main_pid: int


@dataclass(frozen=True)
class Candidate:
    pid: int
    start_ticks: int
    datadir: Path
    socket: Path
    pid_file: Path
    cgroup: str
    version: str


@dataclass(frozen=True)
class Database:
    datadir: Path
    socket: Path
    pid_file: Path
    version: str
    skip_networking: int
    log_bin: int
    flush_at_commit: int
    doublewrite: str
    requested_pool: int
    chunk: int
    instances: int
    requested_redo: int
    page_size: int
    resize_code: int
    pool_pages: int
    applied_redo: int

    @property
    def actual_pool(self) -> int:
        return self.page_size * self.pool_pages


def emit(event: str, **values) -> None:
    print("[workstation-tuning] " + json.dumps({"event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **values}, sort_keys=True), flush=True)


def canonical_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.resolve(strict=True) != path or not path.is_dir() or path == Path(path.anchor):
        raise TuningError("configuration requires existing non-symlinked directories")
    return path


def parser() -> argparse.ArgumentParser:
    result = PrivateParser(description=__doc__)
    result.add_argument("--unit", required=True)
    result.add_argument("--invocation-id", required=True)
    result.add_argument("--mysql-bin", type=Path, required=True)
    result.add_argument("--clean-dir", type=Path, required=True)
    result.add_argument("--snapshot", action="append", required=True)
    result.add_argument("--buffer-pool-gib", type=int, required=True)
    result.add_argument("--redo-log-gib", type=int, required=True)
    result.add_argument("--watch", action="store_true", help="poll every 30s until the original invocation ends")
    result.add_argument("--check-only", action="store_true", help="check identity, resource floors and planned sizes without assignments")
    return result


def configuration(args) -> Config:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,180}\.service", args.unit) is None:
        raise TuningError("an explicit systemd service name is required")
    if re.fullmatch(r"[0-9a-f]{32}", args.invocation_id) is None:
        raise TuningError("an explicit 32-character invocation ID is required")
    if not 1 <= args.buffer_pool_gib <= 64 or not 1 <= args.redo_log_gib <= 16:
        raise TuningError("explicit pool/redo sizes must be 1..64 and 1..16 GiB")
    versions = tuple(args.snapshot)
    if (len(versions) != len(set(versions)) or not versions
            or any(date.fromisoformat(value).isoformat() != value for value in versions)):
        raise TuningError("snapshots must be distinct ISO dates")
    tools = canonical_directory(args.mysql_bin)
    clean = canonical_directory(args.clean_dir)
    for name in ("mysql", "mysqld"):
        binary = tools / name
        if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise TuningError("matching native MySQL client and server executables are required")
    return Config(args.unit, args.invocation_id, tools, clean, versions,
                  args.buffer_pool_gib * GIB, args.redo_log_gib * GIB, args.watch, args.check_only)


def cgroup_path(value: str, root: Path) -> Path:
    relative = PurePosixPath(value)
    if not relative.is_absolute() or ".." in relative.parts or value == "/":
        raise TuningError("a non-root systemd cgroup is required")
    path = root.joinpath(*relative.parts[1:])
    if path.resolve(strict=True) != path or not path.is_dir():
        raise TuningError("systemd cgroup path is unavailable or redirected")
    return path


def finite_limit(value: str) -> int | None:
    if value.strip() == "max":
        return None
    result = int(value)
    if result <= 0:
        raise TuningError("invalid cgroup memory limit")
    return result


def check_resource_numbers(*, available: int, free_disk: int,
                          groups: list[tuple[int, int | None, int | None]],
                          pool_growth: int, redo_growth: int, pool_floor: int) -> None:
    if not groups or groups[0][1] is None or groups[0][2] is None:
        raise TuningError("the service requires finite memory.high and memory.max limits")
    # An expanded pool may not yet be resident. Reserve its future commitment,
    # rather than treating today's low RSS as permanently available headroom.
    service_projected = max(groups[0][0], pool_floor + NON_POOL_ALLOWANCE) + pool_growth
    committed_growth = service_projected - groups[0][0]
    if available < HOST_RESERVE + committed_growth:
        raise TuningError("host memory cannot retain the 12 GiB reserve after requested growth")
    if free_disk < DISK_RESERVE + 2 * redo_growth:
        raise TuningError("disk cannot retain the 40 GiB reserve plus redo growth allowance")
    for current, high, maximum in groups:
        if current < 0:
            raise TuningError("invalid cgroup memory measurement")
        # The same new commitment is charged to every ancestor, including lazy
        # allocation of an already-requested but not fully resident pool.
        projected = current + committed_growth
        if maximum is not None and maximum - projected < CGROUP_RESERVE:
            raise TuningError("cgroup memory.max cannot retain 6 GiB headroom after pool commitment")
        if high is not None and high - projected < CGROUP_HIGH_RESERVE:
            raise TuningError("cgroup memory.high cannot retain 2 GiB headroom after pool commitment")


def client_environment() -> dict[str, str]:
    # No inherited root/API/login-file credentials, client host, or TCP target.
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("MYSQL", "MARIADB", "OJS_"))}


class Backend:
    """Bounded read-only discovery and the two explicitly allowed assignments."""
    def __init__(self, config: Config, *, proc: Path = Path("/proc"),
                 cgroups: Path = Path("/sys/fs/cgroup")):
        self.config, self.proc, self.cgroups = config, proc, cgroups
        self.uid = os.getuid()

    def service(self) -> Service:
        result = subprocess.run(["systemctl", "--user", "show", self.config.unit,
            *(item for name in SYSTEMD_PROPERTIES for item in ("-p", name))],
            capture_output=True, text=True, timeout=10)
        if result.returncode or len(result.stdout) > 16384:
            raise TuningError("cannot inspect the selected systemd service")
        data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return Service(data["ActiveState"], data["SubState"], data["InvocationID"],
                       data["ControlGroup"], int(data["MainPID"]))

    def candidates(self, service: Service) -> list[Candidate]:
        group = cgroup_path(service.cgroup, self.cgroups)
        raw = (group / "cgroup.procs").read_text(encoding="ascii")
        if len(raw) > 65536:
            raise TuningError("unexpectedly large service process list")
        candidates = []
        for value in raw.split():
            pid = int(value)
            try:
                candidate = self.candidate(pid, service.cgroup)
            except (FileNotFoundError, ProcessLookupError, Unavailable):
                continue
            if candidate is not None:
                candidates.append(candidate)
        if len(candidates) > 1:
            raise TuningError("multiple eligible MySQL builders; refusing ambiguous tuning")
        return candidates

    def candidate(self, pid: int, group: str) -> Candidate | None:
        process = self.proc / str(pid)
        if process.stat().st_uid != self.uid:
            return None
        if (process / "comm").read_text(encoding="ascii").strip() != "mysqld":
            return None
        if (process / "exe").resolve(strict=True) != self.config.mysql_bin / "mysqld":
            return None
        memberships = (process / "cgroup").read_text(encoding="ascii").splitlines()
        if f"0::{group}" not in memberships:
            return None
        raw = (process / "cmdline").read_bytes()
        if not raw or len(raw) > 65536:
            raise Unavailable("builder process arguments are not ready")
        argv = raw.rstrip(b"\0").decode("utf-8").split("\0")
        options = {}
        allowed = {"no-defaults", "daemonize", "datadir", "socket", "pid-file", "log-error", "tmpdir",
                   "innodb-temp-tablespaces-dir", "innodb-buffer-pool-size", "max-connections",
                   "temptable-max-ram", "tmp-table-size", "max-heap-table-size", "innodb-redo-log-capacity",
                   "innodb-file-per-table", "skip-networking", "mysqlx", "skip-log-bin", "local-infile",
                   "secure-file-priv", "character-set-server", "collation-server"}
        for value in argv[1:]:
            if not value.startswith("--"):
                return None
            key, separator, content = value[2:].partition("=")
            if key not in allowed or key in options:
                return None
            options[key] = content if separator else None
        fixed = {"no-defaults": None, "daemonize": None, "skip-networking": None,
                 "mysqlx": "0", "skip-log-bin": None, "local-infile": "0", "secure-file-priv": "NULL"}
        if any(key not in options or options[key] != value for key, value in fixed.items()):
            return None
        selected = next((version for version in self.config.versions
                        if options.get("datadir") == str(self.config.clean / f"mysql-{version}.building")), None)
        if selected is None:
            return None
        datadir = Path(options["datadir"])
        if datadir.resolve(strict=True) != datadir or datadir.stat().st_uid != self.uid:
            raise TuningError("builder datadir identity or ownership is unsafe")
        socket = Path(options.get("socket") or "")
        pid_file = Path(options.get("pid-file") or "")
        runtime = socket.parent
        if (not socket.is_absolute() or socket.name != "mysql.sock" or pid_file != runtime / "mysql.pid"
                or not runtime.name.startswith("ojs-api-mysql-")
                or runtime.resolve(strict=True) != runtime):
            raise TuningError("builder socket is outside its private runtime directory")
        runtime_stat = runtime.stat()
        if runtime_stat.st_uid != self.uid or runtime_stat.st_mode & 0o077:
            raise TuningError("builder runtime directory is not private to the current user")
        sock_stat, pid_stat = socket.lstat(), pid_file.lstat()
        if (not stat.S_ISSOCK(sock_stat.st_mode) or sock_stat.st_uid != self.uid
                or not stat.S_ISREG(pid_stat.st_mode) or pid_stat.st_uid != self.uid):
            raise TuningError("builder socket or pid file identity is unsafe")
        if pid_file.stat().st_size > 64 or pid_file.read_text(encoding="ascii").strip() != str(pid):
            raise Unavailable("builder pid file is not ready")
        # comm can contain spaces/parentheses; the last close delimiter ends it.
        start_ticks = int((process / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()[19])
        return Candidate(pid, start_ticks, datadir, socket, pid_file, group, selected)

    def mysql(self, candidate: Candidate, sql: str) -> str:
        result = subprocess.run([str(self.config.mysql_bin / "mysql"), "--no-defaults", "--no-login-paths",
            "--protocol=socket", f"--socket={candidate.socket}", "--user=root", "--skip-password",
            "--connect-timeout=5", "--batch", "--raw", "--skip-column-names"],
            input=sql, capture_output=True, text=True, timeout=20, env=client_environment())
        if result.returncode:
            # stderr may contain private values; never return or print it.
            raise Unavailable("private builder SQL is unavailable; no credentials will be tried")
        if len(result.stdout) > 16384:
            raise TuningError("unexpectedly large tuning query result")
        return result.stdout

    def database(self, candidate: Candidate) -> Database:
        values = self.mysql(candidate, IDENTITY_SQL).strip().split("\t")
        if len(values) != 16 or any("\n" in item or "\r" in item for item in values):
            raise TuningError("private builder returned an invalid identity measurement")
        return Database(Path(values[1]), Path(values[2]), Path(values[3]), values[0],
                        int(values[4]), int(values[5]), int(values[6]), values[7],
                        *(int(value) for value in values[8:]))

    def resources(self, candidate: Candidate, *, pool_growth: int, redo_growth: int, pool_floor: int) -> None:
        available = None
        for line in (self.proc / "meminfo").read_text(encoding="ascii").splitlines():
            fields = line.split()
            if fields[:1] == ["MemAvailable:"] and len(fields) == 3 and fields[2] == "kB":
                available = int(fields[1]) * 1024
        if available is None:
            raise TuningError("host memory availability is unknown")
        group = cgroup_path(candidate.cgroup, self.cgroups)
        groups = []
        while True:
            if (group / "memory.max").exists():
                groups.append((int((group / "memory.current").read_text(encoding="ascii")),
                    finite_limit((group / "memory.high").read_text(encoding="ascii")),
                    finite_limit((group / "memory.max").read_text(encoding="ascii"))))
            if group == self.cgroups:
                break
            group = group.parent
        check_resource_numbers(available=available, free_disk=shutil.disk_usage(candidate.datadir).free,
                              groups=groups, pool_growth=pool_growth, redo_growth=redo_growth, pool_floor=pool_floor)

    def assign(self, candidate: Candidate, name: str, value: int) -> None:
        if name not in ("innodb_buffer_pool_size", "innodb_redo_log_capacity") or not isinstance(value, int) or value <= 0:
            raise TuningError("only fixed positive pool and redo assignments are permitted")
        self.mysql(candidate, f"SET GLOBAL {name} = {value};")

    def sleep(self):
        time.sleep(30)


def verify_database(candidate: Candidate, data: Database) -> None:
    if (data.datadir != candidate.datadir or data.socket != candidate.socket or data.pid_file != candidate.pid_file
            or re.fullmatch(r"8\.4\.\d+(?:[-+][A-Za-z0-9_.-]+)?", data.version) is None
            or data.skip_networking != 1 or data.log_bin != 0 or data.flush_at_commit != 1
            or data.doublewrite != "ON"):
        raise TuningError("private builder SQL identity, version, or durability does not match")
    if (any(value <= 0 for value in (data.requested_pool, data.chunk, data.instances,
                                     data.requested_redo, data.page_size, data.pool_pages, data.applied_redo))
            or not 0 <= data.resize_code <= 7 or data.instances > 64
            or data.chunk > 64 * GIB or data.page_size not in (4096, 8192, 16384, 32768, 65536)):
        raise TuningError("private builder size measurements are invalid")
    if data.resize_code == 7:
        raise TuningError("MySQL reports a failed buffer-pool resize; manual inspection required")


class Controller:
    def __init__(self, config: Config, backend: Backend, reporter=emit):
        self.config, self.backend, self.report = config, backend, reporter

    def current_service(self) -> Service:
        service = self.backend.service()
        if (service.invocation != self.config.invocation or service.active not in ("active", "activating")
                or service.main_pid <= 0):
            raise InvocationEnded("the selected invocation is no longer active")
        return service

    def stable(self, candidate: Candidate) -> None:
        service = self.current_service()
        if service.cgroup != candidate.cgroup or self.backend.candidate(candidate.pid, service.cgroup) != candidate:
            raise Unavailable("builder process identity changed; no assignment made")

    def resources(self, candidate: Candidate, data: Database, target_pool: int, target_redo: int) -> None:
        pool_floor = max(data.requested_pool, data.actual_pool)
        self.backend.resources(candidate,
            pool_growth=max(0, target_pool - pool_floor), pool_floor=pool_floor,
            redo_growth=max(0, max(target_redo, data.requested_redo) - min(data.requested_redo, data.applied_redo)))

    def tick(self) -> bool:
        service = self.current_service()
        candidates = self.backend.candidates(service)
        if not candidates:
            self.report("waiting-for-private-builder")
            return False
        candidate = candidates[0]
        data = self.backend.database(candidate)
        verify_database(candidate, data)
        self.stable(candidate)
        alignment = data.chunk * data.instances
        target_pool = ((self.config.pool_bytes + alignment - 1) // alignment) * alignment
        target_redo = self.config.redo_bytes
        # Actual capacity matters while an externally requested resize is pending.
        # Never lower either requested setting, even when it exceeds our target.
        if self.config.check_only:
            self.resources(candidate, data, target_pool, target_redo)
            self.report("check-only-passed", snapshot=candidate.version, pid=candidate.pid,
                        aligned_pool_target_bytes=target_pool, redo_target_bytes=target_redo)
        elif data.resize_code == 0 and data.requested_pool < target_pool:
            self.resources(candidate, data, target_pool, target_redo)
            self.stable(candidate)
            self.backend.assign(candidate, "innodb_buffer_pool_size", target_pool)
            self.report("pool-growth-requested", snapshot=candidate.version, pid=candidate.pid,
                        requested_bytes=target_pool, previous_bytes=data.requested_pool)
        if not self.config.check_only and data.requested_redo < target_redo:
            data = self.backend.database(candidate)
            verify_database(candidate, data)
            if data.requested_redo < target_redo:
                self.resources(candidate, data, target_pool, target_redo)
                self.stable(candidate)
                self.backend.assign(candidate, "innodb_redo_log_capacity", target_redo)
                self.report("redo-growth-requested", snapshot=candidate.version, pid=candidate.pid,
                            requested_bytes=target_redo, previous_bytes=data.requested_redo)
        measured = self.backend.database(candidate)
        verify_database(candidate, measured)
        self.stable(candidate)
        self.report("observed", snapshot=candidate.version, pid=candidate.pid,
            requested_pool_bytes=measured.requested_pool, actual_pool_bytes=measured.actual_pool,
            resize_status_code=measured.resize_code, requested_redo_bytes=measured.requested_redo,
            applied_redo_bytes=measured.applied_redo,
            at_least_target=(measured.resize_code == 0 and measured.requested_pool >= target_pool
                             and measured.actual_pool >= target_pool and measured.applied_redo >= target_redo))
        return True

    def run(self) -> int:
        initial = True
        while True:
            try:
                observed = self.tick()
            except InvocationEnded:
                self.report("invocation-ended-or-changed")
                return 1 if initial else 0
            except Unavailable:
                self.report("private-builder-not-ready-or-no-longer-blank-root")
                observed = False
            if not self.config.watch:
                return 0 if observed else 1
            initial = False
            self.backend.sleep()


@contextmanager
def invocation_lock(config: Config):
    runtime = Path("/run/user") / str(os.getuid())
    info = runtime.stat()
    if runtime.resolve(strict=True) != runtime or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TuningError("private user runtime directory is unavailable")
    digest = hashlib.sha256((config.unit + config.invocation).encode()).hexdigest()[:24]
    descriptor = os.open(runtime / f"ojs-mysql-tuning-{digest}.lock",
                         os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TuningError("tuning lock is not private to the current user")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TuningError("another tuning helper already owns this invocation") from None
        yield
    finally:
        os.close(descriptor)


def main(argv=None) -> int:
    try:
        config = configuration(parser().parse_args(argv))
        with invocation_lock(config):
            return Controller(config, Backend(config)).run()
    except TuningError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError, KeyError, IndexError, UnicodeError, subprocess.SubprocessError, KeyboardInterrupt):
        print("error: workstation tuning could not be safely verified; the build was not stopped", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
