"""Linux mount checks for destructive artifact cleanup, not publication.

Being absent from Docker's mount list does not make a directory safe to erase:
it can be a host bind mount, contain mounted children, or back another visible
mount. Compare filesystem-relative coordinates from mountinfo, not only visible
pathnames. A caller must still verify ownership, inode identity, symlinks and
process references, and call this check again immediately before deletion.

This observes the caller's mount namespace. It does not replace Docker/process
checks or serialize against a privileged process creating a new mount after the
check. Run cleanup in the host mount namespace without concurrent mount changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


_MAX_MOUNTINFO_BYTES = 8 * 1024 * 1024
_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
_ESCAPE_PATTERN = re.compile(r"\\(040|011|012|134)")


class MountSafetyError(RuntimeError):
    """Cleanup must retain an artifact when mount safety is unverified."""


@dataclass(frozen=True)
class _Mount:
    identifier: int
    device: str
    root: PurePosixPath | None
    point: PurePosixPath


def _path(value: str) -> PurePosixPath:
    # The kernel escapes these four characters in mountinfo paths. Reject an
    # unrecognized escape rather than silently comparing the wrong pathname.
    if "\\" in _ESCAPE_PATTERN.sub("", value):
        raise MountSafetyError("mount path encoding is invalid; cleanup retained")
    decoded = _ESCAPE_PATTERN.sub(lambda match: _ESCAPES[match.group(1)], value)
    if (not decoded.startswith("/") or decoded.startswith("//")
            or "\x00" in decoded or any(part in (".", "..") for part in decoded.split("/"))):
        raise MountSafetyError("mount coordinates are invalid; cleanup retained")
    return PurePosixPath(decoded)


def _read_mounts(path: Path) -> tuple[_Mount, ...]:
    try:
        with path.open("rb") as source:
            payload = source.read(_MAX_MOUNTINFO_BYTES + 1)
        if not payload or len(payload) > _MAX_MOUNTINFO_BYTES:
            raise MountSafetyError("mount inventory is empty or oversized; cleanup retained")
        content = payload.decode("utf-8")
    except (OSError, UnicodeError):
        # Do not expose potentially private mount paths in an OS exception.
        raise MountSafetyError("mount inventory is unavailable; cleanup retained") from None

    mounts = []
    identifiers: set[int] = set()
    for line in content.splitlines():
        before, separator, after = line.partition(" - ")
        fields, details = before.split(), after.split()
        if (not separator or len(fields) < 6 or len(details) != 3
                or not re.fullmatch(r"[1-9][0-9]*", fields[0])
                or not re.fullmatch(r"[0-9]+", fields[1])
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])):
            raise MountSafetyError("mount inventory is malformed; cleanup retained")
        identifier = int(fields[0])
        if identifier in identifiers:
            raise MountSafetyError("mount inventory has duplicate identifiers; cleanup retained")
        identifiers.add(identifier)
        point = _path(fields[4])
        # Namespace-handle bind mounts have a kernel object name, not a path
        # within a data filesystem. Their visible mountpoints still protect
        # artifacts when equal to or nested under the proposed cleanup target.
        if details[0] == "nsfs" and re.fullmatch(r"[a-z_]+:\[[0-9]+\]", fields[3]):
            root = None
        else:
            root = _path(fields[3])
        mounts.append(_Mount(identifier, fields[2], root, point))
    if not mounts:
        raise MountSafetyError("mount inventory is empty; cleanup retained")
    return tuple(mounts)


def assert_cleanup_unmounted(
    path: Path, *, mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Refuse cleanup of mounted artifacts, descendants, or bind sources.

    This deliberately does not reject a normal filesystem mounted *above* the
    artifact. A base mount which projects the same filesystem coordinate to
    the same visible pathname is not an additional alias. Other overlapping
    views on that device are conservatively protected.

    No mount state is cached: call once for planning, then again directly before
    unlink/rmtree. The caller owns existence and identity checks. This function
    only observes paths/mount metadata and never changes mounts or artifacts.
    """
    try:
        selected = Path(path)
        if not selected.is_absolute() or selected.is_symlink():
            raise MountSafetyError("cleanup target must be an absolute non-symlink path")
        target = PurePosixPath(selected.resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, MountSafetyError):
            raise
        raise MountSafetyError("cleanup target coordinates are unavailable") from None

    mounts = _read_mounts(mountinfo_path)
    for mount in mounts:
        if mount.point == target or mount.point.is_relative_to(target):
            raise MountSafetyError("artifact is a mountpoint or contains a mount; cleanup retained")

    containing = [mount for mount in mounts if target.is_relative_to(mount.point)]
    if not containing:
        raise MountSafetyError("artifact filesystem is absent from mount inventory; cleanup retained")
    depth = max(len(mount.point.parts) for mount in containing)
    deepest = [mount for mount in containing if len(mount.point.parts) == depth]
    # Sandboxes and mount-remount operations can stack identical views at one
    # point (e.g. a read-only base followed by the same writable bind). Their
    # filesystem coordinates agree; only differing views are ambiguous here.
    coordinates = {(mount.device, mount.root, mount.point) for mount in deepest}
    if len(coordinates) != 1 or deepest[0].root is None:
        raise MountSafetyError("artifact filesystem coordinates are ambiguous; cleanup retained")
    own = deepest[0]
    filesystem_path = own.root / target.relative_to(own.point)

    for mount in mounts:
        if mount.identifier == own.identifier or mount.device != own.device or mount.root is None:
            continue
        if filesystem_path.is_relative_to(mount.root):
            # A parent/base mount can describe precisely the same visible
            # location (e.g. a self-bind ancestor); that is not a second view.
            projected = mount.point / filesystem_path.relative_to(mount.root)
            if projected == target:
                continue
            raise MountSafetyError("artifact has a same-filesystem mount alias; cleanup retained")
        if mount.root.is_relative_to(filesystem_path):
            raise MountSafetyError("artifact backs a mounted subtree; cleanup retained")
