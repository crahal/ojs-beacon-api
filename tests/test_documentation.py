"""Keep public runbooks navigable without executing their operational commands."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")])


class DocumentationTests(unittest.TestCase):
    def test_local_markdown_links_resolve_inside_public_source(self):
        for document in DOCUMENTS:
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                target = target.strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or target.startswith("#"):
                    continue
                with self.subTest(document=document.name, target=target):
                    destination = (document.parent / unquote(parsed.path)).resolve()
                    self.assertTrue(destination.is_relative_to(ROOT))
                    self.assertTrue(destination.exists())
                    self.assertNotIn(".local", destination.relative_to(ROOT).parts)

    @unittest.skipUnless(shutil.which("bash"), "bash required to check runbook syntax")
    def test_shell_examples_parse_without_being_executed(self):
        count = 0
        for document in DOCUMENTS:
            for number, script in enumerate(re.findall(
                r"```(?:bash|sh|shell)\n(.*?)\n```", document.read_text(), re.DOTALL
            ), start=1):
                with self.subTest(document=document.name, block=number):
                    result = subprocess.run(["bash", "-n"], input=script, text=True,
                                            capture_output=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    count += 1
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
