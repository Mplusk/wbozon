"""Read-only provider model discovery. Scheduling and vault access belong to the caller.

The catalogue is a list of IDs, not a compatibility, pricing or entitlement check.
Failures preserve the last successful snapshot. API secrets never enter SQLite.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

_PROVIDERS = frozenset(('openai', 'gemini'))
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9_+=/.-]{1,512}\Z')
_LIMIT = 1024 * 1024


def _safe_id(name, api_key):
    # Provider content is untrusted: it must not reflect credentials into the UI.
    return (isinstance(name, str) and bool(_MODEL_ID.fullmatch(name))
            and api_key not in name and not re.search(r'sk-|AIza', name, re.IGNORECASE))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(url, headers):
    req = urllib.request.Request(url, headers=headers, method='GET')
    # Do not send provider keys through environment-supplied HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=10) as response:
        data = response.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ValueError('Provider response too large')
    return json.loads(data)


def discover(provider, api_key, *, get=_http):
    """Get IDs using fixed HTTPS destinations; never expose transport errors."""
    if provider not in _PROVIDERS or not isinstance(api_key, str) or not re.fullmatch(r'[!-~]{8,2048}', api_key):
        raise ValueError('Invalid provider configuration')
    try:
        if provider == 'openai':
            result = get('https://api.openai.com/v1/models', {'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json'})
            rows = result['data']
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError()
            names = [row.get('id') for row in rows if isinstance(row, dict)]
        else:
            names, token, seen = [], None, set()
            for _ in range(20):
                query = {'pageSize': '100'}
                if token is not None:
                    query['pageToken'] = token
                result = get('https://generativelanguage.googleapis.com/v1beta/models?' + urllib.parse.urlencode(query),
                             {'x-goog-api-key': api_key, 'Accept': 'application/json'})
                rows = result['models']
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError()
                names.extend(row.get('name') for row in rows if isinstance(row, dict))
                token = result.get('nextPageToken')
                if not token:
                    break
                if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token in seen:
                    raise ValueError()
                seen.add(token)
            else:
                raise ValueError()
        if len(names) > 10000 or any(not _safe_id(n, api_key) for n in names):
            raise ValueError()
        return sorted(set(names))
    except Exception:
        raise RuntimeError('Provider catalogue unavailable') from None


class Catalog:
    """SQLite cache, segregated from vault; fingerprint prevents cross-key reuse."""
    def __init__(self, path, binding_key, *, read_only=False):
        if not isinstance(binding_key, bytes) or len(binding_key) < 32:
            raise ValueError('Private catalogue binding key required')
        self.path, self.binding_key, self.read_only = path, binding_key, read_only is True
        if self.read_only:
            with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {'catalogue'}: raise ValueError('Invalid catalogue database')
                columns = [(row[1], row[2].upper(), row[3], row[5]) for row in db.execute('PRAGMA table_info(catalogue)')]
                if columns != [('provider', 'TEXT', 0, 1), ('binding', 'TEXT', 1, 0),
                               ('updated', 'INTEGER', 1, 0), ('ids', 'TEXT', 1, 0)]:
                    raise ValueError('Invalid catalogue schema')
        else:
            with closing(sqlite3.connect(path, timeout=1)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS catalogue (provider TEXT PRIMARY KEY, binding TEXT NOT NULL, updated INTEGER NOT NULL, ids TEXT NOT NULL)')
                db.commit()

    def _binding(self, provider, api_key):
        return hmac.new(self.binding_key, (provider + '\0' + api_key).encode(), hashlib.sha256).hexdigest()

    def refresh(self, provider, api_key, *, get=_http, now=time.time):
        if self.read_only:
            raise ValueError('Read-only catalogue')
        ids = discover(provider, api_key, get=get)
        binding = self._binding(provider, api_key)
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute('INSERT OR REPLACE INTO catalogue(provider,binding,updated,ids) VALUES (?,?,?,?)',
                       (provider, binding, int(now()), json.dumps(ids, separators=(',', ':'))))
            db.commit()
        return len(ids)

    def read(self, provider, api_key):
        if provider not in _PROVIDERS or not isinstance(api_key, str):
            return None
        db_path = Path(self.path).as_uri() + '?mode=ro' if self.read_only else self.path
        with closing(sqlite3.connect(db_path, uri=self.read_only, timeout=.2 if self.read_only else 1)) as db:
            row = db.execute('SELECT binding,updated,ids FROM catalogue WHERE provider=?', (provider,)).fetchone()
        if row is None or not hmac.compare_digest(row[0], self._binding(provider, api_key)):
            return None
        ids = json.loads(row[2])
        if not isinstance(ids, list) or len(ids) > 10000 or any(not _safe_id(n, api_key) for n in ids):
            raise ValueError('Invalid catalogue')
        return {'updated_at': row[1], 'ids': ids}
