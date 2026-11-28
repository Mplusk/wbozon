import os
import sqlite3
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timezone

import content_factory_model_schedule as schedule


class Vault:
    def __init__(self, values): self.values = values
    def read(self, slot): return self.values.get(slot)


class Catalog:
    def __init__(self): self.calls = []
    def refresh(self, provider, key):
        self.calls.append((provider, key))
        if provider == 'gemini': raise RuntimeError('secret in provider error')


class ModelScheduleTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.getcwd(), '.model-schedule-test-' + uuid.uuid4().hex + '.sqlite')
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.vault = Vault({'content_factory_catalog_openai': {'api_key': 'fake-openai-secret'},
                            'content_factory_catalog_gemini': {'api_key': 'fake-gemini-secret'}})
        self.catalog = Catalog()
        self.clock = lambda: datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc).timestamp()

    def test_once_per_day_and_exception_is_sanitized(self):
        result = schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)
        self.assertEqual(result['providers'], {'openai': 'refreshed', 'gemini': 'unavailable'})
        self.assertNotIn('secret', repr(result))
        attempts = schedule.last_attempts(self.path)
        self.assertEqual(attempts['openai']['outcome'], 'refreshed')
        self.assertEqual(attempts['gemini']['outcome'], 'unavailable')
        self.assertEqual(attempts['gemini']['attempted_at'], int(self.clock()))
        self.assertNotIn('secret', repr(attempts))
        self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)['status'], 'already_attempted')
        self.assertEqual(len(self.catalog.calls), 2)
        with open(self.path, 'rb') as f: self.assertNotIn(b'fake-openai-secret', f.read())

    def test_missing_key_and_next_moscow_day(self):
        self.vault.values = {}
        result = schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)
        self.assertEqual(result['providers']['openai'], 'not_configured')
        new_day = lambda: datetime(2026, 11, 29, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=new_day)['status'], 'attempted')
        self.assertEqual(self.catalog.calls, [])

    def test_before_three_moscow_does_not_create_database_or_call_api(self):
        early = lambda: datetime(2026, 11, 27, 23, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=early)['status'], 'not_due')
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.catalog.calls, [])
        self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)['status'], 'attempted')
        self.assertEqual(len(self.catalog.calls), 2)
        self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)['status'], 'already_attempted')

    def test_missing_journal_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError): schedule.last_attempts(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_another_worker_is_busy(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE catalogue_runs(day TEXT PRIMARY KEY)')
            db.commit()
        db = sqlite3.connect(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.assertEqual(schedule.run_once(self.vault, self.catalog, self.path, now=self.clock)['status'], 'busy')
            self.assertEqual(self.catalog.calls, [])
        finally:
            db.rollback()
            db.close()


if __name__ == '__main__': unittest.main()
