"""Synthetic request and response contract tests; no marketplace traffic."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
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
            {'cursor': 'next-page', 'items': [{'offer_id': 'safe-offer',
             'price': {'price': 'id-secret'}}]}, self.creds)
        encoded = json.dumps(result)
        self.assertNotIn('api-secret', encoded)
        self.assertNotIn('id-secret', encoded)
        self.assertEqual(result['cursor'], 'next-page')
        self.assertEqual(result['items'][0]['offer_id'], 'safe-offer')
        self.assertEqual(result['items'][0]['price']['price'], '[REDACTED]')
        with self.assertRaisesRegex(ValueError, '^Unsafe provider identifier$'):
            ozon.project_response('ozon_prices', {'limit': 1},
                {'cursor': '', 'items': [{'offer_id': 'api-secret'}]}, self.creds)
        with self.assertRaisesRegex(ValueError, 'Unsafe provider cursor'):
            ozon.project_response('ozon_prices', {'limit': 1},
                {'cursor': 'prefix-api-secret-suffix', 'items': []}, self.creds)

    def test_large_identifiers_do_not_lose_precision_in_javascript(self):
        result = ozon.project_response('ozon_products', {'limit': 1},
            {'result': {'items': [{'product_id': 9007199254740993}]}}, self.creds)
        self.assertEqual(result['items'][0]['product_id'], '9007199254740993')

    def test_product_sku_candidates_remain_exact_and_conflicts_are_reported(self):
        rows = [
            {'product_id': 1, 'offer_id': '0030', 'sku': [5100000123, 9007199254740993, '00123', None], 'name': 'ignored'},
            {'product_id': 2, 'offer_id': '0030', 'sku': [5100000123]},
            {'product_id': 1, 'offer_id': '30', 'sku': '00123'},
            {'product_id': '0', 'offer_id': '0030', 'sku': '0'},
        ]
        result = ozon.project_response('ozon_products', {'limit': 4},
            {'result': {'items': rows, 'last_id': 'next'}}, self.creds)
        self.assertEqual(result['items'][0]['sku'], [5100000123, '9007199254740993', '00123', None])
        self.assertEqual([item['offer_id'] for item in result['items']], ['0030', '0030', '30', '0030'])
        self.assertNotIn('name', result['items'][0])
        self.assertEqual(result['pagination_state'], 'cursor_present')
        self.assertEqual(result['catalog_completeness'], 'not_verified')
        identity = result['identity']
        self.assertEqual(identity['incomplete_rows'], [
            {'row_index': 0, 'fields': ['sku']}, {'row_index': 3, 'fields': ['product_id', 'sku']}])
        self.assertEqual(identity['conflicts'], [
            {'kind': 'product_with_multiple_offers', 'row_indices': [0, 2]},
            {'kind': 'offer_with_multiple_products', 'row_indices': [0, 1]},
            {'kind': 'sku_with_multiple_products', 'row_indices': [0, 1]}])
        self.assertEqual(identity['sku_coverage'], 'not_verified')
        self.assertEqual(identity['cross_page_conflicts'], 'not_checked')
        self.assertFalse(identity['name_in_projection'])

    def test_product_ambiguous_formats_repeated_rows_and_cursors_are_visible(self):
        rows = [{'product_id': 'nonnumeric', 'offer_id': '0030', 'sku': '-7.5'}] * 2
        result = ozon.project_response('ozon_products', {'limit': 2, 'last_id': 'same'},
            {'result': {'items': rows, 'last_id': 'same'}}, self.creds)
        self.assertEqual(result['items'][0], rows[0])
        self.assertEqual(result['pagination_state'], 'cursor_repeated')
        self.assertEqual(result['identity']['unverified_identifier_formats'], [
            {'row_index': 0, 'fields': ['product_id', 'sku']},
            {'row_index': 1, 'fields': ['product_id', 'sku']}])
        self.assertEqual(result['identity']['repeated_identity_rows'], [[0, 1]])
        for envelope, expected in [({'items': []}, 'cursor_missing'),
                                   ({'items': [], 'last_id': ''}, 'empty_cursor')]:
            page = ozon.project_response('ozon_products', {'limit': 1}, {'result': envelope}, self.creds)
            self.assertEqual(page['pagination_state'], expected)
            self.assertEqual(page['catalog_completeness'], 'not_verified')
            if expected == 'cursor_missing': self.assertIsNone(page['last_id'])

    def test_product_unsafe_or_inexact_sku_never_gets_silently_truncated(self):
        for value in (True, 1.5, {'sku': '1'}, [1, {'sku': '2'}], [1] * 1001, 'prefix-api-secret'):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                ozon.project_response('ozon_products', {'limit': 1},
                    {'result': {'items': [{'product_id': 1, 'offer_id': 'x', 'sku': value}]}}, self.creds)

    def test_client_id_substrings_do_not_corrupt_identifiers_or_opaque_cursor(self):
        creds = {'client_id': '123', 'api_key': 'synthetic-api-key'}
        result = ozon.project_response('ozon_products', {'limit': 1},
            {'result': {'items': [{'product_id': 123, 'offer_id': 'ABC123', 'sku': '00123'}],
                        'last_id': 'p123end'}}, creds)
        self.assertEqual(result['items'][0]['offer_id'], 'ABC123')
        self.assertEqual(result['items'][0]['sku'], '00123')
        self.assertEqual(result['last_id'], 'p123end')

    def test_product_flag_types_are_not_silently_interpreted(self):
        row = {'product_id': 1, 'offer_id': 'A', 'sku': 11,
               'archived': 'false', 'is_fbo_visible': None, 'is_fbs_visible': False}
        result = ozon.project_response('ozon_products', {'limit': 1},
            {'result': {'items': [row], 'last_id': ''}}, self.creds)
        self.assertEqual(result['items'][0], row)
        self.assertEqual(result['identity']['unverified_flag_formats'],
                         [{'row_index': 0, 'fields': ['archived']}])

    def test_zero_placeholders_and_leading_zero_identifier_formats_are_uncertain(self):
        rows = [{'product_id': '000', 'offer_id': '000', 'sku': '00'},
                {'product_id': '001', 'offer_id': '001', 'sku': '00123'},
                {'product_id': 2, 'offer_id': '1', 'sku': 123}]
        result = ozon.project_response('ozon_products', {'limit': 3},
            {'result': {'items': rows}}, self.creds)
        self.assertEqual(result['items'], rows)
        self.assertEqual(result['identity']['incomplete_rows'], [{'row_index': 0, 'fields': ['product_id', 'sku']}])
        self.assertEqual(result['identity']['unverified_identifier_formats'],
                         [{'row_index': 1, 'fields': ['product_id', 'sku']}])
        self.assertEqual(result['identity']['alternative_numeric_forms'], 'not_checked')
        self.assertEqual(result['identity']['conflicts'], [])

    def test_concurrent_product_projections_do_not_mix_identifiers(self):
        def project(index):
            creds = {'client_id': str(index), 'api_key': 'key-for-' + str(index)}
            offer = f'00{index}'
            page = {'result': {'items': [{'product_id': index, 'offer_id': offer, 'sku': index + 100}],
                               'last_id': f'page-{index}'}}
            result = ozon.project_response('ozon_products', {'limit': 1}, page, creds)
            return result['items'][0]['offer_id'], result['last_id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            actual = list(pool.map(project, range(1, 101)))
        self.assertEqual(actual, [(f'00{i}', f'page-{i}') for i in range(1, 101)])

    def test_seller_identifier_is_exact_or_projection_is_rejected(self):
        for op in ('ozon_products', 'ozon_prices'):
            for value in ('300030', '00300030', '[REDACTED]', 'X' * 256, None):
                data = {'items': [{'offer_id': value}]}
                response = {'result': data} if op == 'ozon_products' else data
                self.assertEqual(ozon.project_response(op, {'limit': 1}, response, self.creds)['items'][0]['offer_id'], value)
            for value in ('X' * 256 + 'A', 'api-secret', 300030, True):
                data = {'items': [{'offer_id': value}]}
                response = {'result': data} if op == 'ozon_products' else data
                with self.assertRaisesRegex(ValueError, '^Unsafe provider identifier$'):
                    ozon.project_response(op, {'limit': 1}, response, self.creds)

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

    def test_order_analytics_daily_aggregation_is_bounded_and_exact(self):
        args = {'date_from': '2026-11-09', 'date_to': '2026-11-15'}
        request = ozon.build_request('ozon_orders_analytics', args, self.creds)
        self.assertEqual(request.full_url, 'https://api-seller.ozon.ru/v1/analytics/data')
        self.assertEqual(json.loads(request.data), {**args, 'metrics': ['revenue', 'ordered_units'],
            'dimension': ['day'], 'limit': 100, 'offset': 0})
        data = {'timestamp': '2026-11-16 00:00:00', 'result': {'data': [
            {'dimensions': [{'id': '2026-11-09', 'name': 'untrusted'}], 'metrics': [0.1, 2]},
            {'dimensions': [{'id': '2026-11-15'}], 'metrics': [0.2, 1]}]}}
        result = ozon.project_response('ozon_orders_analytics', args, data, self.creds)
        self.assertEqual(result['ordered_amount'], '0.3')
        self.assertEqual(result['ordered_units'], '3')
        self.assertFalse(result['complete'])
        self.assertEqual(result['period_completeness'], 'not_verified')
        self.assertEqual(result['missing_dates'], ['2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-14'])
        self.assertIsNone(result['currency'])
        self.assertIn('not delivered sales, payout or profit',result['meaning'])
        self.assertIn('verify it before combining sums',result['meaning'])
        self.assertEqual([r['date'] for r in result['days']], ['2026-11-09', '2026-11-15'])
        self.assertNotIn('untrusted', json.dumps(result['days']))

    def test_empty_order_analytics_is_not_a_confirmed_zero(self):
        args = {'date_from': '2026-11-09', 'date_to': '2026-11-10'}
        result = ozon.project_response('ozon_orders_analytics', args, {'result': {'data': []}}, self.creds)
        self.assertIsNone(result['ordered_amount'])
        self.assertIsNone(result['ordered_units'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_dates'], ['2026-11-09','2026-11-10'])

    def test_order_analytics_rejects_invalid_ranges_and_response(self):
        for args in ({'date_from': '2026-02-30', 'date_to': '2026-03-01'},
                     {'date_from': '2026-02-02', 'date_to': '2026-02-01'},
                     {'date_from': '2026-01-01', 'date_to': '2026-04-01'},
                     {'date_from': '2026-01-01', 'date_to': '2026-01-02', 'offset': 1}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ozon.validate_args('ozon_orders_analytics', args)
        args = {'date_from': '2026-11-09', 'date_to': '2026-11-15'}
        good = {'dimensions': [{'id': '2026-11-09'}], 'metrics': [10.5, 2]}
        for rows in ([good, good], [{'dimensions': [{'id': '2026-11-16'}], 'metrics': [2, 1]}],
                     [{'dimensions': [{'id': '2026-11-09'}], 'metrics': [float('nan'), 1]}],
                     [{'dimensions': [{'id': '2026-11-09'}], 'metrics': [1, 1.5]}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                ozon.project_response('ozon_orders_analytics', args, {'result': {'data': rows}}, self.creds)


if __name__ == '__main__': unittest.main()
