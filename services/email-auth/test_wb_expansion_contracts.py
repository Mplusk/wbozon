"""Offline contracts for the WB expansion, based on the official FBW, reports,
finance, analytics and feedback schemas retrieved on 18 November 2026.
"""
import io
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit

import market_transport
import wb_expansion


FBW = 'https://supplies-api.wildberries.ru'
STAT = 'https://statistics-api.wildberries.ru'
ANALYTICS = 'https://seller-analytics-api.wildberries.ru'
FINANCE = 'https://finance-api.wildberries.ru'
FEEDBACK = 'https://feedbacks-api.wildberries.ru'
ADVERT = 'https://advert-api.wildberries.ru'
SLOTS = 'https://common-api.wildberries.ru/api/tariffs/v1/acceptance/coefficients'
TASK_ID = '7d89a7d8-15cc-43a4-90ab-5bc37e41efac'
# Rolling dates also exercise the statistics provider's 90-day retention rule.
END = datetime.now(timezone(timedelta(hours=3))).date() - timedelta(days=1)
START = END - timedelta(days=6)
PERIOD = {'date_from': START.isoformat(), 'date_to': END.isoformat()}
ARTICLE = '0007'
BARCODE = '004700'
NM = 173


class Wire:
    """Only the declared, synthetic request/response exchanges are possible."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, query=None, body=None, **options):
        parts = urlsplit(url)
        endpoint = parts.scheme + '://' + parts.netloc + parts.path
        parameters = dict(parse_qsl(parts.query))
        parameters.update(query or {})
        self.calls.append((method, endpoint, parameters, body, options))
        if not self.responses:
            raise AssertionError('Unexpected extra WB request: ' + endpoint)
        expected_method, expected_endpoint, value = self.responses.pop(0)
        if (method, endpoint) != (expected_method, expected_endpoint):
            raise AssertionError(f'Expected {expected_method} {expected_endpoint}, got {method} {endpoint}')
        if isinstance(value, BaseException):
            raise value
        return value


def event(when, changed, **extra):
    return {'date': when, 'lastChangeDate': changed, 'warehouseName': 'Минск',
            'warehouseType': 'Склад WB', 'regionName': 'Москва', 'srid': 'order-1',
            'supplierArticle': ARTICLE, 'nmId': NM, 'barcode': BARCODE,
            'totalPrice': 200, 'finishedPrice': 180, 'priceWithDisc': 190, **extra}


def report_download(report, payload):
    base = ANALYTICS + '/api/v1/' + report
    task = base + '/tasks/' + TASK_ID
    return Wire(('GET', base, {'data': {'taskId': TASK_ID}}),
                ('GET', task + '/status', {'data': {'id': TASK_ID, 'status': 'done'}}),
                ('GET', task + '/download', payload))


class WBExpansionContracts(unittest.TestCase):
    def read(self, tool, arguments, wire):
        result = wb_expansion.read(tool, arguments, wire)
        self.assertFalse(wire.responses, 'The fixture contains an unperformed provider request')
        return result

    def read_pages(self, tool, arguments, wire):
        """Either internal traversal or a public continuation may deliver the rows."""
        pages = []
        cursor = None
        for _ in range(5):
            args = arguments if cursor is None else {**arguments, 'cursor': cursor}
            result = wb_expansion.read(tool, args, wire)
            pages.append(result)
            if result['complete']:
                self.assertFalse(wire.responses, 'Provider still has an unread page')
                return pages
            self.assertIsNotNone(result['next_cursor'])
            self.assertNotEqual(result['next_cursor'], cursor)
            cursor = result['next_cursor']
        self.fail('Continuation did not terminate')

    def test_supplies_list_uses_post_and_separate_actual_warehouse(self):
        listed = {'supplyID': 19, 'preorderID': 77, 'statusID': 5,
                  'createDate': PERIOD['date_from'] + 'T09:00:00+03:00',
                  'factDate': PERIOD['date_to'] + 'T12:00:00+03:00',
                  'phone': 'OMIT_SUPPLIER_PHONE'}
        detail = dict(listed, warehouseID=11, warehouseName='Плановый',
                      actualWarehouseID=12, actualWarehouseName='Фактический',
                      paidAcceptanceCoefficient=0, quantity=10, acceptedQuantity=8)
        wire = Wire(('POST', FBW + '/api/v1/supplies', [listed]),
                    ('GET', FBW + '/api/v1/supplies/19', detail))
        result = self.read('wb_supplies', {**PERIOD, 'limit': 2, 'mode': 'details'}, wire)
        first = wire.calls[0]
        self.assertEqual(int(first[2]['limit']), 2)
        dates, = first[3]['dates']
        self.assertEqual((dates['from'][:10], dates['till'][:10]),
                         (PERIOD['date_from'], PERIOD['date_to']))
        row, = result['items']
        self.assertEqual(str(row['supply_id']), '19')
        self.assertEqual(str(row['actual_warehouse_id']), '12')
        self.assertEqual(row['paid_acceptance_coefficient'], 0)
        self.assertNotIn('OMIT_SUPPLIER_PHONE', json.dumps(result, ensure_ascii=False))

    def test_supply_goods_retains_real_receipt_and_box_contents(self):
        goods = [{'vendorCode': ARTICLE, 'nmID': NM, 'barcode': BARCODE, 'quantity': 5,
                  'acceptedQuantity': 3, 'readyForSaleQuantity': 2, 'supplierBoxAmount': 5},
                 {'vendorCode': 'another', 'nmID': 174, 'barcode': '00031', 'quantity': 4,
                  'acceptedQuantity': None, 'readyForSaleQuantity': 0}]
        packages = [{'packageCode': 'box-1', 'packageId': '1', 'quantity': 9,
                     'barcodes': [{'barcode': BARCODE, 'quantity': 5}, {'barcode': '00031', 'quantity': 4}]}]
        wire = Wire(('GET', FBW + '/api/v1/supplies/19/goods', goods),
                    ('GET', FBW + '/api/v1/supplies/19/package', packages))
        result = self.read('wb_supply_goods', {'supply_id': 19, 'mode': 'details'}, wire)
        first, second = result['items']
        self.assertEqual((first['article'], str(first['nm_id']), first['barcode']), (ARTICLE, str(NM), BARCODE))
        self.assertEqual((first['quantity'], first['accepted_quantity'], first['ready_for_sale_quantity']), (5, 3, 2))
        self.assertIsNone(second['accepted_quantity'])
        self.assertEqual(second['ready_for_sale_quantity'], 0)
        self.assertFalse(result['complete'])
        self.assertIn('unreported_receipt_quantity', result['issues'])
        package, = result['packages']
        self.assertEqual(package['package_code'], 'box-1')
        self.assertEqual(package['barcodes'], packages[0]['barcodes'])

    def test_preorder_has_no_package_endpoint(self):
        wire = Wire(('GET', FBW + '/api/v1/supplies/77/goods', []))
        result = self.read('wb_supply_goods', {'supply_id': 77, 'is_preorder': True}, wire)
        self.assertEqual(str(wire.calls[0][2]['isPreorderID']).lower(), 'true')
        self.assertEqual(result['packages'], [])
        self.assertIn('preorder', result['package_scope'])

    def test_orders_progress_uses_the_raw_page_after_article_filter(self):
        endpoint = STAT + '/api/v1/supplier/orders'
        at = PERIOD['date_from'] + 'T09:17:25'
        changed = PERIOD['date_to'] + 'T23:59:59.123456'
        changed_again = PERIOD['date_to'] + 'T23:59:59.124500'
        other = event(at, changed, supplierArticle='7', isCancel=False)
        match = event(at, changed_again, srid='order-2', isCancel=True)
        wire = Wire(('GET', endpoint, [other]), ('GET', endpoint, [match]), ('GET', endpoint, []))
        args = {**PERIOD, 'article': ARTICLE, 'mode': 'details'}
        pages = self.read_pages('wb_orders', args, wire)
        self.assertEqual([call[2]['dateFrom'] for call in wire.calls[1:]], [changed, changed_again])
        row, = [row for page in pages for row in page['items']]
        self.assertEqual(row['article'], ARTICLE)
        self.assertTrue(row['cancelled'])
        self.assertEqual(wire.calls[0][4]['ttl'], 1800)
        self.assertGreaterEqual(wire.calls[0][4]['interval'], 60)

    def test_sales_and_returns_sharing_an_order_keep_distinct_events(self):
        when = PERIOD['date_to'] + 'T00:15:00'
        rows = [event(when, when, saleID='S100001', forPay=123),
                event(when, when, saleID='R100002', forPay=123)]
        endpoint = STAT + '/api/v1/supplier/sales'
        wire = Wire(('GET', endpoint, rows), ('GET', endpoint, []))
        pages = self.read_pages('wb_sales', {**PERIOD, 'article': ARTICLE, 'mode': 'details'}, wire)
        items = [row for page in pages for row in page['items']]
        self.assertEqual([row['sale_id'] for row in items], ['S100001', 'R100002'])
        self.assertEqual([row['event_type'] for row in items], ['sale', 'return'])
        for row in items:
            actual = datetime.fromisoformat(row['date'])
            expected = datetime.fromisoformat(when + '+03:00')
            self.assertEqual(actual, expected)
            self.assertIsNone(row['currency'], 'WB did not supply the seller currency')
            self.assertEqual(row['sku'], BARCODE)

    def test_statistics_calendar_is_moscow_even_for_an_explicit_utc_event(self):
        previous = START - timedelta(days=1)
        at = previous.isoformat() + 'T22:00:00Z'
        changed = PERIOD['date_from'] + 'T04:00:00'
        endpoint = STAT + '/api/v1/supplier/orders'
        wire = Wire(('GET', endpoint, [event(at, changed, isCancel=False)]), ('GET', endpoint, []))
        pages = self.read_pages('wb_orders', {**PERIOD, 'mode': 'details'}, wire)
        item, = [r for page in pages for r in page['items']]
        self.assertEqual(item['article'], ARTICLE)
        group, = [g for page in pages for g in page.get('summary', [])]
        self.assertEqual(group['date'], PERIOD['date_from'])

    def test_statistics_outside_retention_does_not_report_a_zero_sales_day(self):
        old = (END - timedelta(days=120)).isoformat()
        result = self.read('wb_orders', {'date_from': old, 'date_to': old}, Wire())
        self.assertFalse(result['complete'])
        self.assertIn('retention', result['status'])

    def test_current_stock_keeps_transit_and_provider_total_out_of_the_warehouse_sum(self):
        warehouses = [{'warehouseName': 'В пути до получателей', 'quantity': 14},
                      {'warehouseName': 'В пути возвраты на склад WB', 'quantity': 4},
                      {'warehouseName': 'Всего находится на складах', 'quantity': 267},
                      {'warehouseName': 'Склад WB РФ', 'quantity': 134},
                      {'warehouseName': 'Склад WB СГТ РФ', 'quantity': 10},
                      {'warehouseName': 'Минск', 'quantity': 123}]
        wire = report_download('warehouse_remains', [
            {'vendorCode': ARTICLE, 'nmId': NM, 'barcode': BARCODE, 'warehouses': warehouses}])
        result = self.read('wb_stocks', {'article': ARTICLE, 'mode': 'details'}, wire)
        flags = wire.calls[0][2]
        for name in ('groupBySa', 'groupByNm', 'groupByBarcode'):
            self.assertEqual(str(flags[name]).lower(), 'true')
        self.assertTrue(result['complete'])
        rows = result['items']
        quantities = {r['warehouse']: r['quantity'] for r in rows if r['kind'] == 'warehouse'}
        self.assertEqual(quantities, {'Склад WB РФ': 134, 'Склад WB СГТ РФ': 10, 'Минск': 123})
        self.assertEqual(sum(quantities.values()), 267)
        counts = {r['kind']: r['quantity'] for r in rows if r['kind'] != 'warehouse'}
        self.assertEqual(counts, {'provider_total': 267, 'in_transit_to_customer': 14,
                                  'returning_from_customer': 4})

    def test_pending_report_resumes_the_same_task_and_http_204_is_empty(self):
        endpoint = ANALYTICS + '/api/v1/warehouse_remains'
        task = endpoint + '/tasks/' + TASK_ID
        waiting = Wire(('GET', endpoint, {'data': {'taskId': TASK_ID}}),
                       ('GET', task + '/status', {'data': {'id': TASK_ID, 'status': 'processing'}}))
        initial = self.read('wb_stocks', {'article': ARTICLE}, waiting)
        self.assertEqual(initial['status'], 'pending')
        self.assertFalse(initial['complete'])
        self.assertEqual(initial['task_id'], TASK_ID)
        self.assertGreaterEqual(initial['retry_after_seconds'], 5)
        resumed = Wire(('GET', task + '/status', {'data': {'id': TASK_ID, 'status': 'done'}}),
                       ('GET', task + '/download', None))
        ready = self.read('wb_stocks', {'article': ARTICLE, 'task_id': initial['task_id']}, resumed)
        self.assertEqual(ready['items'], [])
        self.assertTrue(ready['complete'])

    def test_stock_matching_nmid_with_missing_vendor_code_is_not_a_final_empty_report(self):
        task = ANALYTICS + '/api/v1/warehouse_remains/tasks/' + TASK_ID
        rows = [{'nmId': NM, 'barcode': BARCODE, 'warehouses': [{'warehouseName':'W','quantity':9}]}]
        wire = Wire(('GET', task + '/status', {'data': {'id': TASK_ID, 'status': 'done'}}),
                    ('GET', task + '/download', rows))
        report = self.read('wb_stocks', {'article':ARTICLE, 'nm_id':NM, 'task_id':TASK_ID}, wire)
        self.assertFalse(report['complete'])
        self.assertIn('stock_identity_unconfirmed', report['issues'])

    def test_report_worker_rejects_status_for_another_task_before_download(self):
        endpoint = ANALYTICS + '/api/v1/warehouse_remains/tasks/' + TASK_ID + '/status'
        wire = Wire(('GET', endpoint, {'data': {'id': 'd84def8c-dd62-4ed9-ab27-36a60bee0322', 'status': 'done'}}))
        with self.assertRaises(ValueError):
            wb_expansion.read('wb_stocks', {'task_id': TASK_ID}, wire)
        self.assertFalse(wire.responses)

    def test_report_accepts_an_opaque_provider_issued_task_identifier(self):
        # WB specifies a string for taskId and the task_id path segment. UUID is an example.
        base = ANALYTICS + '/api/v1/warehouse_remains'
        opaque_id = 'wb-job_47'
        task = base + '/tasks/' + opaque_id
        wire = Wire(('GET', base, {'data': {'taskId': opaque_id}}),
                    ('GET', task + '/status', {'data': {'id': opaque_id, 'status': 'done'}}),
                    ('GET', task + '/download', []))
        report = self.read('wb_stocks', {}, wire)
        self.assertEqual(report['task_id'], opaque_id)
        self.assertTrue(report['complete'])

    def test_pending_id_survives_cache_expiry_and_ready_snapshot_serves_repeat_reads(self):
        endpoint = ANALYTICS + '/api/v1/warehouse_remains'
        def status(task):
            return endpoint + '/tasks/' + task + '/status'
        def download(task):
            return endpoint + '/tasks/' + task + '/download'

        upstream = Wire(('GET', endpoint, {'data': {'taskId': 'snapshot-1'}}),
                        ('GET', status('snapshot-1'), {'data': {'id': 'snapshot-1', 'status': 'processing'}}),
                        ('GET', status('snapshot-1'), {'data': {'id': 'snapshot-1', 'status': 'done'}}),
                        ('GET', download('snapshot-1'), []),
                        ('GET', endpoint, {'data': {'taskId': 'snapshot-2'}}),
                        ('GET', status('snapshot-2'), {'data': {'id': 'snapshot-2', 'status': 'done'}}),
                        ('GET', download('snapshot-2'), []))

        def offline_open(req, timeout):
            stream = io.BytesIO(json.dumps(upstream(req.get_method(), req.full_url)).encode())
            stream.status = 200
            return stream

        with TemporaryDirectory() as tmp:
            wire = market_transport.Wire('wb', {'api_token': 'synthetic-placeholder'},
                                         Path(tmp) / 'synthetic-vault', time.monotonic() + 60)
            with (patch.object(wire.opener, 'open', side_effect=offline_open),
                  patch.object(wire, 'wait', return_value=False),
                  patch.object(market_transport, '_wb_personal', return_value=True)):
                # Once the first report has finished, a repeat must use its downloaded snapshot.
                for elapsed, expected_status, expected_id in ((0, 'pending', 'snapshot-1'),
                        (1801, 'ready', 'snapshot-1'), (1802, 'ready', 'snapshot-1'),
                        (3602, 'ready', 'snapshot-2')):
                    with self.subTest(elapsed=elapsed), patch.object(market_transport.time, 'time',
                                                                    return_value=1_800_000_000 + elapsed):
                        report = wb_expansion.read('wb_stocks', {}, wire)
                        self.assertEqual((report['status'], report['task_id']), (expected_status, expected_id))
        self.assertFalse(upstream.responses)

    def test_completed_report_can_resume_a_rate_limited_download(self):
        base = ANALYTICS + '/api/v1/warehouse_remains'
        task = base + '/tasks/' + TASK_ID
        limited = HTTPError(task + '/download', 429, 'rate limit', {'Retry-After': '45'}, None)
        wire = Wire(('GET', base, {'data': {'taskId': TASK_ID}}),
                    ('GET', task + '/status', {'data': {'id': TASK_ID, 'status': 'done'}}),
                    ('GET', task + '/download', limited))
        result = self.read('wb_stocks', {}, wire)
        self.assertFalse(result['complete'])
        self.assertEqual(result['task_id'], TASK_ID)
        self.assertEqual(result['retry_after_seconds'], 45)

    def test_slots_disclose_provider_suspension(self):
        wire = Wire(('GET', SLOTS, HTTPError(SLOTS, 503, 'unavailable', {}, None)))
        result = self.read('wb_acceptance_slots', {'warehouse_id': 42}, wire)
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'upstream_disabled')
        self.assertEqual(result['items'], [])

    def test_restored_slots_preserve_blocked_free_and_paid_types_for_the_14_day_horizon(self):
        today = datetime.now(timezone(timedelta(hours=3))).date()
        def slot(day, box, coef, unload):
            return {'date': (today + timedelta(days=day)).isoformat() + 'T00:00:00+03:00',
                    'warehouseID': 42, 'warehouseName': 'Тестовый склад', 'boxTypeID': box,
                    'coefficient': coef, 'allowUnload': unload}
        raw = [slot(0, 2, -1, True), slot(1, 5, 0, False),
               slot(2, 2, 0, True), slot(13, 6, 2, True)]
        wire = Wire(('GET', SLOTS, raw))
        result = self.read('wb_acceptance_slots', {'warehouse_id': 42}, wire)
        self.assertEqual(str(wire.calls[0][2]['warehouseIDs']), '42')
        self.assertGreaterEqual(wire.calls[0][4]['basic_interval'], 3600)
        self.assertEqual(result['horizon_days'], 14)
        observed = sorted(result['items'], key=lambda row: row['date'])
        self.assertEqual([(r['date'][:10], str(r['warehouse_id']), r['warehouse']) for r in observed],
                         [(r['date'][:10], '42', 'Тестовый склад') for r in raw])
        self.assertEqual([Decimal(str(r['coefficient'])) for r in observed], [-1, 0, 0, 2])
        self.assertEqual([r['available'] for r in observed], [False, False, True, True])
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_dates_by_warehouse']['42'],
                         [(today + timedelta(days=i)).isoformat() for i in range(3, 13)])

    def test_fourteen_known_slot_dates_are_complete_for_the_selected_warehouse(self):
        today = datetime.now(timezone(timedelta(hours=3))).date()
        raw = [{'date': (today + timedelta(days=i)).isoformat(), 'warehouseID': 42,
                'warehouseName': 'W', 'boxTypeID': 2, 'coefficient': 0, 'allowUnload': True}
               for i in range(14)]
        result = self.read('wb_acceptance_slots', {'warehouse_id': 42}, Wire(('GET', SLOTS, raw)))
        self.assertTrue(result['complete'])
        self.assertEqual(result['missing_dates_by_warehouse'], {})
        raw[-1]['boxTypeID'] = 5
        split = self.read('wb_acceptance_slots', {'warehouse_id': 42}, Wire(('GET', SLOTS, raw)))
        self.assertFalse(split['complete'])
        boxes = split['missing_dates_by_box_type']['42']
        self.assertEqual(boxes['2'], [raw[-1]['date']])
        self.assertEqual(len(boxes['5']), 13)

    def test_slots_preserve_authorization_errors(self):
        wire = Wire(('GET', SLOTS, HTTPError(SLOTS, 403, 'forbidden', {}, None)))
        with self.assertRaises(HTTPError) as caught:
            wb_expansion.read('wb_acceptance_slots', {}, wire)
        self.assertEqual(caught.exception.code, 403)
        self.assertFalse(wire.responses)

    def test_financial_detail_uses_current_endpoint_and_real_currency_until_204(self):
        endpoint = FINANCE + '/api/finance/v1/sales-reports/detailed'
        raw = {'reportId': 41, 'rrdId': 55, 'rrDate': PERIOD['date_to'],
               'nmId': NM, 'vendorCode': ARTICLE, 'sku': BARCODE,
               'quantity': 2, 'docTypeName': 'Продажа', 'sellerOperName': 'Продажа',
               'currency': 'KZT', 'forPay': '61.22', 'vw': '8.10', 'vwNds': '1.62',
               'ppvzSalesCommission': '15.00', 'paidStorage': '0.10', 'paidAcceptance': '0.00',
               'deliveryService': '4.90', 'penalty': '0', 'ppvzSupplierInn': 'OMIT_TAX_ID'}
        args = {**PERIOD, 'article': ARTICLE, 'limit': 10, 'mode': 'details'}
        wire = Wire(('POST', endpoint, [raw]), ('POST', endpoint, None))
        pages = self.read_pages('wb_finance_report', args, wire)
        self.assertEqual([call[3]['rrdId'] for call in wire.calls], [0, 55])
        row, = [row for page in pages for row in page['items']]
        self.assertEqual((row['article'], row['currency'], row['quantity']), (ARTICLE, 'KZT', 2))
        self.assertNotIn('OMIT_TAX_ID', str(pages))

    def test_nonmonotonic_financial_cursor_cannot_reintroduce_an_already_seen_row(self):
        endpoint = FINANCE + '/api/finance/v1/sales-reports/detailed'
        rows = [{'rrdId': n, 'vendorCode': ARTICLE, 'nmId': NM, 'currency': 'RUB'} for n in (55, 43)]
        wire = Wire(('POST', endpoint, rows))
        try:
            result = self.read('wb_finance_report', {**PERIOD, 'mode': 'details'}, wire)
        except ValueError:
            return
        self.assertFalse(result['complete'])
        self.assertIsNone(result['next_cursor'])
        self.assertTrue(result['issues'])

    def test_financial_summary_uses_sold_units_and_excludes_unallocated_fees(self):
        endpoint = FINANCE + '/api/finance/v1/sales-reports/detailed'
        sale = {'rrdId': 20, 'nmId': NM, 'vendorCode': ARTICLE, 'sku': BARCODE,
                'currency': 'RUB', 'quantity': 2, 'sellerOperName': 'Продажа', 'docTypeName': 'Продажа',
                'vw': '8.10', 'vwNds': '1.62', 'ppvzSalesCommission': '15.00',
                'deliveryService': '4.90', 'rebillLogisticCost': '0.00', 'paidStorage': '0.10',
                'paidAcceptance': '0.40', 'penalty': '0.00', 'forPay': '90.00'}
        unallocated = {'rrdId': 21, 'nmId': 0, 'vendorCode': '', 'currency': 'RUB', 'paidStorage': '99.00'}
        wire = Wire(('POST', endpoint, [sale, unallocated]), ('POST', endpoint, None))
        pages = self.read_pages('wb_finance_report', {**PERIOD, 'article': ARTICLE, 'mode': 'details'}, wire)
        group, = [row for page in pages for row in page.get('summary', [])]
        self.assertEqual((group['sold_units'], group['currency']), (2, 'RUB'))
        rates = group['per_sold_unit']
        expected = {'commission_including_vat': '4.86', 'delivery': '2.45',
                    'storage': '0.05', 'acceptance': '0.20', 'penalty': '0.00'}
        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertEqual(Decimal(str(rates[name])), Decimal(value))
        fee, = [row for page in pages for row in page.get('unallocated', [])]
        self.assertEqual(Decimal(str(fee['storage'])), Decimal('99.00'))

    def test_for_pay_subtracts_returns_by_document_and_preserves_detailed_amounts(self):
        # WB Partners, "Еженедельные отчёты реализации": sale-column amount minus return-column amount.
        path = FINANCE + '/api/finance/v1/sales-reports/detailed'
        sale = {'rrdId': 81, 'nmId': NM, 'vendorCode': ARTICLE, 'currency': 'KZT',
                'quantity': 2, 'sellerOperName': 'Продажа', 'docTypeName': 'Продажа', 'forPay': '90.00',
                'vw': '10.00', 'vwNds': '2.00', 'deliveryService': '0.00'}
        returned = dict(sale, rrdId=82, quantity=1, sellerOperName='Возврат', docTypeName='Возврат',
                        forPay='40.00', vw='3.00', vwNds='0.60')
        logistics = dict(sale, rrdId=83, quantity=0, sellerOperName='Логистика', docTypeName='',
                         forPay='0', vw='0', vwNds='0', deliveryService='4.80')
        wire = Wire(('POST', path, [sale, returned, logistics]))
        page = self.read('wb_finance_report', {**PERIOD, 'article': ARTICLE, 'mode': 'details'}, wire)
        self.assertEqual([r['for_pay'] for r in page['items']], ['90.00', '40.00', '0'])
        group, = page['summary']
        self.assertEqual((group['sold_units'], group['returned_units'], group['currency']), (2, 1, 'KZT'))
        self.assertEqual(Decimal(group['amounts']['for_pay']), Decimal('50.00'))
        self.assertEqual(Decimal(group['per_sold_unit']['for_pay']), Decimal('25.00'))
        self.assertEqual(Decimal(group['amounts']['commission_including_vat']), Decimal('8.40'))
        self.assertEqual(Decimal(group['per_sold_unit']['delivery']), Decimal('2.40'))

    def test_undirected_nonzero_commission_is_explicitly_unresolved(self):
        path = FINANCE + '/api/finance/v1/sales-reports/detailed'
        common = {'nmId': NM, 'vendorCode': ARTICLE, 'currency': 'RUB'}
        sale = dict(common, rrdId=71, quantity=1, sellerOperName='Продажа', docTypeName='Продажа',
                    vw='10', vwNds='2', forPay='50')
        correction = dict(common, rrdId=72, quantity=0, sellerOperName='Уточнение', docTypeName='',
                          vw='2', vwNds='0.4', forPay='0')
        wire = Wire(('POST', path, [sale, correction]))
        page = self.read('wb_finance_report', {**PERIOD, 'mode': 'details'}, wire)
        summary, = page['summary']
        self.assertIn('commission_ex_vat', summary['unknown_fields'])
        self.assertIn('commission_vat', summary['unknown_fields'])
        self.assertNotIn('commission_including_vat', summary['amounts'])
        self.assertEqual(Decimal(summary['per_sold_unit']['for_pay']), Decimal(50))
        self.assertEqual(page['items'][1]['commission_ex_vat'], '2')

    def test_storage_keeps_recalculation_and_warehouse_scope(self):
        wire = report_download('paid_storage', [{
            'date': PERIOD['date_to'], 'originalDate': PERIOD['date_from'],
            'vendorCode': ARTICLE, 'nmId': NM, 'barcode': BARCODE, 'officeId': 0,
            'warehouse': 'Склад WB РФ', 'warehousePrice': Decimal('0.40'), 'barcodesCount': 2}])
        result = self.read('wb_paid_storage', {**PERIOD, 'article': ARTICLE, 'mode': 'details'}, wire)
        row, = result['items']
        self.assertEqual(row['warehouse'], 'Склад WB РФ')
        self.assertEqual(row['original_date'][:10], PERIOD['date_from'])
        self.assertEqual(Decimal(str(row['amount'])), Decimal('0.40'))

    def test_acceptance_report_does_not_invent_article_from_nm_id(self):
        wire = report_download('acceptance_report', [{
            'nmID': NM, 'incomeId': 501, 'count': 2, 'total': 12.5,
            'giCreateDate': PERIOD['date_from'], 'shkCreateDate': PERIOD['date_to']}])
        result = self.read('wb_acceptance_report', {**PERIOD, 'nm_id': NM, 'mode': 'details'}, wire)
        row, = result['items']
        self.assertEqual(str(row['nm_id']), str(NM))
        self.assertIsNone(row.get('article'))
        self.assertEqual(str(row['supply_id']), '501')
        self.assertEqual(Decimal(str(row['amount'])), Decimal('12.5'))
        self.assertEqual(row['currency'], 'RUB')

    def test_funnel_uses_selected_period_not_past_or_wb_club(self):
        period = {'start': PERIOD['date_from'], 'end': PERIOD['date_to']}
        selected = {'period': period, 'openCount': 100, 'cartCount': 30, 'orderCount': 6,
                    'orderSum': 125, 'wbClub': {'orderCount': 2},
                    'conversions': {'addToCartPercent': 30, 'cartToOrderPercent': 20}}
        products = [{'product': {'nmId': NM, 'vendorCode': ARTICLE},
                     'statistic': {'selected': selected, 'past': dict(selected, orderCount=64)}}]
        wire = Wire(('POST', ANALYTICS + '/api/analytics/v3/sales-funnel/products',
                     {'data': {'currency': 'KZT', 'products': products}}))
        result = self.read('wb_funnel', {**PERIOD, 'nm_id': NM, 'mode': 'details'}, wire)
        body = wire.calls[0][3]
        self.assertEqual((body['selectedPeriod'], body['nmIds']), (period, [NM]))
        row, = result['items']
        self.assertEqual((row['open_count'], row['cart_count'], row['order_count']), (100, 30, 6))
        self.assertEqual(row['currency'], 'KZT')

    def test_feedback_rating_dates_and_identity_use_marketplace_fields(self):
        raw = {'id': 'review-synthetic', 'createdDate': PERIOD['date_to'] + 'T08:00:00Z',
               'productValuation': 4, 'productDetails': {'nmId': NM, 'supplierArticle': ARTICLE},
               'text': 'отзыв', 'userName': 'OMIT_REVIEWER_NAME'}
        wire = Wire(('GET', FEEDBACK + '/api/v1/feedbacks',
                     {'error': False, 'data': {'countArchive': 300, 'countUnanswered': 20, 'feedbacks': [raw]}}))
        result = self.read('wb_feedbacks', {**PERIOD, 'nm_id': NM, 'mode': 'details'}, wire)
        query = wire.calls[0][2]
        for required in ('isAnswered', 'take', 'skip', 'dateFrom', 'dateTo'):
            self.assertIn(required, query)
        row, = result['items']
        self.assertEqual((row['rating'], row['article'], str(row['nm_id'])), (4, ARTICLE, str(NM)))
        self.assertEqual(result['mean_rating'], 4)
        self.assertEqual(result['rating_scope'], 'returned_page')
        self.assertNotIn('OMIT_REVIEWER_NAME', str(result))

    def test_archive_feedbacks_are_locally_filtered_on_moscow_day(self):
        before = START - timedelta(days=1)
        product = {'nmId': NM, 'supplierArticle': ARTICLE}
        rows = [{'productDetails': product, 'id': 'outside', 'productValuation': 1,
                 'createdDate': before.isoformat() + 'T17:30:00Z'},
                {'productDetails': product, 'id': 'inside', 'productValuation': 4,
                 'createdDate': before.isoformat() + 'T22:30:00Z'}]
        wire = Wire(('GET', FEEDBACK + '/api/v1/feedbacks/archive', {'data': {'feedbacks': rows}}))
        result = self.read('wb_feedbacks', {**PERIOD, 'report_scope': 'archive', 'nm_id': NM}, wire)
        query = wire.calls[0][2]
        self.assertTrue({'dateFrom', 'dateTo', 'isAnswered'}.isdisjoint(query))
        self.assertEqual([r['id'] for r in result['items']], ['inside'])
        self.assertEqual(result['mean_rating'], 4)

    def test_advertising_campaign_list_preserves_status_and_id(self):
        raw = {'all': 2, 'adverts': [{'type': 9, 'status': 9, 'count': 1, 'advert_list': [{'advertId': 123}]},
                                     {'type': 8, 'status': 7, 'count': 1, 'advert_list': [{'advertId': 124}]}]}
        wire = Wire(('GET', ADVERT + '/adv/v1/promotion/count', raw))
        result = self.read('wb_ads', {**PERIOD, 'report': 'campaigns'}, wire)
        self.assertEqual([(r['campaign_id'], r['status'], r['type']) for r in result['items']],
                         [(123, 9, 9), (124, 7, 8)])

    def test_advertising_attribution_and_campaign_debits_remain_distinct(self):
        endpoint = ADVERT + '/api/advert/v4/fullstats'
        ledger = ADVERT + '/adv/v1/upd'
        direct = {'advertId': 123, 'nmId': NM, 'date': PERIOD['date_to'], 'currency': 'KZT',
                  'placement': 'search', 'conversionType': 'direct', 'sum': 48.25, 'views': 40,
                  'clicks': 5, 'orders': 1, 'shks': 3, 'sumPrice': 2910}
        associated = dict(direct, conversionType='associated', sum=0, views=0, clicks=0, orders=2, shks=2)
        charges = [{'advertId': 123, 'updNum': 77, 'updTime': PERIOD['date_to'] + 'T10:00:00+03:00',
                    'updSum': 90, 'paymentType': 'Баланс'}, {'advertId': 999, 'updNum': 78, 'updSum': 500}]
        wire = Wire(('GET', endpoint, {'rows': [direct, associated, dict(direct, nmId=999)]}))
        result = self.read('wb_ads', {**PERIOD, 'report': 'statistics', 'campaign_ids': [123, 124],
                                      'nm_id': NM, 'mode': 'details'}, wire)
        self.assertEqual(wire.calls[0][2], {'advertIds': '123,124', 'beginDate': PERIOD['date_from'],
                                           'endDate': PERIOD['date_to']})
        items = result['items']
        self.assertEqual([(r['conversion_type'], r['orders'], r['ordered_units']) for r in items],
                         [('direct', 1, 3), ('associated', 2, 2)])
        self.assertEqual([(r['spend'], r['currency']) for r in items], [(48.25, 'KZT'), (0, 'KZT')])
        wire = Wire(('GET', ledger, charges))
        report = self.read('wb_ads', {**PERIOD, 'report': 'spend_ledger', 'campaign_ids': [123, 124]}, wire)
        self.assertEqual(wire.calls[0][2], {'from': PERIOD['date_from'], 'to': PERIOD['date_to']})
        debit, = report['items']
        self.assertEqual(str(debit['campaign_id']), '123')
        self.assertIsNone(debit.get('nm_id'))
        self.assertIsNone(debit.get('article'))
        self.assertEqual(debit['payment_type'], 'Баланс')
        self.assertIsNone(debit['currency'])
        self.assertEqual(Decimal(str(debit['spend'])), Decimal('90'))


if __name__ == '__main__':
    unittest.main()
