#!/usr/bin/env python3
"""Adopt server-local credentials in one complete, offline incoming release.

Run on the serving host as the pipeline account. Only the temporary builder
root password travels with the incoming candidate; production secrets remain
in this host's private files. This command never publishes or prunes a release.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import compact_update as update
import run_pipeline as pipeline
from recovery_checks import wait_innodb_rollback

ADOPTION_MARKER = "OJS_CREDENTIALS_ADOPTED.json"
CONFIG_KEYS = frozenset({"MYSQL_ROOT_PASSWORD", "OJS_DB_API_PASSWORD_FILE",
                         "OJS_DB_API_USER", "OJS_DB_NAME", "OJS_MYSQL_IMAGE"})


class AdoptionError(RuntimeError):
    """Fixed diagnostics only: never include private input or driver output."""


def private_text(path: Path, *, limit: int = 65536) -> str:
    path = path.absolute()
    if ".." in path.parts or path.resolve(strict=True) != path:
        raise AdoptionError("Private configuration paths must not contain symlinks.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        information = os.fstat(source.fileno())
        if (not stat.S_ISREG(information.st_mode) or information.st_mode & 0o077
                or information.st_uid not in (0, os.geteuid())):
            raise AdoptionError("Private configuration must be an owner-readable regular file with mode 0600 or stricter.")
        value = source.read(limit + 1)
        if len(value) > limit or "\0" in value:
            raise AdoptionError("Private configuration has an invalid size or encoding.")
        return value


def read_configuration(root: Path) -> dict[str, str]:
    """Read selected literal .env assignments without executing shell code."""
    values = {}
    for line in private_text(root / ".env").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name.strip() not in CONFIG_KEYS:
            continue
        name = name.strip()
        if name in values or "$" in value or "`" in value:
            raise AdoptionError("Selected .env settings must be unique literal assignments without shell expansion.")
        lexer = shlex.shlex(value, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            fields = list(lexer)
        except ValueError:
            raise AdoptionError("Selected .env settings contain invalid quoting.") from None
        if len(fields) != 1 or not fields[0] or "\r" in fields[0] or "\n" in fields[0]:
            raise AdoptionError("Selected .env settings must each contain one nonempty literal value.")
        values[name] = fields[0]
    if not values.get("MYSQL_ROOT_PASSWORD") or not values.get("OJS_MYSQL_IMAGE"):
        raise AdoptionError("The private .env must define a root password and an exact MySQL image patch.")
    return values


def password_file(path: Path) -> str:
    value = private_text(path, limit=4096).rstrip("\n")
    if not value or "\n" in value or "\r" in value:
        raise AdoptionError("Password files must contain one nonempty line.")
    return value


def authenticate(server, passwords: tuple[str, ...]) -> str:
    # mysqladmin ping may succeed even when authentication was refused. Prove
    # authentication with a query, trying only the two explicitly known secrets.
    for password in dict.fromkeys(passwords):
        try:
            if server.execute("SELECT 1;", password=password,
                              label="credential-adoption-authentication") == "1":
                return password
        except pipeline.PipelineError:
            pass
    raise AdoptionError("Neither the server-local nor temporary root credential authenticates this candidate.")


def verify_provenance(server, release, database: str, password: str, version: str) -> None:
    tables = server.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY table_name;",
        database=database, password=password, label="credential-adoption-tables").splitlines()
    if set(tables) != set(pipeline.CLEAN_EXPORT_TABLES) | {"ojs_pipeline_metadata"}:
        raise AdoptionError("The incoming candidate is not a complete clean-only database.")
    provenance = server.execute("""
        SELECT DATE_FORMAT(snapshot_date, '%Y-%m-%d'), source_sha256,
               build_sql_sha256, mysql_version
        FROM ojs_pipeline_metadata WHERE id = 1;
        SELECT DATE_FORMAT(snapshot_date, '%Y-%m-%d'), source_sha256, build_sql_sha256
        FROM ojs_snapshots ORDER BY snapshot_date DESC LIMIT 1;
        """, database=database, password=password,
        label="credential-adoption-provenance").splitlines()
    snapshot = [release.date, release.source_sha256, release.build_sql_sha256]
    if provenance != ["\t".join(snapshot + [version]), "\t".join(snapshot)]:
        raise AdoptionError("Candidate database provenance does not match the verified release and MySQL patch.")


def shutdown_candidate(server, passwords: tuple[str, ...], active_password: str | None) -> None:
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        try:
            if server.running:
                active_password = authenticate(server, passwords)
        except Exception:
            pass  # shutdown also has a graceful SIGTERM fallback.
        finally:
            server.shutdown(active_password)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def adopt(args) -> dict:
    root = args.project_root.absolute()
    if root.resolve(strict=True) != root:
        raise AdoptionError("The project path must not contain symlinks.")
    clean, raw = root / "data" / "clean", root / "data" / "raw"
    for directory in (root / "data", clean, raw):
        if directory.is_symlink() or not directory.is_dir():
            raise AdoptionError("Existing regular project data directories are required.")
    config = read_configuration(root)
    production_root = config["MYSQL_ROOT_PASSWORD"]
    temporary_root = password_file(args.temporary_root_password_file)
    api_path = Path(config.get("OJS_DB_API_PASSWORD_FILE", ".secrets/db-api-password"))
    if not api_path.is_absolute():
        api_path = root / api_path
    api_password = password_file(api_path)
    database = config.get("OJS_DB_NAME", "pkpbeacon_db")
    api_user = config.get("OJS_DB_API_USER", "ojs_api")
    if (not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", api_user) or api_user == "root"):
        raise AdoptionError("The configured database or API account is invalid.")
    coordinator_args = update.parser().parse_args(["--project-root", str(root),
                                                  "--publish-release", args.snapshot])
    coordinator = update.Coordinator(coordinator_args)
    coordinator.env["OJS_MYSQL_IMAGE"] = config["OJS_MYSQL_IMAGE"]
    with update.source_activity(raw), update.automatic_lock(clean), \
            pipeline.pipeline_lock(clean), update.publish_live.publisher_lock(root):
        pause = root / "data" / update.PAUSE_MARKER
        if pause.is_symlink() or not pause.is_file():
            raise AdoptionError("Keep a regular data/.updates-paused marker in place during credential adoption.")
        if coordinator.journal.exists() or coordinator.journal.is_symlink():
            raise AdoptionError("Resolve the pending publication journal before credential adoption.")
        release = update.validate_release(clean, args.snapshot)
        if update.live_directory(clean) == release.datadir:
            raise AdoptionError("The serving database cannot be rekeyed by this command.")
        if any(path.is_symlink() for path in release.datadir.rglob("*")):
            raise AdoptionError("The incoming database must not contain symlinks.")
        tools = coordinator.compatible_mysql_tools()
        marker = update.read_json(release.datadir / update.MARKER)
        if marker.get("mysql_version") != tools.version:
            raise AdoptionError("Candidate, installed MySQL and serving image must use the same exact patch.")
        pipeline.assert_database_stopped(release.datadir)
        coordinator.require_unmounted(release.datadir)
        coordinator.capacity(8)
        coordinator.memory_capacity()
        server = pipeline.MySQLServer(tools, release.datadir, "128M")
        active_password = None
        try:
            server.start(production_root)
            active_password = authenticate(server, (production_root, temporary_root))
            wait_innodb_rollback(server, database=database, password=active_password)
            verify_provenance(server, release, database, active_password, tools.version)
            pipeline.configure_api_database_user(
                server, database=database, password=active_password,
                api_user=api_user, api_password=api_password,
            )
            pipeline.set_root_password(server, active_password, production_root)
            active_password = authenticate(server, (production_root,))
        finally:
            # ALTER USER may commit before its client is interrupted or a later
            # statement fails. Rediscover the known active password and retain
            # all locks until graceful shutdown completes.
            shutdown_candidate(server, (production_root, temporary_root), active_password)
        receipt = {"schema_version": 1, "snapshot_date": release.date,
                   "source_sha256": release.source_sha256,
                   "build_sql_sha256": release.build_sql_sha256,
                   "mysql_version": tools.version,
                   "adopted_at": datetime.now(timezone.utc).isoformat()}
        update.atomic_json(release.datadir / ADOPTION_MARKER, receipt)
        return {"status": "credentials_adopted", "release": release.date,
                "published": False, "updates_paused": True}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-root", type=Path, default=ROOT)
    result.add_argument("--snapshot", type=update.snapshot_date, required=True)
    result.add_argument("--temporary-root-password-file", type=Path, required=True)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    def interrupted(signum, frame):
        raise AdoptionError("Credential adoption was interrupted; the candidate remains unpublished.")
    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        print(json.dumps(adopt(args), sort_keys=True))
        return 0
    except AdoptionError as error:
        print(f"error: {error}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        # Driver errors and malformed input may contain a secret. Keep all
        # unexpected diagnostics fixed; neither configuration is ever echoed.
        print("error: Candidate credential adoption failed; candidate and credential files retained.", file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        os.umask(previous_umask)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
