"""Offline, bidirectional acceptance of the historic and next marketplace releases.

Both supplied services run in separate interpreter processes with synthetic data.
The packaged candidate must match its Git archive and the controller's pinned
inventory. Historical marketplace release identities are never substituted.
This check is not a systemd, network, production-data or provider API test.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import urllib.error
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import check_market_cutover as replay

BASE = 'c8b2257cdd11bb539d3705d269fda6b23c0bde99'
BRIDGE = '6eccd5d159bd2957e6ff25b18783aa6e20e81ea2'
SERVER = '08c6f9b3778a4ea039d8a0093623c815cc95ecc4'
DEPLOYED_V2 = '4de7fd3f83fcd90eacf5221ef9d02651c1a49a09'
BASE_INVENTORY = 'b543e1568394095a20f14213feb3a194a456716c70d43b95c06b33c98cd32aed'
BRIDGE_INVENTORY = '4fbd8858010f37f39b7676c4b34fede5526aa56623569bbc231e77524edce0cf'
SERVER_INVENTORY = '88b7d5dde100b635d7ac881e1f6999fc9d0626fff5c1ea9e722e31ec58aa9dbc'
V2_INVENTORY = 'efce8b3fceb171b0ce4025e08708389623993046cb2962d26fbee43063af4187'
DEPLOYED_REPORT_INVENTORY = '0f009122b40353df9f0a65c8cd8b01b3efaf39703eaa5281349bd8c8183aae81'
DEPLOYED_REPORT_FIX_INVENTORY = '85d5b8a840f480678fe83fbf3bbd301a8db75d0910b8dfb39dfdce5fbe63d41f'
NEW_TOOLS = frozenset((
    'article_map', 'unit_economics', 'sales_drop_check', 'mcp_health',
    'ozon_stocks_analytics', 'ozon_turnover', 'ozon_delivery_time',
    'wb_retentions', 'wb_region_sales', 'wb_returns_report', 'wb_ads_finance', 'wb_search_queries',
))
CREDS = {'client_id': '101', 'api_key': 'synthetic-v2-rollback-key'}
ROTATED_CREDS = {'client_id': '101', 'api_key': 'synthetic-v2-rotated-key'}
BRIDGE_TOKEN = 'b' * 48
SESSION = 's' * 43
NOW = 100000


def export_service(ref, destination):
    destination.mkdir(parents=True)
    raw = subprocess.check_output(['git', '-C', str(REPO), '-c', 'core.autocrlf=false',
                                   'archive', ref + ':services/email-auth'])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(destination, filter='data')


def service_digest(path):
    return replay._service_digest(path)


def assert_sources(old, new, candidate_ref, root):
    if not re.fullmatch(r'[0-9a-f]{40}', candidate_ref):
        raise ValueError('candidate commit must be a full lowercase Git SHA')
    reference = root / 'candidate-reference'
    server = root / 'server-reference'
    deployed = root / 'deployed-v2-reference'
    export_service(candidate_ref, reference)
    export_service(SERVER, server)
    export_service(DEPLOYED_V2, deployed)
    old_hash, new_hash = service_digest(old), service_digest(new)
    if old_hash != SERVER_INVENTORY or service_digest(server) != SERVER_INVENTORY:
        raise AssertionError('the historic 08c6f9b service inventory does not match')
    if new_hash != V2_INVENTORY or service_digest(deployed) != V2_INVENTORY or new_hash != service_digest(reference):
        raise AssertionError('the historic v2 service does not match its exact deployed Git tree')
    for name in ('finance_gate.py', 'market_cooldown.py'):
        if (old / name).read_bytes() != (new / name).read_bytes():
            raise AssertionError('shared cooldown implementation changed: ' + name)
    return old_hash, new_hash


def assert_next_sources(deployed, candidate, candidate_ref, root, expected_parent=V2_INVENTORY):
    if not re.fullmatch(r'[0-9a-f]{40}', candidate_ref):
        raise ValueError('next candidate commit must be a full lowercase Git SHA')
    reference = root / 'next-reference'
    export_service(candidate_ref, reference)
    old_hash, new_hash = service_digest(deployed), service_digest(candidate)
    if old_hash != expected_parent or new_hash == old_hash or new_hash != service_digest(reference):
        raise AssertionError('next candidate must be distinct and match its exact Git service tree')
    for name in ('finance_gate.py', 'market_cooldown.py'):
        if (deployed / name).read_bytes() != (candidate / name).read_bytes():
            raise AssertionError('next candidate changed the shared cooldown implementation: ' + name)
    return old_hash, new_hash


def assert_policy(old, new, root, new_hash, next_service=None, next_hash=None, latest_service=None, latest_hash=None,
                  successors=()):
    # Import the policy which is to be shipped, not an older surrogate.
    import cd
    if (cd.MARKET_BASE, cd.MARKET_BRIDGE, cd.MARKET_REPORTS) != (
            BASE_INVENTORY, BRIDGE_INVENTORY, SERVER_INVENTORY):
        raise AssertionError('historical marketplace policy identities changed')
    if getattr(cd, 'MARKET_V2', None) != V2_INVENTORY or new_hash != V2_INVENTORY:
        raise AssertionError('candidate inventory is not independently pinned by the controller')
    base, bridge, previous, candidate = (root / x for x in ('base', 'bridge', 'previous', 'candidate'))
    export_service(BASE, base / 'service')
    export_service(BRIDGE, bridge / 'service')
    shutil.copytree(old, previous / 'service')
    shutil.copytree(new, candidate / 'service')
    if service_digest(base / 'service') != BASE_INVENTORY or service_digest(bridge / 'service') != BRIDGE_INVENTORY:
        raise AssertionError('historical source archive inventory mismatch')
    for first, second, expected in ((base, bridge, 'bootstrap'), (bridge, previous, 'shared'),
                                    (previous, bridge, 'shared'), (previous, candidate, 'preserve_blocked_finance'),
                                    (candidate, previous, 'preserve_blocked_finance'),
                                    (candidate, candidate, 'preserve_blocked_finance')):
        if cd.market_transition(first, second) != expected:
            raise AssertionError('reviewed transition was not admitted')

    hybrid = root / 'hybrid'
    shutil.copytree(candidate, hybrid)
    with (hybrid / 'service' / 'portal.py').open('ab') as stream:
        stream.write(b'\n# synthetic altered tree\n')
    forbidden = ((bridge, base), (base, previous), (previous, base),
                 (base, candidate), (candidate, base), (bridge, candidate), (candidate, bridge),
                 (previous, hybrid), (hybrid, previous), (candidate, hybrid), (hybrid, candidate))
    for first, second in forbidden:
        try:
            cd.market_transition(first, second)
        except cd.ReleaseError:
            continue
        raise AssertionError('unreviewed marketplace pair admitted: %s -> %s' % (first.name, second.name))
    if next_service is not None:
        if not isinstance(next_hash, str) or getattr(cd, 'MARKET_V2_NEXT', None) != next_hash:
            raise AssertionError('next candidate inventory has not been independently pinned by the controller')
        following, altered = root / 'following', root / 'hybrid-next'
        shutil.copytree(next_service, following / 'service')
        shutil.copytree(following, altered)
        with (altered / 'service' / 'portal.py').open('ab') as stream:
            stream.write(b'\n# synthetic altered next tree\n')
        for first, second in ((candidate, following), (following, candidate), (following, following)):
            if cd.market_transition(first, second) != 'preserve_blocked_finance':
                raise AssertionError('reviewed next transition was not admitted')
        for first, second in ((base, following), (following, base), (bridge, following), (following, bridge),
                              (previous, following), (following, previous),
                              (candidate, altered), (altered, candidate), (following, altered), (altered, following)):
            try:
                cd.market_transition(first, second)
            except cd.ReleaseError:
                continue
            raise AssertionError('unreviewed next marketplace pair admitted: %s -> %s' % (first.name, second.name))
        if latest_service is not None:
            if next_hash != DEPLOYED_REPORT_INVENTORY or cd.MARKET_V2_REPORT_FIX != latest_hash:
                raise AssertionError('the deployed and latest report inventories must both be pinned')
            latest = root / 'latest'
            shutil.copytree(latest_service, latest / 'service')
            for left, right in ((following, latest), (latest, following), (latest, latest)):
                if cd.market_transition(left, right) != 'preserve_blocked_finance':
                    raise AssertionError('reviewed report transition was not admitted')
            changed = root / 'hybrid-latest'
            shutil.copytree(latest, changed)
            with (changed / 'service' / 'portal.py').open('ab') as stream:
                stream.write(b'\n# synthetic altered report service\n')
            for excluded in (base, bridge, previous, candidate, hybrid, altered, changed):
                for left, right in ((excluded, latest), (latest, excluded)):
                    try:
                        cd.market_transition(left, right)
                    except cd.ReleaseError:
                        continue
                    raise AssertionError('unreviewed latest pair admitted: %s -> %s' % (left.name, right.name))
    if successors:
        if tuple(digest for _, digest in successors) != cd.MARKET_REPORT_SUCCESSORS:
            raise AssertionError('the packaged successors must match the complete reviewed inventory chain')
        prior = latest
        excluded = [base, bridge, previous, candidate, following, hybrid, altered, changed]
        for index, (path, _) in enumerate(successors):
            release = root / ('successor-' + str(index))
            shutil.copytree(path, release / 'service')
            mutated = root / ('hybrid-successor-' + str(index))
            shutil.copytree(release, mutated)
            with (mutated / 'service' / 'portal.py').open('ab') as stream:
                stream.write(b'\n# synthetic altered successor\n')
            for left, right in ((prior, release), (release, prior), (release, release)):
                if cd.market_transition(left, right) != 'preserve_blocked_finance':
                    raise AssertionError('reviewed successor pair was rejected')
            for other in [*excluded, mutated]:
                for left, right in ((other, release), (release, other)):
                    try:
                        cd.market_transition(left, right)
                    except cd.ReleaseError:
                        continue
                    raise AssertionError('unreviewed successor pair admitted: %s -> %s' % (left.name, right.name))
            excluded.extend((prior, mutated))
            prior = release


def network_fence():
    for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                   'socket.create_connection', 'socket.getaddrinfo'):
        patch(target, side_effect=AssertionError('network disabled')).start()


def request(app, path, payload=None, token=None):
    raw = json.dumps(payload).encode() if payload is not None else b''
    env = {'REQUEST_METHOD': 'POST' if payload is not None else 'GET', 'PATH_INFO': path,
           'HTTP_HOST': 'example.test', 'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(raw)),
           'wsgi.input': io.BytesIO(raw), 'HTTP_COOKIE': '__Host-sid=' + SESSION}
    if token:
        env['HTTP_AUTHORIZATION'] = 'Bearer ' + token
    status = []
    body = b''.join(app(env, lambda response, headers: status.append(int(response.split()[0]))))
    return status[0], json.loads(body) if payload is not None else body.decode()


def state_child():
    spec = json.load(sys.stdin)
    network_fence()
    sys.path.insert(0, os.getcwd())
    import portal
    import market_transport
    data, stage = Path(spec['data']), spec['stage']
    previous_is_v2 = spec.get('previous_is_v2', False)
    os.environ.update({'DATABASE_PATH': str(data / 'auth.sqlite'), 'VAULT_PATH': str(data / 'vault.sqlite'),
                       'SESSION_SECRET_FILE': str(data / 'session-secret'),
                       'VAULT_KEY_FILE': str(data / 'vault-secret'),
                       'READ_TOKEN_HASH_FILE': str(data / 'bridge-hash'),
                       'OZON_FINANCE_GATE_DIR': str(data / '.ozon-finance-gates'),
                       'BASE_URL': 'https://example.test'})
    # Production is Gunicorn. The installed Host.workers_idle requires exactly
    # one task per sync worker, including after the application has initialized.
    class ProductionOS:
        name = 'posix'
        def __getattr__(self, name):
            return getattr(os, name)
    portal.os = ProductionOS()
    sys.modules['gunicorn'] = types.ModuleType('gunicorn')
    existing_threads = set(threading.enumerate())
    app = portal.create_portal()
    if any(thread.is_alive() for thread in set(threading.enumerate()) - existing_threads):
        raise AssertionError('Gunicorn initialization started a background worker thread')
    now = NOW + {'seed': 0, 'candidate': 10, 'restored': 20, 'resume': 30}[stage]
    app.login.clock = lambda: now
    app.gateway = lambda name, args, creds: {'read_only': True}
    if stage == 'seed':
        with app.login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (app.login.digest(SESSION), NOW + 43200, 'c' * 32))
        app.vault.save('ozon', CREDS)
        _, token = app.external_keys.create('synthetic client', 'r' * 22, NOW)
        (data / 'synthetic-key').write_text(token, encoding='ascii')
        app.vault.record_bridge_activity('bridge', 'wb_prices', 'ok', NOW)
    token = (data / 'synthetic-key').read_text(encoding='ascii')
    if app.vault.read('ozon') != CREDS:
        raise AssertionError('vault did not survive release change')
    status, _ = request(app, '/settings')
    if status != 200:
        raise AssertionError('saved user session is no longer valid')
    status, catalog = request(app, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, token)
    if status != 200 or not catalog.get('result', {}).get('tools'):
        raise AssertionError('saved MCP credential is no longer valid')
    if app.external_keys.list_active()[0][3] != now:
        raise AssertionError('saved MCP last-use timestamp was not recorded')
    if stage == 'candidate':
        status, _ = request(app, '/api/read', {'operation': 'ozon_turnover', 'arguments': {}}, BRIDGE_TOKEN)
        if status != 200 or app.vault.bridge_activity('bridge') != ('unknown', 'ok', now):
            raise AssertionError('new operation did not retain rollback-compatible activity')
    if stage == 'restored':
        if app.vault.bridge_activity('bridge') != ('unknown', 'ok', NOW + 10):
            raise AssertionError('old release cannot read new operation activity')
        if previous_is_v2:
            from market_history import History, scope
            history = History(str(data / '.ozon-finance-gates'))
            if (not history.authorized(CREDS, 'synthetic-owner') or
                    not history.authorized(ROTATED_CREDS, 'synthetic-owner') or
                    scope('ozon', CREDS) != (data / 'archive-scope').read_text(encoding='ascii') or
                    scope('ozon', ROTATED_CREDS) != scope('ozon', CREDS)):
                raise AssertionError('deployed v2 cannot recognize next-release archive identity')
            expected = {
                '2026-11-16': ('2026-11-17T08:00:00+00:00',
                               [{'article': 'previous-synthetic-history', 'quantity': 1}]),
                '2026-11-17': ('2026-11-18T08:00:00+00:00',
                               [{'article': 'private-synthetic-history', 'quantity': 2}]),
            }
            if history.read_days('synthetic-owner', 'wb_orders', '2026-11-16', '2026-11-17') != expected:
                raise AssertionError('deployed v2 cannot read next-release encrypted history')
            if history.check(CREDS, 'ozon_turnover')['status'] != 'ok':
                raise AssertionError('deployed v2 cannot read next-release tool status')
        else:
            status, _ = request(app, '/api/read', {'operation': 'ozon_turnover', 'arguments': {}}, BRIDGE_TOKEN)
            if status != 400:
                raise AssertionError('old release should reject the unknown v2 operation')
    wire = market_transport.Wire('ozon', CREDS, str(data / '.ozon-finance-gates'), 10**12)
    cache_now = int((data / 'cache-clock').read_text(encoding='ascii'))
    if stage == 'seed':
        wire.store('legacy-cache', b'{"origin":"legacy"}', 86400)
        wire.record_stocks(['101'], 'synthetic-item',
                           [{'sku': '101', 'article': 'synthetic-item', 'available': 4}],
                           '2026-11-17T12:00:00Z')
        if previous_is_v2:
            from market_history import History, scope
            history = History(str(data / '.ozon-finance-gates'))
            (data / 'archive-scope').write_text(scope('ozon', CREDS), encoding='ascii')
            history.confirm(CREDS, 'synthetic-owner')
            history.save_day('synthetic-owner', 'wb_orders', '2026-11-16',
                             [{'article': 'previous-synthetic-history', 'quantity': 1}],
                             '2026-11-17T08:00:00+00:00')
    else:
        if wire.cached('legacy-cache') != b'{"origin":"legacy"}':
            raise AssertionError('legacy encrypted cache lost across transition')
        if stage == 'candidate':
            with patch.object(market_transport.time, 'time', return_value=cache_now):
                wire.store('v2-cache', b'{"origin":"v2"}', 86400, fetched=1700000000)
            if wire._http_cache('v2-cache', binary=True) != b'{"origin":"v2"}' or wire.fetch_times[-1] != 1700000000:
                raise AssertionError('valid v2 fetch time was not attached to its own ciphertext')
            wire.record_stocks(['101'], 'synthetic-item',
                               [{'sku': '101', 'article': 'synthetic-item', 'available': 7}],
                               '2026-11-18T12:00:00Z')
            from market_history import History
            history = History(str(data / '.ozon-finance-gates'))
            if previous_is_v2:
                from market_history import scope
                if (not history.authorized(CREDS, 'synthetic-owner') or
                        scope('ozon', CREDS) != (data / 'archive-scope').read_text(encoding='ascii') or
                        scope('ozon', ROTATED_CREDS) != scope('ozon', CREDS)):
                    raise AssertionError('next release cannot recognize deployed v2 archive identity')
                if history.read_days('synthetic-owner', 'wb_orders', '2026-11-16', '2026-11-16') != {
                        '2026-11-16': ('2026-11-17T08:00:00+00:00',
                                       [{'article': 'previous-synthetic-history', 'quantity': 1}])}:
                    raise AssertionError('next release cannot read deployed v2 encrypted history')
                history.confirm(ROTATED_CREDS, 'synthetic-owner')
            history.save_day('synthetic-owner', 'wb_orders', '2026-11-17',
                             [{'article': 'private-synthetic-history', 'quantity': 2}],
                             '2026-11-18T08:00:00+00:00')
            history.save_day('synthetic-owner', 'wb_orders', '2026-11-17',
                             [{'article': 'obsolete-synthetic-history', 'quantity': 9}],
                             '2026-11-18T10:00:00+03:00')
            history.record(CREDS, 'ozon_turnover', 'ok')
            status, health = request(app, '/api/read', {'operation': 'mcp_health',
                                                        'arguments': {'tool_name': 'ozon_turnover'}}, BRIDGE_TOKEN)
            if status != 200 or health['tool_count'] != len(catalog['result']['tools']) or health['items'][0]['status'] != 'ok':
                raise AssertionError('real MCP health gateway did not read the saved observation')
        elif stage == 'restored':
            if wire.cached('v2-cache') != b'{"origin":"v2"}':
                raise AssertionError('old release cannot read v2 encrypted cache')
            # Matching wall time and TTL force equal expiry across the rollback.
            with patch.object(market_transport.time, 'time', return_value=cache_now):
                wire.store('v2-cache', b'{"origin":"rollback"}', 86400)
        else:
            if wire.cached('v2-cache') != b'{"origin":"rollback"}':
                raise AssertionError('v2 cannot read the cache written after rollback')
            if wire._http_cache('v2-cache', binary=True) != b'{"origin":"rollback"}' or wire.fetch_times[-1] is not None:
                raise AssertionError('stale v2 fetch time was attached to a rollback cache write')
            from market_history import History
            history = History(str(data / '.ozon-finance-gates'))
            days = history.read_days('synthetic-owner', 'wb_orders', '2026-11-16', '2026-11-18')
            expected_days = {'2026-11-17': ('2026-11-18T08:00:00+00:00',
                                             [{'article': 'private-synthetic-history', 'quantity': 2}])}
            expected_coverage = {'2026-11-17': '2026-11-18T08:00:00+00:00'}
            if previous_is_v2:
                expected_days['2026-11-16'] = ('2026-11-17T08:00:00+00:00',
                                                [{'article': 'previous-synthetic-history', 'quantity': 1}])
                expected_coverage['2026-11-16'] = '2026-11-17T08:00:00+00:00'
            if days != expected_days:
                raise AssertionError('v2 could not decrypt its archive after rollback')
            if history.coverage('synthetic-owner', 'wb_orders') != expected_coverage:
                raise AssertionError('history coverage did not survive rollback')
            check = history.check(CREDS, 'ozon_turnover')
            if check['status'] != 'ok' or not check['last_checked_at']:
                raise AssertionError('tool status did not survive rollback')
            status, health = request(app, '/api/read', {'operation': 'mcp_health',
                                                        'arguments': {'tool_name': 'ozon_turnover'}}, BRIDGE_TOKEN)
            if status != 200 or health['tool_count'] != len(catalog['result']['tools']) or health['items'][0]['status'] != 'ok':
                raise AssertionError('real MCP health gateway did not survive rollback')
            history.save_day('synthetic-owner', 'wb_orders', '2026-11-18',
                             [{'article': 'private-synthetic-history', 'quantity': 3}],
                             '2026-11-19T08:00:00+00:00')
        result = wire.stock_history({'sku': '101', 'date_from': '2026-11-17', 'date_to': '2026-11-18'})
        items = result['items']
        expected = [4, 7] if stage in ('candidate', 'restored', 'resume') else [4]
        if [item['available'] for item in items] != expected:
            raise AssertionError('encrypted stock history was lost during rollback')
    print(json.dumps({'stage': stage, 'names': sorted(t['name'] for t in catalog['result']['tools'])}))


def run_state(python, service, data, stage, *, previous_is_v2=False):
    env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if name in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_state'], cwd=service,
                            input=json.dumps({'data': str(data), 'stage': stage, 'previous_is_v2': previous_is_v2}),
                            text=True, capture_output=True,
                            env=env, timeout=25)
    if result.returncode:
        raise AssertionError('state probe %s failed: %s' % (stage, result.stderr))
    return json.loads(result.stdout)


def schema(path):
    with contextlib.closing(sqlite3.connect(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise AssertionError('synthetic SQLite database failed integrity_check')
        return {(kind, name): (kind, name, owner, sql) for kind, name, owner, sql in
                db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')}


def cache_observations(path):
    with contextlib.closing(sqlite3.connect(path)) as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(cache_observations)')]
        if columns[:3] != ['k', 'expiry', 'fetched']:
            raise AssertionError('unexpected v2 cache observation schema')
        return db.execute("SELECT * FROM cache_observations WHERE k='v2-cache'").fetchall()


def history_key_loss_child():
    spec = json.load(sys.stdin)
    network_fence()
    sys.path.insert(0, os.getcwd())
    from market_history import History
    data = Path(spec['data'])
    path = data / '.market-reports' / 'history-v2.key'
    if path.exists():
        raise AssertionError('key-loss scenario still has its key')
    history = History(str(data / '.ozon-finance-gates'))
    try:
        history.read_days('synthetic-owner', 'wb_orders', '2026-11-17', '2026-11-17')
    except Exception:
        pass
    else:
        raise AssertionError('history read succeeded despite a missing encryption key')
    if path.exists():
        raise AssertionError('archive key loss was hidden by generating a replacement key')
    print(json.dumps({'blocked': True}))


def check_history_key_loss(python, service, data, root):
    scenario = root / 'key-loss'
    scenario.mkdir(mode=0o700)
    shutil.copytree(data / '.market-reports', scenario / '.market-reports')
    key = scenario / '.market-reports' / 'history-v2.key'
    key.unlink()
    db = scenario / '.market-reports' / 'history-v2.db'
    before = hashlib.sha256(db.read_bytes()).digest()
    env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if name in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_history-key-loss'],
                            cwd=service, input=json.dumps({'data': str(scenario)}), text=True,
                            capture_output=True, env=env, timeout=25)
    if result.returncode or json.loads(result.stdout) != {'blocked': True}:
        raise AssertionError('history key-loss check failed: ' + result.stderr)
    if key.exists() or hashlib.sha256(db.read_bytes()).digest() != before:
        raise AssertionError('history key loss changed the original archive or installed a replacement key')


SELLER_GROUPS = frozenset(('ozon_fbo_reports', 'ozon_fbs_reports', 'ozon_price_and_promotion_reports', 'ozon_product_reports', 'ozon_seller_information', 'ozon_settlement_reports', 'wb_csv_reports', 'wb_inventory_reports', 'wb_product_reports', 'wb_promotion_reports', 'wb_seller_information', 'wb_settlement_reports', 'wb_tariff_reports'))
V3_SELLER_VIEWS = frozenset(('ozon_balance', 'ozon_buyout', 'ozon_card_quality', 'ozon_compensations', 'ozon_competitor_prices', 'ozon_fbs_errors', 'ozon_fbs_orders', 'ozon_fbs_stocks', 'ozon_market', 'ozon_promotions', 'ozon_questions', 'ozon_removals', 'ozon_supply_acts', 'ozon_supply_slots', 'ozon_wrong_volume', 'wb_balance', 'wb_card_issues', 'wb_claims', 'wb_funnel_history', 'wb_promotions', 'wb_questions', 'wb_seller_info', 'wb_stocks_history', 'wb_tariffs'))


def check_state(python, old, new, root, *, previous_is_v2=False):
    data = root / 'state'
    data.mkdir(mode=0o700)
    (data / 'session-secret').write_text('x' * 32, encoding='ascii')
    (data / 'vault-secret').write_bytes(b'V' * 32)
    (data / 'bridge-hash').write_text(hashlib.sha256(BRIDGE_TOKEN.encode()).hexdigest(), encoding='ascii')
    (data / 'cache-clock').write_text(str(int(time.time())), encoding='ascii')
    first = run_state(python, old, data, 'seed', previous_is_v2=previous_is_v2)
    auth, vault, reports = data / 'auth.sqlite', data / 'vault.sqlite', data / '.market-reports' / 'reports.db'
    before = {p: schema(p) for p in (auth, vault, reports)}
    history = reports.parent / 'history-v2.db'
    history_key = reports.parent / 'history-v2.key'
    previous_history_schema = schema(history) if previous_is_v2 else None
    previous_history_key = history_key.read_bytes() if previous_is_v2 else None
    upgraded = run_state(python, new, data, 'candidate', previous_is_v2=previous_is_v2)
    if previous_is_v2:
        before_names, after_names = set(first['names']), set(upgraded['names'])
        if len(before_names - SELLER_GROUPS - V3_SELLER_VIEWS) != 58 or not before_names <= after_names:
            raise AssertionError('previous marketplace tools disappeared')
        additions = after_names - before_names
        if additions not in (frozenset(), SELLER_GROUPS, V3_SELLER_VIEWS, SELLER_GROUPS | V3_SELLER_VIEWS):
            raise AssertionError('unexpected or incomplete marketplace tool additions')
    else:
        if len(first['names']) != 46 or len(upgraded['names']) != 58 or set(upgraded['names']) - set(first['names']) != NEW_TOOLS:
            raise AssertionError('expected 46 existing and 12 additional MCP tools')
        if not set(first['names']) < set(upgraded['names']):
            raise AssertionError('existing MCP tools were removed')
    for path in (auth, vault):
        if schema(path) != before[path]:
            raise AssertionError('controller requires an unchanged auth/vault schema')
    new_schema = schema(reports)
    if not before[reports].items() <= new_schema.items():
        raise AssertionError('candidate changed a previous report schema object')
    if previous_is_v2 and (new_schema != before[reports] or schema(history) != previous_history_schema or
                           history_key.read_bytes() != previous_history_key):
        raise AssertionError('next candidate changed the deployed report/history schema or encryption key')
    observations = cache_observations(reports)
    if len(observations) != 1:
        raise AssertionError('candidate did not persist its observation')
    schema(history)
    key_bytes = history_key.read_bytes()
    if len(key_bytes) != 32 or b'private-synthetic-history' in history.read_bytes():
        raise AssertionError('history was not written with a private encryption key')
    if os.name != 'nt' and stat.S_IMODE(history_key.stat().st_mode) & 0o077:
        raise AssertionError('history encryption key is not private')
    check_history_key_loss(python, new, data, root)
    archive_digest = hashlib.sha256(history.read_bytes()).digest()
    restored = run_state(python, old, data, 'restored', previous_is_v2=previous_is_v2)
    if restored['names'] != first['names']:
        raise AssertionError('rollback did not restore the previous MCP catalogue')
    if hashlib.sha256(history.read_bytes()).digest() != archive_digest or history_key.read_bytes() != key_bytes:
        raise AssertionError('old release modified the v2 history archive or key')
    if cache_observations(reports) != observations:
        raise AssertionError('old release modified the v2 observations')
    resumed = run_state(python, new, data, 'resume', previous_is_v2=previous_is_v2)
    if resumed['names'] != upgraded['names']:
        raise AssertionError('re-upgrade did not restore the v2 MCP catalogue')
    if any(schema(path) != before[path] for path in (auth, vault)):
        raise AssertionError('auth/vault schema changed after rollback or resume')
    if not before[reports].items() <= schema(reports).items():
        raise AssertionError('previous report schema objects were not preserved')
    if previous_is_v2 and (schema(reports) != before[reports] or schema(history) != previous_history_schema or
                           history_key.read_bytes() != previous_history_key):
        raise AssertionError('deployed report/history schema or encryption key changed after rollback')


def check_submissions(python, source, target, data, provider):
    replay._probe(python, source, data, 'submission', provider=provider, reply='body_crash', exit_code=73)
    if replay._attempt_count(data) != 1:
        raise AssertionError('expected one unknown submission')
    for rotated in (False, True):
        outcome = replay._probe(python, target, data, 'submission', provider=provider, tick=1, rotated=rotated)
        if outcome.get('kind') != 'gate' or outcome.get('reason') not in ('retry_time_unknown', 'busy'):
            raise AssertionError('unknown submission was not blocked: ' + repr(outcome))
        if replay._attempt_count(data) != 1:
            raise AssertionError('unknown submission was sent again')
    if provider == 'performance':
        outcome = replay._probe(python, target, data, 'submission', provider=provider, tick=1, rotated=True, other=True)
        if outcome.get('kind') != 'gate' or replay._attempt_count(data) != 1:
            raise AssertionError('active Performance slot was not preserved')


def check_confirmed_submission(python, source, target, data, provider):
    first = replay._probe(python, source, data, 'submission', provider=provider)
    if first.get('kind') != 'ok' or replay._attempt_count(data) != 1:
        raise AssertionError('synthetic submission was not confirmed')
    reused = replay._probe(python, target, data, 'submission', provider=provider, tick=1)
    if reused != first or replay._attempt_count(data) != 1:
        raise AssertionError('confirmed report task was not reused across releases')
    again = replay._probe(python, source, data, 'submission', provider=provider, tick=2)
    if again != first or replay._attempt_count(data) != 1:
        raise AssertionError('confirmed report task was not retained after rollback')
    rotated = replay._probe(python, target, data, 'submission', provider=provider, tick=3, rotated=True)
    if rotated.get('kind') != 'gate' or rotated.get('reason') not in ('retry_time_unknown', 'busy'):
        raise AssertionError('credential rotation allowed a duplicate confirmed submission')
    if replay._attempt_count(data) != 1:
        raise AssertionError('credential rotation repeated a confirmed submission')


def check_submission_429(python, source, target, data, provider, reply):
    first = replay._probe(python, source, data, 'submission', provider=provider, reply=reply)
    if (first.get('kind'), first.get('status')) != ('http', 429) or replay._attempt_count(data) != 1:
        raise AssertionError('submission did not receive its synthetic 429')
    for rotated in (False, True):
        outcome = replay._probe(python, target, data, 'submission', provider=provider, tick=17, rotated=rotated)
        replay._blocked(outcome, unknown=reply == 'unknown429', minimum=43)
        if replay._attempt_count(data) != 1:
            raise AssertionError('submission ignored the shared 429 deadline')
    if reply == 'known429':
        after = replay._probe(python, target, data, 'submission', provider=provider, tick=61, rotated=True)
        if after.get('kind') != 'ok' or replay._attempt_count(data) != 2:
            raise AssertionError('rejected submission did not resume after its known retry time')


def finance_child():
    spec = json.load(sys.stdin)
    network_fence()
    sys.path.insert(0, os.getcwd())
    import portal

    class Clock:
        time = staticmethod(lambda: 1700000000 + spec['wall'])
        monotonic = staticmethod(lambda: spec['tick'])
        sleep = staticmethod(lambda _: None)

    class OfflineOpener:
        def open(self, req, timeout):
            if req.full_url != 'https://api-seller.ozon.ru/v1/finance/accrual/types':
                raise AssertionError('unexpected finance provider request')
            with (Path(spec['data']) / 'finance-attempts').open('a', encoding='ascii') as stream:
                stream.write('attempt\n')
                stream.flush()
                os.fsync(stream.fileno())
            if spec['reply'] == 'crash':
                os._exit(73)
            if spec['reply'] in ('known429', 'unknown429'):
                headers = {'Retry-After': '60'} if spec['reply'] == 'known429' else {}
                raise urllib.error.HTTPError('', 429, 'synthetic', headers, None)
            return io.BytesIO(b'{"accrual_types":[]}')

    creds = dict(CREDS, api_key=('synthetic-rotated' if spec['rotated'] else CREDS['api_key']))
    envelope = {'operation': 'ozon_accrual_types', 'args': {}, 'creds': creds,
                'finance_gate_dir': str(Path(spec['data']) / '.ozon-finance-gates'),
                'deadline_monotonic': time.monotonic() + 18}
    output = io.StringIO()
    with patch.object(portal.finance_gate, 'time', Clock), \
         patch.object(portal.finance_gate, '_boot_identity', return_value=spec['boot']), \
         patch('urllib.request.build_opener', return_value=OfflineOpener()), \
         patch('sys.stdin', io.StringIO(json.dumps(envelope))), patch('sys.stdout', output):
        portal._provider_worker()
    print(output.getvalue())


def run_finance(python, service, data, *, reply='ok', tick=0, wall=None,
                boot=replay.BOOT, rotated=False, exit_code=0):
    spec = {'data': str(data), 'reply': reply, 'tick': tick, 'wall': tick if wall is None else wall,
            'boot': boot, 'rotated': rotated}
    env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if name in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_finance'], cwd=service,
                            input=json.dumps(spec), text=True, capture_output=True, env=env, timeout=25)
    if result.returncode != exit_code:
        raise AssertionError('finance probe exited %s, expected %s: %s' %
                             (result.returncode, exit_code, result.stderr))
    return None if exit_code else json.loads(result.stdout)


def category_child():
    spec = json.load(sys.stdin)
    network_fence()
    sys.path.insert(0, os.getcwd())
    import expansion_gateway
    import market_cooldown
    import market_transport
    import portal

    class Clock:
        time = staticmethod(lambda: 1700000000 + spec['tick'])
        monotonic = staticmethod(lambda: spec['tick'])
        sleep = staticmethod(lambda _: None)

    class OfflineOpener:
        def open(self, req, timeout):
            with (Path(spec['data']) / 'provider-attempts').open('a', encoding='ascii') as stream:
                stream.write('category request\n')
            if spec.get('reply') in ('known429', 'unknown429'):
                headers = {'Retry-After': '60'} if spec['reply'] == 'known429' else {}
                raise urllib.error.HTTPError('', 429, 'synthetic', headers, None)
            raise AssertionError('category credential bypassed the persisted cooldown')

    creds = {'api_token': 'synthetic-wb-A', 'category_keys': {'wb_content': {'api_token': 'synthetic-wb-B'}}}
    with patch.object(portal.finance_gate, 'time', Clock), \
         patch.object(portal.finance_gate, '_boot_identity', return_value=replay.BOOT), \
         patch.object(market_transport, 'time', Clock), \
         patch.object(market_cooldown, 'BOOTSTRAP_HOLD', Path(spec['data']) / '.market-cutover-pending'), \
         patch('urllib.request.build_opener', return_value=OfflineOpener()):
        wire = expansion_gateway.CategoryWire('wb', creds, str(Path(spec['data']) / '.ozon-finance-gates'),
                                              spec['tick'] + 18)
        wire.wait = lambda _: False
        try:
            wire('POST', replay.WB, body={'settings': {'cursor': {'limit': 1}, 'filter': {'withPhoto': -1}}}, interval=0)
        except portal.finance_gate.GateError as exc:
            result = {'kind': 'gate', 'reason': exc.reason, 'retry_after_seconds': exc.retry_after_seconds}
        except urllib.error.HTTPError as exc:
            result = {'kind': 'http', 'status': exc.code}
            exc.close()
        else:
            raise AssertionError('category request was not blocked')
    print(json.dumps(result))


def check_category_wire(python, old, new, root):
    for reply in ('known429', 'unknown429'):
        data = root / ('category-' + reply)
        data.mkdir(mode=0o700)
        first = replay._probe(python, old, data, 'legacy', provider='wb', reply=reply)
        if (first.get('kind'), first.get('status')) != ('http', 429) or replay._attempt_count(data) != 1:
            raise AssertionError('category fixture did not seed one 429')
        env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if name in os.environ}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_category'],
                                cwd=new, input=json.dumps({'data': str(data), 'tick': 17}), text=True,
                                capture_output=True, env=env, timeout=25)
        if result.returncode:
            raise AssertionError('category cooldown probe failed: ' + result.stderr)
        replay._blocked(json.loads(result.stdout), unknown=reply == 'unknown429', minimum=43,
                        recoverable=replay.bounded_reads(new))
        if replay._attempt_count(data) != 1:
            raise AssertionError('another WB category key bypassed the existing cooldown')
        reverse = root / ('category-reverse-' + reply)
        reverse.mkdir(mode=0o700)
        result = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_category'],
                                cwd=new, input=json.dumps({'data': str(reverse), 'tick': 0, 'reply': reply}),
                                text=True, capture_output=True, env=env, timeout=25)
        if result.returncode or json.loads(result.stdout) != {'kind': 'http', 'status': 429}:
            raise AssertionError('category fixture did not get a 429: ' + result.stderr)
        outcome = replay._probe(python, old, reverse, 'legacy', provider='wb', tick=17, rotated=True)
        replay._blocked(outcome, unknown=reply == 'unknown429', minimum=43,recoverable=replay.bounded_reads(new))
        if replay._attempt_count(reverse) != 1:
            raise AssertionError('rollback bypassed the WB category cooldown')
    print('PASS separate WB category key honors account cooldown in both directions', flush=True)


def check_finance(python, old, new, root):
    for label, source, target in (('forward', old, new), ('rollback', new, old)):
        for reply in ('known429', 'unknown429', 'crash'):
            data = root / ('finance-' + label + '-' + reply)
            data.mkdir(mode=0o700)
            outcome = run_finance(python, source, data, reply=reply, exit_code=73 if reply == 'crash' else 0)
            if reply != 'crash' and (outcome.get('kind'), outcome.get('status')) != ('http', 429):
                raise AssertionError('finance fixture did not get the provider 429')
            attempts = data / 'finance-attempts'
            if attempts.read_text(encoding='ascii').count('attempt') != 1:
                raise AssertionError('unexpected initial finance request count')
            for wall, boot, rotated in ((17, replay.BOOT, False), (10000, replay.BOOT, True)):
                outcome = run_finance(python, target, data, tick=17, wall=wall, boot=boot, rotated=rotated)
                replay._blocked(outcome, unknown=reply != 'known429', minimum=43,
                                recoverable=replay.bounded_reads(target) or (replay.bounded_reads(source) and reply=='unknown429'))
                if attempts.read_text(encoding='ascii').count('attempt') != 1:
                    raise AssertionError('finance provider contacted before the retry deadline')
            if reply == 'known429':
                outcome = run_finance(python, target, data, tick=17, boot='b' * 64, rotated=True)
                replay._blocked(outcome, unknown=True,minimum=43,recoverable=replay.bounded_reads(target))
                if attempts.read_text(encoding='ascii').count('attempt') != 1:
                    raise AssertionError('finance provider contacted after reboot with unknown deadline')
                outcome = run_finance(python, target, data, tick=61)
                if outcome.get('kind') != 'ok' or attempts.read_text(encoding='ascii').count('attempt') != 2:
                    raise AssertionError('finance read did not resume after the known deadline')
            else:
                outcome = run_finance(python, target, data, tick=86401, rotated=True)
                if replay.bounded_reads(target) or (replay.bounded_reads(source) and reply=='unknown429'):
                    if outcome.get('kind')=='gate':
                        # Both the method and account can have independent interrupted reads.
                        replay._blocked(outcome,unknown=True,recoverable=True)
                        assert attempts.read_text(encoding='ascii').count('attempt')==1
                        outcome=run_finance(python,target,data,tick=86501,rotated=True)
                    assert outcome.get('kind')=='ok' and attempts.read_text(encoding='ascii').count('attempt')==2, outcome
                else:
                    replay._blocked(outcome, unknown=True)
                    if attempts.read_text(encoding='ascii').count('attempt') != 1:
                        raise AssertionError('finance unknown outcome was repeated')
    print('PASS Ozon finance worker retry state survives forward and rollback', flush=True)


def check_cooldowns(python, old, new, root):
    pairs = (('old-legacy-new-legacy', old, 'legacy', new, 'legacy'),
             ('new-legacy-old-legacy', new, 'legacy', old, 'legacy'),
             ('old-legacy-new-wire', old, 'legacy', new, 'wire'),
             ('new-wire-old-legacy', new, 'wire', old, 'legacy'),
             ('old-wire-new-wire', old, 'wire', new, 'wire'),
             ('new-wire-old-wire', new, 'wire', old, 'wire'))
    for label, source, source_mode, target, target_mode in pairs:
        for provider in ('ozon', 'wb'):
            for reply in ('known429', 'unknown429', 'crash'):
                data = root / (label + '-' + provider + '-' + reply)
                data.mkdir(mode=0o700)
                replay._exercise(python, source, source_mode, target, target_mode, data, provider, reply)
        print('PASS', label, 'known/unknown 429, worker death, clock/boot and credential changes', flush=True)
    for label, source, target in (('forward', old, new), ('rollback', new, old)):
        for provider in ('wb', 'performance'):
            data = root / ('submission-' + label + '-' + provider)
            data.mkdir(mode=0o700)
            check_submissions(python, source, target, data, provider)
            confirmed = root / ('confirmed-submission-' + label + '-' + provider)
            confirmed.mkdir(mode=0o700)
            check_confirmed_submission(python, source, target, confirmed, provider)
            for reply in ('known429', 'unknown429'):
                limited = root / ('submission-limit-' + label + '-' + provider + '-' + reply)
                limited.mkdir(mode=0o700)
                check_submission_429(python, source, target, limited, provider, reply)
    print('PASS WB/Performance submission 429, unknown outcomes and confirmed task reuse', flush=True)


def main():
    if not __debug__:
        raise RuntimeError('acceptance checks must not run with Python optimization enabled')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-service', required=True, type=Path)
    parser.add_argument('--candidate-service', required=True, type=Path)
    parser.add_argument('--candidate-commit', required=True)
    parser.add_argument('--next-service', type=Path)
    parser.add_argument('--next-commit')
    parser.add_argument('--latest-service', type=Path)
    parser.add_argument('--latest-commit')
    parser.add_argument('--successor', nargs=2, action='append', default=[], metavar=('COMMIT', 'SERVICE'))
    parser.add_argument('--python', required=True, type=Path)
    args = parser.parse_args()
    if bool(args.next_service) != bool(args.next_commit):
        parser.error('--next-service and --next-commit must be supplied together')
    if bool(args.latest_service) != bool(args.latest_commit) or (args.latest_service and not args.next_service):
        parser.error('the latest service requires its commit and a verified preceding service')
    if args.successor and not args.latest_service:
        parser.error('successor services require the reviewed predecessor')
    old = args.current_service.resolve(strict=True)
    new = args.candidate_service.resolve(strict=True)
    following = args.next_service.resolve(strict=True) if args.next_service else None
    latest = args.latest_service.resolve(strict=True) if args.latest_service else None
    python = args.python.absolute()  # preserve venv symlink
    if not python.is_file():
        parser.error('--python must be an existing interpreter')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old_hash, new_hash = assert_sources(old, new, args.candidate_commit, root)
        next_hash = None
        if following is not None:
            _, next_hash = assert_next_sources(new, following, args.next_commit, root)
        latest_hash = None
        if latest is not None:
            _, latest_hash = assert_next_sources(following, latest, args.latest_commit, root / 'latest-source', DEPLOYED_REPORT_INVENTORY)
        successors = []
        prior, prior_hash = latest, DEPLOYED_REPORT_FIX_INVENTORY
        for index, (ref, service) in enumerate(args.successor):
            path = Path(service).resolve(strict=True)
            _, digest = assert_next_sources(prior, path, ref, root / ('successor-source-' + str(index)), prior_hash)
            successors.append((path, digest))
            prior, prior_hash = path, digest
        assert_policy(old, new, root, new_hash, following, next_hash, latest, latest_hash, successors)
        print('PASS pinned service inventories and exact allowed release pairs', flush=True)
        pairs = [('historic C to D', old, new, False)]
        if following is not None:
            pairs.append(('deployed D to next E', new, following, True))
        if latest is not None:
            pairs.append(('deployed E to report fix F', following, latest, True))
        prior = latest
        for path, digest in successors:
            pairs.append(('reviewed successor ' + digest[:12], prior, path, True))
            prior = path
        for index, (label, source, target, previous_is_v2) in enumerate(pairs):
            pair_root = root / ('pair-' + str(index))
            pair_root.mkdir(mode=0o700)
            print('Checking', label, flush=True)
            check_state(python, source, target, pair_root, previous_is_v2=previous_is_v2)
            print('PASS session, MCP key, auth/vault schemas, report data and encrypted v2 archive', flush=True)
            check_finance(python, source, target, pair_root)
            check_cooldowns(python, source, target, pair_root)
            check_category_wire(python, source, target, pair_root)
        if (service_digest(old), service_digest(new)) != (old_hash, new_hash):
            raise AssertionError('a tested service changed during the check')
        if following is not None and service_digest(following) != next_hash:
            raise AssertionError('the next tested service changed during the check')
        if latest is not None and service_digest(latest) != latest_hash:
            raise AssertionError('the latest tested service changed during the check')
        if any(service_digest(path) != digest for path, digest in successors):
            raise AssertionError('a tested successor changed during the check')
    print('PASS v2 rollback offline acceptance', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--_state']:
        state_child()
    elif sys.argv[1:] == ['--_finance']:
        finance_child()
    elif sys.argv[1:] == ['--_history-key-loss']:
        history_key_loss_child()
    elif sys.argv[1:] == ['--_category']:
        category_child()
    else:
        main()
