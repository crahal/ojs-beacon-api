"""Durable, opt-in retirement of a redundant predecessor export.

The old live database remains rollback authority. A complete, validated new
database and self-contained export-ready checkpoint must exist before its old
input archive may be removed. No database directory is ever deleted here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from datetime import date

from mount_safety import assert_cleanup_unmounted
from compact_storage import COMPACT_STORAGE_PROFILE
from deferred_payload import DEFERRED_PAYLOAD_PROFILE
from metadata_storage import METADATA_STORAGE_PROFILE
from storage_layout import recorded_layout_audit_options, StorageLayoutError


PROFILE = "validated-export-handoff-v1"
STATE_NAME = "OJS_PIPELINE_STATE.json"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"[0-9a-f]{64}")
GIB = 1024 ** 3


class ExportHandoffError(ValueError):
    pass


def retirement_enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get("OJS_RETIRE_BASELINE_EXPORT", "0")
    if value not in ("0", "1"):
        raise ExportHandoffError("OJS_RETIRE_BASELINE_EXPORT must be exactly 0 or 1")
    return value == "1"


def _date(value):
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise ExportHandoffError("invalid export handoff snapshot date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ExportHandoffError("invalid export handoff calendar date") from exc
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ExportHandoffError("invalid export handoff checksum")
    return value


def canonical_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def document_sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _regular(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ExportHandoffError(f"expected a single-link regular handoff artifact: {path.name}")
    return info


def _document(path: Path):
    return _document_and_sha(path)[0]


def _object(value, label):
    if not isinstance(value, dict):
        raise ExportHandoffError(f"export handoff {label} must be an object")
    return value


def _document_and_sha(path: Path):
    payload = _read_file(path, 1024 * 1024)
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ExportHandoffError("export handoff document contains duplicate keys")
            value[key] = item
        return value
    def invalid_constant(value):
        raise ExportHandoffError("export handoff document contains a non-finite number")
    try:
        value = json.loads(payload, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExportHandoffError("export handoff document is invalid") from exc
    return _object(value, "document"), hashlib.sha256(payload).hexdigest()


read_checkpoint_document = _document


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    _read_file(path, None, digest)
    return digest.hexdigest()


regular_file_sha256 = _file_sha


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(path: Path, limit: int | None, digest=None) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ExportHandoffError("handoff artifact must be a regular, single-link file")
        if limit is not None and before.st_size > limit:
            raise ExportHandoffError("export handoff document exceeds safe size")
        result = bytearray()
        while chunk := os.read(descriptor, 8 * 1024 * 1024 if limit is None else limit + 1):
            if digest is None:
                result.extend(chunk)
                if limit is not None and len(result) > limit:
                    raise ExportHandoffError("export handoff document grew beyond safe size")
            else:
                digest.update(chunk)
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(before) != _identity(_regular(path)):
            raise ExportHandoffError("handoff artifact changed while being verified")
        return bytes(result)
    finally:
        os.close(descriptor)


def _declared(path: Path) -> str:
    sidecar = path.with_name(path.name + ".sha256")
    fields = _read_file(sidecar, 4096).decode("ascii").split()
    if len(fields) != 2 or fields[1].lstrip("*") != path.name:
        raise ExportHandoffError("export checksum filename does not match")
    return _sha(fields[0])


def _sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_handoff_artifacts(clean_dir: Path, final_dir: Path, paths: tuple[Path, ...]) -> None:
    """Make successor artifacts durable before retiring their recovery state."""
    for path in paths:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ExportHandoffError("successor artifact must be a single-link regular file")
            os.fsync(descriptor)
            if _identity(before) != _identity(_regular(path)):
                raise ExportHandoffError("successor artifact changed during durability check")
        finally:
            os.close(descriptor)
    _sync_directory(final_dir)
    _sync_directory(clean_dir)


def atomic_document(path: Path, value):
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ExportHandoffError("export handoff directory must be an existing non-symlink directory")
    if path.exists() or path.is_symlink():
        _regular(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".export-handoff-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_bytes(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def retirement_receipt_path(clean_dir: Path, version: str) -> Path:
    return clean_dir / f"pkpbeacon-export-retirement-{_date(version)}.json"


def _baseline_artifacts(clean_dir: Path, version: str) -> dict:
    version = _date(version)
    export = clean_dir / f"pkpbeacon-clean-{version}.sql.gz"
    report_path = clean_dir / f"pkpbeacon-changes-{version}.json"
    manifest_path = clean_dir / f"pkpbeacon-release-{version}.json"
    report, report_sha = _document_and_sha(report_path)
    manifest, manifest_sha = _document_and_sha(manifest_path)
    export_sha = _declared(export)
    if (_declared(report_path) != report_sha
            or manifest.get("schema_version") != 1
            or manifest.get("snapshot_date") != version
            or manifest.get("clean_export_filename") != export.name
            or manifest.get("clean_export_sha256") != export_sha
            or manifest.get("change_report_filename") != report_path.name
            or manifest.get("change_report_sha256") != report_sha):
        raise ExportHandoffError("baseline manifest/report/checksum bindings disagree")
    if _object(report.get("release_guard"), "baseline release guard").get("status") not in ("passed", "overridden"):
        raise ExportHandoffError("baseline release guard has not passed")
    snapshot = _object(report.get("snapshot"), "baseline snapshot")
    if snapshot.get("date") != version:
        raise ExportHandoffError("baseline report snapshot does not match")
    rollback = clean_dir / f"mysql-{version}"
    if rollback.is_symlink() or not rollback.is_dir():
        raise ExportHandoffError("baseline rollback database is unavailable")
    marker = _document(rollback / "OJS_COMPACT_RELEASE.json")
    expected = {"schema_version": 1, "snapshot_date": version, "clean_only": True,
                "data_directory_name": rollback.name,
                "source_sha256": _sha(snapshot.get("source_sha256")),
                "build_sql_sha256": _sha(snapshot.get("build_sql_sha256"))}
    if any(marker.get(key) != value for key, value in expected.items()):
        raise ExportHandoffError("baseline rollback ownership/provenance does not match")
    return {"snapshot_date": version, "export_filename": export.name,
            "export_sha256": export_sha, "report_sha256": report_sha,
            "manifest_sha256": manifest_sha, "rollback_directory": rollback.name}


def _live_matches(clean_dir: Path, baseline: dict):
    live = clean_dir / "mysql-live"
    if not live.is_symlink() or live.resolve(strict=True) != (clean_dir / baseline["rollback_directory"]).resolve():
        raise ExportHandoffError("baseline retirement requires the unchanged live rollback database")


def preflight_retirement(clean_dir: Path, baseline_version: str, *,
                         expected_export_sha256: str | None = None, env=None) -> dict:
    """Small admission checks only; full archive hash is checked at retirement."""
    env = os.environ if env is None else env
    baseline = _baseline_artifacts(clean_dir, baseline_version)
    _live_matches(clean_dir, baseline)
    export = clean_dir / baseline["export_filename"]
    baseline["export_size_bytes"] = _regular(export).st_size
    if baseline["export_size_bytes"] <= 0:
        raise ExportHandoffError("baseline export is empty")
    if expected_export_sha256 is not None and baseline["export_sha256"] != expected_export_sha256:
        raise ExportHandoffError("baseline export differs from the deferred payload binding")
    receipt_path = env.get("OJS_BASELINE_EXPORT_BACKUP_RECEIPT", "")
    if receipt_path:
        path = Path(receipt_path)
        if not path.is_absolute():
            raise ExportHandoffError("backup receipt requires an absolute private path")
        info = _regular(path)
        if info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise ExportHandoffError("backup receipt must be private and operator owned")
        receipt, receipt_sha = _document_and_sha(path)
        if _identity(info) != _identity(_regular(path)):
            raise ExportHandoffError("backup receipt changed during verification")
        expected = {"schema_version": 1, "snapshot_date": baseline_version,
                    "export_sha256": baseline["export_sha256"],
                    "export_size_bytes": baseline["export_size_bytes"],
                    "verified_elsewhere": True}
        if (any(receipt.get(key) != value for key, value in expected.items())
                or not isinstance(receipt.get("verified_at"), str) or not receipt["verified_at"]
                or not isinstance(receipt.get("backup_location"), str) or not receipt["backup_location"]):
            raise ExportHandoffError("verified external backup receipt does not match baseline")
        baseline["backup_receipt_sha256"] = receipt_sha
    return baseline


def prepare_export_ready(staging_dir: Path, state: dict, *, report: dict,
                         baseline: dict, mysql_version: str, event_auto_increment: int) -> dict:
    ready = dict(state)
    ready["phase"] = "export_ready"
    ready["export_handoff"] = {"profile": PROFILE, "baseline": baseline, "report": report,
                               "report_sha256": document_sha256(report),
                               "mysql_version": mysql_version,
                               "event_auto_increment": event_auto_increment}
    _validate_ready(ready, staging_dir.name)
    atomic_document(staging_dir / STATE_NAME, ready)
    return ready


def _validate_ready(state: dict, directory_name: str):
    _object(state, "checkpoint")
    handoff = _object(state.get("export_handoff"), "profile")
    if state.get("phase") != "export_ready" or handoff.get("profile") != PROFILE:
        raise ExportHandoffError("candidate is not a recognized export-ready checkpoint")
    if (type(handoff.get("event_auto_increment")) is not int
            or not 1 <= handoff["event_auto_increment"] <= 18446744073709551615):
        raise ExportHandoffError("export-ready event allocator is invalid")
    version = _date(state.get("snapshot_version"))
    if directory_name not in (f"mysql-{version}.building", f"mysql-{version}"):
        raise ExportHandoffError("export-ready candidate directory identity does not match")
    report = handoff.get("report")
    if not isinstance(report, dict) or document_sha256(report) != handoff.get("report_sha256"):
        raise ExportHandoffError("export-ready report checksum does not match")
    snapshot = _object(report.get("snapshot"), "snapshot")
    if (state.get("pipeline_version") != "3" or report.get("pipeline_version") != "3"
            or report.get("schema_version") != 1
            or state.get("metadata_storage_profile") != METADATA_STORAGE_PROFILE
            or state.get("baseline_payload_profile") != DEFERRED_PAYLOAD_PROFILE
            or type(state.get("source_size_bytes")) is not int or state["source_size_bytes"] <= 0
            or state.get("source_filename") not in (f"pkpbeacon-{version}.sql", f"pkpbeacon-{version}.sql.gz")
            or not isinstance(handoff.get("mysql_version"), str)
            or not handoff["mysql_version"].startswith("8.4.")):
        raise ExportHandoffError("export-ready pipeline/source/storage profile is unsupported")
    expected = {"date": version, "source_filename": state.get("source_filename"),
                "source_size_bytes": state.get("source_size_bytes"),
                "source_sha256": _sha(state.get("source_sha256")),
                "build_sql_sha256": _sha(state.get("build_sql_sha256"))}
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise ExportHandoffError("export-ready report and checkpoint provenance disagree")
    if _object(report.get("release_guard"), "release guard").get("status") not in ("passed", "overridden"):
        raise ExportHandoffError("export-ready release guard has not passed")
    baseline = _object(handoff.get("baseline"), "baseline")
    previous = _date(baseline.get("snapshot_date"))
    if previous >= version or baseline.get("export_filename") != f"pkpbeacon-clean-{previous}.sql.gz":
        raise ExportHandoffError("export-ready baseline identity is invalid")
    if baseline.get("rollback_directory") != f"mysql-{previous}":
        raise ExportHandoffError("export-ready rollback directory is invalid")
    if snapshot.get("previous_snapshot_date") != previous:
        raise ExportHandoffError("export-ready temporal predecessor does not match retirement plan")
    for name in ("export_sha256", "report_sha256", "manifest_sha256"):
        _sha(baseline.get(name))
    if type(baseline.get("export_size_bytes")) is not int or baseline["export_size_bytes"] <= 0:
        raise ExportHandoffError("export-ready baseline size is invalid")
    storage = _object(report.get("storage"), "storage")
    if storage.get("profile") != COMPACT_STORAGE_PROFILE:
        raise ExportHandoffError("export-ready compact storage profile is unsupported")
    _sha(storage.get("rendered_build_sql_sha256"))
    try:
        recorded_layout_audit_options(storage)
    except StorageLayoutError as exc:
        raise ExportHandoffError(str(exc)) from exc
    deferred = _object(storage.get("deferred_baseline_payload"), "hydration audit")
    for name in ("storage_layout_profile", "event_history_profile"):
        if storage.get(name) != state.get(name):
            raise ExportHandoffError("export-ready physical profiles disagree")
    if (state.get("baseline_export_sha256") != baseline["export_sha256"]
            or deferred.get("baseline_export_sha256") != baseline["export_sha256"]
            or deferred.get("profile") != state.get("baseline_payload_profile")
            or deferred.get("baseline_export_filename") != baseline["export_filename"]):
        raise ExportHandoffError("export-ready hydrated baseline binding does not match")
    for name in ("baseline_articles", "hydrated_articles"):
        if type(deferred.get(name)) is not int or deferred[name] < 0:
            raise ExportHandoffError("export-ready hydration counts are invalid")
    if deferred["hydrated_articles"] > deferred["baseline_articles"]:
        raise ExportHandoffError("export-ready hydration count exceeds predecessor articles")
    for section, fields in (("source_rows", ("retained_total", "active")),
                            ("articles", ("retained_total", "active", "removed_tombstones", "merged_tombstones")),
                            ("article_events", ("total",))):
        values = _object(report.get(section), section)
        if any(type(values.get(name)) is not int or values[name] < 0 for name in fields):
            raise ExportHandoffError("export-ready report counts are invalid")
    if "backup_receipt_sha256" in baseline:
        _sha(baseline["backup_receipt_sha256"])


def validate_export_ready(staging_dir: Path) -> dict | None:
    if staging_dir.is_symlink():
        raise ExportHandoffError("refusing a symlinked export-ready candidate")
    path = staging_dir / STATE_NAME
    if not path.exists() and not path.is_symlink():
        return None
    state = _document(path)
    if state.get("phase") != "export_ready":
        return None
    _validate_ready(state, staging_dir.name)
    return state


def validate_retired_baseline(clean_dir: Path, old_version: str,
                             successor_version: str | None = None) -> dict:
    receipt = _document(retirement_receipt_path(clean_dir, old_version))
    if receipt.get("schema_version") != 1 or receipt.get("profile") != PROFILE:
        raise ExportHandoffError("unrecognized baseline export retirement receipt")
    baseline = _object(receipt.get("baseline"), "retirement baseline")
    current = _baseline_artifacts(clean_dir, old_version)
    if any(baseline.get(key) != value for key, value in current.items()):
        raise ExportHandoffError("retired baseline artifacts no longer match receipt")
    successor = _object(receipt.get("successor"), "retirement successor")
    version = _date(successor.get("snapshot_date"))
    if version <= old_version or (successor_version is not None and version != successor_version):
        raise ExportHandoffError("retirement receipt successor does not match")
    for name in ("source_sha256", "build_sql_sha256", "report_sha256"):
        _sha(successor.get(name))
    if type(baseline.get("export_size_bytes")) is not int or baseline["export_size_bytes"] <= 0:
        raise ExportHandoffError("retirement receipt size is invalid")
    return receipt


validate_retirement_receipt = validate_retired_baseline


def validate_resume_handoff(clean_dir: Path, candidate_version: str, live_version: str) -> dict:
    directory = clean_dir / f"mysql-{_date(candidate_version)}.building"
    if not directory.exists() and not directory.is_symlink():
        directory = clean_dir / f"mysql-{candidate_version}"
    state = validate_export_ready(directory)
    if state is None or state["export_handoff"]["baseline"]["snapshot_date"] != live_version:
        raise ExportHandoffError("export-ready checkpoint does not match live predecessor")
    baseline = state["export_handoff"]["baseline"]
    current = _baseline_artifacts(clean_dir, live_version)
    if any(baseline.get(key) != value for key, value in current.items()):
        raise ExportHandoffError("export-ready baseline artifacts changed")
    _live_matches(clean_dir, baseline)
    export = clean_dir / baseline["export_filename"]
    if not export.exists() and not export.is_symlink():
        receipt = validate_retired_baseline(clean_dir, live_version, candidate_version)
        if receipt["baseline"] != baseline:
            raise ExportHandoffError("retirement receipt and checkpoint disagree")
        expected_successor = {"snapshot_date": candidate_version,
                              "source_sha256": state["source_sha256"],
                              "build_sql_sha256": state["build_sql_sha256"],
                              "report_sha256": state["export_handoff"]["report_sha256"]}
        if receipt["successor"] != expected_successor:
            raise ExportHandoffError("retirement receipt successor provenance differs from checkpoint")
    elif _regular(export).st_size != baseline["export_size_bytes"]:
        raise ExportHandoffError("baseline export size changed")
    return state


def retire_baseline_export(clean_dir: Path, state: dict) -> dict:
    version = state["snapshot_version"]
    baseline = state["export_handoff"]["baseline"]
    persisted = validate_resume_handoff(clean_dir, version, baseline["snapshot_date"])
    if persisted != state:
        raise ExportHandoffError("retirement request differs from the durable export-ready checkpoint")
    receipt = {"schema_version": 1, "profile": PROFILE, "baseline": baseline,
               "successor": {"snapshot_date": version,
                             "source_sha256": state["source_sha256"],
                             "build_sql_sha256": state["build_sql_sha256"],
                             "report_sha256": state["export_handoff"]["report_sha256"]}}
    receipt_path = retirement_receipt_path(clean_dir, baseline["snapshot_date"])
    if (receipt_path.exists() or receipt_path.is_symlink()) and _document(receipt_path) != receipt:
        raise ExportHandoffError("an existing retirement receipt has different bindings")
    export = clean_dir / baseline["export_filename"]
    if export.exists() or export.is_symlink():
        assert_cleanup_unmounted(export)
        before = _regular(export)
        if _file_sha(export) != baseline["export_sha256"]:
            raise ExportHandoffError("refusing retirement of a changed baseline export")
        after = _regular(export)
        if _identity(before) != _identity(after):
            raise ExportHandoffError("baseline export changed during retirement verification")
        if validate_resume_handoff(clean_dir, version, baseline["snapshot_date"]) != state:
            raise ExportHandoffError("export-ready checkpoint changed during archive verification")
        atomic_document(receipt_path, receipt)  # Durable intent precedes exact-file unlink.
        assert_cleanup_unmounted(export)
        if _identity(before) != _identity(_regular(export)):
            raise ExportHandoffError("baseline export changed before exact-file retirement")
        export.unlink()
        _sync_directory(clean_dir)
    else:
        validate_retired_baseline(clean_dir, baseline["snapshot_date"], version)
    return receipt


def export_capacity_requirements(env=None) -> dict:
    env = os.environ if env is None else env
    try:
        budget = float(env.get("OJS_BUILD_EXPORT_GB", ""))
        reserve = float(env.get("OJS_DISK_RESERVE_GB", "32"))
        if (not math.isfinite(budget) or not math.isfinite(reserve)
                or not math.isfinite((budget + reserve) * GIB) or budget <= 0 or reserve < 0):
            raise ValueError
    except (TypeError, ValueError):
        raise ExportHandoffError("export handoff requires positive OJS_BUILD_EXPORT_GB and valid disk reserve") from None
    return {"export_budget_bytes": math.ceil(budget * GIB), "reserve_bytes": math.ceil(reserve * GIB)}


def successor_export_reuse_metadata(clean_dir: Path, state: dict) -> dict | None:
    """Check bounded reuse evidence; archive content still needs runner hashing."""
    version = _date(state["snapshot_version"])
    _validate_ready(state, f"mysql-{version}")
    export = clean_dir / f"pkpbeacon-clean-{version}.sql.gz"
    report_path = clean_dir / f"pkpbeacon-changes-{version}.json"
    paths = (export, report_path, export.with_name(export.name + ".sha256"),
             report_path.with_name(report_path.name + ".sha256"))
    if not all(path.exists() or path.is_symlink() for path in paths):
        return None
    before = _regular(export)
    if before.st_size <= 0:
        raise ExportHandoffError("existing successor export is empty")
    export_sha = _declared(export)
    report, report_sha = _document_and_sha(report_path)
    if (report != state["export_handoff"]["report"]
            or report_sha != state["export_handoff"]["report_sha256"]
            or _declared(report_path) != report_sha):
        raise ExportHandoffError("existing successor export/report does not match its checkpoint")
    manifest_path = clean_dir / f"pkpbeacon-release-{version}.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        manifest = _document(manifest_path)
        expected = {"schema_version": 1, "snapshot_date": version,
                    "clean_export_filename": export.name, "clean_export_sha256": export_sha,
                    "change_report_filename": report_path.name, "change_report_sha256": report_sha}
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ExportHandoffError("existing successor manifest does not match reuse evidence")
    if _identity(before) != _identity(_regular(export)):
        raise ExportHandoffError("successor export changed while checking reuse evidence")
    return {"snapshot_date": version, "export_filename": export.name,
            "export_sha256": export_sha, "export_size_bytes": before.st_size,
            "report_sha256": report_sha, "export_identity": _identity(before)}


def reusable_successor_export(clean_dir: Path, state: dict) -> bool:
    """Authenticate an export already renamed before an interrupted commit."""
    metadata = successor_export_reuse_metadata(clean_dir, state)
    if metadata is None:
        return False
    if (_file_sha(clean_dir / metadata["export_filename"]) != metadata["export_sha256"]
            or successor_export_reuse_metadata(clean_dir, state) != metadata):
        raise ExportHandoffError("existing successor export/report does not match its checkpoint")
    return True


def require_export_capacity(clean_dir: Path, env=None, *, export_already_written: bool = False) -> dict:
    requirements = export_capacity_requirements(env)
    required = requirements["reserve_bytes"] + (0 if export_already_written else requirements["export_budget_bytes"])
    available = shutil.disk_usage(clean_dir).free
    if available < required:
        raise ExportHandoffError("insufficient export budget plus disk reserve; export-ready candidate retained")
    return {**requirements, "available_bytes": available}
