"""Verify configured material prices are used without hiding missing inputs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_economics import EXAMPLE


class ProductionCalculationTests(unittest.TestCase):
    def run_calc(self, inputs, *args):
        return subprocess.run([sys.executable, '-m', 'calc', *args],
                              input=json.dumps({'inputs': inputs}), text=True,
                              capture_output=True, check=False)

    def test_owner_material_prices_are_applied_and_attributed(self):
        inputs = {k: v for k, v in EXAMPLE.items() if k != 'plastic_rub_per_kg'}
        for material in ('PLA', 'PETG'):
            with self.subTest(material=material):
                result = self.run_calc(inputs, '--material', material)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['material_input']['price_rub_per_kg'], 1000)
                self.assertIn('Владелец', output['material_input']['source'])
                self.assertEqual(output['material_rub'], '126.00')

    def test_explicit_price_conflict_and_missing_data_fail_closed(self):
        conflict = self.run_calc(EXAMPLE, '--material', 'PLA')
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('plastic_rub_per_kg уже задан', conflict.stderr)
        missing = self.run_calc({}, '--material', 'PLA')
        self.assertEqual(missing.returncode, 2)
        self.assertIn('Не заданы поля', missing.stderr)
        self.assertNotIn('plastic_rub_per_kg', missing.stderr)
        unknown = self.run_calc({}, '--material', 'UNKNOWN')
        self.assertEqual(unknown.returncode, 2)
        self.assertIn('отсутствует', unknown.stderr)

    def test_uses_changed_config_and_rejects_ambiguous_or_unsourced_material(self):
        inputs = {k: v for k, v in EXAMPLE.items() if k != 'plastic_rub_per_kg'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'production.yaml'
            entry = {'name': 'PLA', 'price_rub_per_kg': 1100, 'price_source': 'Счёт от 2026-11-12'}
            path.write_text(json.dumps({'materials': [entry]}), encoding='utf-8')
            result = self.run_calc(inputs, '--material', 'PLA', '--production', str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['material_rub'], '138.60')
            path.write_text(json.dumps({'materials': [entry, entry]}), encoding='utf-8')
            self.assertEqual(self.run_calc(inputs, '--material', 'PLA', '--production', str(path)).returncode, 2)
            del entry['price_source']
            path.write_text(json.dumps({'materials': [entry]}), encoding='utf-8')
            self.assertEqual(self.run_calc(inputs, '--material', 'PLA', '--production', str(path)).returncode, 2)


if __name__ == '__main__':
    unittest.main()
