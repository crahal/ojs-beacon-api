#!/usr/bin/env python3
"""Supervise an explicit local pipeline command without opening a database.

Check each declared filesystem, host MemAvailable, and available cgroup v2
memory limits before starting and at most every 15 seconds during the build.
The child pipeline owns its locks and database lifecycle. On a breached limit
or interruption, the existing Runner sends SIGINT to its process group and
waits without a deadline for the pipeline's finally blocks and MySQL rollback.
An outer service must also allow unlimited graceful shutdown (for systemd,
TimeoutStopSec=infinity and SendSIGKILL=no). Runtime limits trigger cancellation;
they do not bound the subsequent shutdown time.

The supervisor never prints child arguments or environment values. Child output
is inherited, so the invoked pipeline remains responsible for its own logging.
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from compact_update import Runner, UpdateError
from progress_logging import Progress, progress_interval

GIB = 1024 ** 3


class GuardError(RuntimeError):
    """Only static messages and measured resource counters belong here."""


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats invalid values, including command arguments.
        raise GuardError("invalid supervisor arguments; use --help for syntax")


def positive_number(value: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise GuardError("resource and time limits must be finite positive numbers") from None
    if not math.isfinite(result) or result <= 0:
        raise GuardError("resource and time limits must be finite positive numbers")
    return result


def scaled_limit(value: str | float, scale: int) -> int:
    result = positive_number(value) * scale
    if not math.isfinite(result):
        raise GuardError("resource and time limits are too large")
    return math.ceil(result)


@dataclass(frozen=True)
class DiskReserve:
    path: Path
    minimum: int


def host_available_memory(path: Path = Path("/proc/meminfo")) -> int:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if fields and fields[0] == "MemAvailable:":
                if len(fields) != 3 or fields[2] != "kB":
                    break
                available = int(fields[1]) * 1024
                if available >= 0:
                    return available
                break
    except (OSError, UnicodeError, ValueError):
        pass
    raise GuardError("host available-memory measurement is unavailable")


def cgroup_memory_directories(
    membership: Path = Path("/proc/self/cgroup"),
    root: Path = Path("/sys/fs/cgroup"),
) -> tuple[Path, ...]:
    """Include finite parent constraints as well as the service's own limit."""
    try:
        for line in membership.read_text(encoding="ascii").splitlines():
            if not line.startswith("0::"):
                continue
            relative = PurePosixPath(line[3:])
            if not relative.is_absolute() or ".." in relative.parts:
                raise GuardError("cgroup membership is invalid")
            directory = root.joinpath(*relative.parts[1:])
            if not directory.is_dir():
                raise GuardError("cgroup memory measurements are unavailable")
            directories = []
            while True:
                if (directory / "memory.max").exists():
                    directories.append(directory)
                if directory == root:
                    return tuple(directories)
                directory = directory.parent
    except (OSError, UnicodeError):
        raise GuardError("cgroup membership measurement is unavailable") from None
    return ()  # cgroup v1 or no memory controller; host guard still applies.


class ResourceGuard:
    def __init__(self, disks: tuple[DiskReserve, ...], minimum_memory: int,
                 minimum_cgroup_headroom: int, *, require_cgroup_limit: bool = False):
        self.disks = disks
        self.minimum_memory = minimum_memory
        self.minimum_cgroup_headroom = minimum_cgroup_headroom
        self.require_cgroup_limit = require_cgroup_limit
        self.cgroups = cgroup_memory_directories()
        self.progress: Progress | None = None

    def check(self) -> dict:
        disk_free = []
        for index, reserve in enumerate(self.disks, start=1):
            try:
                usage = os.statvfs(reserve.path)
                free = usage.f_bavail * usage.f_frsize
            except OSError:
                raise GuardError(f"disk measurement is unavailable for reserve {index}") from None
            if free < reserve.minimum:
                raise GuardError(f"disk reserve {index} reached: {free / GIB:.2f} GiB available")
            disk_free.append(free)
        available = host_available_memory()
        if available < self.minimum_memory:
            raise GuardError(f"host memory reserve reached: {available / GIB:.2f} GiB available")
        headrooms = []
        try:
            for directory in self.cgroups:
                value = (directory / "memory.max").read_text(encoding="ascii").strip()
                if value == "max":
                    continue
                maximum = int(value)
                current = int((directory / "memory.current").read_text(encoding="ascii").strip())
                if maximum <= 0 or current < 0:
                    raise ValueError
                headroom = maximum - current
                if headroom < self.minimum_cgroup_headroom:
                    raise GuardError(f"cgroup memory reserve reached: {headroom / GIB:.2f} GiB remaining")
                headrooms.append(headroom)
        except (OSError, UnicodeError, ValueError):
            raise GuardError("cgroup memory measurement is unavailable") from None
        if self.require_cgroup_limit and not headrooms:
            raise GuardError("a finite cgroup memory limit is required before starting")
        metrics = {"minimum_disk_free_bytes": min(disk_free),
                   "host_available_memory_bytes": available,
                   "cgroup_limited_groups": len(headrooms),
                   "cgroup_minimum_headroom_bytes": min(headrooms) if headrooms else None}
        if self.progress is not None:
            self.progress.update(**metrics)
        return metrics


def parser() -> argparse.ArgumentParser:
    result = PrivateArgumentParser(description=__doc__)
    result.add_argument("--working-directory", type=Path, required=True)
    result.add_argument("--disk-reserve", nargs=2, action="append", required=True,
                        metavar=("PATH", "GIB"), help="existing path and free-space floor; repeat for scratch/data")
    result.add_argument("--min-available-memory-gib", type=positive_number, default=8.0)
    result.add_argument("--min-cgroup-headroom-gib", type=positive_number, default=2.0)
    result.add_argument("--require-cgroup-limit", action="store_true")
    result.add_argument("--max-runtime-hours", type=positive_number, required=True)
    result.add_argument("--progress-seconds", type=positive_number, default=30.0)
    result.add_argument("command", nargs=argparse.REMAINDER, help="-- executable [arguments ...]")
    return result


def execute(args: argparse.Namespace, runner: Runner | None = None) -> None:
    command = args.command
    if not command or command[0] != "--" or len(command) < 2 or not command[1]:
        raise GuardError("an explicit child command following -- is required")
    try:
        cwd = args.working_directory.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise GuardError("working directory must be an existing directory")
        disks = []
        for value, minimum in args.disk_reserve:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = cwd / path
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise GuardError("disk reserve paths must be existing directories")
            disks.append(DiskReserve(path, scaled_limit(minimum, GIB)))
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, GuardError):
            raise
        raise GuardError("working directory or disk reserve path is unavailable") from None
    timeout = scaled_limit(args.max_runtime_hours, 3600)
    interval = positive_number(args.progress_seconds)
    if interval > 3600:
        raise GuardError("progress interval must not exceed 3600 seconds")
    # Runner's shutdown heartbeat uses this environment setting. Validate it
    # before startup so it cannot disrupt the mandatory wait during rollback.
    try:
        progress_interval()
    except ValueError:
        raise GuardError("OJS_PROGRESS_SECONDS must be a number from 1 to 3600") from None
    guard = ResourceGuard(tuple(disks), scaled_limit(args.min_available_memory_gib, GIB),
                          scaled_limit(args.min_cgroup_headroom_gib, GIB),
                          require_cgroup_limit=args.require_cgroup_limit)
    initial = guard.check()  # Fail closed before Popen, including missing measurements.
    with Progress("guarded-local-build", interval=interval, step="supervise-child", **initial) as progress:
        guard.progress = progress
        (runner or Runner()).child(command[1:], cwd=cwd, env=os.environ.copy(),
                                   reserve_check=guard.check, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    previous_signals = {}

    def interrupt(signum, frame):
        raise GuardError("supervisor interrupted; waiting for safe pipeline shutdown")

    try:
        args = parser().parse_args(argv)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, interrupt)
        execute(args)
        return 0
    except (GuardError, UpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except (Exception, KeyboardInterrupt):
        # OS/subprocess exceptions can contain argv, environment or private paths.
        print("error: local build supervision failed; inspect pipeline progress", file=sys.stderr, flush=True)
        return 1
    finally:
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
