"""Wire-shaped synthetic Seller API responses, exercised through the public MCP catalog."""
import io
import json
import time
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

import external_mcp
import finance_gate
import ozon_fbo as fbo
import ozon_mcp
import ozon_supply_allocation as allocation
import ozon_unallocated as fees
import portal

CREDS = {'client_id': '754', 'api_key': 'synthetic-supplier-key'}
DAY = '2026-11-15'
ORDER_ID = '9007199254740993'
EXPENSE = {'document_id': 'example-invoice', 'line_id': '03', 'document_date': DAY, 'scope': 'single_supply',
           'supply_id': '70', 'amount': '-10.01', 'currency': 'RUB'}
ARGS = {'order_id': ORDER_ID, 'supply_id': '70', 'expense': EXPENSE}


def details():
    return {'orders': [{'order_id': int(ORDER_ID), 'order_number': '0007', 'state': 'COMPLETED',
                        'created_date': DAY+'T00:00:00Z', 'state_updated_date': DAY+'T15:00:00Z',
                        'supplies': [{'supply_id': 71, 'bundle_id': 'other', 'state': 'CANCELLED'},
                                     {'supply_id': 70, 'bundle_id': 'chosen', 'state': 'COMPLETED',
                                      'storage_warehouse': {'warehouse_id': 80, 'arrival_date': DAY+'T05:00:00Z'}}]}]}


def item(sku, quantity=1, volume=0.5, **overrides):
    return {'sku': sku, 'offer_id': '0007', 'product_id': 10, 'name': 'Товар',
            'quantity': quantity, 'volume_in_litres': volume, **overrides}


def page(rows, total, *, more=False, cursor=''):
    return {'items': rows, 'total_count': total, 'has_next': more, 'last_id': cursor}


def fee(kind, amount, currency='RUB'):
    return {'type_id': kind, 'accrued': {'amount': amount, 'currency': currency}}


def accrual(accrual_id, category, **fields):
    return {'accrual_id': accrual_id, 'accrued_category': category, 'date': DAY, 'unit_number': '70',
            'total_amount': {'amount': '999', 'currency': 'RUB'}, **fields}


class Seller:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((urlsplit(request.full_url).path, json.loads(request.data)))
        expected, payload = next(self.responses)
        if urlsplit(request.full_url).netloc != 'api-seller.ozon.ru' or self.calls[-1][0] != expected:
            raise AssertionError('Unexpected seller endpoint')
        if isinstance(payload, BaseException):
            raise payload
        return io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())


class SupplyCostTests(unittest.TestCase):
    def call(self, name, args, responses):
        seller = Seller(responses)
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
        raw = json.dumps(message).encode()
        environ = {'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),
                   'wsgi.input':io.BytesIO(raw),'HTTP_AUTHORIZATION':'Bearer mcp_' + 'a' * 43}
        with patch('portal.urllib.request.build_opener', return_value=seller):
            output = external_mcp.handle(environ, lambda *_: None, lambda token: 'example-owner',
                                         lambda key, op, arg: portal._provider_read(op, arg, CREDS), 'https://example.org')
            response = json.loads(b''.join(output))['result']
        self.assertNotIn('isError', response, response)
        return json.loads(response['content'][0]['text']), seller.calls

    def model(self, pages, args=ARGS, order=None, validation_pages=None, final_order=None):
        first_details = details() if order is None else order
        again = pages if validation_pages is None and len(pages) > 1 else (validation_pages or [])
        return self.call(allocation.OPERATION, args, [(fbo.ENDPOINTS[fbo.GET], first_details)] +
                         [(fbo.ENDPOINTS[fbo.BUNDLE], p) for p in pages+again] +
                         [(fbo.ENDPOINTS[fbo.GET], first_details if final_order is None else final_order)])

    def test_supply_order_and_bundle_requests_preserve_ids_and_pagination(self):
        result, calls = self.call(fbo.LIST, {'order_number':'0007', 'last_id':'old', 'states':['COMPLETED']},
                                  [(fbo.ENDPOINTS[fbo.LIST], {'order_ids':[ORDER_ID], 'last_id':'new'})])
        self.assertEqual(calls[0][1], {'filter':{'states':['COMPLETED'],'order_number_search':'0007'},
                        'last_id':'old','sort_by':'ORDER_CREATION','sort_dir':'DESC','limit':50})
        self.assertEqual((result['next_cursor'], result['order_ids']), ('new', [ORDER_ID]))
        result, _ = self.call(fbo.GET, {'order_ids':[ORDER_ID,'2']}, [(fbo.ENDPOINTS[fbo.GET], details())])
        self.assertEqual(result['missing_order_ids'], ['2'])
        order, = result['orders']
        self.assertEqual((order['order_number'], order['order_id'], order['state']), ('0007', ORDER_ID, 'COMPLETED'))
        self.assertEqual([s['supply_id'] for s in order['supplies']], ['71','70'])
        self.assertEqual(order['supplies'][1]['arrival_date'], DAY+'T05:00:00Z')
        result, calls = self.call(fbo.BUNDLE, {'bundle_id':'chosen','last_id':'A','limit':1},
                                  [(fbo.ENDPOINTS[fbo.BUNDLE], page([item(11)], 2, cursor='ignored'))])
        self.assertEqual(calls[0][1], {'bundle_ids':['chosen'],'limit':1,'last_id':'A','sort_field':'SKU','is_asc':True})
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(result['items'][0]['volume_in_litres'], '0.5')

    def test_all_requested_accrual_types_are_separated_by_actual_sku(self):
        operations = [accrual(1,'NON_ITEM',non_item_fee=fee(12,'-1.20')),
                      accrual(2,'CONTAINER_FEES',container_fees={'fees':[fee(133,'2.00','KZT'),fee(134,'-3')]}),
                      accrual(3,'ITEM',item_fees={'fees':[{'sku':0,'fees':[fee(77,'-4')]},
                                                        {'sku':95,'fees':[fee(12,'-9')]}]}),
                      accrual(4,'POSTING',posting={'products':[{'delivery':{'services':[fee(46,'-5'),fee(100,'888')],
                                                                            'total_accrued':{'amount':'-5','currency':'RUB'}}}]})]
        types = {'accrual_types':[{'id':n,'name':'synthetic '+str(n)} for n in fees.TYPE_IDS]}
        result, calls = self.call(fees.OPERATION, {'date':DAY,'last_id':'first'},
            [('/v1/finance/accrual/by-day', {'accruals':operations,'last_id':'second'}), ('/v1/finance/accrual/types', types)])
        self.assertEqual([body for _, body in calls], [{'date':DAY,'last_id':'first'}, {}])
        self.assertEqual(result['next_cursor'], 'second')
        rows = result['unallocated_fees']
        self.assertEqual([(r['type_id'],r['amount']) for r in rows], [(12,'-1.20'),(133,'2.00'),(134,'-3'),(77,'-4'),(46,'-5')])
        self.assertEqual((rows[1]['currency'],rows[-1]['posting_number_candidate'],rows[0]['posting_number_candidate']), ('KZT','70',None))
        self.assertTrue(all(row['posting_number'] is None for row in rows))
        self.assertTrue(all(r['supply_id'] is None and r['unit_number'] == '70' and r['date'] == DAY for r in rows))
        self.assertEqual([r['sku'] for r in result['fees_with_explicit_sku']], ['95'])
        self.assertEqual(result['page_accrual_ids'], ['1','2','3','4'])
        self.assertEqual([r['name'] for r in result['type_dictionary']], ['synthetic '+str(n) for n in fees.TYPE_IDS])
        self.assertEqual((result['row_issues'], result['type_dictionary_status']), ([], 'complete'))

    def test_unallocated_incomplete_rows_duplicate_ids_and_cursor_are_visible(self):
        operations = [accrual(1,'UNSPECIFIED'), accrual(1,'NON_ITEM',non_item_fee={'type_id':12}),
                      accrual(2,'NON_ITEM'), accrual(3,'POSTING',posting={}),
                      accrual(4,'POSTING',posting={'products':[{'commission':{}}]})]
        result, calls = self.call(fees.OPERATION, {'date':DAY,'last_id':'A','include_types':False},
                                  [('/v1/finance/accrual/by-day',{'accruals':operations,'last_id':'A'})])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['page_issues'], ['repeated_cursor'])
        self.assertIsNone(result['next_cursor'])
        self.assertIn('repeated_accrual_id', result['row_issues'][1]['issues'])
        self.assertIn('missing_amount_or_currency', result['row_issues'][1]['issues'])
        self.assertIn('missing_delivery', result['row_issues'][-1]['issues'])
        self.assertEqual([r['amount'] for r in result['unallocated_fees']], [None])
        self.assertEqual(result['period_completeness'], 'not_verified')
        result, _ = self.call(fees.OPERATION, {'date':DAY}, [('/v1/finance/accrual/by-day',{}),
                              ('/v1/finance/accrual/types', {'accrual_types':[]})])
        self.assertEqual(result['page_issues'], ['missing_accruals','missing_cursor'])
        self.assertEqual(result['type_dictionary_status'], 'incomplete')
        self.assertTrue(all(row['name'] is None for row in result['type_dictionary']))

    def test_large_ids_and_equal_signed_fees_keep_their_source_identity(self):
        op_id, sku = 9007199254740993, 9007199254740995
        operations = [accrual(op_id,'CONTAINER_FEES',container_fees={'fees':[fee(12,'-1.00'),fee(12,'-1.00')]}),
                      accrual(op_id+1,'ITEM',item_fees={'fees':[{'sku':sku,'fees':[fee(12,'-1.00')]}]})]
        result,_ = self.call(fees.OPERATION, {'date':DAY,'include_types':False},
                             [('/v1/finance/accrual/by-day',{'accruals':operations,'last_id':''})])
        rows = result['unallocated_fees']
        self.assertEqual([r['accrual_id'] for r in rows],[str(op_id)]*2)
        self.assertEqual([r['fee_path'] for r in rows],['container_fees.fees[0]','container_fees.fees[1]'])
        self.assertTrue(all(r['sku'] is None and r['supply_id'] is None for r in rows))
        self.assertEqual([r['sku'] for r in result['fees_with_explicit_sku']],[str(sku)])

    def test_cost_model_uses_exact_selected_supply_and_all_skus_and_reconciles(self):
        result, calls = self.model([page([item(13),item(11)], 3, more=True, cursor='A'),
                                    page([item(12)], 3)])
        self.assertEqual(result['status'], 'modeled_on_document')
        self.assertEqual([body['bundle_ids'] for _, body in calls[1:-1]], [['chosen']]*4)
        self.assertEqual([body['last_id'] for _, body in calls[1:-1]], ['', 'A', '', 'A'])
        self.assertEqual((result['composition_check'],result['snapshot_consistency']),
                         ('matched_two_traversals','not_guaranteed_by_ozon'))
        self.assertEqual(result['expense'], EXPENSE)
        self.assertEqual(result['total_volume_litres'], '1.5')
        values = {row['sku']:row['allocated_amount'] for row in result['items']}
        self.assertEqual(values, {'11':'-3.34','12':'-3.34','13':'-3.33'})
        self.assertEqual(sum(map(Decimal, values.values())), Decimal(result['allocated_total']['amount']))
        self.assertEqual(result['items'][0]['cost_per_unit_exact'], {'numerator':'-1001','denominator':'300'})
        self.assertEqual([row['offer_id'] for row in result['items']], ['0007']*3)
        self.assertFalse(result['ozon_expense_link_verified'])
        self.assertFalse(result['included_in_article_actuals'])

    def test_incomplete_first_item_keeps_later_bundle_pages_without_an_allocation(self):
        first = item(11, name=None, quantity=None)
        result, calls = self.model([page([first],2,more=True,cursor='next'),page([item(12)],2)])
        self.assertEqual([request['last_id'] for path,request in calls if path == fbo.ENDPOINTS[fbo.BUNDLE]],['','next'])
        self.assertEqual([row['sku'] for row in result['items']],['11','12'])
        self.assertTrue({'missing_item_name','missing_item_quantity'} <= set(result['issues']))
        self.assertEqual(result['status'],'incomplete')
        self.assertIsNone(result['allocated_total'])
        self.assertFalse(result['included_in_article_actuals'])

    def test_page_budget_marks_remaining_composition_unknown(self):
        count = allocation.MAX_PAGES
        pages = [page([item(i+1)],count+1,more=True,cursor=str(i+1)) for i in range(count)]
        result, _ = self.model(pages)
        self.assertEqual(result['pages_observed'],count)
        self.assertEqual(len(result['items']),count)
        self.assertIn('bundle_page_limit_reached',result['issues'])
        self.assertEqual(result['status'],'incomplete')
        self.assertIsNone(result['allocated_total'])

    def test_different_quantities_and_volumes_credit_and_currency_precision(self):
        args = {**ARGS,'expense':{**EXPENSE,'amount':'0.010','currency':'KWD'}}
        result, _ = self.model([page([item(20,2,0.2,total_volume_in_litres=0.4),item(21,1,0.1)], 2)], args)
        self.assertEqual([r['allocated_amount'] for r in result['items']], ['0.008','0.002'])
        self.assertEqual(result['items'][0]['cost_per_unit_exact'], {'numerator':'1','denominator':'250'})
        self.assertEqual(Decimal(result['total_volume_litres']), Decimal('0.5'))

    def test_equivalent_money_preserves_half_a_ruble_regardless_of_input_notation(self):
        for amount in ('1','1.0','1.00','1.000'):
            with self.subTest(amount=amount):
                args = {**ARGS,'expense':{**EXPENSE,'amount':amount}}
                result, _ = self.model([page([item(20),item(21)],2)], args)
                self.assertEqual([r['allocated_amount'] for r in result['items']], ['0.50','0.50'])
                self.assertEqual(result['currency_minor_unit_digits'], 2)
        for expense, reason in [({**EXPENSE,'amount':'0.001'},'expense_exceeds_currency_precision'),
                                ({**EXPENSE,'currency':'UNKNOWN'},'currency_minor_unit_unknown')]:
            result, _ = self.model([page([item(20)],1)], {**ARGS,'expense':expense})
            self.assertIn(reason,result['issues'])
            self.assertIsNone(result['allocated_total'])

    def test_integer_ceilings_are_disclosed_but_only_per_unit_litres_weight_the_model(self):
        rows = [item(20,3,0.4,total_volume_in_litres=2),item(21,2,0.4,total_volume_in_litres=1)]
        result, _ = self.model([page(rows,2)], {**ARGS,'expense':{**EXPENSE,'amount':'-1.00'}})
        self.assertEqual(result['status'],'modeled_on_document')
        self.assertEqual(result['total_volume_litres'],'2')
        self.assertEqual([r['allocated_amount'] for r in result['items']],['-0.60','-0.40'])
        self.assertEqual([(v['quantity_times_unit_litres'],v['reported_total_litres'],v['relation'])
                          for v in result['volume_differences']],
                         [('1.2','2','equals_whole_litre_ceiling'),('0.8','1','equals_whole_litre_ceiling')])
        rows[0]['total_volume_in_litres'] = 3
        result, _ = self.model([page(rows,2)])
        self.assertEqual(result['volume_differences'][0]['relation'],'unreconciled')
        self.assertIn('inconsistent_provider_volumes',result['issues'])
        self.assertIsNone(result['allocated_total'])

    def test_consistency_check_catches_changed_composition_with_stable_count_and_changed_bundle_link(self):
        first = [page([item(1),item(2)],4,more=True,cursor='A'),page([item(4),item(5)],4)]
        second = [page([item(2),item(3)],4,more=True,cursor='B'),page([item(4),item(5)],4)]
        result, _ = self.model(first, validation_pages=second)
        self.assertIn('composition_changed_during_read', result['issues'])
        self.assertEqual(result['validation_pages'],2)
        self.assertIsNone(result['allocated_total'])
        changed = details()
        changed['orders'][0]['supplies'][1]['bundle_id'] = 'changed'
        result, _ = self.model([page([item(1)],1)], final_order=changed)
        self.assertIn('supply_changed_during_read',result['issues'])

    def test_missing_volume_zero_quantity_inconsistent_totals_and_duplicates_block_calculation(self):
        for rows,reason in [([item(11,volume=None,total_volume_in_litres=1)],'missing_or_zero_unit_volume'),
                            ([item(11,quantity=0)],'missing_or_zero_quantity'),
                            ([item(11,quantity=None)],'missing_or_zero_quantity'),
                            ([item(11,total_volume_in_litres=0)],'inconsistent_provider_volumes'),
                            ([item(11,contractor_item_code='007')],'conflicting_article_identifiers'),
                            ([item(11),item(11,offer_id='different')],'repeated_sku'),
                            ([], 'empty_composition')]:
            with self.subTest(reason=reason):
                result, _ = self.model([page(rows,len(rows))])
                self.assertIn(reason,result['issues'])
                self.assertEqual(result['status'],'incomplete')
                self.assertIsNone(result['allocated_total'])
                self.assertTrue(all('allocated_amount' not in r for r in result['items']))

    def test_unknown_expense_duplicate_cursor_conflicting_count_and_cross_page_duplicates(self):
        for pages,reason in [
            ([page([item(11)],2,more=True,cursor='A'),page([item(11)],2)],'repeated_sku'),
            ([page([item(11)],2,more=True,cursor='A'),page([item(12)],3)],'unreconciled_bundle_total_count'),
            ([page([item(11)],2,more=True,cursor='A'),page([item(12)],2,more=True,cursor='A')],'repeated_cursor'),
            ([page([item(11)],2,more=True)],'missing_next_cursor'),
            ([{'items':[item(11)],'total_count':1}],'missing_has_next'),
            ([{'items':None,'has_next':False}],'missing_items')]:
            with self.subTest(reason=reason):
                result, _ = self.model(pages)
                self.assertIn(reason,result['issues'])
                self.assertIsNone(result['allocated_total'])
        for expense,reason in [({},'missing_supply_cost_document_fields'),
                               ({**EXPENSE,'supply_id':'71'},'expense_link_to_single_supply_unconfirmed'),
                               ({**EXPENSE,'scope':'shared_or_unknown'},'expense_link_to_single_supply_unconfirmed'),
                               ({**EXPENSE,'accrual_id':'70'},'ozon_accrual_to_supply_link_unverified')]:
            with self.subTest(reason=reason):
                result, _ = self.model([page([item(11)],1)], {**ARGS,'expense':expense})
                self.assertIn(reason,result['issues'])
                self.assertIsNone(result['allocated_total'])

    def test_ambiguous_bundle_or_missing_order_never_reads_an_arbitrary_bundle(self):
        order = details()
        order['orders'][0]['supplies'][0]['bundle_id'] = 'chosen'
        result, calls = self.model([], order=order)
        self.assertEqual(result['issues'], ['bundle_shared_between_supplies'])
        self.assertEqual(len(calls), 1)
        result, _ = self.model([], order={'orders':[]})
        self.assertIn('requested_order_missing', result['issues'])
        self.assertIsNone(result['allocated_total'])

    def test_dictionary_403_keeps_rows_and_short_429_on_dictionary_is_not_replayed(self):
        daily = ('/v1/finance/accrual/by-day', {'accruals':[],'last_id':''})
        path = '/v1/finance/accrual/types'
        result, _ = self.call(fees.OPERATION, {'date':DAY}, [daily,(path,urllib.error.HTTPError('',403,'',{},None))])
        self.assertEqual(result['type_dictionary_status'], 'provider_access_denied')
        error = urllib.error.HTTPError('',429,'',{'Retry-After':'1'},None)
        error.retry_after_seconds, error.retry_after_source = 1, 'delta_seconds'
        seller = Seller([daily,(path,error)])
        gate_dir = str(Path(self.enterContext(TemporaryDirectory())) / 'gate')
        request = {'operation':fees.OPERATION, 'args':{'date':DAY}, 'creds':CREDS,
                   'finance_gate_dir':gate_dir, 'deadline_monotonic':time.monotonic()+60}
        output = io.StringIO()
        with patch('sys.stdin',io.StringIO(json.dumps(request))), patch('sys.stdout',output), \
             patch('portal.urllib.request.build_opener',return_value=seller), patch('portal.time.sleep') as sleep, \
             patch('finance_gate.run',wraps=finance_gate.run) as gate:
            portal._provider_worker()
        self.assertEqual(json.loads(output.getvalue())['status'],429)
        self.assertEqual(json.loads(output.getvalue())['retry_after_seconds'],1)
        self.assertEqual(len(seller.calls),2)
        self.assertEqual(sum(call.args[0] == gate_dir for call in gate.call_args_list), 1)
        sleep.assert_not_called()

    def test_reject_undocumented_inputs_and_keep_the_finance_gate_mandatory(self):
        for name,args in [(fbo.GET,{'order_ids':[1,'1']}),(fbo.GET,{'order_ids':['1']*51}),
                          (fbo.BUNDLE,{'bundle_id':'one','supply_id':'2'}),
                          (fees.OPERATION,{'date':'2021-12-31'}),(fees.OPERATION,{'date':DAY,'url':'anything'}),
                          (allocation.OPERATION,{**ARGS,'expense':{**EXPENSE,'confirmed':True}})]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                ozon_mcp.validate_args(name,args)
        self.assertIn(fees.OPERATION,finance_gate.FINANCE_OPERATIONS)
        with self.assertRaises(finance_gate.GateError):
            portal.provider_read(fees.OPERATION,{'date':DAY},CREDS)
        with self.assertRaises(ValueError):
            portal.finance_request_preflight(fees.OPERATION,{'date':DAY,'last_id':CREDS['api_key']},CREDS)
