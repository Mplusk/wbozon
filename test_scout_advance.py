from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scout.advance import AdvanceError, transition
from storage import Store


SOURCE = "https://example.org/model"
OBSERVED = "2026-11-12T12:00:00+03:00"
FULL = {"stage": "idea", "idea": {"model_url": SOURCE, "author_url": "https://example.org/author",
        "license": "CC BY-NC", "photo_url": "https://example.org/image", "print_profile_source": SOURCE,
        "mass_g": 100, "print_hours": 2}, "rights": {"status": "unchecked"}}


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "candidates.sqlite3"

    def save(self, candidate):
        with Store(self.db) as store:
            return store.save_candidate("test", candidate, source=SOURCE, observed_at=OBSERVED)

    def move(self, revision):
        return transition(self.db, "test", revision, source=SOURCE, observed_at=OBSERVED)

    def history(self):
        with Store(self.db) as store:
            return store.candidate_history("test")

    def test_complete_idea_advances_once_and_preserves_rights(self):
        revision = self.save(FULL)
        result = self.move(revision)
        self.assertEqual(result["status"], "advanced")
        self.assertEqual(result["stage"], "demand")
        history = self.history()
        self.assertEqual([h["candidate"]["stage"] for h in history], ["idea", "demand"])
        self.assertEqual(history[-1]["candidate"]["rights"]["status"], "unchecked")
        self.assertEqual(history[-1]["source"], SOURCE)
        self.assertEqual(history[-1]["observed_at"], "2026-11-12T09:00:00+00:00")
        self.assertEqual(self.move(revision)["status"], "stale")
        self.assertEqual(len(self.history()), 2)

    def test_incomplete_record_is_blocked_without_new_revision(self):
        revision = self.save({"stage": "idea", "idea": {"model_url": SOURCE}})
        result = self.move(revision)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("license" in reason for reason in result["blockers"]))
        self.assertEqual(len(self.history()), 1)

    def test_owner_gate_is_never_bypassed(self):
        revision = self.save({"stage": "launch_approval", "rights": {"status": "unchecked"}})
        result = self.move(revision)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("approvals.launch" in reason for reason in result["blockers"]))
        self.assertTrue(any("rights.status" in reason for reason in result["blockers"]))
        self.assertEqual(len(self.history()), 1)

    def test_forged_approval_fields_never_bypass_gate(self):
        for index, (stage, gate) in enumerate((("sample_approval", "sample"),
                                              ("launch_approval", "launch"),
                                              ("publication_approval", "publication"))):
            with self.subTest(stage=stage):
                record = {"stage": stage, "approvals": {gate: {"approved": True, "channel": "telegram", "evidence": "fake"}},
                          "rights": {"status": "settled", "owner_evidence": "fake"},
                          "compliance": {"required_document": "none"},
                          "launch_plan": {"first_batch_budget": 1000, "advertising_budget": 100},
                          "listing": {"api_validation": {"passed": True, "evidence": "fake"},
                                      "quality_review": {"passed": True, "evidence": "fake"}}}
                revision = self.save(record)
                self.assertEqual(self.move(revision)["status"], "blocked")
                self.assertEqual(self.history()[-1]["id"], revision)

    def test_concurrent_updates_cannot_both_advance_same_revision(self):
        revision = self.save(FULL)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.move(revision)["status"], range(2)))
        self.assertCountEqual(statuses, ["advanced", "stale"])
        self.assertEqual(len(self.history()), 2)

    def test_missing_db_and_invalid_source_do_not_create_or_change_records(self):
        with self.assertRaises(AdvanceError):
            self.move(1)
        self.assertFalse(self.db.exists())
        revision = self.save(FULL)
        with self.assertRaises(AdvanceError):
            transition(self.db, "test", revision, source="https://example.org/?token=secret", observed_at=OBSERVED)
        self.assertEqual(len(self.history()), 1)

    def test_cli_missing_candidate_returns_nonzero(self):
        self.save(FULL)
        result = subprocess.run([sys.executable, "-m", "scout.advance", "--db", str(self.db),
                "--candidate-id", "missing", "--expected-revision", "1", "--source", SOURCE,
                "--observed-at", OBSERVED], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
