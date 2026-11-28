import gc
import hashlib
import os
import secrets
import sqlite3
import time
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from portal import Vault
import content_factory_model_catalog_vault as module


class CatalogVaultTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(os.getcwd(), 'cf-catalog-vault-test-' + uuid.uuid4().hex)
        os.mkdir(self.root, 0o700 if os.name == 'posix' else 0o777)
        self.vault_path = os.path.join(self.root, 'vault.sqlite')
        self.key = secrets.token_bytes(32)
        self.vault = Vault(self.vault_path, self.key)
        if os.name == 'posix': os.chmod(self.vault_path, 0o600)
        self.environ = {'DATABASE_PATH': os.path.join(self.root, 'login.sqlite')}
        self.secret_reader = lambda name, binary: self.key
        self.patch = patch.object(module, 'FEATURE_AVAILABLE', True)
        self.patch.start()
        if os.name == 'nt':
            original_mkdir = os.mkdir
            self.mkdir_patch = patch.object(module.os, 'mkdir', side_effect=lambda path, mode=0o777: original_mkdir(path))
            self.mkdir_patch.start()

    def tearDown(self):
        self.patch.stop()
        if hasattr(self, 'mkdir_patch'): self.mkdir_patch.stop()
        gc.collect()
        # This test creates only flat files in these two unique directories.
        directory = os.path.join(self.root, module.DIRECTORY)
        if os.path.isdir(directory):
            for name in os.listdir(directory): os.unlink(os.path.join(directory, name))
            os.rmdir(directory)
        for name in os.listdir(self.root): os.unlink(os.path.join(self.root, name))
        os.rmdir(self.root)

    def enable(self):
        return module.enable(self.vault, self.environ, self.secret_reader,
                             confirmed=True, expected_vault_path=self.vault_path)

    def load(self):
        return module.load(self.vault, self.environ, self.secret_reader,
                           expected_vault_path=self.vault_path)

    def test_owner_opt_in_load_and_duplicate_refused(self):
        self.assertIsNone(self.load())
        catalog, _ = self.enable()
        catalog.refresh('openai', 'synthetic-key', get=lambda *_: {'data': [{'id': 'gpt-test'}]})
        with self.assertRaises(ValueError): self.enable()
        catalog, _ = self.load()
        self.assertEqual(catalog.read('openai', 'synthetic-key')['ids'], ['gpt-test'])

    def test_failed_setup_preserves_orphan_and_refuses_retry(self):
        with self.assertRaises(Exception):
            module.enable(self.vault, self.environ, lambda *_: b'wrong',
                          confirmed=True, expected_vault_path=self.vault_path)
        self.assertIsNone(self.vault.read(module.SLOT))
        self.assertTrue(module.setup_pending(self.vault, expected_vault_path=self.vault_path))
        with self.assertRaises(ValueError): self.enable()

    def test_setup_pending_is_read_only_and_fails_closed(self):
        self.assertFalse(module.setup_pending(self.vault, expected_vault_path=self.vault_path))
        self.assertFalse(os.path.exists(os.path.join(self.root, module.DIRECTORY)))
        self.enable()
        self.assertTrue(module.setup_pending(self.vault, expected_vault_path=self.vault_path))
        with patch.object(self.vault, 'read', side_effect=RuntimeError('unreadable')):
            self.assertTrue(module.setup_pending(self.vault, expected_vault_path=self.vault_path))

    def test_disabled_feature_and_missing_confirmation_fail_closed(self):
        with self.assertRaises(PermissionError):
            module.enable(self.vault, self.environ, self.secret_reader,
                          expected_vault_path=self.vault_path)
        with patch.object(module, 'FEATURE_AVAILABLE', False):
            self.assertIsNone(self.load())
            with self.assertRaises(PermissionError): self.enable()

    def test_load_is_read_only_and_never_recreates_missing_cache(self):
        catalog, _ = self.enable()
        path = os.path.join(self.root, module.DIRECTORY, 'catalog.sqlite3')
        catalog.refresh('openai', 'synthetic-key', get=lambda *_: {'data': [{'id':'gpt-test'}]})
        with open(path, 'rb') as f: before = hashlib.sha256(f.read()).digest()
        existing, _ = self.load()
        self.assertEqual(existing.read('openai', 'synthetic-key')['ids'], ['gpt-test'])
        with self.assertRaises(ValueError): existing.refresh('openai', 'synthetic-key')
        with open(path, 'rb') as f: self.assertEqual(before, hashlib.sha256(f.read()).digest())
        os.unlink(path)
        with self.assertRaises(Exception): self.load()
        self.assertFalse(os.path.exists(path))

    def test_corrupt_or_locked_cache_fails_closed(self):
        self.enable()
        path = os.path.join(self.root, module.DIRECTORY, 'catalog.sqlite3')
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertRaises(Exception): self.load()
            self.assertLess(time.monotonic() - started, 2)
            db.rollback()
        with closing(sqlite3.connect(path)) as db:
            db.execute('DROP TABLE catalogue')
            db.execute('CREATE TABLE catalogue(unexpected TEXT)')
            db.commit()
        with self.assertRaises(ValueError): self.load()


if __name__ == '__main__': unittest.main()
