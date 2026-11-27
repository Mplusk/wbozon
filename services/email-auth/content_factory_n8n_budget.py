"""Narrow n8n estimated-budget gateway. Not enabled by default.

The caller must authenticate the owner, check CSRF/origin/freshness and arm
the shared audited Fence. It changes only one pilot row/field using server-side
CAS. Budget is an estimate, not a cap on provider charges. Unknown response
blocks all subsequent fenced writes; no automatic retries or reconciliation.
"""
import json
import multiprocessing
import re
import sqlite3
import time
import urllib.request
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation

from content_factory_fence import Fence

TABLE = 'kUd3G9ZAaqVWWcdI'
BASE = 'https://n8n.gamelife.shop/api/v1/data-tables/' + TABLE + '/rows'
LIMIT = 100_000_000


class Unavailable(Exception): pass


def cents(amount):
    if isinstance(amount, bool) or not isinstance(amount, (int,float,str)): raise ValueError()
    try: value = Decimal(str(amount)) * 100
    except InvalidOperation: raise ValueError() from None
    if not value.is_finite() or value != value.to_integral_value() or not 0 <= value <= LIMIT: raise ValueError()
    return int(value)


def _snapshot(value):
    if not isinstance(value,dict) or not isinstance(value.get('data'),list) or value.get('nextCursor', 'missing') is not None: raise ValueError()
    matching = [r for r in value['data'] if isinstance(r,dict) and r.get('setting_key') == 'pilot']
    if len(matching) != 1: raise ValueError()
    row = matching[0]
    if type(row.get('id')) is not int or row['id'] < 1 or type(row.get('enabled')) is not bool: raise ValueError()
    updated = row.get('updatedAt')
    if not isinstance(updated,str) or len(updated) > 40: raise ValueError()
    parsed = datetime.fromisoformat(updated.replace('Z','+00:00'))
    if parsed.utcoffset() is None: raise ValueError()
    return {'id':row['id'], 'updatedAt':updated, 'before_cents':cents(row.get('monthly_budget_usd')), 'enabled':row['enabled']}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def _read(opener, token):
    req = urllib.request.Request(BASE + '?limit=100', headers={'X-N8N-API-KEY':token,'Accept':'application/json'}, method='GET')
    with opener.open(req, timeout=4) as response:
        data = response.read(262145)
        if response.status != 200 or len(data) > 262144: raise ValueError()
        return _snapshot(json.loads(data.decode('utf-8')))


def _worker(action, token, expected=None, after=None):
    """Invoked in disposable process; never returns exception bodies or tokens."""
    if not isinstance(token,str) or not re.fullmatch(r'[!-~]{16,2048}',token): raise ValueError()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
    before = _read(opener, token)
    if action == 'read': return before
    if action != 'write' or before != expected or type(after) is not int or not 1 <= after <= LIMIT: return 'unknown'
    filters = [{'columnName':'id','condition':'eq','value':expected['id']},
               {'columnName':'setting_key','condition':'eq','value':'pilot'},
               {'columnName':'enabled','condition':'eq','value':expected['enabled']},
               {'columnName':'monthly_budget_usd','condition':'eq','value':expected['before_cents']/100},
               {'columnName':'updatedAt','condition':'eq','value':expected['updatedAt']}]
    payload = json.dumps({'filter':{'type':'and','filters':filters},
                          'data':{'monthly_budget_usd':after/100},'returnData':True},separators=(',',':')).encode()
    req = urllib.request.Request(BASE + '/update', data=payload, method='PATCH',
          headers={'X-N8N-API-KEY':token,'Accept':'application/json','Content-Type':'application/json'})
    with opener.open(req, timeout=4) as response:
        raw = response.read(262145)
        if response.status != 200 or len(raw) > 262144: return 'unknown'
        result = json.loads(raw.decode('utf-8'))
    if not isinstance(result,list) or len(result) != 1: return 'unknown'
    updated = _snapshot({'data':result,'nextCursor':None})
    if (updated['id'] != before['id'] or updated['before_cents'] != after or
        updated['enabled'] != before['enabled'] or updated['updatedAt'] == before['updatedAt']): return 'unknown'
    return 'verified' if _read(opener,token) == updated else 'unknown'


def _child(send, args):
    try: send.send(_worker(*args))
    except BaseException:
        try: send.send(None)
        except Exception: pass
    finally: send.close()


def _bounded(*args):
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(send,args),daemon=True)
    result = None
    try:
        process.start(); send.close()
        if receive.poll(8): result = receive.recv()
    except Exception: result = None
    finally:
        receive.close(); send.close()
        if process.pid is not None:
            process.join(.1)
            if process.is_alive(): process.terminate(); process.join(1)
            if process.is_alive(): process.kill(); process.join(1)
            if not process.is_alive(): process.close()
    return result


class LiveBudgetGateway:
    """Composition is intentionally explicit; default production root omits it."""
    source = 'n8n_live'
    def __init__(self, path, key, *, token_reader, transport=_bounded, configuration_ready=lambda:True):
        if not callable(token_reader) or not callable(transport) or not callable(configuration_ready): raise ValueError()
        self.path, self.token_reader, self.transport = str(path), token_reader, transport
        self.configuration_ready = configuration_ready
        self.fence = Fence(path,key)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS cf_live_budget_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, snapshot TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS cf_live_budget_proposals (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, after_cents INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS cf_live_budget_refresh (id INTEGER PRIMARY KEY CHECK(id=1), lease_until REAL NOT NULL, lease_id TEXT, checked_at REAL);
              INSERT OR IGNORE INTO cf_live_budget_refresh VALUES(1,0,NULL,NULL);''')

    def ready(self):
        try: return self.configuration_ready() is True and self.fence.is_ready()
        except Exception: return False

    def _read(self):
        try:
            data = self.transport('read',self.token_reader())
            if not isinstance(data,dict) or set(data) != {'id','updatedAt','before_cents','enabled'}: raise ValueError()
            return data
        except Exception: raise Unavailable('budget read unavailable') from None

    def settings(self):
        """Read cache only: page rendering must never start network traffic."""
        with closing(sqlite3.connect(self.path,timeout=1)) as db:
            row = db.execute('SELECT revision,snapshot FROM cf_live_budget_snapshot WHERE id=1').fetchone()
            checked = db.execute('SELECT checked_at FROM cf_live_budget_refresh WHERE id=1').fetchone()[0]
        snap = json.loads(row[1]) if row else None
        return {'source':'n8n_live','revision':row[0] if row else 0,'enabled':snap['enabled'] if snap else None,
                'monthly_limit_cents':snap['before_cents'] if snap else None,'checked_at':checked,
                'estimated_reserved_cents':None,'actual_spending_cents':None,'can_run':False}

    def refresh(self):
        """Explicit owner action: at most one bounded network read at a time."""
        import secrets
        lease_id = secrets.token_hex(16)
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=20)) as db:
            db.execute('BEGIN IMMEDIATE')
            # The lock may have taken most of the lease duration to obtain.
            # Start the lease only after this transaction owns the lock.
            now = time.time()
            if db.execute('SELECT lease_until FROM cf_live_budget_refresh WHERE id=1').fetchone()[0] > now:
                db.rollback(); raise Unavailable('budget refresh busy')
            db.execute('UPDATE cf_live_budget_refresh SET lease_until=?,lease_id=? WHERE id=1',(now+20,lease_id)); db.commit()
        try:
            snap = self._read(); raw = json.dumps(snap,sort_keys=True,separators=(',',':'))
            with closing(sqlite3.connect(self.path, isolation_level=None, timeout=20)) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT lease_id FROM cf_live_budget_refresh WHERE id=1').fetchone()[0] != lease_id:
                    db.rollback(); raise Unavailable('budget refresh expired')
                row = db.execute('SELECT revision,snapshot FROM cf_live_budget_snapshot WHERE id=1').fetchone()
                rev = 1 if not row else row[0] + (row[1] != raw)
                db.execute('INSERT INTO cf_live_budget_snapshot VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,snapshot=excluded.snapshot',(rev,raw))
                db.execute('UPDATE cf_live_budget_refresh SET lease_until=0,lease_id=NULL,checked_at=? WHERE id=1',(time.time(),)); db.commit()
        except Exception:
            with closing(sqlite3.connect(self.path,timeout=20)) as db:
                db.execute('UPDATE cf_live_budget_refresh SET lease_until=0,lease_id=NULL WHERE id=1 AND lease_id=?',(lease_id,)); db.commit()
            raise Unavailable('budget read unavailable') from None
        return self.settings()

    def prepare(self, owner, expected_revision, limit_cents):
        if type(expected_revision) is not int or type(limit_cents) is not int or not 1 <= limit_cents <= LIMIT: raise ValueError()
        if not self.ready(): raise Unavailable('gateway stopped')
        state = self.refresh()
        if state['revision'] != expected_revision: raise Unavailable('budget changed')
        with closing(sqlite3.connect(self.path,timeout=20)) as db:
            row = db.execute('SELECT revision,snapshot FROM cf_live_budget_snapshot WHERE id=1').fetchone()
        if not row or row[0] != expected_revision: raise Unavailable('budget changed')
        snap = json.loads(row[1]); binding = {'snapshot':snap,'after_cents':limit_cents}
        proposal = self.fence.prepare(owner,'budget','factory-budget',binding)
        with closing(sqlite3.connect(self.path,timeout=20)) as db:
            # Expired proposals cannot be confirmed; preserve audit/fence rows.
            db.execute("DELETE FROM cf_live_budget_proposals WHERE id IN (SELECT id FROM fence_proposal WHERE operation='budget' AND state='expired')")
            db.execute('INSERT INTO cf_live_budget_proposals VALUES (?,?,?)',(proposal,row[1],limit_cents)); db.commit()
        return {'proposal_id':proposal,'before_cents':snap['before_cents'],'after_cents':limit_cents}

    def confirm(self, owner, proposal_id):
        if not isinstance(proposal_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',proposal_id): raise ValueError()
        with closing(sqlite3.connect(self.path,timeout=20)) as db:
            row = db.execute('SELECT snapshot,after_cents FROM cf_live_budget_proposals WHERE id=?',(proposal_id,)).fetchone()
        if not row: raise Unavailable('proposal unavailable')
        snap = json.loads(row[0]); binding = {'snapshot':snap,'after_cents':row[1]}
        def send():
            try: return self.transport('write',self.token_reader(),snap,row[1]) or 'unknown'
            except Exception: return 'unknown'
        outcome = self.fence.dispatch(proposal_id,owner,binding,send)
        if outcome == 'verified':
            # The returned updatedAt belongs to the subprocess; avoid showing
            # stale data as current. Next explicit refresh obtains new state.
            with closing(sqlite3.connect(self.path,timeout=20)) as db:
                db.execute("UPDATE cf_live_budget_snapshot SET revision=revision+1,snapshot='{}' WHERE id=1")
                db.execute('UPDATE cf_live_budget_refresh SET checked_at=NULL WHERE id=1')
                db.commit()
        return outcome == 'verified'
