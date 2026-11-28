import hashlib
import gc
import os
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from content_factory_model_catalog_cli import _ReadOnlyVault
import content_factory_model_catalog_cli as cli
from portal import Vault


class ReadOnlyVaultTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.getcwd(), 'catalog-vault-test-' + uuid.uuid4().hex + '.sqlite')
        self.key = b'v' * 32
        vault = Vault(self.path, self.key)
        vault.save('content_factory_catalog_openai', {'api_key': 'synthetic-provider-key'})
        vault.save('content_factory_catalog_setup', {'version': 1, 'enabled': True})
        vault.save('other', {'api_key': 'other-secret'})

    def tearDown(self):
        gc.collect()
        for suffix in ('', '-journal', '-shm', '-wal'):
            path = self.path + suffix
            if os.path.exists(path): os.unlink(path)

    def test_reads_only_catalogue_slots_without_changing_database(self):
        with open(self.path, 'rb') as file: before = hashlib.sha256(file.read()).digest()
        reader = _ReadOnlyVault(self.path, self.key)
        self.assertEqual(reader.read('content_factory_catalog_openai'), {'api_key': 'synthetic-provider-key'})
        self.assertIsNone(reader.read('content_factory_catalog_gemini'))
        self.assertEqual(reader.read('content_factory_catalog_setup'), {'version': 1, 'enabled': True})
        with self.assertRaises(ValueError): reader.read('other')
        with open(self.path, 'rb') as file: after = hashlib.sha256(file.read()).digest()
        self.assertEqual(before, after)

    def test_invalid_key_and_corrupt_data_fail_closed(self):
        with self.assertRaises(ValueError): _ReadOnlyVault(self.path, b'wrong')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE vault SET blob=? WHERE provider=?', (b'x' * 8193, 'content_factory_catalog_openai'))
        with self.assertRaises(ValueError): _ReadOnlyVault(self.path, self.key).read('content_factory_catalog_openai')

    def test_cli_does_not_dispatch_without_owner_marker(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM vault WHERE provider=?', ('content_factory_catalog_setup',))
        env = {'CF_MODEL_CATALOG_ENABLED': '1', 'VAULT_PATH': self.path}
        with patch.dict(os.environ, env, clear=True), patch.object(cli.sys, 'argv', ['cli']), \
             patch('portal.secret_file', return_value=self.key), \
             patch('content_factory_model_catalog_config.run') as run:
            if hasattr(cli.signal, 'alarm'):
                with patch.object(cli.signal, 'alarm'): self.assertEqual(cli.main(), 0)
            else:
                self.assertEqual(cli.main(), 0)
            run.assert_not_called()
            Vault(self.path, self.key).save('content_factory_catalog_setup', {'version':1, 'enabled':True})
            run.return_value = {'status':'attempted'}
            if hasattr(cli.signal, 'alarm'):
                with patch.object(cli.signal, 'alarm'): self.assertEqual(cli.main(), 0)
            else:
                self.assertEqual(cli.main(), 0)
            run.assert_called_once()


if __name__ == '__main__': unittest.main()
