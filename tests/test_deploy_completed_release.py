from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy_completed_release as deployment


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clean = self.root / "clean"
        self.raw = self.root / "raw"
        self.clean.mkdir()
        self.raw.mkdir()
        self.identity = self.root / "identity"
        self.credentials = self.root / "private.env"
        self.identity.write_text("not-a-real-private-key")
        self.credentials.write_text("MYSQL_ROOT_PASSWORD=do-not-log-this-secret\n")
        self.identity.chmod(0o600)
        self.credentials.chmod(0o600)
        self.argv = ["--source-clean", str(self.clean), "--source-raw", str(self.raw),
                     "--credentials-env", str(self.credentials), "--identity", str(self.identity),
                     "--host", "ubuntu@203.0.113.10", "--snapshot", "2026-10-01",
                     "--history", "2026-01-01", "--history", "2026-07-01",
                     "--history", "2026-10-01", "--public-base-url", "https://203.0.113.10",
                     "--native-only-source"]
        self.args = deployment.parser().parse_args(self.argv)

    def test_ordered_stages_and_success_is_not_online(self):
        with mock.patch.object(deployment, "run_stage") as stage:
            result = deployment.deploy(self.args)
        self.assertEqual([call.args[0] for call in stage.call_args_list],
                         ["transfer-release", "ship-private-credential", "queue-remote-publication"])
        transfer, credential, remote = [call.args[1] for call in stage.call_args_list]
        self.assertEqual(transfer[:2], [sys.executable, "-B"])
        self.assertEqual(credential[:2], [sys.executable, "-B"])
        self.assertIn("--native-only-source", transfer)
        self.assertIn("--reserve-gib", transfer)
        self.assertEqual(result["status"], "publication_started")
        self.assertFalse(result["api_verified"])
        self.assertTrue(result["updates_paused"])
        self.assertEqual(result["remote_unit"], "ojs-api-publish-2026-10-01.service")
        commands = repr(stage.call_args_list)
        self.assertNotIn("do-not-log-this-secret", commands)
        for forbidden in ("download_beacon", "run_pipeline", "--unpause", "--prune", "--skip-transfer"):
            self.assertNotIn(forbidden, commands)

    def test_strict_ssh_and_durable_remote_budget(self):
        origin = deployment.validate_plan(self.args)
        unit, ssh = deployment.remote_publication(self.args, origin)
        self.assertEqual(ssh[:3], ["ssh", "-F", "/dev/null"])
        for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes"):
            self.assertIn(option, ssh)
        command = shlex.split(ssh[-1])
        self.assertEqual(command[:3], ["sudo", "-n", "systemd-run"])
        for option in ("--property=Type=exec", "--property=MemoryHigh=1792M",
                       "--property=MemoryMax=2048M", "--property=MemorySwapMax=1024M",
                       "--property=KillMode=mixed", "--property=TimeoutStopSec=infinity",
                       "--property=RequiresMountsFor=/srv/ojs_api/data",
                       "--property=EnvironmentFile=/srv/ojs_api/.env", "--property=UMask=0077"):
            self.assertIn(option, command)
        self.assertNotIn("--wait", command)
        self.assertNotIn("--no-block", command)
        self.assertIn("/srv/ojs_api/scripts/publish_imported_release.py", command)
        self.assertEqual(command.count("--history-snapshot"), 3)
        self.assertIn("/srv/ojs_api/.secrets/incoming-root-password-2026-10-01", command)

    def test_failure_at_each_stage_never_runs_following_stages(self):
        for index in range(3):
            with self.subTest(index=index), mock.patch.object(deployment, "run_stage") as stage:
                stage.side_effect = [None] * index + [deployment.DeploymentError("test failure")]
                with self.assertRaises(deployment.DeploymentError):
                    deployment.deploy(self.args)
                self.assertEqual(stage.call_count, index + 1)

    def test_finish_only_still_uses_validated_transfer_helper(self):
        self.args.finish_only = True
        with mock.patch.object(deployment, "run_stage") as stage:
            deployment.deploy(self.args)
        self.assertIn("--finish-only", stage.call_args_list[0].args[1])
        self.assertEqual(stage.call_count, 3)

    def test_multiple_raw_lock_directories_preserved(self):
        second = self.root / "other raw"
        second.mkdir()
        self.args.source_raw.append(second)
        with mock.patch.object(deployment, "run_stage") as stage:
            deployment.deploy(self.args)
        transfer = stage.call_args_list[0].args[1]
        self.assertEqual(transfer.count("--source-raw"), 2)
        self.assertIn(str(second), transfer)

    def test_invalid_hosts_fail_before_any_work(self):
        for host in ("-oProxyCommand=bad", "ubuntu@host;touch /tmp/bad", "ubuntu@$(whoami)", "host", "u@-host"):
            self.args.host = host
            with self.subTest(host=host), mock.patch.object(deployment, "run_stage") as stage:
                with self.assertRaises(deployment.DeploymentError):
                    deployment.deploy(self.args)
                stage.assert_not_called()

    def test_unsafe_or_other_remote_project_is_rejected(self):
        for root in (Path("/tmp/elsewhere"), Path("/srv/ojs_api; touch /tmp/bad"), Path("/")):
            self.args.remote_project = root
            with self.assertRaises(deployment.DeploymentError):
                deployment.validate_plan(self.args)

    def test_history_must_be_unique_ordered_and_end_at_release(self):
        for history in (["2026-07-01", "2026-01-01", "2026-10-01"],
                        ["2026-01-01"], ["2026-10-01", "2026-10-01"]):
            self.args.history_snapshot = history
            with self.assertRaises(deployment.DeploymentError):
                deployment.validate_plan(self.args)

    def test_at_least_twenty_gib_reserve(self):
        self.args.reserve_gib = 19
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_plan(self.args)

    def test_https_origin_required_and_credentials_never_accepted(self):
        for value in ("http://127.0.0.1", "https://user:secret@host", "https://host/a",
                      "https://host/?key=secret", "https://host\ncommand"):
            self.args.public_base_url = value
            with self.subTest(value=value), self.assertRaises((deployment.DeploymentError,
                                                             deployment.api_check.CheckError)):
                deployment.validate_plan(self.args)

    def test_secret_and_identity_permissions_are_checked_early(self):
        for path in (self.credentials, self.identity):
            path.chmod(0o644)
            with self.assertRaises(deployment.ship.TransferError):
                deployment.validate_plan(self.args)
            path.chmod(0o600)

    def test_invalid_credential_file_fails_before_long_transfer(self):
        self.credentials.write_text("unrelated=value\n")
        with mock.patch.object(deployment, "run_stage") as stage:
            with self.assertRaisesRegex(deployment.DeploymentError, "credential is invalid"):
                deployment.deploy(self.args)
        stage.assert_not_called()

    def test_symlink_source_is_rejected(self):
        link = self.root / "linked-clean"
        link.symlink_to(self.clean, target_is_directory=True)
        self.args.source_clean = link
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_plan(self.args)

    def test_runner_logs_only_stage_and_uses_own_process_group(self):
        process = mock.Mock(pid=987654)
        process.wait.return_value = 0
        process.poll.return_value = 0
        output = io.StringIO()
        with mock.patch.object(deployment.subprocess, "Popen", return_value=process) as popen, redirect_stdout(output):
            deployment.run_stage("example", ["sensitive-looking-argument"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("sensitive-looking-argument", output.getvalue())
        self.assertEqual([json.loads(line)["event"] for line in output.getvalue().splitlines()],
                         ["started", "completed"])

    def test_runner_nonzero_is_failure(self):
        process = mock.Mock(pid=987654)
        process.wait.return_value = 12
        process.poll.return_value = 12
        with mock.patch.object(deployment.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()):
            with self.assertRaises(deployment.DeploymentError):
                deployment.run_stage("example", ["fixture"])

    def test_signal_terminates_group_waits_and_does_not_complete(self):
        process = mock.Mock(pid=987654)
        process.poll.side_effect = [None, -15]
        handlers = {}

        def install(number, handler):
            previous = handlers.get(number)
            handlers[number] = handler
            return previous

        def wait():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return -15

        process.wait.side_effect = wait
        output = io.StringIO()
        with mock.patch.object(deployment.subprocess, "Popen", return_value=process), \
                mock.patch.object(deployment.signal, "signal", side_effect=install), \
                mock.patch.object(deployment.os, "killpg") as kill, redirect_stdout(output):
            with self.assertRaisesRegex(deployment.DeploymentError, "interrupted"):
                deployment.run_stage("example", ["fixture"])
        kill.assert_called_once_with(process.pid, signal.SIGTERM)
        process.wait.assert_called_once()
        self.assertNotIn("completed", output.getvalue())

    def test_main_does_not_expose_exception_details_or_secrets(self):
        output = io.StringIO()
        with mock.patch.object(deployment, "deploy", side_effect=ValueError("private-secret")), \
                redirect_stderr(output):
            self.assertEqual(deployment.main(self.argv), 1)
        self.assertNotIn("private-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
