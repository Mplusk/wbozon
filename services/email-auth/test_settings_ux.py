"""Offline UX assertions, no live credentials or network."""
import unittest
import test_portal

class SettingsUXTests(unittest.TestCase):
    setUp = test_portal.Tests.setUp
    tearDown = test_portal.Tests.tearDown
    request = test_portal.Tests.request
    save = test_portal.Tests.save
    def test_empty_state_is_not_connection_success(self):
        page=self.request()['body']
        self.assertEqual(page.count('Ключ: не добавлен.'),2)
        self.assertEqual(page.count('Соединение с маркетплейсом: не проверено.'),2)
        self.assertIn('Обращений MCP пока не зафиксировано.',page)
        self.assertIn('Это не проверка текущего подключения MCP.',page)
        self.assertNotIn('настроено',page)
        self.assertFalse(self.calls)

    def test_saved_state_is_not_verified(self):
        self.save()
        page=self.request()['body']
        self.assertEqual(page.count('Ключ: сохранён зашифрованно.'),1)
        self.assertIn('Срок действия и права ключа пока не подтверждены.',page)
        self.assertIn('Заменить сохранённый ключ',page)
        self.assertNotIn('test-secret-value',page)
        self.assertFalse(self.calls)

    def test_labels_and_mobile_semantics(self):
        page=self.request()['body']
        for text in ('lang="ru"','name="viewport"','Client ID','API-ключ Ozon','API-токен Wildberries','for="ozon-client_id"','id="ozon-client_id"'):
            self.assertIn(text,page)
        self.assertEqual(page.count('type="password"'),5)
        for field in ('client_id','client_secret'):
            self.assertIn('for="performance-'+field+'"',page)

    def test_recent_and_stale_help(self):
        self.assertIn('первых 5 минут',self.request()['body'])
        self.now+=301
        self.assertIn('Время для изменения ключей маркетплейсов истекло.',self.request()['body'])
        self.assertTrue(self.save()['status'].startswith('403'))

    def test_ux_does_not_decrypt_vault_or_call_gateway(self):
        self.save()
        self.vault.read=lambda provider: (_ for _ in ()).throw(AssertionError('must not decrypt'))
        page=self.request()
        self.assertTrue(page['status'].startswith('200'))
        self.assertEqual(page['headers']['Cache-Control'],'no-store')
        self.assertIn("default-src 'none'",page['headers']['Content-Security-Policy'])
        self.assertFalse(self.calls)
