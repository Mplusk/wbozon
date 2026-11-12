"""Durable gateway state and append-only audit prototype.

No outbound adapter exists. Every attempted external write is rejected. SQLite
is not tamper-proof; isolate the directory and database from agents.
"""
import hashlib
import fcntl
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone

_held = threading.local()

KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise ValueError("Invalid request identifier")
    return value


def _fingerprint(operation, data):
    return hashlib.sha256(json.dumps({"operation": operation, "data": data},
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class GatewayStore:
    """One instance per operation. All status decisions occur under write lock."""
    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Gateway database path must be absolute")
        parent = path.parent.resolve(strict=True)
        pstat = parent.stat()
        if not stat.S_ISDIR(pstat.st_mode) or pstat.st_uid != os.geteuid() or pstat.st_mode & 0o022:
            raise ValueError("Gateway database directory must be private to the service user")
        path = parent / path.name
        self.path = path
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid database") from None
        else:
            os.close(fd)
        db_stat = path.lstat()
        if not stat.S_ISREG(db_stat.st_mode) or db_stat.st_nlink != 1 or db_stat.st_uid != os.geteuid() or db_stat.st_mode & 0o077:
            raise ValueError("Gateway database must be a private non-linked file")
        self._db_identity = (db_stat.st_dev, db_stat.st_ino)
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS gateway_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), stopped INTEGER NOT NULL CHECK(stopped IN (0,1)),
                    readonly INTEGER NOT NULL CHECK(readonly IN (0,1))
                );
                INSERT OR IGNORE INTO gateway_state VALUES (1,0,1);
                CREATE TABLE IF NOT EXISTS gateway_actions (
                    request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_audit (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL,
                    actor TEXT NOT NULL, request_id TEXT, reason TEXT
                );
            ''')

    @contextmanager
    def coordination_lock(self, timeout=10.0):
        """Serialize stop with trusted local decision/transition workflows.

        All participants must use the same protected DB path and this helper;
        raw SQLite updates, alternate copies and uncooperative processes bypass
        the contract. The guard must not cover network calls or be nested.
        """
        if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError("Invalid lock timeout")
        held = getattr(_held, "paths", set())
        lock_path = self.path.parent / (self.path.name + ".coord.lock")
        if held:
            raise RuntimeError("Coordination lock must not be nested")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            lst = os.fstat(fd)
            if not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1 or lst.st_uid != os.geteuid() or lst.st_mode & 0o077:
                raise ValueError("Coordination lock must be a private, unlinked file")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Coordination lock unavailable") from None
                    time.sleep(0.025)
            current = self.path.lstat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != self._db_identity):
                raise ValueError("Gateway database changed during coordination")
            held.add(str(lock_path))
            _held.paths = held
            yield
        finally:
            if acquired:
                held.discard(str(lock_path))
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def status(self):
        with closing(self._connect()) as db:
            state = db.execute('SELECT stopped, readonly FROM gateway_state WHERE id=1').fetchone()
            if state is None:
                raise ValueError("Missing gateway state")
            return {"stopped": bool(state["stopped"]), "read_only": bool(state["readonly"]),
                    "external_write_available": False}

    def audit(self, after_id=0, limit=100):
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise ValueError("Invalid cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Invalid limit")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id, at, event, actor, request_id, reason FROM gateway_audit WHERE id>? ORDER BY id LIMIT ?",
                              (after_id, limit)).fetchall()
            return [dict(row) for row in rows]

    def attempt(self, request_id, operation, data, actor):
        _key(request_id)
        request_id = hashlib.sha256(request_id.encode()).hexdigest()
        if operation not in {"change_price", "change_ad", "publish_listing", "reply_review", "print_order", "stop_probe"}:
            raise ValueError("Unknown operation")
        if actor not in {"agent", "owner", "system"}:
            raise ValueError("Unknown actor")
        fingerprint = _fingerprint(operation, data)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM gateway_actions WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    db.rollback()
                    return {"status": "conflict"}
                db.commit()
                return {"status": row['state'], "reason": row['reason'], "replayed": True}
            now = _now()
            db.execute('INSERT INTO gateway_actions VALUES (?,?,?,?,?,?,?)',
                       (request_id, operation, fingerprint, 'started', '', now, now))
            db.execute('INSERT INTO gateway_audit(at,event,actor,request_id,reason) VALUES (?,?,?,?,?)',
                       (now, 'attempt_started', actor, request_id, None))
            row = db.execute('SELECT stopped,readonly FROM gateway_state WHERE id=1').fetchone()
            # No live adapters exist: even a corrupt or changed read-only flag
            # cannot turn this prototype into a writer.
            reason = 'STOPPED' if not row or row['stopped'] else ('READ_ONLY' if row['readonly'] else 'NO_VERIFIED_ADAPTER')
            now = _now()
            db.execute('UPDATE gateway_actions SET state=?,reason=?,updated_at=? WHERE request_id=?',
                       ('denied', reason, now, request_id))
            db.execute('INSERT INTO gateway_audit(at,event,actor,request_id,reason) VALUES (?,?,?,?,?)',
                       (now, 'attempt_denied', actor, request_id, reason))
            db.commit()
            return {"status": "denied", "reason": reason, "replayed": False}

    def stop_and_probe(self, request_id):
        with self.coordination_lock():
            return self._stop_and_probe_locked(request_id)

    def _stop_and_probe_locked(self, request_id):
        """Stop persistently then issue a real admission-check probe.

        The caller MUST authenticate the owner independently. If the process
        fails after the first transaction, stop stays engaged and no caller
        should report confirmation until a subsequent probe succeeds.
        """
        _key(request_id)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT stopped FROM gateway_state WHERE id=1').fetchone()
            if row is None:
                raise ValueError("Missing gateway state")
            db.execute('UPDATE gateway_state SET stopped=1, readonly=1 WHERE id=1')
            db.execute('INSERT INTO gateway_audit(at,event,actor,request_id,reason) VALUES (?,?,?,?,?)',
                       (_now(), 'stop', 'owner', hashlib.sha256(request_id.encode()).hexdigest(), 'STOPPED'))
            db.commit()
        probe_id = 'stop-probe-' + hashlib.sha256(request_id.encode()).hexdigest()
        probe = self.attempt(probe_id, 'stop_probe', {}, 'system')
        state = self.status()
        return {"stopped": state['stopped'] and state['read_only'],
                "probe_denied": probe.get('status') == 'denied' and probe.get('reason') == 'STOPPED'}
