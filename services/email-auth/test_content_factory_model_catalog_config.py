import os
import shutil
import sqlite3
import unittest
import uuid

import content_factory_model_catalog_config as config
import content_factory_model_schedule as schedule


class Vault:
    def __init__(self, path): self.path = path
    def read(self, _slot): return None


class CatalogConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = os.path.join(os.getcwd(), '.catalog-config-test-' + uuid.uuid4().hex)
        os.mkdir(self.base)
        self.addCleanup(lambda: shutil.rmtree(self.base))
        self.path = os.path.join(self.base, 'catalog')
        os.mkdir(self.path)
        self.vault = Vault(os.path.join(self.base, 'vault.sqlite'))
        self.env = {'CF_MODEL_CATALOG_ENABLED': '1', 'CF_MODEL_CATALOG_STAGING_VERIFIED': '1',
                    'CF_MODEL_CATALOG_DATA_DIR': self.path, 'VAULT_PATH': self.vault.path,
                    'DATABASE_PATH': os.path.join(self.base, 'auth.sqlite')}
        self.secret = lambda *_: b'a' * 32

    def test_disabled_ignores_configuration_and_secret(self):
        self.assertIsNone(config.configure({}, self.vault, lambda *_: 1/0))
        self.assertEqual(os.listdir(self.path), [])

    def test_separate_valid_and_reopened(self):
        catalogue, lock = config.configure(self.env, self.vault, self.secret)
        self.assertEqual(catalogue.read('openai', 'fake-api-key'), None)
        self.assertEqual(lock, os.path.join(self.path, config.LOCK))
        self.assertIsNotNone(config.configure(self.env, self.vault, self.secret))

    def test_configure_run_configure_and_status(self):
        catalogue, lock = config.configure(self.env, self.vault, self.secret)
        outcome = schedule.run_once(self.vault, catalogue, lock, now=lambda: 1796000000)
        self.assertEqual(outcome['status'], 'attempted')
        _, reopened_lock = config.configure(self.env, self.vault, self.secret)
        attempts = schedule.last_attempts(reopened_lock)
        self.assertEqual(attempts['openai']['outcome'], 'not_configured')
        self.assertEqual(attempts['gemini']['attempted_at'], 1796000000)
        self.assertEqual(schedule.run_once(self.vault, catalogue, lock, now=lambda: 1796000000)['status'], 'already_attempted')

    def test_configure_while_scheduler_holds_write_lock(self):
        catalogue, lock = config.configure(self.env, self.vault, self.secret)
        schedule.run_once(self.vault, catalogue, lock, now=lambda: 1796000000)
        db = sqlite3.connect(lock)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.assertIsNotNone(config.configure(self.env, self.vault, self.secret))
            self.assertEqual(schedule.run_once(self.vault, catalogue, lock, now=lambda: 1796100000)['status'], 'busy')
        finally:
            db.rollback()
            db.close()

    def test_refuses_other_gateway_or_vault_parent(self):
        self.env['CF_GATEWAY_DATA_DIR'] = self.path
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)
        self.env.pop('CF_GATEWAY_DATA_DIR')
        self.env['CF_MODEL_CATALOG_DATA_DIR'] = self.base
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)

    def test_rejects_preexisting_unrelated_database(self):
        path = os.path.join(self.path, config.CATALOG)
        with open(path, 'wb') as f: f.write(b'unrelated')
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)
        with open(path, 'rb') as f: self.assertEqual(f.read(), b'unrelated')

    def test_rejects_hardlink_to_vault(self):
        with open(self.vault.path, 'wb') as f: f.write(b'vault')
        try: os.link(self.vault.path, os.path.join(self.path, config.CATALOG))
        except OSError: self.skipTest('Hardlinks unavailable')
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)
        with open(self.vault.path, 'rb') as f: self.assertEqual(f.read(), b'vault')

    def test_rejects_non_dedicated_and_unverified(self):
        with open(os.path.join(self.path, 'extra'), 'w') as f: f.write('keep')
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)
        self.env['CF_MODEL_CATALOG_STAGING_VERIFIED'] = '0'
        with self.assertRaises(ValueError): config.configure(self.env, self.vault, self.secret)


if __name__ == '__main__': unittest.main()
