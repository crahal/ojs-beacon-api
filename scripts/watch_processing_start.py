#!/usr/bin/env python3
"""Notify configured recipients once after a newly observed snapshot SQL import.

This root-only watcher never loads SMTP/API secrets and never starts a build.
It asks existing hardened email units to report, verifies their per-recipient
SMTP-success receipts, then resets the existing 72-hour timers. Bounded journal
reads are restricted to the current updater invocation and a chosen date.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from email_progress import diagnostic, parse_service, ReportError
from processing_start_notice import (
    INVOCATION, RECEIPT_FIELD, notice_token, sanitize_notice, snapshot_date, timestamp,
)


SERVICE = "ojs-api-update.service"
STATE_ROOT = Path("/var/lib/ojs-api-start-notice")
REPORT_ROOT = Path("/var/lib/ojs-api-report")
MAX_STATE_BYTES = 16384
RETRY_SECONDS = 15 * 60
RECIPIENTS = {
    "primary": ("ojs-api-progress-email.service", "ojs-api-progress-email.timer", "last-sent.json"),
}


class NoticeError(Exception):
    """Keep journal data and command output out of diagnostic messages."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def recipient_units(name: str) -> tuple[str, str, str]:
    if name in RECIPIENTS:
        return RECIPIENTS[name]
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) is None:
        raise NoticeError()
    return (f"ojs-api-progress-email@{name}.service",
            f"ojs-api-progress-email@{name}.timer", f"subscriber-{name}.json")


def read_json(path: Path) -> dict | None:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise NoticeError()
        raw = os.read(descriptor, MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_STATE_BYTES:
        raise NoticeError()
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise NoticeError() from None
    if not isinstance(value, dict):
        raise NoticeError()
    return value


def write_json(path: Path, value: dict, *, mode: int = 0o600) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".notice-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def watcher_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def read_service() -> dict:
    return parse_service(diagnostic([
        "systemctl", "show", SERVICE, "--no-pager",
        "--property=ActiveState,SubState,InvocationID,MainPID",
    ], limit=16384))


def running(service: dict) -> bool:
    return (service.get("ActiveState") in {"active", "activating"}
            and service.get("SubState") in {"running", "start"}
            and isinstance(service.get("MainPID"), int) and service["MainPID"] > 0
            and isinstance(service.get("InvocationID"), str)
            and INVOCATION.fullmatch(service["InvocationID"]) is not None)


def journal_entries(output: str, invocation: str) -> list[tuple[datetime, str, str]]:
    entries = []
    for line in output.splitlines()[-64:]:
        if len(line) > 16384:
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("_SYSTEMD_INVOCATION_ID") != invocation:
                continue
            message, micros, pid = raw.get("MESSAGE"), raw.get("__REALTIME_TIMESTAMP"), raw.get("_PID")
            if (not isinstance(message, str) or len(message) > 4096
                    or not isinstance(micros, str) or not re.fullmatch(r"[0-9]{1,20}", micros)
                    or not isinstance(pid, str) or not re.fullmatch(r"[0-9]{1,10}", pid)):
                continue
            logged = datetime.fromtimestamp(int(micros) / 1_000_000, timezone.utc)
            entries.append((logged, pid, message))
        except (ValueError, OverflowError, OSError, RecursionError):
            continue
    return sorted(entries)


def detect_start(snapshot: str, invocation: str, armed_at: datetime, now: datetime,
                 raw_output: str, import_output: str) -> dict | None:
    """Require date-specific raw loading plus a real SQL-import progress event.

    A transfer heartbeat, checksum, parent supervisor, stale failed job, or an
    earlier snapshot's import is insufficient. The timestamp comes from journald,
    not an arbitrary timestamp embedded in pipeline text.
    """
    expected = re.compile(r"\[transfer\] loading raw pkpbeacon-" + re.escape(snapshot) + r"\.sql(?:\.gz)?\Z")
    raw = [(logged, pid) for logged, pid, message in journal_entries(raw_output, invocation)
           if expected.fullmatch(message) and armed_at <= logged <= now]
    if not raw:
        return None
    began, writer = raw[-1]
    for logged, pid, message in journal_entries(import_output, invocation):
        if pid != writer or not began <= logged <= now or (now - logged).total_seconds() > 300:
            continue
        if not message.startswith("[progress] "):
            continue
        try:
            event = json.loads(message[len("[progress] "):])
        except (ValueError, RecursionError):
            continue
        if (isinstance(event, dict) and event.get("stage") == "sql-import"
                and event.get("event") in {"started", "heartbeat"}
                and event.get("step") in {"feeding-mysql", "waiting-for-mysql"}
                and type(event.get("processed_bytes")) is int and event["processed_bytes"] > 0):
            return {"schema": 1, "snapshot_date": snapshot, "invocation": invocation,
                    "started_at": stamp(began)}
    return None


def observe_start(snapshot: str, armed_at: datetime) -> dict | None:
    service = read_service()
    if not running(service):
        return None
    invocation = service["InvocationID"]
    command = ["journalctl", "--unit=" + SERVICE, "--no-pager", "--output=json",
               "--output-fields=MESSAGE,_PID,_SYSTEMD_INVOCATION_ID,__REALTIME_TIMESTAMP",
               "_SYSTEMD_INVOCATION_ID=" + invocation]
    try:
        raw = diagnostic(command + ["--lines=1", "--grep=" + (
            r"^\[transfer\] loading raw pkpbeacon-" + snapshot + r"\.sql(\.gz)?$")],
            limit=32768)
        imports = diagnostic(command + ["--lines=16", "--grep=" + (
            r'^\[progress\] .*"stage"\s*:\s*"sql-import"')], limit=128 * 1024)
    except ReportError:
        # journalctl --grep exits nonzero when the sought event does not exist.
        # Missing or unavailable evidence must never manufacture a start notice.
        return None
    latest = read_service()
    if not running(latest) or latest.get("InvocationID") != invocation:
        return None
    return detect_start(snapshot, invocation, armed_at, utc_now(), raw, imports)


def systemctl(action: str, unit: str) -> None:
    subprocess.run(["systemctl", action, unit], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=400, check=True)


def delivery_outcome(service: str) -> str:
    output = diagnostic(["systemctl", "show", service, "--no-pager",
                         "--property=ExecMainCode,ExecMainStatus,ActiveState,SubState"], limit=4096)
    result = parse_service(output)
    if result.get("ExecMainCode") == 1 and result.get("ExecMainStatus") == 2:
        return "accepted-unrecorded"
    if (result.get("ExecMainCode") == 1 and result.get("ExecMainStatus") == 1
            and result.get("ActiveState") in {"failed", "inactive"}):
        return "not-accepted"
    # Successful exit without a receipt could be a skip or a stale sender binary;
    # a signal/timeout could have occurred after SMTP accepted the message.
    return "unknown"


def load_watch_state(snapshot: str, recipients: list[str], state_root: Path) -> dict:
    state_path = state_root / f"watch-{snapshot}.json"
    state = read_json(state_path)
    if state is None:
        state = {"schema": 1, "snapshot_date": snapshot, "armed_at": stamp(utc_now()),
                 "recipients": recipients, "scheduled": [], "held": [], "attempted": {}, "inflight": []}
        write_json(state_path, state)
    if (state.get("schema") != 1 or state.get("snapshot_date") != snapshot
            or state.get("recipients") != recipients or timestamp(state.get("armed_at")) is None
            or not isinstance(state.get("scheduled"), list)
            or any(name not in recipients for name in state["scheduled"])
            or not isinstance(state.get("held"), list)
            or any(name not in recipients for name in state["held"])
            or not isinstance(state.get("inflight"), list)
            or any(name not in recipients for name in state["inflight"])
            or not isinstance(state.get("attempted"), dict)
            or any(name not in recipients or timestamp(at) is None
                   for name, at in state["attempted"].items())):
        raise NoticeError()
    return state


def process(snapshot: str, recipients: list[str], *, state_root: Path = STATE_ROOT,
            report_root: Path = REPORT_ROOT) -> bool:
    state_path = state_root / f"watch-{snapshot}.json"
    state = load_watch_state(snapshot, recipients, state_root)
    if set(state["scheduled"]) == set(recipients):
        return True
    notice = sanitize_notice(state.get("notice"))
    if "notice" in state and (notice is None or notice["snapshot_date"] != snapshot):
        raise NoticeError()
    if notice is None:
        notice = observe_start(snapshot, timestamp(state["armed_at"]))
        if notice is None:
            print("Processing-start watcher armed; no new matching SQL import observed.")
            return False
        state["notice"] = notice
        write_json(state_path, state)
    marker = state_root / f"notice-{snapshot}.json"
    write_json(marker, notice, mode=0o644)
    token = notice_token(notice)
    for recipient in recipients:
        if recipient in state["scheduled"]:
            continue
        if recipient in state["held"]:
            print(f"Processing-start report for {recipient} requires manual delivery-state review; no retry sent.")
            continue
        service, timer, receipt_name = recipient_units(recipient)
        receipt_path = report_root / receipt_name
        receipt = read_json(receipt_path) or {}
        if receipt.get(RECEIPT_FIELD) != token:
            if recipient in state["inflight"]:
                state["inflight"].remove(recipient)
                state["held"].append(recipient)
                write_json(state_path, state)
                print(f"Processing-start report for {recipient} was interrupted with unknown delivery; manual review required.")
                continue
            last_attempt = timestamp(state["attempted"].get(recipient))
            if last_attempt and (utc_now() - last_attempt).total_seconds() < RETRY_SECONDS:
                continue
            state["attempted"][recipient] = stamp(utc_now())
            state["inflight"].append(recipient)
            write_json(state_path, state)
            try:
                systemctl("start", service)
            except (subprocess.SubprocessError, OSError):
                # A successful SMTP submission can precede a later unit error;
                # use the durable receipt rather than the systemctl exit alone.
                pass
            receipt = read_json(receipt_path) or {}
            if receipt.get(RECEIPT_FIELD) != token:
                state["inflight"].remove(recipient)
                outcome = delivery_outcome(service)
                if outcome != "not-accepted":
                    state["held"].append(recipient)
                    write_json(state_path, state)
                    print(f"Processing-start report for {recipient} has ambiguous delivery without a saved receipt; manual review required.")
                    continue
                write_json(state_path, state)
                print(f"Processing-start report for {recipient} awaits a successful delivery receipt.")
                continue
        systemctl("enable", timer)
        systemctl("restart", timer)
        if recipient in state["inflight"]:
            state["inflight"].remove(recipient)
        state["scheduled"].append(recipient)
        write_json(state_path, state)
        print(f"Processing-start report accepted for {recipient}; 72-hour timer restarted.")
    return set(state["scheduled"]) == set(recipients)


def prepare_state_directory(root: Path) -> None:
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    information = root.lstat()
    if (not stat.S_ISDIR(information.st_mode) or information.st_uid != 0
            or information.st_mode & 0o022):
        raise NoticeError()
    # An administrator's restrictive umask must not make the nonsecret marker
    # unreadable to the normal unprivileged report service.
    root.chmod(0o755)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--arm", action="store_true",
                        help="Record the earliest allowed observation time only; never observe, send, or start timers.")
    parser.add_argument("--recipient", action="append", required=True,
                        help="primary or an existing configured subscriber instance name")
    args = parser.parse_args(argv)
    if snapshot_date(args.snapshot) is None:
        parser.error("--snapshot must be a valid ISO date")
    try:
        if os.geteuid() != 0 or len(args.recipient) != len(set(args.recipient)):
            raise NoticeError()
        for recipient in args.recipient:
            recipient_units(recipient)
        prepare_state_directory(STATE_ROOT)
        with watcher_lock(STATE_ROOT / "watch.lock") as acquired:
            if not acquired:
                return 0
            if args.arm:
                load_watch_state(args.snapshot, args.recipient, STATE_ROOT)
                print("Processing-start watcher armed. No email was sent and no timer was changed.")
                return 0
            completed = process(args.snapshot, args.recipient)
        print("Processing-start notification completed." if completed else "Processing-start notification pending.")
        if completed:
            timer = f"ojs-api-processing-start@{args.snapshot}.timer"
            systemctl("disable", timer)
            systemctl("stop", timer)
        return 0
    except (Exception, KeyboardInterrupt):
        print("Processing-start watcher could not complete safely; no build action was taken.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
