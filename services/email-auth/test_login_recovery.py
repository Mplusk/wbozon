"""Recovery presentation only: existing auth decisions remain unchanged."""
import unittest
import test_app

class RecoveryTests(unittest.TestCase):
    setUp=test_app.AuthTests.setUp
    tearDown=test_app.AuthTests.tearDown
    call=test_app.AuthTests.call
    request=test_app.AuthTests.request
    landing=test_app.AuthTests.landing
    def assert_recovery(self,response,status,forbidden=()):
        self.assertTrue(response['status'].startswith(str(status)))
        self.assertIn('href="/"',response['body'])
        self.assertIn('Запросить новую ссылку',response['body'])
        self.assertIn('lang="ru"',response['body'])
        self.assertIn('письмо не отправляется автоматически',response['body'])
        self.assertNotIn('name="token"',response['body'])
        self.assertNotIn('name="csrf"',response['body'])
        self.assertNotIn('maksim@kuchermg.ru',response['body'])
        for value in forbidden:self.assertNotIn(value,response['body'])
        headers=dict(response['headers'])
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(headers['Referrer-Policy'],'no-referrer')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertNotIn('Set-Cookie',headers)
    def test_expired_has_safe_recovery_without_mail_or_session(self):
        data,cookie=self.landing();self.now+=901
        self.assert_recovery(self.call('/auth/confirm','POST',data,cookie),400,(data['token'],data['csrf'],cookie))
        self.assertEqual(len(self.sent),1)
        with self.app.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
    def test_used_repeated_requests_do_not_create_session(self):
        data,cookie=self.landing();self.call('/auth/confirm','POST',data,cookie)
        for _ in range(3):self.assert_recovery(self.call('/auth/confirm','POST',data,cookie),400,(data['token'],data['csrf'],cookie))
        with self.app.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],1)
        self.assertEqual(len(self.sent),1)
    def test_malformed_link_no_reflection_or_cookie(self):
        for path in ('/auth/link','/auth/link?token=%3Cscript%3E','/auth/link?token=maksim%40kuchermg.ru','/auth/link?token='+'z'*513,'/auth/link?a=1&b=2&c=3&d=4&e=5'):
            self.assert_recovery(self.call(path),400,('<script>','z'*40))
        self.assertFalse(self.sent)
    def test_missing_cookie_and_bad_nonce_do_not_consume(self):
        data,cookie=self.landing()
        self.assert_recovery(self.call('/auth/confirm','POST',data),403,(data['token'],data['csrf']))
        bad=dict(data,csrf='x'*32)
        self.assert_recovery(self.call('/auth/confirm','POST',bad,cookie),403,(data['token'],cookie))
        with self.app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tokens').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
        self.assertTrue(self.call('/auth/confirm','POST',data,cookie)['status'].startswith('200'))
    def test_get_scanner_and_recovery_home_never_send_email(self):
        link=self.request();self.call(link);self.call(link);self.call('/auth/link?token=bad');self.call('/')
        self.assertEqual(len(self.sent),1)
        with self.app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tokens').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
    def test_cross_origin_still_rejected_before_confirmation(self):
        data,cookie=self.landing();response=self.call('/auth/confirm','POST',data,cookie,origin='https://evil.example')
        self.assertTrue(response['status'].startswith('403'));self.assertNotIn(data['token'],response['body'])
        self.assertTrue(self.call('/auth/confirm','POST',data,cookie)['status'].startswith('200'))
