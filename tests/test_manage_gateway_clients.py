"""Gateway revocation checks using synthetic hashes and mocked Caddy only."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import manage_gateway_clients as gateway


OPERATOR_HASH = "$2b$12$" + "a" * 53
FIRST_HASH = "$2b$12$" + "b" * 53
SECOND_HASH = "$2b$12$" + "c" * 53
ACCOUNTS = (gateway.MARKER + "monitor\n"
            + "\tmonitor " + OPERATOR_HASH + "\n"
            + "  tester01\t" + FIRST_HASH + "\n"
            + "tester02 " + SECOND_HASH + "\n").encode()


class GatewaySchemaTests(unittest.TestCase):
    def adapted(self, payload=ACCOUNTS):
        _, names, lines = gateway.parse_users(payload)
        accounts = [{"username": username,
                     "password": gateway.ACCOUNT.fullmatch(lines[index].removesuffix("\n"))[2]}
                    for username, index in names.items()]
        return {"apps": {"http": {"servers": {"fixture": {"routes": [{"handle": [{
            "handler": "authentication", "providers": {"http_basic": {
                "accounts": accounts, "hash": {"algorithm": "bcrypt"}}}}]}]}}}}}

    def test_effective_account_binding_accepts_only_exact_single_provider(self):
        document = self.adapted()
        gateway.validate_effective_accounts(json.dumps(document).encode(), ACCOUNTS)
        updated = gateway.revoked_payload(ACCOUNTS, "tester01")
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_effective_accounts(json.dumps(document).encode(), updated)
        for value in ({}, {"nested": [document, document]},
                      {"handler": "authentication", "providers": {"other": {}}}):
            with self.assertRaises(gateway.GatewayError):
                gateway.validate_effective_accounts(json.dumps(value).encode(), ACCOUNTS)

    def test_effective_binding_rejects_extra_missing_or_changed_accounts_and_oversize(self):
        for change in ("extra", "missing", "hash", "duplicate", "algorithm"):
            with self.subTest(change=change):
                document = self.adapted()
                provider = document["apps"]["http"]["servers"]["fixture"]["routes"][0]["handle"][0]["providers"]["http_basic"]
                if change == "extra":
                    provider["accounts"].append({"username": "other", "password": "$2b$12$" + "d" * 53})
                elif change == "missing":
                    provider["accounts"].pop()
                elif change == "hash":
                    provider["accounts"][0]["password"] = "$2b$12$" + "d" * 53
                elif change == "duplicate":
                    provider["accounts"].append(provider["accounts"][0])
                else:
                    provider["hash"]["algorithm"] = "other"
                with self.assertRaises(gateway.GatewayError):
                    gateway.validate_effective_accounts(json.dumps(document).encode(), ACCOUNTS)
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_effective_accounts(b" " * (gateway.MAX_ADAPTED_BYTES + 1), ACCOUNTS)

    def test_revocation_preserves_operator_and_other_entries_exactly(self):
        expected = ACCOUNTS.replace(("  tester01\t" + FIRST_HASH + "\n").encode(), b"")
        self.assertEqual(gateway.revoked_payload(ACCOUNTS, "tester01"), expected)

    def test_final_external_client_can_be_removed_but_operator_cannot(self):
        first = gateway.revoked_payload(ACCOUNTS, "tester01")
        final = gateway.revoked_payload(first, "tester02")
        self.assertEqual(set(gateway.parse_users(final)[1]), {"monitor"})
        with self.assertRaises(gateway.GatewayError):
            gateway.revoked_payload(final, "monitor")

    def test_malformed_markers_entries_duplicates_and_directives_are_rejected(self):
        cases = [
            ACCOUNTS.replace((gateway.MARKER + "monitor\n").encode(), b""),
            (gateway.MARKER + "monitor\n").encode() + ACCOUNTS,
            ACCOUNTS.replace(b"monitor ", b"other "),
            ACCOUNTS + ("tester01 " + SECOND_HASH + "\n").encode(),
            ACCOUNTS + ("tester03 " + FIRST_HASH + "\n").encode(),
            ACCOUNTS + b"import other-file\n",
            ACCOUNTS + b"basic_auth {\n",
            ACCOUNTS + b"# an unsupported comment\n",
            ACCOUNTS.replace(b"$2b$12$", b"$2b$03$"),
            ACCOUNTS.replace(b"$2b$12$", b"$2b$32$"),
            ACCOUNTS.replace(b"tester01", b"bad/user"),
            ACCOUNTS.replace(b"\n", b"\r\n"),
            ACCOUNTS[:-1], b"\xff\n", b" " * (gateway.MAX_USERS_BYTES + 1),
        ]
        for value in cases:
            with self.subTest(value=value[:50]), self.assertRaises(gateway.GatewayError):
                gateway.parse_users(value)

    def test_absent_or_invalid_username_never_changes_payload(self):
        for username in ("absent", "bad name", "../../other", "", "x" * 65):
            with self.subTest(username=username), self.assertRaises(gateway.GatewayError):
                gateway.revoked_payload(ACCOUNTS, username)


class ManageGatewayClientsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="ojs-gateway-manager-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.users = self.root / "users.caddy"
        self.config = self.root / "Caddyfile"
        self.users.write_bytes(ACCOUNTS)
        self.users.chmod(0o640)
        self.config.write_text("# synthetic configuration; Caddy is always mocked\n")
        self.config.chmod(0o640)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(gateway.os, "geteuid", return_value=0))
        self.stack.enter_context(mock.patch.object(gateway.grp, "getgrnam",
                                                   return_value=SimpleNamespace(gr_gid=os.getegid())))
        self.service_account = SimpleNamespace(pw_uid=42, pw_gid=os.getegid(), pw_dir=str(self.root))
        self.stack.enter_context(mock.patch.object(gateway.pwd, "getpwnam", return_value=self.service_account))
        self.real_socket_validator = gateway.validate_admin_socket
        self.socket_validator = self.stack.enter_context(mock.patch.object(gateway, "validate_admin_socket",
                                                                           return_value=(1, 2, 3, 4)))
        self.effective = self.stack.enter_context(mock.patch.object(gateway, "assert_effective_accounts"))
        self.chown = self.stack.enter_context(mock.patch.object(gateway.os, "fchown"))
        actual_stat, actual_fstat = os.stat, os.fstat

        def root_owned(function):
            def wrapped(*args, **kwargs):
                info = function(*args, **kwargs)
                fields = {name: getattr(info, name) for name in dir(info)
                          if name.startswith("st_") and name != "st_uid"}
                return SimpleNamespace(**fields, st_uid=0)
            return wrapped

        self.stack.enter_context(mock.patch.object(gateway.os, "stat", side_effect=root_owned(actual_stat)))
        self.stack.enter_context(mock.patch.object(gateway.os, "fstat", side_effect=root_owned(actual_fstat)))
        self.run = self.stack.enter_context(mock.patch.object(gateway.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b"private config", stderr=b"private details")))

    def call_main(self, username="tester01", *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = gateway.main(["--username", username, "--users-file", str(self.users),
                                   "--config", str(self.config), *extra])
        output = stdout.getvalue() + stderr.getvalue()
        for value in (str(self.root), OPERATOR_HASH, FIRST_HASH, SECOND_HASH,
                      "private config", "private details"):
            self.assertNotIn(value, output)
        if username.startswith("tester"):
            self.assertNotIn(username, output)
        return result, output

    def operations(self):
        return [call.args[0][1] for call in self.run.call_args_list]

    def assert_original(self):
        self.assertEqual(self.users.read_bytes(), ACCOUNTS)
        self.assertEqual(stat.S_IMODE(self.users.stat().st_mode), 0o640)

    def test_success_uses_only_validate_and_graceful_local_reload(self):
        result, output = self.call_main()
        self.assertEqual(result, 0, output)
        self.assertEqual(self.users.read_bytes(), gateway.revoked_payload(ACCOUNTS, "tester01"))
        self.assertEqual(self.operations(), ["validate", "reload"])
        self.assertEqual(stat.S_IMODE(self.users.stat().st_mode), 0o640)
        self.chown.assert_called_with(mock.ANY, 0, os.getegid())
        for call in self.run.call_args_list:
            arguments = call.args[0]
            self.assertEqual(arguments[0], "/usr/bin/caddy")
            self.assertEqual(arguments[2:6], ["--config", str(self.config), "--adapter", "caddyfile"])
            self.assertEqual(call.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(call.kwargs["stderr"], subprocess.PIPE)
            self.assertEqual(call.kwargs["timeout"], gateway.COMMAND_TIMEOUT)
            self.assertEqual(call.kwargs["cwd"], "/")
            self.assertFalse(call.kwargs["check"])
            if arguments[1] == "validate":
                self.assertEqual(call.kwargs["user"], self.service_account.pw_uid)
                self.assertEqual(call.kwargs["group"], self.service_account.pw_gid)
                self.assertEqual(call.kwargs["extra_groups"], [])
                self.assertEqual(call.kwargs["env"]["HOME"], self.service_account.pw_dir)
        self.assertEqual(self.run.call_args_list[1].args[0][-2:], ["--address", gateway.DEFAULT_ADMIN])
        self.assertFalse(any(path.name.startswith(".gateway-users-") for path in self.root.iterdir()))

    def test_root_is_required_before_opening_or_reloading(self):
        with mock.patch.object(gateway.os, "geteuid", return_value=1234):
            result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("root", output)
        self.assert_original()
        self.run.assert_not_called()
        self.assertFalse((self.root / "users.caddy.lock").exists())

    def test_operator_absent_client_and_bad_schema_fail_without_reload(self):
        for username in ("monitor", "absent"):
            result, _ = self.call_main(username)
            self.assertEqual(result, 1)
            self.assert_original()
        self.users.write_bytes(ACCOUNTS + b"reverse_proxy arbitrary\n")
        result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.run.assert_not_called()

    def test_remote_or_unsafe_admin_addresses_are_rejected(self):
        self.socket_validator.side_effect = self.real_socket_validator
        for address in ("https://example.org", "localhost:2019", "unix//run/../other.sock", "unix//run/socket?x"):
            with self.subTest(address=address):
                result, _ = self.call_main("tester01", "--admin", address)
                self.assertEqual(result, 1)
                self.assert_original()
        self.run.assert_not_called()

    def test_unused_account_file_is_rejected_before_any_write_or_reload(self):
        self.effective.side_effect = gateway.GatewayError("The account file does not match the effective gateway configuration.")
        result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.assert_original()
        self.run.assert_not_called()

    def test_adaptation_mismatch_after_edit_rolls_back(self):
        self.effective.side_effect = [None, gateway.GatewayError("Effective account mismatch."), None]
        result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.assert_original()
        self.assertEqual(self.operations(), ["validate", "reload"])

    def test_adapt_is_private_and_runs_as_service_user(self):
        document = GatewaySchemaTests().adapted()
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(document).encode(), b"private details")
        gateway.validate_effective_accounts(gateway.caddy_command(
            "adapt", self.config, gateway.DEFAULT_ADMIN, self.service_account), ACCOUNTS)
        self.assertEqual(self.operations(), ["adapt"])
        self.assertEqual(self.run.call_args.kwargs["user"], self.service_account.pw_uid)
        self.assertEqual(self.run.call_args.kwargs["group"], self.service_account.pw_gid)

    def test_root_only_config_is_rejected_as_unreadable_by_service(self):
        self.config.chmod(0o600)
        self.assertEqual(self.call_main()[0], 1)
        self.assert_original()
        self.run.assert_not_called()

    def test_untrusted_or_changed_admin_socket_never_receives_configuration(self):
        self.socket_validator.side_effect = gateway.GatewayError("The admin socket is unsafe.")
        self.assertEqual(self.call_main()[0], 1)
        self.assert_original()
        self.run.assert_not_called()
        self.socket_validator.side_effect = [(1, 2, 3, 4), (1, 2, 3, 5), (1, 2, 3, 5)]
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("rollback is not confirmed", output)
        self.assert_original()
        self.assertEqual(self.operations(), ["validate"])

    def test_socket_validator_requires_private_trusted_parent_and_actual_socket(self):
        template = dict(vars(self.users.stat()))
        for case in ("valid", "public-parent", "foreign-parent", "regular-file", "symlink", "foreign-socket"):
            parent = SimpleNamespace(**{**template, "st_mode": stat.S_IFDIR | 0o700,
                                        "st_uid": self.service_account.pw_uid})
            socket = SimpleNamespace(**{**template, "st_mode": stat.S_IFSOCK | 0o755,
                                        "st_uid": self.service_account.pw_uid, "st_nlink": 1})
            if case == "public-parent":
                parent.st_mode = stat.S_IFDIR | 0o755
            elif case == "foreign-parent":
                parent.st_uid = self.service_account.pw_uid + 1
            elif case == "regular-file":
                socket.st_mode = stat.S_IFREG | 0o600
            elif case == "symlink":
                socket.st_mode = stat.S_IFLNK | 0o777
            elif case == "foreign-socket":
                socket.st_uid = self.service_account.pw_uid + 1
            with self.subTest(case=case), mock.patch.object(gateway.os, "open", return_value=42), \
                    mock.patch.object(gateway.os, "close"), \
                    mock.patch.object(gateway.os, "fstat", return_value=parent), \
                    mock.patch.object(gateway.os, "stat", return_value=socket):
                if case == "valid":
                    self.real_socket_validator(gateway.DEFAULT_ADMIN, self.service_account.pw_uid)
                else:
                    with self.assertRaises(gateway.GatewayError):
                        self.real_socket_validator(gateway.DEFAULT_ADMIN, self.service_account.pw_uid)

    def test_validate_failure_restores_original_and_reloads_it(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 1, b"private config", b"private details"),
                                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("preserved or restored", output)
        self.assert_original()
        self.assertEqual(self.operations(), ["validate", "validate", "reload"])

    def test_reload_failure_restores_original_and_reloads_it(self):
        self.run.side_effect = [subprocess.CompletedProcess([], code) for code in (0, 1, 0, 0)]
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("preserved or restored", output)
        self.assert_original()
        self.assertEqual(self.operations(), ["validate", "reload", "validate", "reload"])

    def test_timeout_and_interrupt_restore_original(self):
        for error in (subprocess.TimeoutExpired(["private details"], 30), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.run.reset_mock()
                self.run.side_effect = [subprocess.CompletedProcess([], 0), error,
                                        subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
                result, _ = self.call_main()
                self.assertEqual(result, 1)
                self.assert_original()
                self.assertEqual(self.operations(), ["validate", "reload", "validate", "reload"])

    def test_failed_rollback_reload_is_reported_without_child_details(self):
        self.run.side_effect = [subprocess.CompletedProcess([], code, b"private config", b"private details")
                                for code in (0, 1, 0, 1)]
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("rollback is not confirmed", output)
        self.assert_original()

    def test_failed_write_preserves_original_without_reloading(self):
        with mock.patch.object(gateway.os, "write", side_effect=OSError("private details")):
            result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.assert_original()
        self.run.assert_not_called()
        self.assertFalse(any(path.name.startswith(".gateway-users-") for path in self.root.iterdir()))

    def test_interrupt_after_rename_is_detected_and_original_restored(self):
        original_replace = os.replace
        interrupted = False

        def interrupt_once(*args, **kwargs):
            nonlocal interrupted
            original_replace(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt

        with mock.patch.object(gateway.os, "replace", side_effect=interrupt_once):
            result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.assert_original()
        self.assertEqual(self.operations(), ["validate", "reload"])

    def test_changed_account_file_is_not_overwritten_during_rollback(self):
        unexpected = (gateway.MARKER + "monitor\nmonitor " + OPERATOR_HASH + "\n").encode()

        def change_and_fail(*args, **kwargs):
            self.users.write_bytes(unexpected)
            return subprocess.CompletedProcess([], 1)

        self.run.side_effect = change_and_fail
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("rollback is not confirmed", output)
        self.assertEqual(self.users.read_bytes(), unexpected)

    def test_held_lock_refuses_concurrent_revocation(self):
        lock = self.root / "users.caddy.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, output = self.call_main()
        self.assertEqual(result, 1)
        self.assertIn("active", output)
        self.assert_original()
        self.run.assert_not_called()

    def test_unsafe_account_permissions_hardlink_and_size_are_rejected(self):
        self.users.chmod(0o644)
        self.assertEqual(self.call_main()[0], 1)
        self.users.chmod(0o640)
        linked = self.root / "linked"
        os.link(self.users, linked)
        self.assertEqual(self.call_main()[0], 1)
        linked.unlink()
        self.users.write_bytes(b" " * (gateway.MAX_USERS_BYTES + 1))
        self.assertEqual(self.call_main()[0], 1)
        self.run.assert_not_called()

    def test_wrong_caddy_group_is_rejected(self):
        with mock.patch.object(gateway.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getegid() + 1)):
            result, _ = self.call_main()
        self.assertEqual(result, 1)
        self.assert_original()
        self.run.assert_not_called()

    def test_symlink_users_config_or_parent_is_rejected(self):
        for target in (self.users, self.config):
            real = target.with_name(target.name + ".real")
            target.rename(real)
            target.symlink_to(real)
            self.assertEqual(self.call_main()[0], 1)
            target.unlink()
            real.rename(target)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        original = self.users
        self.users = alias / original.name
        self.assertEqual(self.call_main()[0], 1)
        self.users = original
        self.assert_original()
        self.run.assert_not_called()

    def test_group_writable_parent_is_rejected(self):
        self.root.chmod(0o770)
        self.assertEqual(self.call_main()[0], 1)
        self.run.assert_not_called()

    def test_unsafe_lock_is_rejected_without_touching_target(self):
        lock = self.root / "users.caddy.lock"
        lock.symlink_to(self.users)
        self.assertEqual(self.call_main()[0], 1)
        self.assert_original()
        self.run.assert_not_called()

    def test_argument_errors_are_sanitized(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = gateway.main(["--unknown", "private details"])
        self.assertEqual(result, 1)
        self.assertNotIn("private details", stdout.getvalue() + stderr.getvalue())
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
