"""Save a demand assessment for an existing local candidate from JSON stdin."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal

from storage import Store
from .record import save_assessment


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сохранить оценку спроса для кандидата на стадии demand")
    parser.add_argument("--db", required=True, type=Path, help="путь к существующей локальной базе SQLite")
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin, parse_float=Decimal)
    except (ValueError, OSError):
        print(json.dumps({"status": "blocked", "issues": ["Некорректный JSON"]}, ensure_ascii=False))
        return 2
    allowed = {"candidate_id", "assessment", "source", "observed_at", "weights", "threshold"}
    if not isinstance(data, dict) or set(data) - allowed:
        print(json.dumps({"status": "blocked", "issues": ["Некорректный объект входных данных"]}, ensure_ascii=False))
        return 2
    if not args.db.is_file():
        print(json.dumps({"status": "blocked", "issues": ["Нужна существующая локальная база"]}, ensure_ascii=False))
        return 2
    try:
        with Store(args.db) as store:
            kwargs = {key: data[key] for key in ("weights", "threshold") if key in data}
            result = save_assessment(store, data.get("candidate_id"), data.get("assessment"),
                                     source=data.get("source"), observed_at=data.get("observed_at"), **kwargs)
    except (OSError, sqlite3.Error):
        result = {"status": "blocked", "issues": ["Не удалось открыть локальную базу"]}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "assessed" else 2


if __name__ == "__main__":
    sys.exit(main())
