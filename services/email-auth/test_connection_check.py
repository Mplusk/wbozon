"""Synthetic-only connection checking. No marketplace/email network calls."""
import io, json, sqlite3, unittest, urllib.error
from unittest.mock import patch
import test_portal
from portal import Vault, _provider_read as provider_read

class ConnectionCheckTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def check(self,provider='wb',**kw):
        return self.request('/settings/check','POST',{'provider':provider,'csrf':self.csrf},**kw)
    def test_requires_session_not_bearer_or_identity(self):
        self.save()
        self.assertTrue(self.check(cookie=False,auth=self.token,headers={'HTTP_OAI_AUTHENTICATED_USER_ID':'owner'})['status'].startswith('401'))
        self.assertFalse(self.calls)
    def test_expired_session_denied(self):
        self.save();self.now+=43201
        self.assertTrue(self.check()['status'].startswith('401'));self.assertFalse(self.calls)
    def test_origin_and_csrf_required(self):
        self.save()
        for kwargs in ({'origin':False},{'headers':{'HTTP_ORIGIN':'https://evil.example'}}):
            self.assertTrue(self.check(**kwargs)['status'].startswith('403'))
        self.csrf='a'*32;self.assertTrue(self.check()['status'].startswith('403'));self.assertFalse(self.calls)
    def test_method_host_guard(self):
        self.assertTrue(self.request('/settings/check','GET')['status'].startswith('405'))
        self.assertTrue(self.check(headers={'HTTP_HOST':'evil.example'})['status'].startswith('400'))
        self.assertFalse(self.calls)
    def test_strict_form_no_dynamic_parameters(self):
        for data in ({'provider':'wb','csrf':self.csrf,'limit':'10'}, {'provider':'wb','csrf':self.csrf,'url':'https://evil.example'}, {'provider':'unknown','csrf':self.csrf}, {'provider':['wb','ozon'],'csrf':self.csrf}):
            self.assertTrue(self.request('/settings/check','POST',data)['status'].startswith('400'))
        self.assertFalse(self.calls)
    def test_missing_key(self):
        self.assertTrue(self.check()['status'].startswith('409'));self.assertFalse(self.calls)
    def test_wb_success_minimal_call_no_data_echo(self):
        self.save()
        response=self.check()
        self.assertEqual(response['status'],'303 See Other');self.assertEqual(response['headers']['Location'],'/settings')
        self.assertEqual(self.calls[0][:2],('wb_prices',{'limit':1}))
        page=self.request()['body']
        self.assertIn('Проверка чтения данных выполнена успешно.',page)
        self.assertIn('UTC',page);self.assertIn('не подтверждение всех прав',page)
        self.assertNotIn('test-secret-value',page)
    def test_ozon_empty_products_success(self):
        self.request(method='POST',data={'provider':'ozon','csrf':self.csrf,'client_id':'123','api_key':'synthetic-key'})
        self.assertTrue(self.check('ozon')['status'].startswith('303'))
        self.assertEqual(self.calls[0][:2],('ozon_products',{'limit':1,'include_identity':False}))
        self.assertEqual(self.vault.check_status('ozon')[0],'ok')
    def test_check_after_five_minutes_does_not_allow_key_change(self):
        self.save();self.now+=301
        self.assertTrue(self.check()['status'].startswith('303'))
        self.assertTrue(self.save()['status'].startswith('403'))
    def test_repeat_and_new_session_share_cooldown(self):
        self.save();self.check()
        self.assertTrue(self.check()['status'].startswith('429'))
        self.assertEqual(len(self.calls),1)
        self.now+=61;self.assertTrue(self.check()['status'].startswith('303'))
    def test_locked_limiter_fails_closed_with_actionable_status(self):
        self.save()
        lock=sqlite3.connect(self.login.db, timeout=0.05)
        try:
            lock.execute('BEGIN IMMEDIATE')
            self.login.connect=lambda: sqlite3.connect(self.login.db, timeout=0.05)
            for path,data in (
                ('/settings/check',{'provider':'wb','csrf':self.csrf}),
                ('/settings/preview',{'provider':'wb','csrf':self.csrf}),
                ('/settings/wb-lookup',{'nm_id':'123','csrf':self.csrf}),
            ):
                with self.subTest(path=path):
                    response=self.request(path,'POST',data)
                    self.assertEqual(response['status'],'503 Service Unavailable')
                    self.assertIn('Проверка временно недоступна',response['body'])
                    self.assertNotIn('locked',response['body'])
                    self.assertFalse(self.calls)
        finally:
            lock.rollback(); lock.close()
    def test_locked_vault_settings_fail_closed_with_503(self):
        self.save()
        lock=sqlite3.connect(self.vault.path, timeout=0.05)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            for stage in ('present','status'):
                with self.subTest(stage=stage):
                    if stage == 'status':
                        with patch.object(self.vault,'present',return_value={'wb'}):
                            response=self.request()
                    else:
                        response=self.request()
                    self.assertEqual(response['status'],'503 Service Unavailable')
                    self.assertIn('Настройки временно недоступны',response['body'])
                    self.assertNotIn('locked',response['body'])
                    self.assertNotIn('test-secret-value',response['body'])
        finally:
            lock.rollback(); lock.close()
    def test_hourly_limit(self):
        self.save()
        for i in range(10):
            self.assertTrue(self.check()['status'].startswith('303'));self.now+=61
        self.assertTrue(self.check()['status'].startswith('429'));self.assertEqual(len(self.calls),10)
    def test_errors_sanitized(self):
        for status,expected in ((401,'denied'),(403,'denied'),(429,'rate_limited'),(503,'unavailable'),(302,'failed')):
            self.save();self.now+=61
            def gateway(*args): raise urllib.error.HTTPError('https://secret.example',status,'SECRET',{},io.BytesIO(b'SECRET'))
            self.app.gateway=gateway
            self.assertTrue(self.check()['status'].startswith('303'))
            self.assertEqual(self.vault.check_status('wb')[0],expected)
            self.assertNotIn('SECRET',self.request()['body'])
    def test_timeout_and_schema_errors(self):
        self.save()
        for exc,expected in ((TimeoutError('SECRET'),'unavailable'),(urllib.error.URLError('SECRET'),'unavailable'),(ValueError('SECRET'),'failed')):
            self.now+=61
            def gateway(*args):raise exc
            self.app.gateway=gateway;self.check()
            self.assertEqual(self.vault.check_status('wb')[0],expected);self.assertNotIn('SECRET',self.request()['body'])
        self.now+=61;self.app.gateway=lambda *args:{'error':'SECRET'};self.check()
        self.assertEqual(self.vault.check_status('wb')[0],'failed')
    def test_replacement_clears_check(self):
        self.save();self.check();self.assertIsNotNone(self.vault.check_status('wb'))
        self.save();self.assertIsNone(self.vault.check_status('wb'))
        self.assertIn('Соединение с маркетплейсом: не проверено.',self.request()['body'])
    def test_replacement_during_check_not_marked_success(self):
        self.save()
        def gateway(*args):
            self.vault.save('wb',{'api_token':'new-value'})
            return {'items':[]}
        self.app.gateway=gateway
        self.assertTrue(self.check()['status'].startswith('409'))
        self.assertIsNone(self.vault.check_status('wb'))
        self.assertEqual(self.vault.read('wb'),{'api_token':'new-value'})
    def test_old_code_replacement_invalidates_by_ciphertext(self):
        self.save();self.check()
        with sqlite3.connect(self.vault.path) as c:c.execute('UPDATE vault SET blob=?',(b'different',))
        self.assertIsNone(self.vault.check_status('wb'))
    def test_render_does_not_decrypt_or_call(self):
        self.save();self.check();self.calls.clear()
        self.vault.snapshot=lambda *args:(_ for _ in ()).throw(AssertionError('no decryption'))
        self.assertTrue(self.request()['status'].startswith('200'));self.assertFalse(self.calls)
    def test_cookie_still_cannot_use_read_api(self):
        self.save();self.check();self.calls.clear()
        self.assertTrue(self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}})['status'].startswith('401'))
        self.assertFalse(self.calls)
    def test_additive_migration_existing_vault(self):
        path=self.tmp.name+'/legacy.db'
        with sqlite3.connect(path) as c:c.execute('CREATE TABLE vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
        vault=Vault(path,b'x'*32);self.assertEqual(vault.present(),set());self.assertIsNone(vault.check_status('wb'))
    def test_corrupt_key_no_request_or_leak(self):
        self.save()
        with sqlite3.connect(self.vault.path) as c:c.execute('UPDATE vault SET blob=?',(b'broken-secret',))
        result=self.check();self.assertTrue(result['status'].startswith('503'));self.assertNotIn('broken-secret',result['body']);self.assertFalse(self.calls)

class ProviderTransportTests(unittest.TestCase):
    def test_fixed_provider_urls_and_minimal_limit(self):
        for op,creds,payload,url,method in (
            ('ozon_products',{'client_id':'123','api_key':'secret'},{'result':{'items':[]}},'https://api-seller.ozon.ru/v3/product/list','POST'),
            ('wb_prices',{'api_token':'secret'},{'data':{'listGoods':[]}},'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1&offset=0','GET')):
            with patch('portal.urllib.request.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
                result=provider_read(op,{'limit':1},creds)
                req=build.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url,url);self.assertEqual(req.method,method)
                self.assertEqual(build.return_value.open.call_args.kwargs['timeout'],15)
                self.assertEqual(result['items'],[])
                if op=='ozon_products':self.assertEqual(json.loads(req.data)['limit'],1)
