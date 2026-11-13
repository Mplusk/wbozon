"""Synthetic exact-article WB lookup: no network, secrets or real account."""
import concurrent.futures
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
import test_portal
from portal import provider_read

class LookupTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def lookup(self,article='123',**kwargs):
        return self.request('/settings/wb-lookup','POST',{'csrf':self.csrf,'nm_id':article},**kwargs)
    def test_valid_exact_id_and_empty_result_truthful(self):
        self.save();response=self.lookup()
        self.assertTrue(response['status'].startswith('200'))
        self.assertEqual(self.calls[0][:2],('wb_prices',{'limit':1,'nm_id':123}))
        self.assertIn('Запрошен артикул Wildberries: 123',response['body'])
        self.assertIn('не доказывает удаление товара',response['body'])
        self.assertNotIn('до 20 товаров',response['body'])
        self.assertEqual(response['headers']['Cache-Control'],'no-store')
        self.assertEqual(response['headers']['Referrer-Policy'],'strict-origin')
        self.assertIsNone(self.vault.check_status('wb'))
    def test_exact_result_escapes_and_redacts(self):
        self.save()
        self.app.gateway=lambda *args:{'items':[{'nmID':123,'vendorCode':'<script>test-secret-value</script>', 'currencyIsoCode4217':'KZT','sizes':[{'price':0}]}]}
        response=self.lookup();page=response['body']
        self.assertTrue(response['status'].startswith('200'))
        self.assertNotIn('<script>',page);self.assertNotIn('test-secret-value',page)
        self.assertIn('&lt;script&gt;',page);self.assertIn('<dd>KZT</dd>',page);self.assertIn('<dd>0</dd>',page)
    def test_reject_mismatch_multirow_and_wrong_id_type(self):
        self.save()
        for rows in ([{'nmID':124,'vendorCode':'wrong-private'}],[{'nmID':'123','vendorCode':'wrong-private'}],[{'nmID':True}], [{},{}], [{'nmID':123},{'nmID':123}]):
            self.app.gateway=lambda *args:{'items':rows}
            response=self.lookup();self.now+=61
            self.assertTrue(response['status'].startswith('502'))
            self.assertNotIn('wrong-private',response['body'])
    def test_invalid_input_rejected_before_call_or_budget(self):
        self.save()
        for article in ('0','-1','+1','01',' 1','1 ','1.0','1e3','１２３','1&limit=100','<script>','', '9223372036854775808', '9'*100):
            self.assertTrue(self.lookup(article)['status'].startswith('400'),article)
        with self.login.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.assertFalse(self.calls)
    def test_extra_provider_or_url_parameter_rejected(self):
        self.save()
        for extra in ({'provider':'ozon'},{'url':'https://evil.example'},{'limit':'100'}):
            response=self.request('/settings/wb-lookup','POST',{'nm_id':'123','csrf':self.csrf,**extra})
            self.assertTrue(response['status'].startswith('400'))
        self.assertFalse(self.calls)
    def test_auth_origin_csrf_method_and_host(self):
        self.save()
        self.assertTrue(self.lookup(cookie=False,auth=self.token)['status'].startswith('401'))
        self.assertTrue(self.lookup(origin=False)['status'].startswith('403'))
        self.assertTrue(self.lookup(headers={'HTTP_ORIGIN':'null'})['status'].startswith('403'))
        self.assertTrue(self.lookup(headers={'HTTP_HOST':'evil.example'})['status'].startswith('400'))
        self.assertTrue(self.request('/settings/wb-lookup')['status'].startswith('405'))
        self.csrf='x'*32;self.assertTrue(self.lookup()['status'].startswith('403'))
        self.assertFalse(self.calls)
    def test_missing_key_does_not_query(self):
        self.assertTrue(self.lookup()['status'].startswith('409'));self.assertFalse(self.calls)
    def test_expired_session_does_not_query(self):
        self.save();self.now+=43201
        self.assertTrue(self.lookup()['status'].startswith('401'));self.assertFalse(self.calls)
    def test_boundaries_remain_exact_integers(self):
        self.save()
        for article in ('1','9223372036854775807'):
            self.assertTrue(self.lookup(article)['status'].startswith('200'))
            self.assertEqual(self.calls[-1][1]['nm_id'],int(article));self.now+=61
    def test_lookup_shares_check_preview_limit(self):
        self.save();self.lookup()
        self.assertTrue(self.request('/settings/check','POST',{'provider':'wb','csrf':self.csrf})['status'].startswith('429'))
        self.assertTrue(self.request('/settings/preview','POST',{'provider':'wb','csrf':self.csrf})['status'].startswith('429'))
        self.assertEqual(len(self.calls),1)
    def test_parallel_lookup_only_one_call(self):
        self.save();gate=threading.Barrier(6)
        def attempt(_):gate.wait();return self.lookup()['status']
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
        self.assertEqual(results.count('200 OK'),1);self.assertEqual(results.count('429 Too Many Requests'),5)
        self.assertEqual(len(self.calls),1)
    def test_logout_or_key_replacement_midflight_hides_data(self):
        self.save()
        def changed(*args):
            self.vault.save('wb',{'api_token':'replacement'})
            return {'items':[{'nmID':123,'vendorCode':'private'}]}
        self.app.gateway=changed;response=self.lookup()
        self.assertTrue(response['status'].startswith('409'));self.assertNotIn('private',response['body'])
        self.now+=61
        def loggedout(*args):
            with self.login.connect() as c:c.execute('DELETE FROM sessions')
            return {'items':[{'nmID':123,'vendorCode':'private'}]}
        self.app.gateway=loggedout;response=self.lookup()
        self.assertTrue(response['status'].startswith('401'));self.assertNotIn('private',response['body'])
    def test_bearer_api_does_not_gain_lookup_arguments(self):
        self.save()
        response=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{'limit':1,'nm_id':123}},auth=self.token)
        self.assertTrue(response['status'].startswith('400'));self.assertFalse(self.calls)
    def test_form_only_when_wb_saved(self):
        self.assertNotIn('action="/settings/wb-lookup"',self.request()['body'])
        self.save();page=self.request()['body']
        self.assertIn('action="/settings/wb-lookup"',page)
        self.assertIn('не артикул продавца',page);self.assertIn('maxlength="19"',page)

class LookupTransportTests(unittest.TestCase):
    def test_exact_fixed_url_and_limit_one(self):
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps({'data':{'listGoods':[{'nmID':123,'sizes':[]}]}}).encode()
            result=provider_read('wb_prices',{'limit':1,'nm_id':123},{'api_token':'synthetic'})
            self.assertEqual(build.return_value.open.call_args.args[0].full_url,'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1&offset=0&filterNmID=123')
            self.assertEqual(result['items'][0]['nmID'],123)
            self.assertEqual(build.return_value.open.call_count,1)
    def test_internal_transport_rejects_unsafe_args(self):
        with patch('portal.urllib.request.build_opener') as build:
            for op,args in [('wb_prices',{'limit':20,'nm_id':123}),('ozon_products',{'limit':1,'nm_id':123})]+[('wb_prices',{'limit':1,'nm_id':v}) for v in (None,True,0,-1,1.5,'123',2**63)]:
                with self.assertRaises(ValueError):provider_read(op,args,{'api_token':'synthetic'})
            build.assert_not_called()
