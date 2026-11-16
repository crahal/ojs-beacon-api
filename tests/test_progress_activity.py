from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import progress_activity as activity


NOW = "2026-11-16T15:00:00+00:00"
RUN = "a" * 32
OTHER = "b" * 32
SECRET = "private-address-and-credential-do-not-display"
UPDATE, DOWNLOAD = activity.FIXED_UNITS
PUBLISH = "ojs-api-publish-2026-10-01.service"


def properties(state="active", invocation=RUN, **extra):
    values = {"LoadState": "loaded", "ActiveState": state, "SubState": "running",
              "MainPID": "123", "InvocationID": invocation, **extra}
    return "\n".join(key + "=" + str(value) for key, value in values.items())


def event(stage="sql-import", kind="heartbeat", *, invocation=RUN, when=NOW, **extra):
    return json.dumps({"_SYSTEMD_INVOCATION_ID": invocation,
                       "__REALTIME_TIMESTAMP": str(int(datetime.fromisoformat(when).timestamp() * 1_000_000)),
                       "MESSAGE": "[progress] " + json.dumps({"stage": stage, "event": kind, **extra})})


class ActivityTests(unittest.TestCase):
    def collect(self, states=None, journals=None, listing="", *, errors=None):
        states = states or {UPDATE: properties("failed"), DOWNLOAD: properties("inactive")}
        journals, errors = journals or {}, errors or set()
        calls, observations = [], {}

        def diagnostic(command, *, timeout, limit):
            calls.append(command)
            self.assertLessEqual(timeout, 5)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(limit, 512 * 1024)
            if command[:2] == ["systemctl", "list-units"]:
                if "listing" in errors:
                    raise RuntimeError(SECRET)
                return listing
            unit = command[2] if command[0] == "systemctl" else command[1].removeprefix("--unit=")
            if unit in errors:
                raise OSError(SECRET)
            if command[0] == "journalctl":
                self.assertIn("_SYSTEMD_INVOCATION_ID=" + RUN, command)
                return journals.get(unit, "")
            value = states.get(unit, properties("inactive"))
            if isinstance(value, list):
                index = observations.get(unit, 0)
                observations[unit] = index + 1
                return value[min(index, len(value) - 1)]
            return value

        return activity.collect_activity(diagnostic=diagnostic, observed_at=NOW), calls

    def test_old_failed_updater_does_not_hide_current_download(self):
        result, calls = self.collect(
            {UPDATE: properties("failed"), DOWNLOAD: properties()},
            {UPDATE: event(), DOWNLOAD: event("beacon-transfer")})
        self.assertEqual(result["status"], "downloading")
        self.assertEqual(result["jobs"][0], {"unit": UPDATE, "status": "idle"})
        self.assertEqual(result["jobs"][1]["stage"], "beacon-transfer")
        self.assertFalse(any(call[0] == "journalctl" and call[1] == "--unit=" + UPDATE for call in calls))
        self.assertIn("not wrangling", activity.activity_summary(result)[0])

    def test_real_processing_requires_current_fresh_pipeline_stage(self):
        result, _ = self.collect({UPDATE: properties()}, {UPDATE: event()})
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["jobs"][0]["invocation_id"], RUN)
        self.assertEqual(result["jobs"][0]["stage"], "sql-import")

    def test_archive_validation_is_not_processing(self):
        result, _ = self.collect({DOWNLOAD: properties()}, {DOWNLOAD: event("beacon-archive-validation")})
        self.assertEqual(result["status"], "validating")
        self.assertIn("does not establish", activity.activity_summary(result)[0])

    def test_actual_processing_does_not_include_source_checksum(self):
        for stage in ("snapshot-checksum", "file-checksum", "beacon-legacy-checksum"):
            with self.subTest(stage=stage):
                result, _ = self.collect({UPDATE: properties()}, {UPDATE: event(stage)})
                self.assertEqual(result["status"], "validating")

    def test_supervisor_heartbeat_does_not_override_sql_stage(self):
        result, _ = self.collect({UPDATE: properties()}, {UPDATE: "\n".join([
            event(), event("update-build", when="2026-11-16T15:00:00.100000+00:00")])})
        self.assertEqual(result["status"], "processing")

    def test_supervisor_alone_is_not_evidence_of_processing(self):
        result, _ = self.collect({UPDATE: properties()}, {UPDATE: event("update-build")})
        self.assertEqual(result["status"], "active_unknown")

    def test_historical_invocation_never_becomes_current_stage(self):
        result, _ = self.collect({UPDATE: properties()}, {UPDATE: event(invocation=OTHER)})
        self.assertEqual(result["status"], "active_unknown")

    def test_changing_invocation_discards_stage(self):
        result, _ = self.collect({UPDATE: [properties(), properties(invocation=OTHER)]}, {UPDATE: event()})
        self.assertEqual(result["status"], "active_unknown")
        self.assertNotIn("stage", result["jobs"][0])

    def test_ended_service_not_reported_as_processing(self):
        result, _ = self.collect({UPDATE: [properties(), properties("inactive")]}, {UPDATE: event()})
        self.assertEqual(result["status"], "idle")

    def test_inactive_failed_and_successful_remain_after_exit_are_idle(self):
        for state in (properties("inactive"), properties("failed"),
                      properties("active", SubState="exited", MainPID=0)):
            result, _ = self.collect({UPDATE: state}, {UPDATE: event()})
            self.assertEqual(result["status"], "idle")

    def test_stale_and_future_events_do_not_establish_processing(self):
        for when in ("2026-11-16T14:54:59+00:00", "2026-11-16T15:00:01+00:00"):
            result, _ = self.collect({UPDATE: properties()}, {UPDATE: event(when=when)})
            self.assertEqual(result["status"], "active_unknown")

    def test_live_clock_is_read_after_diagnostics_for_new_heartbeats(self):
        def diagnostic(command, **_limits):
            if command[:2] == ["systemctl", "list-units"]:
                return ""
            if command[0] == "journalctl":
                return event(when="2026-11-16T15:00:04+00:00")
            return properties() if command[2] == UPDATE else properties("inactive")

        with mock.patch.object(activity, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-11-16T15:00:05+00:00")
            result = activity.collect_activity(diagnostic=diagnostic)
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["observed_at"], "2026-11-16T15:00:05+00:00")

    def test_terminal_stage_not_reported_as_current_processing(self):
        for kind in ("completed", "failed"):
            result, _ = self.collect({UPDATE: properties()}, {UPDATE: event(kind=kind)})
            self.assertEqual(result["status"], "active_unknown")

    def test_downloader_cannot_claim_sql_processing(self):
        result, _ = self.collect({DOWNLOAD: properties()}, {DOWNLOAD: event()})
        self.assertEqual(result["status"], "active_unknown")

    def test_stopping_is_reported_without_claiming_failure_or_outage(self):
        result, _ = self.collect({UPDATE: properties("deactivating")})
        self.assertEqual(result["status"], "stopping")
        self.assertIn("may still be", activity.activity_summary(result)[0])

    def test_dated_publication_units_are_discovered_and_observed(self):
        result, calls = self.collect({PUBLISH: properties()}, {PUBLISH: event("update-cold-verification")},
                                     listing=PUBLISH + " loaded activating start " + SECRET)
        self.assertEqual(result["status"], "publishing")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(any(PUBLISH in call for call in calls))

    def test_publication_discovery_excludes_untrusted_or_noncalendar_names(self):
        names = ["evil.service", "ojs-api-publish-2026-02-30.service", "ojs-api-publish-$(secret).service"]
        result, calls = self.collect(listing="\n".join(names))
        self.assertEqual(len(result["jobs"]), 2)
        for name in names:
            self.assertFalse(any(name in call for call in calls))

    def test_publication_discovery_is_bounded_and_not_complete_if_truncated(self):
        result, _ = self.collect(listing="\n".join(f"ojs-api-publish-2026-11-{day:02}.service" for day in range(1, 9)))
        self.assertEqual(len(result["jobs"]), 6)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["discovery_complete"])

    def test_private_log_values_are_never_returned(self):
        journal = "\n".join([SECRET, event(stage=SECRET), event(note=SECRET, url=SECRET, password=SECRET),
                              json.dumps({"MESSAGE": SECRET})])
        result, _ = self.collect({UPDATE: properties(Description=SECRET)}, {UPDATE: journal})
        self.assertEqual(result["status"], "processing")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, "\n".join(activity.activity_summary(result)))

    def test_service_and_journal_failures_are_explicit_and_do_not_leak_details(self):
        result, _ = self.collect(errors={UPDATE})
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("could not be fully verified", activity.activity_summary(result)[0])

    def test_partial_failure_does_not_hide_known_download(self):
        result, _ = self.collect({DOWNLOAD: properties()}, {DOWNLOAD: event("beacon-transfer")}, errors={UPDATE})
        self.assertEqual(result["status"], "downloading")
        self.assertIn("additional activity cannot be excluded", activity.activity_summary(result)[-1])

    def test_discovery_failure_not_misrepresented_as_idle(self):
        result, _ = self.collect(errors={"listing"})
        self.assertEqual(result["status"], "unknown")

    def test_global_deadline_prevents_further_diagnostics(self):
        diagnostic = mock.Mock(return_value="")
        with mock.patch.object(activity.time, "monotonic", side_effect=[0, 21, 22, 23]):
            result = activity.collect_activity(diagnostic=diagnostic, observed_at=NOW)
        diagnostic.assert_not_called()
        self.assertEqual(result["status"], "unknown")

    def test_sanitization_discards_arbitrary_status_units_fields_and_payloads(self):
        result = activity.sanitize_activity({"status": SECRET, "observed_at": SECRET,
            "discovery_complete": True, "jobs": [
                {"unit": UPDATE, "status": SECRET, "stage": SECRET, "invocation_id": SECRET},
                {"unit": DOWNLOAD, "status": "idle", "command": SECRET},
                {"unit": SECRET, "status": "processing"}]})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["status"], "unknown")

    def test_no_data_always_has_explicit_unverified_summary(self):
        for value in (None, [], {}, {"status": "idle"}):
            self.assertFalse(activity.sanitize_activity(value)["discovery_complete"])
            self.assertTrue(activity.activity_summary(value)[0].startswith("Processing: "))
            self.assertIn("could not be fully verified", activity.activity_summary(value)[0])


if __name__ == "__main__":
    unittest.main()
