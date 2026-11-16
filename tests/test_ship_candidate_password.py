from __future__ import annotations

import argparse
import contextlib
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
import ship_candidate_password as ship


class CredentialShippingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = "2026-10-01"
        self.clean = self.root / "data" / "clean"
        self.candidate = self.clean / f"mysql-{self.snapshot}"
        self.candidate.mkdir(parents=True)
        (self.root / "data" / ".updates-paused").touch()
        (self.root / ".secrets").mkdir(mode=0o700)
        (self.candidate / "OJS_COMPACT_RELEASE.json").write_text(json.dumps({
            "schema_version": 1, "snapshot_date": self.snapshot, "clean_only": True,
            "data_directory_name": self.candidate.name,
        }))
        (self.clean / f"pkpbeacon-release-{self.snapshot}.json").write_text(json.dumps({
            "schema_version": 1, "snapshot_date": self.snapshot,
            "clean_export_filename": f"pkpbeacon-clean-{self.snapshot}.sql.gz",
        }))
        for suffix in (".sql.gz", ".sql.gz.sha256"):
            (self.clean / f"pkpbeacon-clean-{self.snapshot}{suffix}").write_bytes(b"fixture\n")
        for suffix in (".json", ".json.sha256"):
            (self.clean / f"pkpbeacon-changes-{self.snapshot}{suffix}").write_bytes(b"fixture\n")
        self.args = argparse.Namespace(snapshot=self.snapshot)
        self.payload = b"temporary-private-root-credential\n"
        self.destination = self.root / ".secrets" / f"incoming-root-password-{self.snapshot}"
        self.mount = mock.patch.object(ship.os.path, "ismount", return_value=True)
        self.mount.start()
        self.addCleanup(self.mount.stop)
        self.stopped = mock.patch.object(ship.pipeline, "assert_database_stopped")
        self.assert_stopped = self.stopped.start()
        self.addCleanup(self.stopped.stop)

    def receive(self, payload=None):
        return ship.receive(self.args, root=self.root,
                            stream=io.BytesIO(self.payload if payload is None else payload))

    def test_stores_owner_private_one_line_and_no_publication(self):
        result = self.receive()
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result["published"])
        self.assert_stopped.assert_called_once_with(self.candidate)
        self.assertNotIn(self.payload.decode().strip(), json.dumps(result))

    def test_identical_retry_is_idempotent(self):
        self.receive()
        before = self.destination.stat().st_ino
        self.receive()
        self.assertEqual(self.destination.stat().st_ino, before)

    def test_existing_different_secret_not_overwritten(self):
        self.receive()
        with self.assertRaises(ship.TransferError):
            self.receive(b"different-secret\n")
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_rejects_unsafe_destination_mode(self):
        self.receive()
        self.destination.chmod(0o644)
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_rejects_symlinked_secret(self):
        other = self.root / "private"
        other.write_bytes(self.payload)
        other.chmod(0o600)
        self.destination.symlink_to(other)
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_rejects_shared_secrets_directory(self):
        (self.root / ".secrets").chmod(0o750)
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_rejects_unmounted_data(self):
        with mock.patch.object(ship.os.path, "ismount", return_value=False):
            with self.assertRaises(ship.TransferError):
                self.receive()
        self.assertFalse(self.destination.exists())

    def test_requires_pause(self):
        (self.root / "data" / ".updates-paused").unlink()
        with self.assertRaises(FileNotFoundError):
            self.receive()

    def test_rejects_symlink_pause(self):
        pause = self.root / "data" / ".updates-paused"
        pause.unlink()
        pause.symlink_to(self.candidate / "OJS_COMPACT_RELEASE.json")
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_refuses_live_candidate(self):
        (self.clean / "mysql-live").symlink_to(self.candidate.name)
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_refuses_publication_journal(self):
        (self.clean / ".compact-update.json").write_text("{}")
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_refuses_incomplete_marker(self):
        (self.candidate / "OJS_COMPACT_RELEASE.json").write_text("{}")
        with self.assertRaises(ship.TransferError):
            self.receive()

    def test_refuses_missing_export(self):
        (self.clean / f"pkpbeacon-clean-{self.snapshot}.sql.gz").unlink()
        with self.assertRaises(FileNotFoundError):
            self.receive()

    def test_bounded_protocol_rejects_invalid_passwords(self):
        for payload in (b"", b"\n", b"a", b"a\nb\n", b"a\r\n", b"a\0\n", b"x" * 4096 + b"\n"):
            with self.subTest(payload_size=len(payload)), self.assertRaises(ship.TransferError):
                self.receive(payload)
        self.assertFalse(self.destination.exists())

    def sender(self):
        config = self.root / "local.env"
        config.write_text("MYSQL_ROOT_PASSWORD=temporary-private-root-credential\nOJS_DB_API_USER=ojs_api\n")
        config.chmod(0o600)
        identity = self.root / "identity"
        identity.write_text("fixture private identity\n")
        identity.chmod(0o600)
        return argparse.Namespace(snapshot=self.snapshot, credentials_env=config,
                                  identity_file=identity, host="ubuntu@203.0.113.10")

    def receipt(self):
        return {"status": "credential_stored", "snapshot": self.snapshot,
                "path": f"/srv/ojs_api/.secrets/incoming-root-password-{self.snapshot}",
                "published": False}

    def test_sender_uses_stdin_only_with_strict_ssh(self):
        args = self.sender()
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.receipt()).encode(), b"")
        with mock.patch.object(ship.subprocess, "run", return_value=completed) as run:
            result = ship.send(args)
        positional, keywords = run.call_args
        command = positional[0]
        self.assertEqual(keywords["input"], self.payload)
        self.assertNotIn(self.payload.decode().strip(), repr(command))
        self.assertNotIn("env", keywords)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("sudo -n -u ojs python3 -B", command[-1])
        self.assertEqual(result, self.receipt())

    def test_sender_rejects_ssh_option_or_shell_target(self):
        for target in ("-oProxyCommand=bad", "ubuntu@host;echo bad", "ubuntu@host $(bad)"):
            args = self.sender()
            args.host = target
            with self.subTest(target=target), self.assertRaises(ship.TransferError):
                ship.remote_command(args)

    def test_sender_failure_never_echoes_remote_secret(self):
        args = self.sender()
        completed = subprocess.CompletedProcess([], 1, self.payload, self.payload)
        with mock.patch.object(ship.subprocess, "run", return_value=completed):
            with self.assertRaises(ship.TransferError) as failure:
                ship.send(args)
        self.assertNotIn(self.payload.decode().strip(), str(failure.exception))

    def test_sender_rejects_malformed_receipt_without_echo(self):
        completed = subprocess.CompletedProcess([], 0, self.payload, b"")
        with mock.patch.object(ship.subprocess, "run", return_value=completed):
            with self.assertRaises(ship.TransferError) as failure:
                ship.send(self.sender())
        self.assertNotIn(self.payload.decode().strip(), str(failure.exception))

    def test_cli_suppresses_unexpected_private_exceptions(self):
        errors = io.StringIO()
        with mock.patch.object(ship, "receive", side_effect=ValueError(self.payload.decode())), \
                contextlib.redirect_stderr(errors):
            status = ship.main(["receive", "--snapshot", self.snapshot])
        self.assertEqual(status, 1)
        self.assertNotIn(self.payload.decode().strip(), errors.getvalue())

    def test_cli_rejects_invalid_snapshot(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ship.parser().parse_args(["receive", "--snapshot", "2026-02-30"])


if __name__ == "__main__":
    unittest.main()
