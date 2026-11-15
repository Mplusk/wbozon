"""Only authenticated, completed bridge reads contribute to activity."""
import hashlib, secrets, sqlite3, unittest
from unittest.mock import patch
import test_portal


class BridgeActivityTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save

    def read(self, operation='wb_prices', auth=None, arguments=None):
        return self.request('/api/read','POST',{'operation':operation,'arguments':arguments or {}},auth=auth)

    def test_success_and_failures_are_shown_without_secrets(self):
        self.save()
        self.assertTrue(self.read(auth=self.token)['status'].startswith('200'))
        self.assertEqual(self.vault.bridge_activity('bridge'),('wb_prices','ok',self.now))
        page=self.request()['body']
        self.assertIn('Последнее обращение другого подключённого клиента:',page)
        self.assertIn('цены Wildberries, данные получены',page)
        self.assertNotIn('test-secret-value',page)
        self.now+=1
        self.app.gateway=lambda *args: (_ for _ in ()).throw(RuntimeError('sensitive upstream error'))
        self.assertTrue(self.read(auth=self.token)['status'].startswith('502'))
        page=self.request()['body']
        self.assertIn('цены Wildberries, не удалось получить данные',page)
        self.assertNotIn('sensitive upstream error',page)

    def test_missing_key_and_no_unauthorised_or_bad_request_activity(self):
        self.assertTrue(self.read()['status'].startswith('401'))
        self.assertTrue(self.read(operation='delete',auth=self.token)['status'].startswith('400'))
        self.assertIsNone(self.vault.bridge_activity('bridge'))
        self.assertTrue(self.read(operation='ozon_products',auth=self.token)['status'].startswith('409'))
        self.assertEqual(self.vault.bridge_activity('bridge'),('ozon_products','not_configured',self.now))
        self.assertIn('товары Ozon, ключ не добавлен',self.request()['body'])

    def test_browser_check_does_not_count_as_bridge(self):
        self.save()
        self.request('/settings/check','POST',{'provider':'wb','csrf':self.csrf})
        self.assertIsNone(self.vault.bridge_activity('bridge'))

    def test_delayed_write_does_not_replace_newer_completion(self):
        self.vault.record_bridge_activity('bridge','wb_prices','ok',100)
        self.vault.record_bridge_activity('bridge','ozon_products','failed',99)
        self.assertEqual(self.vault.bridge_activity('bridge'),('wb_prices','ok',100))

    def test_distinct_mcp_token_and_activity(self):
        self.save()
        mcp_token=secrets.token_urlsafe(32)
        self.app.mcp_token_hash=hashlib.sha256(mcp_token.encode()).hexdigest()
        self.assertTrue(self.read(auth=mcp_token)['status'].startswith('200'))
        self.assertEqual(self.vault.bridge_activity('mcp'),('wb_prices','ok',self.now))
        self.now+=1
        self.assertTrue(self.read(auth=self.token)['status'].startswith('200'))
        self.assertEqual(self.vault.bridge_activity('mcp'),('wb_prices','ok',self.now-1))
        self.assertEqual(self.vault.bridge_activity('bridge'),('wb_prices','ok',self.now))
        self.assertIn('Последнее обращение MCP:',self.request()['body'])

    def test_mcp_hash_validation_and_default_compatibility(self):
        from portal import Portal
        mcp_token=secrets.token_urlsafe(32)
        self.assertTrue(self.read(auth=mcp_token)['status'].startswith('401'))
        for invalid in ('bad',self.app.token_hash):
            with self.assertRaises(ValueError): Portal(self.login,self.vault,self.app.token_hash,mcp_read_token_hash=invalid)
        instance=Portal(self.login,self.vault,self.app.token_hash,mcp_read_token_hash=hashlib.sha256(mcp_token.encode()).hexdigest())
        self.assertIsNotNone(instance.mcp_token_hash)

    def test_rate_limited_mcp_request_records_safe_unknown_operation(self):
        mcp_token=secrets.token_urlsafe(32)
        self.app.mcp_token_hash=hashlib.sha256(mcp_token.encode()).hexdigest()
        with patch.object(self.login,'permit',return_value=False):
            self.assertTrue(self.read(operation='untrusted-operation',auth=mcp_token)['status'].startswith('429'))
        self.assertEqual(self.vault.bridge_activity('mcp'),('unknown','rate_limited',self.now))
        self.assertFalse(self.calls)
        page=self.request()['body']
        self.assertIn('операция не определена, превышен лимит запросов',page)
        self.assertNotIn('untrusted-operation',page)

    def test_activity_database_failure_does_not_break_reads_or_settings(self):
        self.save()
        with patch.object(self.vault,'record_bridge_activity',side_effect=sqlite3.OperationalError('locked')):
            self.assertTrue(self.read(auth=self.token)['status'].startswith('200'))
        with patch.object(self.vault,'bridge_activity',side_effect=sqlite3.OperationalError('locked')):
            page=self.request()
            self.assertTrue(page['status'].startswith('200'))
            self.assertIn('Не удалось загрузить сведения об обращениях',page['body'])
