#!/usr/bin/env python3
"""Create a deterministic, checked code-only archive without Git history.

Candidate files come from Git's tracked + nonignored working-tree list and an
explicit source-path allowlist. The archive contains no .git directory, local
data, operator notes, credential files, filesystem links, or Git author data.
Review the archive before publishing; automated pattern checks are not proof
that all confidential prose or credentials have been detected.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile

import check_repository as guard


ALLOWED_DIRECTORIES = frozenset({".github", "deploy", "docs", "scripts", "sql", "src", "tests"})
ALLOWED_ROOT_FILES = frozenset({
    ".dockerignore", ".env.example", ".gitignore", ".editorconfig",
    "Dockerfile", "docker-compose.yml", "Makefile", "LICENSE", "README.md",
    "HOW_TO_CALL.md", "HOW_TO_DEPLOY.md", "LIGHTSAIL_DEPLOYMENT.md",
    "PROCESSING_GUIDE.md", "WORKSTATION_RECOVERY.md", "CONTRIBUTING.md", "SECURITY.md",
    "CHANGELOG.md", "CODE_OF_CONDUCT.md", "requirements.txt", "requirements-dev.txt", "pyproject.toml",
})
MAX_TOTAL_BYTES = 64 * 1024 * 1024


class ExportError(Exception):
    pass


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if guard.path_problem(name):
        return False
    return name in ALLOWED_ROOT_FILES or (len(path.parts) > 1 and path.parts[0] in ALLOWED_DIRECTORIES)


def collect_sources(root: Path) -> dict[str, bytes]:
    """Read/validate once; the exact validated byte snapshots are archived."""
    selected: dict[str, bytes] = {}
    failures: list[str] = []
    total = 0
    for name in guard.candidate_paths(root):
        if not allowed_path(name):
            failures.append(ascii(name) + ": outside-public-source-allowlist")
            continue
        raw, problem = guard.read_candidate(root, name)
        if problem:
            failures.append(ascii(name) + ": " + problem)
            continue
        if raw is None:
            continue
        issues = guard.text_problems(raw)
        if issues:
            failures.extend(ascii(name) + ":" + str(line) + ": " + kind for kind, line in issues)
            continue
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ExportError("Source candidate exceeds the 64 MiB total review limit")
        selected[name] = raw
    if failures:
        raise ExportError("Public source export refused:\n" + "\n".join(failures))
    if not selected:
        raise ExportError("No public source files found")
    return selected


def open_output_parent(output: Path) -> int:
    """Create a new output parent as needed without following symlink components."""
    path = output.absolute().parent
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise ExportError("Output path must not contain traversal components")
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_archive(root: Path, output: Path) -> tuple[str, int, int]:
    if ".." in output.parts or not output.name.endswith(".tar.gz"):
        raise ExportError("Choose a new .tar.gz output path without traversal components")
    output = output.absolute()
    try:
        relative = output.relative_to(root)
    except ValueError:
        relative = None
    if relative is not None and (len(relative.parts) < 2 or relative.parts[0] != "dist"):
        raise ExportError("An output inside this checkout must be under ignored dist/")
    if relative is not None:
        # Avoid making the next candidate scan include the generated archive.
        result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", str(relative)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise ExportError("Output must be ignored by Git; add dist/ to .gitignore first")
    selected = collect_sources(root)
    parent_fd = open_output_parent(output)
    created_info = None
    try:
        try:
            descriptor = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ExportError("Output already exists; choose another filename (nothing overwritten)") from exc
        with os.fdopen(descriptor, "wb") as target:
            created_info = os.fstat(target.fileno())
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=9) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for name, raw in sorted(selected.items()):
                        member = tarfile.TarInfo("ojs_api/" + name)
                        member.size = len(raw)
                        member.mode = 0o755 if name.endswith(".sh") else 0o644
                        member.uid = member.gid = member.mtime = 0
                        member.uname = member.gname = ""
                        archive.addfile(member, io.BytesIO(raw))
            target.flush()
            os.fsync(target.fileno())
        os.fsync(parent_fd)
        digest = hashlib.sha256()
        count = 0
        descriptor = os.open(output.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != (created_info.st_dev, created_info.st_ino):
                raise ExportError("Output changed during verification")
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), count, len(selected)
    except BaseException:
        if created_info is not None:
            try:
                info = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == (created_info.st_dev, created_info.st_ino):
                    os.unlink(output.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("dist/ojs-api-source.tar.gz"))
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(guard.git(args.repo, "rev-parse", "--show-toplevel")).strip()).resolve()
        output = args.output if args.output.is_absolute() else root / args.output
        digest, size, count = create_archive(root, output)
    except (ExportError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Source export failed: unreadable/changed filesystem or unsupported archive path; "
              "no existing output was overwritten.", file=sys.stderr)
        return 1
    print(json.dumps({"artifact": output.name, "sha256": digest, "bytes": size,
                      "files": count, "git_history_included": False}, sort_keys=True))
    print("Review this code-only archive before publishing. Existing checkout history has NOT been sanitized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
