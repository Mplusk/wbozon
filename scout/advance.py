"""Advance an existing candidate once, using an atomic SQLite write lock.

Stage checks do not authenticate caller-supplied evidence or owner approvals;
this is a local workflow helper, not a security boundary or publishing gateway.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from pipeline.candidate import advance, blockers
from storage import Store, StorageError


class AdvanceError(ValueError):
    """Invalid or unavailable record; descriptions do not expose input values."""


def transition(db_path, candidate_id, expected_revision, *, source, observed_at):
    """Read latest revision under SQLite write lock and append one checked transition.

    The caller must provide the revision they inspected. Concurrent requests for
    the same revision cannot both succeed, even when the next stage also passes.
    Blocked, stale and failed requests do not write a candidate revision.
    """
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise AdvanceError("Требуется идентификатор кандидата")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision <= 0:
        raise AdvanceError("Требуется положительный номер ожидаемой версии")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise AdvanceError("Файл базы не найден")
    try:
        with Store(db_path) as store:
            try:
                # IMMEDIATE prevents other writers from changing the latest
                # revision between read, validation and the insert/commit.
                store.connection.execute("BEGIN IMMEDIATE")
                history = store.candidate_history(candidate_id.strip())
                if not history:
                    raise AdvanceError("Кандидат не найден")
                latest = history[-1]
                if latest["id"] != expected_revision:
                    store.connection.rollback()
                    return {"status": "stale", "current_revision": latest["id"],
                            "message": "Кандидат изменился; проверьте актуальную версию и повторите запрос."}
                candidate = latest["candidate"]
                reasons = blockers(candidate)
                if candidate.get("stage") in {"sample_approval", "launch_approval", "publication_approval"}:
                    store.connection.rollback()
                    return {"status": "blocked", "current_revision": latest["id"],
                            "blockers": reasons + ["Ворота требуют подтверждённого серверного перехода через GateStore"]}
                if reasons:
                    store.connection.rollback()
                    return {"status": "blocked", "current_revision": latest["id"], "blockers": reasons}
                try:
                    updated = advance(candidate)
                except ValueError:
                    store.connection.rollback()
                    return {"status": "blocked", "current_revision": latest["id"],
                            "blockers": ["Следующая стадия отсутствует или состояние некорректно"]}
                # Store's connection context commits the same active transaction.
                revision = store.save_candidate(candidate_id.strip(), updated, source=source, observed_at=observed_at)
                return {"status": "advanced", "previous_revision": latest["id"],
                        "candidate_revision_id": revision, "stage": updated["stage"],
                        "note": "Локальная проверка полей; подлинность ссылок и решений не проверена."}
            except Exception:
                if store.connection.in_transaction:
                    store.connection.rollback()
                raise
    except AdvanceError:
        raise
    except (StorageError, OSError, sqlite3.Error, TypeError, ValueError):
        raise AdvanceError("Не удалось проверить или сохранить переход; проверьте базу, источник и дату") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локально проверить и записать следующий этап сохранённого кандидата")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--source", required=True, help="источник подтверждающих данных без секретов")
    parser.add_argument("--observed-at", required=True, help="дата и время с часовым поясом, ISO 8601")
    args = parser.parse_args(argv)
    try:
        result = transition(args.db, args.candidate_id, args.expected_revision,
                            source=args.source, observed_at=args.observed_at)
    except AdvanceError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "advanced" else 2


if __name__ == "__main__":
    sys.exit(main())
