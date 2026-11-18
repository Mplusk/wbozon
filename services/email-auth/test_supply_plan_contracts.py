"""Synthetic warehouse planning contracts with a closed, in-memory Seller/WB API."""
import copy
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import supply_plan


UTC = timezone.utc
MOSCOW = timezone(timedelta(hours=3))
AS_OF = date(2026, 4, 5)
MAPPING = {'ozon_offer_id': 'OZ-004', 'wb_vendor_code': 'WB-0007', 'wb_nm_id': 497}
ARGS = {'article': 'тестовая-подставка', 'horizon_days': 21}
OZON = 'https://api-seller.ozon.ru'
WB_REPORT = 'https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains'
WB_ORDERS = 'https://statistics-api.wildberries.ru/api/v1/supplier/orders'
TASK_ID = '61ce29ef-f747-44e7-9a9c-5c984b6f8cda'
WB_TASK = WB_REPORT + '/tasks/' + TASK_ID


class FixtureWire:
    """Records requests and stores continuations without any network or credentials."""

    def __init__(self, exchanges):
        self.exchanges = list(exchanges)
        self.calls = []
        self.checkpoints = {}

    def __call__(self, method, url, *, body=None, query=None, **kwargs):
        if not self.exchanges:
            raise AssertionError('Undeclared provider request: ' + url)
        expected_method, expected_url, response = self.exchanges.pop(0)
        if (method, url) != (expected_method, expected_url):
            raise AssertionError(f'Expected {expected_method} {expected_url}, got {method} {url}')
        self.calls.append((url, body, query))
        return copy.deepcopy(response)

    @contextmanager
    def progress(self, name, key, **kwargs):
        index = (name, tuple(key))

        def save(value, ttl):
            self.checkpoints[index] = copy.deepcopy(value)

        yield copy.deepcopy(self.checkpoints.get(index, {})), save


def ozon_wire():
    when = (datetime.now(UTC).date() - timedelta(days=1)).isoformat() + 'T14:00:00Z'
    article = MAPPING['ozon_offer_id']
    card = {'id': 321, 'name': 'Тестовый вариант', 'offer_id': article, 'sku': 101,
            'sources': [{'sku': 101, 'source': 'fbo'}, {'sku': 102, 'source': 'fbo'}]}

    def stock(sku, warehouse, available, inbound):
        return {'offer_id': article, 'sku': sku, 'warehouse_id': warehouse,
                'warehouse_name': f'Условный склад {warehouse}', 'cluster_id': 52,
                'cluster_name': 'Тестовый кластер', 'available_stock_count': available,
                'transit_stock_count': inbound, 'inbound_replenishment': 17,
                'return_from_customer_stock_count': 30}

    def product(sku, quantity, offer=article):
        return {'sku': sku, 'quantity': quantity, 'offer_id': offer}

    def posting(number, warehouse, products):
        return {'posting_number': number, 'created_at': when, 'status': 'delivered',
                'analytics_data': {'warehouse_id': warehouse, 'warehouse_name': f'Условный склад {warehouse}'},
                'products': products}

    postings = [posting('A-001', 711, [product(101, 3), product(102, 6), product(900, 700, 'OZ-04')]),
                posting('A-002', 712, [product(101, 12)])]
    return FixtureWire([
        ('POST', OZON + '/v3/product/info/list', {'items': [card]}),
        ('POST', OZON + '/v1/analytics/stocks', {'items': [
            stock(101, 711, 2, 1), stock(102, 711, 2, 2), stock(101, 712, 7, 1)]}),
        ('POST', OZON + '/v3/posting/fbo/list', {'has_next': False, 'postings': postings}),
    ])


def wb_orders():
    day = (datetime.now(MOSCOW).date() - timedelta(days=1)).isoformat()

    def event(index, article='WB-0007', nm_id=497):
        return {'date': day + 'T03:00:00+03:00', 'lastChangeDate': day + f'T05:00:{index:02d}',
                'srid': f'B-{index}', 'supplierArticle': article, 'nmId': nm_id, 'barcode': '0000310',
                'warehouseName': 'Условный склад Б', 'warehouseType': 'Склад WB', 'isCancel': index == 2}

    events = [event(i) for i in range(8)]
    # WB may use the same warehouseName on a seller-fulfilled order as on its own warehouse.
    seller_fulfilled = dict(event(10), warehouseType='Склад продавца')
    return [('GET', WB_ORDERS, events + [event(7), event(8, 'WB-007'), event(9, nm_id=498), seller_fulfilled]),
            ('GET', WB_ORDERS, [])]


def wb_stock_download(task_id=TASK_ID):
    def row(warehouse, quantity):
        return {'warehouseName': warehouse, 'quantity': quantity}

    warehouses = [row('Условный склад Б', 3), row('Всего находится на складах', 3),
                  row('В пути до получателей', 42), row('В пути возвраты на склад WB', 9)]
    task = WB_REPORT + '/tasks/' + task_id
    return [('GET', task + '/status', {'data': {'id': task_id, 'status': 'done'}}),
            ('GET', task + '/download', [
                {'vendorCode': 'WB-0007', 'nmId': 497, 'barcode': '0000310', 'warehouses': warehouses},
                {'vendorCode': 'WB-007', 'nmId': 497, 'barcode': '0000311',
                 'warehouses': [row('Условный склад Б', 99)]},
                {'vendorCode': 'WB-0007', 'nmId': 498, 'barcode': '0000312',
                 'warehouses': [row('Условный склад Б', 98)]}])]


def calculation(daily, stock, *, stock_done=True, orders_done=True):
    dates = {(AS_OF - timedelta(days=days_ago)).isoformat(): units for days_ago, units in daily.items()}
    return {'locations': {'77': {'warehouse': 'Тестовый склад', 'warehouse_id': '77',
                                  'cluster': 'Тестовый кластер', 'cluster_id': 9, 'daily': dates,
                                  'days_with_cancellations': {}, 'stock': stock}},
            'done': {'stock': stock_done, 'orders': orders_done}, 'issues': set()}


class SupplyPlanContracts(unittest.TestCase):
    def test_exact_windows_include_zero_days_and_ceiling_before_subtracting_units(self):
        # 14/7, 24/30 and 45/90; a day outside the 90-day window carries 500 irrelevant units.
        state = calculation({0: 12, 6: 2, 7: 3, 29: 7, 30: 8, 89: 13, 90: 500},
                            {'101': {'available': 5, 'inbound': 3}})
        row, = supply_plan._locations(state, 21, AS_OF)
        speeds = {k: Decimal(v) for k, v in row['orders_per_day'].items()}
        self.assertEqual(speeds, {'7': Decimal(2), '30': Decimal('0.8'), '90': Decimal('0.5')})
        self.assertEqual(Decimal(row['days_of_cover']), Decimal('6.25'))
        self.assertEqual((row['shortfall_before_inbound'], row['recommended_quantity']), (12, 9))

    def test_integer_demand_never_rounds_up_a_second_time(self):
        state = calculation({0: 5}, {'101': {'available': 0, 'inbound': 0}})
        for horizon, expected in ((18, 3), (24, 4), (30, 5), (6, 1)):
            with self.subTest(horizon=horizon):
                row, = supply_plan._locations(state, horizon, AS_OF)
                self.assertEqual(row['recommended_quantity'], expected)

    def test_recommendations_distinguish_arrival_from_unconfirmed_inbound(self):
        # Five orders in 30 days require four more pieces beyond the one already held.
        for inbound, if_arrives, displayed, confirmed in ((2, 2, 2, None), (6, 0, 0, None),
                                                           (0, 4, 4, 4), (None, None, 4, None)):
            with self.subTest(inbound=inbound):
                state = calculation({0: 5}, {'101': {'available': 1, 'inbound': inbound}})
                row, = supply_plan._locations(state, 30, AS_OF)
                self.assertEqual(row['recommendation_without_inbound'], 4)
                self.assertEqual(row['recommendation_if_inbound_arrives'], if_arrives)
                self.assertEqual(row['recommended_quantity'], displayed)
                self.assertEqual(row['confirmed_recommended_quantity'], confirmed)

    def test_unknown_availability_or_incomplete_history_withholds_recommendation(self):
        cases = [calculation({0: 15}, {'101': {'available': None, 'inbound': 0}}),
                 calculation({0: 15}, {'101': {'available': 0, 'inbound': 8}}, stock_done=False),
                 calculation({0: 15}, {'101': {'available': 0, 'inbound': 8}}, orders_done=False)]
        for state in cases:
            with self.subTest(state=state):
                row, = supply_plan._locations(state, 21, AS_OF)
                self.assertIsNone(row['recommended_quantity'])
                self.assertIsNone(row['days_of_cover'])
        zero_demand = calculation({}, {'101': {'available': 7, 'inbound': None}})
        row, = supply_plan._locations(zero_demand, 21, AS_OF)
        self.assertEqual(row['recommended_quantity'], 0)
        self.assertIsNone(row['days_of_cover'])

    def test_raw_marketplaces_use_exact_mapped_articles_and_distinguish_transit(self):
        ozon = ozon_wire()
        wb = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}})]
                         + wb_stock_download() + wb_orders())
        result = supply_plan.read(ARGS, {'wb': wb, 'ozon': ozon}, MAPPING)
        self.assertEqual((wb.exchanges, ozon.exchanges), ([], []))
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['quantity_unit'], 'pcs')
        platforms = {r['marketplace']: r for r in result['items']}
        self.assertEqual(platforms['ozon']['article'], 'OZ-004')
        self.assertEqual(platforms['wb']['article'], 'WB-0007')
        self.assertEqual(platforms['wb']['excluded_seller_warehouse_units'], 1)
        self.assertEqual(ozon.calls[0][1]['offer_id'], ['OZ-004'])
        self.assertEqual(set(ozon.calls[1][1]['skus']), {101, 102})
        self.assertEqual(platforms['wb']['orders_through'],
                         (datetime.now(MOSCOW).date() - timedelta(days=1)).isoformat())
        self.assertEqual(platforms['ozon']['orders_through'],
                         (datetime.now(UTC).date() - timedelta(days=1)).isoformat())
        warehouses = {str(row['warehouse_id']): row for row in platforms['ozon']['locations']}
        self.assertEqual(set(warehouses), {'711', '712'})
        self.assertEqual(warehouses['711']['available_units'], 4)
        self.assertEqual(warehouses['711']['inbound_units'], 3)
        self.assertEqual(warehouses['711']['transfer_units'], 34)
        self.assertEqual(warehouses['711']['customer_return_units'], 60)
        self.assertEqual(Decimal(warehouses['711']['orders_per_day']['30']), Decimal('0.3'))
        self.assertEqual(warehouses['712']['recommended_quantity'], 1)
        wb_row, = platforms['wb']['locations']
        self.assertEqual(wb_row['available_units'], 3)
        self.assertIsNone(wb_row['inbound_units'])
        self.assertEqual(Decimal(wb_row['orders_per_day']['30']), Decimal(8) / 30)
        self.assertEqual(wb_row['recommended_quantity'], 3)
        transit = {row['kind']: row['quantity'] for row in platforms['wb']['reported_transit']}
        self.assertEqual(transit, {'in_transit_to_customer': 42, 'returning_from_customer': 9})

    def test_cluster_rounds_its_total_demand_once(self):
        # Both warehouses share 21 ordered pcs/30 d. Over 21 d: ceil(14.7) - 11 - 4 = 0.
        # Warehouse 712 on its own needs 1 pc. Adding clipped warehouse deficits gives the wrong answer.
        result = supply_plan.read(ARGS, {'ozon': ozon_wire()}, MAPPING)
        ozon, = (p for p in result['items'] if p['marketplace'] == 'ozon')
        cluster, = ozon['clusters']
        self.assertEqual(str(cluster['cluster_id']), '52')
        self.assertEqual((cluster['available_units'], cluster['inbound_units']), (11, 4))
        self.assertEqual(Decimal(cluster['orders_per_day']['30']), Decimal('0.7'))
        self.assertEqual((cluster['shortfall_before_inbound'], cluster['recommended_quantity']), (4, 0))

    def test_shipping_to_two_clusters_is_distinct_from_pooling_warehouse_stock(self):
        wire = ozon_wire()
        stock = wire.exchanges[1][2]['items']
        for position in stock:
            if position['warehouse_id'] == 712:
                position.update(cluster_id=53, cluster_name='Второй тестовый кластер')
        report = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)
        ozon, = (p for p in report['items'] if p['marketplace'] == 'ozon')
        total = ozon['marketplace_total']
        self.assertEqual(total['destination_basis'], 'clusters')
        self.assertEqual((total['recommendation_without_inbound'], total['recommendation_if_inbound_arrives']),
                         (4, 0))
        self.assertEqual((total['ship_without_transfers_without_inbound'],
                          total['ship_without_transfers_if_inbound_arrives']), (5, 1))
        results = {str(c['cluster_id']): c for c in ozon['clusters']}
        self.assertEqual(results['52']['recommendation_if_inbound_arrives'], 0)
        self.assertEqual(results['53']['recommendation_if_inbound_arrives'], 1)

    def test_full_fulfillment_plan_remains_partial_account_coverage_and_exposes_cancellations(self):
        ozon = ozon_wire()
        ozon.exchanges[-1][2]['postings'][0]['status'] = 'cancelled'
        wb = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}})]
                         + wb_stock_download() + wb_orders())
        report = supply_plan.read(ARGS, {'ozon': ozon, 'wb': wb}, MAPPING)
        self.assertTrue(report['complete'])
        self.assertEqual(report['complete_scope'], 'marketplace_warehouse_plan_only')
        self.assertEqual(report['account_coverage'], 'partial')
        platforms = {p['marketplace']: p for p in report['items']}
        for key, segment, gross, cancelled in (('ozon', 'Ozon FBO', 21, 9), ('wb', 'Wildberries FBW', 8, 1)):
            with self.subTest(platform=key):
                p = platforms[key]
                self.assertTrue(p['complete'])
                self.assertEqual(p['complete_scope'], report['complete_scope'])
                self.assertEqual(p['fulfillment_scope'], segment)
                self.assertEqual(p['account_coverage']['status'], 'partial')
                self.assertTrue(p['account_coverage']['excluded'])
                for total in [p['marketplace_total'], *p['clusters']]:
                    for days in ('7', '30', '90'):
                        self.assertEqual(total['ordered_units'][days], gross)
                        self.assertEqual(total['cancelled_units'][days], cancelled)
                        self.assertEqual(total['ordered_units_after_known_cancellations'][days], gross - cancelled)
                    self.assertEqual(Decimal(total['orders_per_day']['30']), Decimal(gross) / 30)
        self.assertEqual(platforms['wb']['excluded_seller_warehouse_units'], 1)

    def test_missing_cancellation_status_only_affects_enclosing_windows(self):
        wire = ozon_wire()
        orders = wire.exchanges[-1][2]['postings']
        day = datetime.now(UTC).date() - timedelta(days=61)
        orders.append({'posting_number': 'A-003', 'created_at': day.isoformat() + 'T12:00:00Z',
                       'analytics_data': {'warehouse_id': 711, 'warehouse_name': 'Условный склад 711'},
                       'products': [{'offer_id': MAPPING['ozon_offer_id'], 'sku': 101, 'quantity': 4}]})
        report = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)
        platform, = (p for p in report['items'] if p['marketplace'] == 'ozon')
        for row in [platform['marketplace_total'], *platform['clusters']]:
            self.assertEqual(row['ordered_units'], {'7': 21, '30': 21, '90': 25})
            self.assertEqual(row['cancelled_units'], {'7': 0, '30': 0, '90': None})
            self.assertEqual(row['ordered_units_after_known_cancellations'], {'7': 21, '30': 21, '90': None})

    def test_conflicting_order_revision_does_not_publish_the_first_version_as_final_counts(self):
        wire = ozon_wire()
        orders = wire.exchanges[-1][2]['postings']
        revision = copy.deepcopy(orders[0])
        revision['status'] = 'cancelled'
        revision['products'][0]['quantity'] = 4
        orders.append(revision)
        report = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)
        platform, = (p for p in report['items'] if p['marketplace'] == 'ozon')
        self.assertFalse(platform['complete'])
        self.assertIn('inconsistent_order_revision', platform['issues'])
        for row in [platform['marketplace_total'], *platform['clusters']]:
            for field in ('ordered_units', 'cancelled_units', 'ordered_units_after_known_cancellations'):
                self.assertIsNone((row.get(field) or {}).get('30'))
            self.assertIsNone(row['recommended_quantity'])

    def test_missing_warehouse_cluster_cannot_look_like_a_complete_cluster_plan(self):
        wire = ozon_wire()
        stock = wire.exchanges[1][2]['items']
        stock[:] = [s for s in stock if s['warehouse_id'] != 712]
        result = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)
        ozon, = (p for p in result['items'] if p['marketplace'] == 'ozon')
        self.assertFalse(ozon.get('cluster_coverage_complete', ozon['complete']))
        self.assertIsNone(ozon['marketplace_total']['ship_without_transfers_without_inbound'])
        self.assertIsNone(ozon['marketplace_total']['ship_without_transfers_if_inbound_arrives'])
        unknown, = (r for r in ozon['locations'] if str(r['warehouse_id']) == '712')
        self.assertIsNone(unknown['recommended_quantity'])
        self.assertEqual(unknown['ordered_units']['30'], 12)

    def test_no_warehouse_observation_does_not_confirm_a_zero_plan(self):
        wb = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}}),
                          ('GET', WB_TASK + '/status', {'data': {'id': TASK_ID, 'status': 'done'}}),
                          ('GET', WB_TASK + '/download', []), ('GET', WB_ORDERS, [])])
        platform = supply_plan.read(ARGS, {'wb': wb}, MAPPING)['items'][1]
        self.assertTrue(all(platform['source_complete'].values()))
        self.assertFalse(platform['complete'])
        self.assertFalse(platform['recommendations_complete'])
        self.assertIn('no_warehouse_stock_observed', platform['issues'])
        self.assertIsNone(platform['marketplace_total']['available_units'])
        self.assertIsNone(platform['marketplace_total']['recommended_quantity'])

    def test_conflicting_cluster_assignments_never_use_the_last_row_as_authoritative(self):
        for reverse in (False, True):
            wire = ozon_wire()
            stock = wire.exchanges[1][2]['items']
            stock[1]['cluster_id'] = 53
            if reverse:
                stock.reverse()
            platform = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)['items'][0]
            self.assertFalse(platform['cluster_coverage_complete'])
            self.assertIn('711', platform['unassigned_warehouse_ids'])
            warehouse = next(r for r in platform['locations'] if r['warehouse_id'] == '711')
            self.assertIsNone(warehouse['cluster_id'])
            self.assertEqual(warehouse['available_units'], 4)
            for cluster in platform['clusters']:
                self.assertFalse(cluster['coverage_complete'])
                self.assertIsNone(cluster['recommended_quantity'])
            self.assertIsNone(platform['marketplace_total']['ship_without_transfers_without_inbound'])

    def test_fbo_missing_article_or_sku_cannot_silently_reduce_demand(self):
        for missing in ('offer_id', 'sku'):
            wire = ozon_wire()
            products = wire.exchanges[2][2]['postings'][0]['products']
            if missing == 'offer_id':
                wire.exchanges.append(copy.deepcopy(wire.exchanges[0]))
            for product in products[:2]:
                product[missing] = None
                product['quantity'] = 1
            platform = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)['items'][0]
            if platform['complete']:
                self.assertEqual(platform['marketplace_total']['ordered_units']['30'], 14)
            else:
                self.assertIsNone(platform['marketplace_total']['ordered_units'])

    def test_undated_wb_inbound_only_completes_the_no_inbound_scenario(self):
        wire = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}})]
                           + wb_stock_download() + wb_orders())
        platform = supply_plan.read(ARGS, {'wb': wire}, MAPPING)['items'][1]
        self.assertTrue(platform['scenario_recommendations_complete'])
        self.assertFalse(platform['recommendations_complete'])
        self.assertEqual(platform['statistics_status'], 'preliminary')
        self.assertIsNone(platform['marketplace_total']['confirmed_recommended_quantity'])
        empty_orders = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}})]
                                   + wb_stock_download() + [('GET', WB_ORDERS, [])])
        provisional = supply_plan.read(ARGS, {'wb': empty_orders}, MAPPING)['items'][1]
        self.assertEqual(provisional['marketplace_total']['recommendation_without_inbound'], 0)
        self.assertIsNone(provisional['marketplace_total']['confirmed_recommended_quantity'])

    def test_two_product_rows_with_the_same_sku_in_one_fbo_posting_are_not_silently_collapsed(self):
        wire = ozon_wire()
        products = wire.exchanges[-1][2]['postings'][0]['products']
        products.append(copy.deepcopy(products[0]))
        platform = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)['items'][0]
        self.assertFalse(platform['complete'])
        self.assertIn('duplicate_posting_sku', platform['pending'][0]['issues'])
        self.assertIsNone(platform['marketplace_total']['ordered_units'])
        self.assertIsNone(platform['unmapped_cluster_order_units_90d'])
        self.assertIsNone(platform['excluded_seller_warehouse_units'])

    def test_only_catalog_confirmed_fbs_skus_can_be_absent_from_an_fbo_plan(self):
        for scheme, complete in (('FBS', True), ('SDS', False), ('unrecognized', False)):
            with self.subTest(scheme=scheme):
                wire = ozon_wire()
                wire.exchanges[0][2]['items'][0]['sources'][1]['source'] = scheme
                stocks = wire.exchanges[1][2]['items']
                stocks[:] = [s for s in stocks if s['sku'] == 101]
                products = wire.exchanges[2][2]['postings'][0]['products']
                products[:] = [p for p in products if p['sku'] != 102]
                platform = supply_plan.read(ARGS, {'ozon': wire}, MAPPING)['items'][0]
                self.assertEqual(platform['complete'], complete)
                self.assertEqual(platform['marketplace_total']['ordered_units']['30'], 15)
                if complete:
                    self.assertEqual(platform['marketplace_total']['recommendation_without_inbound'], 2)

    def test_pending_wb_report_resumes_without_recounting_orders_or_recreating_task(self):
        wb = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}}),
                          ('GET', WB_TASK + '/status', {'data': {'id': TASK_ID, 'status': 'processing'}})]
                         + wb_orders())
        result = supply_plan.read(ARGS, {'wb': wb}, MAPPING)
        report, = (p for p in result['items'] if p['marketplace'] == 'wb')
        self.assertFalse(result['complete'])
        self.assertFalse(report['complete'])
        self.assertTrue(report['source_complete']['orders'])
        self.assertFalse(report['source_complete']['stock'])
        self.assertTrue(report['pending'])
        self.assertTrue(all(row['recommended_quantity'] is None for row in report['locations']))
        self.assertEqual(wb.exchanges, [])
        wb.exchanges.extend(wb_stock_download())
        resumed = supply_plan.read(ARGS, {'wb': wb}, MAPPING)
        report, = (p for p in resumed['items'] if p['marketplace'] == 'wb')
        self.assertTrue(report['complete'], report)
        row, = report['locations']
        self.assertEqual(row['recommended_quantity'], 3)
        self.assertEqual(wb.exchanges, [])

    def test_unknown_mapping_never_guesses_by_product_name(self):
        empty = FixtureWire([])
        report = supply_plan.read(ARGS, {'wb': empty, 'ozon': empty}, None)
        self.assertFalse(report['complete'])
        self.assertEqual(report['status'], 'mapping_required')
        self.assertEqual(empty.calls, [])

    def test_confirmed_purged_snapshot_can_be_regenerated_and_resumed(self):
        wb = FixtureWire([('GET', WB_REPORT, {'data': {'taskId': TASK_ID}}),
                          ('GET', WB_TASK + '/status', {'data': {'id': TASK_ID, 'status': 'processing'}})]
                         + wb_orders())
        first = supply_plan.read(ARGS, {'wb': wb}, MAPPING)
        self.assertFalse(first['complete'])
        self.assertEqual(wb.exchanges, [])
        fresh_id = '901d9464-48be-4656-b964-c50a4e19fbd3'
        wb.exchanges.extend([('GET', WB_TASK + '/status', {'data': {'id': TASK_ID, 'status': 'purged'}}),
                             ('GET', WB_REPORT, {'data': {'taskId': fresh_id}})]
                            + wb_stock_download(fresh_id))
        for _ in range(2):
            report = supply_plan.read(ARGS, {'wb': wb}, MAPPING)
            platform, = (p for p in report['items'] if p['marketplace'] == 'wb')
            if platform['complete']:
                break
        self.assertTrue(platform['complete'], platform)
        self.assertEqual(wb.exchanges, [])
        self.assertEqual(platform['locations'][0]['recommended_quantity'], 3)


if __name__ == '__main__':
    unittest.main()
