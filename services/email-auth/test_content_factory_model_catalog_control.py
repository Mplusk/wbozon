import unittest
import os
import sqlite3
import uuid
import content_factory_model_catalog_control as c


class Vault:
    def __init__(self): self.values = {}
    def read(self, slot): return self.values.get(slot)
    def save(self, slot, value): self.values[slot] = value


class Catalog:
    def read(self, provider, key):
        if provider == 'gemini': raise RuntimeError('sensitive failure')
        return {'updated_at': 100, 'ids': ['model-id']}


class ControlTest(unittest.TestCase):
    def test_separate_keys_and_snapshot_without_secrets(self):
        vault = Vault()
        c.save_catalog_key(vault, 'openai', 'fake-test-key-openai')
        c.save_catalog_key(vault, 'gemini', 'fake-test-key-gemini')
        result = c.snapshot(vault, Catalog(), now=lambda: 200)
        self.assertEqual(result['openai']['status'], 'ready')
        self.assertEqual(result['gemini']['status'], 'unavailable')
        self.assertNotIn('fake-test-key', repr(result))
        self.assertEqual(c.snapshot(vault, Catalog(), now=lambda: 200000)['openai']['status'], 'stale')

    def test_missing_bad_provider_and_pending(self):
        vault = Vault()
        self.assertEqual(c.snapshot(vault, None)['openai']['status'], 'not_configured')
        with self.assertRaises(ValueError): c.save_catalog_key(vault, 'unknown', 'fake-test-key')
        with self.assertRaises(ValueError): c.save_catalog_key(vault, 'openai', 'bad\nkey')
        c.save_catalog_key(vault, 'openai', 'fake-test-key-openai')
        self.assertEqual(c.snapshot(vault, None)['openai']['status'], 'unavailable')
        self.assertEqual(c.snapshot(vault, type('C', (), {'read':lambda *_:None})())['openai']['status'], 'pending')

    def test_vault_failure_does_not_expose_error_or_break_page(self):
        class BadVault:
            def read(self, slot): raise RuntimeError('fake-secret')
            def save(self, slot, value): raise RuntimeError('fake-secret')
        self.assertNotIn('fake-secret', repr(c.snapshot(BadVault(), None)))
        with self.assertRaisesRegex(RuntimeError, '^Could not save provider key$'):
            c.save_catalog_key(BadVault(), 'openai', 'fake-secret')

    def test_last_failed_attempt_does_not_discard_fresh_cache(self):
        path = os.path.join(os.getcwd(), '.model-attempt-test-' + uuid.uuid4().hex + '.sqlite')
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE catalogue_attempts(provider TEXT PRIMARY KEY, attempted_at INTEGER, outcome TEXT)')
            db.execute('INSERT INTO catalogue_attempts VALUES (?,?,?)', ('openai', 199, 'unavailable'))
            db.commit()
        finally: db.close()
        vault = Vault()
        c.save_catalog_key(vault, 'openai', 'fake-test-key-openai')
        result = c.snapshot(vault, Catalog(), schedule_path=path, now=lambda:200)
        self.assertEqual(result['openai']['status'], 'ready')
        self.assertEqual(result['openai']['ids'], ['model-id'])
        self.assertEqual(result['openai']['last_attempt'], {'attempted_at':199, 'outcome':'unavailable'})
        self.assertEqual(c.snapshot(vault, Catalog(), schedule_path=path + '-missing', now=lambda:200)['openai']['status'], 'ready')


if __name__ == '__main__': unittest.main()
