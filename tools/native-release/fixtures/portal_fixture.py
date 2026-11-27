"""Synthetic Gunicorn application for the systemd deployment acceptance test."""
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
sys.path.insert(0, os.getcwd())
import finance_gate

data = Path(os.environ['VAULT_PATH']).parent
release_pair = os.environ.get('WBOZON_QA_RELEASE_PAIR') == '1'
restore_fixture = os.environ.get('WBOZON_QA_RESTORE') == '1'
wal_reader = None


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
    if restore_fixture and route in ('/seed', '/restore-write'):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = b'c' * 32
        def sealed(provider, value):
            nonce = os.urandom(12)
            return nonce + AESGCM(key).encrypt(nonce, value, provider.encode())
        if route == '/seed':
            with sqlite3.connect(data / 'auth.sqlite') as db:
                db.executescript('CREATE TABLE IF NOT EXISTS tokens(hash TEXT); CREATE TABLE IF NOT EXISTS sessions(hash TEXT);'
                                 'CREATE TABLE IF NOT EXISTS attempts(bucket TEXT); INSERT INTO sessions VALUES (\'test-session\');')
            with sqlite3.connect(data / 'vault.sqlite') as db:
                db.executescript('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY,blob BLOB NOT NULL);'
                    'CREATE TABLE IF NOT EXISTS connection_checks(provider TEXT);'
                    'CREATE TABLE IF NOT EXISTS external_mcp_keys(public_id TEXT,revoked_at REAL);'
                    'CREATE TABLE IF NOT EXISTS oauth_codes(hash TEXT); CREATE TABLE IF NOT EXISTS oauth_tokens(hash TEXT);')
                for provider, value in (('yandex_backup', b'test-settings'), ('yandex_backup_metadata', b'test-metadata'),
                                        ('ozon', b'before-restore')):
                    db.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', (provider, sealed(provider, value)))
            for name in ('auth.sqlite', 'vault.sqlite'):
                os.chmod(data / name, 0o600)
        else:
            with sqlite3.connect(data / 'vault.sqlite') as db:
                db.execute('UPDATE vault SET blob=? WHERE provider=?', (sealed('ozon', b'after-admission'), 'ozon'))
            start('200 OK', [])
            return [b'changed']
    if route in ('/interrupt-finance', '/check-interrupted-finance'):
        if route == '/interrupt-finance':
            child = subprocess.run([sys.executable, __file__, '--interrupt-finance'])
            assert child.returncode == 73
        def unexpected_request():
            raise AssertionError('replayed an interrupted finance request')
        try:
            finance_gate.run(str(data / '.ozon-finance-gates'), '7654321', unexpected_request)
        except finance_gate.GateError as exc:
            if exc.reason != 'retry_time_unknown':
                raise
        else:
            raise AssertionError('missing interrupted finance gate')
        start('200 OK', [])
        return [b'held']
    if route in ('/market-limit-wb', '/market-limit-ozon'):
        import importlib.util
        import market_cooldown
        import urllib.error
        import urllib.request
        provider = route.rsplit('-', 1)[1]
        creds = {'api_token':'fixture-token'} if provider == 'wb' else {'client_id':'101','api_key':'fixture-key'}
        url = ('https://content-api.wildberries.ru/content/v2/get/cards/list' if provider == 'wb'
               else 'https://api-seller.ozon.ru/v3/product/info/list')
        gate = str(data / '.ozon-finance-gates')
        class Offline:
            def open(self, req, timeout):
                with (data / ('attempts-' + provider)).open('a') as f:
                    f.write('attempt\n')
                raise urllib.error.HTTPError('', 429, 'synthetic', {'Retry-After':'3600'}, None)
        try:
            if importlib.util.find_spec('market_transport'):
                from market_transport import Wire
                wire = Wire(provider, creds, gate, time.monotonic()+5)
                wire.opener = Offline()
                wire('POST', url, body={}, interval=0)
            else:
                req = urllib.request.Request(url, data=b'{}')
                with market_cooldown.worker(gate, creds):
                    market_cooldown.legacy_open(Offline(), req, 5)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            result = b'recorded'
        except finance_gate.GateError as exc:
            if exc.reason != 'cooldown' or exc.retry_after_seconds <= 0:
                raise
            result = b'held'
        else:
            raise AssertionError('mock provider did not return its expected rate limit')
        start('200 OK', [])
        return [result]
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
        if route == '/seed' and (Path.cwd() / 'avito_mcp.py').is_file():
            from external_keys import KeyStore
            global wal_reader
            keys = KeyStore(data / 'vault.sqlite')
            wal_reader = sqlite3.connect(data / 'vault.sqlite.avito-keys')
            assert wal_reader.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
            wal_reader.execute('BEGIN')
            wal_reader.execute('SELECT COUNT(*) FROM external_mcp_keys').fetchone()
            keys.create('Avito fixture', secrets.token_urlsafe(16), time.time(), 'avito')
    duration = {'/hold':'3','/rollback-hold':'6'}.get(route,'0')
    subprocess.run([sys.executable, __file__, '--provider-worker', duration, route.lstrip('/')], check=True)
    start('200 OK', [])
    return [b'finished']


if __name__ == '__main__' and sys.argv[1] == '--provider-worker':
    provider()
elif __name__ == '__main__' and sys.argv[1] == '--interrupt-finance':
    def interrupted_request():
        os._exit(73)
    finance_gate.run(str(data / '.ozon-finance-gates'), '7654321', interrupted_request)
