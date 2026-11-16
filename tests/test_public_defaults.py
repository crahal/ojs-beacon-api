"""Sharing and deployment defaults must not expose local/private artifacts."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def docker_pattern(pattern: str) -> re.Pattern[str]:
    """Translate the small *, **, ? subset used by this project's ignore file.

    Docker matches paths AND their parent directories. A broad !src/ therefore
    includes children unless followed by a more specific deny. This model is
    deliberately not a general-purpose replacement for Moby's patternmatcher.
    """
    pattern = pattern.strip("/")
    output = "^"
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if pattern[index:index + 2] == "**":
            index += 2
            if index < len(pattern) and pattern[index] == "/":
                output += "(?:.*/)?"
                index += 1
            else:
                output += ".*"
            continue
        if character == "*":
            output += "[^/]*"
        elif character == "?":
            output += "[^/]"
        elif character in "[]\\":
            raise AssertionError("Extend the test model before adding advanced Docker ignore syntax")
        else:
            output += re.escape(character)
        index += 1
    return re.compile(output + "$")


def docker_ignored(name: str, rules: list[str]) -> bool:
    parts = name.split("/")
    parents_and_self = ["/".join(parts[:length]) for length in range(1, len(parts) + 1)]
    ignored = False
    for rule in rules:
        if not rule or rule.startswith("#"):
            continue
        include = rule.startswith("!")
        matcher = docker_pattern(rule[1:] if include else rule)
        if any(matcher.fullmatch(candidate) for candidate in parents_and_self):
            ignored = not include
    return ignored


class GitIgnoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (self.root / ".gitignore").write_text((ROOT / ".gitignore").read_text())

    def ignored(self, name):
        result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--quiet", "--no-index", "--", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(result.returncode, (0, 1))
        return result.returncode == 0

    def test_local_config_and_operator_data_ignored(self):
        for name in (".env", ".env.production", "nested/.env.local", ".secrets/beacon.ini",
                     "data/raw/fixture.sql", "data/clean/mysql-live/ibdata1", ".local/operations/notes.md",
                     ".aws/credentials", ".ssh/id_ed25519", "operator-notes/status.md",
                     "private.pem", "local.key", "export.sql.gz", "records.ibd", "dist/source.tar.gz"):
            with self.subTest(name=name):
                self.assertTrue(self.ignored(name), name)

    def test_templates_and_sql_implementation_not_ignored(self):
        for name in (".env.example", "sql/01_build_ojs_tables.sql", "src/3_build_api.py",
                     "tests/fixtures/synthetic.sql", ".github/workflows/ci.yml"):
            with self.subTest(name=name):
                self.assertFalse(self.ignored(name), name)


class DockerContextTests(unittest.TestCase):
    def setUp(self):
        self.rules = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]

    def test_build_input_files_included(self):
        for name in ("Dockerfile", "requirements.txt", "src/3_build_api.py", "src/progress_logging.py",
                     "README.md", "HOW_TO_CALL.md", "LICENSE"):
            with self.subTest(name=name):
                self.assertFalse(docker_ignored(name, self.rules), name)

    def test_noncode_artifacts_excluded_even_under_src(self):
        for name in (".git/config", ".env", ".env.example", ".env.production", ".secrets/beacon.ini",
                     "data/raw/source.sql", "data/clean/records.ibd", ".local/operations/notes.md",
                     "dist/public.tar.gz", "tests/test_api.py", "src/accidental.sql", "src/accidental.sql.gz",
                     "src/archive.tar.gz", "src/private.pem", "src/private.key", "src/.env",
                     "src/.env.production", "src/.secrets/key", "src/private.json", "src/private.txt",
                     "src/__pycache__/api.pyc", "src/nested/accidental.sql", "src/package.py/accidental.sql"):
            with self.subTest(name=name):
                self.assertTrue(docker_ignored(name, self.rules), name)

    def test_ancestor_negation_regression_is_modeled(self):
        unsafe = ["**", "!src/", "!src/*.py"]
        self.assertFalse(docker_ignored("src/accidental.sql", unsafe))
        safe = ["**", "!src/", "src/**", "!src/*.py", "src/*/**"]
        self.assertTrue(docker_ignored("src/accidental.sql", safe))
        self.assertTrue(docker_ignored("src/package.py/accidental.sql", safe))
        self.assertFalse(docker_ignored("src/api.py", safe))

    def test_dockerfile_does_not_copy_entire_checkout(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertNotRegex(text, r"(?m)^(?:COPY|ADD)\s+(?:--\S+\s+)*\.?/\s")
        self.assertNotRegex(text, r"(?m)^(?:COPY|ADD)\s+(?:--\S+\s+)*\.\s")


class ComposeDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / "docker-compose.yml").read_text()
        self.environment = {}
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                self.environment[key] = value

    def service(self, name):
        match = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", self.compose)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_mysql_version_is_exact_and_matches_template(self):
        match = re.search(r"\$\{OJS_MYSQL_IMAGE:-([^}]+)\}", self.service("mysql"))
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^mysql:8\.4\.\d+$")
        self.assertEqual(match.group(1), self.environment["OJS_MYSQL_IMAGE"])

    def test_mysql_never_publishes_host_port(self):
        mysql = self.service("mysql")
        self.assertNotRegex(mysql, r"(?m)^    ports:")
        self.assertNotRegex(mysql, r"(?m)^    network_mode:\s*host")

    def test_mysql_liveness_check_has_no_password_arguments(self):
        mysql = self.service("mysql")
        check = re.search(r"(?m)^      test: (.+)$", mysql).group(1)
        self.assertIn('"CMD", "mysqladmin"', check)
        self.assertIn('"--no-defaults", "--no-login-paths"', check)
        self.assertNotIn("MYSQL_ROOT_PASSWORD", check)
        self.assertNotIn("password", check.lower())
        self.assertNotIn("CMD-SHELL", check)

    def test_api_publishes_loopback_only(self):
        api = self.service("ojs-api")
        self.assertIn('"127.0.0.1:${OJS_API_PORT:-8000}:8000"', api)
        self.assertEqual(len(re.findall(r"(?m)^    ports:", self.compose)), 1)
        self.assertNotRegex(api, r"(?m)^    network_mode:\s*host")
        self.assertIn("    read_only: true", api)

    def test_api_private_credentials_mounted_read_only(self):
        api = self.service("ojs-api")
        self.assertIn("/run/secrets/ojs-api-client.env:ro", api)
        self.assertIn("/run/secrets/ojs-db-api-password:ro", api)
        self.assertNotIn("OJS_API_PASSWORD:", api)
        self.assertEqual(self.environment["OJS_PUBLIC_BASE_URL"], "https://PUBLIC_IP")
        self.assertIn("replace", self.environment["MYSQL_ROOT_PASSWORD"])

    def test_make_sharing_targets_invoke_guard_and_exporter(self):
        text = (ROOT / "Makefile").read_text()
        for target, script in (("check-repository", "scripts/check_repository.py"),
                               ("check-history", "scripts/check_repository.py --history"),
                               ("export-public", "scripts/export_public_source.py")):
            with self.subTest(target=target):
                match = re.search(r"(?ms)^" + target + r":[^\n]*\n(.*?)(?=^\S|\Z)", text)
                self.assertIsNotNone(match, target)
                self.assertIn(script, match.group(1))


class PublicationWorkflowTests(unittest.TestCase):
    def test_ci_checks_full_public_history_without_persisting_credentials(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        portable = workflow.split("  portable-tests:\n", 1)[1].split("\n  api-image:", 1)[0]
        checkout = portable.split("      - uses: actions/checkout@", 1)[1].split("\n      - ", 1)[0]
        self.assertIn("          fetch-depth: 0", checkout)
        self.assertIn("          persist-credentials: false", checkout)
        self.assertIn("run: make check-repository", portable)
        self.assertIn("run: make check-history", portable)
        self.assertLess(portable.index("run: make check-history"), portable.index("run: make test"))

    def test_contributor_checks_cover_commit_metadata_before_upload(self):
        guidance = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("after creating your commit and before pushing", guidance)
        self.assertIn("cannot prevent an initial disclosure", guidance)


if __name__ == "__main__":
    unittest.main()
