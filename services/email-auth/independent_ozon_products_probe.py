"""Independent synthetic review of the Ozon product-page projection.

No credentials, seller services, external network, or production data are used.
Run from services/email-auth with ``python -m unittest independent_ozon_products_probe``.
"""
import io
import json
import unittest
from unittest.mock import patch

import external_mcp
import ozon_mcp
import portal


CREDS = {'client_id': '123', 'api_key': 'synthetic-test-key-do-not-use'}


def project(items, cursor='', args=None):
    return ozon_mcp.project_response(
        'ozon_products', args or {'limit': 20},
        {'result': {'items': items, 'last_id': cursor}}, CREDS)


class IndependentProductProjection(unittest.TestCase):
    def test_fixed_read_endpoint_and_rejected_unknown_arguments(self):
        request = ozon_mcp.build_request('ozon_products', {'limit': 7, 'last_id': 'next'}, CREDS)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, 'https://api-seller.ozon.ru/v3/product/list')
        self.assertEqual(json.loads(request.data),
                         {'filter': {'visibility': 'ALL'}, 'limit': 7, 'last_id': 'next'})
        for args in ({'limit': 0}, {'limit': 101}, {'limit': True},
                     {'last_id': 'x\ny'}, {'url': 'https://example.invalid'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ozon_mcp.build_request('ozon_products', args, CREDS)

    def test_opaque_offer_id_preserved(self):
        for offer in ('00030', ' 30 ', 'Ёлка', '[REDACTED]', 'X' * 256):
            with self.subTest(offer=offer):
                result = project([{'product_id': 1, 'offer_id': offer, 'sku': 1}])
                self.assertEqual(result['items'][0]['offer_id'], offer)

    def test_identifiers_survive_json_consumers(self):
        for value in (2**53 - 1, 2**53, 2**53 + 1, 2**63 - 1):
            with self.subTest(value=value):
                result = project([{'product_id': value, 'offer_id': '0', 'sku': value}])
                item = json.loads(json.dumps(result))['items'][0]
                expected = str(value) if value > 2**53 - 1 else value
                self.assertEqual(item['product_id'], expected)
                self.assertEqual(item['sku'], expected)

    def test_inexact_or_unsafe_identifiers_are_not_repaired(self):
        for value in (True, 1.0, -1, {}, [1] * 1001, 'X' * 257,
                      'synthetic-test-key-do-not-use'):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                project([{'product_id': 1, 'offer_id': 'A', 'sku': value}])

    def test_missing_identifiers_are_distinct_from_presence(self):
        for value in (None, '', 0, '0'):
            with self.subTest(value=value):
                result = project([{'product_id': value, 'offer_id': 'A', 'sku': value}])
                self.assertEqual(result['identity']['incomplete_rows'],
                                 [{'row_index': 0, 'fields': ['product_id', 'sku']}])

    def test_conflicts_are_visible_and_page_scoped(self):
        items = [{'product_id': 1, 'offer_id': '0001', 'sku': 5},
                 {'product_id': 2, 'offer_id': '0001', 'sku': 5},
                 {'product_id': 1, 'offer_id': '1', 'sku': 6}]
        identity = project(items)['identity']
        kinds = {entry['kind'] for entry in identity['conflicts']}
        self.assertEqual(kinds, {'product_with_multiple_offers',
                                 'offer_with_multiple_products', 'sku_with_multiple_products'})
        self.assertEqual(identity['scope'], 'current_page_only')
        self.assertEqual(identity['cross_page_conflicts'], 'not_checked')

    def test_identical_rows_are_marked(self):
        row = {'product_id': 1, 'offer_id': 'A', 'sku': 9}
        identity = project([row, dict(row)])['identity']
        self.assertEqual(identity['repeated_identity_rows'], [[0, 1]])

    def test_unusual_flags_are_visible_without_changing_value(self):
        row = {'product_id': 1, 'offer_id': 'A', 'sku': 9, 'archived': 'false'}
        result = project([row])
        self.assertEqual(result['items'][0]['archived'], 'false')
        self.assertEqual(result['identity']['unverified_flag_formats'],
                         [{'row_index': 0, 'fields': ['archived']}])

    def test_unknown_identifier_format_is_visible(self):
        result = project([{'product_id': 'x', 'offer_id': 'A', 'sku': '-7.5'}])
        self.assertEqual(result['identity']['unverified_identifier_formats'],
                         [{'row_index': 0, 'fields': ['product_id', 'sku']}])

    def test_zero_placeholders_and_alternate_numeric_forms_are_not_asserted_equal(self):
        rows = [{'product_id': '000', 'offer_id': 'A', 'sku': '00'},
                {'product_id': '01', 'offer_id': 'B', 'sku': '00123'},
                {'product_id': 2, 'offer_id': 'C', 'sku': 123}]
        result = project(rows)
        identity = result['identity']
        self.assertEqual(result['items'], rows)
        self.assertEqual(identity['incomplete_rows'],
                         [{'row_index': 0, 'fields': ['product_id', 'sku']}])
        self.assertEqual(identity['unverified_identifier_formats'],
                         [{'row_index': 1, 'fields': ['product_id', 'sku']}])
        self.assertEqual(identity['alternative_numeric_forms'], 'not_checked')

    def test_absent_empty_and_repeated_cursors_differ(self):
        missing = ozon_mcp.project_response('ozon_products', {'limit': 1},
                                            {'result': {'items': []}}, CREDS)
        self.assertIsNone(missing['last_id'])
        self.assertEqual(missing['pagination_state'], 'cursor_missing')
        self.assertEqual(project([])['pagination_state'], 'empty_cursor')
        repeated = project([], 'next', {'limit': 1, 'last_id': 'next'})
        self.assertEqual(repeated['pagination_state'], 'cursor_repeated')
        self.assertEqual(repeated['catalog_completeness'], 'not_verified')

    def test_client_identifier_in_cursor_is_unchanged(self):
        result = project([{'product_id': 1, 'offer_id': 'A123', 'sku': 1}], 'next123')
        self.assertEqual(result['last_id'], 'next123')
        self.assertEqual(result['items'][0]['offer_id'], 'A123')

    def test_name_and_related_sku_are_not_claimed_complete(self):
        result = project([{'product_id': 1, 'offer_id': 'A', 'sku': 5,
                           'name': 'untrusted', 'sources': [{'sku': 6}]}])
        self.assertNotIn('name', result['items'][0])
        self.assertFalse(result['identity']['name_in_projection'])
        self.assertEqual(result['identity']['sku_coverage'], 'not_verified')

    def test_missing_items_and_oversized_page_do_not_become_empty_success(self):
        for data in ({'result': {}}, {'result': {'items': None}},
                     {'result': {'items': [{}, {}]}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ozon_mcp.project_response('ozon_products', {'limit': 1}, data, CREDS)

    def test_upstream_duplicate_keys_fail(self):
        for raw in (b'{"result":{"items":[],"items":[]}}',
                    b'{"result":{"items":[{"offer_id":"A","offer_id":"B"}]}}'):
            with self.subTest(raw=raw), patch('portal.urllib.request.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaisesRegex(ValueError, 'Duplicate provider JSON key'):
                    portal._provider_read('ozon_products', {'limit': 1}, CREDS)

    def test_upstream_size_limit_is_not_silent_truncation(self):
        with patch('portal.urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'x' * 512001
            with self.assertRaisesRegex(ValueError, 'Response too large'):
                portal._provider_read('ozon_products', {'limit': 1}, CREDS)

    def test_mcp_returns_projected_data_and_read_only_descriptor(self):
        def call(method, params):
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            environ = {'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': 'application/json',
                       'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body),
                       'HTTP_AUTHORIZATION': 'Bearer mcp_' + 'A' * 43}
            def execute(_, name, args):
                return project([{'product_id': 1, 'offer_id': '0001', 'sku': 2**53 + 1}],
                               args=args)
            response = external_mcp.handle(environ, lambda *_: None,
                                           lambda _: 'fixture', execute, 'https://example.invalid')
            return json.loads(b''.join(response))['result']
        listed = call('tools/list', {})['tools']
        tool = next(item for item in listed if item['name'] == 'ozon_products')
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertFalse(tool['inputSchema']['additionalProperties'])
        result = call('tools/call', {'name': 'ozon_products', 'arguments': {'limit': 1}})
        self.assertFalse(result.get('isError', False))
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual(payload['items'][0]['sku'], str(2**53 + 1))
        self.assertNotIn(CREDS['api_key'], json.dumps(result))


if __name__ == '__main__':
    unittest.main()
