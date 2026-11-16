from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import install_automatic_updates as install


class FakeSystemd:
    def __init__(self):
        self.calls = []
        self.updater = "LoadState=loaded\nActiveState=failed\nUnitFileState=static\n"
        self.timer = "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
        self.reload_code = 0

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[1] == "show":
            output = self.timer if command[2].endswith(".timer") else self.updater
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == ["/usr/bin/systemctl", "daemon-reload"]:
            return subprocess.CompletedProcess(command, self.reload_code, "", "private diagnostics")
        raise AssertionError(f"unexpected operation: {command}")


class InstallAutomaticUpdatesTest(unittest.TestCase):
    def setUp(self):
        prior_umask = os.umask(0o022)
        self.addCleanup(os.umask, prior_umask)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.layout = install.Layout(Path(self.temporary.name))
        for path in (self.layout.service.parent, self.layout.active_cron.parent,
                     self.layout.project / "deploy", self.layout.project / "data"):
            path.mkdir(parents=True, exist_ok=True)
        for name in ("ojs-api-update.service", "ojs-api.cron"):
            (self.layout.project / "deploy" / name).write_bytes(
                (PROJECT_ROOT / "deploy" / name).read_bytes())
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.account = SimpleNamespace(pw_uid=self.uid or 1000, pw_gid=self.gid or 1000)
        self.lookup = mock.patch.object(install.pwd, "getpwnam", return_value=self.account)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.systemd = FakeSystemd()
        self.installer = install.Installer(self.layout, root_uid=self.uid,
                                           root_gid=self.gid, run=self.systemd)

    def assert_not_prepared(self):
        self.assertFalse(self.layout.pause.exists())
        self.assertFalse(self.layout.service.exists())
        self.assertFalse(self.layout.parked_cron.exists())

    def test_prepares_paused_without_starting_or_enabling_anything(self):
        result = self.installer.prepare()
        self.assertEqual(result, {"service": "created", "parked_cron": "created",
                                  "pause": "created", "enabled": False})
        self.assertFalse(self.layout.active_cron.exists())
        self.assertEqual(self.layout.pause.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(self.layout.pause.stat().st_mode), 0o600)
        self.assertEqual(self.layout.pause.stat().st_uid, self.account.pw_uid)
        for path in (self.layout.service, self.layout.parked_cron):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(path.stat().st_uid, self.uid)
        self.assertEqual(self.systemd.calls[-1], ["/usr/bin/systemctl", "daemon-reload"])
        self.assertTrue(all(command[1] in {"show", "daemon-reload"} for command in self.systemd.calls))

    def test_idempotence_preserves_inodes_pause_contents_and_dropins(self):
        self.installer.prepare()
        self.layout.pause.write_text("operator reason: preserve me\n")
        override = self.layout.service.parent / "ojs-api-update.service.d/50-recovery.conf"
        override.parent.mkdir()
        override.write_text("[Unit]\nRefuseManualStart=yes\n")
        before = {path: path.stat().st_ino for path in
                  (self.layout.pause, self.layout.service, self.layout.parked_cron, override)}
        self.systemd.calls.clear()
        result = self.installer.prepare()
        self.assertEqual(result["service"], "already matching")
        self.assertEqual(result["pause"], "preserved")
        self.assertEqual(before, {path: path.stat().st_ino for path in before})
        self.assertEqual(self.layout.pause.read_text(), "operator reason: preserve me\n")
        self.assertEqual(override.read_text(), "[Unit]\nRefuseManualStart=yes\n")
        self.assertFalse(any(command[1] == "daemon-reload" for command in self.systemd.calls))

    def test_requires_root_before_inspecting_or_writing(self):
        with mock.patch.object(install.os, "geteuid", return_value=self.uid + 100):
            with self.assertRaisesRegex(install.InstallationError, "root"):
                self.installer.prepare()
        self.assertEqual(self.systemd.calls, [])
        self.assert_not_prepared()

    def test_requires_nonroot_existing_deployment_account(self):
        for value in (KeyError("ojs"), SimpleNamespace(pw_uid=0, pw_gid=0)):
            with self.subTest(value=value):
                context = (mock.patch.object(install.pwd, "getpwnam", side_effect=value)
                           if isinstance(value, Exception) else
                           mock.patch.object(install.pwd, "getpwnam", return_value=value))
                with context, self.assertRaises(install.InstallationError):
                    self.installer.prepare()
                self.assert_not_prepared()

    def test_refuses_different_existing_service_without_any_mutation(self):
        self.layout.service.write_text("[Service]\nExecStart=/custom/operator-job\n")
        before = self.layout.service.read_bytes()
        with self.assertRaisesRegex(install.InstallationError, "refusing to overwrite"):
            self.installer.prepare()
        self.assertEqual(self.layout.service.read_bytes(), before)
        self.assertFalse(self.layout.pause.exists())
        self.assertFalse(self.layout.parked_cron.exists())

    def test_refuses_different_parked_cron_before_installing_service(self):
        self.layout.parked_cron.write_text("keep my parked schedule")
        with self.assertRaisesRegex(install.InstallationError, "refusing to overwrite"):
            self.installer.prepare()
        self.assertEqual(self.layout.parked_cron.read_text(), "keep my parked schedule")
        self.assertFalse(self.layout.service.exists())
        self.assertFalse(self.layout.pause.exists())

    def test_refuses_existing_active_cron_including_broken_link(self):
        for broken_link in (False, True):
            with self.subTest(broken_link=broken_link):
                if broken_link:
                    self.layout.active_cron.symlink_to(self.layout.root / "missing")
                else:
                    self.layout.active_cron.write_text("operator schedule")
                try:
                    with self.assertRaisesRegex(install.InstallationError, "active cron"):
                        self.installer.prepare()
                    self.assertTrue(os.path.lexists(self.layout.active_cron))
                    self.assert_not_prepared()
                finally:
                    self.layout.active_cron.unlink()

    def test_refuses_active_or_changing_update_job(self):
        for state in ("active", "activating", "deactivating", "reloading", "unknown"):
            with self.subTest(state=state):
                self.systemd.updater = f"LoadState=loaded\nActiveState={state}\nUnitFileState=static\n"
                with self.assertRaisesRegex(install.InstallationError, "updater is active"):
                    self.installer.prepare()
                self.assert_not_prepared()

    def test_refuses_active_or_enabled_timer_without_stopping_it(self):
        for state, enabled in (("active", "disabled"), ("inactive", "enabled"),
                               ("inactive", "enabled-runtime"), ("activating", "disabled")):
            with self.subTest(state=state, enabled=enabled):
                self.systemd.timer = f"LoadState=loaded\nActiveState={state}\nUnitFileState={enabled}\n"
                with self.assertRaisesRegex(install.InstallationError, "timer is active or enabled"):
                    self.installer.prepare()
                self.assert_not_prepared()
        self.assertTrue(all(command[1] == "show" for command in self.systemd.calls))

    def test_failed_or_unknown_systemd_inspection_is_fail_closed(self):
        for result in (subprocess.CompletedProcess([], 1, "", "secret diagnostics"),
                       subprocess.CompletedProcess([], 0, "LoadState=error\n", "")):
            with self.subTest(result=result):
                self.installer.run = mock.Mock(return_value=result)
                with self.assertRaises(install.InstallationError) as caught:
                    self.installer.prepare()
                self.assertNotIn("secret diagnostics", str(caught.exception))
                self.assert_not_prepared()

    def test_systemd_timeout_is_sanitized(self):
        self.installer.run = mock.Mock(side_effect=subprocess.TimeoutExpired(["private argument"], 1))
        with self.assertRaises(install.InstallationError) as caught:
            self.installer.prepare()
        self.assertNotIn("private argument", str(caught.exception))
        self.assert_not_prepared()

    def test_unsafe_directory_is_refused(self):
        directory = self.layout.project / "data"
        directory.chmod(0o777)
        with self.assertRaisesRegex(install.InstallationError, "unsafe directory"):
            self.installer.prepare()
        self.assert_not_prepared()

    def test_symlink_ancestor_is_refused(self):
        directory = self.layout.project / "data"
        directory.rmdir()
        target = self.layout.root / "actual-data"
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_target_is_refused_without_touching_referent(self):
        target = self.layout.root / "private-file"
        target.write_text("private data")
        self.layout.pause.symlink_to(target)
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        self.assertEqual(target.read_text(), "private data")
        self.assertFalse(self.layout.service.exists())

    def test_hardlinked_or_writable_file_is_refused(self):
        source = self.layout.project / "deploy/ojs-api.cron"
        os.link(source, self.layout.root / "linked-cron")
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        (self.layout.root / "linked-cron").unlink()
        source.chmod(0o666)
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        self.assert_not_prepared()

    def test_nonregular_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.layout.pause)
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        self.assertFalse(self.layout.service.exists())

    def test_missing_template_does_not_prepare_partial_configuration(self):
        (self.layout.project / "deploy/ojs-api.cron").unlink()
        with self.assertRaises(install.InstallationError):
            self.installer.prepare()
        self.assert_not_prepared()

    def test_pause_is_created_before_service_or_parked_cron(self):
        original = self.installer.create
        created = []

        def record(path, *args, **kwargs):
            created.append(path)
            original(path, *args, **kwargs)

        with mock.patch.object(self.installer, "create", side_effect=record):
            self.installer.prepare()
        self.assertEqual(created[0], self.layout.pause)

    def test_creation_race_never_overwrites_competing_file(self):
        original_open = install.os.open

        def race(path, flags, *args, **kwargs):
            if path == self.layout.pause and flags & os.O_CREAT:
                path.write_text("operator created this first")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(install.os, "open", side_effect=race):
            with self.assertRaises(install.InstallationError):
                self.installer.prepare()
        self.assertEqual(self.layout.pause.read_text(), "operator created this first")
        self.assertFalse(self.layout.service.exists())

    def test_failed_create_removes_only_its_partial_file(self):
        with mock.patch.object(install.os, "fchown", side_effect=OSError("private failure")):
            with self.assertRaises(install.InstallationError) as caught:
                self.installer.prepare()
        self.assertNotIn("private failure", str(caught.exception))
        self.assert_not_prepared()

    def test_failed_reload_leaves_configuration_paused_and_never_enabled(self):
        self.systemd.reload_code = 1
        with self.assertRaisesRegex(install.InstallationError, "staged and paused"):
            self.installer.prepare()
        self.assertTrue(self.layout.pause.is_file())
        self.assertTrue(self.layout.service.is_file())
        self.assertTrue(self.layout.parked_cron.is_file())
        self.assertFalse(self.layout.active_cron.exists())

    def test_cli_refuses_enable_and_arbitrary_root(self):
        for arguments in (["--enable"], ["--project-root", "/different/root"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    install.main(arguments)
                self.assertEqual(caught.exception.code, 2)

    def test_cli_reports_preparation_without_claiming_readiness(self):
        with mock.patch.object(install, "Installer", return_value=self.installer):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(install.main([]), 0)
        self.assertIn("No job was started", output.getvalue())
        self.assertIn("--readiness", output.getvalue())
        self.assertIn("cron parked", output.getvalue())

    def test_wrapper_is_syntax_valid_and_uses_system_python(self):
        wrapper = PROJECT_ROOT / "scripts/install_automatic_updates.sh"
        subprocess.run(["bash", "-n", str(wrapper)], check=True)
        text = wrapper.read_text()
        self.assertIn("exec /usr/bin/python3 -B", text)
        self.assertNotIn("sudo", text)
        self.assertNotIn(".env", text)


if __name__ == "__main__":
    unittest.main()
