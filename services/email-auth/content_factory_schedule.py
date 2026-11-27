"""Offline scheduling primitives. No network, provider keys, or dispatch.

This module is deliberately not wired into the portal or production n8n.
Before switching from the existing workflow, reconcile CF_BudgetLedger and
the existing publish queue; include this database in the release/backup policy.
A claimed slot is a reservation, not proof of dispatch: an outbox and explicit
reconciliation are required to handle uncertain external outcomes.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
FIELDS = frozenset(('enabled', 'account_id', 'product_key', 'publication_time',
                    'generation_lead_minutes', 'video_weekdays', 'models',
                    'image_estimate_cents', 'video_estimate_cents', 'monthly_budget_cents'))
MODEL_FIELDS = frozenset(('text', 'image', 'video', 'quality'))
# Only the four identifiers recorded in the current pilot design; availability
# and provider support still need confirmation before enabling their use.
MODEL_ALLOWLIST = {
    'text': frozenset(('gpt-5.4-mini',)),
    'image': frozenset(('models/gemini-3.1-flash-image',)),
    'video': frozenset(('gemini-omni-1.2-flash',)),
    'quality': frozenset(('gemini-3.8-flash',)),
}


class Conflict(ValueError):
    """The submitted revision no longer matches the persisted one."""


def validate(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError('invalid settings fields')
    if type(config['enabled']) is not bool:
        raise ValueError('invalid enabled flag')
    if not isinstance(config['account_id'], str) or not re.fullmatch(r'[0-9]{1,32}', config['account_id']):
        raise ValueError('invalid account id')
    if not isinstance(config['product_key'], str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', config['product_key']):
        raise ValueError('invalid product key')
    if not isinstance(config['publication_time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', config['publication_time']):
        raise ValueError('invalid publication time')
    lead = config['generation_lead_minutes']
    if type(lead) is not int or not 1 <= lead <= 1440:
        raise ValueError('invalid generation lead')
    days = config['video_weekdays']
    if not isinstance(days, list) or any(type(day) is not int or day not in range(1, 8) for day in days) or len(set(days)) != len(days):
        raise ValueError('invalid weekdays')
    models = config['models']
    if not isinstance(models, dict) or set(models) != MODEL_FIELDS:
        raise ValueError('invalid model fields')
    for field, value in models.items():
        if not isinstance(value, str) or value not in MODEL_ALLOWLIST[field]:
            raise ValueError('invalid model identifier')
    for field in ('image_estimate_cents', 'video_estimate_cents', 'monthly_budget_cents'):
        value = config[field]
        if type(value) is not int or not 1 <= value <= 100_000_000:
            raise ValueError('invalid estimated budget')
    # Canonical copy prevents caller-side mutation from altering this snapshot.
    return json.loads(json.dumps(config, ensure_ascii=True, sort_keys=True))


def _slot(now, config):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    if not config['enabled']:
        return None
    local = now.astimezone(MOSCOW)
    hour, minute = map(int, config['publication_time'].split(':'))
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        scheduled = datetime(day.year, day.month, day.day, hour, minute, tzinfo=MOSCOW)
        start = scheduled - timedelta(minutes=config['generation_lead_minutes'])
        if start <= local < min(start + timedelta(minutes=1), scheduled):
            return day.isoformat(), scheduled.astimezone(timezone.utc).isoformat(), ('reel' if day.isoweekday() in config['video_weekdays'] else 'image')
    return None


class Store:
    def __init__(self, path):
        self.path = str(path)
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cf_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL CHECK(revision>0), settings_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cf_slots (
                    account_id TEXT NOT NULL, local_date TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL, revision INTEGER NOT NULL,
                    settings_json TEXT NOT NULL, media_type TEXT NOT NULL CHECK(media_type IN ('image','reel')),
                    state TEXT NOT NULL CHECK(state='claimed'), PRIMARY KEY(account_id, local_date));
                CREATE TABLE IF NOT EXISTS cf_reservations (
                    account_id TEXT NOT NULL, local_date TEXT NOT NULL, month_key TEXT NOT NULL,
                    estimated_cents INTEGER NOT NULL CHECK(estimated_cents>0),
                    PRIMARY KEY(account_id, local_date),
                    FOREIGN KEY(account_id, local_date) REFERENCES cf_slots(account_id, local_date));
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        return db

    def get(self):
        with closing(self._connect()) as db:
            row = db.execute('SELECT revision,settings_json FROM cf_settings WHERE singleton=1').fetchone()
        if not row:
            return None
        return {'revision': row[0], 'settings': validate(json.loads(row[1]))}

    def save(self, config, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('invalid expected revision')
        payload = json.dumps(validate(config), ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        with closing(self._connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision FROM cf_settings WHERE singleton=1').fetchone()
                actual = row[0] if row else 0
                if actual != expected_revision:
                    raise Conflict('settings changed')
                revision = actual + 1
                db.execute('INSERT INTO cf_settings VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,settings_json=excluded.settings_json', (revision, payload))
                db.commit()
                return revision
            except BaseException:
                db.rollback()
                raise

    def claim_due(self, now):
        """Claim current settings and estimated budget together; no dispatch implied."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('now must be timezone-aware')
        with closing(self._connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT revision,settings_json FROM cf_settings WHERE singleton=1').fetchone()
                if not row:
                    db.rollback()
                    return {'claimed': False, 'reason': 'not_configured'}
                revision, raw = row
                config = validate(json.loads(raw))
                slot = _slot(now, config)
                if slot is None:
                    db.rollback()
                    return {'claimed': False, 'reason': 'disabled' if not config['enabled'] else 'not_due'}
                day, scheduled, media = slot
                account = config['account_id']
                if db.execute('SELECT 1 FROM cf_slots WHERE account_id=? AND local_date=?', (account, day)).fetchone():
                    db.rollback()
                    return {'claimed': False, 'reason': 'already_claimed'}
                month = now.astimezone(MOSCOW).strftime('%Y-%m')
                used = db.execute('SELECT COALESCE(SUM(estimated_cents),0) FROM cf_reservations WHERE month_key=?', (month,)).fetchone()[0]
                amount = config['video_estimate_cents'] if media == 'reel' else config['image_estimate_cents']
                if used + amount > config['monthly_budget_cents']:
                    db.rollback()
                    return {'claimed': False, 'reason': 'budget_exhausted'}
                db.execute('INSERT INTO cf_slots VALUES (?,?,?,?,?,?,?)', (account, day, scheduled, revision, raw, media, 'claimed'))
                db.execute('INSERT INTO cf_reservations VALUES (?,?,?,?)', (account, day, month, amount))
                db.commit()
                return {'claimed': True, 'local_date': day, 'scheduled_at_utc': scheduled,
                        'revision': revision, 'media_type': media, 'estimate_cents': amount}
            except BaseException:
                db.rollback()
                raise
