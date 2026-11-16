from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_imported_release as handoff


class ImportedHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data" / "clean").mkdir(parents=True)
        (self.root / "data" / ".updates-paused").touch()
        (self.root / "scripts").mkdir()
        for name in ("adopt_candidate_credentials.py", "check_api_history.py", "automatic_update.sh"):
            (self.root / "scripts" / name).touch()
        self.args = handoff.parser().parse_args([
            "--project-root", str(self.root), "--snapshot", "2026-10-01",
            "--history-snapshot", "2026-01-01", "--history-snapshot", "2026-07-01",
            "--history-snapshot", "2026-10-01", "--public-base-url", "https://203.0.113.10",
            "--temporary-root-password-file", str(self.root / "temporary-secret"),
        ])
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "is_mount", return_value=True).start()
        mock.patch.object(handoff.api_check, "credentials", return_value=("private-user", "private-key")).start()
        self.actual_runner = handoff.run_stage
        self.stage = mock.patch.object(handoff, "run_stage").start()
        self.live = mock.patch.object(handoff.update, "live_directory", side_effect=[None, self.candidate]).start()

    @property
    def candidate(self):
        return self.root / "data" / "clean" / "mysql-2026-10-01"

    def test_runs_adopt_exact_publish_and_both_checks_without_unpausing(self):
        result = handoff.handoff(self.args)
        self.assertEqual(result["status"], "passed")
        self.assertEqual([call.args[0] for call in self.stage.call_args_list],
                         ["adopt-credentials", "publish-release", "verify-loopback", "verify-public-https"])
        publish = self.stage.call_args_list[1].args[1]
        self.assertIn("--publish-release", publish)
        self.assertIn("2026-10-01", publish)
        self.assertIn("--no-prune", publish)
        self.assertIn("20", publish)
        self.assertNotIn("private-key", repr(self.stage.call_args_list))
        self.assertTrue((self.root / "data" / ".updates-paused").is_file())

    def test_retry_after_live_switch_never_rekeys_live_database(self):
        self.live.side_effect = [self.candidate, self.candidate]
        handoff.handoff(self.args)
        self.assertEqual(self.stage.call_args_list[0].args[0], "publish-release")

    def test_success_exit_without_live_switch_is_not_success(self):
        self.live.side_effect = [None, None]
        with self.assertRaisesRegex(handoff.HandoffError, "did not select"):
            handoff.handoff(self.args)
        self.assertEqual(self.stage.call_count, 2)

    def test_stage_failure_stops_remaining_work(self):
        self.stage.side_effect = handoff.HandoffError("failed")
        with self.assertRaises(handoff.HandoffError):
            handoff.handoff(self.args)
        self.assertEqual(self.stage.call_count, 1)

    def test_pause_is_required(self):
        (self.root / "data" / ".updates-paused").unlink()
        with self.assertRaises(handoff.HandoffError):
            handoff.handoff(self.args)
        self.stage.assert_not_called()

    def test_mount_is_required(self):
        with mock.patch.object(Path, "is_mount", return_value=False):
            with self.assertRaises(handoff.HandoffError):
                handoff.handoff(self.args)

    def test_requires_ordered_complete_history_ending_at_release(self):
        for history in (["2026-10-01", "2026-07-01"], ["2026-01-01"],
                        ["2026-10-01", "2026-10-01"]):
            self.args.history_snapshot = history
            with self.assertRaises(handoff.HandoffError):
                handoff.handoff(self.args)
        self.stage.assert_not_called()

    def test_public_plain_http_rejected(self):
        self.args.public_base_url = "http://127.0.0.1:8000"
        with self.assertRaises(handoff.HandoffError):
            handoff.handoff(self.args)

    def test_invalid_port_rejected(self):
        with mock.patch.dict(handoff.os.environ, {"OJS_API_PORT": "invalid"}):
            with self.assertRaises(handoff.HandoffError):
                handoff.handoff(self.args)

    def test_runner_propagates_nonzero_and_never_logs_command(self):
        process = mock.Mock()
        process.wait.return_value = 1
        with mock.patch.object(handoff.subprocess, "Popen", return_value=process), \
                mock.patch("builtins.print") as output:
            with self.assertRaisesRegex(handoff.HandoffError, "step failed"):
                self.actual_runner("test", ["private-looking-argument"], self.root)
        self.assertNotIn("private-looking-argument", repr(output.call_args_list))


if __name__ == "__main__":
    unittest.main()
