"""Bounded, read-only observations of catalogue download and processing jobs.

Only closed-vocabulary service properties and current-invocation progress events
leave this module. Raw journal messages, command lines, paths and exceptions are
never returned. A running monitor is not evidence that SQL is advancing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time


FIXED_UNITS = ("ojs-api-update.service", "ojs-november-download.service")
PUBLISH_UNIT = re.compile(r"ojs-api-publish-(\d{4}-\d{2}-\d{2})\.service\Z")
INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
MAX_PUBLISH_UNITS = 4
JOURNAL_LINES = 100
STALE_SECONDS = 300
DEADLINE_SECONDS = 20
ACTIVE_STATES = frozenset({"active", "activating", "reloading", "refreshing"})
STATUSES = frozenset({
    "processing", "downloading", "validating", "publishing", "checking",
    "preparing", "recovering", "stopping", "active_unknown", "idle", "unknown",
})
PRIORITY = (
    "processing", "recovering", "publishing", "downloading", "validating",
    "preparing", "checking", "stopping", "active_unknown", "unknown", "idle",
)
STAGE_STATUS = {
    **dict.fromkeys(("sql-import", "source-index phase", "metadata-shard",
                     "temporal finalization phase", "clean-export"), "processing"),
    **dict.fromkeys(("beacon-connect", "beacon-transfer"), "downloading"),
    **dict.fromkeys(("beacon-archive-validation", "beacon-legacy-checksum",
                     "snapshot-checksum", "file-checksum", "validate-raw-tables",
                     "validate-counts-and-identities"), "validating"),
    **dict.fromkeys(("update-promotion", "update-cold-verification",
                     "update-release-checksums", "update-api-health",
                     "update-cleanup"), "publishing"),
    **dict.fromkeys(("update-source-check", "beacon-source-check"), "checking"),
    **dict.fromkeys(("mysql-initialize", "mysql-start"), "preparing"),
    **dict.fromkeys(("recovery-rollback", "recovery-metadata-audit",
                     "update-rollback", "update-recover-exact-keys"), "recovering"),
    **dict.fromkeys(("mysql-shutdown", "mysql-shutdown-wait",
                     "update-graceful-shutdown"), "stopping"),
    "update-download": "active_unknown", "update-build": "active_unknown",
}
SUPERVISORS = frozenset({"update-download", "update-build", "update-recover-exact-keys"})
EVENTS = frozenset({"started", "heartbeat", "completed", "failed", "sql-stage",
                    "dedup-pass", "metadata-rows", "range-verified", "export-verified"})
PROPERTIES = "LoadState,ActiveState,SubState,MainPID,InvocationID"


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _datetime(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _unit(value) -> bool:
    if not isinstance(value, str):
        return False
    if value in FIXED_UNITS:
        return True
    match = PUBLISH_UNIT.fullmatch(value)
    if not match:
        return False
    try:
        return datetime.strptime(match[1], "%Y-%m-%d").date().isoformat() == match[1]
    except ValueError:
        return False


def _invocation(value) -> bool:
    return isinstance(value, str) and bool(INVOCATION.fullmatch(value)) and value != "0" * 32


def _service(output: str) -> dict:
    safe = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in safe:
            continue
        if key == "LoadState" and value in {"loaded", "not-found", "masked", "error"}:
            safe[key] = value
        elif key == "ActiveState" and value in ACTIVE_STATES | {"deactivating", "inactive", "failed"}:
            safe[key] = value
        elif key == "SubState" and value in {"exited", "running", "start", "dead", "failed"}:
            safe[key] = value
        elif key == "MainPID" and re.fullmatch(r"[0-9]{1,10}", value):
            safe[key] = int(value)
        elif key == "InvocationID" and _invocation(value):
            safe[key] = value
    return safe


def _event(output: str, invocation: str) -> dict | None:
    events = []
    for line in output.splitlines()[-JOURNAL_LINES:]:
        if len(line) > 16384:
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("_SYSTEMD_INVOCATION_ID") != invocation:
                continue
            message = entry.get("MESSAGE")
            if not isinstance(message, str) or len(message) > 4096 or not message.startswith("[progress] "):
                continue
            payload = json.loads(message[len("[progress] "):])
            if not isinstance(payload, dict):
                continue
            stage, event = payload.get("stage"), payload.get("event")
            if not isinstance(stage, str) or stage not in STAGE_STATUS or not isinstance(event, str) or event not in EVENTS:
                continue
            if stage in SUPERVISORS and event == "heartbeat":
                continue
            micros = entry.get("__REALTIME_TIMESTAMP")
            if not isinstance(micros, str) or not re.fullmatch(r"[0-9]{1,20}", micros):
                continue
            logged = datetime.fromtimestamp(int(micros) / 1_000_000, timezone.utc)
            events.append((int(micros), {"stage": stage, "event": event,
                                       "observed_at": _stamp(logged)}))
        except (ValueError, OverflowError, OSError, RecursionError):
            continue
    return max(events, key=lambda item: item[0])[1] if events else None


def _classify(unit: str, service: dict, event: dict | None, now: datetime) -> dict:
    result = {"unit": unit, "status": "unknown"}
    state = service.get("ActiveState")
    if service.get("LoadState") == "not-found" or state in {"inactive", "failed"}:
        result["status"] = "idle"
        return result
    if state == "deactivating":
        result["status"] = "stopping"
        return result
    if state not in ACTIVE_STATES:
        return result
    if state == "active" and service.get("SubState") == "exited" and service.get("MainPID") == 0:
        result["status"] = "idle"
        return result
    result["status"] = "active_unknown"
    invocation = service.get("InvocationID")
    if _invocation(invocation):
        result["invocation_id"] = invocation
    if not event or not _invocation(invocation):
        return result
    observed = _datetime(event.get("observed_at"))
    if observed is None or not 0 <= (now - observed).total_seconds() <= STALE_SECONDS:
        return result
    if event["event"] in {"completed", "failed"}:
        return result
    stage = event["stage"]
    # The standalone downloader cannot establish that a wrangler is running.
    if unit == "ojs-november-download.service" and STAGE_STATUS[stage] not in {"downloading", "validating", "checking"}:
        return result
    result.update(status=STAGE_STATUS[stage], stage=stage, observed_at=event["observed_at"])
    return result


def sanitize_activity(raw) -> dict:
    """Re-allowlist observations before reporting, persisting or sending to AI."""
    if not isinstance(raw, dict):
        return {"status": "unknown", "jobs": [], "discovery_complete": False}
    jobs, seen = [], set()
    incoming = raw.get("jobs")
    if isinstance(incoming, list):
        for job in incoming[:len(FIXED_UNITS) + MAX_PUBLISH_UNITS]:
            if not isinstance(job, dict) or not _unit(job.get("unit")) or job["unit"] in seen:
                continue
            unit, status = job["unit"], job.get("status")
            if not isinstance(status, str) or status not in STATUSES:
                status = "unknown"
            safe = {"unit": unit, "status": status}
            stage = job.get("stage")
            if isinstance(stage, str) and stage in STAGE_STATUS and STAGE_STATUS[stage] == status:
                safe["stage"] = stage
            observed = _datetime(job.get("observed_at"))
            if observed:
                safe["observed_at"] = _stamp(observed)
            if _invocation(job.get("invocation_id")):
                safe["invocation_id"] = job["invocation_id"]
            jobs.append(safe)
            seen.add(unit)
    statuses = {job["status"] for job in jobs}
    if not set(FIXED_UNITS).issubset(seen) or raw.get("discovery_complete") is not True:
        statuses.add("unknown")
    status = next((candidate for candidate in PRIORITY if candidate in statuses), "unknown")
    safe = {"status": status, "jobs": jobs, "discovery_complete": raw.get("discovery_complete") is True}
    observed = _datetime(raw.get("observed_at"))
    if observed:
        safe["observed_at"] = _stamp(observed)
    return safe


def collect_activity(*, diagnostic, observed_at: str | None = None) -> dict:
    """Inspect only known pipeline units; diagnostic must bound bytes and time.

    The reporter supplies its existing bounded subprocess reader. ``observed_at``
    is a fixed clock override for tests; live collection checks the clock again
    after each diagnostic so a newly arrived heartbeat is not deemed future.
    No subprocess
    here starts, stops or signals a pipeline; only reader processes may time out.
    """
    started = time.monotonic()
    fixed_now = _datetime(observed_at)

    def now() -> datetime:
        return fixed_now or datetime.now(timezone.utc)

    raw = {"jobs": [], "discovery_complete": False}

    def read(command: list[str], *, limit: int) -> str:
        remaining = DEADLINE_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError()
        return diagnostic(command, timeout=min(5.0, remaining), limit=limit)

    def service(unit: str) -> dict:
        return _service(read(["systemctl", "show", unit, "--no-pager", "--property=" + PROPERTIES], limit=16384))

    publishers = []
    try:
        listing = read(["systemctl", "list-units", "--type=service", "--all", "--plain",
                        "--no-legend", "--no-pager", "ojs-api-publish-????-??-??.service"], limit=16384)
        publishers = sorted({line.split()[0] for line in listing.splitlines()
                             if line.split() and _unit(line.split()[0]) and line.split()[0] not in FIXED_UNITS})
        raw["discovery_complete"] = len(publishers) <= MAX_PUBLISH_UNITS
    except Exception:
        pass  # Diagnostic failures and their private details must not escape.
    for unit in (*FIXED_UNITS, *publishers[-MAX_PUBLISH_UNITS:]):
        try:
            before = service(unit)
            event = None
            invocation = before.get("InvocationID")
            if before.get("ActiveState") in ACTIVE_STATES and _invocation(invocation):
                output = read([
                    "journalctl", "--unit=" + unit, "--no-pager", "--output=json",
                    "--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID,__REALTIME_TIMESTAMP",
                    "--lines=" + str(JOURNAL_LINES), "_SYSTEMD_INVOCATION_ID=" + invocation,
                ], limit=512 * 1024)
                event = _event(output, invocation)
                after = service(unit)
                if after.get("InvocationID") != invocation or after.get("ActiveState") != before.get("ActiveState"):
                    event = None
                before = after
            raw["jobs"].append(_classify(unit, before, event, now()))
        except Exception:
            raw["jobs"].append({"unit": unit, "status": "unknown"})
    raw["observed_at"] = _stamp(now())
    return sanitize_activity(raw)


def activity_summary(raw) -> list[str]:
    """Always provide an explicit processing status, even without observations."""
    safe = sanitize_activity(raw)
    descriptions = {
        "processing": "Processing: a monitored job is running a fresh SQL import, metadata, deduplication or export stage; completion and ETA are not inferred.",
        "recovering": "Processing: a monitored job is performing recovery work; normal wrangling progress is not established.",
        "downloading": "Processing: a source download is running; downloading is not wrangling or deduplication.",
        "validating": "Processing: source or database validation is running; this does not establish that deduplication has started.",
        "publishing": "Processing: release verification, publication or cleanup is running; API availability is checked separately.",
        "checking": "Processing: an upstream source check is running; no wrangling stage is currently confirmed.",
        "preparing": "Processing: a database build is preparing; a SQL import or deduplication stage is not yet confirmed.",
        "stopping": "Processing: a monitored job is stopping or shutting down; it may still be cleaning up or rolling back.",
        "active_unknown": "Processing: a monitored job is active, but no fresh work-stage observation establishes what it is doing.",
        "idle": "Processing: no monitored download, processing or publication job is running.",
        "unknown": "Processing: current download and wrangling activity could not be fully verified.",
    }
    lines = [descriptions[safe["status"]]]
    labels = {
        "processing": "processing", "recovering": "recovering", "downloading": "downloading",
        "validating": "validating", "publishing": "publishing", "checking": "checking upstream",
        "preparing": "preparing a build", "stopping": "stopping", "active_unknown": "active; stage unverified",
        "unknown": "observation unavailable",
    }
    for job in safe["jobs"]:
        if job["status"] == "idle":
            continue
        label = ("Daily updater" if job["unit"] == FIXED_UNITS[0] else
                 "November downloader" if job["unit"] == FIXED_UNITS[1] else "Release publication job")
        stage = " (" + job["stage"] + ")" if "stage" in job else ""
        lines.append(label + ": " + labels[job["status"]] + stage + ".")
    if safe["status"] != "unknown" and (not safe["discovery_complete"] or any(job["status"] == "unknown" for job in safe["jobs"])):
        lines.append("Some monitored job observations are unavailable; additional activity cannot be excluded.")
    return lines
