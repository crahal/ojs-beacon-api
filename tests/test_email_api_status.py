from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_email_progress import NOW, SECRET, snapshot, service_text
import email_progress as report


def serving():
    return {"status": "serving", "health": "ok", "latest_snapshot": "2026-10-01",
            "snapshot_dates": ["2026-01-01", "2026-07-01", "2026-10-01"],
            "active_articles": 100, "total_articles": 110, "active_sources": 150,
            "high_watermark_event_id": 1234}


class OperationalReportTests(unittest.TestCase):
    def current(self):
        current = snapshot()
        current.update(api=serving(), scheduler={"updates_paused": True, "cron_installed": False})
        current["service"].update(ActiveState="failed", SubState="failed", Result="exit-code")
        current["event"] = None
        return current

    def test_serving_api_wins_over_historical_updater_failure(self):
        subject, body = report.render_report(self.current())
        self.assertTrue(subject.startswith("[OJS API] Serving"))
        self.assertIn("update job: Failed", subject)
        self.assertIn("Published snapshot: 2026-10-01", body)
        self.assertIn("2026-01-01, 2026-07-01, 2026-10-01", body)
        self.assertIn("100 active articles", body)
        self.assertIn("Automatic updates: paused", body)
        self.assertIn("Daily updater cron: not installed", body)
        self.assertIn("retained failure is not evidence", body)
        self.assertNotIn("Stopped with a reported service failure.", body)
        self.assertLess(body.index("API: serving"), body.index("Update job"))

    def test_running_build_can_coexist_with_serving_api(self):
        current = self.current()
        current["service"] = report.parse_service(service_text())
        subject, body = report.render_report(current)
        self.assertIn("Serving | work:", subject)
        self.assertIn("update job: Running", subject)
        self.assertIn("recovery service is running", body)

    def test_unavailable_api_is_not_masked_by_successful_job(self):
        current = self.current()
        current["service"].update(ActiveState="inactive", Result="success", ExecMainCode=1, ExecMainStatus=0)
        current["api"] = {"status": "unavailable", "health": "unknown"}
        subject, body = report.render_report(current)
        self.assertIn("Check failed", subject)
        self.assertNotIn("API: serving", body)
        self.assertNotIn("Published snapshot:", body)

    def test_health_alone_is_not_serving_and_auth_failure_is_distinct(self):
        current = self.current()
        current["api"] = {"status": "auth_failed", "health": "ok"}
        subject, body = report.render_report(current)
        self.assertIn("Authentication failed", subject)
        self.assertIn("health endpoint responded", body)
        self.assertNotIn("API: serving", body)

    def test_no_configuration_is_explicit_not_outage(self):
        current = self.current()
        current["api"] = {"status": "not_configured", "health": "unknown"}
        subject, body = report.render_report(current)
        self.assertIn("Not checked", subject)
        self.assertIn("live checks are not configured", body)

    def test_untrusted_api_and_scheduler_data_cannot_reach_email_or_model(self):
        current = self.current()
        current["api"].update(url=SECRET, error=SECRET, password=SECRET)
        current["scheduler"]["cron_installed"] = SECRET
        evidence = report.llm_evidence(current, None)
        self.assertTrue(evidence["api_availability_verified"])
        self.assertFalse(evidence["overall_completion_verified"])
        self.assertEqual(evidence["api"], serving())
        self.assertIsNone(evidence["scheduler"]["cron_installed"])
        self.assertNotIn(SECRET, json.dumps(evidence))
        self.assertNotIn(SECRET, report.render_report(current)[1])
        current["api"]["latest_snapshot"] = SECRET
        evidence = report.llm_evidence(current, None)
        self.assertFalse(evidence["api_availability_verified"])
        self.assertNotIn(SECRET, json.dumps(evidence))
        self.assertNotIn(SECRET, report.render_report(current)[1])

    def test_api_observation_survives_missing_systemd(self):
        with mock.patch.object(report.progress_api, "collect_api", return_value=serving()) as probe, \
                mock.patch.object(report, "diagnostic", side_effect=report.ReportError()), \
                mock.patch.object(report, "free_bytes", return_value=10), \
                mock.patch.object(report, "host_memory", return_value={}), \
                mock.patch.object(report, "path_present", return_value=True):
            current = report.collect_snapshot(Path("/project"))
        probe.assert_called_once()
        self.assertEqual(current["api"]["status"], "serving")
        self.assertTrue(report.render_report(current)[0].startswith("[OJS API] Serving"))

    def test_api_observation_survives_no_current_invocation(self):
        with mock.patch.object(report.progress_api, "collect_api", return_value=serving()), \
                mock.patch.object(report, "diagnostic", return_value="ActiveState=inactive\n"), \
                mock.patch.object(report, "free_bytes", return_value=10), \
                mock.patch.object(report, "host_memory", return_value={}), \
                mock.patch.object(report, "path_present", return_value=False):
            current = report.collect_snapshot(Path("/project"))
        self.assertTrue(report.llm_evidence(current, None)["api_availability_verified"])

    def test_saved_api_state_only_compares_verified_snapshots(self):
        current = self.current()
        previous = report.comparable_state(current, NOW)
        previous["api"]["latest_snapshot"] = "2026-07-01"
        previous["api"]["snapshot_dates"].pop()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "last-sent.json"
            report.write_state(path, previous)
            restored = report.read_state(path)
        body = report.render_report(current, restored)[1]
        self.assertIn("2026-07-01 → 2026-10-01", body)
        restored.pop("api")  # Existing version-one state remains compatible.
        self.assertNotIn("changed since the previous email", report.render_report(current, restored)[1])

    def test_pause_probe_distinguishes_absence_from_permission_error(self):
        with mock.patch.object(Path, "stat", side_effect=PermissionError()):
            self.assertIsNone(report.path_present(Path("/no-access")))
        with mock.patch.object(Path, "stat", side_effect=FileNotFoundError()):
            self.assertIs(report.path_present(Path("/absent")), False)

    def test_every_report_includes_api_and_processing_status(self):
        for api_status in ("serving", "unavailable", "auth_failed", "unverified", "not_configured"):
            current = self.current()
            current["api"]["status"] = api_status
            body = report.render_report(current)[1]
            self.assertIn("API:", body)
            self.assertIn("Processing:", body)
            self.assertLess(body.index("Processing:"), body.index("Technical details"))

    def test_standalone_download_not_confused_with_old_failed_updater(self):
        current = self.current()
        current["workload"] = {"discovery_complete": True, "jobs": [
            {"unit": "ojs-api-update.service", "status": "idle"},
            {"unit": "ojs-november-download.service", "status": "downloading",
             "stage": "beacon-transfer", "invocation_id": "c" * 32,
             "url": SECRET, "command": SECRET}]}
        body = report.render_report(current)[1]
        self.assertIn("November downloader: downloading", body)
        self.assertIn("work: Downloading", report.render_report(current)[0])
        self.assertIn("downloading is not wrangling", body)
        self.assertIn("API: serving", body)
        evidence = report.llm_evidence(current, None)
        self.assertEqual(evidence["workload"]["status"], "downloading")
        self.assertEqual(evidence["service_status"], "Failed")
        self.assertNotIn(SECRET, json.dumps(evidence))
        self.assertNotIn("c" * 32, json.dumps(evidence))
        self.assertNotIn("ojs-november-download.service", json.dumps(evidence))
        report.progress_llm._payload(evidence)

    def test_monitored_idle_is_explicit_but_missing_observations_are_not_idle(self):
        current = self.current()
        current["workload"] = {"discovery_complete": True, "jobs": [
            {"unit": unit, "status": "idle"} for unit in report.progress_activity.FIXED_UNITS]}
        self.assertIn("no monitored download, processing or publication job is running", report.render_report(current)[1])
        current["workload"]["jobs"].pop()
        self.assertIn("could not be fully verified", report.render_report(current)[1])


if __name__ == "__main__":
    unittest.main()
