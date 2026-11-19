"""Apply the same durable provider deadline to reports and legacy HTTP requests."""
import contextlib
import contextvars
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
from urllib.parse import urlsplit

import finance_gate


_service = Path(__file__).resolve().parent
# Only native, immutable deployments have a controller-owned bootstrap fence.
BOOTSTRAP_HOLD = (
    _service.parents[2] / '.market-cutover-pending'
    if _service.name == 'service' and _service.parent.parent.name == 'releases'
    else None
)
_worker = contextvars.ContextVar('market_worker', default=None)


@contextlib.contextmanager
def worker(directory, credentials):
    token = _worker.set((directory, credentials))
    try:
        yield
    finally:
        _worker.reset(token)


def method_key(provider, credentials, url):
    parts = urlsplit(url)
    path = re.sub(r'^/v[0-9]+/posting/(fbs|fbo)/list$', r'/posting/\1/list', parts.path)
    path = re.sub(r'^(/api/v1/supplies)/[0-9]+(?=/|$)', r'\1/{supply}', path)
    path = re.sub(r'/tasks/[^/]+/', '/tasks/{task}/', path)
    path = re.sub(r'^/api/client/statistics/(?!report$)[^/]+$', '/api/client/statistics/{task}', path)
    if parts.hostname == 'feedbacks-api.wildberries.ru':
        path = '/api/v1/feedbacks'
    # There is one WB vault per portal. Token rotation must respect the same deadline.
    account = 'cabinet' if provider == 'wb' else str(credentials.get('client_id') or 'cabinet')
    if account.isascii() and account.isdigit():
        account = account.lstrip('0') or '0'
    return json.dumps([provider, account, parts.hostname, path], ensure_ascii=True, separators=(',', ':'))


class MethodLimited(finance_gate.GateError, finance_gate.KnownProviderResponseError):
    """No HTTP request was sent by this attempt. Retain its own method deadline."""


def _analytics_slot(directory, credentials):
    # Preserve the existing credential-scoped report records and include their
    # previous requests in the seller-wide rolling 24 hour window after upgrade.
    folder = Path(directory).parent / '.market-reports'
    folder.mkdir(mode=0o700, exist_ok=True)
    account = str(credentials['client_id']).lstrip('0') or '0'
    stable = hashlib.sha256(('analytics-seller-v1:' + account).encode()).hexdigest()
    original = hashlib.sha256(json.dumps(['ozon', credentials], ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    with contextlib.closing(sqlite3.connect(folder / 'reports.db', timeout=1)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS analytics_requests(scope TEXT NOT NULL, at REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS analytics_window(scope TEXT NOT NULL, boot TEXT NOT NULL,'
                   ' wall REAL NOT NULL, mono REAL NOT NULL)')
        now, mono, boot = finance_gate.time.time(), finance_gate.time.monotonic(), finance_gate._boot_identity()
        # Older rows carry only a wall-clock timestamp. Anchor them once, conservatively,
        # to the current boot. They must not disappear on either a reboot or a clock jump.
        old = db.execute('SELECT 1 FROM analytics_requests WHERE scope IN (?,?) LIMIT 50',
                         (stable, original)).fetchall()
        if old:
            db.executemany('INSERT INTO analytics_window VALUES(?,?,?,?)', [(stable, boot, now, mono)] * len(old))
            db.execute('DELETE FROM analytics_requests WHERE scope IN (?,?)', (stable, original))
        db.execute('DELETE FROM analytics_window WHERE scope=? AND wall<=? AND '
                   '((boot=? AND mono<=?) OR (boot<>? AND ?>=86400))',
                   (stable, now - 86400, boot, mono - 86400, boot, mono))
        remaining = [max(wall + 86400 - now, (start if anchor == boot else 0) + 86400 - mono)
                     for anchor, wall, start in db.execute('SELECT boot,wall,mono FROM analytics_window WHERE scope=?',
                                                          (stable,))]
        if len(remaining) < 50:
            db.execute('INSERT INTO analytics_window VALUES(?,?,?,?)', (stable, boot, now, mono))
    # Commit a legacy import even when it fills the entire window.
    if len(remaining) >= 50:
        delay = max(1, math.ceil(sorted(remaining)[len(remaining) - 50]))
        raise urllib.error.HTTPError('', 429, 'Analytics daily limit', {'Retry-After': str(delay)}, None)


def request(directory, provider, credentials, req, send, *, before_send=None):
    if BOOTSTRAP_HOLD is not None:
        try:
            BOOTSTRAP_HOLD.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise finance_gate.GateError('storage_unavailable') from None
        else:
            raise MethodLimited('busy', 1)
    if not directory or not os.path.isabs(directory):
        raise finance_gate.GateError('storage_unavailable')
    url = req.full_url
    def prepare():
        if before_send is not None:
            before_send()
        if provider == 'ozon' and urlsplit(url).path == '/v1/analytics/data':
            _analytics_slot(directory, credentials)

    def open_http():
        try:
            return send()
        except urllib.error.HTTPError as exc:
            if provider == 'wb' and exc.code == 429 and exc.headers is not None and 'Retry-After' not in exc.headers:
                retry = finance_gate._single_header(exc.headers, 'X-Ratelimit-Retry')
                if isinstance(retry, str) and retry.isascii() and retry.isdigit() and len(retry) < 10:
                    exc.headers['Retry-After'] = retry
            raise

    root = str(Path(directory).parent / '.market-method-gates')
    try:
        return finance_gate.run(root, method_key(provider, credentials, url), open_http,
            (credentials.get('api_key'), credentials.get('api_token'), credentials.get('client_secret'), credentials.get('digital_token')),
            before_send=prepare)
    except finance_gate.GateError as exc:
        if exc.reason in ('busy', 'cooldown', 'retry_time_unknown'):
            raise MethodLimited(exc.reason, exc.retry_after_seconds) from None
        raise


def legacy_open(opener, req, timeout):
    active = _worker.get()
    if active is None:
        # Pure projection callers may inject an opener without starting a worker.
        return opener.open(req, timeout=timeout)
    directory, credentials = active
    provider = 'wb' if 'api_token' in credentials else 'ozon'
    return request(directory, provider, credentials, req, lambda: opener.open(req, timeout=timeout))
