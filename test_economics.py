import copy
import json
import subprocess
import sys
import unittest
from decimal import Decimal

from calc import CalculationError, calculate


# Example from section 7.3; all values are illustrative, not marketplace tariffs.
EXAMPLE = {
    "mass_g": "120", "print_hours": "3.5", "waste_rate": "0.05",
    "plastic_rub_per_kg": "1200", "power_kw": "0.12",
    "electricity_rub_per_kwh": "7", "depreciation_rub_per_hour": "12",
    "maintenance_rub_per_hour": "3", "manual_minutes": "4",
    "labor_rub_per_hour": "500", "defect_rate": "0.05",
    "packaging_rub": "35", "inbound_delivery_rub": "0", "price_rub": "790",
    "commission_rate": "0.25", "outbound_logistics_rub": "60",
    "return_logistics_rub": "50", "buyout_rate": "0.90",
    "storage_acceptance_rub": "3", "acquiring_rate": "0.02", "ad_rate": "0.12",
    "tax_rate": "0.06",
}


class EconomicsTests(unittest.TestCase):
    def test_specification_reference_example(self):
        r = calculate(EXAMPLE)
        self.assertEqual(r["material_rub"], Decimal("151.2"))
        self.assertEqual(r["energy_rub"], Decimal("2.94"))
        self.assertEqual(r["equipment_rub"], Decimal("52.50"))
        self.assertAlmostEqual(r["labor_rub"], Decimal("33.33"), places=2)
        self.assertAlmostEqual(r["printing_per_good_item_rub"], Decimal("252.60"), places=2)
        self.assertAlmostEqual(r["logistics_per_buyout_rub"], Decimal("72.22"), places=2)
        self.assertAlmostEqual(r["profit_rub"], Decimal("71.67"), places=2)
        self.assertAlmostEqual(r["margin_rate"], Decimal("0.091"), places=3)
        self.assertAlmostEqual(r["stress_profit_rub"], Decimal("-42.35"), places=2)
        self.assertEqual(r["price_for_target_margin_rub"], Decimal("1036.65"))
        self.assertFalse(r["passes_all"])

    def test_thresholds_and_rounded_minimum_are_effective(self):
        x = dict(EXAMPLE, price_rub="2000")
        default = calculate(x)
        self.assertTrue(default["passes_all"])
        self.assertFalse(calculate(x, {"profit_rub": "10000"})["checks"]["profit"])
        floor = default["minimum_price_rub"]
        self.assertGreaterEqual(calculate(dict(x, price_rub=str(floor)))["margin_rate"], Decimal("0.10"))
        self.assertLess(calculate(dict(x, price_rub=str(floor - Decimal("0.01"))))["margin_rate"], Decimal("0.10"))

    def test_unattainable_margin_and_invalid_stress_do_not_pass(self):
        x = dict(EXAMPLE, commission_rate="0.80", buyout_rate="0.10")
        r = calculate(x)
        self.assertIsNone(r["minimum_price_rub"])
        self.assertIsNone(r["price_for_target_margin_rub"])
        self.assertIsNone(r["stress_profit_rub"])
        self.assertFalse(r["checks"]["stress"])
        self.assertFalse(r["passes_all"])

    def test_missing_invalid_and_unknown_inputs_are_rejected(self):
        for name, value in (("buyout_rate", "0"), ("defect_rate", "1"),
                            ("print_hours", "0"), ("price_rub", "NaN"),
                            ("ad_rate", "12"), ("tax_rate", None), ("mass_g", -1)):
            with self.subTest(name=name), self.assertRaises(CalculationError):
                calculate(dict(EXAMPLE, **{name: value}))
        x = copy.copy(EXAMPLE)
        del x["tax_rate"]
        with self.assertRaisesRegex(CalculationError, "tax_rate"):
            calculate(x)
        with self.assertRaises(CalculationError):
            calculate(dict(EXAMPLE, wrong_field=0))
        with self.assertRaises(CalculationError):
            calculate(EXAMPLE, {"wrong_limit": 1})

    def test_cli_outputs_json_and_errors_without_traceback(self):
        good = subprocess.run([sys.executable, "-m", "calc"], input=json.dumps({"inputs": EXAMPLE}),
                              text=True, capture_output=True, check=False)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertFalse(json.loads(good.stdout)["passes_all"])
        bad = subprocess.run([sys.executable, "-m", "calc"], input='{"inputs":{}}',
                             text=True, capture_output=True, check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("Traceback", bad.stderr)


if __name__ == "__main__":
    unittest.main()
