"""Synthetic request and response contract tests; no marketplace traffic."""
import json
import unittest
import ozon_mcp as ozon


class OzonBridgeTests(unittest.TestCase):
    def setUp(self):
        self.creds = {'client_id': 'id-secret', 'api_key': 'api-secret'}

    def test_product_pagination_is_sent_to_fixed_read_endpoint(self):
        req = ozon.build_request('ozon_products', {'limit': 17, 'last_id': 'next-page'}, self.creds)
        self.assertEqual(req.full_url, 'https://api-seller.ozon.ru/v3/product/list')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data), {'filter': {'visibility': 'ALL'}, 'limit': 17, 'last_id': 'next-page'})
        result = ozon.project_response('ozon_products', {'limit': 17, 'last_id': 'next-page'},
            {'result': {'items': [{'product_id': 42, 'offer_id': 'SKU'}], 'last_id': 'page-three'}}, self.creds)
        self.assertEqual(result['last_id'], 'page-three')
        self.assertFalse(result['first_page_only'])
        self.assertEqual(result['items'], [{'product_id': 42, 'offer_id': 'SKU'}])

    def test_price_request_and_project_preserve_prices_commissions_and_cursor(self):
        args = {'limit': 10, 'cursor': 'page-one', 'offer_ids': ['SKU-1', 'SKU-2']}
        req = ozon.build_request('ozon_prices', args, self.creds)
        self.assertEqual(req.full_url, 'https://api-seller.ozon.ru/v5/product/info/prices')
        self.assertEqual(json.loads(req.data), {'filter': {'visibility': 'ALL', 'offer_id': ['SKU-1', 'SKU-2']}, 'limit': 10, 'cursor': 'page-one'})
        data = {'cursor': 'page-two', 'items': [{'product_id': 10, 'offer_id': 'SKU-1',
            'price': {'price': '120.00', 'currency_code': 'RUB', 'private': 'discard'},
            'commissions': {'sales_percent_fbo': 17.5, 'private': 'discard'}, 'private': 'discard'}]}
        result = ozon.project_response('ozon_prices', args, data, self.creds)
        self.assertEqual(result['cursor'], 'page-two')
        self.assertEqual(result['items'][0]['price'], {'price': '120.00', 'currency_code': 'RUB'})
        self.assertEqual(result['items'][0]['commissions'], {'sales_percent_fbo': 17.5})
        self.assertNotIn('private', json.dumps(result))

    def test_declared_price_object_and_additional_commissions(self):
        response = {'cursor': '', 'items': [{'product_id': 10,
            'price': {'declared_price': {'amount': '123.45', 'currency': 'RUB', 'private': 'api-secret'}},
            'commissions': {'sales_percent_rfbs': 12.5, 'sales_percent_fbp': 7, 'private': 'api-secret'}}]}
        result = ozon.project_response('ozon_prices', {'limit': 1}, response, self.creds)
        self.assertEqual(result['items'][0]['price']['declared_price'], {'amount': '123.45', 'currency': 'RUB'})
        self.assertEqual(result['items'][0]['commissions'], {'sales_percent_rfbs': 12.5, 'sales_percent_fbp': 7})
        self.assertNotIn('api-secret', json.dumps(result))
        response['items'][0]['price']['declared_price'] = {'amount': 'api-secret', 'currency': 'RUB'}
        result = ozon.project_response('ozon_prices', {'limit': 1}, response, self.creds)
        self.assertEqual(result['items'][0]['price']['declared_price']['amount'], '[REDACTED]')
        response['items'][0]['price']['declared_price'] = {'amount': {'unsafe': 1}}
        with self.assertRaises(ValueError): ozon.project_response('ozon_prices', {'limit': 1}, response, self.creds)

    def test_credentials_are_redacted_in_projected_fields_and_never_in_cursor(self):
        result = ozon.project_response('ozon_prices', {'limit': 1},
            {'cursor': 'next-page', 'items': [{'offer_id': 'api-secret',
             'price': {'price': 'id-secret'}}]}, self.creds)
        encoded = json.dumps(result)
        self.assertNotIn('api-secret', encoded)
        self.assertNotIn('id-secret', encoded)
        self.assertEqual(result['cursor'], 'next-page')
        self.assertEqual(result['items'][0]['offer_id'], '[REDACTED]')
        with self.assertRaisesRegex(ValueError, 'Unsafe provider cursor'):
            ozon.project_response('ozon_prices', {'limit': 1},
                {'cursor': 'prefix-api-secret-suffix', 'items': []}, self.creds)

    def test_large_identifiers_do_not_lose_precision_in_javascript(self):
        result = ozon.project_response('ozon_products', {'limit': 1},
            {'result': {'items': [{'product_id': 9007199254740993}]}}, self.creds)
        self.assertEqual(result['items'][0]['product_id'], '9007199254740993')

    def test_invalid_arguments_cannot_choose_url_or_method(self):
        bad = [('ozon_products', {'url': 'https://evil'}), ('ozon_products', {'limit': True}),
               ('ozon_products', {'last_id': 'a\nInjected'}), ('ozon_prices', {'limit': 101}),
               ('ozon_prices', {'offer_ids': ['x'] * 101}), ('ozon_prices', {'offer_ids': [1]}),
               ('ozon_prices', {'cursor': 'x' * 513}), ('ozon_update_prices', {})]
        for op, args in bad:
            with self.subTest(op=op, args=args), self.assertRaises(ValueError):
                ozon.build_request(op, args, self.creds)

    def test_provider_schema_is_checked_and_never_silently_truncated(self):
        for data in ({'items': []}, {'result': {'items': [1]}},
                     {'result': {'items': [{}, {}]}}, {'result': {'items': [], 'last_id': None}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ozon.project_response('ozon_products', {'limit': 1}, data, self.creds)
        with self.assertRaises(ValueError):
            ozon.project_response('ozon_prices', {'limit': 1},
                {'items': [{'price': {'price': float('nan')}}]}, self.creds)


if __name__ == '__main__': unittest.main()
