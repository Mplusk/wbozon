"""Local limiter hints only. No external calls, real secrets or clock sleeps."""
import sqlite3
import unittest
import test_portal

class RequestBudgetTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def check(self,provider='wb'):
        return self.request('/settings/check','POST',{'provider':provider,'csrf':self.csrf})
    def test_unused_budget_and_no_new_queries_to_marketplace(self):
        self.save()
        for _ in range(3):
            page=self.request()['body']
            self.assertIn('осталось до 10 из 10',page)
            self.assertIn('Локальный лимит сейчас допускает попытку',page)
            self.assertIn('Это не резервирование запроса',page)
        with self.login.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertFalse(self.calls)
    def test_cooldown_and_exact_boundary_match_enforcement(self):
        self.save();self.assertTrue(self.check()['status'].startswith('303'))
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':60,'hourly_remaining':9})
        self.now+=59.1
        self.assertEqual(self.app.request_budget('wb',self.now)['retry_seconds'],1)
        self.assertTrue(self.check()['status'].startswith('429'))
        self.now+=.9
        self.assertEqual(self.app.request_budget('wb',self.now)['retry_seconds'],0)
        self.assertTrue(self.check()['status'].startswith('303'))
    def test_hour_limit_is_shared_but_provider_cooldown_is_not(self):
        self.save();self.check()
        self.assertEqual(self.app.request_budget('ozon',self.now),{'retry_seconds':0,'hourly_remaining':9})
        for _ in range(9):self.now+=61;self.check()
        self.assertEqual(self.app.request_budget('ozon',self.now),{'retry_seconds':3051,'hourly_remaining':0})
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':3051,'hourly_remaining':0})
        self.now+=3051
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':0,'hourly_remaining':1})
        self.assertTrue(self.check()['status'].startswith('303'))
    def test_short_circuit_denial_still_reserves_provider_attempt_as_before(self):
        self.save()
        with self.login.connect() as c:
            c.executemany('INSERT INTO attempts VALUES (?,?)',[('connection-check:all',self.now-3590)]*10)
        self.assertTrue(self.check()['status'].startswith('429'))
        # Existing permit order consumes provider slot even when all-slot is full.
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':60,'hourly_remaining':0})
        self.now+=10
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':50,'hourly_remaining':10})
    def test_non_connection_attempts_do_not_count_and_unknown_provider_rejected(self):
        with self.login.connect() as c:
            c.executemany('INSERT INTO attempts VALUES (?,?)',[(b,self.now) for b in ['api','recipient','ip:synthetic']]*30)
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':0,'hourly_remaining':10})
        with self.assertRaises(ValueError):self.app.request_budget("wb' OR 1=1",self.now)
    def test_metadata_failure_does_not_leak_or_hide_settings(self):
        self.save()
        def fail(*args):raise sqlite3.OperationalError('SECRET-SYNTHETIC-PATH')
        self.app.request_budget=fail
        response=self.request()
        self.assertTrue(response['status'].startswith('200'))
        self.assertIn('Не удалось оценить локальный лимит',response['body'])
        self.assertNotIn('SECRET',response['body'])
        self.assertIn('Проверить подключение',response['body'])
        self.assertFalse(self.calls)
    def test_anonymous_cannot_see_metadata(self):
        self.save()
        self.app.request_budget=lambda *args:(_ for _ in ()).throw(AssertionError('must authenticate first'))
        response=self.request(cookie=False)
        self.assertTrue(response['status'].startswith('401'))
        self.assertNotIn('Локальный лимит',response['body'])
    def test_render_does_not_reserve_or_decrypt(self):
        self.save();self.check();self.calls.clear()
        self.vault.snapshot=lambda *args:(_ for _ in ()).throw(AssertionError('must not decrypt'))
        with self.login.connect() as c:before=c.execute('SELECT bucket,at FROM attempts ORDER BY bucket,at').fetchall()
        page=self.request()['body']
        self.assertIn('не раньше чем через 60 с',page)
        self.assertNotIn('test-secret-value',page)
        with self.login.connect() as c:after=c.execute('SELECT bucket,at FROM attempts ORDER BY bucket,at').fetchall()
        self.assertEqual(before,after)
        self.assertFalse(self.calls)
    def test_more_than_limit_historical_rows_uses_tenth_newest(self):
        with self.login.connect() as c:
            c.executemany('INSERT INTO attempts VALUES (?,?)',[('connection-check:all',self.now-i*100) for i in range(12)])
        self.assertEqual(self.app.request_budget('wb',self.now),{'retry_seconds':2700,'hourly_remaining':0})
    def test_no_keys_no_budget_hint(self):
        self.assertNotIn('Локальный лимит портала на момент',self.request()['body'])
