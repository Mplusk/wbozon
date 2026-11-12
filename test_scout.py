import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scout.__main__ import ImportError, import_candidate
from storage import Store


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "candidates.sqlite3"
        self.base = {"candidate_id": "test-001", "model_url": "https://example.org/models/1",
                     "observed_at": "2026-11-12T13:00:00+03:00"}

    def test_link_alone_is_recorded_as_blocked_without_fabricated_data(self):
        r = import_candidate(self.base, self.db)
        self.assertEqual(r["status"], "blocked")
        self.assertTrue(any("license" in x for x in r["blockers"]))
        with Store(self.db) as db:
            history = db.candidate_history("test-001")
        candidate = history[0]["candidate"]
        self.assertEqual(candidate["stage"], "idea")
        self.assertEqual(candidate["rights"]["status"], "unchecked")
        self.assertEqual(candidate["idea"], {"model_url": self.base["model_url"]})

    def test_passport_licenses_are_recorded_not_filtered_or_approved(self):
        record = dict(self.base, author_url="https://example.org/author/1", license="CC BY-NC",
                      photo_url="https://example.org/photo/1", print_profile_source="https://example.org/profile/1",
                      mass_g="120", print_hours="3.5")
        r = import_candidate(record, self.db)
        self.assertEqual(r["status"], "ready_for_demand_review")
        with Store(self.db) as db:
            saved = db.candidate_history("test-001")[0]["candidate"]
        self.assertEqual(saved["idea"]["license"], "CC BY-NC")
        self.assertEqual(saved["rights"]["status"], "unchecked")
        self.assertEqual(saved["stage"], "idea")

    def test_bad_urls_or_injected_fields_rejected_before_database_creation(self):
        for val in ["file:///tmp/model", "https://user:pw@example.org/model", "https://example.org/model?key=secret", "https://example.org/model#secret", "https://example.org:22/model"]:
            with self.subTest(value=val), self.assertRaises(ImportError):
                import_candidate(dict(self.base, model_url=val), self.db)
            self.assertFalse(self.db.exists())
        with self.assertRaises(ImportError):
            import_candidate(dict(self.base, approvals={"launch": True}), self.db)
        self.assertFalse(self.db.exists())

    def test_invalid_measurement_is_blocked_not_marked_complete(self):
        r = import_candidate(dict(self.base, mass_g=-5), self.db)
        self.assertTrue(any("mass_g" in x for x in r["blockers"]))

    def test_cli_input_is_recorded_without_echoing_url_secrets(self):
        proc = subprocess.run([sys.executable, "-m", "scout", "--db", str(self.db)],
                              input=json.dumps(self.base), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "blocked")
        bad = dict(self.base, model_url="https://example.org/?token=secret123")
        proc = subprocess.run([sys.executable, "-m", "scout", "--db", str(self.db)],
                              input=json.dumps(bad), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("secret123", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
