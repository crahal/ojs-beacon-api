"""Read-only, per-filesystem admission budget for unattended full builds.

Budgets are operator-reviewed additional working bytes, not an estimate derived
from gzip size. Existing artifacts already consume the measured free space.
The caller must still enforce reserves throughout processing.
"""
from __future__ import annotations

import fcntl
import math
import os
import shutil
import stat
from pathlib import Path

GIB = 1024 ** 3


def _budget(env: dict, name: str, blockers: list[str]) -> int:
    value = env.get(name, "")
    code = name.lower().removeprefix("ojs_")
    if not str(value).strip():
        blockers.append(code + "_unset")
        return 0
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0 or number * GIB >= 2**63:
            raise ValueError
        return math.ceil(number * GIB)
    except (TypeError, ValueError, OverflowError):
        blockers.append(code + "_invalid")
        return 0


def build_budget(*, clean: Path, raw: Path, scratch: Path | None, env: dict,
                 needs_download: bool, remote_size: object,
                 reserve_gib: float, minimum_gib: float) -> dict:
    blockers: list[str] = []
    workspace = _budget(env, "OJS_BUILD_WORKSPACE_GB", blockers)
    scratch_bytes = _budget(env, "OJS_BUILD_SCRATCH_GB", blockers) if scratch else 0
    download_bytes = 0
    if needs_download:
        # Reserve the whole transfer even if a partial may be resumable. Do not
        # assume that a validator will allow reuse of those existing bytes.
        if type(remote_size) is not int or remote_size <= 0 or remote_size >= 2**63:
            blockers.append("source_size_unknown")
        else:
            download_bytes = remote_size
    limits = (reserve_gib, minimum_gib)
    if any(not math.isfinite(value) or value < 0 or value * GIB >= 2**63 for value in limits):
        raise ValueError("invalid build budget reserve")
    reserve = math.ceil(reserve_gib * GIB)
    minimum = math.ceil(minimum_gib * GIB)
    locations = [("data", clean, workspace, 0), ("raw", raw, 0, download_bytes)]
    if scratch:
        locations.append(("scratch", scratch, scratch_bytes, 0))
    filesystems: dict[int, dict] = {}
    for role, path, working, incoming in locations:
        try:
            if not path.is_dir() or path.is_symlink():
                raise OSError
            device = path.stat().st_dev
            available = shutil.disk_usage(path).free
            if available < 0:
                raise OSError
        except OSError:
            blockers.append(role + "_filesystem_unavailable")
            continue
        group = filesystems.setdefault(device, {
            "roles": [], "available_bytes": available, "working_bytes": 0,
            "download_bytes": 0, "reserve_bytes": reserve,
        })
        group["roles"].append(role)
        group["available_bytes"] = min(group["available_bytes"], available)
        group["working_bytes"] += working
        group["download_bytes"] += incoming
    for group in filesystems.values():
        group["required_free_bytes"] = max(
            minimum, group["working_bytes"] + group["download_bytes"] + reserve)
        group["enough_space"] = group["available_bytes"] >= group["required_free_bytes"]
        if not group["enough_space"]:
            blockers.append("insufficient_" + "_".join(group["roles"]) + "_space")
    return {"approved": not blockers, "blockers": blockers,
            "filesystems": list(filesystems.values()),
            "budget_is_operator_estimate": True}


def existing_lock_busy(path: Path) -> bool:
    """Inspect only an existing lock. Never create, replace, or unlink one."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        current = path.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("lock changed during observation")
        return False
    finally:
        os.close(descriptor)
