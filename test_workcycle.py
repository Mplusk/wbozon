import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from storage import Store
from workcycle.__main__ import run


EXAMPLE = json.loads(Path('examples/workcycle_demo.json').read_text(encoding='utf-8'))


class WorkcycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'local.sqlite'
        self.addCleanup(self.tmp.cleanup)

    def test_calculation_and_material_are_recorded_and_failures_block(self):
        result = run(copy.deepcopy(EXAMPLE), self.db)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('порог' in s for s in result['reasons']))
        self.assertEqual(result['material_source']['price_rub_per_kg'], 1000)
        with Store(self.db) as db:
            candidate = db.candidate_history('demo-organizer')[0]
            self.assertEqual(candidate['candidate']['stage'], 'idea')
            self.assertEqual(candidate['candidate']['workcycle_material_provenance']['name'], 'PLA')
            calculation = db.calculation_history(result['candidate_revision_id'])[0]
        self.assertEqual(calculation['inputs']['plastic_rub_per_kg'], 1000)
        self.assertEqual(calculation['source'], EXAMPLE['economics']['source'])

    def test_missing_economics_and_provenance_are_blocked(self):
        data = copy.deepcopy(EXAMPLE)
        del data['economics']
        result = run(data, self.db)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['calculation_id'])
        self.assertTrue(any('economics' in s for s in result['reasons']))
        data = copy.deepcopy(EXAMPLE)
        del data['economics']['source']
        result = run(data, self.db)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('source' in s for s in result['reasons']))

    def test_missing_or_bad_candidate_source_cannot_be_silently_saved(self):
        data = copy.deepcopy(EXAMPLE)
        data['observed_at'] = '2026-11-12'
        result = run(data, self.db)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('observed_at' in s for s in result['reasons']))
        with Store(self.db) as db:
            self.assertEqual(db.candidate_history('demo-organizer'), [])

    def test_explicit_price_cannot_silently_conflict(self):
        data = copy.deepcopy(EXAMPLE)
        data['economics']['inputs']['plastic_rub_per_kg'] = '1'
        result = run(data, self.db)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['calculation_id'])
        self.assertTrue(any('plastic_rub_per_kg' in s for s in result['reasons']))

    def test_success_is_only_local_review_and_never_advances(self):
        data = copy.deepcopy(EXAMPLE)
        data['economics']['inputs']['price_rub'] = '2000'
        result = run(data, self.db)
        self.assertEqual(result['status'], 'ready_for_review')
        self.assertIn('не проверены', result['note'])
        with Store(self.db) as db:
            history = db.candidate_history('demo-organizer')
        self.assertEqual(history[0]['candidate']['stage'], 'idea')

    def test_cli_reports_blocked_as_json(self):
        process = subprocess.run([sys.executable, '-m', 'workcycle', '--db', str(self.db)],
                                 input='{}', capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
