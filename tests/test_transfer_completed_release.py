from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import transfer_completed_release as transfer

HISTORY = ["2026-01-01", "2026-07-01", "2026-10-01"]
SELECTED = HISTORY[-1]


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(clean):
    clean.mkdir(parents=True, exist_ok=True)
    db = clean / f"mysql-{SELECTED}"
    db.mkdir()
    (db / "ibdata1").write_bytes(b"stopped example database")
    (db / "pkpbeacon_db").mkdir()
    (db / "pkpbeacon_db/ojs_articles.ibd").write_bytes(b"physical content")
    write_json(db / transfer.update.MARKER, {
        "schema_version": 1, "snapshot_date": SELECTED, "clean_only": True,
        "data_directory_name": db.name, "mysql_version": "8.4.11", "pipeline_version": "3",
        "source_sha256": "a" * 64, "build_sql_sha256": "b" * 64,
    })
    export = clean / f"pkpbeacon-clean-{SELECTED}.sql.gz"
    export.write_bytes(b"example checksummed export")
    export_hash = transfer.sha256(export)
    export.with_name(export.name + ".sha256").write_text(export_hash + "  " + export.name + "\n")
    previous = None
    for version in HISTORY:
        report = clean / f"pkpbeacon-changes-{version}.json"
        write_json(report, {
            "schema_version": 1, "snapshot": {"date": version,
                "previous_snapshot_date": previous, "source_sha256": "a" * 64,
                "build_sql_sha256": "b" * 64}, "release_guard": {"status": "passed"},
        })
        checksum = transfer.sha256(report)
        report.with_name(report.name + ".sha256").write_text(checksum + "  " + report.name + "\n")
        write_json(clean / f"pkpbeacon-release-{version}.json", {
            "schema_version": 1, "snapshot_date": version,
            "change_report_filename": report.name, "change_report_sha256": checksum,
            "clean_export_filename": f"pkpbeacon-clean-{version}.sql.gz",
            "clean_export_sha256": export_hash if version == SELECTED else "c" * 64,
        })
        previous = version
    return transfer.target_paths(clean, transfer.artifact_names(SELECTED, HISTORY))


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        offline_mount_check = mock.patch.object(transfer, "unmounted")
        self.unmounted = offline_mount_check.start()
        self.addCleanup(offline_mount_check.stop)
        self.source = self.root / "source"
        self.paths = fixture(self.source)
        self.names = list(self.paths)
        self.project = self.root / "project"
        self.clean = self.project / "data/clean"
        self.raw = self.project / "data/raw"
        self.clean.mkdir(parents=True)
        self.raw.mkdir()
        (self.project / "data/.updates-paused").touch()
        rows = transfer.inventory(self.paths)
        self.args = transfer.parser().parse_args([
            "finish", "--project-root", str(self.project), "--snapshot", SELECTED,
            "--total-bytes", str(sum(row[2] for row in rows)),
            "--inventory-sha256", transfer.inventory_digest(rows),
            *[item for version in HISTORY for item in ("--history", version)],
        ])
        self.incoming = self.clean / f".incoming-{SELECTED}"
        self.payload = self.incoming / "files"

    def stage(self):
        shutil.copytree(self.source, self.payload)
        write_json(self.incoming / "transfer.json", transfer.plan(self.args))

    def finish(self):
        with redirect_stderr(io.StringIO()):
            transfer.finish(self.args, self.clean, self.incoming, self.payload, self.names)

    def test_valid_source_and_only_latest_export(self):
        transfer.verify_release(self.paths, SELECTED, HISTORY, verify_export=True)
        self.assertNotIn("pkpbeacon-clean-2026-01-01.sql.gz", self.names)
        self.assertNotIn("pkpbeacon-clean-2026-07-01.sql.gz", self.names)

    def test_dates_and_lineage_selection(self):
        for value in ["2026-13-01", "2026-1-01", "../2026-10-01"]:
            with self.assertRaises(Exception):
                transfer.snapshot(value)
        for history in [HISTORY[::-1], HISTORY + [SELECTED], HISTORY[:-1], []]:
            with self.assertRaises(transfer.TransferError):
                transfer.artifact_names(SELECTED, history)

    def test_report_corruption_fails(self):
        self.paths[f"pkpbeacon-changes-{HISTORY[0]}.json"].write_text("{}")
        with self.assertRaises(transfer.TransferError):
            transfer.verify_release(self.paths, SELECTED, HISTORY, verify_export=False)

    def test_export_corruption_fails(self):
        self.paths[f"pkpbeacon-clean-{SELECTED}.sql.gz"].write_text("corrupt")
        with self.assertRaises(transfer.TransferError):
            transfer.verify_release(self.paths, SELECTED, HISTORY, verify_export=True)

    def test_marker_patch_must_match(self):
        path = self.paths[f"mysql-{SELECTED}"] / transfer.update.MARKER
        marker = json.loads(path.read_text())
        marker["mysql_version"] = "8.4.12"
        write_json(path, marker)
        with self.assertRaises(transfer.TransferError):
            transfer.verify_release(self.paths, SELECTED, HISTORY, verify_export=False)

    def test_symlinks_and_fifo_rejected(self):
        db = self.paths[f"mysql-{SELECTED}"]
        link = db / "bad"
        link.symlink_to(self.source)
        with self.assertRaises(transfer.TransferError):
            transfer.inventory(self.paths)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaises(transfer.TransferError):
            transfer.inventory(self.paths)

    def test_apparent_size_not_sparse_allocated_size(self):
        path = self.paths[f"mysql-{SELECTED}"] / "sparse"
        with path.open("wb") as output:
            output.truncate(1024 * 1024)
        rows = transfer.inventory(self.paths)
        self.assertIn([f"mysql-{SELECTED}/sparse", "file", 1024 * 1024], rows)

    def test_actual_rsync_server_bundle_accepted(self):
        command = ["--server", "-trcRe.iLsfxCIvu", "--inplace", ".", str(self.payload) + "/"]
        self.assertEqual(transfer.receiver_arguments(["--", *command], self.payload), command)

    def test_unsafe_receiver_options_refused(self):
        for option in ["--sender", "--delete", "--log-file=/etc/passwd", "--backup-dir=/etc", "--files-from=/etc/passwd"]:
            with self.assertRaises(transfer.TransferError):
                transfer.receiver_arguments(["--server", option, ".", str(self.payload) + "/"], self.payload)

    def test_other_destination_refused(self):
        with self.assertRaises(transfer.TransferError):
            transfer.receiver_arguments(["--server", "-trcRe.iLsfxCIvu", ".", "/etc/"], self.payload)

    def test_mount_and_pause_required(self):
        with self.assertRaises(transfer.TransferError):
            transfer.remote_layout(self.args)
        with mock.patch.object(transfer.os.path, "ismount", return_value=True):
            transfer.remote_layout(self.args)
            (self.project / "data/.updates-paused").unlink()
            with self.assertRaises(transfer.TransferError):
                transfer.remote_layout(self.args)

    def test_live_pointer_and_pending_publication_refused(self):
        live = self.clean / "mysql-live"
        live.symlink_to(f"mysql-{SELECTED}")
        with mock.patch.object(transfer.os.path, "ismount", return_value=True):
            with self.assertRaises(transfer.TransferError):
                transfer.remote_layout(self.args)
            live.unlink()
            (self.clean / ".compact-update.json").write_text("{}")
            with self.assertRaises(transfer.TransferError):
                transfer.remote_layout(self.args)

    def test_offline_source_lock_blocks_other_process(self):
        database = self.paths[f"mysql-{SELECTED}"]
        code = "import sys, fcntl; f=open(sys.argv[1], 'r+b'); fcntl.lockf(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)"
        with transfer.offline_database(database):
            result = subprocess.run([sys.executable, "-c", code, str(database / "ibdata1")], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, "-c", code, str(database / "ibdata1")], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_finish_installs_manifests_last(self):
        self.stage()
        moved = []
        original = os.rename
        def rename(source, dest):
            moved.append(Path(dest).name)
            original(source, dest)
        with mock.patch.object(transfer.os, "rename", side_effect=rename):
            self.finish()
        self.assertEqual(moved[-3:], [f"pkpbeacon-release-{version}.json" for version in HISTORY])
        self.assertTrue(json.loads((self.incoming / "transfer.json").read_text())["installed"])
        self.assertFalse((self.clean / "mysql-live").exists())
        transfer.verify_release(transfer.target_paths(self.clean, self.names), SELECTED, HISTORY, verify_export=True)

    def test_failed_transfer_inventory_never_installed(self):
        self.stage()
        (self.payload / f"mysql-{SELECTED}" / "unexpected").write_text("x")
        with self.assertRaises(transfer.TransferError):
            self.finish()
        self.assertFalse((self.clean / f"mysql-{SELECTED}").exists())

    def test_cannot_overwrite_preexisting_database(self):
        self.stage()
        (self.clean / f"mysql-{SELECTED}").mkdir()
        with self.assertRaises(transfer.TransferError):
            self.finish()

    def test_resume_between_report_and_checksum_renames(self):
        self.stage()
        original = os.rename
        def interrupted(source, dest):
            if str(dest).endswith(f"pkpbeacon-changes-{HISTORY[0]}.json.sha256"):
                raise OSError("simulated interruption")
            original(source, dest)
        with mock.patch.object(transfer.os, "rename", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.finish()
        self.finish()
        self.assertTrue((self.clean / f"pkpbeacon-release-{SELECTED}.json").exists())

    def test_altered_installed_candidate_cannot_be_resumed(self):
        self.stage()
        self.finish()
        (self.clean / f"mysql-{SELECTED}" / "credentials-adopted").write_text("changed")
        with self.assertRaises(transfer.TransferError):
            self.finish()

    def test_lock_contention(self):
        path = self.clean / ".pipeline.lock"
        with transfer.held_lock(path):
            with self.assertRaises(transfer.TransferError):
                with transfer.held_lock(path):
                    pass

    def test_inherited_lock_outlives_parent_context(self):
        path = self.clean / ".pipeline.lock"
        with transfer.held_lock(path) as descriptor:
            process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                       stdin=subprocess.PIPE, pass_fds=(descriptor,))
        try:
            with self.assertRaises(transfer.TransferError):
                with transfer.held_lock(path):
                    pass
        finally:
            process.communicate(timeout=5)
        with transfer.held_lock(path):
            pass

    def test_completed_installation_cannot_be_run_twice(self):
        self.stage()
        self.finish()
        with self.assertRaisesRegex(transfer.TransferError, "already installed"):
            self.finish()

    def test_receiver_capacity_stops_before_rsync(self):
        self.args.mode = "receive"
        self.args.rsync_args = ["--server", "-trcRe.iLsfxCIvu", "--inplace", ".", str(self.payload) + "/"]
        with mock.patch.object(transfer.os.path, "ismount", return_value=True), \
                mock.patch.object(transfer.shutil, "disk_usage", return_value=mock.Mock(free=1024)), \
                mock.patch.object(transfer.subprocess, "Popen") as process:
            with self.assertRaises(transfer.TransferError):
                transfer.receive(self.args)
            process.assert_not_called()

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required")
    def test_real_rsync_roundtrip_and_resume_partial(self):
        # A local SSH transport stub runs the real receiver without a network,
        # sudo privileges or mounted test volume. All other guards/locks run.
        stub = self.root / "ssh_stub.py"
        stub.write_text(
            "import sys, shlex\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(transfer.__file__).parent)!r})\n"
            "import transfer_completed_release as t\n"
            "t.os.path.ismount = lambda path: True\n"
            "t.unmounted = lambda *args, **kwargs: None\n"
            "args = []\n"
            "for value in sys.argv[1:]:\n"
            "    args.extend(shlex.split(value) if 'transfer_completed_release.py' in value else [value])\n"
            "raise SystemExit(t.main(args[args.index('receive'):]))\n")
        self.payload.mkdir(parents=True)
        write_json(self.incoming / "transfer.json", transfer.plan(self.args))
        # Resume a truncated export in place. It must not be mistaken for a
        # completed transfer or copied to a second temporary full-size file.
        (self.payload / f"pkpbeacon-clean-{SELECTED}.sql.gz").write_bytes(b"example")
        arguments = transfer.parser().parse_args([
            "send", "--source-clean", str(self.source), "--source-raw", str(self.raw),
            "--host", "ubuntu@example.invalid", "--identity", str(self.root / "unused"),
            "--remote-project", str(self.project), "--snapshot", SELECTED,
            *[item for version in HISTORY for item in ("--history", version)],
        ])
        # Source and destination raw directories must be distinct lock domains.
        source_raw = self.root / "source-raw"
        source_raw.mkdir()
        arguments.source_raw = [source_raw]
        actual_run = subprocess.run
        def run(command, **kwargs):
            command[command.index("-e") + 1] = f"{sys.executable} {stub}"
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
            kwargs["text"] = True
            result = actual_run(command, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result
        with mock.patch.object(transfer.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(transfer.send(arguments), 0)
        for name, source in self.paths.items():
            if source.is_file():
                self.assertEqual(source.read_bytes(), (self.clean / name).read_bytes())
        self.assertEqual((self.clean / f"mysql-{SELECTED}/pkpbeacon_db/ojs_articles.ibd").read_bytes(), b"physical content")


if __name__ == "__main__":
    unittest.main()
