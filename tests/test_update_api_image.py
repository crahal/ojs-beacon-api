from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import update_api_image as rollout


OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
MYSQL = "sha256:" + "c" * 64


class FakeRunner:
    def __init__(self, root, descriptors=()):
        self.root, self.descriptors = root, descriptors
        self.calls = []
        self.current = OLD
        self.tags = {"ojs_api-ojs-api": OLD, "ojs-api:reviewed": NEW}
        self.mysql_started = "synthetic-start"
        self.failure = None
        self.up_calls = 0
        self.configuration = {
            "name": "ojs_api",
            "services": {
                "mysql": {"environment": {"MYSQL_ROOT_PASSWORD": "private-root-value"}},
                "ojs-api": {
                    "environment": {
                        "OJS_PUBLIC_BASE_URL": "https://api.example.org",
                        "OJS_API_CREDENTIALS_FILE": "/run/secrets/ojs-api-client.env",
                    },
                    "ports": [{"target": 8000, "published": "8000", "host_ip": "127.0.0.1", "protocol": "tcp"}],
                    "volumes": [{"type": "bind", "source": str(root / ".secrets/api-client.env"),
                                 "target": "/run/secrets/ojs-api-client.env", "read_only": True}],
                },
            },
        }

    def run(self, command, timeout=30):
        self.calls.append(command)
        if command[:2] == ["docker", "compose"]:
            tail = command[8:]
            if tail == ["config", "--quiet"]:
                return ""
            if tail == ["config", "--format", "json"]:
                return json.dumps(self.configuration)
            if tail[:2] == ["ps", "--quiet"]:
                return "mysql-id" if tail[-1] == "mysql" else "api-id"
            if tail[0] == "up":
                self.up_calls += 1
                self.current = self.tags["ojs_api-ojs-api"]
                if self.failure == "first-up" and self.up_calls == 1:
                    raise rollout.RolloutError("Private child details suppressed.")
                if self.failure == "all-up":
                    raise rollout.RolloutError("Private child details suppressed.")
                if self.failure == "mysql-restarted":
                    self.mysql_started = "unexpected-new-start"
                return ""
        if command[:3] == ["docker", "image", "inspect"]:
            return self.tags[command[-1]]
        if command[:3] == ["docker", "image", "tag"]:
            source, destination = command[-2:]
            self.tags[destination] = self.tags.get(source, source)
            return ""
        if command[:2] == ["docker", "inspect"]:
            mysql = command[-1] == "mysql-id"
            values = [command[-1], MYSQL if mysql else self.current,
                      "mysql:8.4.11" if mysql else "ojs_api-ojs-api",
                      self.mysql_started if mysql else "synthetic-api-start", True, "healthy",
                      "mysql" if mysql else "ojs-api"]
            return "\n".join(json.dumps(value) for value in values)
        if "check_api_history.py" in command[2]:
            return json.dumps({"status": "passed"})
        raise AssertionError("Unexpected fixture command")


class APIImageRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="ojs-image-rollout-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("data/raw", "data/clean/mysql-2026-10-01", ".secrets"):
            (self.root / name).mkdir(parents=True)
        (self.root / "data/clean/mysql-live").symlink_to("mysql-2026-10-01")
        for name in (".env", "docker-compose.yml", "data/.updates-paused",
                     "data/raw/.source-activity.lock", "data/clean/.automatic-update.lock",
                     "data/clean/.pipeline.lock", "data/clean/.publish-live.lock"):
            (self.root / name).touch(mode=0o600)
        self.credentials = self.root / ".secrets/api-client.env"
        self.credentials.write_text("OJS_API_USERNAME=fixture-client\nOJS_API_KEY=private-http-value\n")
        self.credentials.chmod(0o600)
        self.argv = ["--project-root", str(self.root), "--prebuilt-image", "ojs-api:reviewed",
                     "--snapshot", "2026-01-01", "--snapshot", "2026-07-01", "--snapshot", "2026-10-01"]
        self.args = rollout.parser().parse_args(self.argv)
        self.runner = FakeRunner(self.root)
        self.checker = mock.patch.object(rollout, "check_api", return_value=[{"stable": True}] * 2).start()
        self.factory = mock.patch.object(rollout, "Runner", return_value=self.runner).start()
        self.addCleanup(mock.patch.stopall)

    def up_commands(self):
        return [command for command in self.runner.calls if command[:2] == ["docker", "compose"] and "up" in command]

    def test_success_only_recreates_api_keeps_pause_and_retains_rollback_image(self):
        self.assertEqual(rollout.rollout(self.args)["status"], "updated")
        self.assertEqual(self.runner.current, NEW)
        self.assertEqual(self.runner.tags["ojs_api-ojs-api"], NEW)
        self.assertTrue(any(key.startswith("ojs-api-rollback:") and value == OLD for key, value in self.runner.tags.items()))
        self.assertTrue((self.root / "data/.updates-paused").is_file())
        self.assertEqual(len(self.up_commands()), 1)
        up = self.up_commands()[0]
        self.assertEqual(up[-1], "ojs-api")
        for required in ("--no-deps", "--no-build", "--force-recreate", "--wait"):
            self.assertIn(required, up)
        self.assertEqual(up[up.index("--pull") + 1], "never")
        for command in self.runner.calls:
            for prohibited in ("build", "download", "prune", "stop", "restart", "down"):
                self.assertNotIn(prohibited, command)
            self.assertNotIn("private-http-value", repr(command))
            self.assertNotIn("private-root-value", repr(command))

    def test_failed_recreation_restores_and_verifies_previous_api(self):
        self.runner.failure = "first-up"
        with self.assertRaisesRegex(rollout.RolloutError, "previous API image was restored and verified"):
            rollout.rollout(self.args)
        self.assertEqual(self.runner.current, OLD)
        self.assertEqual(self.runner.tags["ojs_api-ojs-api"], OLD)
        self.assertEqual(len(self.up_commands()), 2)

    def test_failed_validation_rolls_back(self):
        self.checker.side_effect = [[{"stable": True}] * 2, RuntimeError("private-response"), [{"stable": True}] * 2]
        with self.assertRaisesRegex(rollout.RolloutError, "restored and verified"):
            rollout.rollout(self.args)
        self.assertEqual(self.runner.current, OLD)

    def test_interrupt_after_change_restores_and_verifies_previous_image(self):
        self.checker.side_effect = [[{"stable": True}] * 2, KeyboardInterrupt(), [{"stable": True}] * 2]
        with self.assertRaisesRegex(rollout.RolloutError, "restored and verified"):
            rollout.rollout(self.args)
        self.assertEqual(self.runner.current, OLD)

    def test_unhealthy_initial_api_prevents_mutation(self):
        self.checker.side_effect = RuntimeError("private-response")
        with self.assertRaises(RuntimeError):
            rollout.rollout(self.args)
        self.assertFalse(self.up_commands())
        self.assertFalse(any(command[:3] == ["docker", "image", "tag"] for command in self.runner.calls))

    def test_changed_authenticated_metadata_rolls_back(self):
        self.checker.side_effect = [[{"stable": True}] * 2, [{"changed": True}] * 2, [{"stable": True}] * 2]
        with self.assertRaisesRegex(rollout.RolloutError, "restored and verified"):
            rollout.rollout(self.args)
        self.assertEqual(self.runner.current, OLD)

    def test_failed_rollback_reports_manual_recovery_without_details(self):
        self.runner.failure = "all-up"
        with self.assertRaisesRegex(rollout.RolloutError, "verified rollback failed") as caught:
            rollout.rollout(self.args)
        self.assertNotIn("private", str(caught.exception).replace("private deployment", ""))
        self.assertTrue((self.root / "data/.updates-paused").exists())

    def test_mysql_restart_is_detected_and_never_performed_by_helper(self):
        self.runner.failure = "mysql-restarted"
        with self.assertRaisesRegex(rollout.RolloutError, "verified rollback failed"):
            rollout.rollout(self.args)
        self.assertTrue(all(command[-1] == "ojs-api" for command in self.up_commands()))

    def test_current_image_is_verified_without_mutating_tags_or_containers(self):
        self.runner.tags["ojs-api:reviewed"] = OLD
        self.assertEqual(rollout.rollout(self.args)["status"], "unchanged")
        self.assertFalse(self.up_commands())
        self.assertFalse(any(command[:3] == ["docker", "image", "tag"] for command in self.runner.calls))

    def test_all_four_busy_locks_fail_before_docker(self):
        for name in ("data/raw/.source-activity.lock", "data/clean/.automatic-update.lock",
                     "data/clean/.pipeline.lock", "data/clean/.publish-live.lock"):
            with self.subTest(lock=name), (self.root / name).open() as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(RuntimeError):
                    rollout.rollout(self.args)
        self.factory.assert_not_called()

    def test_locks_remain_held_during_rollout_and_are_passed_to_children(self):
        def inspect_held(root, descriptors):
            self.assertEqual(len(descriptors), 4)
            for name in ("data/raw/.source-activity.lock", "data/clean/.automatic-update.lock",
                         "data/clean/.pipeline.lock", "data/clean/.publish-live.lock"):
                with (root / name).open() as independent:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(independent.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self.runner
        self.factory.side_effect = inspect_held
        rollout.rollout(self.args)

    def test_pause_and_no_recovery_journal_are_required(self):
        marker = self.root / "data/.updates-paused"
        marker.unlink()
        with self.assertRaises(FileNotFoundError):
            rollout.rollout(self.args)
        marker.touch()
        journal = self.root / "data/clean/.compact-update.json"
        journal.symlink_to("missing-journal")
        with self.assertRaisesRegex(rollout.RolloutError, "recovery is pending"):
            rollout.rollout(self.args)
        self.factory.assert_not_called()

    def test_public_environment_permissions_and_symlink_locks_rejected(self):
        (self.root / ".env").chmod(0o644)
        with self.assertRaisesRegex(rollout.RolloutError, "owner-private"):
            rollout.rollout(self.args)
        (self.root / ".env").chmod(0o600)
        lock = self.root / "data/clean/.pipeline.lock"
        lock.unlink()
        lock.symlink_to(self.root / ".env")
        with self.assertRaises(rollout.RolloutError):
            rollout.rollout(self.args)
        self.factory.assert_not_called()

    def test_unsafe_image_reference_and_unsorted_history_rejected(self):
        for image in ("--help", "image; echo private", "image name", "$(command)"):
            self.args.prebuilt_image = image
            with self.assertRaises(rollout.RolloutError):
                rollout.rollout(self.args)
        self.args.prebuilt_image = "ojs-api:reviewed"
        self.args.snapshot.reverse()
        with self.assertRaises(rollout.RolloutError):
            rollout.rollout(self.args)
        self.factory.assert_not_called()

    def test_nonfinite_or_excessive_timeout_is_rejected_before_docker(self):
        for timeout in (float("nan"), float("inf"), 0, 901):
            with self.subTest(timeout=timeout):
                self.args.health_timeout = timeout
                with self.assertRaises(rollout.RolloutError):
                    rollout.rollout(self.args)
        self.factory.assert_not_called()

    def test_mismatched_compose_image_is_not_silently_overwritten(self):
        self.runner.configuration["services"]["ojs-api"]["image"] = "other:tag"
        with self.assertRaisesRegex(rollout.RolloutError, "differs"):
            rollout.rollout(self.args)
        self.assertFalse(self.up_commands())

    def test_external_plain_http_or_public_backend_bind_is_rejected(self):
        service = self.runner.configuration["services"]["ojs-api"]
        service["environment"]["OJS_PUBLIC_BASE_URL"] = "http://api.example.org"
        with self.assertRaises(RuntimeError):
            rollout.rollout(self.args)
        service["environment"]["OJS_PUBLIC_BASE_URL"] = "https://api.example.org"
        service["ports"][0]["host_ip"] = "0.0.0.0"
        with self.assertRaises(rollout.RolloutError):
            rollout.rollout(self.args)
        self.assertFalse(self.up_commands())

    def test_main_never_prints_arbitrary_exception_details(self):
        with mock.patch.object(rollout, "rollout", side_effect=ValueError("private-root-value private-host")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(rollout.main(self.argv), 1)
        self.assertNotIn("private-root-value", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("private-host", stdout.getvalue() + stderr.getvalue())


class APIImageChecksTests(unittest.TestCase):
    def test_check_api_uses_private_file_and_both_origins_with_full_history(self):
        runner = FakeRunner(Path("/synthetic-project"))
        metadata = {"latest_snapshot": {"snapshot_date": "2026-10-01"}, "high_watermark_event_id": 10}
        client = mock.Mock()
        client.get.side_effect = [{"status": "ok"}, {}, metadata] * 2
        with mock.patch.object(rollout.api_check, "credentials", return_value=("fixture", "private-token")), \
                mock.patch.object(rollout.api_check, "Client", return_value=client) as client_factory:
            self.assertEqual(rollout.check_api(runner, ["http://127.0.0.1:8000", "https://api.example.org"],
                                             Path("/synthetic-private-file"), ["2026-10-01"], 180), [metadata] * 2)
        self.assertEqual(client_factory.call_count, 2)
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all("--snapshot" in command and "--reports-dir" in command for command in runner.calls))
        self.assertNotIn("private-token", repr(runner.calls))

    def test_runner_redacts_timeout_child_stderr_and_removes_exported_overrides(self):
        with mock.patch.dict(os.environ, {"MYSQL_PWD": "private", "MYSQL_ROOT_PASSWORD": "private",
                                          "OJS_PUBLIC_BASE_URL": "private", "COMPOSE_FILE": "private"}):
            runner = rollout.Runner(Path("/synthetic-project"), (8, 9, 10, 11))
        self.assertTrue(all(key not in runner.environment for key in (
            "MYSQL_PWD", "MYSQL_ROOT_PASSWORD", "OJS_PUBLIC_BASE_URL", "COMPOSE_FILE")))
        for failure in (subprocess.TimeoutExpired(["private-command"], 30, stderr="private-stderr"),
                        OSError("private-path")):
            with mock.patch.object(rollout.subprocess, "run", side_effect=failure), \
                    self.assertRaises(rollout.RolloutError) as caught:
                runner.run(["docker", "synthetic-command"])
            self.assertNotIn("private-", str(caught.exception))
        completed = subprocess.CompletedProcess(["docker"], 1, "private-stdout", "private-stderr")
        with mock.patch.object(rollout.subprocess, "run", return_value=completed) as execute, \
                self.assertRaises(rollout.RolloutError):
            runner.run(["docker", "synthetic-command"])
        self.assertEqual(execute.call_args.kwargs["pass_fds"], (8, 9, 10, 11))


if __name__ == "__main__":
    unittest.main()
