"""Transactional reservation ledger for the content factory; not yet connected.

All public methods are server-side building blocks, not HTTP handlers. The
composition root must authenticate the owner, verify provider-account baseline,
enforce the exact bounded request manifest and supply a trusted quote builder.
Absent a quote builder, reservations always fail closed. Estimates are NOT
provider invoices or a guarantee against out-of-band provider spending.
"""
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LIMIT = 100_000_000


class BudgetUnavailable(Exception):
    pass


class BudgetStore:
    def __init__(self, path, *, quote_builder=None, clock=None):
        self.path = str(path)
        self.quote_builder = quote_builder
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(self._db()) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cf_budget_settings (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              revision INTEGER NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
              limit_cents INTEGER NOT NULL CHECK(limit_cents>=0));
            INSERT OR IGNORE INTO cf_budget_settings VALUES (1,0,0,0);
            CREATE TABLE IF NOT EXISTS cf_budget_baseline (
              month TEXT PRIMARY KEY, cents INTEGER NOT NULL CHECK(cents>=0),
              source TEXT NOT NULL, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cf_budget_reservations (
              request_id TEXT PRIMARY KEY, month TEXT NOT NULL, graph_hash TEXT NOT NULL,
              cents INTEGER NOT NULL CHECK(cents>0), rate_date TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('reserved','dispatching','unknown','completed','cancelled')));
            CREATE TABLE IF NOT EXISTS cf_budget_proposals (
              id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, revision INTEGER NOT NULL,
              before_cents INTEGER NOT NULL, after_cents INTEGER NOT NULL,
              expires_utc INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','confirmed')));
            CREATE TABLE IF NOT EXISTS cf_budget_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at_utc INTEGER NOT NULL,
              owner_hash TEXT NOT NULL, proposal_id TEXT NOT NULL, event TEXT NOT NULL,
              revision INTEGER NOT NULL, before_cents INTEGER NOT NULL, after_cents INTEGER NOT NULL);
            ''')

    def _db(self):
        return sqlite3.connect(self.path, isolation_level=None, timeout=5)

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise BudgetUnavailable('clock unavailable')
        return now.astimezone(ZoneInfo('Europe/Moscow'))

    def status(self, request_id):
        """Read state after a lost response; never interpret absence as proof of no provider call."""
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise ValueError('invalid request')
        with closing(self._db()) as db:
            row = db.execute('SELECT month,graph_hash,cents,rate_date,state FROM cf_budget_reservations WHERE request_id=?',(request_id,)).fetchone()
        if row is None: return None
        return dict(zip(('month','graph_hash','cents','rate_date','state'), row))

    def settings(self):
        """Local config and reservations only; it is not actual n8n/provider state."""
        month = self._now().strftime('%Y-%m')
        with closing(self._db()) as db:
            db.execute('BEGIN')
            rev, enabled, limit = db.execute('SELECT revision,enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            baseline = db.execute('SELECT cents FROM cf_budget_baseline WHERE month=?',(month,)).fetchone()
            reserved = db.execute("SELECT COALESCE(SUM(cents),0) FROM cf_budget_reservations WHERE month=? AND state != 'cancelled'",(month,)).fetchone()[0]
            uncertain = bool(db.execute("SELECT 1 FROM cf_budget_reservations WHERE state IN ('dispatching','unknown') LIMIT 1").fetchone())
            db.commit()
        return {'revision':rev, 'enabled':bool(enabled), 'monthly_limit_cents':limit,
                'month':month, 'estimated_reserved_cents':baseline[0]+reserved if baseline else None,
                'actual_spending_cents':None, 'uncertain':uncertain, 'can_run':False}

    def change_limit(self, expected_revision, limit_cents):
        """Trusted owner action; does not enable automation or touch n8n."""
        if type(expected_revision) is not int or expected_revision < 0 or type(limit_cents) is not int or not 0 <= limit_cents <= LIMIT:
            raise ValueError('invalid budget settings')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision,enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            if row[0] != expected_revision: db.rollback(); raise BudgetUnavailable('settings changed')
            if row[1] and not limit_cents: db.rollback(); raise ValueError('disable before clearing limit')
            db.execute('UPDATE cf_budget_settings SET revision=revision+1, limit_cents=? WHERE singleton=1',(limit_cents,))
            if limit_cents < row[2]:
                db.execute("UPDATE cf_budget_reservations SET state='cancelled' WHERE state='reserved'")
            db.commit()
        return expected_revision + 1

    def configure(self, expected_revision, limit_cents, enabled):
        """Trusted owner action; authorisation and audit required from caller."""
        if type(expected_revision) is not int or expected_revision < 0 or type(limit_cents) is not int or not 0 <= limit_cents <= LIMIT or type(enabled) is not bool or (enabled and limit_cents == 0):
            raise ValueError('invalid budget settings')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            prev_limit = db.execute('SELECT limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()[0]
            changed = db.execute('UPDATE cf_budget_settings SET revision=revision+1, enabled=?, limit_cents=? WHERE singleton=1 AND revision=?',
                                 (int(enabled), limit_cents, expected_revision)).rowcount
            if changed != 1: db.rollback(); raise BudgetUnavailable('settings changed')
            if not enabled or limit_cents < prev_limit:
                db.execute("UPDATE cf_budget_reservations SET state='cancelled' WHERE state='reserved'")
            db.commit()
            return expected_revision + 1

    def seed_baseline(self, month, cents, source):
        """Trusted importer only; caller must verify completeness independently."""
        now = self._now()
        if month != now.strftime('%Y-%m') or type(cents) is not int or not 0 <= cents <= LIMIT or not isinstance(source, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', source):
            raise ValueError('invalid baseline')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO cf_budget_baseline VALUES (?,?,?,?)', (month, cents, source, now.isoformat()))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback(); raise BudgetUnavailable('baseline already exists') from None

    def reserve(self, request_id, graph_hash):
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id) or not isinstance(graph_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', graph_hash):
            raise ValueError('invalid reservation')
        if self.quote_builder is None:
            raise BudgetUnavailable('verified quote unavailable')
        now = self._now()
        try:
            quote = self.quote_builder.quote(graph_hash, now.date())
        except Exception:
            raise BudgetUnavailable('verified quote unavailable') from None
        if not isinstance(quote, dict) or set(quote) != {'graph_hash','estimated_cents','rate_date'} or quote['graph_hash'] != graph_hash or type(quote['estimated_cents']) is not int or not 0 < quote['estimated_cents'] <= LIMIT or not isinstance(quote['rate_date'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',quote['rate_date']):
            raise BudgetUnavailable('invalid verified quote')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            # Check server time after acquiring the lock; a quote must never
            # cross months or be used after the verifier's checked date.
            locked_now = self._now()
            if locked_now.strftime('%Y-%m') != now.strftime('%Y-%m') or locked_now.date() != now.date():
                db.rollback(); raise BudgetUnavailable('quote expired')
            rev, enabled, limit = db.execute('SELECT revision,enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            month = locked_now.strftime('%Y-%m')
            baseline = db.execute('SELECT cents FROM cf_budget_baseline WHERE month=?',(month,)).fetchone()
            inflight = db.execute("SELECT 1 FROM cf_budget_reservations WHERE state IN ('dispatching','unknown') LIMIT 1").fetchone()
            if not enabled or not limit or baseline is None or inflight:
                db.rollback(); raise BudgetUnavailable('budget disabled or uncertain')
            used = baseline[0] + db.execute("SELECT COALESCE(SUM(cents),0) FROM cf_budget_reservations WHERE month=? AND state != 'cancelled'",(month,)).fetchone()[0]
            if used + quote['estimated_cents'] > limit:
                db.rollback(); raise BudgetUnavailable('budget exhausted')
            try:
                db.execute('INSERT INTO cf_budget_reservations VALUES (?,?,?,?,?,?)',
                           (request_id, month, graph_hash, quote['estimated_cents'], quote['rate_date'], 'reserved'))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback(); raise BudgetUnavailable('request already reserved') from None
            return {'request_id':request_id,'cents':quote['estimated_cents'],'revision':rev,'month':month}

    def _transition(self, request_id, old, new):
        """Internal state transition; never use as authority to send a request."""
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}',request_id):
            raise ValueError('invalid request')
        if (old,new) not in {('reserved','dispatching'),('reserved','cancelled')}:
            raise ValueError('invalid transition')
        quote = None
        quote_day = None
        if new == 'dispatching':
            if self.quote_builder is None: raise BudgetUnavailable('verified quote unavailable')
            with closing(self._db()) as db:
                candidate = db.execute("SELECT graph_hash,cents,rate_date FROM cf_budget_reservations WHERE request_id=? AND state='reserved'", (request_id,)).fetchone()
            if candidate is None: raise BudgetUnavailable('reservation state changed')
            quote_day = self._now().date()
            try: quote = self.quote_builder.quote(candidate[0], quote_day)
            except Exception: raise BudgetUnavailable('verified quote unavailable') from None
            if quote != {'graph_hash':candidate[0], 'estimated_cents':candidate[1], 'rate_date':candidate[2]}:
                raise BudgetUnavailable('quote changed')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            if new == 'dispatching':
                enabled, limit = db.execute('SELECT enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
                row = db.execute('SELECT month,graph_hash,cents,rate_date FROM cf_budget_reservations WHERE request_id=? AND state=?',(request_id,old)).fetchone()
                now = self._now()
                if not enabled or not row or row[0] != now.strftime('%Y-%m') or now.date() != quote_day or tuple(row[1:]) != candidate:
                    db.rollback(); raise BudgetUnavailable('dispatch disabled')
                baseline = db.execute('SELECT cents FROM cf_budget_baseline WHERE month=?',(row[0],)).fetchone()
                used = baseline[0] + db.execute("SELECT COALESCE(SUM(cents),0) FROM cf_budget_reservations WHERE month=? AND state != 'cancelled'",(row[0],)).fetchone()[0] if baseline else LIMIT + 1
                if used > limit or db.execute("SELECT 1 FROM cf_budget_reservations WHERE state IN ('dispatching','unknown') LIMIT 1").fetchone():
                    db.rollback(); raise BudgetUnavailable('dispatch disabled')
            if db.execute('UPDATE cf_budget_reservations SET state=? WHERE request_id=? AND state=?',(new,request_id,old)).rowcount != 1:
                db.rollback(); raise BudgetUnavailable('reservation state changed')
            db.commit()

    def cancel_reserved(self, request_id):
        self._transition(request_id, 'reserved', 'cancelled')

    def dispatch_once(self, request_id, send_once):
        """Fence one trusted bounded send against successful stop operations.

        A send may have been accepted even after errors or timeouts. Every
        outcome therefore stays unknown until separately reconciled. Caller
        MUST supply a bounded, authenticated send bound to the reserved graph.
        This method does not verify such binding or actual provider usage.
        """
        if not callable(send_once): raise ValueError('send unavailable')
        # Persist an in-flight marker BEFORE any possible network write.
        self._transition(request_id, 'reserved', 'dispatching')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            enabled, limit = db.execute('SELECT enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            row = db.execute("SELECT month,graph_hash,cents,rate_date FROM cf_budget_reservations WHERE request_id=? AND state='dispatching'", (request_id,)).fetchone()
            if row is None: db.rollback(); raise BudgetUnavailable('reservation state changed')
            baseline = db.execute('SELECT cents FROM cf_budget_baseline WHERE month=?',(row[0],)).fetchone()
            used = baseline[0] + db.execute("SELECT COALESCE(SUM(cents),0) FROM cf_budget_reservations WHERE month=? AND state != 'cancelled'",(row[0],)).fetchone()[0] if baseline else LIMIT + 1
            current = self._now()
            try: current_quote = self.quote_builder.quote(row[1], current.date())
            except Exception: current_quote = None
            if (not enabled or row[0] != current.strftime('%Y-%m') or used > limit or
                current_quote != {'graph_hash':row[1], 'estimated_cents':row[2], 'rate_date':row[3]}):
                # This process knows it has not sent the request.
                db.execute("UPDATE cf_budget_reservations SET state='cancelled' WHERE request_id=? AND state='dispatching'", (request_id,))
                db.commit(); raise BudgetUnavailable('dispatch disabled')
            try:
                send_once()
            except Exception:
                pass
            db.execute("UPDATE cf_budget_reservations SET state='unknown' WHERE request_id=? AND state='dispatching'", (request_id,))
            db.commit()
        raise BudgetUnavailable('dispatch outcome requires reconciliation')


class BudgetDraftGateway:
    """Owner-bound two-step local draft only; no n8n mutation or activation.

    `owner` must be a server-computed session/identity hash, never a form field.
    The portal must verify identity, origin, CSRF and recent login. Explicitly
    enabling this object only enables LOCAL budget drafts; can_run remains false.
    """
    def __init__(self, store, *, enabled=False):
        self.store = store
        self.enabled = enabled is True

    def ready(self): return self.enabled

    def settings(self):
        if not self.ready(): raise BudgetUnavailable('draft gateway unavailable')
        return self.store.settings()

    @staticmethod
    def _owner(owner):
        if not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{64}',owner):
            raise BudgetUnavailable('owner unavailable')

    def prepare(self, owner, expected_revision, limit_cents):
        self._owner(owner)
        if not self.ready(): raise BudgetUnavailable('draft gateway unavailable')
        if type(expected_revision) is not int or expected_revision < 0 or type(limit_cents) is not int or not 0 <= limit_cents <= LIMIT:
            raise ValueError('invalid budget settings')
        with closing(self.store._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            now = int(self.store._now().timestamp())
            per_owner = db.execute("SELECT COUNT(*) FROM cf_budget_audit WHERE event='prepared' AND owner_hash=? AND at_utc>?", (owner,now-3600)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM cf_budget_audit WHERE event='prepared' AND at_utc>?", (now-3600,)).fetchone()[0]
            if per_owner >= 10 or total >= 100:
                db.rollback(); raise BudgetUnavailable('proposal rate limit')
            row = db.execute('SELECT revision,enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            if row[0] != expected_revision: db.rollback(); raise BudgetUnavailable('settings changed')
            if row[1] and not limit_cents: db.rollback(); raise ValueError('disable before clearing limit')
            proposal = secrets.token_hex(16)
            db.execute('INSERT INTO cf_budget_proposals VALUES (?,?,?,?,?,?,?)',
                       (proposal,owner,expected_revision,row[2],limit_cents,now+300,'pending'))
            db.execute('INSERT INTO cf_budget_audit(at_utc,owner_hash,proposal_id,event,revision,before_cents,after_cents) VALUES (?,?,?,?,?,?,?)',
                       (now,owner,proposal,'prepared',expected_revision,row[2],limit_cents))
            db.commit()
            return {'proposal_id':proposal, 'before_cents':row[2], 'after_cents':limit_cents}

    def confirm(self, owner, proposal_id):
        self._owner(owner)
        if not self.ready(): raise BudgetUnavailable('draft gateway unavailable')
        if not isinstance(proposal_id,str) or not re.fullmatch(r'[a-f0-9]{32}',proposal_id):
            raise ValueError('invalid proposal')
        with closing(self.store._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            now = int(self.store._now().timestamp())
            row = db.execute('SELECT owner_hash,revision,before_cents,after_cents,expires_utc,state FROM cf_budget_proposals WHERE id=?',(proposal_id,)).fetchone()
            if not row or row[0] != owner or row[5] != 'pending' or now >= row[4]:
                db.rollback(); raise BudgetUnavailable('proposal unavailable')
            current = db.execute('SELECT revision,enabled,limit_cents FROM cf_budget_settings WHERE singleton=1').fetchone()
            if current[0] != row[1] or current[2] != row[2] or (current[1] and not row[3]):
                db.rollback(); raise BudgetUnavailable('settings changed')
            db.execute('UPDATE cf_budget_settings SET revision=revision+1,limit_cents=? WHERE singleton=1',(row[3],))
            if row[3] < row[2]: db.execute("UPDATE cf_budget_reservations SET state='cancelled' WHERE state='reserved'")
            db.execute("UPDATE cf_budget_proposals SET state='confirmed' WHERE id=?",(proposal_id,))
            db.execute('INSERT INTO cf_budget_audit(at_utc,owner_hash,proposal_id,event,revision,before_cents,after_cents) VALUES (?,?,?,?,?,?,?)',
                       (now,owner,proposal_id,'confirmed',row[1]+1,row[2],row[3]))
            db.commit()
        return True
