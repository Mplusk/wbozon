"""Owner-only access and secret handling for the content-factory tab."""
import unittest
from unittest.mock import patch

import test_portal


class ContentFactoryPortalTests(test_portal.Tests):
    def request_tab(self, **kwargs):
        headers = kwargs.pop('headers', {})
        return self.request('/settings', headers={**headers, 'QUERY_STRING': 'tab=content-factory'}, **kwargs)

    def test_tab_requires_login_and_is_read_only(self):
        self.assertTrue(self.request_tab(cookie=False)['status'].startswith('401'))
        page = self.request_tab()
        self.assertTrue(page['status'].startswith('200'))
        self.assertIn('Контент-фабрика', page['body'])
        self.assertIn('action="/settings/content-factory/connect"', page['body'])
        self.assertTrue(self.request_tab(method='POST')['status'].startswith('405'))

    def test_bridge_key_is_encrypted_and_never_echoed(self):
        secret = 'synthetic-n8n-key-for-tests'
        fields = {'csrf': self.csrf, 'api_key': secret}
        bad_origin = self.request('/settings/content-factory/connect', 'POST', fields, origin=False)
        self.assertTrue(bad_origin['status'].startswith('403'))
        bad_csrf = self.request('/settings/content-factory/connect', 'POST', {**fields, 'csrf': 'bad'})
        self.assertTrue(bad_csrf['status'].startswith('403'))
        result = self.request('/settings/content-factory/connect', 'POST', fields)
        self.assertTrue(result['status'].startswith('303'))
        self.assertEqual(self.vault.read('n8n')['api_key'], secret)
        with open(self.vault.path, 'rb') as db:
            self.assertNotIn(secret.encode(), db.read())
        with patch('content_factory.snapshot', return_value={'status': 'unavailable'}):
            self.assertNotIn(secret, self.request_tab()['body'])

    def test_saving_key_requires_recent_login_and_valid_input(self):
        self.now += 301
        result = self.request('/settings/content-factory/connect', 'POST', {'csrf': self.csrf, 'api_key': 'x' * 30})
        self.assertTrue(result['status'].startswith('403'))
        self.assertNotIn('n8n', self.vault.present())


if __name__ == '__main__':
    unittest.main()
