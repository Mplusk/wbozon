"""Independent, offline wire-contract tests for Ozon read-only expansion.

The fixtures follow Ozon's Seller API schema, not responses from a seller.
No credential, live account, HTTP request, or application state is used.
"""
import copy
import hashlib
import json
import sqlite3
import unittest
import urllib.error
from contextlib import contextmanager
from decimal import Decimal
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import ozon_expansion
import market_transport


BASE = 'https://api-seller.ozon.ru'
ARTICLE = 'model-0030'
SKU = '9007199254740993'  # valid int64 that cannot be represented exactly by a JS Number


class SellerFixture:
    """Return only explicitly registered synthetic Seller API responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, *, query=None, body=None, **options):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'api-seller.ozon.ru' or parsed.fragment:
            raise AssertionError('unexpected Seller API host')
        if method.upper() != 'POST' or parsed.path not in self.responses:
            raise AssertionError('unexpected Seller API operation: ' + parsed.path)
        self.calls.append({'path': parsed.path, 'body': copy.deepcopy(body),
                           'query': copy.deepcopy(query), 'options': dict(options)})
        response = self.responses[parsed.path]
        if callable(response):
            response = response(body)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)

    def body_for(self, path):
        bodies = [call['body'] for call in self.calls if call['path'] == path]
        if len(bodies) != 1:
            raise AssertionError('expected exactly one call: ' + path)
        return bodies[0]


def act_items_in(value, act_id=None):
    """Find quantities with their act ID, including within nested act groups."""
    if isinstance(value, dict):
        act_id = value.get('act_id', act_id)
        if act_id is not None and {'fact_quantity', 'declared_quantity', 'approved_quantity'} <= set(value):
            yield str(act_id), value
        for item in value.values():
            yield from act_items_in(item, act_id)
    elif isinstance(value, list):
        for item in value:
            yield from act_items_in(item, act_id)


def one_supply_wire(*, act_item, acts=None):
    """One synthetic declared supply with an optionally incomplete act item."""
    return {
        '/v1/supply-order/bundle': {'items': [{'sku': 101, 'offer_id': 'A', 'name': 'A', 'quantity': 10}],
                                    'has_next': False, 'last_id': '', 'total_count': 1},
        '/v3/supply-order/get': {'orders': [{'order_id': 42, 'supplies': [
            {'supply_id': 77, 'bundle_id': 'B'}]}]},
        '/v1/supply-order/act/summary/get': {'supplies_acts': [{'supply_id': 77,
            'is_agreement_completed': True, 'supply_acts': [
                {'act_id': 51, 'type': 'ACCEPTANCE', 'act_state': 'ACCEPTED'}]}]},
        '/v1/supply-order/act/product/get': {'supply_id': 77, 'supply_acts': acts if acts is not None else [
            {'act_id': 51, 'type': 'ACCEPTANCE', 'items': [act_item]}]},
    }


class OzonExpansionContracts(unittest.TestCase):
    def read(self, name, args, responses):
        provider = SellerFixture(responses)
        result = ozon_expansion.read(name, args, provider)
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result.get('items'), list)
        self.assertIn('complete', result)
        return result, provider

    def test_stocks_preserve_fbo_fbs_and_do_not_split_warehouse_totals(self):
        path = '/v4/product/info/stocks'
        data = {'items': [{'offer_id': ARTICLE, 'product_id': 701,
                           'stocks': [{'sku': int(SKU), 'type': 'fbo', 'present': 9, 'reserved': 2,
                                       'warehouse_ids': [501, 502]},
                                      {'sku': 102, 'type': 'fbs', 'present': 5, 'reserved': 1}]}],
                'cursor': 'next-1', 'total_items': 2}
        out, provider = self.read('ozon_stocks', {'article': ARTICLE, 'limit': 1}, {path: data})
        body = provider.body_for(path)
        self.assertEqual(body['filter']['offer_id'], [ARTICLE])
        self.assertEqual(body['limit'], 1)
        self.assertFalse(out['complete'])
        self.assertEqual(out['next_cursor'], 'next-1')
        self.assertEqual(len(out['items']), 2)
        by_scheme = {row['scheme'].lower(): row for row in out['items']}
        self.assertEqual(set(by_scheme), {'fbo', 'fbs'})
        self.assertEqual(str(by_scheme['fbo']['sku']), SKU)
        self.assertEqual((by_scheme['fbo']['present'], by_scheme['fbo']['reserved']), (9, 2))
        self.assertEqual([str(x) for x in by_scheme['fbo']['warehouse_ids']], ['501', '502'])
        self.assertEqual((by_scheme['fbs']['present'], by_scheme['fbs']['reserved']), (5, 1))

    def test_stocks_missing_reserved_is_not_zero(self):
        path = '/v4/product/info/stocks'
        data = {'items': [{'offer_id': ARTICLE, 'product_id': 701,
                           'stocks': [{'sku': 101, 'type': 'fbo', 'present': 4}]}],
                'cursor': '', 'total_items': 1}
        out, _ = self.read('ozon_stocks', {'article': ARTICLE}, {path: data})
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(out['items'][0]['present'], 4)
        self.assertIsNone(out['items'][0].get('reserved'))

    def test_stocks_repeated_cursor_does_not_report_completion(self):
        path = '/v4/product/info/stocks'
        provider = SellerFixture({path: {'items': [], 'cursor': 'repeat', 'total_items': 1}})
        try:
            result = ozon_expansion.read('ozon_stocks', {'article': ARTICLE, 'cursor': 'repeat'}, provider)
        except (TypeError, ValueError):
            pass
        else:
            self.assertFalse(result.get('complete'))
            self.assertNotEqual(result.get('next_cursor'), 'repeat')

    def test_stocks_by_cluster_keep_sku_warehouse_and_stock_states_distinct(self):
        path = '/v1/analytics/stocks'
        data = {'items': [
            {'sku': int(SKU), 'offer_id': ARTICLE, 'cluster_id': 31, 'cluster_name': 'Cluster A',
             'warehouse_id': 501, 'warehouse_name': 'Warehouse A', 'available_stock_count': 3,
             'valid_stock_count': 1, 'transit_stock_count': 2},
            {'sku': int(SKU), 'offer_id': ARTICLE, 'cluster_id': 31, 'cluster_name': 'Cluster A',
             'warehouse_id': 502, 'warehouse_name': 'Warehouse B', 'available_stock_count': 5,
             'valid_stock_count': 0, 'transit_stock_count': 0}]}
        out, provider = self.read('ozon_stocks_by_cluster', {'sku': SKU}, {path: data})
        self.assertEqual([str(x) for x in provider.body_for(path)['skus']], [SKU])
        self.assertEqual(len(out['items']), 2)
        by_warehouse = {str(item['warehouse_id']): item for item in out['items']}
        self.assertEqual(set(by_warehouse), {'501', '502'})
        self.assertEqual((by_warehouse['501']['available_stock_count'],
                          by_warehouse['501']['valid_stock_count'],
                          by_warehouse['501']['transit_stock_count']), (3, 1, 2))
        self.assertEqual(by_warehouse['502']['available_stock_count'], 5)

    def test_contradictory_sku_selectors_are_rejected_before_any_read(self):
        provider = SellerFixture({'/v1/analytics/stocks': {'items': []}})
        with self.assertRaises(ValueError):
            ozon_expansion.read('ozon_stocks_by_cluster', {'sku':103, 'skus':[101,102]}, provider)
        self.assertEqual(provider.calls, [])

    def test_article_lookup_includes_sku_found_only_in_stock_sources(self):
        info = '/v3/product/info/list'
        stocks = '/v1/analytics/stocks'
        cards = {'items': [{'id': 701, 'offer_id': ARTICLE, 'name': 'Example', 'sku': 101,
                            'sources': [{'source': 'FBO', 'sku': 101}],
                            'stocks': {'stocks': [{'source': 'FBS', 'sku': 102}]}}]}
        def cluster_stock(body):
            self.assertEqual({str(sku) for sku in body['skus']}, {'101', '102'})
            return {'items': [{'sku': 101, 'offer_id': ARTICLE, 'warehouse_id': 501,
                               'available_stock_count': 2},
                              {'sku': 102, 'offer_id': ARTICLE, 'warehouse_id': 502,
                               'available_stock_count': 3}]}
        out, _ = self.read('ozon_stocks_by_cluster', {'article': ARTICLE},
                           {info: cards, stocks: cluster_stock})
        self.assertEqual({str(item['sku']) for item in out['items']}, {'101', '102'})

    def test_article_with_incomplete_source_identity_is_not_a_complete_stock_report(self):
        info = '/v3/product/info/list'
        stocks = '/v1/analytics/stocks'
        cards = {'items': [{'id': 701, 'offer_id': ARTICLE, 'name': 'Example', 'sku': 101,
                            'sources': [{'source': 'FBO', 'sku': None}]}]}
        provider = SellerFixture({info: cards, stocks: {'items': [{'sku': 101, 'offer_id': ARTICLE,
                                                                   'available_stock_count': 2}]}})
        try:
            result = ozon_expansion.read('ozon_stocks_by_cluster', {'article': ARTICLE}, provider)
        except ValueError:
            return
        self.assertFalse(result.get('complete'))

    def test_stock_history_shared_database_is_scoped_and_encrypted_per_account(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE stock_history(series TEXT NOT NULL, day TEXT NOT NULL, observed TEXT NOT NULL, '
                   'blob BLOB NOT NULL, PRIMARY KEY (series, day))')
        db.execute('CREATE TABLE stock_coverage(series TEXT, day TEXT, observed TEXT, PRIMARY KEY (series, day))')
        def wire(secret):
            w = market_transport.Wire.__new__(market_transport.Wire)
            w.provider, w.creds = 'ozon', {'client_id': 'synthetic-client', 'api_key': secret}
            w.scope = market_transport._id([w.provider, w.creds])
            w.aead = AESGCM(hashlib.sha256(('market-cache-v1:' + w.scope).encode()).digest())
            w.db = lambda: db
            return w
        first, second = wire('synthetic-A'), wire('synthetic-B')
        stamp = '2026-10-01T12:00:00+00:00'
        args = {'sku': 101, 'date_from': '2026-10-01', 'date_to': '2026-10-01'}
        first.record_stocks([101], 'private-A', [{'sku': 101, 'article': 'private-A',
                                                  'warehouse_id': 7, 'available_stock_count': 3}], stamp)
        self.assertEqual(ozon_expansion.read('ozon_stocks_by_cluster', args, second)['items'], [])
        second.record_stocks([101], 'private-A', [{'sku': 101, 'article': 'private-A',
                                                   'warehouse_id': 7, 'available_stock_count': 99}], stamp)
        a = ozon_expansion.read('ozon_stocks_by_cluster', args, first)
        b = ozon_expansion.read('ozon_stocks_by_cluster', args, second)
        self.assertEqual([r['available_stock_count'] for r in a['items']], [3])
        self.assertEqual([r['available_stock_count'] for r in b['items']], [99])
        self.assertTrue(a['complete'] and b['complete'])
        self.assertTrue(all(b'private-A' not in blob for (blob,) in db.execute('SELECT blob FROM stock_history')))

    def test_sku_daily_uses_wire_metric_order_and_does_not_invent_currency(self):
        path = '/v1/analytics/data'
        def analytics(body):
            self.assertEqual(set(body['dimension']), {'sku', 'day'})
            # These two metrics are available without a Premium subscription.
            self.assertEqual(set(body['metrics']), {'revenue', 'ordered_units'})
            dimensions = {'sku': {'id': SKU, 'name': 'Example'},
                          'day': {'id': '2026-10-03', 'name': '2026-10-03'}}
            metrics = {'revenue': 113.25, 'ordered_units': 3}
            return {'result': {'data': [{'dimensions': [dimensions[d] for d in body['dimension']],
                                         'metrics': [metrics[m] for m in body['metrics']]}],
                               'totals': [metrics[m] for m in body['metrics']]},
                    'timestamp': '2026-10-04 00:10:00'}
        out, provider = self.read('ozon_sku_daily', {'sku': SKU, 'date_from': '2026-10-03',
                                                       'date_to': '2026-10-03', 'limit': 1},
                                  {path: analytics})
        body = provider.body_for(path)
        self.assertEqual((body['date_from'], body['date_to']), ('2026-10-03', '2026-10-03'))
        item, = out['items']
        self.assertEqual(str(item['sku']), SKU)
        self.assertEqual(item['date'], '2026-10-03')
        self.assertEqual(item['ordered_units'], 3)
        self.assertEqual(str(item['revenue']), '113.25')
        self.assertIsNone(item.get('currency'))

    def test_supply_orders_fetch_details_without_losing_the_list_cursor(self):
        listing = '/v3/supply-order/list'
        details = '/v3/supply-order/get'
        data = {listing: {'order_ids': ['42'], 'last_id': 'order-next'},
                details: {'orders': [{'order_id': 42, 'order_number': '00042', 'state': 'COMPLETED',
                          'supplies': [{'supply_id': 77, 'bundle_id': 'bundle-7',
                                        'state': 'COMPLETED'}]}]}}
        out, provider = self.read('ozon_supply_orders', {'limit': 1}, data)
        self.assertEqual(provider.body_for(listing)['limit'], 1)
        self.assertEqual([str(x) for x in provider.body_for(details)['order_ids']], ['42'])
        self.assertFalse(out['complete'])
        self.assertEqual(out['next_cursor'], 'order-next')
        encoded = json.dumps(out, ensure_ascii=False)
        for value in ('00042', 'COMPLETED', '77', 'bundle-7'):
            self.assertIn(value, encoded)

    def test_supply_items_keep_requested_and_received_distinct_by_act(self):
        bundle = '/v1/supply-order/bundle'
        order = '/v3/supply-order/get'
        summary = '/v1/supply-order/act/summary/get'
        products = '/v1/supply-order/act/product/get'
        product = {'sku': int(SKU), 'offer_id': ARTICLE, 'name': 'Example', 'quantity': 10}
        responses = {
            bundle: {'items': [product], 'has_next': False, 'last_id': '', 'total_count': 1},
            order: {'orders': [{'order_id': 42, 'supplies': [{'supply_id': 77, 'bundle_id': 'bundle-7'}]}]},
            summary: {'supplies_acts': [{'supply_id': 77, 'is_agreement_completed': False,
                       'supply_acts': [{'act_id': 51, 'type': 'ACCEPTANCE',
                                        'act_state': 'AWAITING_APPROVAL_BY_SELLER'},
                                       {'act_id': 52, 'type': 'ACCEPTANCE', 'act_state': 'ACCEPTED'}]}]},
            products: {'supply_id': 77, 'supply_acts': [
                {'act_id': 51, 'type': 'ACCEPTANCE', 'items': [{'sku_info': {'sku': int(SKU),
                    'offer_id': ARTICLE, 'name': 'Example'}, 'declared_quantity': 10,
                    'fact_quantity': 8, 'approved_quantity': 7}]},
                {'act_id': 52, 'type': 'ACCEPTANCE', 'items': [{'sku_info': {'sku': int(SKU),
                    'offer_id': ARTICLE, 'name': 'Example'}, 'declared_quantity': 10,
                    'fact_quantity': 9, 'approved_quantity': 9}]}]}}
        out, provider = self.read('ozon_supply_items', {'bundle_id': 'bundle-7', 'order_id': '42',
                                                         'supply_id': '77'}, responses)
        self.assertEqual(set(c['path'] for c in provider.calls), set(responses))
        self.assertEqual(provider.body_for(bundle)['bundle_ids'], ['bundle-7'])
        self.assertEqual(str(provider.body_for(products)['supply_id']), '77')
        acts = list(act_items_in(out))
        self.assertEqual({act_id for act_id, _ in acts}, {'51', '52'})
        amounts = {act_id: (x['declared_quantity'], x['fact_quantity'], x['approved_quantity'])
                   for act_id, x in acts}
        self.assertEqual(amounts, {'51': (10, 8, 7), '52': (10, 9, 9)})
        self.assertIn('AWAITING_APPROVAL_BY_SELLER', json.dumps(out, ensure_ascii=False))

    def test_supply_items_require_order_for_a_supply_act_before_any_request(self):
        provider = SellerFixture({})
        with self.assertRaises((TypeError, ValueError)):
            ozon_expansion.read('ozon_supply_items', {'bundle_id': 'bundle-7', 'supply_id': '77'}, provider)
        self.assertEqual(provider.calls, [])

    def test_supply_mismatched_bundle_does_not_read_another_supply_act(self):
        order = '/v3/supply-order/get'
        provider = SellerFixture({order: {'orders': [{'order_id': 42,
                    'supplies': [{'supply_id': 77, 'bundle_id': 'different-bundle'}]}]},
                    '/v1/supply-order/bundle': {'items': [], 'has_next': False,
                                                'last_id': '', 'total_count': 0}})
        try:
            result = ozon_expansion.read('ozon_supply_items', {'bundle_id': 'bundle-7',
                                          'order_id': '42', 'supply_id': '77'}, provider)
        except (TypeError, ValueError):
            pass
        else:
            self.assertFalse(result.get('complete'))
            self.assertNotIn('fact_quantity', json.dumps(result))
        self.assertNotIn('/v1/supply-order/act/product/get', [x['path'] for x in provider.calls])

    def test_supply_act_missing_summary_is_explicitly_incomplete(self):
        bundle = '/v1/supply-order/bundle'
        order = '/v3/supply-order/get'
        summary = '/v1/supply-order/act/summary/get'
        products = '/v1/supply-order/act/product/get'
        data = {
            bundle: {'items': [{'sku': 101, 'offer_id': 'A', 'name': 'A', 'quantity': 10}],
                     'has_next': False, 'last_id': '', 'total_count': 1},
            order: {'orders': [{'order_id': 42, 'supplies': [{'supply_id': 77, 'bundle_id': 'B'}]}]},
            summary: {'supplies_acts': []},
            products: {'supply_id': 77, 'supply_acts': [{'act_id': 51, 'type': 'ACCEPTANCE',
                       'items': [{'sku_info': {'sku': 101, 'offer_id': 'A'},
                                  'declared_quantity': 10, 'fact_quantity': 8, 'approved_quantity': 7}]}]}}
        out, _ = self.read('ozon_supply_items', {'bundle_id': 'B', 'order_id': '42', 'supply_id': '77'}, data)
        self.assertFalse(out['complete'])
        self.assertNotEqual(out.get('acceptance_status'), 'complete')
        self.assertTrue(out.get('issues') or out.get('acceptance_status') != 'observed_acts')

    def test_unagreed_receipt_does_not_confirm_the_final_quantity(self):
        item = {'sku_info': {'sku': 101, 'offer_id': 'A'}, 'declared_quantity': 10,
                'fact_quantity': 8, 'approved_quantity': 7}
        wire = one_supply_wire(act_item=item)
        summary = wire['/v1/supply-order/act/summary/get']['supplies_acts'][0]
        summary['is_agreement_completed'] = False
        summary['supply_acts'][0]['act_state'] = 'AWAITING_APPROVAL_BY_SELLER'
        out, _ = self.read('ozon_supply_items', {'bundle_id':'B','supply_id':77,'order_id':42}, wire)
        self.assertFalse(out['complete'])
        self.assertEqual(out['acceptance_status'], 'agreement_pending')
        self.assertEqual(out['acts'][0]['fact_quantity'], 8)

    def test_supply_act_missing_identity_is_not_a_complete_article_result(self):
        data = one_supply_wire(act_item={'sku_info': None, 'declared_quantity': 10,
                                        'fact_quantity': 8, 'approved_quantity': 7})
        out, _ = self.read('ozon_supply_items', {'bundle_id': 'B', 'order_id': '42',
                                                 'supply_id': '77', 'article': 'A'}, data)
        self.assertFalse(out['complete'])
        self.assertTrue(out.get('issues') or 'incomplete' in out.get('acceptance_status', ''))

    def test_supply_act_missing_fact_quantity_is_unknown(self):
        data = one_supply_wire(act_item={'sku_info': {'sku': 101, 'offer_id': 'A'},
                                        'declared_quantity': 10, 'approved_quantity': 7})
        out, _ = self.read('ozon_supply_items', {'bundle_id': 'B', 'order_id': '42', 'supply_id': '77'}, data)
        acts = list(act_items_in(out))
        self.assertEqual(len(acts), 1)
        self.assertIsNone(acts[0][1]['fact_quantity'])
        self.assertFalse(out['complete'])

    def test_supply_act_summary_and_product_identity_and_type_must_agree(self):
        item = {'sku_info': {'sku': 101, 'offer_id': 'A'}, 'declared_quantity': 10,
                'fact_quantity': 8, 'approved_quantity': 7}
        for conflict in ('missing_product_act', 'different_act_type'):
            with self.subTest(conflict=conflict):
                data = one_supply_wire(act_item=item)
                summary = data['/v1/supply-order/act/summary/get']['supplies_acts'][0]['supply_acts']
                if conflict == 'missing_product_act':
                    summary.append({'act_id': 52, 'type': 'DEFECT', 'act_state': 'ACCEPTED'})
                else:
                    summary[0]['type'] = 'DEFECT'
                provider = SellerFixture(data)
                try:
                    out = ozon_expansion.read('ozon_supply_items', {'bundle_id': 'B',
                        'order_id': '42', 'supply_id': '77'}, provider)
                except ValueError:
                    continue
                self.assertFalse(out.get('complete'))

    def test_supply_act_invalid_fact_quantity_is_not_a_valid_count(self):
        for value in (True, 1.5, -1):
            with self.subTest(value=value):
                data = one_supply_wire(act_item={'sku_info': {'sku': 101, 'offer_id': 'A'},
                    'declared_quantity': 10, 'fact_quantity': value, 'approved_quantity': 7})
                provider = SellerFixture(data)
                try:
                    out = ozon_expansion.read('ozon_supply_items', {'bundle_id': 'B',
                        'order_id': '42', 'supply_id': '77'}, provider)
                except ValueError:
                    continue
                self.assertFalse(out.get('complete'))

    def test_repeated_act_identity_is_not_silently_complete(self):
        item = {'sku_info': {'sku': 101, 'offer_id': 'A'}, 'declared_quantity': 10,
                'fact_quantity': 8, 'approved_quantity': 7}
        act = {'act_id': 51, 'type': 'ACCEPTANCE', 'items': [item]}
        provider = SellerFixture(one_supply_wire(act_item=item, acts=[act, copy.deepcopy(act)]))
        try:
            result = ozon_expansion.read('ozon_supply_items', {'bundle_id': 'B', 'order_id': '42',
                                                               'supply_id': '77'}, provider)
        except ValueError:
            return
        self.assertFalse(result.get('complete'))

    def test_cash_flow_keeps_reporting_period_and_currency_with_page_continuation(self):
        path = '/v1/finance/cash-flow-statement/list'
        data = {'result': {'cash_flows': [{'period': {'begin': '2026-10-01T00:00:00Z',
                   'end': '2026-10-15T23:59:59Z', 'id': 9}, 'orders_amount': 100,
                   'returns_amount': -15, 'commission_amount': -7, 'services_amount': -3,
                   'item_delivery_and_return_amount': -2, 'currency_code': 'RUB'}], 'page_count': 2}}
        out, provider = self.read('ozon_cash_flow', {'date_from': '2026-10-01',
                                                     'date_to': '2026-10-31', 'page': 1}, {path: data})
        body = provider.body_for(path)
        self.assertEqual(body['page'], 1)
        self.assertFalse(out['complete'])
        self.assertEqual(str(out['next_cursor']), '2')
        row, = out['items']
        self.assertEqual(row['orders_amount'], 100)
        self.assertEqual(row['returns_amount'], -15)
        self.assertEqual(row['currency_code'], 'RUB')
        self.assertIn('2026-10-01', json.dumps(row))
        self.assertIn('2026-10-15', json.dumps(row))

    def test_cash_flow_missing_amount_or_currency_does_not_claim_a_complete_payout(self):
        path = '/v1/finance/cash-flow-statement/list'
        period = {'id': 1, 'begin': '2026-10-01T00:00:00Z', 'end': '2026-10-15T23:59:59Z'}
        data = {'result': {'cash_flows': [{'period': period, 'currency_code': 'RUB'}], 'page_count': 1,
                           'details': {'period': period, 'payments': [{'payment': 100, 'currency_code': 'RUB'},
                                                    {'payment': None, 'currency_code': 'RUB'},
                                                    {'payment': 5, 'currency_code': None}]}}}
        out, _ = self.read('ozon_cash_flow', {'date_from': '2026-10-01', 'date_to': '2026-10-15'}, {path: data})
        self.assertFalse(out['complete'])
        self.assertTrue(out.get('issues') or out.get('payout_completeness') in ('incomplete', 'not_verified'))

    def test_cash_flow_does_not_silently_round_a_small_payment(self):
        path = '/v1/finance/cash-flow-statement/list'
        period = {'id': 1, 'begin': '2026-10-01T00:00:00Z', 'end': '2026-10-15T23:59:59Z'}
        data = {'result': {'cash_flows': [{'period': period, 'currency_code': 'RUB'}], 'page_count': 1,
                           'details': {'period': period, 'payments': [
            {'payment': 10000000000000000000000000000, 'currency_code': 'RUB'},
            {'payment': 0.01, 'currency_code': 'RUB'}]}}}
        provider = SellerFixture({path: data})
        try:
            out = ozon_expansion.read('ozon_cash_flow', {'date_from': '2026-10-01',
                                                          'date_to': '2026-10-15'}, provider)
        except ValueError:
            return  # A documented, explicit safe numeric limit is also acceptable.
        actual = out['payout_by_currency']['RUB']
        self.assertEqual(Decimal(actual), Decimal('10000000000000000000000000000.01'))

    def test_cash_flow_missing_detail_period_is_not_a_complete_period_payout(self):
        path = '/v1/finance/cash-flow-statement/list'
        period = {'id': 1, 'begin': '2026-10-01T00:00:00Z', 'end': '2026-10-15T23:59:59Z'}
        data = {'result': {'cash_flows': [{'period': period, 'currency_code': 'RUB'}], 'page_count': 1,
                           'details': {'payments': [{'payment': 100, 'currency_code': 'RUB'}]}}}
        out, _ = self.read('ozon_cash_flow', {'date_from': '2026-10-01', 'date_to': '2026-10-15'}, {path: data})
        self.assertFalse(out['complete'])

    def test_cash_flow_equal_payment_projections_are_not_claimed_unique(self):
        path = '/v1/finance/cash-flow-statement/list'
        period = {'id': 1, 'begin': '2026-10-01T00:00:00Z', 'end': '2026-10-15T23:59:59Z'}
        data = {'result': {'cash_flows': [{'period': period, 'currency_code': 'RUB'}], 'page_count': 1,
                           'details': {'period': period, 'payments': [
                               {'payment': 100, 'currency_code': 'RUB'},
                               {'payment': 100, 'currency_code': 'RUB'},
                               {'payment': 25, 'currency_code': 'RUB'}]}}}
        out, _ = self.read('ozon_cash_flow', {'date_from': '2026-10-01', 'date_to': '2026-10-15'}, {path: data})
        uncertainty = (out.get('payment_identity') in ('not_verified', 'ambiguous') or
                       out.get('payout_additivity') in ('not_verified', 'ambiguous') or
                       bool(out.get('issues')))
        self.assertTrue(not out['complete'] or uncertainty)

    def test_cash_summary_deduplicates_repeated_pages_and_withholds_gaps_or_conflicts(self):
        path = '/v1/finance/cash-flow-statement/list'
        def response(day, amount, page_count=3):
            period = {'begin': f'2026-10-{day:02d}T00:00:00Z', 'end': f'2026-10-{day:02d}T23:59:59Z'}
            return {'result': {'page_count': page_count, 'cash_flows': [{'period': period, 'currency_code': 'RUB',
                'orders_amount': amount}], 'details': {'period': period,
                'payments': [{'payment': amount, 'currency_code': 'RUB'}]}}}
        args = {'date_from': '2026-10-01', 'date_to': '2026-10-02', 'mode': 'summary'}
        for kind in ('repeat', 'conflict', 'gap'):
            with self.subTest(kind=kind):
                pages = {1: response(1, 100), 2: response(1, 100), 3: response(2, 25)}
                if kind == 'conflict': pages[2] = response(1, 101)
                if kind == 'gap': pages[3] = response(3, 25)
                provider = SellerFixture({path: lambda body: pages[body['page']]})
                out = ozon_expansion.read('ozon_cash_flow', args, provider)
                self.assertEqual([call['body']['page'] for call in provider.calls], [1, 2, 3])
                if kind == 'repeat':
                    self.assertTrue(out['complete'], out)
                    self.assertEqual(Decimal(out['payout_by_currency']['RUB']), Decimal('125'))
                    self.assertEqual(len(out['statements']), 2)
                else:
                    self.assertFalse(out['complete'], out)
                    self.assertEqual(out['payout_by_currency'], {})

    def test_cash_summary_accumulates_full_period_without_rounding(self):
        path = '/v1/finance/cash-flow-statement/list'
        def response(body):
            day = body['page']
            amount = '10000000000000000000000000000' if day == 1 else '0.01'
            period = {'begin': f'2026-10-{day:02d}T00:00:00Z', 'end': f'2026-10-{day:02d}T23:59:59Z'}
            return {'result': {'page_count': 2, 'cash_flows': [], 'details': {'period': period,
                    'payments': [{'payment': amount, 'currency_code': 'RUB'}]}}}
        provider = SellerFixture({path: response})
        try:
            out = ozon_expansion.read('ozon_cash_flow', {'date_from': '2026-10-01',
                'date_to': '2026-10-02', 'mode': 'summary'}, provider)
        except ValueError:
            return  # Explicit numerical bounds are preferable to silent rounding.
        self.assertTrue(out['complete'], out)
        self.assertEqual(Decimal(out['payout_by_currency']['RUB']), Decimal('10000000000000000000000000000.01'))

    def test_cash_summary_does_not_trust_a_shrinking_provider_page_count(self):
        path = '/v1/finance/cash-flow-statement/list'
        def response(body):
            day = body['page']
            period = {'begin': f'2026-10-{day:02d}T00:00:00Z', 'end': f'2026-10-{day:02d}T23:59:59Z'}
            return {'result': {'page_count': 3 if day == 1 else 2, 'cash_flows': [],
                'details': {'period': period, 'payments': [{'payment': day, 'currency_code': 'RUB'}]}}}
        provider = SellerFixture({path: response})
        try:
            out = ozon_expansion.read('ozon_cash_flow', {'date_from': '2026-10-01',
                'date_to': '2026-10-02', 'mode': 'summary'}, provider)
        except ValueError:
            return
        self.assertFalse(out['complete'], out)
        self.assertEqual(out['payout_by_currency'], {})

    def test_cash_summary_resumes_after_rate_limit_without_double_counting(self):
        path = '/v1/finance/cash-flow-statement/list'
        class Resumable(SellerFixture):
            def __init__(self):
                self.saved, self.once = {}, False
                super().__init__({path: self.page})
            @contextmanager
            def progress(self, name, query):
                def save(state, _ttl): self.saved = copy.deepcopy(state)
                yield copy.deepcopy(self.saved), save
            def page(self, body):
                day = body['page']
                if day == 2 and not self.once:
                    self.once = True
                    raise urllib.error.HTTPError('', 429, 'synthetic', {'Retry-After': '10'}, None)
                period = {'begin': f'2026-10-{day:02d}T00:00:00Z', 'end': f'2026-10-{day:02d}T23:59:59Z'}
                return {'result': {'page_count': 2, 'cash_flows': [], 'details': {'period': period,
                    'payments': [{'payment': day, 'currency_code': 'RUB'}]}}}
        wire = Resumable()
        args = {'date_from': '2026-10-01', 'date_to': '2026-10-02', 'mode': 'summary'}
        pending = ozon_expansion.read('ozon_cash_flow', args, wire)
        self.assertFalse(pending['complete'])
        self.assertEqual(pending['payout_by_currency'], {})
        final = ozon_expansion.read('ozon_cash_flow', args, wire)
        self.assertTrue(final['complete'], final)
        self.assertEqual(Decimal(final['payout_by_currency']['RUB']), Decimal(3))
        self.assertEqual([x['body']['page'] for x in wire.calls], [1, 2, 2])

    def test_fbo_geo_uses_analytics_without_exposing_legal_info(self):
        path = '/v3/posting/fbo/list'
        data = {'cursor': 'next-posting', 'has_next': True, 'postings': [
            {'posting_number': 'P-1', 'status': 'delivered',
             'analytics_data': {'warehouse_id': 501, 'warehouse_name': 'Warehouse A', 'city': None},
             'financial_data': {'cluster_from': 'FROM', 'cluster_to': 'TO'},
             'products': [{'sku': int(SKU), 'offer_id': ARTICLE, 'quantity': 2}],
             'legal_info': {'inn': 'SYNTHETIC_PRIVATE_INN'}}]}
        out, provider = self.read('ozon_fbo_postings_geo', {'date_from': '2026-10-01',
                                                            'date_to': '2026-10-03'}, {path: data})
        body = provider.body_for(path)
        self.assertTrue(body['with']['analytics_data'])
        self.assertTrue(body['with']['financial_data'])
        self.assertFalse(body['with'].get('legal_info', False))
        self.assertFalse(out['complete'])
        self.assertEqual(out['next_cursor'], 'next-posting')
        self.assertNotIn('SYNTHETIC_PRIVATE_INN', json.dumps(out))
        self.assertIn('P-1', json.dumps(out))
        self.assertIn('FROM', json.dumps(out))
        self.assertIn('TO', json.dumps(out))

    def test_clusters_preserve_cluster_and_warehouse_identity(self):
        # v1 exposes both the cluster and macrolocal IDs as well as warehouses.
        path = '/v1/cluster/list'
        data = {'clusters': [{'id': 30, 'macrolocal_cluster_id': 31, 'name': 'Cluster A',
                              'type': 'CLUSTER_TYPE_OZON', 'logistic_clusters': [
                                  {'warehouses': [{'warehouse_id': 501, 'name': 'Warehouse A'},
                                                  {'warehouse_id': 502, 'name': 'Warehouse B'}]}]}]}
        out, _ = self.read('ozon_clusters', {}, {path: data})
        encoded = json.dumps(out, ensure_ascii=False)
        for expected in ('31', '501', '502', 'Cluster A', 'Warehouse A', 'Warehouse B'):
            self.assertIn(expected, encoded)

    def test_reviews_use_read_only_v2_and_keep_pagination_and_sku(self):
        path = '/v2/review/list'
        data = {'has_next': True, 'last_id': 'review-next',
                'reviews': [{'id': 'review-1', 'sku': int(SKU), 'text': 'Synthetic review',
                             'published_at': '2026-10-03T10:00:00Z', 'rating': 4,
                             'status': 'NEW', 'order_status': 'DELIVERED'}]}
        out, provider = self.read('ozon_reviews', {'sku': SKU, 'limit': 20,
                                                    'date_from': '2026-10-01',
                                                    'date_to': '2026-10-03'}, {path: data})
        body = provider.body_for(path)
        self.assertEqual([str(x) for x in body['filters']['skus']], [SKU])
        # Ozon describes published_to as a strict "before" boundary. Advancing
        # to the next midnight includes fractional timestamps in the final second.
        self.assertEqual(body['filters']['published_to'], '2026-10-04T00:00:00Z')
        self.assertFalse(out['complete'])
        self.assertEqual(out['next_cursor'], 'review-next')
        item, = out['items']
        self.assertEqual(str(item['sku']), SKU)
        self.assertEqual(item['rating'], 4)
        self.assertEqual(item['text'], 'Synthetic review')

    def test_reviews_filter_dates_in_utc_not_the_timestamp_local_calendar_day(self):
        path = '/v2/review/list'
        data = {'has_next': False, 'last_id': '', 'reviews': [
            {'id': 'review-offset', 'sku': int(SKU), 'text': 'Synthetic review',
             'published_at': '2026-10-04T01:00:00+03:00', 'rating': 4, 'status': 'NEW'}]}
        out, _ = self.read('ozon_reviews', {'sku': SKU, 'limit': 20,
                                             'date_from': '2026-10-03', 'date_to': '2026-10-03'}, {path: data})
        item, = out['items']
        self.assertEqual(item['id'], 'review-offset')

    def test_reviews_with_missing_publication_time_do_not_imply_complete_period(self):
        path = '/v2/review/list'
        data = {'has_next': False, 'last_id': '', 'reviews': [
            {'id': 'review-undated', 'sku': int(SKU), 'text': 'Synthetic review',
             'published_at': None, 'rating': 4, 'status': 'NEW'}]}
        provider = SellerFixture({path: data})
        try:
            out = ozon_expansion.read('ozon_reviews', {'sku': SKU, 'limit': 20,
                'date_from': '2026-10-03', 'date_to': '2026-10-03'}, provider)
        except ValueError:
            return
        self.assertFalse(out.get('complete'))

    def test_rating_is_seller_rating_and_keeps_values_without_guessing(self):
        path = '/v1/rating/summary'
        data = {'groups': [{'group_name': 'Delivery', 'items': [{'rating': 'on_time',
                   'name': 'On time', 'current_value': 97.5, 'past_value': 98.0,
                   'status': 'OK', 'value_type': 'PERCENT'}]}], 'premium': False}
        out, _ = self.read('ozon_rating', {}, {path: data})
        encoded = json.dumps(out, ensure_ascii=False)
        self.assertIn('97.5', encoded)
        self.assertIn('on_time', encoded)
        self.assertIn('PERCENT', encoded)


if __name__ == '__main__':
    unittest.main()
