"""Lossless widening of XML-derived payload fields, without changing identity.

Apply this profile *after* the compact SQL renderer. The original SQL file and
its matching/hash expressions remain unchanged. Existing imported history and
metadata-ready databases require the matching, explicit schema migration.
Callers must hold the normal pipeline locks and own a private MySQL instance.
This module neither changes checkpoints nor truncates or deletes any rows.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from progress_logging import Progress


METADATA_STORAGE_PROFILE = "lossless-mediumtext-payload-v1"
_PAYLOAD = ("title", "publisher", "published", "types", "formats",
            "source_title", "languages", "coverage")
_TABLE_COLUMNS = {
    "ojs_articles": _PAYLOAD,
    "ojs_article_sources": ("title", "first_creator"),
    "ojs_stage_sources": (*_PAYLOAD, "first_creator"),
    "ojs_stage_canonical_payload": _PAYLOAD,
}
_REQUIRED_TABLES = ("ojs_articles", "ojs_article_sources", "ojs_stage_sources")
_MIGRATION_ORDER = ("ojs_stage_sources", "ojs_article_sources", "ojs_articles",
                    "ojs_stage_canonical_payload")
_COLLATION = "utf8mb4_0900_ai_ci"
_MIN_COPY_BYTES = 64 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


class MetadataStorageError(ValueError):
    """Unsupported SQL/schema or insufficient space for a lossless migration."""


def _fail(message: str) -> None:
    raise MetadataStorageError(f"metadata storage migration refused: {message}")


def render_metadata_storage_sql(sql: str) -> str:
    """Widen only the reviewed free-text declarations; leave all SQL intact.

The complete, already rendered build program must contain the four known
schemas. Reapplying the profile is harmless. Unexpected declarations fail
closed rather than silently changing a matching key, collation or default.
"""
    if not isinstance(sql, str):
        _fail("SQL text is required")
    for table, fields in _TABLE_COLUMNS.items():
        pattern = re.compile(
            rf"(CREATE TABLE(?: IF NOT EXISTS)? {table} \(\n)"
            rf"(.*?)(\n\) ENGINE=InnoDB)", re.DOTALL,
        )
        matches = list(pattern.finditer(sql))
        if len(matches) != 1:
            _fail(f"expected one {table} definition")
        match = matches[0]
        schema = match.group(2)
        for field in fields:
            declaration = re.compile(rf"^    {field} (?:TEXT|MEDIUMTEXT) NULL,$", re.MULTILINE)
            if len(declaration.findall(schema)) != 1:
                _fail(f"unexpected {table}.{field} declaration")
            schema = declaration.sub(f"    {field} MEDIUMTEXT NULL,", schema)
        if re.search(r"^    \w+ TEXT\b", schema, re.MULTILINE):
            _fail(f"unreviewed free-text declaration in {table}")
        sql = sql[:match.start(2)] + schema + sql[match.end(2):]
    return sql


def _query(server, sql: str, *, database: str, password: str | None, label: str) -> str:
    return server.execute(sql, database=database, password=password, label=label)


def _arguments(database: str, reserve_bytes: int, required_tables) -> tuple[str, ...]:
    if not isinstance(database, str) or not _IDENTIFIER.fullmatch(database):
        _fail("invalid database identifier")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        _fail("reserve_bytes must be a nonnegative integer")
    required = tuple(required_tables)
    if len(set(required)) != len(required) or any(name not in _TABLE_COLUMNS for name in required):
        _fail("unknown or duplicated required table")
    return required


def _inventory(server, *, database: str, password: str | None,
               required_tables: tuple[str, ...]) -> dict:
    names = ", ".join(f"'{name}'" for name in _TABLE_COLUMNS)
    output = _query(server, f"""
        SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION,
               COALESCE(DATA_LENGTH, 0), COALESCE(INDEX_LENGTH, 0)
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({names})
        ORDER BY TABLE_NAME;
        """, database=database, password=password, label="metadata-storage-tables")
    tables = {}
    for line in output.splitlines():
        row = line.split("\t")
        if len(row) != 6 or row[0] not in _TABLE_COLUMNS or row[0] in tables:
            _fail("invalid table inventory")
        name, kind, engine, collation, data, indexes = row
        if (kind, engine, collation) != ("BASE TABLE", "InnoDB", _COLLATION):
            _fail(f"unexpected engine, table kind or collation for {name}")
        if not data.isdecimal() or not indexes.isdecimal():
            _fail("invalid table size estimate")
        tables[name] = {"data_bytes": int(data), "index_bytes": int(indexes), "columns": {}}
    if any(name not in tables for name in required_tables):
        _fail("required tables are missing")
    output = _query(server, f"""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
               COALESCE(CHARACTER_SET_NAME, ''), COALESCE(COLLATION_NAME, ''),
               COLUMN_DEFAULT IS NULL, EXTRA, COALESCE(GENERATION_EXPRESSION, '')
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({names})
        ORDER BY TABLE_NAME, ORDINAL_POSITION;
        """, database=database, password=password, label="metadata-storage-columns")
    for line in output.splitlines():
        row = line.split("\t")
        # MySQLServer.execute strips the final newline and whitespace. The last
        # row's empty EXTRA/generation fields therefore need explicit padding.
        if 7 <= len(row) < 9:
            row += [""] * (9 - len(row))
        if len(row) != 9 or row[0] not in tables:
            _fail("invalid column inventory")
        table, field, kind, nullable, charset, collation, default_null, extra, generated = row
        if field not in _TABLE_COLUMNS[table]:
            if kind.lower() == "text":
                _fail(f"unreviewed free-text column in {table}")
            continue
        columns = tables[table]["columns"]
        if field in columns:
            _fail("duplicate payload column")
        if (kind.lower() not in ("text", "mediumtext") or nullable != "YES"
                or charset != "utf8mb4" or collation != _COLLATION
                or default_null != "1" or extra or generated):
            _fail(f"unexpected type, nullability, collation or default for {table}.{field}")
        columns[field] = kind.lower()
    for table, details in tables.items():
        if set(details["columns"]) != set(_TABLE_COLUMNS[table]):
            _fail(f"missing payload columns in {table}")
    return tables


def _table_bytes(server, database: str, table: str, details: dict) -> int:
    """Prefer physical file length when persistent table statistics are stale."""
    estimated = details["data_bytes"] + details["index_bytes"]
    path = Path(server.datadir) / database / f"{table}.ibd"
    if path.is_symlink():
        _fail("table file must not be a symlink")
    try:
        if path.exists():
            if not path.is_file():
                _fail("unexpected table-file type")
            estimated = max(estimated, path.stat().st_size)
    except OSError as exc:
        raise MetadataStorageError("could not inspect table copy size") from exc
    return estimated


def _space(server, copy_bytes: int, reserve_bytes: int) -> dict:
    """Check datadir and optional scratch volumes without a recursive scan."""
    paths = [Path(server.datadir)]
    scratch = getattr(server, "scratch_root", None)
    if scratch is not None:
        paths.append(Path(scratch))
    available = []
    devices = set()
    try:
        for path in paths:
            device = path.stat().st_dev
            if device not in devices:
                devices.add(device)
                available.append(shutil.disk_usage(path).free)
    except OSError as exc:
        raise MetadataStorageError("could not inspect migration disk capacity") from exc
    free = min(available)
    required = reserve_bytes + copy_bytes
    return {"available_bytes": free, "reserve_bytes": reserve_bytes,
            "copy_budget_bytes": copy_bytes, "required_free_bytes": required,
            "capacity_ok": free >= required}


def estimate_metadata_storage_migration(
    server, *, database: str, password: str | None, reserve_bytes: int,
    required_tables: tuple[str, ...] = _REQUIRED_TABLES,
) -> dict:
    """Read-only schema validation and conservative, explicitly estimated plan.

An ALTER copies one table at a time. Budget 1.5 times its larger reported or
physical size, with a 64 MiB minimum, plus the caller's reserve. This is not a
hard peak-size guarantee: the caller's live disk guard must remain active.
"""
    required = _arguments(database, reserve_bytes, required_tables)
    tables = _inventory(server, database=database, password=password, required_tables=required)
    plan = []
    for table in _MIGRATION_ORDER:
        if table not in tables:
            continue
        details = tables[table]
        before = dict(details["columns"])
        changed = [field for field in _TABLE_COLUMNS[table] if before[field] == "text"]
        size = _table_bytes(server, database, table, details)
        budget = max(_MIN_COPY_BYTES, (size * 3 + 1) // 2) if changed else 0
        plan.append({"table": table, "columns_before": before,
                     "columns_after": {field: "mediumtext" for field in before},
                     "changed_columns": changed, "table_bytes_estimate": size,
                     **_space(server, budget, reserve_bytes)})
    return {"profile": METADATA_STORAGE_PROFILE, "schema_version": 1,
            "copy_budget_factor": 1.5, "tables": plan,
            "capacity_ok": all(item["capacity_ok"] for item in plan)}


def migrate_metadata_storage(
    server, *, database: str, password: str | None, reserve_bytes: int,
    required_tables: tuple[str, ...] = _REQUIRED_TABLES,
    progress_seconds: float = 30,
) -> dict:
    """Validate all tables first, then widen once per table without data loss.

DDL is atomic per table, not across tables. A retry validates every table and
accepts any already widened columns; it never narrows columns or recreates a
table. The server client emits heartbeat/process metrics while COPY blocks.
"""
    plan = estimate_metadata_storage_migration(
        server, database=database, password=password, reserve_bytes=reserve_bytes,
        required_tables=required_tables,
    )
    if not plan["capacity_ok"]:
        _fail("insufficient disk headroom for table copy and reserve")
    changes = [item for item in plan["tables"] if item["changed_columns"]]
    if changes:
        isolation = _query(server, """
            SELECT (SELECT COUNT(*) FROM information_schema.PROCESSLIST
                    WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon'),
                   (SELECT COUNT(*) FROM information_schema.INNODB_TRX);
            """, database=database, password=password, label="metadata-storage-isolation")
        if isolation != "0\t0":
            _fail("another client or active transaction exists")
    with Progress("metadata-storage-migration", interval=progress_seconds,
                  disk_path=Path(server.datadir), profile=METADATA_STORAGE_PROFILE,
                  total_tables=len(changes), completed_tables=0) as progress:
        for completed, item in enumerate(changes, 1):
            capacity = _space(server, item["copy_budget_bytes"], reserve_bytes)
            if not capacity["capacity_ok"]:
                _fail("disk headroom fell below table-copy budget and reserve")
            table = item["table"]
            modifications = ",\n    ".join(
                f"MODIFY COLUMN `{field}` MEDIUMTEXT CHARACTER SET utf8mb4 "
                f"COLLATE {_COLLATION} NULL DEFAULT NULL"
                for field in item["changed_columns"]
            )
            statement = f"ALTER TABLE `{table}`\n    {modifications},\n    ALGORITHM=COPY, LOCK=EXCLUSIVE;"
            progress.event("table-copy-started", table=table,
                           copy_budget_bytes=item["copy_budget_bytes"],
                           available_bytes=capacity["available_bytes"])
            server.run_sql_text(
                statement, label="metadata-storage-table-copy", database=database,
                password=password,
                prelude="SET SESSION lock_wait_timeout = 60;\n"
                        "SET SESSION sql_mode = CONCAT_WS(',', @@SESSION.sql_mode, 'STRICT_ALL_TABLES');",
                table=table, changed_columns=len(item["changed_columns"]),
                copy_budget_bytes=item["copy_budget_bytes"],
            )
            verified = _inventory(server, database=database, password=password,
                                  required_tables=tuple(required_tables))
            if verified[table]["columns"] != item["columns_after"]:
                _fail("table copy did not produce the reviewed target schema")
            progress.event("table-copy-completed", table=table, completed_tables=completed)
    plan["completed"] = True
    plan["changed_table_count"] = len(changes)
    # Bind audit interpretation to this exact module, independently of the
    # original build SQL checksum and the rendered program's own checksum.
    plan["implementation_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return plan
