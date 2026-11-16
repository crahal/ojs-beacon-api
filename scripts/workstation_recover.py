#!/usr/bin/env python3
"""Run one audited recovery with temporary local credentials and matching tools.

Invoke only beneath run_guarded_local_build.py in a bounded service. The
local private config is read as data, never sourced as shell instructions.
Production secrets never need to leave the serving host. A finished imported
candidate must adopt the serving host's credentials before publication.
"""
from pathlib import Path
import argparse
import os
import re
import shlex
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import run_pipeline
from run_guarded_local_build import GIB, GuardError, host_available_memory, scaled_limit


def private_text(path: Path, *, limit: int = 65536) -> str:
    """Read bounded private configuration, including the generated two lines."""
    path = path.expanduser().absolute()
    if ".." in path.parts or path.resolve(strict=True) != path:
        raise run_pipeline.PipelineError("private build files must not contain symlinks")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid() or metadata.st_size > limit):
            raise run_pipeline.PipelineError("private build files must be bounded, regular, private, and owned by this user")
        value = source.read(limit + 1)
        if not value or len(value) > limit or b"\0" in value:
            raise run_pipeline.PipelineError("private build file content is invalid")
        return value.decode("utf-8")


def credentials(path: Path) -> dict[str, str]:
    text = private_text(path)
    selected = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in ("MYSQL_ROOT_PASSWORD", "OJS_DB_API_USER"):
            key = key.strip()
            if key in selected:
                raise run_pipeline.PipelineError("duplicate private build setting")
            tokens = shlex.split(value, comments=False)
            if len(tokens) != 1 or not tokens[0]:
                raise run_pipeline.PipelineError("invalid private build setting")
            selected[key] = tokens[0]
    if not selected.get("MYSQL_ROOT_PASSWORD"):
        raise run_pipeline.PipelineError("private build configuration has no root password")
    return selected


def matching_tools(directory: Path, expected: str) -> run_pipeline.MySQLTools:
    if re.fullmatch(r"8\.4\.\d+", expected) is None:
        raise run_pipeline.PipelineError("an exact MySQL 8.4 patch version is required")
    directory = directory.expanduser().resolve(strict=True)
    paths = {}
    for name in ("mysqld", "mysql", "mysqladmin", "mysqldump"):
        path = directory / name
        if not path.is_file() or not os.access(path, os.X_OK) or path.resolve().parent != directory:
            raise run_pipeline.PipelineError("the selected MySQL directory must contain all four local executables")
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
        match = re.search(r"\bVer\s+(\d+\.\d+\.\d+)", version.stdout or version.stderr)
        if version.returncode or match is None or match.group(1) != expected:
            raise run_pipeline.PipelineError("MySQL tool patch version differs from the stopped candidate")
        paths[name] = str(path)
    return run_pipeline.MySQLTools(**paths, version=expected)


def buffer_pool_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMG]?)", value, re.I)
    if match is None:
        raise run_pipeline.PipelineError("buffer pool size must be a positive integer with an optional K, M, or G suffix")
    return int(match.group(1)) * 1024 ** {"": 0, "K": 1, "M": 2, "G": 3}[match.group(2).upper()]


def disk_scratch(path: Path, *, mountinfo: Path = Path("/proc/self/mountinfo")) -> Path:
    scratch = run_pipeline.mysql_scratch_root(configured=str(path.expanduser().absolute()))
    filesystem, specificity = None, -1
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        fields, details = before.split(), after.split()
        if not separator or len(fields) < 5 or not details:
            raise run_pipeline.PipelineError("scratch filesystem measurement is invalid")
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4]))
        if mount.is_absolute() and scratch.is_relative_to(mount) and len(mount.parts) >= specificity:
            filesystem, specificity = details[0], len(mount.parts)
    if filesystem is None or filesystem in ("tmpfs", "ramfs", "devtmpfs", "hugetlbfs"):
        raise run_pipeline.PipelineError("scratch must use a measured disk-backed filesystem")
    return scratch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-env", type=Path, required=True)
    parser.add_argument("--api-password-file", type=Path, required=True)
    parser.add_argument("--mysql-bin", type=Path, required=True)
    parser.add_argument("--mysql-version", default="8.4.11")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--clean-dir", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--buffer-pool-size", default="8G")
    parser.add_argument("--min-available-memory-gib", default="8")
    args = parser.parse_args(argv)
    try:
        requested_memory = buffer_pool_bytes(args.buffer_pool_size)
        reserve = scaled_limit(args.min_available_memory_gib, GIB)
        if requested_memory > host_available_memory() - reserve:
            raise run_pipeline.PipelineError("requested buffer pool would breach the host memory reserve")
        scratch = disk_scratch(args.scratch_dir)
        settings = credentials(args.credentials_env)
        api_password = private_text(args.api_password_file, limit=4096).rstrip("\n")
        if not api_password or "\n" in api_password or "\r" in api_password:
            raise run_pipeline.PipelineError("API password must contain exactly one nonempty line")
        tools = matching_tools(args.mysql_bin, args.mysql_version)
        os.environ.update(settings)
        os.environ["OJS_DB_API_PASSWORD_FILE"] = str(args.api_password_file.resolve())
        os.environ.pop("OJS_DB_API_PASSWORD", None)
        os.environ["PATH"] = str(Path(tools.mysqld).parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ["OJS_MYSQL_TMP_ROOT"] = str(scratch)
        os.environ["OJS_BUILDER_MYSQL_TEMPTABLE_MAX_RAM"] = "256M"
        os.environ["OJS_BUILDER_MYSQL_MAX_CONNECTIONS"] = "8"
        os.environ["OJS_PROGRESS_SECONDS"] = "30"
        return run_pipeline.main([
            "--snapshot", str(args.snapshot.resolve()),
            "--raw-dir", str(args.snapshot.resolve().parent),
            "--clean-dir", str(args.clean_dir.resolve()),
            "--api-db-user", settings.get("OJS_DB_API_USER", "ojs_api"),
            "--mysql-buffer-pool-size", args.buffer_pool_size,
            "--skip-download", "--resume-building", "--compact-storage",
            "--recover-touched-keys",
        ])
    except (OSError, ValueError, GuardError, subprocess.TimeoutExpired, run_pipeline.PipelineError):
        print("error: local recovery setup failed; verify memory reserve, private files, disk-backed scratch, and matching MySQL tools", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
