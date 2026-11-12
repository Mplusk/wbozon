"""SQLite storage of candidate revisions and reproducible economics calculations.

Local prototype only. This is not a PostgreSQL service, an authorization
boundary, a secret store, a backup system, or a verifier of evidence.
"""
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from calc import calculate
from calc.economics import DEFAULT_LIMITS
from pipeline.candidate import Stage


class StorageError(ValueError):
    """Invalid local record or missing referenced revision."""


# These are common mistakes, not a comprehensive secret detector. Callers must
# ensure records and references contain no credentials or sensitive secrets.
_SECRET_KEYS = {"api_key", "apikey", "authorization", "access_token", "refresh_token",
                "token", "password", "passwd", "secret", "client_secret", "private_key"}


def _clean(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StorageError("Ключи записи должны быть строками")
            if key.lower().replace("-", "_") in _SECRET_KEYS:
                raise StorageError("Запись содержит поле, похожее на секрет")
            _clean(item)
    elif isinstance(value, list):
        for item in value:
            _clean(item)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise StorageError("Число должно быть конечным")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise StorageError("Число должно быть конечным")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise StorageError("Неподдерживаемый тип данных")


def _json(value):
    _clean(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=lambda v: str(v) if isinstance(v, Decimal) else None)


def _name(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise StorageError(f"{field}: требуется непустая строка без управляющих символов")
    return value.strip()


def _source(value):
    value = _name(value, "source")
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise StorageError("URL источника не должен содержать учётные данные, параметры или фрагмент")
    return value


def _observed(value):
    if not isinstance(value, str):
        raise StorageError("observed_at: требуется дата и время с часовым поясом в ISO 8601")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise StorageError("observed_at: требуется дата и время с часовым поясом в ISO 8601") from None
    return timestamp.astimezone(timezone.utc).isoformat()


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """One SQLite connection, usable as a context manager and not thread-safe."""

    def __init__(self, path):
        # New database files are private to the current OS user; an existing
        # database's permissions and directory protection are the owner's job.
        if str(path) != ":memory:":
            try:
                fd = os.open(os.fspath(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_revisions (
                id INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS candidate_history
                ON candidate_revisions(candidate_id, id);
            CREATE TABLE IF NOT EXISTS economics_runs (
                id INTEGER PRIMARY KEY,
                candidate_revision_id INTEGER NOT NULL REFERENCES candidate_revisions(id),
                inputs_json TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS economics_history
                ON economics_runs(candidate_revision_id, id);
        """)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def save_candidate(self, candidate_id, candidate, *, source, observed_at):
        """Append a candidate snapshot; return its immutable revision id.

        A snapshot and its stages are caller assertions, not authorization.
        """
        candidate_id = _name(candidate_id, "candidate_id")
        if not isinstance(candidate, dict):
            raise StorageError("candidate должен быть объектом")
        try:
            Stage(candidate.get("stage"))
        except (ValueError, TypeError):
            raise StorageError("candidate.stage: неизвестная стадия") from None
        payload = _json(candidate)
        source, observed_at = _source(source), _observed(observed_at)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO candidate_revisions(candidate_id,payload_json,source,observed_at,recorded_at) VALUES (?,?,?,?,?)",
                (candidate_id, payload, source, observed_at, _now()))
        return cursor.lastrowid

    def candidate_history(self, candidate_id):
        candidate_id = _name(candidate_id, "candidate_id")
        rows = self.connection.execute(
            "SELECT * FROM candidate_revisions WHERE candidate_id=? ORDER BY id", (candidate_id,))
        return [{"id": r["id"], "candidate_id": r["candidate_id"], "candidate": json.loads(r["payload_json"]),
                 "source": r["source"], "observed_at": r["observed_at"], "recorded_at": r["recorded_at"]} for r in rows]

    def save_calculation(self, candidate_revision_id, inputs, limits=None, *, source, observed_at):
        """Calculate from supplied inputs and append an immutable result and its inputs."""
        if isinstance(candidate_revision_id, bool) or not isinstance(candidate_revision_id, int) or candidate_revision_id <= 0:
            raise StorageError("candidate_revision_id: требуется положительный целый идентификатор")
        source, observed_at = _source(source), _observed(observed_at)
        input_json = _json(inputs)
        result = calculate(inputs, limits)
        effective_limits = {**DEFAULT_LIMITS, **({} if limits is None else limits)}
        limits_json = _json(effective_limits)
        result_json = _json(result)
        with self.connection:
            if self.connection.execute("SELECT 1 FROM candidate_revisions WHERE id=?", (candidate_revision_id,)).fetchone() is None:
                raise StorageError("Версия кандидата не найдена")
            cursor = self.connection.execute(
                "INSERT INTO economics_runs(candidate_revision_id,inputs_json,limits_json,result_json,source,observed_at,recorded_at) VALUES (?,?,?,?,?,?,?)",
                (candidate_revision_id, input_json, limits_json, result_json, source, observed_at, _now()))
        return cursor.lastrowid

    def calculation_history(self, candidate_revision_id):
        rows = self.connection.execute("SELECT * FROM economics_runs WHERE candidate_revision_id=? ORDER BY id", (candidate_revision_id,))
        return [{"id": r["id"], "candidate_revision_id": r["candidate_revision_id"],
                 "inputs": json.loads(r["inputs_json"]), "limits": json.loads(r["limits_json"]),
                 "result": json.loads(r["result_json"]), "source": r["source"],
                 "observed_at": r["observed_at"], "recorded_at": r["recorded_at"]} for r in rows]
