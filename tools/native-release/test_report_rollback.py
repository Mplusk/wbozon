"""The native workflow checks archives from a nested tooling directory."""

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import check_report_rollback as rollback


class ServiceArchiveTests(unittest.TestCase):
    def test_export_service_matches_git_tree_including_nested_files(self):
        names = (
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(rollback.REPO),
                    "ls-tree",
                    "-r",
                    "-z",
                    "--name-only",
                    "HEAD:services/email-auth",
                ]
            )
            .decode()
            .split("\0")
        )
        expected = set(names) - {""}
        self.assertIn("portal.py", expected)
        with TemporaryDirectory() as directory:
            service = Path(directory) / "exported"
            rollback.export_service("HEAD", service)
            self.assertEqual(
                {p.relative_to(service).as_posix() for p in service.rglob("*") if p.is_file()}, expected
            )
            original = subprocess.check_output(
                ["git", "-C", str(rollback.REPO), "show", "HEAD:services/email-auth/portal.py"]
            )
            self.assertEqual((service / "portal.py").read_bytes(), original)
