"""Record a model reference and optional observed passport data without network access."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from pipeline.candidate import blockers
from storage import Store, StorageError

# Input is deliberately limited to known data fields: arbitrary source text is
# not treated as instructions and permissions/reviews cannot be supplied here.
IDEA_FIELDS = {"model_url", "author_url", "license", "photo_url",
               "print_profile_source", "mass_g", "print_hours", "name"}
URL_FIELDS = {"model_url", "author_url", "photo_url", "print_profile_source"}


class ImportError(ValueError):
    pass


def _url(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise ImportError(f"{name}: нужен публичный HTTP(S)-адрес без пробелов")
    try:
        parts = urlsplit(value)
        # Force port parsing to reject malformed ports.
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or port is not None):
            raise ValueError
    except ValueError:
        raise ImportError(f"{name}: нужен публичный HTTP(S)-адрес без параметров, фрагмента или учётных данных") from None
    return value


def import_candidate(data, db_path):
    """Append an initial idea snapshot and return local completeness blockers.

    The source is a caller-supplied claim; URLs are stored, not fetched, and no
    licence/ownership rights are inferred. Incomplete records are useful and
    deliberately saved, while schema errors are rejected before any writes.
    """
    if not isinstance(data, dict):
        raise ImportError("Ожидается JSON-объект")
    unknown = set(data) - (IDEA_FIELDS | {"candidate_id", "observed_at"})
    if unknown:
        raise ImportError("Объект содержит неподдерживаемые поля")
    candidate_id = data.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ImportError("candidate_id: требуется идентификатор кандидата")
    model_url = _url(data.get("model_url"), "model_url")
    idea = {"model_url": model_url}
    for key in IDEA_FIELDS - {"model_url"}:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if key in URL_FIELDS:
            value = _url(value, key)
        elif key in {"license", "name"}:
            if not isinstance(value, str) or not value.strip():
                raise ImportError(f"{key}: требуется непустая строка")
        elif isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
            raise ImportError(f"{key}: требуется число")
        idea[key] = value
    observed_at = data.get("observed_at")
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    candidate = {"stage": "idea", "idea": idea, "rights": {"status": "unchecked"}}
    missing = blockers(candidate)
    try:
        with Store(db_path) as db:
            revision = db.save_candidate(candidate_id.strip(), candidate, source=model_url, observed_at=observed_at)
    except (StorageError, OSError, sqlite3.Error):
        # Avoid leaking caller-supplied text or paths through exceptions.
        raise ImportError("Не удалось сохранить кандидат; проверьте идентификатор, дату и доступ к локальной базе") from None
    return {"candidate_id": candidate_id.strip(), "candidate_revision_id": revision,
            "status": "blocked" if missing else "ready_for_demand_review", "blockers": missing,
            "observed_at": observed_at,
            "note": "Данные переданы вызывающей стороной: ссылки не открывались, права не проверены, стадия не изменена."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локально записать ссылку на модель и данные паспорта; без запросов в интернет")
    parser.add_argument("--db", type=Path, required=True, help="путь к локальной SQLite базе")
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin, parse_float=Decimal)
        result = import_candidate(data, args.db)
    except (ImportError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # Report details for known validation errors without echoing untrusted input.
        exc = sys.exc_info()[1]
        message = str(exc) if isinstance(exc, ImportError) else "Некорректный JSON"
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main())
