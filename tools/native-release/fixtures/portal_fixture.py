"""Synthetic Gunicorn application for the systemd deployment acceptance test."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import finance_gate

data = Path(os.environ['VAULT_PATH']).parent


def provider():
    gate = str(data / '.ozon-finance-gates')
    def read():
        (data / 'started').touch()
        time.sleep(float(sys.argv[2]))
        with (data / 'completed').open('a') as f:
            f.write('finished\n')
    finance_gate.run(gate, '1234', read)


def application(env, start):
    route = env['PATH_INFO']
    if route == '/healthz':
        if (data / ('fail-' + Path.cwd().parent.name)).exists():
            start('503 Service Unavailable', [])
            return [b'{}']
        start('200 OK', [('Content-Type', 'application/json')])
        return [b'{"status":"ok"}']
    for name, tables in (('auth.sqlite', ['tokens', 'sessions']), ('vault.sqlite', ['vault', 'connection_checks'])):
        with sqlite3.connect(data / name) as db:
            for table in tables:
                db.execute('CREATE TABLE IF NOT EXISTS ' + table + ' (id INTEGER PRIMARY KEY)')
    subprocess.run([sys.executable, __file__, '--provider-worker', '3' if route == '/hold' else '0'], check=True)
    start('200 OK', [])
    return [b'finished']


if __name__ == '__main__' and sys.argv[1] == '--provider-worker':
    provider()
