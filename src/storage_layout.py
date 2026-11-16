"""Optional physical InnoDB layouts; SQL values and identity stay unchanged.

The clean-export transformer understands the bounded CREATE TABLE statements
emitted by this project's mysqldump. It tracks SQL quoting across input chunks
so text inside INSERT values can never be mistaken for a table declaration.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping


PAGE_SIZE_ENV = "OJS_MYSQL_COMPRESSED_PAGE_KB"
EVENT_COMPRESSION_ENV = "OJS_COMPRESS_EVENT_HISTORY"
SUPPORTED_PAGE_KB = (8, 4)
COMPACT_TABLES = frozenset(("ojs_articles", "ojs_article_sources", "ojs_article_keys"))
CLEAN_TABLES = COMPACT_TABLES | {"ojs_snapshots", "ojs_article_events"}


class StorageLayoutError(ValueError):
    """The requested layout or imported DDL cannot be safely transformed."""


def compression_page_kb(env: Mapping[str, str] | None = None) -> int:
    value = (os.environ if env is None else env).get(PAGE_SIZE_ENV, "8")
    if value not in ("8", "4"):
        raise StorageLayoutError(f"{PAGE_SIZE_ENV} must be exactly 8 or 4")
    return int(value)


def event_history_enabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(EVENT_COMPRESSION_ENV, "0")
    if value not in ("0", "1"):
        raise StorageLayoutError(f"{EVENT_COMPRESSION_ENV} must be exactly 0 or 1")
    return value == "1"


def _validate_page_kb(page_kb: int) -> None:
    if type(page_kb) is not int or page_kb not in SUPPORTED_PAGE_KB:
        raise StorageLayoutError("compressed InnoDB page size must be 8 or 4 KiB")


def layout_profile(page_kb: int) -> str:
    _validate_page_kb(page_kb)
    return f"innodb-compressed-{page_kb}k-v1"


def event_history_profile(page_kb: int) -> str:
    _validate_page_kb(page_kb)
    return f"innodb-events-compressed-{page_kb}k-v1"


def compression_options(page_kb: int) -> str:
    _validate_page_kb(page_kb)
    return f"ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE={page_kb}"


def recorded_layout_audit_options(storage: Mapping) -> dict | None:
    """Derive release verification from recorded profiles, never current flags."""
    if not isinstance(storage, Mapping):
        raise StorageLayoutError("release storage metadata must be an object")
    profile = storage.get("storage_layout_profile")
    events = storage.get("event_history_profile")
    if profile is None and events is None:
        return None
    layouts = {layout_profile(size): size for size in SUPPORTED_PAGE_KB}
    event_layouts = {event_history_profile(size): size for size in SUPPORTED_PAGE_KB}
    if profile is not None and (not isinstance(profile, str) or profile not in layouts):
        raise StorageLayoutError("release has an unsupported physical storage profile")
    if events is not None and (not isinstance(events, str) or events not in event_layouts):
        raise StorageLayoutError("release has an unsupported event history profile")
    page_kb = layouts.get(profile, 8)
    if events is not None and event_layouts[events] != page_kb:
        raise StorageLayoutError("release physical and event storage profiles disagree")
    return {"page_kb": page_kb, "include_event_history": events is not None}


def render_event_history_sql(sql: str, page_kb: int) -> str:
    """Compress newly created event tables without rewriting an existing table."""
    options = compression_options(page_kb)
    start = "CREATE TABLE IF NOT EXISTS ojs_article_events (\n"
    ending = ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;\n"
    if sql.count(start) != 1:
        raise StorageLayoutError("event compression requires one supported event table definition")
    before, rest = sql.split(start, 1)
    schema, separator, after = rest.partition(ending)
    if not separator or "\nCREATE " in schema or re.search(r"\b(?:ROW_FORMAT|KEY_BLOCK_SIZE)\b", schema):
        raise StorageLayoutError("event compression found unsupported event table storage options")
    return before + start + schema + ending.replace(
        ") ENGINE=InnoDB", ") ENGINE=InnoDB " + options, 1,
    ) + after


def audit_storage_layout(server, *, database: str, password: str | None, page_kb: int,
                         include_event_history: bool = False) -> dict:
    """Check physical tablespace metadata, not requested DDL or table contents."""
    _validate_page_kb(page_kb)
    expected_tables = COMPACT_TABLES | ({"ojs_article_events"} if include_event_history else set())
    table_names = ", ".join(
        f"CONCAT(DATABASE(), '/{table}')" for table in sorted(expected_tables)
    )
    output = server.execute(
        "SELECT SUBSTRING_INDEX(NAME, '/', -1), ROW_FORMAT, ZIP_PAGE_SIZE "
        "FROM information_schema.INNODB_TABLESPACES "
        f"WHERE NAME IN ({table_names}) ORDER BY NAME;",
        database=database, password=password, label="physical-storage-audit",
    )
    tables = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in expected_tables or fields[0] in tables:
            raise StorageLayoutError("physical storage audit returned unexpected tablespace metadata")
        table, row_format, zip_size = fields
        if row_format != "Compressed" or zip_size != str(page_kb * 1024):
            raise StorageLayoutError(
                f"physical storage audit failed for {table}: expected compressed {page_kb} KiB pages"
            )
        tables[table] = {"row_format": row_format, "zip_page_size": int(zip_size)}
    if tables.keys() != expected_tables:
        raise StorageLayoutError("physical storage audit is missing required InnoDB tablespaces")
    return {"profile": layout_profile(page_kb), "tables": tables}


class _SQLBoundary:
    """Recognize statement boundaries without buffering SQL payloads.

    Searches run in C over ordinary payload bytes; Python handles only quotes,
    escapes, comment delimiters and semicolons. Memory is bounded by the caller's
    input chunk, even for a multi-megabyte extended INSERT.
    """

    _outside = re.compile(rb"['\"`;#]|/\*|--(?=[\x00-\x20])")
    _quoted = {quote: re.compile(re.escape(quote) + rb"|\\")
               for quote in (b"'", b'"', b"`")}

    def __init__(self) -> None:
        self.statement = False
        self.quote: bytes | None = None
        self.escape = False
        self.comment: str | None = None
        self.tail = b""

    @property
    def plain(self) -> bool:
        return self.quote is None and self.comment is None and not self.tail

    @property
    def start(self) -> bool:
        return self.plain and not self.statement

    def feed(self, chunk: bytes) -> None:
        data = self.tail + chunk
        self.tail = b""
        position = 0
        while position < len(data):
            if self.comment == "line":
                end = data.find(b"\n", position)
                if end < 0:
                    return
                self.comment = None
                position = end + 1
            elif self.comment == "block":
                end = data.find(b"*/", position)
                if end < 0:
                    self.tail = b"*" if data.endswith(b"*") else b""
                    return
                self.comment = None
                position = end + 2
            elif self.quote is not None:
                if self.escape:
                    position += 1
                    self.escape = False
                    continue
                token = self._quoted[self.quote].search(data, position)
                if token is None:
                    return
                position = token.end()
                if token.group() == b"\\":
                    self.escape = True
                else:
                    self.quote = None
            else:
                token = self._outside.search(data, position)
                if token is None:
                    # A comment opener may be split across bounded chunks.
                    suffix = next((ending for ending in (b"--", b"/", b"-")
                                   if data[position:].endswith(ending)), b"")
                    end = len(data) - len(suffix)
                    if data[position:end].strip():
                        self.statement = True
                    self.tail = suffix
                    return
                if data[position:token.start()].strip():
                    self.statement = True
                position = token.end()
                value = token.group()
                if value == b";":
                    self.statement = False
                elif value in self._quoted:
                    self.quote = value
                    self.statement = True
                else:
                    self.comment = "block" if value == b"/*" else "line"


class CleanExportDDL:
    """Rewrite only known clean-table storage options in a mysqldump stream.

    Supply bounded ``readline`` fragments, preserving their original boundaries.
    INSERT bytes are returned verbatim. Unknown/duplicate/incomplete CREATE
    statements or storage options fail closed before a release can be produced.
    """

    _create = re.compile(rb"CREATE TABLE `([a-z_]+)` \(\r?\n")
    _closing = re.compile(rb"\) ENGINE=InnoDB(?P<options>[^\r\n]*);(?P<newline>\r?\n)")
    _option = re.compile(
        rb" (?:(?P<charset>DEFAULT CHARSET)=(?P<charset_value>[A-Za-z0-9_]+)"
        rb"|(?P<collate>COLLATE)=(?P<collate_value>[A-Za-z0-9_]+)"
        rb"|(?P<row>ROW_FORMAT)=(?P<row_value>COMPRESSED|DYNAMIC|COMPACT|REDUNDANT)"
        rb"|(?P<key>KEY_BLOCK_SIZE)=(?P<key_value>1|2|4|8|16)"
        rb"|(?P<auto>AUTO_INCREMENT)=(?P<auto_value>[0-9]+))"
    )

    def __init__(self, page_kb: int, *, compress_events: bool = False,
                 restore_uncompressed_events: bool = False) -> None:
        _validate_page_kb(page_kb)
        if (type(compress_events) is not bool or type(restore_uncompressed_events) is not bool
                or (compress_events and restore_uncompressed_events)):
            raise StorageLayoutError("event table compression options are inconsistent")
        self.page_kb = page_kb
        self.compress_events = compress_events
        self.restore_uncompressed_events = restore_uncompressed_events
        self.line_start = True
        self.table: str | None = None
        self.ddl_bytes = 0
        self.seen: set[str] = set()
        self.boundary = _SQLBoundary()

    def transform(self, chunk: bytes) -> bytes:
        at_start = self.line_start
        self.line_start = chunk.endswith(b"\n")
        output = chunk
        if at_start and self.boundary.start and chunk.lstrip().upper().startswith(b"CREATE"):
            match = self._create.fullmatch(chunk)
            if match is None:
                raise StorageLayoutError("unsupported clean-export CREATE TABLE declaration")
            table = match[1].decode("ascii")
            if table not in CLEAN_TABLES or table in self.seen or self.table is not None:
                raise StorageLayoutError("unexpected or duplicate clean-export table declaration")
            self.table = table
            self.ddl_bytes = 0
        if self.table is not None:
            self.ddl_bytes += len(chunk)
            if self.ddl_bytes > 256 * 1024:
                raise StorageLayoutError("clean-export table declaration exceeds the safe DDL limit")
            if at_start and self.boundary.plain and chunk.startswith(b")"):
                output = self._finish_table(chunk)
                self.seen.add(self.table)
                self.table = None
        self.boundary.feed(chunk)
        return output

    def _finish_table(self, chunk: bytes) -> bytes:
        match = self._closing.fullmatch(chunk)
        if match is None:
            raise StorageLayoutError("unsupported clean-export InnoDB table declaration")
        options = match["options"]
        kept = []
        names = set()
        position = 0
        while position < len(options):
            option = self._option.match(options, position)
            if option is None:
                raise StorageLayoutError("unsupported clean-export table storage options")
            name = next(name for name in ("charset", "collate", "row", "key", "auto")
                        if option[name] is not None)
            if name in names:
                raise StorageLayoutError("duplicate clean-export table storage option")
            names.add(name)
            if name not in ("row", "key"):
                kept.append(option.group())
            position = option.end()
        if self.table == "ojs_article_events" and self.restore_uncompressed_events:
            return b") ENGINE=InnoDB" + b"".join(kept) + b" ROW_FORMAT=DYNAMIC;" + match["newline"]
        if self.table not in COMPACT_TABLES and not (self.table == "ojs_article_events" and self.compress_events):
            return chunk
        return (b") ENGINE=InnoDB" + b"".join(kept) + b" "
                + compression_options(self.page_kb).encode("ascii") + b";" + match["newline"])

    def finish(self) -> None:
        if self.table is not None or self.seen != CLEAN_TABLES:
            raise StorageLayoutError("clean export does not contain exactly the expected complete tables")
        if (self.boundary.quote is not None or self.boundary.comment == "block"
                or self.boundary.statement or self.boundary.tail):
            raise StorageLayoutError("clean export ends inside an incomplete SQL statement")
