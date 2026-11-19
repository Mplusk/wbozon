"""Offline cross-release acceptance for the marketplace cooldown cutover.

Every probe runs the supplied release's actual worker or Wire in a separate
process with synthetic credentials, time and HTTP responses.  No network
connections or production data are used.  This does not exercise systemd or
the deployment controller's own durable cutover state.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
from decimal import Decimal
from unittest.mock import patch


OZON = 'https://api-seller.ozon.ru/v1/analytics/data'
WB = 'https://content-api.wildberries.ru/content/v2/get/cards/list'
PROFILE = 'https://api-seller.ozon.ru/v1/seller/info'
WB_REPORT = 'https://seller-analytics-api.wildberries.ru/api/v1/paid_storage'
PERFORMANCE_REPORT = 'https://api-performance.ozon.ru/api/client/statistics'
RETRY = 60
BOOT = 'a' * 64
BASELINE_SERVICE_SHA256 = 'b543e1568394095a20f14213feb3a194a456716c70d43b95c06b33c98cd32aed'


def _credentials(provider, rotated):
    suffix = 'C' if rotated == 2 else 'B' if rotated else 'A'
    if provider == 'ozon':
        return {'client_id': '101', 'api_key': 'synthetic-ozon-' + suffix}
    if provider == 'performance':
        return {'client_id': '101', 'client_secret': 'synthetic-performance-' + suffix}
    return {'api_token': 'synthetic-wb-' + suffix}


def _parameters(provider):
    if provider == 'ozon':
        return ('ozon_orders_analytics', {'date_from': '2026-11-17', 'date_to': '2026-11-17'},
                OZON, {'date_from': '2026-11-17', 'date_to': '2026-11-17',
                       'metrics': ['revenue', 'ordered_units'], 'dimension': ['day'],
                       'limit': 100, 'offset': 0})
    return ('wb_products', {'limit': 1}, WB,
            {'settings': {'cursor': {'limit': 1}, 'filter': {'withPhoto': -1}}})


def _child():
    spec = json.load(sys.stdin)
    # Fence network before importing the candidate, not only around its worker.
    for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                   'socket.create_connection', 'socket.getaddrinfo'):
        patch(target, side_effect=AssertionError('network disabled')).start()
    sys.path.insert(0, os.getcwd())
    import portal

    if spec['mode'] == 'catalog':
        print(json.dumps(sorted(portal.external_mcp.TOOL_NAMES)))
        return

    if not spec['baseline']:
        import market_cooldown
    provider = spec['provider']
    if spec['mode'] == 'submission':
        operation, arguments = None, None
        url = PERFORMANCE_REPORT if provider == 'performance' else WB_REPORT
        body = {'campaigns': ['13' if spec['other'] else '12']} if provider == 'performance' else None
        query = None if provider == 'performance' else {'dateFrom': '2026-11-01', 'dateTo': '2026-11-02'}
        method = 'POST' if provider == 'performance' else 'GET'
    else:
        operation, arguments, url, body = _parameters(provider)
        query, method = None, 'POST'
    creds = _credentials(provider, spec['rotated'])
    gate = str(Path(spec['data']) / '.ozon-finance-gates')
    marker = Path(spec['data']) / '.market-cutover-pending'
    attempts = Path(spec['data']) / 'provider-attempts'

    class Clock:
        time = staticmethod(lambda: 1700000000 + spec['wall'])
        monotonic = staticmethod(lambda: spec['tick'])
        sleep = staticmethod(lambda _: None)

    class Response(io.BytesIO):
        status = 200

        def read(self, size=-1):
            if spec['mode'] == 'submission' and spec['reply'] == 'body_crash':
                os._exit(73)
            return super().read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    class OfflineOpener:
        def open(self, request, timeout):
            del timeout
            target = request.full_url.split('?', 1)[0]
            if (target != url and not (target == PROFILE and provider == 'ozon') or
                    request.get_method() != method):
                raise AssertionError('unexpected synthetic provider request')
            with attempts.open('a', encoding='utf-8') as stream:
                stream.write(provider + ' ' + target + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            if target == PROFILE:
                if spec['reply'] == 'profile_unknown429':
                    raise urllib.error.HTTPError('', 429, 'synthetic', {}, None)
                return Response(b'{"company":{"currency":"RUB"}}')
            if spec['reply'] == 'crash':
                os._exit(73)
            if spec['reply'] in ('known429', 'unknown429'):
                headers = {'Retry-After': str(RETRY)} if spec['reply'] == 'known429' else {}
                raise urllib.error.HTTPError('', 429, 'synthetic', headers, None)
            if spec['mode'] == 'submission':
                return Response(b'{"UUID":"e3ddde4e-110c-48f1-bfe2-0244f6477fb8"}' if provider == 'performance'
                                else b'{"data":{"taskId":"synthetic-task"}}')
            if provider == 'ozon':
                return Response(b'{"result":{"data":[{"dimensions":[{"id":"2026-11-17"}],"metrics":[123.45,2]}]}}')
            return Response(b'{"cards":[{"nmID":123,"vendorCode":"fixture"}],"cursor":{"total":1,"updatedAt":"2026-11-17T00:00:00Z","nmID":123}}')

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('urllib.request.build_opener', return_value=OfflineOpener()))
        if not spec['baseline']:
            stack.enter_context(patch.object(market_cooldown, 'BOOTSTRAP_HOLD', marker))
        stack.enter_context(patch.object(portal.finance_gate, 'time', Clock))
        stack.enter_context(patch.object(portal.finance_gate, '_boot_identity', return_value=spec['boot']))
        if spec['mode'] == 'legacy':
            # The outer worker deadline and the inner provider both use monotonic time.
            stack.enter_context(patch.object(portal, 'time', Clock))
            envelope = {'operation': operation, 'args': arguments, 'creds': creds,
                        'finance_gate_dir': gate, 'deadline_monotonic': spec['tick'] + 18}
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(json.dumps(envelope))), patch('sys.stdout', output):
                portal._provider_worker()
            result = json.loads(output.getvalue())
        elif spec['mode'] in ('wire', 'submission'):
            import market_transport
            with patch.object(market_transport, 'time', Clock):
                wire = market_transport.Wire(provider, creds, gate, spec['tick'] + 18)
                wire.wait = lambda _: False
                if provider == 'performance':
                    wire.token = 'synthetic-access-token'
                try:
                    result = {'kind': 'ok', 'result': wire(method, url, query=query, body=body,
                                                            interval=0, job=spec['mode'] == 'submission')}
                except portal.finance_gate.GateError as exc:
                    result = {'kind': 'gate', 'reason': exc.reason,
                              'retry_after_seconds': exc.retry_after_seconds}
                except urllib.error.HTTPError as exc:
                    result = {'kind': 'http', 'status': exc.code,
                              'retry_after_seconds': getattr(exc, 'retry_after_seconds', None)}
                    if result['retry_after_seconds'] is None and exc.headers:
                        value = exc.headers.get('Retry-After')
                        if isinstance(value, str) and value.isdigit():
                            result['retry_after_seconds'] = int(value)
                    exc.close()
        else:
            raise AssertionError('invalid probe mode')
    print(json.dumps(result, default=lambda value: str(value) if isinstance(value, Decimal)
                     else (_ for _ in ()).throw(TypeError('unexpected provider value'))))


def _probe(python, service, data, mode, *, provider='ozon', reply='ok', tick=0,
           wall=None, boot=BOOT, rotated=False, exit_code=0, baseline=False, other=False):
    spec = {'mode': mode, 'data': str(data), 'provider': provider, 'reply': reply,
            'tick': tick, 'wall': tick if wall is None else wall, 'boot': boot,
            'rotated': rotated, 'baseline': baseline, 'other': other}
    env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
           if name in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.run([str(python), '-B', '-I', str(Path(__file__).resolve()), '--_child'],
                             cwd=service, input=json.dumps(spec), text=True, capture_output=True,
                             env=env, timeout=25)
    if process.returncode != exit_code:
        raise AssertionError('probe exited %s, expected %s: %s' %
                             (process.returncode, exit_code, process.stderr))
    if exit_code:
        return None
    return json.loads(process.stdout)


def _attempt_count(data):
    path = data / 'provider-attempts'
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


def _service_digest(service):
    inventory = {p.relative_to(service).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in service.rglob('*') if p.is_file()}
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _target_count(data, provider):
    path = data / 'provider-attempts'
    target = OZON if provider == 'ozon' else WB
    return sum(line == provider + ' ' + target for line in path.read_text(encoding='utf-8').splitlines())


def _profile_count(data):
    path = data / 'provider-attempts'
    return sum(line == 'ozon ' + PROFILE for line in path.read_text(encoding='utf-8').splitlines())


def bounded_reads(service):
    return 'READ_RECOVERY_SECONDS = 60' in (Path(service)/'finance_gate.py').read_text(encoding='utf8')


def _blocked(result, *, unknown=False, minimum=1, recoverable=False):
    if recoverable and result.get('kind')=='gate' and result.get('reason') in ('busy','cooldown'):
        delay=result.get('retry_after_seconds')
        assert type(delay) is int and delay>=minimum, result
        return
    if result.get('kind') == 'gate':
        if unknown:
            assert result.get('reason') == 'retry_time_unknown', result
        else:
            assert result.get('reason') == 'cooldown', result
            assert type(result.get('retry_after_seconds')) is int and result['retry_after_seconds'] >= minimum, result
    else:
        assert not unknown and result.get('kind') == 'http' and result.get('status') == 429, result
        assert type(result.get('retry_after_seconds')) is int and result['retry_after_seconds'] >= minimum, result


def _success(result, provider, mode):
    assert result.get('kind') == 'ok', result
    value = result['result']
    if provider == 'ozon' and mode == 'legacy':
        assert value['ordered_amount'] == '123.45' and value['ordered_units'] == '2', value
    elif provider == 'wb' and mode == 'legacy':
        assert value['items'][0]['nmID'] == 123, value
    elif provider == 'ozon':
        metrics = value['result']['data'][0]['metrics']
        assert str(metrics[0]) == '123.45' and metrics[1] == 2, value
    else:
        assert value['cards'][0]['nmID'] == 123, value


def _exercise(python, source, source_mode, target, target_mode, data, provider, reply):
    source_result = _probe(python, source, data, source_mode, provider=provider, reply=reply,
                           exit_code=73 if reply == 'crash' else 0)
    if reply != 'crash':
        assert source_result.get('kind') == 'http' and source_result.get('status') == 429, source_result
    assert _attempt_count(data) == 1
    unknown = reply != 'known429'
    for tick, wall, boot, rotated in ((17, 17, BOOT, False), (17, 17, BOOT, True),
                                      (17, 10000, BOOT, True)):
        result = _probe(python, target, data, target_mode, provider=provider, tick=tick, wall=wall,
                        boot=boot, rotated=rotated)
        _blocked(result, unknown=unknown, minimum=RETRY - tick,
                 recoverable=bounded_reads(target) or (bounded_reads(source) and reply=='unknown429'))
        assert _attempt_count(data) == 1, 'provider contacted before cooldown ended'
    if not unknown:
        result = _probe(python, target, data, target_mode, provider=provider, tick=17,
                        boot='b' * 64, rotated=2)
        _blocked(result, unknown=True,minimum=RETRY-17,recoverable=bounded_reads(target))
        assert _attempt_count(data) == 1, 'provider contacted with unknown post-boot cooldown'
        result = _probe(python, target, data, target_mode, provider=provider, tick=RETRY + 1)
        _success(result, provider, target_mode)
        assert _target_count(data, provider) == 2
    else:
        result = _probe(python, target, data, target_mode, provider=provider,
                        tick=86401, rotated=True)
        if bounded_reads(target) or (bounded_reads(source) and reply=='unknown429'):
            _success(result,provider,target_mode)
            assert _target_count(data,provider)==2, 'read did not resume at the end of the recovery interval'
        else:
            _blocked(result, unknown=True)
            assert _attempt_count(data) == 1, 'provider contacted with unknown cooldown'


def _profile_exercise(python, source, target, data):
    first = _probe(python, source, data, 'legacy', provider='ozon', reply='profile_unknown429')
    _success(first, 'ozon', 'legacy')
    assert _target_count(data, 'ozon') == 1 and _profile_count(data) == 1
    assert 'company_currency' not in first['result']
    second = _probe(python, target, data, 'legacy', provider='ozon', tick=2, rotated=True)
    _success(second, 'ozon', 'legacy')
    assert _target_count(data, 'ozon') == 2 and _profile_count(data) == 1, 'profile retry bypassed guard'
    assert 'company_currency' not in second['result']


def _submission_exercise(python, service, data, provider):
    # The provider has returned HTTP 200, but the worker dies before it can
    # read and persist the task ID. A rotated credential must not create it again.
    _probe(python, service, data, 'submission', provider=provider,
           reply='body_crash', exit_code=73)
    assert _attempt_count(data) == 1
    for rotated in (False, True):
        result = _probe(python, service, data, 'submission', provider=provider, tick=1, rotated=rotated)
        assert result.get('kind') == 'gate' and result.get('reason') in ('retry_time_unknown', 'busy'), result
        assert _attempt_count(data) == 1, 'unknown report submission was repeated'
    if provider == 'performance':
        result = _probe(python, service, data, 'submission', provider=provider,
                        tick=1, rotated=True, other=True)
        assert result.get('kind') == 'gate' and result.get('reason') in ('retry_time_unknown', 'busy'), result
        assert _attempt_count(data) == 1, 'another Performance job bypassed the active slot'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-service', type=Path, required=True)
    parser.add_argument('--bridge-service', type=Path, required=True)
    parser.add_argument('--full-service', type=Path)
    parser.add_argument('--python', type=Path, required=True)
    args = parser.parse_args()
    # Do not resolve a venv's interpreter symlink to the system interpreter.
    python = args.python.absolute()
    if not python.is_file():
        parser.error('--python must be an existing interpreter')
    baseline = args.baseline_service.resolve(strict=True)
    bridge = args.bridge_service.resolve(strict=True)
    full = args.full_service.resolve(strict=True) if args.full_service else None
    if _service_digest(baseline) != BASELINE_SERVICE_SHA256:
        raise AssertionError('baseline differs from the exact c8b2257 service inventory')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        baseline_names = _probe(python, baseline, root, 'catalog')
        bridge_names = _probe(python, bridge, root, 'catalog')
        assert baseline_names == bridge_names, 'bridge changes the legacy MCP tool catalogue'
        if full is not None:
            full_names = _probe(python, full, root, 'catalog')
            assert set(bridge_names) < set(full_names), 'full service lacks the expanded MCP catalogue'
        print('PASS expected baseline, bridge and optional expanded tool catalogues', flush=True)

        for provider in ('ozon', 'wb'):
            data = root / ('baseline-' + provider)
            data.mkdir(mode=0o700)
            result = _probe(python, baseline, data, 'legacy', provider=provider,
                            reply='known429', baseline=True)
            assert result.get('kind') == 'http' and result.get('status') == 429, result
            assert _attempt_count(data) == 1
        print('PASS exact c8b2257 workers reach only the offline provider fixture', flush=True)

        hold = root / 'hold'
        hold.mkdir(mode=0o700)
        (hold / '.market-cutover-pending').write_text('synthetic', encoding='ascii')
        for provider in ('ozon', 'wb'):
            result = _probe(python, bridge, hold, 'legacy', provider=provider)
            assert result.get('kind') == 'gate' and result.get('reason') in (
                'busy', 'storage_unavailable', 'retry_time_unknown'), result
            assert _attempt_count(hold) == 0, 'provider contacted while bootstrap hold exists'
        print('PASS bridge blocks outbound legacy reads while the bootstrap hold exists', flush=True)

        pairs = [('bridge-bridge', bridge, 'legacy', bridge, 'legacy')]
        if full is not None:
            pairs.extend((('bridge-full-wire', bridge, 'legacy', full, 'wire'),
                          ('full-wire-bridge', full, 'wire', bridge, 'legacy'),
                          ('bridge-full-legacy', bridge, 'legacy', full, 'legacy'),
                          ('full-legacy-bridge', full, 'legacy', bridge, 'legacy')))
        for label, source, source_mode, target, target_mode in pairs:
            for provider in ('ozon', 'wb'):
                for reply in ('known429', 'unknown429', 'crash'):
                    data = root / (label + '-' + provider + '-' + reply)
                    data.mkdir(mode=0o700)
                    _exercise(python, source, source_mode, target, target_mode, data, provider, reply)
            print('PASS', label, 'known/unknown cooldown, crash, clock, reboot and key change', flush=True)
        profile_pairs = [('bridge-bridge-profile', bridge, bridge)]
        if full is not None:
            profile_pairs.extend((('bridge-full-profile', bridge, full), ('full-bridge-profile', full, bridge)))
        for label, source, target in profile_pairs:
            data = root / label
            data.mkdir(mode=0o700)
            _profile_exercise(python, source, target, data)
        print('PASS seller-info thread retains its own unknown cooldown', flush=True)
        if full is not None:
            for provider in ('wb', 'performance'):
                data = root / ('submission-' + provider)
                data.mkdir(mode=0o700)
                _submission_exercise(python, full, data, provider)
            print('PASS unknown report submissions survive worker death and credential rotation', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--_child']:
        _child()
    else:
        main()
