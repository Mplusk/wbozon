"""Two synthetic WSGI workers sharing one encrypted owner vault."""
import gc
import hashlib
import os
import secrets
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import content_factory_model_catalog_vault as module
from app import LoginApp
from portal import Portal, Vault
import test_portal


class CatalogOptInPortalTests(unittest.TestCase):
    request = test_portal.Tests.request

    def setUp(self):
        self.root = os.path.join(os.getcwd(), 'cf-optin-test-' + uuid.uuid4().hex)
        os.mkdir(self.root, 0o700 if os.name == 'posix' else 0o777)
        self.now = 100000
        self.key = secrets.token_bytes(32)
        self.vault_path = os.path.join(self.root, 'vault.sqlite')
        self.login = LoginApp(os.path.join(self.root, 'login.sqlite'), 'x'*32,
                              'https://3dprinta.shop', clock=lambda: self.now)
        self.vault = Vault(self.vault_path, self.key)
        if os.name == 'posix': os.chmod(self.vault_path, 0o600)
        self.env = {'DATABASE_PATH': self.login.db, 'VAULT_PATH': self.vault_path}
        self.app = self.make_worker()
        self.sid, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)',
                       (self.login.digest(self.sid), self.now + 43200, self.csrf))
        self.patches = [patch.object(module, 'FEATURE_AVAILABLE', True),
                        patch.dict(os.environ, self.env, clear=True),
                        patch('portal.secret_file', return_value=self.key)]
        original_enable, original_load = module.enable, module.load
        self.patches += [patch.object(module, 'enable', side_effect=lambda vault, env, reader, **kw:
                                      original_enable(vault, env, reader, expected_vault_path=self.vault_path, **kw)),
                         patch.object(module, 'load', side_effect=lambda vault, env, reader:
                                      original_load(vault, env, reader, expected_vault_path=self.vault_path))]
        if os.name == 'nt':
            original_mkdir = os.mkdir
            self.patches.append(patch.object(module.os, 'mkdir', side_effect=lambda path, mode=0o777: original_mkdir(path)))
        for p in self.patches: p.start()

    def make_worker(self):
        return Portal(self.login, self.vault, hashlib.sha256(b'test-read').hexdigest(),
                      lambda *_: {'items':[]}, catalog_setup_feature=True)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        gc.collect()
        directory = os.path.join(self.root, module.DIRECTORY)
        if os.path.isdir(directory):
            for name in os.listdir(directory): os.unlink(os.path.join(directory, name))
            os.rmdir(directory)
        for name in os.listdir(self.root): os.unlink(os.path.join(self.root, name))
        os.rmdir(self.root)

    def test_opt_in_auth_and_second_worker(self):
        path, fields = '/settings/content-factory/catalog/enable', {'csrf':self.csrf}
        self.assertTrue(self.request(path, 'POST', fields, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path, 'POST', fields, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path, 'POST', {'csrf':'bad'})['status'].startswith('403'))
        self.now += 301
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('403'))
        self.now -= 301
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('303'))
        self.assertEqual(self.vault.read(module.SLOT), {'version':1, 'enabled':True})
        worker2 = self.make_worker()
        self.assertIsNone(worker2.model_catalog)
        self.assertTrue(worker2.model_catalog_ready())
        self.assertTrue(worker2.model_catalog.read_only)
        self.app = worker2
        key = 'synthetic-provider-key'
        result = self.request('/settings/content-factory/catalog/key', 'POST',
                              {'csrf':self.csrf, 'provider':'openai', 'api_key':key})
        self.assertTrue(result['status'].startswith('303'), result['status'])
        self.assertNotIn(key, result['body'])
        self.assertEqual(self.vault.read('content_factory_catalog_openai'), {'api_key':key})

    def test_two_workers_do_not_create_two_catalogues(self):
        first, second = self.make_worker(), self.make_worker()
        clients = [SimpleNamespace(app=worker, sid=self.sid, csrf=self.csrf) for worker in (first, second)]
        def submit(client):
            return test_portal.Tests.request(client, '/settings/content-factory/catalog/enable',
                                             'POST', {'csrf':self.csrf})['status']
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, clients))
        self.assertEqual(sum(s.startswith('303') for s in results), 1, results)
        self.assertTrue(all(s.startswith(('303','400','503')) for s in results), results)
        self.assertEqual(self.vault.read(module.SLOT), {'version':1, 'enabled':True})
        self.assertTrue(first.model_catalog_ready())
        self.assertTrue(second.model_catalog_ready())
        self.assertTrue(first.model_catalog.read_only)
        self.assertTrue(second.model_catalog.read_only)

    def test_load_failure_after_enable_does_not_offer_another_setup(self):
        path = '/settings/content-factory/catalog/enable'
        with patch.object(module, 'load', side_effect=RuntimeError('synthetic error')):
            result = self.request(path, 'POST', {'csrf':self.csrf})
            self.assertTrue(result['status'].startswith('503'), result['status'])
            self.assertIn('Настройка каталога сохранена', result['body'])
            self.assertNotIn('synthetic error', result['body'])
            self.assertEqual(self.vault.read(module.SLOT), {'version':1, 'enabled':True})
            with patch('content_factory.snapshot', return_value={'status':'unavailable'}):
                page = self.request('/settings', headers={'QUERY_STRING':'tab=content-factory'})
            self.assertTrue(page['status'].startswith('200'), page['status'])
            self.assertIn('Настройка каталога сохранена', page['body'])
            self.assertNotIn('action="/settings/content-factory/catalog/enable"', page['body'])
        self.assertTrue(self.app.model_catalog_ready())
        self.assertTrue(self.app.model_catalog.read_only)


if __name__ == '__main__': unittest.main()
