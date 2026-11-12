"""Synthetic WSGI guards. Browser header generation is tested separately."""
import unittest
import test_app, test_portal

class LoginOriginPolicyTests(unittest.TestCase):
    setUp=test_app.AuthTests.setUp
    tearDown=test_app.AuthTests.tearDown
    call=test_app.AuthTests.call
    request=test_app.AuthTests.request
    landing=test_app.AuthTests.landing
    def test_login_and_confirmation_use_origin_only_referrer_policy(self):
        self.assertEqual(dict(self.call()['headers'])['Referrer-Policy'],'strict-origin')
        link=self.request()
        self.assertEqual(dict(self.call(link)['headers'])['Referrer-Policy'],'strict-origin')
    def test_null_missing_and_foreign_origin_remain_forbidden(self):
        for origin in ('null','',None,'https://evil.example'):
            response=self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'},origin=origin)
            self.assertTrue(response['status'].startswith('403'))
        self.assertFalse(self.sent)
    def test_normal_origin_allows_synthetic_request_not_real_email(self):
        self.assertTrue(self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'})['status'].startswith('200'))
        self.assertEqual(len(self.sent),1)
    def test_null_origin_confirmation_cannot_consume_token(self):
        data,cookie=self.landing()
        self.assertTrue(self.call('/auth/confirm','POST',data,cookie,origin='null')['status'].startswith('403'))
        self.assertTrue(self.call('/auth/confirm','POST',data,cookie)['status'].startswith('200'))

class PortalOriginPolicyTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def test_settings_forms_use_strict_origin_and_api_keeps_no_referrer(self):
        self.assertEqual(self.request()['headers']['Referrer-Policy'],'strict-origin')
        result=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}})
        self.assertEqual(result['headers']['Referrer-Policy'],'no-referrer')
    def test_null_origin_rejected_for_key_save_check_preview_logout(self):
        self.save()
        for path,data in (
            ('/settings',{'provider':'wb','csrf':self.csrf,'api_token':'replacement'}),
            ('/settings/check',{'provider':'wb','csrf':self.csrf}),
            ('/settings/preview',{'provider':'wb','csrf':self.csrf}),
            ('/logout',{'csrf':self.csrf})):
            response=self.request(path,'POST',data,headers={'HTTP_ORIGIN':'null'})
            self.assertTrue(response['status'].startswith('403'))
        self.assertEqual(self.vault.read('wb')['api_token'],'test-secret-value')
        self.assertFalse(self.calls)
