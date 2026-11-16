"""Bounded, lossless deferral of baseline-only article payloads.

Only the exact column order and INSERT grammar emitted by our mysqldump are
accepted. Values are tokenized across fragments; payload bytes are never
decoded, re-escaped, normalized, or matched with a substitution expression.
"""
from __future__ import annotations

import os
import re

from storage_layout import _SQLBoundary


DEFERRED_PAYLOAD_PROFILE = "baseline-article-payload-v1"
DEFER_ENV = "OJS_DEFER_BASELINE_PAYLOAD"
TRACKING_TABLE = "ojs_pipeline_deferred_payload"
HYDRATION_TABLE = "ojs_pipeline_payload_hydration"
ARTICLE_COLUMNS = (
    "article_id", "status", "merged_into_article_id", "canonical_source_record_id",
    "source_count", "active_source_count", "version_number", "date_added",
    "date_modified", "date_removed", "application", "journal_issn", "journal_title",
    "endpoint_oai_url", "source_oai_identifier", "record_update_date",
    "record_publish_date", "title", "creators", "subjects", "description",
    "publisher", "published", "types", "formats", "identifiers", "source_title",
    "languages", "relations", "coverage", "rights", "doi", "article_url",
    "metadata_xml", "canonical_metadata_hash", "data_hash", "provenance_hash",
)
PAYLOAD_COLUMNS = ARTICLE_COLUMNS[17:31] + ARTICLE_COLUMNS[32:34]
_PAYLOAD_POSITIONS = frozenset(ARTICLE_COLUMNS.index(column) for column in PAYLOAD_COLUMNS)
_INSERT = b"INSERT INTO `ojs_articles` VALUES "
_QUOTED_SPECIAL = re.compile(rb"['\\]")
_UNQUOTED_END = re.compile(rb"[,\)\s]")
_UNQUOTED_VALUE = re.compile(rb"(?:NULL|0x[0-9a-fA-F]*|-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)")


class DeferredPayloadError(ValueError):
    pass


def deferred_payload_enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get(DEFER_ENV, "0")
    if value not in ("0", "1"):
        raise DeferredPayloadError(f"{DEFER_ENV} must be exactly 0 or 1")
    return value == "1"


TRACKING_SQL = f"""CREATE TABLE {TRACKING_TABLE} (
    article_id BIGINT UNSIGNED NOT NULL PRIMARY KEY
) ENGINE=InnoDB;
INSERT INTO {TRACKING_TABLE} SELECT article_id FROM ojs_articles;
"""


def hydration_prefix() -> bytes:
    columns = ",\n".join(f"`{name}` MEDIUMTEXT NULL" for name in PAYLOAD_COLUMNS)
    return (f"CREATE TEMPORARY TABLE {HYDRATION_TABLE} ("
            f"article_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,\n{columns}"
            ") ENGINE=InnoDB;\n").encode("ascii")


_HYDRATION_INSERT = (
    f"INSERT INTO {HYDRATION_TABLE} (article_id,"
    + ",".join(f"`{name}`" for name in PAYLOAD_COLUMNS) + ") VALUES "
).encode("ascii")
_HYDRATION_BATCH_END = (
    ";\nUPDATE ojs_articles article\n"
    f"INNER JOIN {HYDRATION_TABLE} payload ON payload.article_id = article.article_id\n"
    f"INNER JOIN {TRACKING_TABLE} retained ON retained.article_id = article.article_id\n"
    "SET " + ",\n".join(f"article.`{name}` = payload.`{name}`" for name in PAYLOAD_COLUMNS)
    + f";\nDELETE retained FROM {TRACKING_TABLE} retained\n"
    f"INNER JOIN {HYDRATION_TABLE} payload ON payload.article_id = retained.article_id\n"
    "INNER JOIN ojs_articles article ON article.article_id = retained.article_id\n"
    "WHERE " + " AND\n".join(
        f"(CAST(article.`{name}` AS BINARY) <=> CAST(payload.`{name}` AS BINARY))"
        for name in PAYLOAD_COLUMNS
    ) + ";\n"
    f"TRUNCATE TABLE {HYDRATION_TABLE};\n"
).encode("ascii")


class BaselinePayloadTransform:
    """Transform bounded readline fragments, retaining at most one short token.

    Defer mode preserves all SQL except the sixteen payload values in article
    tuples. Hydrate mode emits only payload batches, each limited to 100 rows or
    8 MiB plus at most one original row; even that row is streamed, not buffered.
    """

    def __init__(self, *, hydrate: bool = False, batch_rows: int = 100,
                 batch_bytes: int = 8 * 1024 * 1024) -> None:
        if batch_rows <= 0 or batch_bytes <= 0:
            raise DeferredPayloadError("hydration batch limits must be positive")
        self.hydrate = hydrate
        self.batch_rows_limit = batch_rows
        self.batch_bytes_limit = batch_bytes
        self.batch_rows = 0
        self.batch_bytes = 0
        self.rows = 0
        self.line_start = True
        self.boundary = _SQLBoundary()
        self.in_ddl = False
        self.ddl_complete = False
        self.ddl_bytes = 0
        self.columns: list[str] = []
        self.state: str | None = None
        self.column = 0
        self.value_started = False
        self.quoted = False
        self.quote_pending = False
        self.escape = False
        self.token = bytearray()
        self.output: list[bytes] = []

    def _emit(self, data: bytes) -> None:
        self.output.append(data)
        if self.hydrate:
            self.batch_bytes += len(data)

    @property
    def keep_value(self) -> bool:
        return ((self.column == 0 or self.column in _PAYLOAD_POSITIONS)
                if self.hydrate else self.column not in _PAYLOAD_POSITIONS)

    def _emit_value(self, data: bytes) -> None:
        if self.keep_value:
            self._emit(data)

    def _begin_value(self, quoted: bool) -> None:
        self.value_started = True
        self.quoted = quoted
        self.quote_pending = False
        self.escape = False
        self.token.clear()
        if not self.hydrate and not self.keep_value:
            self._emit(b"NULL")
        if self.hydrate and self.keep_value and self.column != 0:
            self._emit(b",")
        if quoted:
            if self.column == 0:
                raise DeferredPayloadError("baseline article ID must be an unsigned integer")
            self._emit_value(b"'")

    def _end_value(self) -> None:
        if not self.value_started:
            raise DeferredPayloadError("empty baseline article value")
        if not self.quoted:
            token = bytes(self.token)
            if not _UNQUOTED_VALUE.fullmatch(token):
                raise DeferredPayloadError("unsupported baseline article value token")
            if self.column == 0 and (not token.isdigit() or len(token) > 20
                                     or int(token) >= 2**64):
                raise DeferredPayloadError("baseline article ID must be an unsigned integer")
        self.value_started = False
        self.quoted = False
        self.quote_pending = False
        self.escape = False
        self.token.clear()

    def _flush_batch(self) -> None:
        if self.hydrate and self.batch_rows:
            self._emit(_HYDRATION_BATCH_END)
            self.batch_rows = 0
            self.batch_bytes = 0

    def _values(self, data: bytes) -> None:
        position = 0
        while position < len(data):
            byte = data[position:position + 1]
            if self.state == "row":
                if byte.isspace():
                    if not self.hydrate:
                        self._emit(byte)
                    position += 1
                    continue
                if byte != b"(":
                    raise DeferredPayloadError("expected a baseline article tuple")
                if self.hydrate:
                    self._emit(_HYDRATION_INSERT if self.batch_rows == 0 else b",")
                self._emit(b"(")
                self.column = 0
                self.state = "value"
                position += 1
            elif self.state == "value":
                if not self.value_started:
                    if byte.isspace():
                        if not self.hydrate:
                            self._emit_value(byte)
                        position += 1
                        continue
                    self._begin_value(byte == b"'")
                    if self.quoted:
                        position += 1
                        continue
                if self.quoted:
                    if self.quote_pending:
                        if byte == b"'":
                            self._emit_value(byte)
                            self.quote_pending = False
                            position += 1
                            continue
                        self.state = "delimiter"
                        continue
                    if self.escape:
                        self._emit_value(byte)
                        self.escape = False
                        position += 1
                        continue
                    special = _QUOTED_SPECIAL.search(data, position)
                    end = len(data) if special is None else special.end()
                    self._emit_value(data[position:end])
                    position = end
                    if special is not None:
                        self.escape = special.group() == b"\\"
                        self.quote_pending = not self.escape
                else:
                    end_match = _UNQUOTED_END.search(data, position)
                    end = len(data) if end_match is None else end_match.start()
                    token = data[position:end]
                    self.token.extend(token)
                    if len(self.token) > 4096:
                        raise DeferredPayloadError("baseline article scalar token exceeds the safe limit")
                    self._emit_value(token)
                    position = end
                    if end_match is not None:
                        self.state = "delimiter"
            elif self.state == "delimiter":
                if byte.isspace():
                    if not self.hydrate:
                        self._emit_value(byte)
                    position += 1
                    continue
                if byte not in (b",", b")"):
                    raise DeferredPayloadError("unsupported baseline article value separator")
                self._end_value()
                if byte == b",":
                    self.column += 1
                    if self.column >= len(ARTICLE_COLUMNS):
                        raise DeferredPayloadError("baseline article tuple has too many columns")
                    if not self.hydrate:
                        self._emit(b",")
                    self.state = "value"
                else:
                    if self.column != len(ARTICLE_COLUMNS) - 1:
                        raise DeferredPayloadError("baseline article tuple has missing columns")
                    self._emit(b")")
                    self.rows += 1
                    if self.hydrate:
                        self.batch_rows += 1
                        if self.batch_rows >= self.batch_rows_limit or self.batch_bytes >= self.batch_bytes_limit:
                            self._flush_batch()
                    self.state = "separator"
                position += 1
            elif self.state == "separator":
                if byte.isspace():
                    if not self.hydrate:
                        self._emit(byte)
                    position += 1
                    continue
                if byte == b",":
                    self.state = "row"
                elif byte == b";":
                    self.state = None
                else:
                    raise DeferredPayloadError("unsupported baseline article tuple separator")
                if not self.hydrate:
                    self._emit(byte)
                position += 1
            else:
                # Canonical dumps end the INSERT's physical line here. Refuse
                # appended SQL rather than reinterpret an unfamiliar export.
                remainder = data[position:]
                if remainder.strip():
                    raise DeferredPayloadError("unexpected SQL after baseline article INSERT")
                if not self.hydrate:
                    self._emit(remainder)
                return

    def transform(self, chunk: bytes) -> bytes:
        self.output = []
        at_start = self.line_start
        self.line_start = chunk.endswith(b"\n")
        if (at_start and self.boundary.start
                and chunk.rstrip(b"\r\n") == b"CREATE TABLE `ojs_articles` ("):
            if self.in_ddl or self.ddl_complete:
                raise DeferredPayloadError("duplicate baseline article schema")
            self.in_ddl = True
        if self.in_ddl:
            self.ddl_bytes += len(chunk)
            if self.ddl_bytes > 256 * 1024:
                raise DeferredPayloadError("baseline article schema exceeds the safe limit")
            column = re.match(rb"  `([a-z_]+)` ", chunk) if at_start else None
            if column:
                self.columns.append(column[1].decode("ascii"))
            if at_start and self.boundary.plain and chunk.startswith(b") ENGINE=InnoDB"):
                if tuple(self.columns) != ARTICLE_COLUMNS:
                    raise DeferredPayloadError("baseline article column order is unsupported")
                self.in_ddl = False
                self.ddl_complete = True
        active = self.state is not None
        if (at_start and self.boundary.start
                and re.match(rb"(?:INSERT|REPLACE|UPDATE)\b", chunk)
                and b"`ojs_articles`" in chunk[:256] and not chunk.startswith(_INSERT)):
            raise DeferredPayloadError("unsupported baseline article data statement")
        if at_start and self.boundary.start and chunk.startswith(b"INSERT INTO `ojs_articles`"):
            if not self.ddl_complete or not chunk.startswith(_INSERT):
                raise DeferredPayloadError("unsupported baseline article INSERT declaration")
            self.state = "row"
            if not self.hydrate:
                self._emit(_INSERT)
            self._values(chunk[len(_INSERT):])
        elif active:
            self._values(chunk)
        elif not self.hydrate:
            self._emit(chunk)
        self.boundary.feed(chunk)
        return b"".join(self.output)

    def finish(self) -> bytes:
        self.output = []
        if (not self.ddl_complete or self.in_ddl or self.state is not None
                or self.boundary.quote is not None or self.boundary.comment == "block"
                or self.boundary.statement or self.boundary.tail):
            raise DeferredPayloadError("incomplete or unsupported baseline article export")
        self._flush_batch()
        if self.hydrate:
            self._emit(f"DROP TEMPORARY TABLE {HYDRATION_TABLE};\n".encode("ascii"))
        return b"".join(self.output)
