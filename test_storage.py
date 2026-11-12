import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from calc import CalculationError
from storage import Store, StorageError
from test_economics import EXAMPLE


class StoreTests(unittest.TestCase):
    def test_persistent_history_and_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.sqlite3"
            with Store(path) as store:
                a = store.save_candidate("model-1", {"stage": "idea", "idea": {"mass_g": 120}},
                                         source="https://example.org/models/1", observed_at="2026-11-12T10:30:00+03:00")
                b = store.save_candidate("model-1", {"stage": "demand"}, source="Решение владельца", observed_at="2026-11-12T08:00:00Z")
                run_id = store.save_calculation(a, EXAMPLE, source="Тестовые данные ТЗ v2", observed_at="2026-11-12T08:00:00Z")
                self.assertEqual(len(store.candidate_history("model-1")), 2)
                self.assertEqual(store.candidate_history("model-1")[0]["observed_at"], "2026-11-12T07:30:00+00:00")
                self.assertNotEqual(a, b)
                run = store.calculation_history(a)[0]
                self.assertEqual(run["id"], run_id)
                self.assertAlmostEqual(Decimal(run["result"]["profit_rub"]), Decimal("71.67"), places=2)
                self.assertEqual(run["limits"]["margin_rate"], "0.20")
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            with Store(path) as store:
                self.assertEqual(store.candidate_history("model-1")[0]["candidate"]["idea"]["mass_g"], 120)
                self.assertEqual(len(store.calculation_history(a)), 1)

    def test_rejections_do_not_write_incomplete_records(self):
        with Store(":memory:") as store:
            for source, observed_at in [("https://example.org/?token=oops", "2026-11-12T00:00:00Z"),
                                        ("data", "2026-11-12T00:00:00")]:
                with self.subTest(source=source), self.assertRaises(StorageError):
                    store.save_candidate("id", {"stage": "idea"}, source=source, observed_at=observed_at)
            with self.assertRaises(StorageError):
                store.save_candidate("id", {"stage": "idea", "data": {"Api-Key": "secret"}}, source="x", observed_at="2026-11-12T00:00:00Z")
            self.assertEqual(store.candidate_history("id"), [])
            rev = store.save_candidate("id", {"stage": "idea"}, source="x", observed_at="2026-11-12T00:00:00Z")
            with self.assertRaises(CalculationError):
                store.save_calculation(rev, {}, source="x", observed_at="2026-11-12T00:00:00Z")
            self.assertEqual(store.calculation_history(rev), [])
            with self.assertRaises(StorageError):
                store.save_calculation(999, EXAMPLE, source="x", observed_at="2026-11-12T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
