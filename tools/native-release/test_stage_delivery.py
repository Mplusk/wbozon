"""Execute the staging workflow's transport loop with a fake SSH receiver and clock."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
PAYLOAD = b"synthetic staged artifact\n"
COMMIT = "a" * 40


class StagingDeliveryTests(unittest.TestCase):
    def test_staging_verifies_the_build_attempt_before_transferring(self):
        preflight = (
            WORKFLOW.read_text(encoding="utf-8")
            .split("  native-stage:\n", 1)[1]
            .split("      - name: Verify artifact identity", 1)[1]
            .split("        run: |\n", 1)[1]
            .split("      - name:", 1)[0]
        )
        bash = os.environ.get("WBOZON_TEST_BASH") or shutil.which("bash")
        self.assertIsNotNone(bash)
        for attempt, expected in (("7", 0), ("8", 1)):
            with self.subTest(attempt=attempt):
                env = dict(os.environ, BUILD_ARTIFACT_ID="381", BUILD_ATTEMPT="7",
                           BUILD_SHA256="a" * 64, GITHUB_RUN_ATTEMPT=attempt)
                run = subprocess.run([bash, "-s"], input=textwrap.dedent(preflight),
                                     env=env, text=True, capture_output=True, timeout=5)
                self.assertEqual(run.returncode, expected)
                if expected:
                    self.assertIn("Re-run all jobs", run.stdout)

    def run_transfer(self, outcomes):
        loop = (
            WORKFLOW.read_text(encoding="utf-8")
            .split("  native-stage:\n", 1)[1]
            .split("\n  native-deploy:", 1)[0]
            .split("# Retrying the same identity", 1)[1]
            .split("\n", 1)[1]
        )
        # Every program that could transmit bytes or wait is replaced by a shell function.
        fakes = r"""
set -euo pipefail
export PATH=/usr/bin:/bin
private_dir=.
artifact=archive
timeout() {
    [[ "$1" == --kill-after=* && "$2" == *s && "$3" == ssh ]] || return 98
    shift 2
    "$@"
}
sleep() { printf '%s\n' "$1" >> waits; }
ssh() {
    local outcome index
    read -ra replies <<< "$OUTCOMES"
    index=$(wc -l < calls)
    printf 'call\n' >> calls
    printf '%s\n' "$@" > "arguments-$index"
    cat > "received-$index"
    outcome=${replies[$index]}
    case "$outcome" in
        timeout) echo 'Timeout, server 192.0.2.55 not responding.' >&2; return 255 ;;
        reset) echo 'Connection reset by peer' >&2; return 255 ;;
        deadline) return 124 ;;
        hostkey) echo 'Host key verification failed.' >&2; return 255 ;;
        auth) echo 'Permission denied (publickey).' >&2; return 255 ;;
        mixed) printf '%s\n' 'Host key verification failed.' 'Connection reset by peer' >&2; return 255 ;;
        deadline_key) echo 'Host key verification failed.' >&2; return 124 ;;
        deadline_rejected) echo 'Staging outcome not confirmed; inspect before retrying.' >&2; return 124 ;;
        rejected) echo 'Staging outcome not confirmed; inspect before retrying.' >&2; return 1 ;;
        success) return 0 ;;
        *) return 99 ;;
    esac
}
"""
        bash = os.environ.get("WBOZON_TEST_BASH") or shutil.which("bash")
        self.assertIsNotNone(bash, "Bash is required for the GitHub Actions runner test")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "archive").write_bytes(PAYLOAD)
            (directory / "calls").touch()
            digest = hashlib.sha256(PAYLOAD).hexdigest()
            env = {
                "PATH": os.defpath,
                "OUTCOMES": " ".join(outcomes),
                "GITHUB_SHA": COMMIT,
                "GITHUB_RUN_ID": "381",
                "BUILD_ATTEMPT": "7",
                "digest": digest,
                "bytes": str(len(PAYLOAD)),
            }
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
                if key in os.environ:
                    env[key] = os.environ[key]
            proc = subprocess.run(
                [bash, "-s"],
                input=fakes + textwrap.dedent(loop),
                text=True,
                cwd=tmp,
                env=env,
                capture_output=True,
                timeout=8,
            )
            attempts = len((directory / "calls").read_text().splitlines())
            for i in range(attempts):
                self.assertEqual((directory / f"received-{i}").read_bytes(), PAYLOAD)
                options = (directory / f"arguments-{i}").read_text().splitlines()
                self.assertEqual(
                    options[-2:],
                    ["staging-account@portal.example.invalid", f"stage {COMMIT} 381 7 {digest} {len(PAYLOAD)}"],
                )
                self.assertIn("StrictHostKeyChecking=yes", options)
                self.assertIn("IdentitiesOnly=yes", options)
                self.assertIn("ClearAllForwardings=yes", options)
            waits = directory / "waits"
            delays = waits.read_text().splitlines() if waits.exists() else []
            return proc.returncode, attempts, delays

    def test_transient_disconnect_reopens_identical_artifact_and_build_attempt(self):
        for condition in ("timeout", "reset", "deadline"):
            with self.subTest(condition=condition):
                self.assertEqual(self.run_transfer([condition, "success"]), (0, 2, ["10"]))

    def test_receiver_rejection_and_ssh_identity_errors_stop_immediately(self):
        for condition, status in (
            ("hostkey", 255),
            ("auth", 255),
            ("mixed", 255),
            ("rejected", 1),
            ("deadline_key", 124),
            ("deadline_rejected", 124),
        ):
            with self.subTest(condition=condition):
                self.assertEqual(self.run_transfer([condition, "success"]), (status, 1, []))

    def test_persistent_failure_is_bounded_and_never_reported_as_delivered(self):
        self.assertEqual(self.run_transfer(["timeout"] * 3), (255, 3, ["10", "20"]))
        self.assertEqual(self.run_transfer(["success"]), (0, 1, []))
