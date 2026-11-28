"""Once-per-Moscow-day catalogue refresh; caller supplies vault and isolated DB.

The timer/process identity and secret-file handling are deployment concerns.  This
module must never be wired to request-supplied paths or emit provider exceptions.
"""
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SLOTS = {'openai': 'content_factory_catalog_openai', 'gemini': 'content_factory_catalog_gemini'}


def last_attempts(lock_path):
    """Read the independent scheduler journal without creating or changing it."""
    with closing(sqlite3.connect(Path(lock_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
        rows = db.execute('SELECT provider,attempted_at,outcome FROM catalogue_attempts').fetchall()
    result = {}
    for provider, attempted_at, outcome in rows:
        if (provider not in SLOTS or type(attempted_at) is not int or attempted_at < 0
                or outcome not in ('refreshed', 'unavailable', 'not_configured')):
            raise ValueError('Invalid scheduler status')
        result[provider] = {'attempted_at': attempted_at, 'outcome': outcome}
    return result


def run_once(vault, catalogue, lock_path, *, now=time.time):
    """Perform at most one attempt per day, serialized across processes.

    The dedicated database is not the vault, catalogue, or another gateway DB.
    A crash rolls back the run marker; repeat consists only of provider GETs.
    """
    local_time = datetime.fromtimestamp(now(), timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
    # Timer may poll after a missed portal start. Never perform the daily
    # provider calls before the advertised 03:00 Moscow window.
    if local_time.hour < 3:
        return {'status': 'not_due'}
    day = local_time.date().isoformat()
    with closing(sqlite3.connect(lock_path, timeout=0.2)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS catalogue_runs(day TEXT PRIMARY KEY)')
        db.commit()
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            return {'status': 'busy'}
        if db.execute('SELECT 1 FROM catalogue_runs WHERE day=?', (day,)).fetchone():
            db.rollback()
            return {'status': 'already_attempted'}
        db.execute('CREATE TABLE IF NOT EXISTS catalogue_attempts(provider TEXT PRIMARY KEY, attempted_at INTEGER NOT NULL, outcome TEXT NOT NULL)')
        outcomes = {}
        for provider, slot in SLOTS.items():
            try:
                saved = vault.read(slot)
                key = saved.get('api_key') if isinstance(saved, dict) else None
                if not isinstance(key, str) or not key:
                    outcomes[provider] = 'not_configured'
                    continue
                catalogue.refresh(provider, key)
                # The catalogue is key-bound. If the owner replaced the key during
                # the request, the old response remains invisible to the new key.
                outcomes[provider] = 'refreshed'
            except Exception:
                outcomes[provider] = 'unavailable'
        attempted_at = int(now())
        for provider, outcome in outcomes.items():
            db.execute('INSERT OR REPLACE INTO catalogue_attempts(provider,attempted_at,outcome) VALUES (?,?,?)',
                       (provider, attempted_at, outcome))
        db.execute('INSERT INTO catalogue_runs(day) VALUES (?)', (day,))
        db.commit()
    return {'status': 'attempted', 'providers': outcomes}
