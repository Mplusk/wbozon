"""Synthetic WB currency preservation: no real credentials or API requests."""
import json
import unittest
from unittest.mock import patch
from portal import provider_read, render_preview
import test_portal

class CurrencyTests(unittest.TestCase):
    def test_transport_preserves_currency_from_fixed_wb_response(self):
        payload={'data':{'listGoods':[{'nmID':7,'currencyIsoCode4217':'KZT','sizes':[{'price':123.5}]}]}}
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
            result=provider_read('wb_prices',{'limit':20},{'api_token':'synthetic-secret'})
            self.assertEqual(result['items'][0]['currencyIsoCode4217'],'KZT')
            self.assertEqual(result['items'][0]['sizes'][0]['price'],123.5)
            self.assertEqual(build.return_value.open.call_count,1)
            self.assertEqual(build.return_value.open.call_args.args[0].full_url,'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=20&offset=0')

    def test_each_product_has_own_code_without_conversion(self):
        page=render_preview('wb',{'items':[
            {'currencyIsoCode4217':'RUB','sizes':[{'price':0}]},
            {'currencyIsoCode4217':'KZT','sizes':[{'price':123.5}]},
            {'currencyIsoCode4217':'BYN','sizes':[{'price':8}]}]},0)
        self.assertEqual(page.count('<dt>Код валюты из API</dt>'),3)
        for value in ('RUB','KZT','BYN','0','123.5','8'):
            self.assertIn('<dd>'+value+'</dd>',page)
        self.assertIn('без пересчёта',page)

    def test_missing_invalid_currency_never_defaults_to_rub(self):
        for code in (None,True,643,{},[], 'rub','RUB ', 'RUB<script>', 'R\u202eUB', '', 'RUBR'):
            with self.subTest(code=code):
                page=render_preview('wb',{'items':[{'currencyIsoCode4217':code,'sizes':[{'price':99}]}]},0)
                self.assertIn('<dt>Код валюты из API</dt><dd>не указано</dd>',page)
                self.assertNotIn('<dd>RUB</dd>',page)
                self.assertNotIn('<script>',page)
                self.assertIn('<dd>99</dd>',page)
        page=render_preview('wb',{'items':[{'sizes':[]}]},0)
        self.assertIn('<dt>Код валюты из API</dt><dd>не указано</dd>',page)

    def test_code_remains_redacted_if_it_matches_secret(self):
        page=render_preview('wb',{'items':[{'currencyIsoCode4217':'RUB','sizes':[]}]},0,('RUB',))
        self.assertNotIn('<dd>RUB</dd>',page)
        self.assertIn('[REDACTED]',page)
        payload={'data':{'listGoods':[{'currencyIsoCode4217':'RUB','sizes':[]}]}}
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
            result=provider_read('wb_prices',{'limit':1},{'api_token':'RUB'})
            self.assertNotEqual(result['items'][0]['currencyIsoCode4217'],'RUB')

    def test_ozon_does_not_gain_invented_currency(self):
        page=render_preview('ozon',{'items':[{'product_id':7,'currencyIsoCode4217':'RUB'}]},0)
        self.assertNotIn('Код валюты',page)
        self.assertNotIn('RUB',page)

class CurrencyIntegrationTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def test_preview_shows_code_only_after_authenticated_request(self):
        self.save()
        self.app.gateway=lambda *args: {'items':[{'currencyIsoCode4217':'KZT','sizes':[{'price':123.5}]}]}
        form={'provider':'wb','csrf':self.csrf}
        denied=self.request('/settings/preview','POST',form,cookie=False)
        self.assertTrue(denied['status'].startswith('401'))
        self.assertNotIn('KZT',denied['body'])
        ok=self.request('/settings/preview','POST',form)
        self.assertTrue(ok['status'].startswith('200'))
        self.assertIn('<dd>KZT</dd>',ok['body'])
        self.assertEqual(ok['headers']['Cache-Control'],'no-store')
        self.assertIsNone(self.vault.check_status('wb'))
