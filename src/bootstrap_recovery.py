"""Audited bootstrap replay from touched keys, retaining completed key work.

The caller holds the source/pipeline locks, verifies the original archive and
SQL checksums, and waits for rollback before calling the read-only validator.
Rendering is deliberately separate: its SQL may run only after validation.
"""
from __future__ import annotations

import hashlib
import re

import recovery_checks as recovery
from progress_logging import Progress


RECOVERY_PROFILE = "bootstrap-touched-direct-keys-v1"
# This one-off recovery supports the reviewed original compact program only.
# A new SQL shape needs another review, even if familiar stage markers survive.
_SUPPORTED_SUFFIX_SHA256 = "1886bacfd5169fc5aba73c6e5c3cfb7a024527a9a6c3fe22cc5a7b986464208c"
_KEY_TYPES = ("doi", "fingerprint", "oai", "url")
_RETAINED = frozenset({"ojs_stage_keys", "ojs_stage_effective_keys", "ojs_stage_noisy_keys"})
_OPTIONAL = frozenset({"ojs_stage_touched_keys"})
_ENUM = "enum('doi','fingerprint','oai','url')"
_EXPECTED_COLUMNS = {
    "ojs_stage_keys": {
        "source_record_id": ("bigint unsigned", "NO"),
        "key_type": (_ENUM, "NO"), "key_hash": ("binary(32)", "NO"),
    },
    "ojs_stage_effective_keys": {
        "source_record_id": ("bigint unsigned", "NO"),
        "key_type": (_ENUM, "NO"), "key_hash": ("binary(32)", "NO"),
        "article_id": ("bigint unsigned", "YES"), "is_present": ("tinyint(1)", "NO"),
    },
    "ojs_stage_noisy_keys": {
        "key_type": (_ENUM, "NO"), "key_hash": ("binary(32)", "NO"),
    },
}


def _fail(message: str) -> None:
    raise recovery._pipeline().PipelineError(f"bootstrap touched-key recovery refused: {message}")


def _schema(server, database: str, password: str | None) -> None:
    """Prove uniqueness and the covering index used by the membership audit."""
    names = ", ".join(f"'{name}'" for name in sorted(_RETAINED))
    output = recovery._query(server, f"""
        SELECT table_name, column_name, column_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name IN ({names})
        ORDER BY table_name, ordinal_position;
        """, database, password, "recovery-key-columns")
    columns = {name: {} for name in _RETAINED}
    for row in output.splitlines():
        fields = row.split("\t")
        if len(fields) != 4 or fields[0] not in columns or fields[1] in columns[fields[0]]:
            _fail("invalid retained key column inventory")
        table, column, kind, nullable = fields
        kind = re.sub(r"^bigint\(20\)", "bigint", kind.lower())
        columns[table][column] = (kind, nullable)
    if columns != _EXPECTED_COLUMNS:
        _fail("retained key tables do not have the compact bootstrap schema")
    output = recovery._query(server, f"""
        SELECT table_name, index_name, non_unique, seq_in_index, column_name,
               COALESCE(sub_part, 0)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND table_name IN ({names})
        ORDER BY table_name, index_name, seq_in_index;
        """, database, password, "recovery-key-indexes")
    indexes = {}
    for row in output.splitlines():
        fields = row.split("\t")
        if len(fields) != 6 or fields[0] not in _RETAINED:
            _fail("invalid retained key index inventory")
        table, name, non_unique, sequence, column, prefix = fields
        parts = indexes.setdefault((table, name), [])
        if sequence != str(len(parts) + 1) or non_unique not in ("0", "1") or prefix != "0":
            _fail("retained key indexes are incomplete or prefix indexes")
        parts.append((column, non_unique))
    for table in _RETAINED:
        key = ("key_type", "key_hash") if table.endswith("noisy_keys") else ("source_record_id", "key_type")
        if indexes.get((table, "PRIMARY")) != [(column, "0") for column in key]:
            _fail("retained key primary-key uniqueness is unproved")
    if indexes.get(("ojs_stage_effective_keys", "idx_ojs_effective_keys_lookup")) != [
        (column, "1") for column in ("key_type", "key_hash", "article_id", "source_record_id")
    ]:
        _fail("effective-key covering index is missing or changed")
    triggers = recovery._numbers(recovery._query(server, """
        SELECT COUNT(*) FROM information_schema.triggers
        WHERE trigger_schema = DATABASE();
        """, database, password, "recovery-trigger-inventory"), 1, "trigger inventory")[0]
    if triggers:
        _fail("unexpected database triggers exist")


def _prove_noisy_keys(server, database: str, password: str | None, progress) -> int:
    """Aggregate covering-index ranges; never fetch effective clustered rows.

    The preceding PK audit proves every membership is present. Omitting that
    redundant predicate makes this query covering. Only above-threshold groups
    enter the derived result, so individual groups do not cross bucket bounds.
    """
    actual = recovery._numbers(recovery._query(server,
        "SELECT COUNT(*) FROM ojs_stage_noisy_keys FORCE INDEX (PRIMARY);",
        database, password, "recovery-noisy-count"), 1, "noisy key count")[0]
    expected = 0
    for kind in _KEY_TYPES:
        threshold = 25 if kind == "fingerprint" else 1000
        for bucket in range(16):
            lower = f"{bucket * 16:02x}" + "00" * 31
            predicate = f"key_type = '{kind}' AND key_hash >= UNHEX('{lower}')"
            if bucket < 15:
                upper = f"{(bucket + 1) * 16:02x}" + "00" * 31
                predicate += f" AND key_hash < UNHEX('{upper}')"
            count, missing = recovery._numbers(recovery._query(server, f"""
                SELECT COUNT(*), COALESCE(SUM(noisy.key_hash IS NULL), 0)
                FROM (
                    SELECT key_type, key_hash
                    FROM ojs_stage_effective_keys FORCE INDEX (idx_ojs_effective_keys_lookup)
                    WHERE {predicate}
                    GROUP BY key_type, key_hash
                    HAVING COUNT(*) > {threshold}
                ) expected
                LEFT JOIN ojs_stage_noisy_keys noisy
                    ON noisy.key_type = expected.key_type AND noisy.key_hash = expected.key_hash;
                """, database, password, "recovery-noisy-range"), 2, "noisy key range")
            if missing:
                _fail("retained noisy keys omit an over-threshold key")
            expected += count
            progress.event("noisy-range-verified", checked_rows=expected)
    if actual != expected:
        _fail("retained noisy keys include an incorrect key")
    return expected


def validate_bootstrap_touched_keys(
    server, *, database: str, password: str | None, snapshot_version: str,
    source_sha256: str, build_sql_sha256: str, checkpoint: dict,
    expected_checkpoint: dict | None = None, progress_seconds: float = 30,
    range_count: int = 32,
) -> dict:
    """Fully audit retained metadata and keys without modifying the database.

    Missing exact memberships are permitted only for verified noisy groups:
    the last touched-stage DELETE might already have committed. Arbitrary
    partial effective/noisy tables and every later phase are refused.
    """
    # The shared audit remains strict for the original exact-key public API.
    # This call performs the complete XML, identity and source-coverage audit;
    # it does not trust old journal messages or an unbound cached receipt.
    audit = recovery._validate_bootstrap_metadata(
        server, database=database, password=password,
        snapshot_version=snapshot_version, source_sha256=source_sha256,
        build_sql_sha256=build_sql_sha256, checkpoint=checkpoint,
        expected_checkpoint=expected_checkpoint, progress_seconds=progress_seconds,
        range_count=range_count, additional_required_tables=_RETAINED,
        additional_optional_tables=_OPTIONAL,
    )
    _schema(server, database, password)
    counts = {}
    for table in ("ojs_stage_keys", "ojs_stage_effective_keys"):
        counts[table] = recovery._numbers(recovery._query(server,
            f"SELECT COUNT(*) FROM {table} FORCE INDEX (PRIMARY);",
            database, password, "recovery-retained-key-count"), 1, "retained key count")[0]

    expected_total = checked_total = missing_total = 0
    ranges = audit["metadata_ranges"]
    expected_hash = "CASE effective.key_type " + " ".join(
        f"WHEN '{kind}' THEN staged.{digest}" for kind, _, digest in recovery._KEY_FIELDS
    ) + " END"
    with Progress("recovery-key-audit", interval=progress_seconds) as progress:
        for position, entry in enumerate(ranges, 1):
            lower, upper = entry["min_id"], entry["max_id"]
            expected = recovery._numbers(recovery._query(server, f"""
                SELECT COALESCE(SUM((oai_key_hash IS NOT NULL) + (doi_key_hash IS NOT NULL)
                    + (url_key_hash IS NOT NULL) + (fingerprint_key_hash IS NOT NULL)), 0)
                FROM ojs_stage_sources FORCE INDEX (PRIMARY)
                WHERE source_record_id BETWEEN {lower} AND {upper};
                """, database, password, "recovery-expected-key-range"), 1, "expected key range")[0]
            result = recovery._numbers(recovery._query(server, f"""
                SELECT COUNT(*),
                    COALESCE(SUM(staged.source_record_id IS NULL OR
                        NOT (effective.key_hash <=> {expected_hash})), 0),
                    COALESCE(SUM(effective.article_id IS NOT NULL OR effective.is_present <> 1), 0),
                    COALESCE(SUM(exact_key.source_record_id IS NULL AND noisy.key_hash IS NULL), 0),
                    COALESCE(SUM(exact_key.source_record_id IS NOT NULL AND
                        NOT (exact_key.key_hash <=> effective.key_hash)), 0),
                    COALESCE(SUM(exact_key.source_record_id IS NULL), 0)
                FROM ojs_stage_effective_keys effective FORCE INDEX (PRIMARY)
                LEFT JOIN ojs_stage_sources staged ON staged.source_record_id = effective.source_record_id
                LEFT JOIN ojs_stage_keys exact_key ON exact_key.source_record_id = effective.source_record_id
                    AND exact_key.key_type = effective.key_type
                LEFT JOIN ojs_stage_noisy_keys noisy ON noisy.key_type = effective.key_type
                    AND noisy.key_hash = effective.key_hash
                WHERE effective.source_record_id BETWEEN {lower} AND {upper};
                """, database, password, "recovery-retained-key-range"), 6, "retained key range")
            checked, invalid_hash, invalid_state, missing_non_noisy, wrong_exact, missing = result
            if any((invalid_hash, invalid_state, missing_non_noisy, wrong_exact)) or checked != expected:
                _fail(f"retained key range {position} failed membership, hash or bootstrap-state checks")
            expected_total += expected
            checked_total += checked
            missing_total += missing
            progress.event("key-range-verified", completed_ranges=position,
                           total_ranges=len(ranges), checked_rows=checked_total)
        if checked_total != counts["ojs_stage_effective_keys"]:
            _fail("effective keys include rows outside the audited source ranges")
        if counts["ojs_stage_keys"] != checked_total - missing_total:
            _fail("exact keys contain extra memberships")
        noisy_count = _prove_noisy_keys(server, database, password, progress)
    recovery._quiescent(server, database, password)
    audit.update({
        "recovery_boundary": "bootstrap_touched_keys", "recovery_profile": RECOVERY_PROFILE,
        "original_build_sql_sha256": build_sql_sha256,
        "effective_key_rows": checked_total, "expected_key_rows": expected_total,
        "exact_key_rows": counts["ojs_stage_keys"], "already_removed_noisy_memberships": missing_total,
        "noisy_key_groups": noisy_count, "retained_keys_verified": True,
    })
    return audit


def _anchor(stage: str) -> str:
    return f"SELECT 'OJS_PROGRESS_V1', 'stage', '{stage}';\n"


def _once(text: str, fragment: str, label: str) -> None:
    if text.count(fragment) != 1:
        _fail(f"unsupported SQL shape for {label}")


def render_bootstrap_touched_recovery(rendered_suffix: str) -> str:
    """Render only the audited compact bootstrap suffix from touched keys.

    Original SQL provenance must remain unchanged. Record the hash of this
    result separately as recovery_sql_sha256, alongside RECOVERY_PROFILE.
    """
    if not isinstance(rendered_suffix, str):
        _fail("SQL suffix is not text")
    if recovery_sql_sha256(rendered_suffix) != _SUPPORTED_SUFFIX_SHA256:
        _fail("SQL suffix differs from the reviewed compact bootstrap program")
    stages = ("exact_keys", "effective_keys", "noisy_keys", "touched_keys",
              "existing_candidates", "initial_assignments", "key_reconciliation", "article_rollup")
    for stage in stages:
        _once(rendered_suffix, _anchor(stage), stage)
    positions = [rendered_suffix.index(_anchor(stage)) for stage in stages]
    if positions != sorted(positions):
        _fail("SQL recovery stages are out of order")
    touched = rendered_suffix.split(_anchor("touched_keys"), 1)[1].split(_anchor("existing_candidates"), 1)[0]
    deletion = """DELETE k
FROM ojs_stage_keys k
INNER JOIN ojs_stage_noisy_keys noisy
    ON noisy.key_type = k.key_type
   AND noisy.key_hash = k.key_hash;
"""
    _once(touched, deletion, "noisy membership removal")
    if touched.count("INSERT INTO ojs_stage_touched_keys\n") != 3 or "retained_key_value" not in touched:
        _fail("touched SQL is not the supported compact representation")
    # Discard only the interrupted disposable result. No touched table is
    # needed when every source is new and no prior identity keys exist.
    replacement = (_anchor("touched_keys") + "DROP TABLE IF EXISTS ojs_stage_touched_keys;\n"
                   + deletion + "\n")
    after = rendered_suffix.split(_anchor("existing_candidates"), 1)[1]
    candidates, rest = after.split(_anchor("initial_assignments"), 1)
    if candidates.count("INSERT INTO ojs_stage_existing_candidates\n") != 2:
        _fail("existing-candidate SQL is not the supported bootstrap shape")
    empty_candidates = candidates.split("INSERT INTO ojs_stage_existing_candidates\n", 1)[0]
    _once(empty_candidates, "CREATE TABLE ojs_stage_existing_candidates (", "candidate table")
    result = replacement + _anchor("existing_candidates") + empty_candidates + _anchor("initial_assignments") + rest

    before, reconciliation = result.split(_anchor("key_reconciliation"), 1)
    reconcile, after = reconciliation.split(_anchor("article_rollup"), 1)
    # Preserve the committed source->membership update and the compact early
    # reclamation tail. Replace only the prior-key deletion and key INSERT.
    original_delete = "DELETE identity_key\nFROM ojs_article_keys identity_key\n"
    _once(reconcile, original_delete, "prior key deletion")
    update, insertion = reconcile.split(original_delete, 1)
    cleanup = "-- Compact storage: reclaim completed work tables.\n"
    _once(insertion, cleanup, "key work-table reclamation")
    original_insert, cleanup_tail = insertion.split(cleanup, 1)
    if (original_insert.count("INSERT INTO ojs_article_keys (") != 1
            or original_insert.count("touched.retained_key_value") != 2
            or original_insert.count("HAVING COUNT(DISTINCT effective.article_id) = 1") != 1):
        _fail("key reconciliation is not the supported compact representation")
    value = """CASE effective.key_type
            WHEN 'oai' THEN staged.oai_key_value
            WHEN 'doi' THEN staged.doi
            WHEN 'url' THEN staged.url_key_value
            WHEN 'fingerprint' THEN staged.fingerprint_key_value
        END"""
    new_insert = f"""INSERT INTO ojs_article_keys (key_type, key_hash, key_value, article_id, date_added)
SELECT effective.key_type, effective.key_hash, MAX({value}),
       MIN(effective.article_id), @ojs_snapshot_date
FROM ojs_stage_effective_keys effective
INNER JOIN ojs_stage_sources staged ON staged.source_record_id = effective.source_record_id
LEFT JOIN ojs_stage_noisy_keys noisy ON noisy.key_type = effective.key_type
    AND noisy.key_hash = effective.key_hash
WHERE effective.article_id IS NOT NULL AND noisy.key_hash IS NULL
GROUP BY effective.key_type, effective.key_hash
HAVING COUNT(DISTINCT effective.article_id) = 1 AND MAX({value}) IS NOT NULL;

"""
    result = (before + _anchor("key_reconciliation") + update + new_insert
              + cleanup + cleanup_tail + _anchor("article_rollup") + after)
    # Every remaining touched reference must be a harmless cleanup of the
    # absent table; reject new consumers instead of silently changing meaning.
    remaining = result.replace("DROP TABLE IF EXISTS ojs_stage_touched_keys;", "")
    if re.search(r"\bojs_stage_touched_keys\b|\btouched\.", remaining):
        _fail("unexpected later touched-key consumer")
    return result


def recovery_sql_sha256(rendered_suffix: str) -> str:
    """Checksum the executed recovery program without replacing original provenance."""
    return hashlib.sha256(rendered_suffix.encode("utf-8")).hexdigest()
