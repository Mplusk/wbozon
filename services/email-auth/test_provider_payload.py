"""Synthetic raw provider payload validation; never performs network requests."""
import json
import unittest
from unittest.mock import patch
import test_portal
from portal import provider_read, decode_provider_payload

class ProviderPayloadTests(unittest.TestCase):
    def read(self,payload,operation='wb_prices'):
        raw=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
        creds={'api_token':'synthetic-secret'} if operation=='wb_prices' else {'client_id':'123','api_key':'synthetic-secret'}
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=raw
            return provider_read(operation,{'limit':1},creds)
    def test_wb_error_flag_cannot_become_empty_success(self):
        for flag in (True,None,0,1,'false',[],{}):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError,'Provider reported an error'):
                self.read({'error':flag,'errorText':'','data':{'listGoods':[]}})
    def test_wb_error_text_cannot_become_data_success_or_leak(self):
        for text in ('synthetic-secret',False,0,{},[]):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                self.read({'error':False,'errorText':text,'data':{'listGoods':[{'nmID':1,'sizes':[]}]}})
            self.assertNotIn('synthetic-secret',str(caught.exception))
    def test_optional_wb_envelope_fields_preserve_compatibility(self):
        for extra in ({},{'error':False},{'error':False,'errorText':''},{'error':False,'errorText':None}):
            result=self.read({'data':{'listGoods':[{'nmID':1,'currencyIsoCode4217':'RUB','sizes':[{'price':0,'discountedPrice':12.34}]}]},**extra})
            self.assertEqual(result['items'][0]['sizes'][0]['discountedPrice'],12.34)
            self.assertEqual(result['items'][0]['currencyIsoCode4217'],'RUB')
            json.dumps(result,allow_nan=False)
    def test_nonstandard_json_numbers_and_overflow_rejected(self):
        for value in (b'NaN',b'Infinity',b'-Infinity',b'1e9999',b'-1e9999'):
            for operation in ('wb_prices','ozon_products'):
                raw=(b'{"data":{"listGoods":[{"sizes":[{"price":'+value+b'}]}]}}' if operation=='wb_prices' else b'{"result":{"items":[{"product_id":'+value+b'}]}}')
                with self.subTest(value=value,operation=operation),self.assertRaises(ValueError):self.read(raw,operation)
    def test_all_response_must_be_valid_json_even_unused_metadata(self):
        with self.assertRaises(ValueError):
            self.read(b'{"data":{"listGoods":[]},"metadata":NaN}')
    def test_object_shape_and_normal_json_scalars(self):
        for raw in (b'null',b'[]',b'1',b'"text"',b'true'):
            with self.assertRaises(ValueError):decode_provider_payload(raw)
        self.assertEqual(decode_provider_payload(b'{"x":1.25,"y":1e2,"z":null}'),{'x':1.25,'y':100.0,'z':None})
    def test_ozon_valid_payload_unchanged(self):
        result=self.read({'result':{'items':[{'product_id':456,'offer_id':'article','archived':False}]}},'ozon_products')
        self.assertEqual(result['items'][0],{'product_id':456,'offer_id':'article','archived':False})

class ProviderPayloadIntegrationTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def test_error_flag_is_failed_check_not_success(self):
        self.save();self.app.gateway=provider_read
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=b'{"error":true,"errorText":"SECRET-MESSAGE","data":{"listGoods":[]}}'
            result=self.request('/settings/check','POST',{'provider':'wb','csrf':self.csrf})
            self.assertTrue(result['status'].startswith('303'))
        self.assertEqual(self.vault.check_status('wb')[0],'failed')
        self.assertNotIn('SECRET-MESSAGE',self.request()['body'])
    def test_preview_and_bearer_never_echo_error_or_nonfinite_number(self):
        self.save();self.app.gateway=provider_read
        for raw in (b'{"error":true,"errorText":"SECRET-MESSAGE","data":{"listGoods":[]}}',b'{"data":{"listGoods":[{"sizes":[{"price":Infinity}]}]}}'):
            with patch('portal.urllib.request.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value=raw
                result=self.request('/settings/preview','POST',{'provider':'wb','csrf':self.csrf})
                self.assertTrue(result['status'].startswith('502'))
                self.assertNotIn('SECRET-MESSAGE',result['body']);self.assertNotIn('Infinity',result['body'])
                result=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{'limit':1}},auth=self.token)
                self.assertTrue(result['status'].startswith('502'))
                self.assertEqual(json.loads(result['body']),{'error':'read_failed'})
            self.now+=61
