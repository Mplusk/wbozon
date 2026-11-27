import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from content_factory_schedule import Conflict, Store


def settings(**changes):
    value = {'enabled': True, 'account_id': '123', 'product_key': 'ozon:300023',
             'publication_time': '12:00', 'generation_lead_minutes': 120,
             'video_weekdays': [1], 'models': {'text': 'gpt-5.4-mini',
               'image': 'models/gemini-3.1-flash-image', 'video': 'gemini-omni-1.2-flash',
               'quality': 'gemini-3.8-flash'}, 'image_estimate_cents': 20,
             'video_estimate_cents': 90, 'monthly_budget_cents': 1000}
    value.update(changes)
    return value


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.gettempdir()) / ('cf-schedule-' + uuid.uuid4().hex + '.sqlite'))
        self.store = Store(self.path)

    def tearDown(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            Path(self.path + suffix).unlink(missing_ok=True)

    def test_cas_rejects_stale_writers_and_preserves_snapshot(self):
        self.assertIsNone(self.store.get())
        self.assertEqual(self.store.save(settings(), 0), 1)
        self.assertEqual(self.store.save(settings(publication_time='13:30'), 1), 2)
        with self.assertRaises(Conflict):
            self.store.save(settings(publication_time='14:00'), 1)
        self.assertEqual(self.store.get()['settings']['publication_time'], '13:30')

    def test_two_simultaneous_writers_have_one_winner(self):
        self.store.save(settings(), 0)
        gate = threading.Barrier(2)
        def save(value):
            gate.wait()
            try:
                return self.store.save(settings(publication_time=value), 1)
            except Conflict:
                return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ['13:00', '14:00']))
        self.assertCountEqual(results, [2, 'conflict'])

    def test_claim_uses_committed_revision_and_cannot_duplicate(self):
        self.store.save(settings(), 0)
        now = datetime.fromisoformat('2026-11-27T07:00:00+00:00')
        first = self.store.claim_due(now)
        self.assertTrue(first['claimed'])
        self.store.save(settings(publication_time='11:30', generation_lead_minutes=90), 1)
        self.assertEqual(self.store.claim_due(now)['reason'], 'already_claimed')
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT revision,scheduled_at_utc,settings_json FROM cf_slots').fetchone()
        self.assertEqual(row[:2], (1, '2026-11-27T09:00:00+00:00'))
        self.assertIn('12:00', row[2])

    def test_many_claims_create_one_slot_and_budget_reservation(self):
        self.store.save(settings(), 0)
        gate = threading.Barrier(12)
        def claim(_):
            gate.wait()
            return self.store.claim_due(datetime.fromisoformat('2026-11-27T07:00:00+00:00'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(claim, range(12)))
        self.assertEqual(sum(row['claimed'] for row in result), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT SUM(estimated_cents) FROM cf_reservations').fetchone()[0], 20)

    def test_budget_and_rollback(self):
        self.store.save(settings(monthly_budget_cents=20), 0)
        now = datetime.fromisoformat('2026-11-27T07:00:00+00:00')
        self.assertTrue(self.store.claim_due(now)['claimed'])
        self.store.save(settings(account_id='456', monthly_budget_cents=20), 1)
        self.assertEqual(self.store.claim_due(now)['reason'], 'budget_exhausted')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cf_slots').fetchone()[0], 1)

    def test_failed_reservation_rolls_back_the_slot(self):
        self.store.save(settings(), 0)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_reservation BEFORE INSERT ON cf_reservations BEGIN SELECT RAISE(ABORT,'injected'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.claim_due(datetime.fromisoformat('2026-11-27T07:00:00+00:00'))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cf_slots').fetchone()[0], 0)

    def test_cross_year_and_invalid_or_disabled_inputs(self):
        self.assertEqual(self.store.claim_due(datetime.fromisoformat('2026-11-27T07:00:00+00:00'))['reason'], 'not_configured')
        self.store.save(settings(publication_time='01:00'), 0)
        result = self.store.claim_due(datetime.fromisoformat('2026-12-31T20:00:00+00:00'))
        self.assertEqual(result['local_date'], '2027-01-01')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT month_key FROM cf_reservations').fetchone()[0], '2026-12')
        with self.assertRaises(ValueError):
            self.store.save(settings(publication_time='24:00'), 1)
        with self.assertRaises(ValueError):
            self.store.save({**settings(), 'api_key': 'secret'}, 1)
        with self.assertRaises(ValueError):
            self.store.save(settings(models={'text':'sk-secret','image':'x','video':'x','quality':'x'}), 1)
        self.store.save(settings(enabled=False), 1)
        self.assertEqual(self.store.claim_due(datetime.now().astimezone())['reason'], 'disabled')

    def test_scheduler_does_not_catch_up_after_the_start_minute(self):
        self.store.save(settings(), 0)
        self.assertEqual(self.store.claim_due(datetime.fromisoformat('2026-11-27T08:59:00+00:00'))['reason'], 'not_due')
        self.assertEqual(self.store.claim_due(datetime.fromisoformat('2026-11-27T07:01:00+00:00'))['reason'], 'not_due')
        self.assertTrue(self.store.claim_due(datetime.fromisoformat('2026-11-27T07:00:59+00:00'))['claimed'])

    def test_only_current_pilot_model_ids_are_accepted(self):
        original = settings()
        for role in ('text', 'image', 'video', 'quality'):
            bad = {**original, 'models': {**original['models'], role: 'gpt-made-up'}}
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.store.save(bad, 0)
        self.assertIsNone(self.store.get())


if __name__ == '__main__':
    unittest.main()
