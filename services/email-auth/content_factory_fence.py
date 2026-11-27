"""Dispatch-fence component; NOT a configured production gateway.

Callers must perform real owner authentication and validate targets before
calling this module. The adapter must impose a strict network deadline. Never
put credentials or request bodies into the database or adapter exceptions.
A timed-out request may already be executing remotely after halt returns; the
unknown state remains blocked and halt is not proof of remote cancellation.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing


class Fence:
    def __init__(self, path, key, clock=time.time, daily_limit=3):
        if not isinstance(key, bytes) or len(key) < 32 or type(daily_limit) is not int or daily_limit < 1:
            raise ValueError('Invalid server configuration')
        self.path, self.key, self.clock, self.limit = str(path), key, clock, daily_limit
        with closing(self._db()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS fence_control (id INTEGER PRIMARY KEY CHECK(id=1), halted INTEGER NOT NULL);
                INSERT OR IGNORE INTO fence_control VALUES (1,1);
                CREATE TABLE IF NOT EXISTS fence_proposal (id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, binding TEXT NOT NULL,
                  operation TEXT NOT NULL, target TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fence_audit (id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, owner_hash TEXT NOT NULL,
                  operation TEXT NOT NULL, target TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS fence_reconciliation (proposal_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL,
                  observation TEXT NOT NULL, evidence TEXT NOT NULL, expires REAL NOT NULL);
            ''')

    def _db(self):
        return sqlite3.connect(self.path, timeout=20, isolation_level=None)

    def _mac(self, domain, value):
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        return hmac.new(self.key, domain.encode('ascii') + b'\0' + raw, hashlib.sha256).hexdigest()

    def prepare(self, owner, operation, target, binding, *, ttl=300):
        if not isinstance(owner, str) or len(owner) < 16 or operation not in ('models', 'credential', 'budget', 'cutover'):
            raise ValueError('Invalid request')
        if target not in ('factory-text', 'factory-image', 'factory-model-config', 'factory-openai', 'factory-gemini', 'factory-reader', 'factory-budget', 'factory-cutover') or not 1 <= ttl <= 300:
            raise ValueError('Invalid target')
        allowed = {'models': ('factory-text', 'factory-image', 'factory-model-config'),
                   'credential': ('factory-openai', 'factory-gemini', 'factory-reader'),
                   'budget': ('factory-budget',), 'cutover': ('factory-cutover',)}
        if target not in allowed[operation]:
            raise ValueError('Invalid target')
        proposal = secrets.token_urlsafe(24)
        owner_hash = self._mac('owner', owner)
        bind = self._mac('binding', binding)
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            if db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0]:
                db.rollback(); raise PermissionError('Gateway stopped')
            if operation == 'budget':
                db.execute("UPDATE fence_proposal SET state='expired' WHERE operation='budget' AND state='pending' AND expires<=?",(now,))
                count = db.execute("SELECT COUNT(*) FROM fence_proposal WHERE operation='budget' AND state='pending'").fetchone()[0]
                owner_count = db.execute("SELECT COUNT(*) FROM fence_proposal WHERE operation='budget' AND state='pending' AND owner_hash=?",(owner_hash,)).fetchone()[0]
                if count >= 20 or owner_count >= 5:
                    db.rollback(); raise PermissionError('Too many pending budget proposals')
            db.execute('INSERT INTO fence_proposal VALUES (?,?,?,?,?,?,?)', (proposal, owner_hash, bind, operation, target, now+ttl, 'pending'))
            db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                       (proposal, owner_hash, operation, target, 'prepared', now))
            db.commit()
        return proposal

    def dispatch(self, proposal, owner, binding, send_once):
        if not callable(send_once): raise ValueError('Missing adapter')
        # Phase one is committed before I/O. A crash preserves dispatching and
        # therefore blocks future requests until authorised reconciliation.
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            row = db.execute('SELECT owner_hash,binding,operation,target,expires,state FROM fence_proposal WHERE id=?', (proposal,)).fetchone()
            halted = db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0]
            busy = db.execute("SELECT count(*) FROM fence_proposal WHERE state IN ('dispatching','unknown','owner_acknowledged_unknown')").fetchone()[0]
            count = db.execute("SELECT count(*) FROM fence_audit WHERE event='dispatching' AND at>? AND at<=?", (now-86400, now)).fetchone()[0]
            if (halted or busy or count >= self.limit or not row or row[5] != 'pending' or row[4] <= now
                or not hmac.compare_digest(row[0], self._mac('owner', owner))
                or not hmac.compare_digest(row[1], self._mac('binding', binding))):
                db.rollback(); raise PermissionError('Approval unavailable')
            db.execute("UPDATE fence_proposal SET state='dispatching' WHERE id=?", (proposal,))
            db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                       (proposal, row[0], row[2], row[3], 'dispatching', now))
            db.commit()
        # A stop may win the gap between transactions; check again under the
        # write lock. While I/O proceeds, halt waits for the same lock.
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT expires,state FROM fence_proposal WHERE id=?', (proposal,)).fetchone()
            if (db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0]
                    or not current or current[1] != 'dispatching' or current[0] <= self.clock()):
                db.execute("UPDATE fence_proposal SET state='cancelled' WHERE id=? AND state='dispatching'", (proposal,))
                db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                           (proposal, row[0], row[2], row[3], 'cancelled', self.clock()))
                db.commit(); return 'cancelled'
            try:
                outcome = send_once()
            except Exception:
                outcome = 'unknown'
            if outcome not in ('verified', 'accepted_unconfirmed', 'unknown'): outcome = 'unknown'
            db.execute('UPDATE fence_proposal SET state=? WHERE id=?', (outcome, proposal))
            db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                       (proposal, row[0], row[2], row[3], outcome, self.clock()))
            db.commit()
            return outcome

    def halt(self):
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE fence_control SET halted=1 WHERE id=1')
            db.execute("UPDATE fence_proposal SET state='cancelled' WHERE state='pending'")
            db.commit()

    def arm(self, authorised):
        """Enable only on a trusted owner's explicit authenticated action."""
        if not callable(authorised) or authorised() is not True:
            raise PermissionError('Owner authorisation required')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM fence_proposal WHERE state IN ('dispatching','unknown','owner_acknowledged_unknown')").fetchone()[0]:
                db.rollback(); raise PermissionError('Reconciliation required')
            db.execute('UPDATE fence_control SET halted=0 WHERE id=1')
            db.commit()

    def is_ready(self):
        with closing(self._db()) as db:
            return (db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0] == 0
                    and db.execute("SELECT count(*) FROM fence_proposal WHERE state IN ('dispatching','unknown','owner_acknowledged_unknown')").fetchone()[0] == 0)

    @staticmethod
    def _observation(reader, operation, target):
        # Reader is trusted composition-root code and must use read-only calls.
        # A credential test does not establish the exact stored key value.
        if not callable(reader): raise PermissionError('Independent inspection required')
        try:
            result = reader(operation, target)
        except Exception:
            raise PermissionError('Independent inspection unavailable') from None
        if (not isinstance(result, dict) or set(result) != {'observation', 'evidence'}
                or result['observation'] not in ('credential_exists_unverified', 'credential_absent', 'workflow_observed')
                or not isinstance(result['evidence'], str) or not re.fullmatch(r'[a-f0-9]{64}', result['evidence'])):
            raise PermissionError('Independent inspection unavailable')
        if operation == 'credential' and result['observation'] == 'workflow_observed':
            raise PermissionError('Invalid inspection')
        if operation == 'models' and result['observation'] != 'workflow_observed':
            raise PermissionError('Invalid inspection')
        return result

    def inspect_unknown(self, proposal, owner, authorised, reader):
        """Read-only inspection for an owner; no unknown outcome is resolved."""
        if not callable(authorised) or authorised() is not True: raise PermissionError('Owner authorisation required')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner_hash,operation,target,state FROM fence_proposal WHERE id=?', (proposal,)).fetchone()
            if (not row or row[3] not in ('unknown', 'dispatching', 'accepted_unconfirmed')
                    or not db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0]
                    or not hmac.compare_digest(row[0], self._mac('owner', owner))):
                db.rollback(); raise PermissionError('Inspection unavailable')
            obs = self._observation(reader, row[1], row[2])
            now = self.clock()
            db.execute('INSERT OR REPLACE INTO fence_reconciliation VALUES(?,?,?,?,?)',
                       (proposal, row[0], obs['observation'], obs['evidence'], now+300))
            db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                       (proposal, row[0], row[1], row[2], 'inspected:' + obs['observation'] + ':' + obs['evidence'], now))
            db.commit()
            return obs['observation']

    def confirm_unknown(self, proposal, owner, authorised, reader, *, risk_confirmed=False):
        """Acknowledge ambiguity, not proof of whether the original write succeeded.

        The UI must show the observed result, require fresh owner confirmation,
        and state explicitly that a delayed remote request may still execute and
        a passing connection test does not identify the stored key. The gateway
        remains halted. This method does not retry or write to n8n.
        """
        if risk_confirmed is not True or not callable(authorised) or authorised() is not True:
            raise PermissionError('Owner confirmation required')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner_hash,operation,target,state FROM fence_proposal WHERE id=?', (proposal,)).fetchone()
            check = db.execute('SELECT owner_hash,observation,evidence,expires FROM fence_reconciliation WHERE proposal_id=?', (proposal,)).fetchone()
            if (not row or row[3] not in ('unknown', 'dispatching', 'accepted_unconfirmed') or not check or check[3] <= self.clock()
                    or not db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone()[0]
                    or not hmac.compare_digest(row[0], self._mac('owner', owner))
                    or not hmac.compare_digest(check[0], row[0])):
                db.rollback(); raise PermissionError('Inspection expired')
            obs = self._observation(reader, row[1], row[2])
            if obs['observation'] != check[1] or not hmac.compare_digest(obs['evidence'], check[2]):
                db.rollback(); raise PermissionError('Inspection changed')
            db.execute("UPDATE fence_proposal SET state='owner_acknowledged_unknown' WHERE id=?", (proposal,))
            db.execute('DELETE FROM fence_reconciliation WHERE proposal_id=?', (proposal,))
            db.execute('INSERT INTO fence_audit(proposal_id,owner_hash,operation,target,event,at) VALUES (?,?,?,?,?,?)',
                       (proposal, row[0], row[1], row[2], 'owner_acknowledged_unknown:' + obs['observation'] + ':' + obs['evidence'], self.clock()))
            db.commit()
