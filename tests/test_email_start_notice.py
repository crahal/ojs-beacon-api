"""One observed start notification per recipient, then three-day reporting."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest import mock

from test_email_progress import NOW as FIXTURE_NOW, SECRET, snapshot
from test_email_api_status import serving
import email_progress as report
import processing_start_notice as notices


NOW = FIXTURE_NOW + timedelta(days=36)
RECEIPT = notices.RECEIPT_FIELD
NO_OVERRIDE = object()


class StartNoticeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.primary = self.directory / "last-sent.json"
        self.subscriber = self.directory / "subscriber-colleague.json"
        self.notice = {
            "schema": 1,
            "snapshot_date": "2026-11-01",
            "invocation": "b" * 32,
            "started_at": report.stamp(NOW - timedelta(minutes=1)),
        }
        self.token = notices.notice_token(self.notice)
        self.current = snapshot(stage="sql-import", processed_bytes=8192, total_bytes=32768)
        self.current.update(observed_at=report.stamp(NOW), api=serving())
        self.current["event"]["logged_at"] = report.stamp(NOW - timedelta(seconds=10))

    def save(self, path, age, *, receipt=None):
        state = report.comparable_state(self.current, NOW - age)
        if receipt is not None:
            state[RECEIPT] = receipt
        report.write_state(path, state)

    def run_report(self, path=None, *arguments, notice=NO_OVERRIDE, send_error=None,
                   recipient="owner@example.org", ai=False):
        path = path or self.primary
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                "OJS_PROGRESS_EMAIL_FROM": "sender@example.org",
                "OJS_PROGRESS_EMAIL_TO": recipient,
                "OJS_PROGRESS_LLM_ENABLED": "1" if ai else "0",
            }, clear=True))
            collect = stack.enter_context(mock.patch.object(
                report, "collect_snapshot", return_value=self.current))
            stack.enter_context(mock.patch.object(report, "utc_now", return_value=NOW))
            marker = stack.enter_context(mock.patch.object(
                notices, "configured_notice", return_value=self.notice if notice is NO_OVERRIDE else notice))
            password = stack.enter_context(mock.patch.object(report, "read_password", return_value=SECRET))
            send = stack.enter_context(mock.patch.object(report, "send_email", side_effect=send_error))
            assessment = stack.enter_context(mock.patch.object(
                report, "ai_assessment", side_effect=report.progress_llm.SummaryUnavailable()))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            code = report.main(["--state-file", str(path), *arguments])
        return code, output.getvalue() + errors.getvalue(), send, password, collect, marker, assessment

    def test_first_notice_records_success_and_reports_start_as_historical(self):
        # A job can stop between the observed start and delivery. The notice
        # must not turn that old event into a claim it is still running.
        self.current["service"].update(ActiveState="failed", SubState="failed", Result="exit-code")
        code, output, send, *_ = self.run_report()
        self.assertEqual(code, 0)
        send.assert_called_once()
        sender, recipient, _, subject, body = send.call_args.args
        self.assertEqual((sender, recipient), ("sender@example.org", "owner@example.org"))
        self.assertIn("Processing started: 2026-11-01", subject)
        self.assertIn("snapshot 2026-11-01 began SQL import at " + self.notice["started_at"], body)
        self.assertIn("current API and processing status", body)
        self.assertIn("API: serving", body)
        self.assertIn("last run failed", body)
        state = report.read_state(self.primary)
        self.assertEqual(state[RECEIPT], self.token)
        self.assertEqual(state["sent_at"], report.stamp(NOW))
        self.assertEqual(self.primary.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, output + subject + body + self.primary.read_text())

    def test_pending_notice_bypasses_recent_send_exactly_once(self):
        self.save(self.primary, timedelta(minutes=1))
        first = self.run_report()
        self.assertEqual(first[0], 0)
        first[2].assert_called_once()
        before = self.primary.read_bytes()
        second = self.run_report()
        self.assertEqual(second[0], 0)
        self.assertIn("already sent; skipped", second[1])
        second[2].assert_not_called()
        second[3].assert_not_called()
        second[4].assert_not_called()
        self.assertEqual(before, self.primary.read_bytes())

    def test_three_day_guard_applies_after_the_notice_including_exact_boundary(self):
        for age, expected in (
            (timedelta(hours=72, seconds=-1), False),
            (timedelta(hours=72), True),
            (timedelta(hours=72, seconds=1), True),
            (timedelta(seconds=-1), False),
        ):
            with self.subTest(age=age):
                self.save(self.primary, age, receipt=self.token)
                before = self.primary.read_bytes()
                code, _, send, password, collect, *_ = self.run_report()
                self.assertEqual(code, 0)
                self.assertEqual(send.called, expected)
                self.assertEqual(password.called, expected)
                self.assertEqual(collect.called, expected)
                self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)
                if expected:
                    self.assertNotIn("Processing started:", send.call_args.args[3])
                    self.assertNotIn("Processing-start notice:", send.call_args.args[4])
                else:
                    self.assertEqual(before, self.primary.read_bytes())

    def test_routine_report_retains_receipt_when_notice_marker_is_absent(self):
        self.save(self.primary, timedelta(hours=73), receipt=self.token)
        code, _, send, *_ = self.run_report(notice=None)
        self.assertEqual(code, 0)
        send.assert_called_once()
        self.assertNotIn("Processing started:", send.call_args.args[3])
        self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)
        # Restoring the same root marker must not authorize another notice.
        code, _, send, password, *_ = self.run_report()
        self.assertEqual(code, 0)
        send.assert_not_called()
        password.assert_not_called()

    def test_two_recipients_receive_independent_notice_and_delivery_checkpoints(self):
        self.save(self.primary, timedelta(minutes=1))
        self.save(self.subscriber, timedelta(minutes=1))
        second_before = self.subscriber.read_bytes()
        owner = self.run_report()
        self.assertEqual(owner[0], 0)
        self.assertEqual(second_before, self.subscriber.read_bytes())
        owner_before = self.primary.read_bytes()
        colleague = self.run_report(self.subscriber, recipient="colleague@example.org")
        self.assertEqual(colleague[0], 0)
        self.assertEqual(colleague[2].call_args.args[1], "colleague@example.org")
        self.assertEqual(owner_before, self.primary.read_bytes())
        self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)
        self.assertEqual(report.read_state(self.subscriber)[RECEIPT], self.token)

    def test_failure_for_second_recipient_does_not_repeat_first_recipient_notice(self):
        self.assertEqual(self.run_report()[0], 0)
        owner_before = self.primary.read_bytes()
        failed = self.run_report(self.subscriber, recipient="colleague@example.org",
                                 send_error=smtplib.SMTPAuthenticationError(535, SECRET.encode()))
        self.assertEqual(failed[0], 1)
        self.assertNotIn(SECRET, failed[1])
        self.assertFalse(self.subscriber.exists())
        repeated_owner = self.run_report()
        repeated_owner[2].assert_not_called()
        self.assertEqual(owner_before, self.primary.read_bytes())
        retried_colleague = self.run_report(self.subscriber, recipient="colleague@example.org")
        self.assertEqual(retried_colleague[0], 0)
        retried_colleague[2].assert_called_once()
        self.assertEqual(report.read_state(self.subscriber)[RECEIPT], self.token)

    def test_failed_smtp_retains_previous_report_without_fabricating_receipt(self):
        self.save(self.primary, timedelta(minutes=1))
        before = self.primary.read_bytes()
        code, output, send, *_ = self.run_report(send_error=OSError(SECRET))
        self.assertEqual(code, 1)
        send.assert_called_once()
        self.assertEqual(before, self.primary.read_bytes())
        self.assertIsNone(report.read_state(self.primary)[RECEIPT])
        self.assertNotIn(SECRET, output)

    def test_accepted_but_unsaved_notice_has_distinct_review_required_exit(self):
        self.save(self.primary, timedelta(minutes=1))
        before = self.primary.read_bytes()
        with mock.patch.object(report, "write_state", side_effect=OSError(SECRET)):
            code, output, send, *_ = self.run_report()
        self.assertEqual(code, 2)
        send.assert_called_once()
        self.assertIn("accepted by SMTP, but comparison state could not be saved", output)
        self.assertNotIn(SECRET, output)
        self.assertEqual(before, self.primary.read_bytes())
        self.assertIsNone(report.read_state(self.primary)[RECEIPT])

    def test_invalid_markers_never_bypass_cadence_or_leak_untrusted_values(self):
        invalid = [None, {}, [], SECRET,
                   {**self.notice, "schema": True},
                   {**self.notice, "snapshot_date": SECRET},
                   {**self.notice, "snapshot_date": "2026-02-30"},
                   {**self.notice, "invocation": SECRET},
                   {**self.notice, "invocation": "0" * 32},
                   {**self.notice, "started_at": SECRET}]
        for value in invalid:
            with self.subTest(notice=value):
                self.save(self.primary, timedelta(minutes=1))
                before = self.primary.read_bytes()
                code, output, send, password, collect, *_ = self.run_report(notice=value)
                self.assertEqual(code, 0)
                send.assert_not_called()
                password.assert_not_called()
                collect.assert_not_called()
                self.assertEqual(before, self.primary.read_bytes())
                self.assertNotIn(SECRET, output)

    def test_unrelated_prior_notice_is_not_a_receipt_for_this_notice(self):
        old = {**self.notice, "snapshot_date": "2026-10-01", "invocation": "c" * 32}
        self.save(self.primary, timedelta(minutes=1), receipt=notices.notice_token(old))
        code, _, send, *_ = self.run_report()
        self.assertEqual(code, 0)
        send.assert_called_once()
        self.assertIn("Processing started: 2026-11-01", send.call_args.args[3])
        self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)

    def test_invalid_saved_receipt_is_not_echoed_or_propagated(self):
        self.save(self.primary, timedelta(hours=73), receipt=SECRET)
        self.assertIsNone(report.read_state(self.primary)[RECEIPT])
        code, output, send, *_ = self.run_report(notice=None)
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, output + send.call_args.args[3] + send.call_args.args[4])
        self.assertIsNone(report.read_state(self.primary)[RECEIPT])

    def test_untrusted_extra_marker_fields_do_not_enter_report_or_saved_state(self):
        code, output, send, *_ = self.run_report(notice={**self.notice, "password": SECRET,
                                                        "raw_logs": SECRET, "url": SECRET})
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, output + send.call_args.args[3] + send.call_args.args[4])
        self.assertNotIn(SECRET, self.primary.read_text())
        self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)

    def test_dry_run_never_sends_or_acknowledges_a_pending_notice(self):
        self.save(self.primary, timedelta(minutes=1))
        before = self.primary.read_bytes()
        code, output, send, password, _, marker, assessment = self.run_report(None, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("API: serving", output)
        send.assert_not_called()
        password.assert_not_called()
        marker.assert_not_called()
        assessment.assert_not_called()
        self.assertEqual(before, self.primary.read_bytes())
        self.assertNotIn(SECRET, output)

    def test_pending_notice_does_not_override_shared_delivery_lock(self):
        with report.report_lock(self.primary) as acquired:
            self.assertTrue(acquired)
            code, output, send, password, collect, marker, assessment = self.run_report()
        self.assertEqual(code, 0)
        self.assertIn("another report is in progress", output)
        for action in (send, password, collect, marker, assessment):
            action.assert_not_called()
        self.assertFalse(self.primary.exists())

    def test_ai_unavailable_does_not_suppress_factual_notice_or_bypass_ai_caps(self):
        code, _, send, _, _, _, assessment = self.run_report(ai=True)
        self.assertEqual(code, 0)
        assessment.assert_called_once()
        self.assertIn("Optional AI interpretation unavailable", send.call_args.args[4])
        self.assertIn("Processing-start notice:", send.call_args.args[4])
        self.assertEqual(report.read_state(self.primary)[RECEIPT], self.token)
        repeated = self.run_report(ai=True)
        repeated[2].assert_not_called()
        repeated[6].assert_not_called()

    def test_smtp_acceptance_precedes_receipt_commit(self):
        calls = []
        original_write = report.write_state

        def persist(path, state):
            calls.append("receipt")
            self.assertEqual(state[RECEIPT], self.token)
            original_write(path, state)

        with mock.patch.object(report, "write_state", side_effect=persist):
            code, *_ = self.run_report(send_error=lambda *args: calls.append("accepted"))
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["accepted", "receipt"])
        self.assertEqual(json.loads(self.primary.read_text())[RECEIPT], self.token)


if __name__ == "__main__":
    unittest.main()
