from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import configure_progress_email as email_setup
import configure_progress_api as configure


class ConfigureProgressAPITest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ojs-api"
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(email_setup, "CONFIG_DIR", self.path))
        self.patches.enter_context(mock.patch.object(configure.os, "geteuid", return_value=0))
        original_fstat, original_stat = os.fstat, os.stat

        def root_owned(function):
            def wrapped(*args, **kwargs):
                information = list(function(*args, **kwargs))
                information[4:6] = [0, 0]
                return os.stat_result(information)
            return wrapped

        self.patches.enter_context(mock.patch.object(configure.os, "fstat", side_effect=root_owned(original_fstat)))
        self.patches.enter_context(mock.patch.object(configure.os, "stat", side_effect=root_owned(original_stat)))
        self.patches.enter_context(mock.patch.object(configure.os, "fchown"))

    def run_main(self, origin="https://api.example.org"):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = configure.main(["--base-url", origin])
        combined = output.getvalue() + error.getvalue()
        self.assertNotIn(origin, combined)
        return result, combined

    def test_creates_only_private_origin_and_never_credentials(self):
        result, output = self.run_main()
        self.assertEqual(result, 0, output)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        target = self.path / configure.ENVIRONMENT_FILE
        self.assertEqual(list(self.path.iterdir()), [target])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(), "OJS_PROGRESS_API_BASE_URL=https://api.example.org\n")
        self.assertIn("No credentials were read", output)

    def test_same_configuration_is_idempotent_without_replacing_inode(self):
        self.assertEqual(self.run_main()[0], 0)
        target = self.path / configure.ENVIRONMENT_FILE
        before = target.stat().st_ino
        result, output = self.run_main("https://api.example.org/")
        self.assertEqual(result, 0, output)
        self.assertEqual(target.stat().st_ino, before)
        self.assertIn("already matches", output)

    def test_different_existing_configuration_is_not_overwritten(self):
        self.assertEqual(self.run_main()[0], 0)
        target = self.path / configure.ENVIRONMENT_FILE
        before = target.read_bytes()
        result, output = self.run_main("https://other.example.org")
        self.assertEqual(result, 1)
        self.assertIn("refusing to overwrite", output)
        self.assertEqual(target.read_bytes(), before)

    def test_root_required_before_creating_files(self):
        with mock.patch.object(configure.os, "geteuid", return_value=1000):
            result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertIn("root", output)
        self.assertFalse(self.path.exists())

    def test_invalid_origin_never_creates_configuration(self):
        for origin in ("http://api.example.org", "https://u:key@api.example.org",
                       "https://api.example.org/path", "https://api.example.org?x=y",
                       "https://api.example.org#fragment", "https://api.example.org\nOTHER=yes",
                       "https://api.example.org;id", "https://$(whoami).example.org",
                       "https://api.example.org\\test", " https://api.example.org"):
            with self.subTest(origin=origin):
                result, _ = self.run_main(origin)
                self.assertEqual(result, 1)
                self.assertFalse(self.path.exists())

    def test_symlink_directory_refused_without_altering_target(self):
        destination = Path(self.temporary.name) / "actual"
        destination.mkdir(mode=0o700)
        self.path.symlink_to(destination, target_is_directory=True)
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(list(destination.iterdir()), [])

    def test_symlink_file_is_refused_without_touching_referent(self):
        self.path.mkdir(mode=0o700)
        destination = Path(self.temporary.name) / "private"
        destination.write_text("preserve me")
        (self.path / configure.ENVIRONMENT_FILE).symlink_to(destination)
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(destination.read_text(), "preserve me")

    def test_insecure_or_hardlinked_file_is_refused(self):
        self.assertEqual(self.run_main()[0], 0)
        target = self.path / configure.ENVIRONMENT_FILE
        target.chmod(0o644)
        self.assertEqual(self.run_main()[0], 1)
        target.chmod(0o600)
        os.link(target, self.path / "linked")
        self.assertEqual(self.run_main()[0], 1)

    def test_failed_creation_removes_only_own_partial_file(self):
        with mock.patch.object(configure.os, "write", side_effect=OSError("private error details")):
            result, output = self.run_main()
        self.assertEqual(result, 1)
        self.assertNotIn("private error details", output)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_exclusive_creation_preserves_a_competing_file(self):
        self.path.mkdir(mode=0o700)
        target = self.path / configure.ENVIRONMENT_FILE
        original_open = os.open

        def competing_open(name, flags, *args, **kwargs):
            if name == configure.ENVIRONMENT_FILE and flags & os.O_CREAT:
                target.write_text("competing configuration")
            return original_open(name, flags, *args, **kwargs)

        with mock.patch.object(configure.os, "open", side_effect=competing_open):
            result, _ = self.run_main()
        self.assertEqual(result, 1)
        self.assertEqual(target.read_text(), "competing configuration")


class ProgressAPIDeploymentTest(unittest.TestCase):
    def test_dropin_only_adds_origin_and_readonly_systemd_credential(self):
        lines = (PROJECT_ROOT / "deploy/ojs-api-progress-email-api.conf").read_text().splitlines()
        actual = [line for line in lines if line and not line.startswith("#")]
        self.assertEqual(actual, [
            "[Service]",
            "EnvironmentFile=/etc/ojs-api/progress-api.env",
            "LoadCredential=api-client.env:/srv/ojs_api/.secrets/api-client.env",
        ])
        for name in ("ojs-api-progress-email.service", "ojs-api-progress-email@.service"):
            unit = (PROJECT_ROOT / "deploy" / name).read_text()
            self.assertIn("/srv/ojs_api/.env /srv/ojs_api/.secrets", unit)

    def test_preview_is_bounded_readonly_without_paid_ai_or_email(self):
        script = PROJECT_ROOT / "scripts/preview_progress_report.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        arguments = shlex.split(script.read_text(), comments=True)
        properties = dict(token.removeprefix("--property=").split("=", 1)
                          for token in arguments if token.startswith("--property="))
        self.assertEqual(properties["User"], "ojs")
        self.assertEqual(properties["EnvironmentFile"], "/etc/ojs-api/progress-api.env")
        self.assertEqual(properties["LoadCredential"], "api-client.env:/srv/ojs_api/.secrets/api-client.env")
        self.assertEqual(properties["Environment"], "OJS_PROGRESS_LLM_ENABLED=0")
        self.assertEqual(properties["ProtectSystem"], "strict")
        self.assertEqual(properties["CapabilityBoundingSet"], "")
        self.assertEqual(properties["SupplementaryGroups"], "systemd-journal")
        self.assertIn("/srv/ojs_api/.env /srv/ojs_api/.secrets", properties["InaccessiblePaths"])
        self.assertNotIn("StateDirectory", properties)
        self.assertNotIn("ReadWritePaths", properties)
        self.assertLessEqual(int(properties["TimeoutStartSec"]), 180)
        self.assertEqual(properties["MemoryMax"], "96M")
        self.assertIn("--dry-run", arguments)
        self.assertNotIn("--preview-llm", arguments)
        self.assertNotIn("--force", arguments)
        self.assertNotIn("gmail-app-password", script.read_text())
        self.assertNotIn("openai-api-key", script.read_text())
        self.assertNotIn("systemctl", arguments)

    def test_instructions_cover_primary_and_subscriber_without_timer_changes(self):
        text = (PROJECT_ROOT / "docs/OPERATIONS.md").read_text()
        section = text.split("### Observe the live API", 1)[1].split("### ", 1)[0]
        self.assertIn("ojs-api-progress-email.service.d", section)
        self.assertIn("ojs-api-progress-email@.service.d", section)
        self.assertIn("preview_progress_report.sh", section)
        self.assertIn("daemon-reload", section)
        self.assertNotIn("enable --now", section)
        self.assertNotIn("--force", section)
        self.assertNotIn("systemctl start", section)


if __name__ == "__main__":
    unittest.main()
