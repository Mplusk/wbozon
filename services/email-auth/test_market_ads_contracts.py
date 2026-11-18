"""Synthetic OAuth and Ozon Performance CSV/ZIP contract tests; no network."""
import io
import copy
import sqlite3
import time
import unittest
import urllib.error
import zipfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

import finance_gate
import expansion_gateway
import market_transport
import market_specs
import ozon_performance


BASE = 'https://api-performance.ozon.ru/api/client'
TASK = '12345678-1234-4234-9234-1234567890ab'
CREDS = {'client_id': 'synthetic-client', 'client_secret': 'synthetic-secret'}
TOKEN = 'synthetic-performance-token'
ARGS = {'campaign_ids': ['12'], 'date_from': '2026-10-01', 'date_to': '2026-10-03'}


def sample_csv(*, sku='101', name='Example', views='10', clicks='2', orders='1', campaign='12',
               spend='12,50', currency='₽', quoted=False):
    """Header follows Ozon's public CPC sample; all data values are synthetic."""
    header = ('sku;Название товара;Цена товара, {0};Показы;Клики;CTR (%);В корзину;'
              'Средняя стоимость клика, {0};Расход, {0}, с НДС;Заказы;Продажи, {0};'
              'Заказы модели;Продажи с заказов модели, {0};ДРР, %;Заказано на сумму, {0};'
              'Общий ДРР;Дата добавления').format(currency)
    row = f'{sku};{name};100,00;{views};{clicks};20,00;0;6,25;{spend};{orders};100,00;0;0,00;12,5;100,00;12,5;01.10.2026'
    total = 'Всего;;;10;2;20,00;0;6,25;12,50;1;100,00;0;0,00;12,5;100,00;12,5;'
    if quoted:
        header, row, total = (f'"{value}"' for value in (header, row, total))
    return '\r\n'.join((f'Кампания по продвижению товаров № {campaign}, период 01.10.2026-03.10.2026',
                         header, row, total, ''))


def zipped(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return data.getvalue()


class PerformanceFixture:
    """Only fixed synthetic Performance endpoints are available to the adapter."""
    def __init__(self, state='OK', report=None, campaigns=None):
        self.token = None
        self.calls = []
        self.bindings = []
        self.state = state
        self.report = sample_csv().encode('utf-8') if report is None else report
        self.campaigns = campaigns if campaigns is not None else [{'id': '12', 'advObjectType': 'SKU'}]

    def __call__(self, method, url, *, query=None, body=None, **options):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'api-performance.ozon.ru' or parsed.query or parsed.fragment:
            raise AssertionError('unexpected Performance API endpoint')
        self.calls.append({'method': method, 'path': parsed.path, 'query': query, 'body': body, 'options': options})
        if url == BASE + '/token':
            if method != 'POST' or not options.get('token_request'):
                raise AssertionError('wrong token request')
            return {'access_token': TOKEN, 'expires_in': 1800, 'token_type': 'Bearer'}
        if self.token != TOKEN:
            raise AssertionError('request lacks the synthetic bearer token')
        if url == BASE + '/campaign' and method == 'GET':
            return {'list': self.campaigns}
        if url == BASE + '/statistics' and method == 'POST':
            return {'UUID': TASK}
        if url == BASE + '/statistics/' + TASK and method == 'GET':
            # An untrusted link must not redirect the access token anywhere.
            return {'UUID': TASK, 'state': self.state, 'link': 'https://invalid.example/report'}
        if url == BASE + '/statistics/report' and method == 'GET' and query == {'UUID': TASK}:
            if options.get('binary') is not True:
                raise AssertionError('the report must be read as bounded bytes')
            return self.report
        raise AssertionError('unexpected Performance API operation')

    def bind_job(self, endpoint, query, task, *, existing):
        self.bindings.append((endpoint, query, task, existing))


class OzonPerformanceContracts(unittest.TestCase):
    def wire_fixture(self):
        wire = market_transport.Wire.__new__(market_transport.Wire)
        wire.gate_dir = Path(self.enterContext(TemporaryDirectory())) / 'gate'
        return wire

    def test_oauth_and_pending_report_use_official_paths_and_do_not_download(self):
        provider = PerformanceFixture(state='IN_PROGRESS')
        result = ozon_performance.read(ARGS, provider, CREDS)
        token_call, create_call, status_call = provider.calls
        self.assertEqual(token_call['path'], '/api/client/token')
        self.assertEqual(token_call['body'], {**CREDS, 'grant_type': 'client_credentials'})
        self.assertTrue(token_call['options']['token_request'])
        self.assertEqual(create_call['path'], '/api/client/statistics')
        self.assertEqual(create_call['body'], {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                                               'dateTo': '2026-10-03', 'groupBy': 'DATE'})
        self.assertTrue(create_call['options']['job'])
        self.assertEqual(status_call['path'], '/api/client/statistics/' + TASK)
        self.assertFalse(result['complete'])
        self.assertEqual(result['task_id'], TASK)
        self.assertEqual(result['items'], [])
        self.assertNotIn(TOKEN, str(result))

    def test_success_fetches_only_fixed_official_report_endpoint(self):
        provider = PerformanceFixture()
        result = ozon_performance.read(ARGS, provider, CREDS)
        self.assertEqual(provider.calls[-1]['path'], '/api/client/statistics/report')
        self.assertEqual(provider.calls[-1]['query'], {'UUID': TASK})
        item, = result['items']
        self.assertEqual((item['campaign_id'], item['sku']), ('12', '101'))
        self.assertEqual(item['spend'], '12.50')
        self.assertNotIn(TOKEN, str(result))

    def test_existing_pending_job_is_not_created_again(self):
        provider = PerformanceFixture(state='NOT_STARTED')
        result = ozon_performance.read({**ARGS, 'task_id': TASK}, provider, CREDS)
        self.assertFalse(result['complete'])
        self.assertFalse(any(call['method'] == 'POST' and call['path'].endswith('/statistics')
                             for call in provider.calls))
        self.assertTrue(provider.bindings[0][3])

    def test_campaign_discovery_calls_read_only_official_endpoint(self):
        provider = PerformanceFixture()
        result = ozon_performance.read({'date_from': ARGS['date_from'], 'date_to': ARGS['date_to']}, provider, CREDS)
        self.assertEqual(provider.calls[1]['path'], '/api/client/campaign')
        self.assertEqual(provider.calls[1]['method'], 'GET')
        self.assertEqual(result['status'], 'choose_campaign_ids')
        self.assertEqual(str(result['items'][0]['id']), '12')

    def test_quoted_cp1251_public_sample_style_and_unknown_currency(self):
        raw = sample_csv(currency='?', quoted=True).encode('cp1251')
        item, = ozon_performance.parse_report(raw, ['12'])
        self.assertEqual((item['sku'], item['views'], item['clicks'], item['orders']), ('101', '10', '2', '1'))
        self.assertEqual(item['spend'], '12.50')
        self.assertEqual(item['tax_basis'], 'including_vat')
        self.assertIsNone(item['currency'])

    def test_utf8_report_does_not_add_total_to_product(self):
        items = ozon_performance.parse_report(sample_csv().encode('utf-8'), ['12'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['currency'], 'RUB')
        self.assertEqual(items[0]['spend'], '12.50')

    def test_zip_assigns_each_named_campaign_once(self):
        raw = zipped([('12.csv', sample_csv(sku='101').encode('utf-8')),
                      ('34.csv', sample_csv(sku='102', campaign='34').encode('utf-8')),
                      ('__MACOSX/._12.csv', b'non-report metadata')])
        rows = ozon_performance.parse_report(raw, ['12', '34'])
        self.assertEqual({(r['campaign_id'], r['sku']) for r in rows}, {('12', '101'), ('34', '102')})

    def test_zip_does_not_relabel_a_different_single_campaign(self):
        for filename in ('999.csv', 'campaign-999.csv'):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                ozon_performance.parse_report(zipped([(filename, sample_csv(campaign='999').encode('utf-8'))]), ['12'])

    def test_public_style_zip_uses_csv_campaign_when_filename_has_no_campaign_id(self):
        # Ozon's published sample is named stencils16-02.csv but its preamble
        # identifies campaign 11111111; there is no campaign ID in the filename.
        archive = zipped([('stencils16-02.csv', sample_csv(campaign='11111111', currency='?', quoted=True).encode('cp1251'))])
        item, = ozon_performance.parse_report(archive, ['11111111'])
        self.assertEqual(item['campaign_id'], '11111111')

    def test_single_csv_preamble_with_other_campaign_must_not_be_relabelled(self):
        raw = sample_csv(campaign='999').encode('utf-8')
        with self.assertRaises(ValueError):
            ozon_performance.parse_report(raw, ['12'])

    def test_zip_filename_number_does_not_override_a_different_preamble_campaign(self):
        # The date in an example filename can coincide with a campaign number.
        raw = zipped([('stencils16-02.csv', sample_csv(campaign='11111111').encode('utf-8'))])
        with self.assertRaises(ValueError):
            ozon_performance.parse_report(raw, ['16'])

    def test_csv_explicitly_reporting_a_different_period_is_not_called_complete(self):
        # Ozon's public CPC preamble carries the report period; it must not be
        # silently relabelled as the requested dates when these disagree.
        raw = sample_csv().replace('01.10.2026-03.10.2026', '01.09.2026-03.09.2026').encode('utf-8')
        provider = PerformanceFixture(report=raw)
        try:
            result = ozon_performance.read(ARGS, provider, CREDS)
        except ValueError:
            return
        self.assertFalse(result.get('complete'))

    def test_unknown_empty_header_is_not_treated_as_an_empty_report(self):
        with self.assertRaises(ValueError):
            ozon_performance.parse_report(b'sku;unknown\n', ['12'])

    def test_empty_and_nonempty_unknown_spend_headers_have_consistent_validation(self):
        header = 'sku;Показы;Клики;Заказы;Расход\n'
        def accepted(raw):
            try:
                ozon_performance.parse_report(raw.encode('utf-8'), ['12'])
            except ValueError:
                return False
            return True
        self.assertEqual(accepted(header), accepted(header + '101;10;2;1;3,00\n'))

    def test_duplicated_count_header_is_not_arbitrarily_overwritten(self):
        raw = ('sku;Показы;Показы;Клики;Заказы;Расход, ₽, с НДС\n'
               '101;10;999;2;1;12,50\n').encode('utf-8')
        with self.assertRaises(ValueError):
            ozon_performance.parse_report(raw, ['12'])

    def test_product_named_itogo_is_not_mistaken_for_total_row(self):
        raw = sample_csv(name='Итого').encode('utf-8')
        items = ozon_performance.parse_report(raw, ['12'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sku'], '101')
        self.assertEqual(items[0]['spend'], '12.50')

    def test_product_named_sku_is_not_mistaken_for_a_header(self):
        raw = sample_csv(name='sku').encode('utf-8')
        items = ozon_performance.parse_report(raw, ['12'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sku'], '101')

    def test_missing_spend_is_not_a_complete_financial_report(self):
        provider = PerformanceFixture(report=sample_csv(spend='').encode('utf-8'))
        result = ozon_performance.read(ARGS, provider, CREDS)
        item, = result['items']
        self.assertIsNone(item['spend'])
        self.assertFalse(result['complete'])

    def test_unknown_spend_currency_is_not_a_complete_financial_report(self):
        provider = PerformanceFixture(report=sample_csv(currency='?', quoted=True).encode('cp1251'))
        result = ozon_performance.read(ARGS, provider, CREDS)
        item, = result['items']
        self.assertEqual(item['spend'], '12.50')
        self.assertIsNone(item['currency'])
        self.assertFalse(result['complete'])

    def test_negative_counts_are_not_presented_as_valid_counts(self):
        raw = sample_csv(clicks='-1').encode('utf-8')
        with self.assertRaises(ValueError):
            ozon_performance.parse_report(raw, ['12'])

    def test_missing_uuid_keeps_job_outcome_unknown_without_another_post(self):
        # Exercise the real transport's job marker in memory. The fake provider
        # accepts the report creation but returns a valid JSON object without UUID.
        # Cache eviction is simulated; no socket or filesystem is involved.
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        class Opener:
            def __init__(self): self.job_calls = 0
            def open(self, req, timeout):
                if req.full_url.endswith('/api/client/token'):
                    return Response(b'{"access_token":"synthetic-token","expires_in":1800,"token_type":"Bearer"}')
                if req.full_url.endswith('/api/client/statistics'):
                    self.job_calls += 1
                    return Response(b'{}')
                raise AssertionError('unexpected fake endpoint')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
        wire = self.wire_fixture()
        wire.provider, wire.creds, wire.scope = 'performance', CREDS, 'synthetic-scope'
        wire.deadline, wire.token, wire.opener = time.monotonic() + 30, None, Opener()
        wire.db = lambda: db
        wire.cached = lambda _key: None
        wire.store = lambda *_args: None
        wire._admit = lambda *_args: None
        with self.assertRaises(ValueError):
            ozon_performance.read(ARGS, wire, CREDS)
        try:
            result = ozon_performance.read(ARGS, wire, CREDS)
        except (ValueError, finance_gate.GateError):
            pass
        else:
            self.assertFalse(result.get('complete'))
        self.assertEqual(wire.opener.job_calls, 1)

    def test_active_report_is_not_recreated_after_cache_or_job_identity_expiry(self):
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        class Opener:
            def __init__(self): self.job_calls = 0
            def open(self, req, timeout):
                self.job_calls += 1
                return Response(('{"UUID":"' + TASK + '"}').encode('ascii'))
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
        wire = self.wire_fixture()
        wire.provider, wire.creds, wire.scope = 'performance', CREDS, 'synthetic-scope'
        wire.deadline, wire.token, wire.opener = time.monotonic() + 30, TOKEN, Opener()
        wire.db = lambda: db
        wire.cached = lambda _key: None
        wire.store = lambda *_args: None
        wire._admit = lambda *_args: None
        body = {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                'dateTo': '2026-10-03', 'groupBy': 'DATE'}
        with patch('market_transport.time.time', return_value=1_800_000_000):
            first = wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
        self.assertEqual(first['UUID'], TASK)
        # The report status is still unknown: knowing its ID or the passage of
        # the cache/job TTL does not establish a terminal provider state.
        for now in (1_800_003_601, 1_800_086_401):
            with patch('market_transport.time.time', return_value=now):
                try:
                    again = wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
                except finance_gate.GateError:
                    pass
                else:
                    self.assertEqual(again.get('UUID'), TASK)
        self.assertEqual(wire.opener.job_calls, 1)

    def test_unknown_creation_does_not_self_authorize_retry_after_one_day(self):
        # The elapsed cache TTL cannot establish whether the first report was
        # accepted. Preserve unknown outcome until an explicit reconciliation.
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        class Opener:
            def __init__(self): self.calls = 0
            def open(self, req, timeout):
                self.calls += 1
                return Response(b'{}')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
        wire = self.wire_fixture()
        wire.provider, wire.creds, wire.scope = 'performance', CREDS, 'synthetic-scope'
        wire.deadline, wire.token, wire.opener = time.monotonic() + 30, TOKEN, Opener()
        wire.db = lambda: db
        wire.cached = lambda _key: None
        wire.store = lambda *_args: None
        wire._admit = lambda *_args: None
        body = {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                'dateTo': '2026-10-03', 'groupBy': 'DATE'}
        with patch('market_transport.time.time', return_value=1_800_000_000):
            with self.assertRaises(ValueError):
                wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
        with patch('market_transport.time.time', return_value=1_800_086_401):
            try:
                wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
            except (ValueError, finance_gate.GateError):
                pass
        self.assertEqual(wire.opener.calls, 1)

    def test_http_timeout_or_server_error_keeps_unknown_job_without_recreation(self):
        # An intermediary may return a timeout or server error after the provider
        # accepted the creation; a repeated POST is unsafe until reconciled.
        class Opener:
            def __init__(self, code):
                self.code, self.calls = code, 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(req.full_url, self.code, 'synthetic failure', {}, None)
        body = {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                'dateTo': '2026-10-03', 'groupBy': 'DATE'}
        for code in (408, 500, 502, 504):
            with self.subTest(code=code):
                db = sqlite3.connect(':memory:')
                self.addCleanup(db.close)
                db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
                wire = self.wire_fixture()
                wire.provider, wire.creds, wire.scope = 'performance', CREDS, 'synthetic-scope'
                wire.deadline, wire.token, wire.opener = time.monotonic() + 30, TOKEN, Opener(code)
                wire.db = lambda: db
                wire.cached = lambda _key: None
                wire.store = lambda *_args: None
                wire._admit = lambda *_args: None
                with self.assertRaises(urllib.error.HTTPError):
                    wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
                with self.assertRaises(finance_gate.GateError):
                    wire('POST', BASE + '/statistics', body=body, ttl=3600, job=True)
                self.assertEqual(wire.opener.calls, 1)

    def test_confirmed_terminal_error_allows_a_new_explicit_request(self):
        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): self.close()
        class Opener:
            def __init__(self): self.job_calls = 0
            def open(self, req, timeout):
                if req.full_url.endswith('/api/client/token'):
                    return Response(b'{"access_token":"synthetic-token","expires_in":1800,"token_type":"Bearer"}')
                if req.full_url.endswith('/api/client/statistics'):
                    self.job_calls += 1
                    return Response(('{"UUID":"' + TASK + '"}').encode('ascii'))
                if req.full_url.endswith('/api/client/statistics/' + TASK):
                    return Response(('{"UUID":"' + TASK + '","state":"ERROR"}').encode('ascii'))
                raise AssertionError('unexpected fake endpoint')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
        db.execute('CREATE TABLE cache(k TEXT PRIMARY KEY, expiry REAL NOT NULL, blob BLOB NOT NULL)')
        wire = self.wire_fixture()
        wire.provider, wire.creds, wire.scope = 'performance', CREDS, 'synthetic-scope'
        wire.deadline, wire.token, wire.opener = time.monotonic() + 30, None, Opener()
        wire.aead = market_transport.AESGCM(bytes(32))
        wire.db = lambda: db
        wire._admit = lambda *_args: None
        first = ozon_performance.read(ARGS, wire, CREDS)
        self.assertEqual(first['provider_status'], 'ERROR')
        body = {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                'dateTo': '2026-10-03', 'groupBy': 'DATE'}
        key = wire._key('POST', BASE + '/statistics', None, body)
        slot = market_transport._id([wire.scope, 'active-report'])
        self.assertIsNone(db.execute('SELECT k FROM jobs WHERE k IN (?,?)', (key, slot)).fetchone())
        self.assertIsNone(db.execute('SELECT k FROM cache WHERE k=?', (key,)).fetchone())
        second = ozon_performance.read(ARGS, wire, CREDS)
        self.assertEqual(second['provider_status'], 'ERROR')
        self.assertEqual(wire.opener.job_calls, 2)

    def test_finish_job_matches_identity_and_rolls_back_partial_cleanup(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)')
        db.execute('CREATE TABLE cache(k TEXT PRIMARY KEY, expiry REAL NOT NULL, blob BLOB NOT NULL)')
        wire = self.wire_fixture()
        wire.scope, wire.db = 'synthetic-scope', lambda: db
        endpoint = BASE + '/statistics'
        params = {'campaigns': ['12'], 'dateFrom': '2026-10-01',
                  'dateTo': '2026-10-03', 'groupBy': 'DATE'}
        key = wire._key('POST', endpoint, None, params)
        slot = market_transport._id([wire.scope, 'active-report'])
        db.executemany('INSERT INTO jobs VALUES(?,?,?)',
                       [(key, 'id:' + TASK, 2_000_000_000), (slot, key, 2_000_000_000)])
        db.execute('INSERT INTO cache VALUES(?,?,?)', (key, 2_000_000_000, b'synthetic'))
        db.commit()

        wire.finish_job(endpoint, params, 'ffffffff-ffff-4fff-8fff-ffffffffffff', failed=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 1)

        db.execute("CREATE TRIGGER cache_delete_failure BEFORE DELETE ON cache BEGIN SELECT RAISE(ABORT, 'injected'); END")
        db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            wire.finish_job(endpoint, params, TASK, failed=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 1)


class OrderShareContracts(unittest.TestCase):
    """Only synthetic provider JSON; no marketplace or account access."""

    def test_ozon_denominator_deduplicates_split_orders_per_sku_across_schemes(self):
        class Wire:
            def __init__(self): self.calls = []
            @contextmanager
            def progress(self, name, args):
                self.asserted_scope = (name, args)
                yield {}, lambda *_: None
            def __call__(self, method, url, *, body, **kwargs):
                self.calls.append((method, url, copy.deepcopy(body)))
                product = lambda sku: {'sku': sku, 'quantity': 1}
                scheme = 'FBS' if url.endswith('/fbs/list') else 'FBO'
                posting = lambda order, *skus: {'order_id': order, 'posting_number': f'{scheme}-{order}',
                                                 'products': [product(sku) for sku in skus]}
                if url.endswith('/v3/posting/fbo/list'):
                    if body['cursor'] == '':
                        return {'postings': [posting(1, 101), posting(2, 102)], 'has_next': True, 'cursor': 'next'}
                    if body['cursor'] == 'next':
                        return {'postings': [posting(1, 101), posting(3, 101)], 'has_next': False, 'cursor': ''}
                if url.endswith('/v3/posting/fbs/list') and body['offset'] == 0:
                    return {'result': {'postings': [posting(2, 102), posting(1, 101)], 'has_next': False}}
                raise AssertionError('unexpected synthetic order request')
        wire = Wire()
        counts, issue = expansion_gateway._ozon_order_counts(wire, ARGS, {'101', '102'})
        self.assertIsNone(issue)
        self.assertEqual(counts, {'101': {'orders': 2, 'units': 3}, '102': {'orders': 1, 'units': 2}})
        self.assertEqual(len(wire.calls), 3)
        filters = []
        for method, _, body in wire.calls:
            self.assertEqual(method, 'POST')
            self.assertTrue(body['filter']['since'] and body['filter']['to'])
            filters.append(body['filter'])
        self.assertTrue(all(value == filters[0] for value in filters))

    def test_ozon_share_requires_agreement_of_orders_and_ordered_units(self):
        for counts, expected in (({'orders': 2, 'units': 2}, Decimal('50')),
                                 ({'orders': 2, 'units': 3}, None),
                                 ({'orders': 2, 'units': None}, None)):
            with self.subTest(counts=counts):
                data = {'direct_attribution': [{'campaign_id': '12', 'sku': '101', 'direct_attributed': 1}]}
                with patch('expansion_gateway._ozon_order_counts', return_value=({'101': counts}, None)):
                    expansion_gateway._order_share('ozon_ads', ARGS, data, object())
                row, = data['order_share']['rows']
                self.assertEqual(row['unique_orders'], counts['orders'])
                self.assertEqual(row['ordered_units'], counts['units'])
                if expected is None:
                    self.assertIsNone(row['percent'])
                    self.assertNotEqual(data['order_share']['status'], 'ready')
                else:
                    self.assertEqual(Decimal(row['percent']), expected)
                    self.assertEqual(data['order_share']['status'], 'ready')
                self.assertTrue(data['order_share'].get('interpretation'))

    def test_ozon_unknown_product_or_nonadvancing_page_does_not_supply_percent(self):
        for malformed in ('missing_order_id', 'unknown_product', 'missing_cursor'):
            class Wire:
                @contextmanager
                def progress(self, *_): yield {}, lambda *_: None
                def __call__(self, method, url, *, body, **kwargs):
                    if url.endswith('/fbs/list'):
                        return {'result': {'postings': [], 'has_next': False}}
                    post = {'order_id': 1, 'products': [{'sku': 101}]}
                    if malformed == 'missing_order_id': post.pop('order_id')
                    if malformed == 'unknown_product': post['products'] = [{'sku': None}]
                    return {'postings': [post], 'has_next': malformed == 'missing_cursor', 'cursor': ''}
            with self.subTest(malformed=malformed):
                data = {'direct_attribution': [{'campaign_id': '12', 'sku': '101', 'direct_attributed': 1}]}
                expansion_gateway._order_share('ozon_ads', ARGS, data, Wire())
                self.assertNotEqual(data['order_share']['status'], 'ready')
                self.assertTrue(all(row['percent'] is None for row in data['order_share']['rows']))

    def test_ozon_same_posting_cannot_belong_to_two_different_order_ids(self):
        class Wire:
            @contextmanager
            def progress(self, *_): yield {}, lambda *_: None
            def __call__(self, method, url, *, body, **kwargs):
                if url.endswith('/fbs/list'): return {'result': {'postings': [], 'has_next': False}}
                def post(number, order):
                    return {'posting_number': number, 'order_id': order, 'products': [{'sku': 101, 'quantity': 1}]}
                return {'postings': [post('same', 1), post('same', 2), post('other', 1)], 'has_next': False}
        data = {'direct_attribution': [{'campaign_id': '12', 'sku': '101', 'direct_attributed': 1}]}
        expansion_gateway._order_share('ozon_ads', ARGS, data, Wire())
        self.assertNotEqual(data['order_share']['status'], 'ready')
        self.assertTrue(all(row['percent'] is None for row in data['order_share']['rows']))

    def test_ozon_denominator_detects_cursor_cycles_before_repeating_pages(self):
        class Wire:
            def __init__(self): self.cursors = []
            @contextmanager
            def progress(self, *_): yield {}, lambda *_: None
            def __call__(self, method, url, *, body, **kwargs):
                if not url.endswith('/fbo/list'): raise AssertionError('FBO scan should stop on a cursor cycle')
                cursor = body['cursor']
                self.cursors.append(cursor)
                next_cursor = {'': 'A', 'A': 'B', 'B': 'A'}[cursor]
                return {'postings': [{'posting_number': 'P', 'order_id': 1,
                    'products': [{'sku': 101, 'quantity': 1}]}], 'has_next': True, 'cursor': next_cursor}
        wire = Wire()
        counts, issue = expansion_gateway._ozon_order_counts(wire, ARGS, {'101'})
        self.assertEqual(counts, {})
        self.assertIsNotNone(issue)
        self.assertNotEqual(issue, 'pending_order_pages')
        self.assertEqual(wire.cursors, ['', 'A', 'B'])

    def test_wb_share_uses_direct_units_and_selected_funnel_order_count(self):
        source = [
            {'campaign_id': 12, 'nm_id': 71, 'date': '2026-10-01', 'placement': 'search',
             'conversion_type': 'direct', 'ordered_units': 2},
            {'campaign_id': 12, 'nm_id': 71, 'date': '2026-10-02', 'placement': 'search',
             'conversion_type': 'direct', 'ordered_units': 2},
            {'campaign_id': 12, 'nm_id': 71, 'date': '2026-10-02', 'placement': 'search',
             'conversion_type': 'associated', 'ordered_units': 90},
            {'campaign_id': 12, 'nm_id': 71, 'date': '2026-10-03', 'placement': 'search',
             'conversion_type': 'multicard', 'ordered_units': 90},
        ]
        facts = market_specs.direct_attribution(source, 'nm_id', 'ordered_units')
        self.assertEqual(facts, [{'campaign_id': '12', 'nm_id': '71', 'direct_attributed': 4}])
        class Wire:
            def __call__(self, method, url, *, body, **kwargs):
                self_assert = (method == 'POST' and url.endswith('/api/analytics/v3/sales-funnel/products')
                               and body['selectedPeriod'] == {'start': ARGS['date_from'], 'end': ARGS['date_to']}
                               and body['nmIds'] == [71])
                if not self_assert: raise AssertionError('unexpected synthetic funnel request')
                return {'data': {'products': [{'product': {'nmId': 71, 'vendorCode': 'A'},
                    'statistic': {'selected': {'orderCount': 10}, 'past': {'orderCount': 900}}}]}}
        data = {'direct_attribution': facts}
        expansion_gateway._order_share('wb_ads', dict(ARGS, nm_id=71), data, Wire())
        row, = data['order_share']['rows']
        self.assertEqual(data['order_share']['status'], 'ready')
        self.assertEqual((row['direct_attributed'], row['total_orders_or_units']), (4, 10))
        self.assertEqual(Decimal(row['percent']), Decimal('40'))

    def test_wb_denominator_unknown_and_over_100_percent_are_not_numeric(self):
        for selected in ({'orderCount': 0}, {'orderCount': 2}, {}):
            class Wire:
                def __call__(self, *_args, **_kwargs):
                    return {'data': {'products': [{'product': {'nmId': 71}, 'statistic': {'selected': selected}}]}}
            data = {'direct_attribution': [{'campaign_id': '12', 'nm_id': '71', 'direct_attributed': 3}]}
            expansion_gateway._order_share('wb_ads', dict(ARGS, nm_id=71), data, Wire())
            row, = data['order_share']['rows']
            self.assertIsNone(row['percent'])
            self.assertNotEqual(data['order_share']['status'], 'ready')

if __name__ == '__main__':
    unittest.main()
