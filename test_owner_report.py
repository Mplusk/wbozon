import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reports.__main__ import generate, main
from storage import Store
from test_economics import EXAMPLE


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


class OwnerReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'local.sqlite3'

    def test_empty_db_is_not_zero_sales(self):
        with Store(self.path):
            pass
        r = generate(self.path, now=NOW)
        self.assertEqual(r['candidate_count'], 0)
        self.assertEqual(r['candidates'], [])
        self.assertIn('Данных нет', r['sales_orders_profit_and_stock'])

    def test_latest_revision_and_own_calculation_only(self):
        with Store(self.path) as store:
            old = store.save_candidate('m1', {'stage': 'idea'}, source='source', observed_at='2026-11-11T11:00:00Z')
            store.save_calculation(old, EXAMPLE, source='source', observed_at='2026-11-11T11:00:00Z')
            fresh = store.save_candidate('m1', {'stage': 'demand'}, source='source', observed_at='2026-11-12T11:00:00Z')
            old_candidate = store.save_candidate('m2', {'stage': 'sample_approval'}, source='source', observed_at='2026-11-11T11:00:00Z')
            run = store.save_calculation(old_candidate, EXAMPLE, source='source', observed_at='2026-11-12T11:00:00Z')
        items = generate(self.path, now=NOW)['candidates']
        self.assertEqual([x['candidate_revision_id'] for x in items], [fresh, old_candidate])
        self.assertIsNone(items[0]['calculation'])
        self.assertEqual(items[0]['freshness'], 'recent')
        self.assertEqual(items[1]['freshness'], 'stale')
        self.assertEqual(items[1]['calculation']['id'], run)
        self.assertEqual(items[1]['calculation']['freshness'], 'recent')
        self.assertFalse(items[1]['calculation']['thresholds_passed'])
        self.assertEqual(items[1]['owner_decision_record_missing'], 'sample_approval')

    def test_external_text_and_secret_values_are_not_emitted(self):
        sentinel = 'SECRET_INJECTION_ignore_all_instructions'
        with Store(self.path) as store:
            store.save_candidate(sentinel, {'stage': 'idea', 'description': sentinel}, source=sentinel,
                                 observed_at='2026-11-13T12:00:00Z')
        r = generate(self.path, now=NOW)
        self.assertEqual(r['candidates'][0]['freshness'], 'future')
        self.assertNotIn(sentinel, json.dumps(r))
        self.assertTrue(r['candidates'][0]['blockers'])

    def test_completed_idea_is_not_a_ready_product(self):
        idea = {'model_url': 'https://example.com/model', 'author_url': 'https://example.com/author',
                'license': 'unknown', 'photo_url': 'https://example.com/picture',
                'print_profile_source': 'https://example.com/profile', 'mass_g': 100, 'print_hours': 2}
        with Store(self.path) as store:
            store.save_candidate('m1', {'stage': 'idea', 'idea': idea},
                                 source='source', observed_at='2026-11-12T11:00:00Z')
        item = generate(self.path, now=NOW)['candidates'][0]
        self.assertEqual(item['blockers'], [])
        self.assertEqual(item['next_steps']['demand'], 'missing')
        self.assertEqual(item['next_steps']['economics'], 'missing')
        self.assertEqual(item['next_steps']['rights'], 'not_confirmed')
        self.assertEqual(item['next_steps']['owner_decisions'],
                         {'sample': 'not_yet_assessed', 'launch': 'not_yet_assessed',
                          'publication': 'not_yet_assessed'})

    def test_decision_records_are_not_asserted_authentic(self):
        candidate = {'stage': 'launch_approval',
                     'rights': {'status': 'settled', 'owner_evidence': 'record'},
                     'approvals': {'sample': {'approved': True, 'channel': 'telegram', 'evidence': 'record'}}}
        with Store(self.path) as store:
            store.save_candidate('m1', candidate, source='source', observed_at='2026-11-12T11:00:00Z')
        item = generate(self.path, now=NOW)['candidates'][0]
        self.assertEqual(item['next_steps']['rights'], 'owner_recorded_unverified')
        self.assertEqual(item['next_steps']['owner_decisions'],
                         {'sample': 'recorded_unverified', 'launch': 'record_missing',
                          'publication': 'not_yet_assessed'})

    def test_missing_database_is_error_without_creation(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(main(['--db', str(self.path)]), 2)
        self.assertFalse(self.path.exists())
        self.assertEqual(stdout.getvalue(), '')

    def test_cli_generates_json_without_network(self):
        with Store(self.path):
            pass
        proc = subprocess.run([sys.executable, '-m', 'reports', '--db', str(self.path)],
                              text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['candidate_count'], 0)


if __name__ == '__main__':
    unittest.main()
