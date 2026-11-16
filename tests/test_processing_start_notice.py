from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
import processing_start_notice as notice

spec = importlib.util.spec_from_file_location("watch_processing_start", ROOT / "scripts/watch_processing_start.py")
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
INVOCATION = "a" * 32
SNAPSHOT = "2026-11-01"
NOTICE = {"schema": 1, "snapshot_date": SNAPSHOT, "invocation": INVOCATION,
          "started_at": NOW.isoformat(timespec="seconds")}


def entry(message, *, age=0, invocation=INVOCATION, pid="100"):
    return json.dumps({"MESSAGE": message, "_SYSTEMD_INVOCATION_ID": invocation,
                       "_PID": pid,
                       "__REALTIME_TIMESTAMP": str(int((NOW - timedelta(seconds=age)).timestamp() * 1_000_000))})


def raw(**kwargs):
    return entry(f"[transfer] loading raw pkpbeacon-{SNAPSHOT}.sql.gz", **kwargs)


def progress(*, age=0, pid="100", **kwargs):
    event = {"stage": "sql-import", "event": "heartbeat", "step": "feeding-mysql",
             "processed_bytes": 1024, **kwargs}
    return entry("[progress] " + json.dumps(event), age=age, pid=pid)


class MarkerTests(unittest.TestCase):
    def test_notice_reduces_fields_and_token_is_fixed(self):
        self.assertEqual(notice.sanitize_notice({**NOTICE, "secret": "never-copy"}), NOTICE)
        self.assertEqual(notice.notice_token(NOTICE), SNAPSHOT + ":" + INVOCATION)
        self.assertNotIn("never-copy", str(notice.sanitize_notice({**NOTICE, "secret": "never-copy"})))

    def test_invalid_marker_fields_cannot_trigger(self):
        for fields in ({"schema": True}, {"snapshot_date": "2026-02-30"},
                       {"snapshot_date": "2026-11-01;command"}, {"invocation": "0" * 32},
                       {"invocation": "arbitrary"}, {"started_at": "2026-11-16T12:00:00"},
                       {"started_at": "invalid"}):
            with self.subTest(fields=fields):
                self.assertIsNone(notice.sanitize_notice({**NOTICE, **fields}))

    def test_pending_is_per_recipient(self):
        token = notice.notice_token(NOTICE)
        self.assertTrue(notice.pending_notice(NOTICE, None))
        self.assertTrue(notice.pending_notice(NOTICE, {notice.RECEIPT_FIELD: "different"}))
        self.assertFalse(notice.pending_notice(NOTICE, {notice.RECEIPT_FIELD: token}))
        self.assertFalse(notice.pending_notice({}, None))

    def test_receipt_validator_rejects_arbitrary_content(self):
        self.assertEqual(notice.valid_receipt(notice.notice_token(NOTICE)), SNAPSHOT + ":" + INVOCATION)
        for value in (None, True, "bad", SNAPSHOT + ":" + "0" * 32,
                      "2026-02-30:" + INVOCATION, SNAPSHOT + ":" + INVOCATION + "\n"):
            self.assertIsNone(notice.valid_receipt(value))

    def test_notice_line_is_past_tense_and_separates_current_status(self):
        line = notice.notice_line(NOTICE)
        self.assertIn("began SQL import at", line)
        self.assertIn("current API and processing status", line)
        self.assertNotIn("is processing", line)

    def test_file_ownership_permissions_size_and_symlinks_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            path = parent / "notice.json"
            path.write_text(json.dumps(NOTICE))
            path.chmod(0o644)
            self.assertEqual(notice.read_notice(path, expected_uid=os.getuid()), NOTICE)
            self.assertIsNone(notice.read_notice(path, expected_uid=os.getuid() + 1))
            path.chmod(0o666)
            self.assertIsNone(notice.read_notice(path, expected_uid=os.getuid()))
            path.chmod(0o644)
            parent.chmod(0o777)
            self.assertIsNone(notice.read_notice(path, expected_uid=os.getuid()))
            parent.chmod(0o700)
            link = parent / "link.json"
            link.symlink_to(path)
            self.assertIsNone(notice.read_notice(link, expected_uid=os.getuid()))
            path.write_text(" " * (notice.MAX_NOTICE_BYTES + 1))
            self.assertIsNone(notice.read_notice(path, expected_uid=os.getuid()))

    def test_fifo_cannot_block_marker_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fifo"
            os.mkfifo(path)
            self.assertIsNone(notice.read_notice(path, expected_uid=os.getuid()))
            with self.assertRaises(watcher.NoticeError):
                watcher.read_json(path)

    def test_unconfigured_notice_does_not_read_anything(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(notice, "read_notice") as read:
            self.assertIsNone(notice.configured_notice())
            read.assert_not_called()


class DetectionTests(unittest.TestCase):
    def detect(self, raw_output=None, import_output=None, *, armed_age=60):
        return watcher.detect_start(SNAPSHOT, INVOCATION, NOW - timedelta(seconds=armed_age), NOW,
                                    raw(age=30) if raw_output is None else raw_output,
                                    progress() if import_output is None else import_output)

    def test_date_specific_active_sql_import_is_evidence(self):
        result = self.detect()
        self.assertEqual(result["snapshot_date"], SNAPSHOT)
        self.assertEqual(result["invocation"], INVOCATION)
        self.assertEqual(result["started_at"], (NOW - timedelta(seconds=30)).isoformat(timespec="seconds"))

    def test_no_zero_byte_supervisor_download_checksum_or_failed_claim(self):
        for event in ({"processed_bytes": 0}, {"processed_bytes": True}, {"processed_bytes": -1},
                      {"processed_bytes": "1024"}, {"stage": "beacon-transfer"},
                      {"stage": "update-build"}, {"stage": "snapshot-checksum"},
                      {"event": "failed"}, {"event": "completed"}):
            with self.subTest(event=event):
                self.assertIsNone(self.detect(import_output=progress(**event)))

    def test_old_run_wrong_snapshot_and_old_before_arm_are_ignored(self):
        self.assertIsNone(self.detect(raw_output=raw(invocation="b" * 32)))
        self.assertIsNone(self.detect(raw_output=entry("[transfer] loading raw pkpbeacon-2026-10-01.sql.gz", age=30)))
        self.assertIsNone(self.detect(armed_age=5))

    def test_stale_future_reversed_or_different_process_events_are_ignored(self):
        self.assertIsNone(self.detect(raw_output=raw(age=500), import_output=progress(age=301), armed_age=600))
        self.assertIsNone(self.detect(import_output=progress(age=-1)))
        self.assertIsNone(self.detect(import_output=progress(age=40)))
        self.assertIsNone(self.detect(import_output=progress(pid="999")))

    def test_untrusted_journal_lines_are_bounded_and_structured(self):
        for value in ("not JSON", "[]", "null", "x" * 20000,
                      json.dumps({"MESSAGE": "x", "_PID": "1", "_SYSTEMD_INVOCATION_ID": INVOCATION,
                                  "__REALTIME_TIMESTAMP": "bad"})):
            self.assertEqual(watcher.journal_entries(value, INVOCATION), [])

    def test_failed_or_inactive_service_cannot_trigger_old_logs(self):
        for active, sub in (("failed", "failed"), ("inactive", "dead"), ("deactivating", "stop")):
            with patch.object(watcher, "read_service", return_value={"ActiveState": active, "SubState": sub,
                                                                   "MainPID": 123, "InvocationID": INVOCATION}), \
                    patch.object(watcher, "diagnostic") as diagnostic:
                self.assertIsNone(watcher.observe_start(SNAPSHOT, NOW - timedelta(seconds=60)))
                diagnostic.assert_not_called()

    def test_invocation_change_during_observation_is_rejected(self):
        service = {"ActiveState": "activating", "SubState": "start", "MainPID": 123,
                   "InvocationID": INVOCATION}
        with patch.object(watcher, "read_service", side_effect=[service, {**service, "InvocationID": "b" * 32}]), \
                patch.object(watcher, "diagnostic", side_effect=[raw(age=30), progress()]), \
                patch.object(watcher, "utc_now", return_value=NOW):
            self.assertIsNone(watcher.observe_start(SNAPSHOT, NOW - timedelta(seconds=60)))

    def test_no_matching_journal_event_is_a_normal_pending_observation(self):
        service = {"ActiveState": "activating", "SubState": "start", "MainPID": 123,
                   "InvocationID": INVOCATION}
        with patch.object(watcher, "read_service", return_value=service), \
                patch.object(watcher, "diagnostic", side_effect=watcher.ReportError):
            self.assertIsNone(watcher.observe_start(SNAPSHOT, NOW - timedelta(seconds=60)))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.report = self.root / "report"
        self.report.mkdir()
        self.recipients = ["primary", "subscriber"]
        self.clock = patch.object(watcher, "utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def process(self):
        return watcher.process(SNAPSHOT, self.recipients, state_root=self.root, report_root=self.report)

    def receipt(self, recipient):
        return self.report / watcher.recipient_units(recipient)[2]

    def deliver(self, action, unit):
        if action == "start":
            recipient = "primary" if unit == "ojs-api-progress-email.service" else "subscriber"
            watcher.write_json(self.receipt(recipient), {notice.RECEIPT_FIELD: notice.notice_token(NOTICE)})

    def test_arming_without_actual_import_never_sends_or_enables_timer(self):
        with patch.object(watcher, "observe_start", return_value=None), patch.object(watcher, "systemctl") as systemctl:
            self.assertFalse(self.process())
        systemctl.assert_not_called()
        self.assertFalse((self.root / f"notice-{SNAPSHOT}.json").exists())

    def test_success_sends_individually_then_enables_each_three_day_timer(self):
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "systemctl", side_effect=self.deliver) as systemctl:
            self.assertTrue(self.process())
            expected = []
            for recipient in self.recipients:
                service, timer, _ = watcher.recipient_units(recipient)
                expected.extend([(("start", service),), (("enable", timer),), (("restart", timer),)])
            self.assertEqual(systemctl.call_args_list, expected)
            systemctl.reset_mock()
            self.assertTrue(self.process())
            systemctl.assert_not_called()
        self.assertEqual((self.root / f"notice-{SNAPSHOT}.json").stat().st_mode & 0o777, 0o644)

    def test_failed_second_recipient_never_resends_successful_first_and_retries_bounded(self):
        def first_only(action, unit):
            if unit == "ojs-api-progress-email.service":
                self.deliver(action, unit)
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "delivery_outcome", return_value="not-accepted"), \
                patch.object(watcher, "systemctl", side_effect=first_only) as systemctl:
            self.assertFalse(self.process())
            systemctl.reset_mock()
            self.assertFalse(self.process())
            systemctl.assert_not_called()
        with patch.object(watcher, "utc_now", return_value=NOW + timedelta(minutes=16)), \
                patch.object(watcher, "observe_start", side_effect=AssertionError("do not change recorded event")), \
                patch.object(watcher, "systemctl", side_effect=self.deliver) as systemctl:
            self.assertTrue(self.process())
        self.assertNotIn((("start", "ojs-api-progress-email.service"),), systemctl.call_args_list)

    def test_existing_receipt_survives_watcher_crash_before_scheduling(self):
        watcher.write_json(self.receipt("primary"), {notice.RECEIPT_FIELD: notice.notice_token(NOTICE)})
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "systemctl", side_effect=self.deliver) as systemctl:
            self.assertTrue(self.process())
        self.assertNotIn((("start", "ojs-api-progress-email.service"),), systemctl.call_args_list)

    def test_smtp_accepted_without_receipt_is_held_for_manual_review(self):
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "delivery_outcome", return_value="accepted-unrecorded"), \
                patch.object(watcher, "systemctl") as systemctl:
            self.assertFalse(self.process())
            self.assertEqual(systemctl.call_count, 2)
            systemctl.reset_mock()
            with patch.object(watcher, "utc_now", return_value=NOW + timedelta(days=1)):
                self.assertFalse(self.process())
            systemctl.assert_not_called()
        state = watcher.read_json(self.root / f"watch-{SNAPSHOT}.json")
        self.assertEqual(state["held"], self.recipients)

    def test_interrupted_inflight_delivery_is_held_without_blind_retry(self):
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "systemctl", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.process()
        with patch.object(watcher, "systemctl", side_effect=self.deliver) as systemctl:
            self.assertFalse(self.process())
        self.assertNotIn((("start", "ojs-api-progress-email.service"),), systemctl.call_args_list)
        state = watcher.read_json(self.root / f"watch-{SNAPSHOT}.json")
        self.assertEqual(state["held"], ["primary"])
        self.assertEqual(state["scheduled"], ["subscriber"])

    def test_explicit_arm_never_observes_or_sends_and_keeps_original_arm_time(self):
        with patch.object(watcher, "STATE_ROOT", self.root), patch.object(watcher.os, "geteuid", return_value=0), \
                patch.object(watcher, "prepare_state_directory"), \
                patch.object(watcher, "observe_start") as observe, patch.object(watcher, "systemctl") as systemctl:
            self.assertEqual(watcher.main(["--snapshot", SNAPSHOT, "--recipient", "primary",
                                           "--recipient", "subscriber", "--arm"]), 0)
            with patch.object(watcher, "utc_now", return_value=NOW + timedelta(days=1)):
                self.assertEqual(watcher.main(["--snapshot", SNAPSHOT, "--recipient", "primary",
                                               "--recipient", "subscriber", "--arm"]), 0)
            observe.assert_not_called()
            systemctl.assert_not_called()
        self.assertEqual(watcher.read_json(self.root / f"watch-{SNAPSHOT}.json")["armed_at"], watcher.stamp(NOW))

    def test_successful_receipt_wins_even_if_systemctl_returns_failure(self):
        def accepted(action, unit):
            self.deliver(action, unit)
            if action == "start":
                raise subprocess.CalledProcessError(1, ["systemctl"])
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "systemctl", side_effect=accepted):
            self.assertTrue(self.process())

    def test_corrupted_state_fails_closed_not_rearms_and_resends(self):
        path = self.root / f"watch-{SNAPSHOT}.json"
        path.write_text("bad JSON")
        with patch.object(watcher, "systemctl") as systemctl, self.assertRaises(watcher.NoticeError):
            self.process()
        systemctl.assert_not_called()

    def test_invalid_recipient_cannot_inject_a_systemctl_unit(self):
        for name in ("--all", "primary.service", "a/b", "", "a;reboot"):
            with self.assertRaises(watcher.NoticeError):
                watcher.recipient_units(name)

    def test_receipts_are_checked_not_only_successful_service_exit(self):
        with patch.object(watcher, "observe_start", return_value=NOTICE), \
                patch.object(watcher, "delivery_outcome", return_value="not-accepted"), \
                patch.object(watcher, "systemctl") as systemctl:
            self.assertFalse(self.process())
        self.assertTrue(all(call.args[0] == "start" for call in systemctl.call_args_list))

    def test_signal_timeout_and_success_without_receipt_are_ambiguous(self):
        for output in ("ExecMainCode=2\nExecMainStatus=9\nActiveState=failed\n",
                       "ExecMainCode=2\nExecMainStatus=15\nActiveState=failed\n",
                       "ExecMainCode=0\nExecMainStatus=0\nActiveState=activating\n",
                       "ExecMainCode=1\nExecMainStatus=0\nActiveState=inactive\n"):
            with patch.object(watcher, "diagnostic", return_value=output):
                self.assertEqual(watcher.delivery_outcome("ojs-api-progress-email.service"), "unknown")
        with patch.object(watcher, "diagnostic", return_value="ExecMainCode=1\nExecMainStatus=1\nActiveState=failed\n"):
            self.assertEqual(watcher.delivery_outcome("ojs-api-progress-email.service"), "not-accepted")

    def test_completion_stops_only_its_own_watcher_timer(self):
        with patch.object(watcher, "STATE_ROOT", self.root), patch.object(watcher.os, "geteuid", return_value=0), \
                patch.object(watcher, "prepare_state_directory"), patch.object(watcher, "process", return_value=True), \
                patch.object(watcher, "systemctl") as systemctl:
            self.assertEqual(watcher.main(["--snapshot", SNAPSHOT, "--recipient", "primary"]), 0)
        self.assertEqual(systemctl.call_args_list, [(("disable", f"ojs-api-processing-start@{SNAPSHOT}.timer"),),
                                                   (("stop", f"ojs-api-processing-start@{SNAPSHOT}.timer"),)])


class DeploymentTests(unittest.TestCase):
    def test_watcher_unit_has_no_email_api_or_openai_credential(self):
        service = (ROOT / "deploy/ojs-api-processing-start@.service").read_text()
        self.assertNotIn("LoadCredential=", service)
        self.assertNotIn("EnvironmentFile=/etc/ojs-api/progress-email.env", service)
        self.assertIn("StateDirectoryMode=0755", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/ojs-api-monitor/scripts/watch_processing_start.py", service)
        self.assertNotIn("automatic_update.sh", service)
        self.assertNotIn("run_pipeline.py", service)

    def test_timer_polling_is_bounded_and_does_not_start_an_update(self):
        timer = (ROOT / "deploy/ojs-api-processing-start@.timer").read_text()
        self.assertIn("OnUnitInactiveSec=1min", timer)
        self.assertNotIn("ojs-api-update", timer)
        self.assertEqual(watcher.RETRY_SECONDS, 900)


if __name__ == "__main__":
    unittest.main()
