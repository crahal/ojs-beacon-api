#!/usr/bin/env python3
"""Bounded, read-only checks for a code-only, privacy-conscious repository.

The default scans tracked and nonignored candidate files in the working tree.
--history also checks reachable Git objects (including commit metadata). No data
directory, private configuration, symlink target, or oversized file is read.
This is a release guard, not a comprehensive secret or personal-data detector.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


MAX_BYTES = 5 * 1024 * 1024
PRIVATE_PARTS = frozenset({"data", ".secrets", ".aws", ".ssh", "credentials", ".local",
                           ".git", ".codex", ".agents", "operator-notes", ".operator-notes"})
PRIVATE_NAMES = frozenset({".envrc", ".beacon-credentials", "beacon.ini", "api-client.env",
                           "db-api-password", "local.env", "id_rsa", "id_dsa", "id_ecdsa",
                           "id_ed25519", "authorized_keys", "known_hosts"})
ARCHIVE_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zip", ".tar", ".7z", ".rar")
DATABASE_SUFFIXES = (".ibd", ".ibdata", ".sqlite", ".sqlite3", ".db", ".dump")
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?:%[A-Za-z0-9_.-]+)?(?![\w:])")
EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
HOME_PATH = re.compile(r"/(?:home|Users)/([A-Za-z0-9_.-]+)(?=/|\b)")
UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
VOLUME = re.compile(r"\bvol-?[0-9a-f]{8,17}\b")
ACCOUNT = re.compile(r"\barn:aws(?:-[a-z]+)?:[^:\s]+:[^:\s]*:\d{12}:")
EXAMPLE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8"))
EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid", "example.test"}
# GitHub uses these exact public service identities for web committers and
# Dependabot's Signed-off-by trailer. Do not allow the whole github.com domain:
# ordinary personal addresses and lookalike/plus-addressed variants still fail.
PUBLIC_SYSTEM_EMAILS = frozenset({"noreply@github.com", "support@github.com"})
GENERIC_USERS = {"user", "username", "example", "ojs", "ubuntu"}
EXAMPLE_UUIDS = {"00000000-0000-0000-0000-000000000000",
                 "11111111-1111-1111-1111-111111111111"}


@dataclass(frozen=True, order=True)
class Finding:
    scope: str
    path: str
    kind: str
    line: int = 0


def path_problem(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return "unsafe-path"
    if any(part.lower() in PRIVATE_PARTS for part in path.parts):
        return "private-or-data-path"
    base = path.name.lower()
    if base in PRIVATE_NAMES or (base.startswith(".env") and base != ".env.example"):
        return "private-configuration"
    if base.endswith(KEY_SUFFIXES):
        return "credential-file"
    if base.endswith(ARCHIVE_SUFFIXES + DATABASE_SUFFIXES):
        return "archive-or-database"
    if base.endswith(".sql") and not (path.parts[0] == "sql" or path.parts[:2] == ("tests", "fixtures")):
        return "sql-outside-source-or-synthetic-fixtures"
    if base.startswith("pkpbeacon-") or base in {"ibdata1", "auto.cnf"}:
        return "generated-dataset-artifact"
    return None


def safe_email(domain: str) -> bool:
    domain = domain.lower()
    return (domain in EXAMPLE_DOMAINS or domain.endswith(".example.org")
            or domain.endswith(".example.com") or domain.endswith(".example.net")
            or domain.endswith(".invalid") or domain.endswith(".test")
            or domain == "users.noreply.github.com")


def text_problems(raw: bytes) -> list[tuple[str, int]]:
    """Report types/line numbers only, never the matched string."""
    if b"\x00" in raw:
        return [("binary-file-requires-review", 0)]
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return [("non-utf8-file-requires-review", 0)]
    problems: set[tuple[str, int]] = set()

    def add(kind: str, match: re.Match[str]) -> None:
        problems.add((kind, text.count("\n", 0, match.start()) + 1))

    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            add(kind, match)
    for match in IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if str(address) != "0.0.0.0" and not any(address in network for network in EXAMPLE_NETWORKS):
            add("non-example-ipv4", match)
    for match in IPV6.finditer(text):
        try:
            address = ipaddress.IPv6Address(match.group().split("%", 1)[0])
        except ValueError:
            continue
        if (address.is_loopback or address.is_unspecified
                or address in ipaddress.ip_network("2001:db8::/32")):
            continue
        if address.ipv4_mapped and any(address.ipv4_mapped in network for network in EXAMPLE_NETWORKS):
            continue
        add("non-example-ipv6", match)
    for match in EMAIL.finditer(text):
        if not safe_email(match.group(2)) and match.group().lower() not in PUBLIC_SYSTEM_EMAILS:
            add("non-example-email", match)
    for match in HOME_PATH.finditer(text):
        if match.group(1) not in GENERIC_USERS:
            add("personal-home-path", match)
    for match in UUID.finditer(text):
        if match.group().lower() not in EXAMPLE_UUIDS:
            add("deployment-uuid", match)
    for match in VOLUME.finditer(text):
        if match.group().replace("-", "") not in {"vol0123456789abcdef0", "vol00000000000000000"}:
            add("deployment-volume-id", match)
    for match in ACCOUNT.finditer(text):
        add("aws-account-id", match)
    return sorted(problems)


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Git errors can contain remote URLs or local private paths.
        raise RuntimeError("Git inspection failed; run from a readable Git checkout")
    return result.stdout


def candidate_paths(root: Path) -> list[str]:
    raw = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({os.fsdecode(name) for name in raw.split(b"\0") if name})


def read_candidate(root: Path, name: str) -> tuple[bytes | None, str | None]:
    """Read one bounded regular code file without following any symlink.

    None/None denotes an absent (deleted) candidate; a problem is never read.
    A returned byte snapshot can be safely reused by the public-source exporter.
    """
    problem = path_problem(name)
    if problem:
        return None, problem
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            info = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                return None, "symlink-not-read"
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        info = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return None, "symlink-not-read"
        if not stat.S_ISREG(info.st_mode):
            return None, "non-regular-file-not-read"
        if info.st_nlink != 1:
            return None, "hardlink-not-read"
        if info.st_size > MAX_BYTES:
            return None, "file-exceeds-5-MiB"
        # Refuse a final-component symlink switched in between lstat and open.
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptors[-1])
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_BYTES or opened.st_nlink != 1:
                return None, "file-changed-during-check"
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None, "file-exceeds-5-MiB"
    except FileNotFoundError:
        # A tracked deletion is absent from the candidate working-tree export.
        return None, None
    except OSError:
        return None, "file-unreadable-or-changed"
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return raw, None


def inspect_file(root: Path, name: str) -> list[Finding]:
    raw, problem = read_candidate(root, name)
    if problem:
        return [Finding("worktree", name, problem)]
    return [Finding("worktree", name, kind, line) for kind, line in text_problems(raw)] if raw is not None else []


def inspect_history(root: Path) -> tuple[list[Finding], int]:
    objects: dict[str, str] = {}
    for line in git(root, "rev-list", "--objects", "--all").splitlines():
        oid, _, name = line.partition(b" ")
        objects[oid.decode("ascii")] = os.fsdecode(name) if name else "<git-metadata>"
    if not objects:
        return [], 0
    metadata = subprocess.run(["git", "-C", str(root), "cat-file", "--batch-check"],
                              input="".join(oid + "\n" for oid in objects).encode("ascii"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if metadata.returncode:
        raise RuntimeError("Git object metadata inspection failed")
    records = [record.split() for record in metadata.stdout.splitlines()]
    # rev-list names only one path for a reused blob. Inspect every reachable
    # commit tree so a private file renamed to a public path cannot disappear
    # from the audit, and never read a blob that had a private/data alias.
    aliases: dict[str, set[str]] = {}
    linked: set[tuple[str, str]] = set()
    for oid_raw, kind, size_raw in records:
        if kind != b"commit":
            continue
        for entry in git(root, "ls-tree", "--full-tree", "-r", "-z", oid_raw.decode("ascii")).split(b"\0"):
            if not entry:
                continue
            attributes, name_raw = entry.split(b"\t", 1)
            mode, entry_kind, entry_oid = attributes.split()
            oid = entry_oid.decode("ascii")
            name = os.fsdecode(name_raw)
            if entry_kind == b"blob":
                aliases.setdefault(oid, set()).add(name)
                if mode == b"120000":
                    linked.add((oid, name))
    findings: list[Finding] = []
    inspected = 0
    for oid_raw, kind, size_raw in records:
        if kind not in {b"blob", b"commit", b"tag"}:
            continue
        inspected += 1
        oid = oid_raw.decode("ascii")
        scope = "history:" + oid[:12]
        name = "<git-metadata>"
        if kind == b"blob":
            names = sorted(aliases.get(oid, set()))
            if not names:
                findings.append(Finding(scope, "<unmapped-blob>", "unmapped-history-blob-not-read"))
                continue
            blocked = False
            for candidate in names:
                problem = path_problem(candidate)
                if problem or (oid, candidate) in linked:
                    findings.append(Finding(scope, candidate, problem or "historical-symlink-not-read"))
                    blocked = True
            if blocked:
                continue
            name = names[0]
        if int(size_raw) > MAX_BYTES:
            findings.append(Finding(scope, name, "object-exceeds-5-MiB"))
            continue
        raw = git(root, "cat-file", kind.decode("ascii"), oid)
        findings.extend(Finding(scope, name, reason, line) for reason, line in text_problems(raw))
    return findings, inspected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--history", action="store_true", help="also check all reachable Git history and commit metadata")
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(git(args.repo, "rev-parse", "--show-toplevel")).strip()).resolve()
        names = candidate_paths(root)
        findings = [finding for name in names for finding in inspect_file(root, name)]
        count = 0
        if args.history:
            historical, count = inspect_history(root)
            findings.extend(historical)
    except (OSError, RuntimeError, ValueError) as exc:
        print("Repository check could not complete: " + (str(exc) if isinstance(exc, RuntimeError)
                                                       else "filesystem or Git tool unavailable"), file=sys.stderr)
        return 2
    for finding in sorted(set(findings)):
        # ascii() escapes terminal control characters in filenames; matches are never printed.
        location = ascii(finding.path) + (":" + str(finding.line) if finding.line else "")
        print(f"FAIL [{finding.scope}] {location}: {finding.kind}")
    print(f"Checked {len(names)} candidate paths and {count} historical objects; "
          f"{len(set(findings))} finding(s). No data/private file contents or matches were printed.")
    if findings:
        print("Remove private artifacts from the share candidate; use example.org and RFC 5737 addresses. "
              "History findings require a reviewed clean-history export or a separately authorized cleanup.")
    else:
        print("Code-only/privacy guard passed. This does not prove that every secret or personal detail is absent.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
