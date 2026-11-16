"""Offline private-client issuance with synthetic keys and temporary files only."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import manage_api_clients as manage
from api_clients import read_registry


SYNTHETIC_KEY = "ab" * 32


class ManageAPIClientsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="ojs-client-manager-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "clients.json"
        self.output = self.root / "issued-secret.txt"
        original_token_hex = manage.secrets.token_hex
        self.random = mock.patch.object(manage.secrets, "token_hex", side_effect=
            lambda size: SYNTHETIC_KEY if size == 32 else original_token_hex(size))
        self.random.start()
        self.addCleanup(self.random.stop)

    def initialize(self):
        manage.initialize_registry(self.registry)

    def document(self):
        return read_registry(self.registry)

    def write_private(self, path, payload):
        path.write_bytes(payload if isinstance(payload, bytes) else payload.encode())
        path.chmod(0o600)

    def invitation(self, username="tester01"):
        path = self.root / (username + "-invitation.txt")
        self.write_private(path, "Hello,\nUsername: [CONFIRMED_USERNAME_FOR_"
                           + username.upper() + "]\nAPI key: " + manage.KEY_PLACEHOLDER
                           + "\nKeep this private.\n")
        return path

    def run_main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = manage.main(list(arguments))
        combined = stdout.getvalue() + stderr.getvalue()
        for value in (SYNTHETIC_KEY, str(self.root), "sensitive exception detail"):
            self.assertNotIn(value, combined)
        return result, combined

    def test_init_creates_private_empty_registry_and_lock_without_overwrite(self):
        self.initialize()
        self.assertEqual(self.document(), {"version": 1, "clients": []})
        before = (self.registry.read_bytes(), self.registry.stat().st_ino)
        for path in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
        result, _ = self.run_main("init", "--registry", str(self.registry))
        self.assertEqual(result, 1)
        self.assertEqual((self.registry.read_bytes(), self.registry.stat().st_ino), before)

    def test_issue_writes_only_hash_to_registry_and_exclusive_private_key_file(self):
        self.initialize()
        result, output = self.run_main("issue", "--registry", str(self.registry),
                                     "--username", "tester01", "--output", str(self.output))
        self.assertEqual(result, 0, output)
        self.assertEqual(self.output.read_text(), SYNTHETIC_KEY + "\n")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotIn(SYNTHETIC_KEY, self.registry.read_text())
        self.assertEqual(self.document()["clients"], [{"username": "tester01",
            "key_sha256": hashlib.sha256(SYNTHETIC_KEY.encode()).hexdigest(),
            "enabled": True, "expires_at": None}])

    def test_invitation_replaces_only_matching_placeholders_without_separate_key_file(self):
        self.initialize()
        invitation = self.invitation()
        expected = invitation.read_text().replace("[CONFIRMED_USERNAME_FOR_TESTER01]", "tester01")
        expected = expected.replace(manage.KEY_PLACEHOLDER, SYNTHETIC_KEY)
        result, output = self.run_main("issue", "--registry", str(self.registry),
                                     "--username", "tester01", "--invitation", str(invitation))
        self.assertEqual(result, 0, output)
        self.assertEqual(invitation.read_text(), expected)
        self.assertEqual(stat.S_IMODE(invitation.stat().st_mode), 0o600)
        self.assertEqual({path.name for path in self.root.iterdir()},
                         {"clients.json", "clients.json.lock", invitation.name})
        self.assertNotIn(SYNTHETIC_KEY, self.registry.read_text())

    def test_ten_invitation_issues_preserve_prior_clients_and_have_distinct_synthetic_keys(self):
        self.initialize()
        original = manage.secrets.token_hex
        counter = iter(range(1, 11))
        with mock.patch.object(manage.secrets, "token_hex", side_effect=
                lambda size: f"{next(counter):064x}" if size == 32 else original(size)):
            for index in range(1, 11):
                username = f"tester{index:02d}"
                invitation = self.invitation(username)
                manage.issue_client(self.registry, username, invitation=invitation)
                self.assertIn(f"{index:064x}", invitation.read_text())
        clients = self.document()["clients"]
        self.assertEqual(len(clients), 10)
        self.assertEqual(len({client["key_sha256"] for client in clients}), 10)
        self.assertEqual(len(list(self.root.iterdir())), 12)

    def test_revoke_changes_only_requested_user_and_is_idempotent(self):
        self.initialize()
        manage.issue_client(self.registry, "tester01", output=self.output)
        original = manage.secrets.token_hex
        with mock.patch.object(manage.secrets, "token_hex", side_effect=
                lambda size: "cd" * 32 if size == 32 else original(size)):
            manage.issue_client(self.registry, "tester02", output=self.root / "other-secret.txt")
        before = self.document()
        manage.revoke_client(self.registry, "tester01")
        after = self.document()
        before["clients"][0]["enabled"] = False
        self.assertEqual(after, before)
        inode = self.registry.stat().st_ino
        manage.revoke_client(self.registry, "tester01")
        self.assertEqual(self.registry.stat().st_ino, inode)
        self.assertEqual(self.output.read_text(), SYNTHETIC_KEY + "\n")

    def test_unknown_revoke_preserves_registry(self):
        self.initialize()
        before = self.registry.read_bytes()
        result, _ = self.run_main("revoke", "--registry", str(self.registry), "--username", "absent")
        self.assertEqual(result, 1)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_duplicate_and_reserved_names_never_overwrite_or_issue_another_key(self):
        self.initialize()
        manage.issue_client(self.registry, "tester01", output=self.output)
        before = self.registry.read_bytes()
        for name, options in (("tester01", []), ("operator", ["--reserved-username", "operator"])):
            with self.subTest(name=name):
                candidate = self.root / "not-created.txt"
                result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", name,
                                         "--output", str(candidate), *options)
                self.assertEqual(result, 1)
                self.assertFalse(candidate.exists())
                self.assertEqual(self.registry.read_bytes(), before)

    def test_duplicate_generated_digest_fails_before_writing_key(self):
        self.initialize()
        manage.issue_client(self.registry, "tester01", output=self.output)
        before = self.registry.read_bytes()
        destination = self.root / "not-created.txt"
        result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester02",
                                 "--output", str(destination))
        self.assertEqual(result, 1)
        self.assertFalse(destination.exists())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_valid_future_utc_expiry_and_invalid_or_past_expiry(self):
        self.initialize()
        manage.issue_client(self.registry, "tester01", output=self.output,
                            expires_at="2099-01-01T00:00:00Z")
        self.assertEqual(self.document()["clients"][0]["expires_at"], "2099-01-01T00:00:00Z")
        before = self.registry.read_bytes()
        for expiry in ("2000-01-01T00:00:00Z", "2099-01-01", "2099-01-01T00:00:00+01:00",
                       "2099-01-01T00:00:00", "2099-99-99T00:00:00Z"):
            with self.subTest(expiry=expiry):
                result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester02",
                                         "--output", str(self.root / "not-created.txt"), "--expires-at", expiry)
                self.assertEqual(result, 1)
                self.assertEqual(self.registry.read_bytes(), before)
                self.assertFalse((self.root / "not-created.txt").exists())

    def test_existing_output_is_never_overwritten(self):
        self.initialize()
        self.write_private(self.output, "keep existing secret")
        before = self.registry.read_bytes()
        result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                 "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertEqual(self.output.read_text(), "keep existing secret")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_wrong_missing_or_repeated_invitation_placeholders_are_rejected(self):
        self.initialize()
        invitation = self.invitation()
        for text in ("no placeholders", "[CONFIRMED_USERNAME_FOR_TESTER02] " + manage.KEY_PLACEHOLDER,
                     "[CONFIRMED_USERNAME_FOR_TESTER01] " + manage.KEY_PLACEHOLDER * 2,
                     "[CONFIRMED_USERNAME_FOR_TESTER01] " * 2 + manage.KEY_PLACEHOLDER):
            with self.subTest(text=text):
                self.write_private(invitation, text)
                result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                         "--invitation", str(invitation))
                self.assertEqual(result, 1)
                self.assertEqual(invitation.read_text(), text)
                self.assertEqual(self.document()["clients"], [])

    def test_registry_and_lock_cannot_be_key_destinations(self):
        self.initialize()
        before = self.registry.read_bytes()
        for target in (self.registry, self.root / "clients.json.lock"):
            result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                     "--output", str(target))
            self.assertEqual(result, 1)
            self.assertEqual(self.registry.read_bytes(), before)

    def test_nonprivate_parent_and_symlink_ancestor_are_rejected(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o755)
        self.assertEqual(self.run_main("init", "--registry", str(unsafe / "clients.json"))[0], 1)
        self.assertEqual(list(unsafe.iterdir()), [])
        real = self.root / "private"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.run_main("init", "--registry", str(alias / "clients.json"))[0], 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_shared_ancestor_with_private_parent_is_allowed(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o777)
        private = shared / "private"
        private.mkdir(mode=0o700)
        manage.initialize_registry(private / "clients.json")
        self.assertEqual(read_registry(private / "clients.json")["clients"], [])

    def test_insecure_symlink_hardlink_and_oversized_registries_are_rejected(self):
        for kind in ("permissions", "symlink", "hardlink", "oversized", "fifo"):
            with self.subTest(kind=kind), TemporaryDirectory(dir=self.root) as temporary:
                directory = Path(temporary)
                registry = directory / "clients.json"
                if kind == "fifo":
                    os.mkfifo(registry, 0o600)
                else:
                    self.write_private(registry, '{"version":1,"clients":[]}')
                    if kind == "permissions":
                        registry.chmod(0o644)
                    elif kind == "symlink":
                        real = directory / "original"
                        registry.rename(real)
                        registry.symlink_to(real)
                    elif kind == "hardlink":
                        os.link(registry, directory / "linked")
                    else:
                        self.write_private(registry, b" " * (manage.MAX_REGISTRY_BYTES + 1))
                result, _ = self.run_main("issue", "--registry", str(registry), "--username", "tester01",
                                         "--output", str(directory / "not-created.txt"))
                self.assertEqual(result, 1)
                self.assertFalse((directory / "not-created.txt").exists())

    def test_foreign_owner_is_rejected(self):
        self.initialize()
        before = self.registry.read_bytes()
        with mock.patch.object(manage.os, "geteuid", return_value=os.geteuid() + 1):
            result, _ = self.run_main("revoke", "--registry", str(self.registry), "--username", "tester01")
        self.assertEqual(result, 1)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_duplicate_json_keys_and_malformed_registry_are_preserved(self):
        for payload in ('{"version":1,"version":1,"clients":[]}',
                        '{"version":1,"clients":[],"unexpected":true}', '{invalid'):
            with self.subTest(payload=payload):
                self.write_private(self.registry, payload)
                result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                         "--output", str(self.output))
                self.assertEqual(result, 1)
                self.assertEqual(self.registry.read_text(), payload)
                self.assertFalse(self.output.exists())

    def test_held_lock_refuses_concurrent_issuer_without_changes(self):
        self.initialize()
        descriptor = os.open(self.root / "clients.json.lock", os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = self.registry.read_bytes()
        result, output = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                      "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertIn("active", output)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_unsafe_lock_does_not_change_registry_or_its_target(self):
        self.initialize()
        lock = self.root / "clients.json.lock"
        lock.unlink()
        self.write_private(self.output, "preserve")
        lock.symlink_to(self.output)
        before = self.registry.read_bytes()
        result, _ = self.run_main("revoke", "--registry", str(self.registry), "--username", "tester01")
        self.assertEqual(result, 1)
        self.assertEqual(self.output.read_text(), "preserve")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_failed_output_write_removes_own_partial_key_and_preserves_registry(self):
        self.initialize()
        before = self.registry.read_bytes()
        with mock.patch.object(manage.os, "write", side_effect=OSError("sensitive exception detail")):
            result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                     "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_failed_registry_replace_removes_key_or_restores_invitation(self):
        for invitation_mode in (False, True):
            with self.subTest(invitation=invitation_mode), TemporaryDirectory(dir=self.root) as temporary:
                directory = Path(temporary)
                registry = directory / "clients.json"
                manage.initialize_registry(registry)
                original_registry = registry.read_bytes()
                target = directory / "destination.txt"
                original_invitation = "[CONFIRMED_USERNAME_FOR_TESTER01] " + manage.KEY_PLACEHOLDER
                if invitation_mode:
                    self.write_private(target, original_invitation)
                original_replace = os.replace

                def fail_registry(source, destination, **options):
                    if destination == registry.name:
                        raise OSError("sensitive exception detail")
                    return original_replace(source, destination, **options)

                with mock.patch.object(manage.os, "replace", side_effect=fail_registry):
                    result, _ = self.run_main("issue", "--registry", str(registry), "--username", "tester01",
                                             "--invitation" if invitation_mode else "--output", str(target))
                self.assertEqual(result, 1)
                self.assertEqual(registry.read_bytes(), original_registry)
                if invitation_mode:
                    self.assertEqual(target.read_text(), original_invitation)
                else:
                    self.assertFalse(target.exists())
                self.assertFalse(any(path.name.startswith(".api-clients-") for path in directory.iterdir()))

    def test_registry_post_commit_fsync_failure_preserves_issued_key(self):
        self.initialize()
        original_replace, original_fsync = os.replace, os.fsync
        committed = False

        def mark_commit(source, destination, **options):
            nonlocal committed
            result = original_replace(source, destination, **options)
            committed = destination == self.registry.name
            return result

        def fail_after_commit(descriptor):
            if committed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("sensitive exception detail")
            return original_fsync(descriptor)

        with mock.patch.object(manage.os, "replace", side_effect=mark_commit), \
                mock.patch.object(manage.os, "fsync", side_effect=fail_after_commit):
            result, output = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                          "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertIn("may have committed", output)
        self.assertEqual(self.output.read_text(), SYNTHETIC_KEY + "\n")
        self.assertEqual(self.document()["clients"][0]["username"], "tester01")

    def test_interrupt_after_registry_rename_preserves_authorized_key(self):
        self.initialize()
        original = os.replace

        def interrupt_after_rename(source, destination, **options):
            original(source, destination, **options)
            if destination == self.registry.name:
                raise KeyboardInterrupt

        with mock.patch.object(manage.os, "replace", side_effect=interrupt_after_rename):
            result, output = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                          "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertIn("may have committed", output)
        self.assertEqual(self.output.read_text(), SYNTHETIC_KEY + "\n")
        self.assertEqual(self.document()["clients"][0]["username"], "tester01")

    def test_interrupt_after_successful_registry_store_preserves_authorized_invitation(self):
        self.initialize()
        invitation = self.invitation()
        original = manage.atomic_store

        def interrupt_after_store(directory, name, payload, expected):
            result = original(directory, name, payload, expected)
            if name == self.registry.name:
                raise KeyboardInterrupt
            return result

        with mock.patch.object(manage, "atomic_store", side_effect=interrupt_after_store):
            result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                     "--invitation", str(invitation))
        self.assertEqual(result, 1)
        self.assertIn(SYNTHETIC_KEY, invitation.read_text())
        self.assertEqual(self.document()["clients"][0]["username"], "tester01")

    def test_interrupt_after_invitation_rename_restores_unissued_invitation(self):
        self.initialize()
        invitation = self.invitation()
        before = invitation.read_bytes()
        original = os.replace
        interrupted = False

        def interrupt_once(source, destination, **options):
            nonlocal interrupted
            original(source, destination, **options)
            if destination == invitation.name and not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        with mock.patch.object(manage.os, "replace", side_effect=interrupt_once):
            result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                     "--invitation", str(invitation))
        self.assertEqual(result, 1)
        self.assertEqual(invitation.read_bytes(), before)
        self.assertEqual(self.document()["clients"], [])

    def test_invitation_permissions_hardlinks_and_size_are_checked_before_mutation(self):
        self.initialize()
        for kind in ("permissions", "symlink", "hardlink", "oversized"):
            with self.subTest(kind=kind), TemporaryDirectory(dir=self.root) as temporary:
                target = Path(temporary) / "invitation.txt"
                self.write_private(target, "[CONFIRMED_USERNAME_FOR_TESTER01] " + manage.KEY_PLACEHOLDER)
                if kind == "permissions":
                    target.chmod(0o644)
                elif kind == "symlink":
                    real = target.with_name("original")
                    target.rename(real)
                    target.symlink_to(real)
                elif kind == "hardlink":
                    os.link(target, target.with_name("linked"))
                else:
                    self.write_private(target, b" " * (manage.MAX_INVITATION_BYTES + 1))
                before = target.read_bytes()
                result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                         "--invitation", str(target))
                self.assertEqual(result, 1)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(self.document()["clients"], [])

    def test_competing_output_creation_is_preserved(self):
        self.initialize()
        original = manage.exclusive_key_output

        def compete(directory, name, payload):
            self.write_private(self.output, "competing file")
            return original(directory, name, payload)

        with mock.patch.object(manage, "exclusive_key_output", side_effect=compete):
            result, _ = self.run_main("issue", "--registry", str(self.registry), "--username", "tester01",
                                     "--output", str(self.output))
        self.assertEqual(result, 1)
        self.assertEqual(self.output.read_text(), "competing file")
        self.assertEqual(self.document()["clients"], [])

    def test_invalid_arguments_are_redacted(self):
        result, output = self.run_main("issue", "--registry", str(self.registry),
                                      "--unknown-sensitive-option", "sensitive exception detail")
        self.assertEqual(result, 1)
        self.assertIn("Invalid arguments", output)


if __name__ == "__main__":
    unittest.main()
