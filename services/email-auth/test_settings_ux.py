"""Offline UX assertions, no live credentials or network."""
import unittest
from html.parser import HTMLParser
import test_portal


class FormFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.labels = set()
        self.ids = set()
        self.open_form = False
        self.nested_forms = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag == 'label' and attrs.get('for'):
            self.labels.add(attrs['for'])
        if tag in ('input', 'select') and attrs.get('id'):
            self.fields[attrs['id']] = attrs
        if tag == 'form':
            self.nested_forms |= self.open_form
            self.open_form = True

    def handle_endtag(self, tag):
        if tag == 'form':
            self.open_form = False


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
        self.assertEqual(page.count('type="password"'),8)
        self.assertIn('for="yandex-backup-token"',page)
        self.assertIn('for="avito-secret"',page)
        self.assertIn('for="category-token"', page)
        for field in ('client_id','client_secret'):
            self.assertIn('for="performance-'+field+'"',page)

    def test_backup_inputs_are_distinct_and_accessible(self):
        page = self.request()['body']
        parsed = FormFields()
        parsed.feed(page)
        for field in ('yandex-backup-link', 'yandex-backup-folder', 'yandex-backup-token'):
            self.assertIn(field, parsed.labels)
            self.assertIn(field, parsed.fields)
            self.assertIn(parsed.fields[field]['aria-describedby'], parsed.ids)
        self.assertEqual(parsed.fields['yandex-backup-link']['type'], 'url')
        self.assertEqual(parsed.fields['yandex-backup-folder']['type'], 'text')
        self.assertEqual(parsed.fields['yandex-backup-token']['type'], 'password')
        self.assertIn('required', parsed.fields['yandex-backup-token'])
        self.assertFalse(parsed.nested_forms)

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
