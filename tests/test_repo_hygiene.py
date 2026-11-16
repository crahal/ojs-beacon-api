from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_repository", ROOT / "scripts/check_repository.py")
CHECK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECK
SPEC.loader.exec_module(CHECK)


class PathPolicyTests(unittest.TestCase):
    def test_code_and_synthetic_source_allowed(self):
        for name in ("README.md", ".env.example", "sql/01_build_ojs_tables.sql",
                     "tests/fixtures/synthetic.sql", "src/service.py", "deploy/example.conf"):
            with self.subTest(name=name):
                self.assertIsNone(CHECK.path_problem(name))

    def test_private_data_and_archives_refused_without_content(self):
        for name in ("data/raw/anything.txt", ".secrets/beacon.ini", ".env", ".env.production",
                     "nested/.secrets/password", "credentials/config", "operator-notes/private.md",
                     "private.pem", "database.ibd", "export.sql", "release.sql.gz", "release.zip",
                     "nested/local.env", "id_ed25519", ".aws/config", "pkpbeacon-changes.json",
                     ".local/operations/note.md", ".git/config", ".codex/session.json", ".agents/private.md",
                     "DATA/RAW/file.txt"):
            with self.subTest(name=name):
                self.assertIsNotNone(CHECK.path_problem(name))

    def test_escape_paths_refused(self):
        for name in ("/etc/passwd", "../private", "src/../../private", ""):
            self.assertEqual(CHECK.path_problem(name), "unsafe-path")


class ContentPolicyTests(unittest.TestCase):
    def kinds(self, text):
        return {kind for kind, line in CHECK.text_problems(text.encode())}

    def test_documentation_addresses_and_generic_paths_allowed(self):
        text = "https://203.0.113.10 192.0.2.1 198.51.100.1 127.0.0.1 0.0.0.0\n"
        text += "admin@example.org person@project.example.com user@example.invalid /home/user/code\n"
        text += "name@users.noreply.github.com /home/ojs/code /home/ubuntu/code\n"
        text += "vol-0123456789abcdef0 00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.kinds(text), set())

    def test_public_and_private_real_addresses_flagged(self):
        for address in ("8." + "8.8.8", "10." + "20.30.40", "172." + "26.1.2"):
            with self.subTest(address=address):
                self.assertIn("non-example-ipv4", self.kinds(address))

    def test_personal_email_and_home_path_flagged(self):
        self.assertEqual(self.kinds("person@" + "gmail.com"), {"non-example-email"})
        self.assertEqual(self.kinds("/home/" + "private-person/repository"), {"personal-home-path"})
        self.assertEqual(self.kinds("/Users/" + "private-person/repository"), {"personal-home-path"})

    def test_ipv6_examples_allowed_real_addresses_flagged(self):
        self.assertEqual(self.kinds("https://[2001:db8::1]:443 ::1 ::"), set())
        for address in (":".join(("2606", "4700", "4700", "", "1111")), "fe80:" + ":abcd%eth0", "fd00:" + ":abcd"):
            self.assertIn("non-example-ipv6", self.kinds(address))

    def test_deployment_identifiers_flagged(self):
        self.assertIn("deployment-uuid", self.kinds("-".join(("a" * 8, "b" * 4, "c" * 4, "d" * 4, "e" * 12))))
        self.assertIn("deployment-volume-id", self.kinds("vol-" + "a" * 17))
        self.assertIn("aws-account-id", self.kinds("arn:aws:iam::" + "1" * 12 + ":role/thing"))

    def test_high_confidence_credentials_flagged(self):
        inputs = (("private-key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
                  ("openai-key", "sk-" + "proj-" + "a" * 40),
                  ("github-token", "ghp_" + "a" * 36),
                  ("aws-access-key", "AKIA" + "A" * 16))
        for kind, value in inputs:
            with self.subTest(kind=kind):
                self.assertEqual(self.kinds(value), {kind})

    def test_line_number_does_not_include_match(self):
        issues = CHECK.text_problems(("heading\n" + "person@" + "gmail.com" + "\n").encode())
        self.assertEqual(issues, [("non-example-email", 2)])

    def test_binary_and_invalid_encoding_require_review(self):
        self.assertEqual(CHECK.text_problems(b"\x00data"), [("binary-file-requires-review", 0)])
        self.assertEqual(CHECK.text_problems(b"\xff"), [("non-utf8-file-requires-review", 0)])


class FilesystemPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_blocked_path_never_opened(self):
        with mock.patch.object(CHECK.os, "open", side_effect=AssertionError("must not read")):
            findings = CHECK.inspect_file(self.root, "data/raw/anything")
        self.assertEqual(findings[0].kind, "private-or-data-path")

    def test_final_symlink_not_read(self):
        (self.root / "private").write_text("person@" + "gmail.com")
        (self.root / "candidate.txt").symlink_to("private")
        self.assertEqual(CHECK.inspect_file(self.root, "candidate.txt")[0].kind, "symlink-not-read")

    def test_ancestor_symlink_not_read(self):
        (self.root / "private").mkdir()
        (self.root / "private" / "file").write_text("person@" + "gmail.com")
        (self.root / "linked").symlink_to("private", target_is_directory=True)
        self.assertEqual(CHECK.inspect_file(self.root, "linked/file")[0].kind, "symlink-not-read")

    def test_oversized_file_not_read(self):
        path = self.root / "large.txt"
        with path.open("wb") as stream:
            stream.truncate(CHECK.MAX_BYTES + 1)
        self.assertEqual(CHECK.inspect_file(self.root, "large.txt")[0].kind, "file-exceeds-5-MiB")

    def test_hardlinked_file_not_read(self):
        import os
        (self.root / "private").write_text("do not read")
        os.link(self.root / "private", self.root / "candidate")
        self.assertEqual(CHECK.inspect_file(self.root, "candidate")[0].kind, "hardlink-not-read")

    def test_deleted_tracked_file_ignored_in_candidate(self):
        self.assertEqual(CHECK.inspect_file(self.root, "deleted.md"), [])

    def test_regular_source_inspected(self):
        (self.root / "README.md").write_text("API URL: https://203.0.113.10\n")
        self.assertEqual(CHECK.inspect_file(self.root, "README.md"), [])


class GitInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Example Maintainer")
        self.git("config", "user.email", "maintainer@example.org")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def run_check(self, *extra):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = CHECK.main(["--repo", str(self.root), *extra])
        return result, stdout.getvalue()

    def test_candidates_include_untracked_but_exclude_ignored(self):
        (self.root / ".gitignore").write_text("data/\n.env\n")
        (self.root / "data").mkdir()
        (self.root / "data" / "private").write_text("not to read")
        (self.root / ".env").write_text("not to read")
        (self.root / "README.md").write_text("public code\n")
        self.assertEqual(CHECK.candidate_paths(self.root), [".gitignore", "README.md"])
        result, text = self.run_check()
        self.assertEqual(result, 0)
        self.assertIn("2 candidate paths", text)

    def test_force_tracked_private_path_is_not_hidden_by_ignore(self):
        (self.root / ".gitignore").write_text(".env\n")
        (self.root / ".env").write_text("private content must never appear")
        self.git("add", "-f", ".env")
        result, output = self.run_check()
        self.assertEqual(result, 1)
        self.assertIn("private-configuration", output)
        self.assertNotIn("private content", output)

    def test_removed_identifier_still_fails_history(self):
        identifier = "person@" + "gmail.com"
        (self.root / "README.md").write_text(identifier)
        self.commit("Add readme")
        (self.root / "README.md").write_text("public documentation")
        self.commit("Remove personal address")
        self.assertEqual(self.run_check()[0], 0)
        result, output = self.run_check("--history")
        self.assertEqual(result, 1)
        self.assertIn("history:", output)
        self.assertIn("non-example-email", output)
        self.assertNotIn(identifier, output)

    def test_history_checks_commit_author_privacy(self):
        self.git("config", "user.email", "person@" + "gmail.com")
        (self.root / "README.md").write_text("public\n")
        self.commit("Start")
        result, output = self.run_check("--history")
        self.assertEqual(result, 1)
        self.assertIn("<git-metadata>", output)
        self.assertNotIn("person@" + "gmail.com", output)

    def test_clean_example_history_passes(self):
        (self.root / "README.md").write_text("https://203.0.113.10 admin@example.org\n")
        self.commit("Start")
        self.assertEqual(self.run_check("--history")[0], 0)

    def test_history_private_files_not_read(self):
        (self.root / ".env").write_text("never print this")
        self.commit("Legacy config")
        (self.root / ".env").unlink()
        self.commit("Remove config")
        findings, count = CHECK.inspect_history(self.root)
        self.assertTrue(any(finding.kind == "private-configuration" for finding in findings))
        self.assertGreater(count, 0)

    def test_private_historical_alias_found_after_rename(self):
        (self.root / ".env").write_text("same bytes under another path\n")
        self.commit("Add legacy config")
        self.git("mv", ".env", "README.md")
        self.commit("Rename without changing contents")
        findings, count = CHECK.inspect_history(self.root)
        self.assertTrue(any(finding.path == ".env" and finding.kind == "private-configuration"
                            for finding in findings))

    def test_historical_symlink_not_read(self):
        (self.root / "README.md").symlink_to("/etc/private-file")
        self.commit("Legacy link")
        (self.root / "README.md").unlink()
        self.commit("Remove link")
        findings, count = CHECK.inspect_history(self.root)
        self.assertTrue(any(finding.kind == "historical-symlink-not-read" for finding in findings))


if __name__ == "__main__":
    unittest.main()
