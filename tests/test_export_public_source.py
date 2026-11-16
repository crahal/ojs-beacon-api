from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_public_source as export


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (self.root / ".gitignore").write_text("dist/\ndata/\n.secrets/\n.env\n.local/\n")
        (self.root / "README.md").write_text("Public code: https://203.0.113.10\n")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "example.sh").write_text("#!/bin/bash\nexit 0\n")
        self.output = self.root / "dist" / "source.tar.gz"

    def run_export(self, output=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = export.main(["--repo", str(self.root), "--output", str(output or self.output)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_export_has_only_regular_sanitized_source(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "private.sql").write_text("do not include")
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / "private").write_text("do not include")
        (self.root / ".env").write_text("do not include")
        result, output, errors = self.run_export()
        self.assertEqual(result, 0, errors)
        self.assertIn(hashlib.sha256(self.output.read_bytes()).hexdigest(), output)
        self.assertNotIn(str(self.root), output)
        with tarfile.open(self.output, "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["ojs_api/.gitignore", "ojs_api/README.md", "ojs_api/scripts/example.sh"])
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime, member.uname, member.gname), (0, 0, 0, "", ""))
            self.assertEqual(archive.getmember("ojs_api/scripts/example.sh").mode, 0o755)

    def test_archive_deterministic_despite_source_mtime(self):
        self.assertEqual(self.run_export()[0], 0)
        os.utime(self.root / "README.md", (1234567, 1234567))
        second = self.root / "dist" / "second.tar.gz"
        self.assertEqual(self.run_export(second)[0], 0)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_never_overwrites_existing_output(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"existing")
        result, output, error = self.run_export()
        self.assertEqual(result, 1)
        self.assertIn("already exists", error)
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_personal_address_refused_without_echo(self):
        address = "person@" + "gmail.com"
        (self.root / "README.md").write_text(address)
        result, output, error = self.run_export()
        self.assertEqual(result, 1)
        self.assertIn("non-example-email", error)
        self.assertNotIn(address, output + error)
        self.assertFalse(self.output.exists())

    def test_unknown_nonignored_file_refused(self):
        (self.root / "private.txt").write_text("not public source")
        result, output, error = self.run_export()
        self.assertEqual(result, 1)
        self.assertIn("outside-public-source-allowlist", error)
        self.assertFalse(self.output.exists())

    def test_force_tracked_private_data_refused(self):
        (self.root / ".env").write_text("do not include")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".env"], check=True)
        result, output, error = self.run_export()
        self.assertEqual(result, 1)
        self.assertNotIn("do not include", error + output)
        self.assertFalse(self.output.exists())

    def test_symlink_and_hardlink_sources_refused(self):
        (self.root / "README.md").unlink()
        target = self.root / ".env"
        target.write_text("private")
        (self.root / "README.md").symlink_to(target)
        self.assertIn("symlink-not-read", self.run_export()[2])
        (self.root / "README.md").unlink()
        os.link(target, self.root / "README.md")
        self.assertIn("hardlink-not-read", self.run_export()[2])

    def test_symlink_output_parent_refused(self):
        (self.root / "outside").mkdir()
        (self.root / "dist").symlink_to(self.root / "outside", target_is_directory=True)
        # The unignored outside directory is empty and has no candidate files.
        self.assertEqual(self.run_export()[0], 1)
        self.assertFalse((self.root / "outside" / "source.tar.gz").exists())

    def test_output_must_be_ignored_and_under_dist_inside_checkout(self):
        self.assertEqual(self.run_export(self.root / "source.tar.gz")[0], 1)
        (self.root / ".gitignore").write_text("")
        self.assertEqual(self.run_export()[0], 1)

    def test_traversal_output_refused(self):
        self.assertEqual(self.run_export(self.root / "dist" / ".." / "source.tar.gz")[0], 1)

    def test_validated_bytes_are_used_even_if_source_changes_after_collection(self):
        original = export.collect_sources

        def changing_collection(root):
            selected = original(root)
            (root / "README.md").write_text("person@" + "gmail.com")
            return selected

        with mock.patch.object(export, "collect_sources", side_effect=changing_collection):
            self.assertEqual(self.run_export()[0], 0)
        with tarfile.open(self.output, "r:gz") as archive:
            self.assertEqual(archive.extractfile("ojs_api/README.md").read(), b"Public code: https://203.0.113.10\n")

    def test_no_git_history_even_with_private_old_commit(self):
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Example Person"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "person@" + "gmail.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "Private old metadata"], check=True)
        self.assertEqual(self.run_export()[0], 0)
        with tarfile.open(self.output, "r:gz") as archive:
            self.assertFalse(any(".git/" in name for name in archive.getnames()))

    def test_partial_new_archive_removed_on_write_failure(self):
        with mock.patch.object(export.tarfile.TarFile, "addfile", side_effect=OSError("fixture failure")):
            self.assertEqual(self.run_export()[0], 1)
        self.assertFalse(self.output.exists())

    def test_total_source_limit_enforced(self):
        with mock.patch.object(export, "MAX_TOTAL_BYTES", 1):
            result, output, error = self.run_export()
        self.assertEqual(result, 1)
        self.assertIn("total review limit", error)


if __name__ == "__main__":
    unittest.main()
