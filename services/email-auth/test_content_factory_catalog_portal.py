"""Full WSGI-route checks using a real encrypted vault and synthetic keys."""
import gc
import hashlib
import os
import secrets
import shutil
import time
import unittest
import uuid
from unittest.mock import patch

from app import LoginApp
from portal import Portal, Vault
from portal import create_portal
import test_portal


class CatalogPortalTests(unittest.TestCase):
    request = test_portal.Tests.request

    def setUp(self):
        self.prefix = os.path.join(os.getcwd(), 'cf-catalog-test-' + uuid.uuid4().hex)
        self.now = 100000
        self.login = LoginApp(self.prefix + '-login.sqlite', 'x' * 32, 'https://3dprinta.shop', clock=lambda:self.now)
        self.vault = Vault(self.prefix + '-vault.sqlite', secrets.token_bytes(32))
        self.app = Portal(self.login, self.vault, hashlib.sha256(b'test-read').hexdigest(), lambda *_:{'items':[]})
        self.sid, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (self.login.digest(self.sid), self.now + 43200, self.csrf))
        class Catalog:
            def read(self, provider, key):
                return {'updated_at': int(time.time()), 'ids':['gpt-synthetic-model']} if provider == 'openai' else None
        self.app.model_catalog = Catalog()

    def tearDown(self):
        gc.collect()
        for suffix in ('-login.sqlite', '-login.sqlite-journal', '-login.sqlite-wal', '-login.sqlite-shm',
                       '-vault.sqlite', '-vault.sqlite-journal', '-vault.sqlite-wal', '-vault.sqlite-shm'):
            path = self.prefix + suffix
            if os.path.exists(path): os.unlink(path)

    def test_route_auth_csrf_origin_recent_login_and_disabled(self):
        path = '/settings/content-factory/catalog/key'
        form = {'csrf':self.csrf, 'provider':'openai', 'api_key':'synthetic-provider-key'}
        self.assertTrue(self.request(path, 'POST', form, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path, 'POST', form, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path, 'POST', {**form, 'csrf':'x' * 32})['status'].startswith('403'))
        self.now += 301
        self.assertTrue(self.request(path, 'POST', form)['status'].startswith('403'))
        self.now -= 301
        self.app.model_catalog = None
        self.assertTrue(self.request(path, 'POST', form)['status'].startswith('503'))
        self.assertIsNone(self.vault.read('content_factory_catalog_openai'))

    def test_key_is_encrypted_not_rendered_and_catalog_reaches_ui(self):
        path = '/settings/content-factory/catalog/key'
        key = 'synthetic-provider-key'
        result = self.request(path, 'POST', {'csrf':self.csrf, 'provider':'openai', 'api_key':key})
        self.assertTrue(result['status'].startswith('303'), result['status'])
        self.assertNotIn(key, result['body'])
        self.assertEqual(self.vault.read('content_factory_catalog_openai'), {'api_key':key})
        with open(self.vault.path, 'rb') as f: self.assertNotIn(key.encode(), f.read())
        with patch('content_factory.snapshot', return_value={'status':'unavailable'}):
            page = self.request('/settings', headers={'QUERY_STRING':'tab=content-factory'})
        self.assertTrue(page['status'].startswith('200'), page['status'])
        self.assertIn('gpt-synthetic-model', page['body'])
        self.assertIn('совместимость с конвейером', page['body'])
        self.assertNotIn(key, page['body'])

    def test_opt_in_requires_owner_and_other_worker_loads_shared_configuration(self):
        import content_factory_model_catalog_vault as optin
        from portal import Portal
        path = '/settings/content-factory/catalog/enable'
        self.app.model_catalog = None
        self.app.catalog_setup_feature = True
        first = self.app
        second = Portal(self.login, self.vault, hashlib.sha256(b'test-read').hexdigest(),
                        lambda *_:{'items':[]}, catalog_setup_feature=True)
        state = {'configured':None}
        catalog = object()
        def enable(vault, environ, secret_reader, *, confirmed):
            self.assertIs(vault, self.vault)
            self.assertTrue(confirmed)
            state['configured'] = (catalog, 'synthetic-schedule')
            return state['configured']
        def load(vault, environ, secret_reader):
            return state['configured']
        with patch.object(optin, 'enable', side_effect=enable), patch.object(optin, 'load', side_effect=load):
            self.assertTrue(self.request(path, 'POST', {'csrf':self.csrf}, cookie=False)['status'].startswith('401'))
            self.assertTrue(self.request(path, 'POST', {'csrf':self.csrf}, origin=False)['status'].startswith('403'))
            self.assertTrue(self.request(path, 'POST', {'csrf':'bad'})['status'].startswith('403'))
            self.now += 301
            self.assertTrue(self.request(path, 'POST', {'csrf':self.csrf})['status'].startswith('403'))
            self.now -= 301
            self.assertIsNone(state['configured'])
            self.assertTrue(self.request(path, 'POST', {'csrf':self.csrf})['status'].startswith('303'))
            self.assertIs(first.model_catalog, catalog)
            self.app = second
            self.assertTrue(second.model_catalog_ready())
            self.assertIs(second.model_catalog, catalog)


class CatalogProductionWsgiTest(unittest.TestCase):
    request = test_portal.Tests.request

    def setUp(self):
        self.root = os.path.join(os.getcwd(), 'cf-production-test-' + uuid.uuid4().hex)
        os.mkdir(self.root)
        catalog_dir = os.path.join(self.root, 'catalog')
        os.mkdir(catalog_dir)
        self.env = {'DATABASE_PATH':os.path.join(self.root, 'auth.sqlite'),
                    'VAULT_PATH':os.path.join(self.root, 'vault.sqlite'),
                    'CF_MODEL_CATALOG_ENABLED':'1', 'CF_MODEL_CATALOG_STAGING_VERIFIED':'1',
                    'CF_MODEL_CATALOG_DATA_DIR':catalog_dir}
        secrets_to_write = {'SESSION_SECRET_FILE':secrets.token_hex(32).encode(),
                            'VAULT_KEY_FILE':secrets.token_bytes(32),
                            'READ_TOKEN_HASH_FILE':hashlib.sha256(b'fake-test-token').hexdigest().encode()}
        for name, value in secrets_to_write.items():
            filename = os.path.join(self.root, name)
            with open(filename, 'wb') as file: file.write(value)
            self.env[name] = filename
        with patch.dict(os.environ, self.env, clear=True): self.app = create_portal()
        self.assertIsNotNone(self.app.model_catalog, 'Production composition must enable test catalogue')
        self.login, self.vault = self.app.login, self.app.vault
        self.sid, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (self.login.digest(self.sid), int(time.time()) + 43200, self.csrf))

    def tearDown(self):
        gc.collect()
        target = os.path.realpath(self.root)
        base = os.path.realpath(os.getcwd())
        if os.path.dirname(target) != base or not os.path.basename(target).startswith('cf-production-test-'):
            raise AssertionError('Refusing unsafe cleanup')
        shutil.rmtree(target)

    def test_restart_with_saved_key_and_fake_nightly_provider(self):
        import content_factory_model_catalog as catalog_module
        import content_factory_model_schedule as schedule
        model_form = {'csrf':self.csrf, 'text_model':'gpt-previous-model',
                      'image_model':'models/gemini-previous', 'video_model':''}
        self.assertTrue(self.request('/settings/content-factory/models', 'POST', model_form)['status'].startswith('303'))
        key = 'synthetic-provider-secret'
        saved = self.request('/settings/content-factory/catalog/key', 'POST',
                             {'csrf':self.csrf, 'provider':'openai', 'api_key':key})
        self.assertTrue(saved['status'].startswith('303'))
        original = catalog_module.discover
        def fake_http(url, headers):
            self.assertEqual(url, 'https://api.openai.com/v1/models')
            self.assertEqual(headers.get('Authorization'), 'Bearer ' + key)
            return {'data':[{'id':'gpt-current-model'}]}
        with patch.object(catalog_module, 'discover', side_effect=lambda provider, api_key, get: original(provider, api_key, get=fake_http)):
            result = schedule.run_once(self.vault, self.app.model_catalog, self.app.model_catalog_schedule_path)
        self.assertEqual(result['providers']['openai'], 'refreshed')
        with patch.dict(os.environ, self.env, clear=True): self.app = create_portal()
        self.assertIsNotNone(self.app.model_catalog)
        with patch('content_factory.snapshot', return_value={'status':'unavailable'}):
            page = self.request('/settings', headers={'QUERY_STRING':'tab=content-factory'})
        self.assertTrue(page['status'].startswith('200'), page['status'])
        self.assertIn('gpt-current-model', page['body'])
        self.assertIn('gpt-previous-model', page['body'])
        self.assertIn('Последнее успешное обновление OpenAI', page['body'])
        self.assertNotIn(key, page['body'])
        with open(self.env['VAULT_PATH'], 'rb') as file: self.assertNotIn(key.encode(), file.read())


if __name__ == '__main__': unittest.main()
