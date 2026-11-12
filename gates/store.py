"""Revision- and payload-bound owner decisions for the local server.

This module is not an HTTP endpoint. Only a trusted, authenticated Telegram
transport may call decide(); matching numeric IDs do not authenticate a call.
Candidate records, reviewer results and database integrity are not verified.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import threading
from functools import wraps

from storage import Store
from pipeline.candidate import blockers


class GateError(ValueError):
    """Invalid request or unavailable decision, without exposing payloads."""


STAGES = {'sample': 'sample_approval', 'launch': 'launch_approval',
          'publication': 'publication_approval'}
RIGHTS_STAGES = {'launch_approval', 'publication_approval'}


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise GateError('Некорректный Telegram ID')
    try:
        value = int(value)
    except (ValueError, TypeError):
        raise GateError('Некорректный Telegram ID') from None
    if value == 0:
        raise GateError('Некорректный Telegram ID')
    return value


def _payload(value):
    if not isinstance(value, dict) or not value:
        raise GateError('Нужен непустой объект действия')
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise GateError('Некорректные данные действия') from None
    try:
        size = len(encoded.encode('utf-8'))
    except UnicodeError:
        raise GateError('Некорректные данные действия') from None
    if size > 16384:
        raise GateError('Слишком большой пакет действия')
    return encoded


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _expected_payload(candidate, gate):
    """Bind the owner's displayed package to the complete stored revision.

    Reject missing critical information rather than approving a generic message.
    The source/reviewer assertions in this package are not authenticated here.
    """
    idea = candidate.get('idea')
    if not isinstance(idea, dict) or not all(isinstance(idea.get(k), str) and idea[k].strip() for k in ('model_url', 'name')):
        raise GateError('Не заданы модель и название товара')
    if gate == 'rights':
        return {'model_url': idea['model_url'], 'confirmation': 'rights_settled'}
    previous_stage = {'sample': 'preliminary_economics', 'launch': 'final_economics', 'publication': 'listing'}[gate]
    previous = dict(candidate, stage=previous_stage)
    if blockers(previous):
        raise GateError('Не выполнены документированные условия предшествующей стадии')
    if gate == 'sample':
        screening = candidate.get('screening')
        if not isinstance(screening, dict) or any(screening.get(k) is not False for k in ('restricted_category', 'third_party_brand')):
            raise GateError('Исключения по категории или бренду пока не поддерживаются')
    if gate == 'launch':
        probe = dict(candidate, stage='launch_approval',
                     approvals={'launch': {'approved': True, 'channel': 'telegram', 'evidence': 'probe'}},
                     rights={'status': 'settled', 'owner_evidence': 'probe'})
        if blockers(probe):
            raise GateError('Не заданы бюджет партии, бюджет рекламы или сведения о документах')
    if gate == 'publication':
        listing = candidate.get('listing')
        draft = listing.get('draft') if isinstance(listing, dict) else None
        if (not isinstance(listing, dict) or listing.get('marketplace') != 'ozon'
            or not isinstance(draft, dict)
            or not all(isinstance(draft.get(k), str) and draft[k].strip() for k in ('title', 'description'))
            or not isinstance(draft.get('characteristics'), dict) or not draft['characteristics']
            or not isinstance(draft.get('images'), list) or not draft['images']
            or not all(isinstance(image, str) and image.strip() for image in draft['images'])):
            raise GateError('Не задан полный черновик карточки Ozon')
    return {'decision': gate, 'candidate': candidate}


def coordinated_action(fn):
    """Serialize trusted local state changes with the gateway emergency stop."""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.coordinated():
            return fn(self, *args, **kwargs)
    return wrapper


class GateStore:
    """Use a database shared with candidate revisions, with separately protected writes.

    The configured Telegram owner and chat are mandatory. Construct this object
    only inside trusted server code; never expose decide() to agent role tokens.
    """

    def __init__(self, db_path, owner_user_id, owner_chat_id, *, gateway=None):
        self.owner_user_id, self.owner_chat_id = _id(owner_user_id), _id(owner_chat_id)
        self.gateway = gateway
        self._coordination = threading.local()
        self.store = Store(db_path)
        self.connection = self.store.connection
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS owner_gate_requests (
            nonce TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
            revision_id INTEGER NOT NULL REFERENCES candidate_revisions(id),
            candidate_hash TEXT NOT NULL, stage TEXT NOT NULL, gate TEXT NOT NULL,
            payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','consumed')),
            decided_at INTEGER, owner_user_id INTEGER, owner_chat_id INTEGER, update_id INTEGER,
            consumed_at INTEGER
          );
          CREATE UNIQUE INDEX IF NOT EXISTS owner_gate_one_active
          ON owner_gate_requests(candidate_id, revision_id, gate, payload_hash)
          WHERE status IN ('pending','approved','consumed');
          CREATE TABLE IF NOT EXISTS owner_gate_updates (
            update_id INTEGER PRIMARY KEY, nonce TEXT NOT NULL, decision TEXT NOT NULL,
            processed_at INTEGER NOT NULL
          );
          CREATE TABLE IF NOT EXISTS owner_gate_transitions (
            nonce TEXT PRIMARY KEY REFERENCES owner_gate_requests(nonce),
            candidate_id TEXT NOT NULL, previous_revision INTEGER NOT NULL,
            candidate_revision_id INTEGER NOT NULL REFERENCES candidate_revisions(id),
            stage TEXT NOT NULL
          );
        ''')

    @contextmanager
    def coordinated(self):
        """Use the shared gateway guard; no network calls are allowed inside.

        Nested operations on the same instance/thread share the held guard;
        every nesting rechecks the current stop status. A missing, failed, or
        stopped gateway always blocks mutation. Different threads do not share
        the reentrancy marker. All processes must use the same gateway DB path.
        """
        if self.gateway is None:
            raise GateError('Не настроена координация с шлюзом')
        if getattr(self._coordination, 'depth', 0):
            if self.gateway.status().get('stopped') is not False:
                raise GateError('Шлюз остановлен или его состояние неизвестно')
            self._coordination.depth += 1
            try:
                yield
            finally:
                self._coordination.depth -= 1
            return
        # Infrastructure failures propagate to the caller as retryable errors.
        # In particular, do not turn a transient SQLite failure during decide
        # into a terminal GateError: the Telegram cursor must not advance.
        with self.gateway.coordination_lock():
            if self.gateway.status().get('stopped') is not False:
                raise GateError('Шлюз остановлен или его состояние неизвестно')
            self._coordination.depth = 1
            try:
                yield
            finally:
                self._coordination.depth = 0

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        if self.connection.in_transaction:
            raise GateError('Вложенная транзакция недопустима')
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _latest(self, candidate_id, revision_id):
        row = self.connection.execute(
            'SELECT id, payload_json FROM candidate_revisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1',
            (candidate_id,)).fetchone()
        if row is None or row['id'] != revision_id:
            raise GateError('Версия кандидата не актуальна')
        try:
            candidate = json.loads(row['payload_json'])
            stage = candidate['stage']
        except (ValueError, TypeError, KeyError):
            raise GateError('Некорректный паспорт кандидата') from None
        return stage, _hash(row['payload_json']), candidate

    def _validate_current(self, row):
        stage, digest, _ = self._latest(row['candidate_id'], row['revision_id'])
        if stage != row['stage'] or not hmac.compare_digest(digest, row['candidate_hash']):
            raise GateError('Версия или содержание кандидата изменились')
        if row['gate'] == 'rights':
            if stage not in RIGHTS_STAGES:
                raise GateError('Этап не допускает подтверждение прав')
        elif STAGES.get(row['gate']) != stage:
            raise GateError('Неверный этап для решения')

    def proposal(self, candidate_id, revision_id, gate):
        """Construct exactly the package that must be displayed to the owner."""
        if gate not in (*STAGES, 'rights'):
            raise GateError('Неизвестное решение')
        with self._transaction():
            stage, _, candidate = self._latest(candidate_id, revision_id)
            if (gate == 'rights' and stage not in RIGHTS_STAGES) or (gate != 'rights' and STAGES[gate] != stage):
                raise GateError('Неверный этап для решения')
            return _expected_payload(candidate, gate)

    @coordinated_action
    def request_gate(self, candidate_id, revision_id, gate, payload, *, ttl_seconds=3600):
        if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > 256:
            raise GateError('Нужен идентификатор кандидата')
        if isinstance(revision_id, bool) or not isinstance(revision_id, int) or revision_id < 1:
            raise GateError('Нужен номер версии')
        if gate not in (*STAGES, 'rights'):
            raise GateError('Неизвестное решение')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 86400:
            raise GateError('Недопустимый срок решения')
        content = _payload(payload)
        digest = _hash(content)
        now = int(time.time())
        with self._transaction():
            stage, candidate_hash, candidate = self._latest(candidate_id, revision_id)
            if gate == 'rights':
                if stage not in RIGHTS_STAGES:
                    raise GateError('Этап не допускает подтверждение прав')
                model_url = candidate.get('idea', {}).get('model_url') if isinstance(candidate.get('idea'), dict) else None
                if not model_url or payload.get('model_url') != model_url or payload.get('confirmation') != 'rights_settled':
                    raise GateError('Укажите модель и явное решение по правам')
            elif STAGES[gate] != stage:
                raise GateError('Неверный этап для решения')
            if _payload(_expected_payload(candidate, gate)) != content:
                raise GateError('Данные для владельца не соответствуют полному паспорту товара')
            if gate in ('launch', 'publication'):
                rights = self.connection.execute("""SELECT * FROM owner_gate_requests WHERE candidate_id=? AND revision_id=?
                    AND candidate_hash=? AND gate='rights' AND status='approved' AND expires_at>?
                    AND owner_user_id=? AND owner_chat_id=? ORDER BY decided_at DESC LIMIT 1""",
                    (candidate_id, revision_id, candidate_hash, now, self.owner_user_id, self.owner_chat_id)).fetchone()
                if rights is None or not hmac.compare_digest(_hash(rights['payload_json']), rights['payload_hash']):
                    raise GateError('Сначала требуется подтверждение прав на текущую версию')
                self._validate_current(rights)
            self.connection.execute("UPDATE owner_gate_requests SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=?", (now,))
            existing = self.connection.execute('''SELECT * FROM owner_gate_requests WHERE candidate_id=? AND revision_id=?
                AND gate=? AND payload_hash=? AND status IN ('pending','approved','consumed')''',
                (candidate_id, revision_id, gate, digest)).fetchone()
            if existing:
                raise GateError('Такое решение уже запрошено или использовано')
            nonce = secrets.token_urlsafe(18)
            self.connection.execute('''INSERT INTO owner_gate_requests(nonce,candidate_id,revision_id,candidate_hash,
                stage,gate,payload_json,payload_hash,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (nonce, candidate_id, revision_id, candidate_hash, stage, gate, content, digest, now, now + ttl_seconds, 'pending'))
        return {'nonce': nonce, 'candidate_id': candidate_id, 'revision_id': revision_id,
                'gate': gate, 'payload': json.loads(content), 'expires_at': now + ttl_seconds}

    def list_pending(self):
        """List pending unexpired nonces; each still requires describe() recheck."""
        rows = self.connection.execute("SELECT nonce FROM owner_gate_requests WHERE status='pending' AND expires_at>? ORDER BY created_at, nonce", (int(time.time()),))
        return [row['nonce'] for row in rows]

    def describe(self, nonce):
        """Return exactly the bounded request for display; reject stale or expired requests."""
        with self._transaction():
            row = self.connection.execute('SELECT * FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()
            if row is None or row['status'] != 'pending' or row['expires_at'] <= int(time.time()):
                raise GateError('Запрос недоступен или истёк')
            self._validate_current(row)
            if not hmac.compare_digest(_hash(row['payload_json']), row['payload_hash']):
                raise GateError('Пакет действия изменился')
            return {'nonce': row['nonce'], 'candidate_id': row['candidate_id'], 'revision_id': row['revision_id'],
                    'gate': row['gate'], 'payload': json.loads(row['payload_json']), 'expires_at': row['expires_at']}

    @coordinated_action
    def decide(self, nonce, *, user_id, chat_id, update_id, approve):
        """Accept a decision from a trusted Telegram adapter, exactly once.

        This function checks owner and chat, but only the adapter can authenticate
        that an update came from Telegram. It must not be exposed to agents.
        """
        if _id(user_id) != self.owner_user_id or _id(chat_id) != self.owner_chat_id:
            raise GateError('Неавторизованный пользователь или чат')
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0 or not isinstance(approve, bool):
            raise GateError('Некорректное событие')
        now = int(time.time())
        with self._transaction():
            replay = self.connection.execute('SELECT nonce,decision FROM owner_gate_updates WHERE update_id=?', (update_id,)).fetchone()
            if replay:
                if replay['nonce'] == nonce and replay['decision'] == ('approved' if approve else 'rejected'):
                    return {'status': 'duplicate'}
                raise GateError('Повторный идентификатор события')
            row = self.connection.execute('SELECT * FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()
            if row is None or row['status'] != 'pending' or row['expires_at'] <= now:
                raise GateError('Запрос недоступен или истёк')
            self._validate_current(row)
            if not hmac.compare_digest(_hash(row['payload_json']), row['payload_hash']):
                raise GateError('Пакет действия изменился')
            status = 'approved' if approve else 'rejected'
            self.connection.execute('''UPDATE owner_gate_requests SET status=?,decided_at=?,owner_user_id=?,
                owner_chat_id=?,update_id=? WHERE nonce=?''', (status, now, self.owner_user_id, self.owner_chat_id, update_id, nonce))
            self.connection.execute('INSERT INTO owner_gate_updates VALUES(?,?,?,?)', (update_id, nonce, status, now))
        return {'status': status, 'gate': row['gate'], 'candidate_id': row['candidate_id'], 'revision_id': row['revision_id']}

    def consume_in_transaction(self, nonce, *, candidate_id, revision_id, gate, payload):
        """Consume a specific approval in an already-open writer transaction.

        A future gateway must atomically record its intent in the SAME SQLite
        connection and transaction. This is not a marketplace call or authorization
        endpoint. Caller owns commit/rollback. Rights are checked separately.
        """
        if not getattr(self._coordination, 'depth', 0):
            raise GateError('Требуется общая блокировка с шлюзом')
        if not self.connection.in_transaction:
            raise GateError('Требуется открытая транзакция записи')
        if gate not in STAGES:
            raise GateError('Неизвестные ворота')
        row = self.connection.execute('SELECT * FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()
        now = int(time.time())
        if (row is None or row['status'] != 'approved' or row['expires_at'] <= now
            or row['candidate_id'] != candidate_id or row['revision_id'] != revision_id or row['gate'] != gate
            or row['owner_user_id'] != self.owner_user_id or row['owner_chat_id'] != self.owner_chat_id
            or not hmac.compare_digest(row['payload_hash'], _hash(_payload(payload)))):
            raise GateError('Нет действующего подтверждения действия')
        self._validate_current(row)
        if gate in ('launch', 'publication'):
            rights = self.connection.execute('''SELECT * FROM owner_gate_requests WHERE candidate_id=? AND revision_id=?
                AND candidate_hash=? AND gate='rights' AND status='approved' AND expires_at>?
                AND owner_user_id=? AND owner_chat_id=? ORDER BY decided_at DESC LIMIT 1''',
                (candidate_id, revision_id, row['candidate_hash'], now, self.owner_user_id, self.owner_chat_id)).fetchone()
            if rights is None:
                raise GateError('Нет подтверждения владельца о правах для этой версии')
            self._validate_current(rights)
        self.connection.execute("UPDATE owner_gate_requests SET status='consumed',consumed_at=? WHERE nonce=?", (now, nonce))
        return {'status': 'consumed', 'gate': gate, 'candidate_id': candidate_id, 'revision_id': revision_id}

    @coordinated_action
    def transition_approved(self, nonce, *, candidate_id, revision_id, gate, payload):
        """Consume an owner decision and append the next candidate version atomically.

        The candidate and action are rechecked under one write lock. The caller
        cannot supply the approval, rights decision or owner identity. This does
        not authenticate reviewer reports or call a marketplace. Publication is
        still a separate gateway action and requires its own authorization.
        """
        from datetime import datetime, timezone
        from pipeline.candidate import advance

        with self._transaction():
            previous = self.connection.execute('SELECT * FROM owner_gate_transitions WHERE nonce=?', (nonce,)).fetchone()
            if previous:
                row = self.connection.execute('SELECT * FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()
                if (row is None or row['candidate_id'] != candidate_id or row['revision_id'] != revision_id
                    or row['gate'] != gate or not hmac.compare_digest(row['payload_hash'], _hash(_payload(payload)))):
                    raise GateError('Данные повтора не соответствуют подтверждению')
                return {'status': 'already_advanced', 'previous_revision': previous['previous_revision'],
                        'candidate_revision_id': previous['candidate_revision_id'], 'stage': previous['stage']}
            decision = self.consume_in_transaction(nonce, candidate_id=candidate_id,
                                                    revision_id=revision_id, gate=gate, payload=payload)
            _, _, candidate = self._latest(candidate_id, revision_id)
            approval = {'approved': True, 'channel': 'telegram', 'evidence': 'owner-gate:' + nonce}
            approvals = candidate.get('approvals')
            # Caller-supplied approvals for other gates are not made trustworthy by
            # this method; this entry comes only from the consumed server decision.
            if not isinstance(approvals, dict):
                approvals = {}
            approvals[gate] = approval
            candidate['approvals'] = approvals
            if gate in ('launch', 'publication'):
                rights = self.connection.execute('''SELECT * FROM owner_gate_requests WHERE candidate_id=? AND revision_id=?
                    AND gate='rights' AND status='approved' AND expires_at>?
                    AND owner_user_id=? AND owner_chat_id=? ORDER BY decided_at DESC LIMIT 1''',
                    (candidate_id, revision_id, int(time.time()), self.owner_user_id, self.owner_chat_id)).fetchone()
                if rights is None or not hmac.compare_digest(_hash(rights['payload_json']), rights['payload_hash']):
                    raise GateError('Нет подтверждения владельца о правах для этой версии')
                self._validate_current(rights)
                candidate['rights'] = {'status': 'settled', 'owner_evidence': 'owner-gate:' + rights['nonce']}
            try:
                updated = advance(candidate)
            except ValueError:
                raise GateError('Не выполнены условия перехода текущей стадии') from None
            payload_json = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            now = datetime.now(timezone.utc).isoformat()
            cursor = self.connection.execute('''INSERT INTO candidate_revisions
                (candidate_id,payload_json,source,observed_at,recorded_at) VALUES(?,?,?,?,?)''',
                (candidate_id, payload_json, 'owner-gate:' + nonce, now, now))
            self.connection.execute('INSERT INTO owner_gate_transitions VALUES(?,?,?,?,?)',
                                    (nonce, candidate_id, revision_id, cursor.lastrowid, updated['stage']))
            return {'status': 'advanced', 'previous_revision': revision_id,
                    'candidate_revision_id': cursor.lastrowid, 'stage': updated['stage'], 'decision': decision}

    @coordinated_action
    def transition_decided(self, nonce):
        """Retry-safe transition from the stored owner decision; rights never advances."""
        row = self.connection.execute('SELECT * FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()
        if row is None:
            raise GateError('Запрос не найден')
        if row['gate'] == 'rights':
            if (row['status'] != 'approved' or row['expires_at'] <= int(time.time())
                or row['owner_user_id'] != self.owner_user_id or row['owner_chat_id'] != self.owner_chat_id):
                raise GateError('Нет действующего подтверждения прав')
            self._validate_current(row)
            return {'status': 'rights_recorded', 'candidate_id': row['candidate_id'], 'revision_id': row['revision_id']}
        try:
            payload = json.loads(row['payload_json'])
        except (ValueError, TypeError):
            raise GateError('Некорректный сохранённый пакет') from None
        return self.transition_approved(nonce, candidate_id=row['candidate_id'], revision_id=row['revision_id'],
                                        gate=row['gate'], payload=payload)
