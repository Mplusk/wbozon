import copy
import json
import subprocess
import sys
import unittest
from decimal import Decimal

from demand import assess
from demand.assessment import DEFAULT_WEIGHTS


def evidence(fraction):
    return {"score_fraction": fraction, "source": "Отчёт аналитика, стр. 2",
            "observed_at": "2026-11-12", "rationale": "Условное обоснование оценки"}


def sample():
    return {"factors": {name: evidence("0.6") for name in DEFAULT_WEIGHTS},
            "price_range": {"min_rub": "700", "max_rub": "1200",
                            "source": "Условный обзор цен", "observed_at": "2026-11-12"}}


class DemandAssessmentTests(unittest.TestCase):
    def test_weighted_scoring_and_threshold_are_exact(self):
        data = sample()
        result = assess(data)
        self.assertEqual(result["score"], Decimal("60.0"))
        self.assertTrue(result["passes_threshold"])
        self.assertEqual(result["points"]["volume"], Decimal("15.0"))
        self.assertIs(result["factors"], data["factors"])
        self.assertFalse(assess(data, threshold="60.01")["passes_threshold"])

    def test_no_rounding_a_failure_up_to_threshold(self):
        data = sample()
        data["factors"]["volume"]["score_fraction"] = "0.59999999999999999999999999999999999999"
        result = assess(data)
        self.assertLess(result["score"], Decimal(60))
        self.assertFalse(result["passes_threshold"])

    def test_zero_is_observed_score_while_missing_blocks(self):
        data = sample()
        data["factors"]["volume"] = evidence(0)
        result = assess(data)
        self.assertEqual(result["score"], Decimal("45.0"))
        self.assertFalse(result["passes_threshold"])
        data["factors"]["volume"]["score_fraction"] = None
        result = assess(data)
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["passes_threshold"])
        self.assertTrue(any("volume.score_fraction" in issue for issue in result["issues"]))

    def test_missing_sources_dates_and_range_block(self):
        data = sample()
        del data["factors"]["newcomers"]
        data["factors"]["price"]["source"] = " "
        data["factors"]["volume"]["observed_at"] = "2026-02-30"
        data["price_range"]["min_rub"] = "1500"
        result = assess(data)
        self.assertEqual(result["status"], "blocked")
        for field in ("newcomers", "price.source", "volume.observed_at", "price_range"):
            self.assertTrue(any(field in issue for issue in result["issues"]), field)

    def test_invalid_weights_and_values_do_not_produce_score(self):
        for change in (float("nan"), -1, 1.1, True):
            data = sample()
            data["factors"]["stability"]["score_fraction"] = change
            self.assertEqual(assess(data)["status"], "blocked")
        self.assertEqual(assess(sample(), weights={"volume": 100})["status"], "blocked")
        weights = copy.copy(DEFAULT_WEIGHTS)
        weights["volume"] = 26
        self.assertEqual(assess(sample(), weights=weights)["status"], "blocked")
        self.assertEqual(assess(sample(), threshold=101)["status"], "blocked")

    def test_cli_has_no_fabricated_score_when_incomplete(self):
        proc = subprocess.run([sys.executable, "-m", "demand"], input=json.dumps({"assessment": {}}),
                              text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 2)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["score"])
        good = subprocess.run([sys.executable, "-m", "demand"], input=json.dumps({"assessment": sample()}),
                              text=True, capture_output=True, check=False)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(Decimal(json.loads(good.stdout)["score"]), Decimal("60"))


if __name__ == "__main__":
    unittest.main()
