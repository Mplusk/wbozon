import os
import sqlite3
import uuid
import unittest
from contextlib import closing
from unittest import mock
import content_factory_model_catalog as c


class ModelCatalogTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(os.getcwd(), 'catalog-test-' + uuid.uuid4().hex + '.sqlite')
        self.catalog = c.Catalog(self.db, b'a' * 32)
        self.key = 'fake-key-for-tests-only'

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def test_openai_snapshot_is_bound_to_key_and_preserved_on_failure(self):
        def get(url, headers):
            self.assertEqual(url, 'https://api.openai.com/v1/models')
            self.assertEqual(headers['Authorization'], 'Bearer ' + self.key)
            return {'data': [{'id':'gpt-5'}, {'id':'gpt-5'}, {'id':'model-2'}]}
        self.assertEqual(self.catalog.refresh('openai', self.key, get=get, now=lambda:42), 2)
        self.assertEqual(self.catalog.read('openai', self.key), {'updated_at':42, 'ids':['gpt-5', 'model-2']})
        self.assertIsNone(self.catalog.read('openai', 'different-fake-key'))
        def fail(url, headers):
            raise RuntimeError('private ' + self.key)
        with self.assertRaisesRegex(RuntimeError, '^Provider catalogue unavailable$'):
            self.catalog.refresh('openai', self.key, get=fail)
        self.assertEqual(self.catalog.read('openai', self.key)['updated_at'], 42)
        with open(self.db, 'rb') as file:
            self.assertNotIn(self.key.encode(), file.read())

    def test_gemini_paginates_and_key_is_only_header(self):
        calls = []
        def get(url, headers):
            calls.append(url)
            self.assertNotIn(self.key, url)
            self.assertEqual(headers['x-goog-api-key'], self.key)
            return ({'models':[{'name':'models/a'}], 'nextPageToken':'abc+/='} if len(calls) == 1
                    else {'models':[{'name':'models/b'}]})
        self.assertEqual(c.discover('gemini', self.key, get=get), ['models/a','models/b'])
        self.assertIn('pageToken=abc%2B%2F%3D', calls[1])

    def test_pagination_loop_and_bad_data_fail_closed(self):
        with self.assertRaises(RuntimeError):
            c.discover('gemini', self.key, get=lambda u,h: {'models': [], 'nextPageToken':'same'})
        with self.assertRaises(RuntimeError):
            c.discover('openai', self.key, get=lambda u,h: {'data':[{'id':'bad\nvalue'}]})

    def test_provider_reflected_secret_is_rejected_even_in_cache(self):
        for unsafe in (self.key, 'prefix-' + self.key, 'sk-example-secret', 'models/AIza-test'):
            with self.assertRaisesRegex(RuntimeError, '^Provider catalogue unavailable$'):
                c.discover('openai', self.key, get=lambda u,h: {'data':[{'id':unsafe}]})
        self.catalog.refresh('openai', self.key, get=lambda u,h: {'data':[{'id':'safe-id'}]})
        with closing(sqlite3.connect(self.db)) as db:
            import json
            db.execute('UPDATE catalogue SET ids=?', (json.dumps([self.key]),))
            db.commit()
        with self.assertRaises(ValueError): self.catalog.read('openai', self.key)

    def test_http_refuses_redirect_and_proxy(self):
        handler = c._NoRedirect()
        self.assertIsNone(handler.redirect_request(None,None,302,None,None,'https://bad.example'))
        with mock.patch('urllib.request.build_opener') as build:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"data":[]}'
            build.return_value.open.return_value = response
            c._http('https://api.openai.com/v1/models', {'Authorization':'Bearer secret'})
            self.assertEqual(build.call_args.args[0].proxies, {})

    def test_corrupt_cache_rejected(self):
        self.catalog.refresh('openai', self.key, get=lambda u,h: {'data':[]})
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE catalogue SET ids=?', ('["bad\\nname"]',))
            db.commit()
        with self.assertRaises(ValueError):
            self.catalog.read('openai', self.key)


if __name__ == '__main__':
    unittest.main()
