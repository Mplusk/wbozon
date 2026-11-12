"""Schedule local owner-gate requests; never send messages or publish products.

Call only in a trusted server process, after emergency-stop handling. A stop
policy is the caller's responsibility; this module does not re-enable writes.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from gateway.store import GatewayStore
from .store import GateError, GateStore, STAGES, _hash


_BY_STAGE = {stage: gate for gate, stage in STAGES.items()}


BATCH_SIZE = 20


def scan(store):
    """Process at most 20 candidates, with one stop guard per candidate.

    A persisted cursor uses each candidate's first revision id. Concurrent
    workers advance it under the shared guard, and a subsequent pass wraps at
    the end. No network or sleep occurs under the guard. An emergency stop
    aborts with GateError before the next candidate's work can begin.
    """
    if not isinstance(store, GateStore):
        raise TypeError('GateStore required')
    if getattr(store._coordination, 'depth', 0):
        raise GateError('Обход кандидатов нельзя запускать под общей блокировкой')
    with store.coordinated():
        with store._transaction():
            store.connection.execute('CREATE TABLE IF NOT EXISTS owner_gate_worker_cursor (id INTEGER PRIMARY KEY CHECK(id=1), first_revision INTEGER NOT NULL)')
            store.connection.execute('INSERT OR IGNORE INTO owner_gate_worker_cursor VALUES(1,0)')
    result = {'created': 0, 'waiting': 0, 'advanced': 0, 'blocked': [], 'scanned': 0}
    seen = set()
    for _ in range(BATCH_SIZE):
        with store.coordinated():
            cursor = store.connection.execute('SELECT first_revision FROM owner_gate_worker_cursor WHERE id=1').fetchone()[0]
            # Find each candidate by its immutable first revision rather than
            # latest id, which changes after every transition.
            def find_after(position):
                return store.connection.execute("""SELECT r.candidate_id,r.id AS first_revision
                    FROM candidate_revisions r WHERE r.id>? AND NOT EXISTS
                    (SELECT 1 FROM candidate_revisions p WHERE p.candidate_id=r.candidate_id AND p.id<r.id)
                    ORDER BY r.id LIMIT 1""", (position,)).fetchone()
            item = find_after(cursor)
            if item is None:
                item = find_after(0)
            if item is None or item['candidate_id'] in seen:
                break
            seen.add(item['candidate_id'])
            row = store.connection.execute(
                'SELECT id,candidate_id,payload_json FROM candidate_revisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1',
                (item['candidate_id'],)).fetchone()
            if row is not None:
                _scan_one(store, row, result)
            # Requests are idempotent if a process fails before recording this
            # cursor. The next pass retries the same candidate safely.
            with store._transaction():
                store.connection.execute('UPDATE owner_gate_worker_cursor SET first_revision=? WHERE id=1',
                                         (item['first_revision'],))
            result['scanned'] += 1
    return result


def _scan_one(store, row, result):
    """Process one latest revision while its gateway stop guard is held."""
    try:
        candidate = json.loads(row['payload_json'])
        stage = candidate.get('stage') if isinstance(candidate, dict) else None
        gate = _BY_STAGE.get(stage)
        if not gate:
            return
        cid, revision, digest = row['candidate_id'], row['id'], _hash(row['payload_json'])
        target = gate
        if gate in ('launch', 'publication'):
            rights = store.connection.execute('''SELECT * FROM owner_gate_requests
                WHERE candidate_id=? AND revision_id=? AND gate='rights' ORDER BY rowid DESC LIMIT 1''',
                (cid, revision)).fetchone()
            if rights is not None and rights['candidate_hash'] != digest:
                result['blocked'].append({'revision_id': revision, 'reason': 'candidate_changed'})
                return
            if rights is None:
                target = 'rights'
            elif rights['status'] == 'approved' and rights['expires_at'] > int(time.time()):
                if rights['owner_user_id'] != store.owner_user_id or rights['owner_chat_id'] != store.owner_chat_id:
                    result['blocked'].append({'revision_id': revision, 'reason': 'rights_owner_changed'})
                    return
                store._validate_current(rights)
            elif rights['status'] == 'pending' and rights['expires_at'] > int(time.time()):
                result['waiting'] += 1
                return
            else:
                result['blocked'].append({'revision_id': revision, 'reason': 'rights_rejected_or_expired'})
                return
        existing = store.connection.execute('''SELECT * FROM owner_gate_requests
            WHERE candidate_id=? AND revision_id=? AND gate=? ORDER BY rowid DESC LIMIT 1''',
            (cid, revision, target)).fetchone()
        if existing is not None:
            if existing['candidate_hash'] != digest:
                result['blocked'].append({'revision_id': revision, 'reason': 'candidate_changed'})
            elif existing['status'] == 'pending' and existing['expires_at'] > int(time.time()):
                result['waiting'] += 1
            elif existing['status'] == 'approved' and existing['expires_at'] > int(time.time()) and target != 'rights':
                status = store.transition_decided(existing['nonce'])['status']
                if status in ('advanced', 'already_advanced'):
                    result['advanced'] += 1
            else:
                result['blocked'].append({'revision_id': revision, 'reason': 'decision_rejected_or_expired'})
            return
        payload = store.proposal(cid, revision, target)
        store.request_gate(cid, revision, target, payload)
        result['created'] += 1
    except (GateError, ValueError, TypeError, KeyError):
        # Some concurrent worker may have created or advanced the same
        # revision. The next scan will observe it. Do not echo source data.
        result['blocked'].append({'revision_id': row['id'], 'reason': 'not_ready_or_changed'})



def main(argv=None):
    parser = argparse.ArgumentParser(description='Подготовить локальные запросы решений владельца без отправки')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--gateway-db', required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.db.is_file() or not args.gateway_db.is_absolute() or not args.gateway_db.is_file():
        print('База не найдена.', file=sys.stderr)
        return 1
    try:
        with GateStore(args.db, os.environ.get('TELEGRAM_OWNER_USER_ID'), os.environ.get('TELEGRAM_OWNER_CHAT_ID'), gateway=GatewayStore(args.gateway_db)) as store:
            result = scan(store)
    except (GateError, OSError, sqlite3.Error, ValueError):
        print('Не удалось проверить локальные решения владельца.', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
