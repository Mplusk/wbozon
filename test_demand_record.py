import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from demand.record import save_assessment
from pipeline.candidate import blockers
from storage import Store
from test_demand_assessment import sample


class DemandRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "products.sqlite3"
        with Store(self.db_path) as store:
            self.original_id = store.save_candidate("demo", {"stage": "demand", "idea": {"model_url": "https://example.org/item"}},
                                                    source="Источник кандидата", observed_at="2026-11-12T09:00:00Z")

    def save(self, data=None, **overrides):
        options = {"source": "Пакет аналитики", "observed_at": "2026-11-12T10:00:00Z", **overrides}
        with Store(self.db_path) as store:
            return save_assessment(store, "demo", sample() if data is None else data, **options)

    def test_saved_revision_is_pipeline_compatible_and_preserves_evidence(self):
        result = self.save()
        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["previous_revision_id"], self.original_id)
        with Store(self.db_path) as store:
            records = store.candidate_history("demo")
        self.assertEqual(len(records), 2)
        saved = records[-1]["candidate"]
        self.assertEqual(saved["stage"], "demand")
        self.assertEqual(saved["idea"]["model_url"], "https://example.org/item")
        self.assertEqual(blockers(saved), [])
        self.assertEqual(saved["demand"]["assessment"]["factors"]["volume"]["source"], "Отчёт аналитика, стр. 2")
        self.assertEqual(saved["demand"]["assessment"]["points"]["volume"], "15.0")
        self.assertEqual(records[0]["candidate"].get("demand"), None)

    def test_incomplete_and_wrong_stage_do_not_write(self):
        incomplete = sample()
        del incomplete["factors"]["volume"]["source"]
        self.assertEqual(self.save(incomplete)["status"], "blocked")
        with Store(self.db_path) as store:
            store.save_candidate("demo", {"stage": "idea"}, source="Описание", observed_at="2026-11-12T11:00:00Z")
        self.assertEqual(self.save()["status"], "blocked")
        with Store(self.db_path) as store:
            self.assertEqual(len(store.candidate_history("demo")), 2)

    def test_invalid_source_and_unknown_candidate_do_not_write(self):
        self.assertEqual(self.save(source="")["status"], "blocked")
        with Store(self.db_path) as store:
            result = save_assessment(store, "missing", sample(), source="Источники", observed_at="2026-11-12T10:00:00Z")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(len(store.candidate_history("demo")), 1)

    def test_two_concurrent_updates_use_latest_revision(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.save(), range(2)))
        self.assertEqual([r["status"] for r in results], ["assessed", "assessed"])
        with Store(self.db_path) as store:
            history = store.candidate_history("demo")
        self.assertEqual(len(history), 3)
        self.assertEqual({r["previous_revision_id"] for r in results}, {history[0]["id"], history[1]["id"]})

    def test_cli_records_and_requires_existing_database(self):
        request = {"candidate_id": "demo", "assessment": sample(), "source": "Пакет аналитики", "observed_at": "2026-11-12T10:00:00Z"}
        cmd = [sys.executable, "-m", "demand.record_cli", "--db", str(self.db_path)]
        proc = subprocess.run(cmd, input=json.dumps(request), text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "assessed")
        cmd[-1] = str(self.db_path.parent / "missing.sqlite3")
        proc = subprocess.run(cmd, input=json.dumps(request), text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(cmd[-1]).exists())

    def test_below_threshold_is_saved_but_not_a_pass(self):
        data = sample()
        data["factors"]["volume"]["score_fraction"] = 0
        result = self.save(data)
        self.assertEqual(result["status"], "assessed")
        self.assertFalse(result["passes_threshold"])
        with Store(self.db_path) as store:
            self.assertTrue(any("ниже порога" in issue for issue in blockers(store.candidate_history("demo")[-1]["candidate"])))


if __name__ == "__main__":
    unittest.main()
