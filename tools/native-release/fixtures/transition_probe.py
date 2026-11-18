"""Exercise the portal from the current release directory with synthetic inputs only."""
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, os.getcwd())
import portal

NOW = 100000
NEW_TOOLS = {
    'ozon_unallocated_accruals': {'date': '2026-11-17'},
    'ozon_fbo_supply_orders': {},
    'ozon_fbo_supply_details': {'order_ids': ['42']},
    'ozon_fbo_supply_items': {'bundle_id': 'bundle'},
    'ozon_fbo_volume_allocation': {'order_id': '42', 'supply_id': '71'},
}
CREDENTIALS = {'client_id': '101', 'api_key': 'offline-transition-fixture'}


def request(app, path, payload=None, auth=None):
    raw = json.dumps(payload).encode() if payload is not None else b''
    env = {'REQUEST_METHOD': 'POST' if payload is not None else 'GET', 'PATH_INFO': path,
           'HTTP_HOST': 'example.test', 'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(raw)),
           'wsgi.input': io.BytesIO(raw), 'HTTP_COOKIE': '__Host-sid=' + 's' * 43}
    if auth:
        env['HTTP_AUTHORIZATION'] = 'Bearer ' + auth
    status = []
    body = b''.join(app(env, lambda response, headers: status.append(int(response.split()[0]))))
    return status[0], json.loads(body) if payload is not None else body.decode()


def activity(data, stage):
    """Use the version's own WSGI entry points, SQLite tables, session and MCP key."""
    now = NOW + {'seed': 0, 'candidate': 10, 'restored': 100}[stage]
    login = portal.LoginApp(str(data / 'auth.sqlite'), 'x' * 32, 'https://example.test', clock=lambda: now)
    vault = portal.Vault(str(data / 'vault.sqlite'), b'V' * 32)
    bridge, mcp = 'b' * 48, 'm' * 48
    def offline(name, args, creds):
        assert creds == CREDENTIALS
        if name == 'ozon_fbo_supply_details':
            raise urllib.error.HTTPError('', 429, '', {'Retry-After':'17'}, None)
        if name == 'ozon_fbo_supply_items':
            raise urllib.error.HTTPError('', 403, '', {}, None)
        return {'read_only': True}
    app = portal.Portal(login, vault, hashlib.sha256(bridge.encode()).hexdigest(), offline,
                        mcp_read_token_hash=hashlib.sha256(mcp.encode()).hexdigest())
    if stage == 'seed':
        with login.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (login.digest('s' * 43), NOW+43200, 'c' * 32))
        vault.save('ozon', CREDENTIALS)
        _, token = app.external_keys.create('test client', secrets.token_urlsafe(16), NOW)
        (data / 'test-key').write_text(token)
        vault.record_bridge_activity('bridge', 'ozon_products', 'ok', NOW)
        vault.record_bridge_activity('mcp', 'wb_prices', 'ok', NOW)
    token = (data / 'test-key').read_text()
    status, catalog = request(app, '/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/list'}, token)
    assert status == 200
    names = {t['name'] for t in catalog['result']['tools']}
    assert 'ozon_accrual_types' in names and (names & NEW_TOOLS.keys()) == (set(NEW_TOOLS) if stage == 'candidate' else set())
    assert vault.read('ozon') == CREDENTIALS and app.external_keys.list_active()[0][3] == now
    if stage == 'candidate':
        assert vault.bridge_activity('bridge') == ('ozon_products','ok',NOW)
        assert vault.bridge_activity('mcp') == ('wb_prices','ok',NOW)
        for source, auth in [('bridge',bridge),('mcp',mcp)]:
            for name, args in NEW_TOOLS.items():
                now += 1
                status, _ = request(app, '/api/read', {'operation':name, 'arguments':args}, auth)
                assert status == {'ozon_fbo_supply_details':429,'ozon_fbo_supply_items':403}.get(name,200), (name,status)
                assert vault.bridge_activity(source) == ('unknown','ok' if status == 200 else 'failed',now)
            vault.record_bridge_activity(source,'wb_prices','ok',NOW)
        (data / 'activity-times').write_text(json.dumps([vault.bridge_activity(s)[2] for s in ('bridge','mcp')]))
    elif stage == 'restored':
        times = json.loads((data / 'activity-times').read_text())
        for source, stamp in zip(('bridge','mcp'),times):
            assert vault.bridge_activity(source) == ('unknown','ok',stamp)
        _, unsupported = request(app, '/mcp', {'jsonrpc':'2.0','id':2,'method':'tools/call',
            'params':{'name':'ozon_fbo_supply_orders','arguments':{}}}, token)
        assert unsupported['error']['code'] == -32602
    status, settings = request(app, '/settings')
    assert status == 200 and 'Последнее обращение MCP:' in settings
    assert 'Последнее обращение другого подключённого клиента:' in settings
    assert 'Не удалось загрузить сведения об обращениях' not in settings


def worker(data, scenario, tag, tick=None, hold=0):
    """Run the actual child dispatcher, replacing only the Seller HTTP transport."""
    name = 'ozon_unallocated_accruals' if 'ozon_unallocated_accruals' in portal.ozon_mcp.OPERATIONS else 'ozon_accrual_types'
    args = {'date':'2026-11-17', 'include_types':scenario == 'dictionary429'} if name == 'ozon_unallocated_accruals' else {}
    envelope = {'operation':name, 'args':args, 'creds':CREDENTIALS, 'finance_gate_dir':str(data / '.ozon-finance-gates'),
                'deadline_monotonic':time.monotonic()+18}

    class Seller:
        def open(self, req, timeout):
            path = req.full_url.removeprefix('https://api-seller.ozon.ru')
            assert path in ('/v1/finance/accrual/types','/v1/finance/accrual/by-day')
            with (data / 'provider-attempts').open('a') as f:
                f.write(tag+' '+path+'\n')
            if tick is None:
                (data / 'started').touch()
            if scenario == 'hold':
                (data / ('entered-'+tag)).touch()
                deadline = time.monotonic()+12
                while not (data / ('release-'+tag)).exists():
                    if time.monotonic()>deadline: raise RuntimeError('test provider was not released')
                    time.sleep(.025)
            elif hold:
                (data / ('provider-active-'+tag)).touch()
                time.sleep(hold)
            if scenario in ('unknown429','known429') or scenario == 'dictionary429' and path.endswith('/types'):
                headers = {} if scenario == 'unknown429' else {'Retry-After':'1' if scenario == 'dictionary429' else '60'}
                raise urllib.error.HTTPError('',429,'',headers,None)
            return io.BytesIO(b'{"accrual_types":[]}' if path.endswith('/types') else b'{"accruals":[],"last_id":""}')

    class GateClock:
        time = staticmethod(lambda: 1700000000+tick)
        monotonic = staticmethod(lambda: tick)

    output = io.StringIO()
    with patch('sys.stdin',io.StringIO(json.dumps(envelope))), patch('sys.stdout',output), \
         patch('portal.urllib.request.build_opener',return_value=Seller()), \
         patch.object(portal.finance_gate,'time',GateClock if tick is not None else time):
        portal._provider_worker()
    return json.loads(output.getvalue())


def report_state(data, stage):
    """Observe the real worker boundary with synthetic state and an offline HTTP transport."""
    gate = str(data / '.ozon-finance-gates')
    wb = {'api_token': 'synthetic-report-fixture'}
    paths = {
        'ozon': ('https://api-seller.ozon.ru/v1/analytics/data', CREDENTIALS, 'ozon_orders_analytics',
                 {'date_from': '2026-11-17', 'date_to': '2026-11-17'}),
        'wb': ('https://content-api.wildberries.ru/content/v2/get/cards/list', wb, 'wb_products', {'limit': 1}),
    }
    honored = {}
    for provider, (url, creds, name, args) in paths.items():
        http = Mock()
        http.open.side_effect = urllib.error.HTTPError('', 429, '', {}, None)
        if stage == 'legacy':
            envelope = dict(operation=name, args=args, creds=creds, finance_gate_dir=gate,
                            deadline_monotonic=time.monotonic() + 8)
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(json.dumps(envelope))), patch('sys.stdout', output), \
                 patch('urllib.request.build_opener', return_value=http):
                portal._provider_worker()
            result = json.loads(output.getvalue())
            if http.open.called:
                http.open.assert_called_once()
                assert http.open.call_args.args[0].full_url == url and result['status'] == 429, result
            else:
                assert result.get('reason') == 'retry_time_unknown', result
            honored[provider] = not http.open.called
        else:
            from market_transport import Wire
            wire = Wire(provider, creds, gate, time.monotonic() + 5)
            wire.opener = http
            try:
                wire('POST', url, body={}, interval=0)
            except urllib.error.HTTPError as exc:
                assert stage == 'seed' and exc.code == 429 and http.open.call_count == 1
                exc.close()
            except portal.finance_gate.GateError as exc:
                assert stage == 'resume' and exc.reason == 'retry_time_unknown' and not http.open.called
            else:
                raise AssertionError('offline method limit unexpectedly admitted')
    if stage == 'legacy':
        return honored

    wire = Wire('wb', wb, gate, time.monotonic() + 5)
    base = 'https://seller-analytics-api.wildberries.ru/api/v1/'
    for path, confirmed in (('warehouse_remains', True), ('paid_storage', False)):
        wire.opener = Mock()
        wire.opener.open.side_effect = TimeoutError()
        if confirmed:
            response = io.BytesIO(b'{"data":{"taskId":"synthetic-report-id"}}')
            response.status = 200
            wire.opener.open.side_effect = None
            wire.opener.open.return_value = response
        try:
            value = wire('GET', base + path, query={}, interval=0, job=True)
            assert confirmed and value['data']['taskId'] == 'synthetic-report-id'
        except TimeoutError:
            assert not confirmed and stage == 'seed'
        except portal.finance_gate.GateError as exc:
            assert not confirmed and stage == 'resume' and exc.reason == 'retry_time_unknown'
        assert wire.opener.open.call_count == (1 if stage == 'seed' else 0)
    return {'confirmed_task_reused_and_unknown_creation_blocked': stage == 'resume'}


if __name__ == '__main__':
    action, directory, *args = sys.argv[1:]
    if action == 'worker':
        print(json.dumps(worker(Path(directory), args[0], args[1], int(args[2]))))
    elif action == 'activity':
        activity(Path(directory), args[0])
    elif action == 'report-state' and args[0] in ('seed', 'legacy', 'resume'):
        with patch('socket.socket.connect', side_effect=AssertionError('network disabled')):
            print(json.dumps(report_state(Path(directory), args[0])))
