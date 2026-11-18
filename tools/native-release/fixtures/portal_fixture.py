"""Synthetic Gunicorn application for the systemd deployment acceptance test."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0, os.getcwd())
import finance_gate

data = Path(os.environ['VAULT_PATH']).parent
release_pair = os.environ.get('WBOZON_QA_RELEASE_PAIR') == '1'


def provider():
    if release_pair:
        from transition_probe import worker
        result = worker(data, 'ok', sys.argv[3], hold=float(sys.argv[2]))
        assert result['kind'] == 'ok', result['kind']
        with (data / 'completed').open('a') as f:
            f.write('finished\n')
        return
    gate = str(data / '.ozon-finance-gates')
    def read():
        (data / 'started').touch()
        time.sleep(float(sys.argv[2]))
        with (data / 'completed').open('a') as f:
            f.write('finished\n')
    finance_gate.run(gate, '1234', read)


def application(env, start):
    route = env['PATH_INFO']
    if route == '/market-cutover':
        import market_cooldown
        import urllib.request
        request = urllib.request.Request('https://content-api.wildberries.ru/content/v2/get/cards/list')
        try:
            result = market_cooldown.request(str(data / '.ozon-finance-gates'), 'wb', {'api_token':'fixture'},
                                             request, lambda: b'sent')
        except finance_gate.GateError as exc:
            if exc.reason != 'busy':
                raise
            result = b'held'
        start('200 OK', [])
        return [result]
    if route == '/healthz':
        if (data / ('fail-' + Path.cwd().parent.name)).exists():
            start('503 Service Unavailable', [])
            return [b'{}']
        start('200 OK', [('Content-Type', 'application/json')])
        return [b'{"status":"ok"}']
    if route == '/detached':
        (data / 'detached-handler').touch()
        time.sleep(7)
    if release_pair:
        from transition_probe import activity
        if route in ('/activity-candidate','/activity-restored'):
            activity(data,route.removeprefix('/activity-'))
            start('200 OK', [])
            return [b'checked']
        if route == '/seed':
            activity(data,'seed')
    else:
        for name, tables in (('auth.sqlite', ['tokens', 'sessions']), ('vault.sqlite', ['vault', 'connection_checks'])):
            with sqlite3.connect(data / name) as db:
                for table in tables:
                    db.execute('CREATE TABLE IF NOT EXISTS ' + table + ' (id INTEGER PRIMARY KEY)')
    duration = {'/hold':'3','/rollback-hold':'6'}.get(route,'0')
    subprocess.run([sys.executable, __file__, '--provider-worker', duration, route.lstrip('/')], check=True)
    start('200 OK', [])
    return [b'finished']


if __name__ == '__main__' and sys.argv[1] == '--provider-worker':
    provider()
