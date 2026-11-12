import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operations import readiness


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "marketplace": "ozon",
            "materials": [{"name": "PLA", "price_rub_per_kg": 1000, "price_source": "владелец"}],
            "printers": [{"model": "A", "count": 1, "purchase_price_rub": 50000,
                          "useful_life_hours": 5000, "power_kw": 0.12, "maintenance_rub_per_hour": 0}],
            "electricity_rub_per_kwh": 7, "operator_rub_per_hour": 500,
            "packaging_rub_per_unit": 10, "tax_regime": "УСН", "tax_rate": 0.06,
            "fulfillment_scheme": "FBS", "warehouse": "склад",
        }

    def test_available_data_do_not_assert_authentication_or_deployment(self):
        report = readiness.build_report(self.config, {"OZON_CLIENT_ID": "id", "OZON_API_KEY": "key"})
        self.assertTrue(all(value == "provided" for value in report["checks"].values()))
        self.assertIn("ozon_permissions_and_connectivity", report["unverified"])
        self.assertIn("scheduler", report["unverified"])
        self.assertIn("action_gateway", report["unverified"])

    def test_incomplete_and_invalid_values_are_missing(self):
        self.config["printers"] = []
        self.config["tax_rate"] = 2
        self.config["electricity_rub_per_kwh"] = False
        self.config["materials"][0]["price_source"] = ""
        report = readiness.build_report(self.config, {"OZON_CLIENT_ID": "id", "OZON_API_KEY": ""})
        for name in ("printers", "tax_regime_and_rate", "electricity_tariff", "material_prices_and_sources",
                     "ozon_credentials_present_in_this_process"):
            self.assertEqual(report["checks"][name], "missing_or_invalid")

    def test_cli_does_not_emit_secrets_or_config_contents(self):
        secret = "arbitrary-super-secret-value"
        self.config["warehouse"] = secret
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps(self.config), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"OZON_CLIENT_ID": secret, "OZON_API_KEY": secret}, clear=True), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(readiness.main(["--config", str(p)]), 0)
            self.assertNotIn(secret, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["checks"]["ozon_credentials_present_in_this_process"], "provided")

    def test_invalid_config_fails_without_echoing_path(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(readiness.main(["--config", "/missing-secret-path"]), 2)
        self.assertNotIn("missing-secret-path", output.getvalue())


if __name__ == "__main__":
    unittest.main()
