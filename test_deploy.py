import os
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from deploy import deploy as deploy_module

SCRIPT = Path(__file__).resolve().parent / "deploy" / "deploy.py"
GOOD_TEST = "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n"
BAD_TEST = "import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail('deliberate')\n"


@unittest.skipIf(os.environ.get("WBANDOZON_DEPLOY_TEST_STAGE") == "1", "avoid recursive deployment integration tests")
class DeployIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "Deployment Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "test_economics.py").write_text(GOOD_TEST)
        (self.repo / "test_candidate_pipeline.py").write_text(GOOD_TEST)
        (self.repo / "requirements.txt").write_text("".join(f"{name}=={version(name)}\n" for name in ("fastapi", "httpx", "uvicorn")))
        self.commit()
        self.base = self.root / "deploy"

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def deploy(self, repo=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", repo or self.repo.as_uri(),
                               "--base", str(self.base), "--allow-local-repository"],
                              capture_output=True, text=True)

    def test_success_then_failure_keeps_previous_release(self):
        first = self.deploy()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.base / "current").is_symlink())
        before = (self.base / "current").resolve()
        (self.repo / "test_economics.py").write_text(BAD_TEST)
        self.commit()
        failed = self.deploy()
        self.assertEqual(failed.returncode, 1)
        self.assertEqual((self.base / "current").resolve(), before)
        self.assertEqual((before / "test_economics.py").read_text(), GOOD_TEST)
        self.assertEqual(len(list((self.base / "releases").iterdir())), 1)

    def test_same_revision_is_a_noop(self):
        self.assertEqual(self.deploy().returncode, 0)
        before = (self.base / "current").resolve()
        again = self.deploy()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Уже активен", again.stdout)
        self.assertEqual((self.base / "current").resolve(), before)
        self.assertEqual(len(list((self.base / "releases").iterdir())), 1)

    def test_missing_remote_and_existing_directory_do_not_replace_current(self):
        self.assertEqual(self.deploy().returncode, 0)
        current = self.base / "current"
        before = current.resolve()
        failed = self.deploy((self.root / "does-not-exist").as_uri())
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(current.resolve(), before)
        current.unlink()
        current.mkdir()
        self.assertEqual(self.deploy().returncode, 1)
        self.assertTrue(current.is_dir())
        self.assertFalse(current.is_symlink())

    def test_repo_with_no_required_tests_is_rejected(self):
        (self.repo / "test_economics.py").unlink()
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "remove required test")
        failed = self.deploy()
        self.assertEqual(failed.returncode, 1)
        self.assertFalse((self.base / "current").exists())

    def test_refuses_to_run_as_root(self):
        with mock.patch.object(deploy_module.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(deploy_module.DeployError, "root"):
                deploy_module.deploy("git@github.com:owner/repo.git", self.base)
        self.assertFalse(self.base.exists())

    def test_wrong_dependency_keeps_active_release(self):
        self.assertEqual(self.deploy().returncode, 0)
        before = (self.base / "current").resolve()
        (self.repo / "requirements.txt").write_text(
            f"fastapi==0.0.0\nhttpx=={version('httpx')}\nuvicorn=={version('uvicorn')}\n")
        self.commit()
        failed = self.deploy()
        self.assertEqual(failed.returncode, 1)
        self.assertIn("не совпадает", failed.stderr)
        self.assertEqual((self.base / "current").resolve(), before)

    def test_requirements_never_execute_options_or_allow_unpinned_packages(self):
        requirement = self.repo / "requirements.txt"
        original = requirement.read_text()
        for bad in ("--extra-index-url https://bad.example\n", "other>=1\n", "fastapi==1.0\n", "missing-package==1.0\n"):
            with self.subTest(bad=bad):
                requirement.write_text(original + bad)
                self.commit()
                self.assertEqual(self.deploy().returncode, 1)
                self.assertFalse((self.base / "current").exists())

    def test_missing_or_linked_requirements_are_rejected(self):
        requirement = self.repo / "requirements.txt"
        requirement.unlink()
        self.commit()
        self.assertEqual(self.deploy().returncode, 1)
        self.assertFalse((self.base / "current").exists())
        requirement.symlink_to("/etc/passwd")
        self.commit()
        self.assertEqual(self.deploy().returncode, 1)
        self.assertFalse((self.base / "current").exists())

    def test_insecure_repository_url_rejected(self):
        result = self.deploy("https://example.com/repo.git")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.base / "current").exists())


if __name__ == "__main__":
    unittest.main()
