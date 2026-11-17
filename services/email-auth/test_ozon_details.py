"""Synthetic contract tests; no real credentials or marketplace calls."""
import io
import json
import unittest
from unittest.mock import patch

import ozon_details as details
import ozon_mcp
import portal
import test_external_mcp


CREDS = {'client_id': 'synthetic-client', 'api_key': 'synthetic-private-key'}


class OzonDetailsTests(unittest.TestCase):
    def test_fixed_endpoints_and_provider_bodies(self):
        examples = [
            ('ozon_product_info', {'offer_ids': ['0030']}, '/v3/product/info/list', {'offer_id': ['0030']}),
            ('ozon_product_info', {'product_ids': ['12']}, '/v3/product/info/list', {'product_id': ['12']}),
            ('ozon_product_info', {'skus': ['5215904379']}, '/v3/product/info/list', {'sku': ['5215904379']}),
            ('ozon_product_info', {'skus': [5215904379]}, '/v3/product/info/list', {'sku': ['5215904379']}),
            ('ozon_accrual_types', {}, '/v1/finance/accrual/types', {}),
            ('ozon_posting_accruals', {'posting_numbers': ['ABC-1']}, '/v1/finance/accrual/postings', {'posting_numbers': ['ABC-1']}),
        ]
        for name, args, endpoint, body in examples:
            with self.subTest(name=name, args=args):
                req = ozon_mcp.build_request(name, args, CREDS)
                self.assertEqual(req.full_url, 'https://api-seller.ozon.ru' + endpoint)
                self.assertEqual(req.get_method(), 'POST')
                self.assertEqual(json.loads(req.data), body)

    def test_invalid_requests_are_rejected_before_the_network(self):
        invalid = [
            ('ozon_product_info', {}), ('ozon_product_info', {'offer_ids': []}),
            ('ozon_product_info', {'offer_ids': ['x'], 'skus': ['1']}),
            ('ozon_product_info', {'offer_ids': ['x'] * 101}),
            ('ozon_product_info', {'offer_ids': ['x', 'x']}),
            ('ozon_product_info', {'skus': [True]}),
            ('ozon_product_info', {'skus': ['001']}),
            ('ozon_product_info', {'skus': ['9223372036854775808']}),
            ('ozon_product_info', {'skus': [1.1]}),
            ('ozon_product_info', {'url': ['https://example.com']}),
            ('ozon_accrual_types', {'extra': 1}),
            ('ozon_posting_accruals', {'posting_numbers': []}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'] * 201}),
            ('ozon_posting_accruals', {'posting_numbers': ['x\nother']}),
            ('ozon_posting_accruals', {'posting_numbers': ['x', 'x']}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'date_from': '2026-11-01'}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'date_from': '2026-11-02', 'date_to': '2026-11-01'}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'view': 'unknown'}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'skus': ['1']}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'view': 'summary', 'skus': []}),
            ('ozon_posting_accruals', {'posting_numbers': ['x'], 'view': 'summary', 'skus': ['1', '1']}),
        ]
        for name, args in invalid:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                ozon_mcp.build_request(name, args, CREDS)

    def test_multiple_skus_are_preserved_without_selecting_first(self):
        data = {'items': [{'id': 42, 'offer_id': '0030', 'name': 'Модель', 'sku': 5215904379,
            'sources': [{'source': 'FBO', 'sku': 9007199254740993},
                        {'source': 'FBS', 'sku': 5215904379},
                        {'source': 'SDS', 'sku': 8675309}],
            'private': 'not returned'}]}
        result = details.project_response('ozon_product_info', {'offer_ids': ['0030']}, data, CREDS)
        item = result['items'][0]
        self.assertEqual(item['offer_id'], '0030')
        self.assertEqual(item['name'], 'Модель')
        self.assertEqual(item['sku_candidates'], ['8675309', '5215904379', '9007199254740993'])
        self.assertEqual([s['source'] for s in item['sources']], ['FBO', 'FBS', 'SDS'])
        self.assertEqual(item['incomplete_fields'], [])
        self.assertEqual(result['unmatched_identifiers'], [])
        self.assertEqual(item['matched_identifiers'], ['0030'])
        self.assertEqual(result['sku_coverage'], 'not_verified')
        self.assertNotIn('private', json.dumps(result))

    def test_missing_sku_sources_and_unmatched_are_not_successful_mappings(self):
        result = details.project_response('ozon_product_info', {'skus': ['5215904379']},
            {'items': [{'id': 10, 'offer_id': 'A', 'name': 'X', 'sku': 0}]}, CREDS)
        self.assertEqual(result['unmatched_identifiers'], ['5215904379'])
        self.assertEqual(result['unmatched_row_indices'], [0])
        self.assertEqual(result['items'][0]['incomplete_fields'], ['sku', 'sources'])
        self.assertFalse(result['items'][0]['sources_reported'])
        self.assertEqual(result['items'][0]['sku_candidates'], [])

    def test_conflicting_sku_or_offer_is_flagged(self):
        rows = [{'id': 1, 'offer_id': 'A', 'name': 'One', 'sku': 55, 'sources': []},
                {'id': 2, 'offer_id': 'A', 'name': 'Two', 'sku': 55, 'sources': []}]
        result = details.project_response('ozon_product_info', {'offer_ids': ['A']}, {'items': rows}, CREDS)
        self.assertEqual(result['conflicts'], [{'identifier_type': 'offer_id', 'row_indices': [0, 1]},
                                              {'identifier_type': 'sku', 'row_indices': [0, 1]}])

    def test_inexact_and_unsafe_product_values_are_rejected(self):
        for key, bad in [('id', True), ('sku', False), ('sku', 1.25), ('sku', '001'),
                         ('name', CREDS['api_key']), ('offer_id', 'bad\nvalue'),
                         ('sources', [{'sku': True}])]:
            row = {'id': 10, 'offer_id': 'A', 'name': 'X', 'sku': 9, 'sources': []}
            row[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                details.project_response('ozon_product_info', {'offer_ids': ['A']}, {'items': [row]}, CREDS)

    def test_accrual_types_are_deduplicated_and_labeled_by_provider(self):
        data = {'accrual_types': [{'id': 4, 'name': 'Факт', 'description': 'строка 1\nстрока 2'}]}
        result = details.project_response('ozon_accrual_types', {}, data, CREDS)
        self.assertEqual(result['types'], [{**data['accrual_types'][0], 'category':'other'}])
        self.assertTrue(result['untrusted_data'])
        with self.assertRaises(ValueError):
            details.project_response('ozon_accrual_types', {}, {'accrual_types': data['accrual_types'] * 2}, CREDS)

    def test_postings_preserve_dates_sku_money_nulls_and_unreturned(self):
        args = {'posting_numbers': ['A-1', 'B-1']}
        data = {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [
            {'accrual_date': '2026-11-04', 'type_id': 2,
             'accrued': {'amount': '-12.35', 'currency': 'RUB'}, 'quantity': 2,
             'seller_price': {'amount': '100.00', 'currency': 'RUB'}, 'sku': 9007199254740993},
            {'accrual_date': '2026-11-05', 'type_id': 3,
             'accrued': None, 'quantity': None, 'seller_price': None, 'sku': None}]}]}
        result = details.project_response('ozon_posting_accruals', args, data, CREDS)
        records = result['posting_accruals'][0]['accruals']
        self.assertEqual(records[0]['sku'], '9007199254740993')
        self.assertEqual(records[0]['accrued'], {'amount': '-12.35', 'currency': 'RUB'})
        self.assertIsNone(records[1]['accrued'])
        self.assertEqual(result['unreturned_posting_numbers'], ['B-1'])
        self.assertEqual(result['period_basis'], 'accrual_date')
        self.assertFalse(result['complete_for_seller'])

    def test_summary_sku_filter_does_not_allocate_untagged_costs(self):
        def row(sku):
            return {'accrual_date':'2026-11-02', 'type_id':1, 'sku':sku,
                    'accrued':{'amount':'-7.00','currency':'RUB'}}
        data = {'posting_accruals':[{'posting_number':'A-1',
                                     'accruals':[row(10),row(11),row(None)]}]}
        args = {'posting_numbers':['A-1'], 'view':'summary', 'skus':['10']}
        result = details.project_response('ozon_posting_accruals', args, data, CREDS)
        self.assertEqual(result['filtered_skus'], ['10'])
        self.assertIsNone(result['breakdown'][0]['financial_total'])
        self.assertEqual(result['breakdown'][0]['observed_row_sum'], '-7.00')
        self.assertTrue(result['breakdown'][0]['unattributed_rows_of_same_type'])
        self.assertEqual(result['unattributed_rows_excluded'], 1)
        self.assertEqual(result['other_sku_rows_excluded'], 1)
        self.assertFalse(result['complete_for_seller'])

    def test_summary_uses_accrual_date_and_exact_separate_currency_sums(self):
        args = {'posting_numbers': ['A-1', 'B-1'], 'date_from': '2026-11-01',
                'date_to': '2026-11-07', 'view': 'summary'}
        def entry(day, amount, currency, sku=9):
            return {'accrual_date': day, 'type_id': 10, 'sku': sku, 'quantity': 2,
                    'accrued': {'amount': amount, 'currency': currency} if amount is not None else None}
        data = {'posting_accruals': [
            {'posting_number': 'A-1', 'accruals': [entry('2026-11-01', '0.10', 'RUB'),
                entry('2026-11-07', '0.20', 'RUB'), entry('2026-11-08', '999', 'RUB'),
                entry('2026-11-03', '2.00', 'EUR'), entry('2026-11-04', None, None)]},
            {'posting_number': 'B-1', 'accruals': [entry('2026-11-05', '-0.05', 'RUB')]}]}
        result = details.project_response('ozon_posting_accruals', args, data, CREDS)
        self.assertEqual(result['period_basis'], 'accrual_date')
        self.assertEqual(result['summary_scope'], 'requested_postings_only')
        self.assertNotIn('posting_accruals', result)
        groups = {row['currency']: row for row in result['breakdown']}
        self.assertIsNone(groups['RUB']['financial_total'])
        self.assertEqual(groups['RUB']['observed_row_sum'], '0.25')
        self.assertEqual(groups['RUB']['row_count'], 3)
        self.assertIsNone(groups['EUR']['financial_total'])
        self.assertEqual(groups['EUR']['observed_row_sum'], '2.00')
        self.assertIsNone(groups[None]['financial_total'])
        self.assertIsNone(groups[None]['observed_row_sum'])
        self.assertEqual(groups[None]['missing_amounts'], 1)
        self.assertFalse(result['complete_for_seller'])

    def test_missing_amount_with_known_currency_marks_that_currency_incomplete(self):
        args = {'posting_numbers':['A-1'], 'view':'summary'}
        def row(money): return {'accrual_date':'2026-11-03', 'type_id':1, 'sku':5, 'accrued':money}
        data = {'posting_accruals':[{'posting_number':'A-1', 'accruals':[
            row({'amount':'0.20','currency':'RUB'}), row({'amount':None,'currency':'RUB'})]}]}
        result = details.project_response('ozon_posting_accruals', args, data, CREDS)
        group = result['breakdown'][0]
        self.assertEqual(group['currency'], 'RUB')
        self.assertEqual(group['observed_row_sum'], '0.20')
        self.assertIsNone(group['financial_total'])
        self.assertEqual(group['missing_amounts'], 1)

    def test_known_amounts_without_currencies_are_not_summed(self):
        args = {'posting_numbers':['A-1'], 'view':'summary'}
        def row(amount): return {'accrual_date':'2026-11-03', 'type_id':1, 'sku':5,
                                 'accrued':{'amount':amount,'currency':None}}
        data = {'posting_accruals':[{'posting_number':'A-1', 'accruals':[row('3'),row('4')]}]}
        group = details.project_response('ozon_posting_accruals', args, data, CREDS)['breakdown'][0]
        self.assertIsNone(group['financial_total'])
        self.assertIsNone(group['observed_row_sum'])
        self.assertEqual(group['unknown_currency_amounts'], 2)

    def test_unreturned_posting_prevents_complete_total(self):
        args = {'posting_numbers':['A-1','B-1'], 'view':'summary'}
        data = {'posting_accruals':[{'posting_number':'A-1', 'accruals':[
            {'accrual_date':'2026-11-03', 'type_id':1, 'sku':5,
             'accrued':{'amount':'7.00','currency':'RUB'}}]}]}
        result = details.project_response('ozon_posting_accruals', args, data, CREDS)
        group = result['breakdown'][0]
        self.assertIsNone(group['financial_total'])
        self.assertEqual(group['observed_row_sum'], '7.00')
        self.assertTrue(group['unreturned_postings'])

    def test_equal_projections_are_preserved_not_declared_duplicates(self):
        args={'posting_numbers':['A','B'],'view':'summary'}
        row={'accrual_date':'2026-11-04','type_id':7,'sku':9,
             'accrued':{'amount':'10.00','currency':'RUB'}}
        data={'posting_accruals':[{'posting_number':'A','accruals':[row,row.copy()]},
                                  {'posting_number':'B','accruals':[row.copy()]}]}
        result=details.project_response('ozon_posting_accruals',args,data,CREDS)
        group=result['breakdown'][0]
        self.assertEqual(group['row_count'],3)
        self.assertEqual(group['observed_row_sum'],'30.00')
        self.assertIsNone(group['financial_total'])
        self.assertEqual(result['same_posting_equal_projection_groups'],
                         [{'posting_number':'A','row_indices':[0,1],'sku':'9'}])
        self.assertEqual(result['requested_posting_numbers'],['A','B'])
        self.assertEqual(result['returned_posting_numbers'],['A','B'])
        self.assertEqual(result['row_identity'],'not_verified')
        self.assertEqual(result['financial_additivity'],'not_verified')

    def test_postings_reject_duplicate_unrequested_or_invalid_finance_data(self):
        args = {'posting_numbers': ['A-1']}
        good = {'accrual_date': '2026-11-04', 'type_id': 1,
                'accrued': {'amount': '-1.00', 'currency': 'RUB'},
                'quantity': 1, 'sku': 2, 'seller_price': None}
        payloads = [
            {'posting_accruals': [{'posting_number': 'other', 'accruals': []}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': []}] * 2},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'accrual_date': '2026-02-30'}]}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'type_id': True}]}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'sku': False}]}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'quantity': True}]}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'accrued': {'amount': '1e300', 'currency': 'RUB'}}]}]},
            {'posting_accruals': [{'posting_number': 'A-1', 'accruals': [{**good, 'accrued': {'amount': 1.5, 'currency': 'RUB'}}]}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                details.project_response('ozon_posting_accruals', args, payload, CREDS)

    def test_provider_rejects_duplicate_json_keys(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        class Opener:
            def open(self, request, timeout):
                return Response(b'{"items": [], "items": []}')
        with patch('portal.urllib.request.build_opener', return_value=Opener()), self.assertRaises(ValueError):
            portal._provider_read('ozon_product_info', {'offer_ids': ['A']}, CREDS)

    def test_new_tools_pass_mcp_validation_with_fake_gateway(self):
        client = test_external_mcp.ExternalMCPTests()
        client.setUp()
        for name, args in [('ozon_product_info', {'offer_ids': ['0030']}),
                           ('ozon_accrual_types', {}),
                           ('ozon_posting_accruals', {'posting_numbers': ['A-1']})]:
            with self.subTest(name=name):
                response = client.request(client.rpc('tools/call', {'name': name, 'arguments': args}))
                self.assertNotIn('isError', response['body']['result'])
                self.assertEqual(client.calls[-1][1:], (name, args))

    def test_large_posting_returns_explicit_size_error_without_truncating(self):
        client=test_external_mcp.ExternalMCPTests()
        client.setUp()
        row={'accrual_date':'2026-11-04','type_id':7,'sku':9,
             'accrued':{'amount':'10.00','currency':'RUB'}}
        data={'posting_accruals':[{'posting_number':'A','accruals':[row]*5000}]}
        def run(_,name,args): return ozon_mcp.project_response(name,args,data,CREDS)
        def call(view):
            request=client.rpc('tools/call',{'name':'ozon_posting_accruals',
                               'arguments':{'posting_numbers':['A'],'view':view}})
            return client.request(request,execute=run)['body']['result']
        detail=call('details')
        self.assertTrue(detail['isError'])
        self.assertEqual(json.loads(detail['content'][0]['text'])['error'],'response_too_large')
        summary=call('summary')
        self.assertNotIn('isError',summary)
        group=json.loads(summary['content'][0]['text'])['breakdown'][0]
        self.assertEqual(group['row_count'],5000)
        self.assertIsNone(group['financial_total'])


if __name__ == '__main__': unittest.main()
