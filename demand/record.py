"""Attach a sourced demand assessment to the latest local candidate revision.

This does not verify sources, approve the candidate, or advance its stage.
"""
from copy import deepcopy
import sqlite3

from pipeline.candidate import Stage
from storage import StorageError
from .assessment import assess


def save_assessment(store, candidate_id, data, *, source, observed_at, weights=None, threshold=60):
    """Assess, then append a revised candidate while retaining full provenance.

    A write lock covers the read of the most recent revision and append; callers
    must pass a Store whose connection is not already inside a transaction.
    Incomplete assessments never create a candidate revision. Local source and
    approval fields are caller assertions, not verified marketplace evidence.
    """
    result = assess(data, weights=weights, threshold=threshold)
    if result["status"] != "assessed":
        return {"status": "blocked", "issues": result["issues"]}
    connection = store.connection
    if connection.in_transaction:
        return {"status": "blocked", "issues": ["Хранилище уже выполняет другую транзакцию"]}
    try:
        connection.execute("BEGIN IMMEDIATE")
        history = store.candidate_history(candidate_id)
        if not history:
            connection.rollback()
            return {"status": "blocked", "issues": ["Кандидат не найден"]}
        previous = history[-1]
        candidate = deepcopy(previous["candidate"])
        if candidate.get("stage") != Stage.DEMAND.value:
            connection.rollback()
            return {"status": "blocked", "issues": ["Кандидат должен находиться на стадии demand"]}
        existing = candidate.get("demand")
        existing = dict(existing) if isinstance(existing, dict) else {}
        existing.update({
            "score": result["score"],
            "price_min": data["price_range"]["min_rub"],
            "price_max": data["price_range"]["max_rub"],
            "source": source,
            "observed_at": observed_at,
            "assessment": {
                "factors": result["factors"],
                "price_range": result["price_range"],
                "points": result["points"],
                "weights": result["weights"],
                "threshold": result["threshold"],
                "passes_threshold": result["passes_threshold"],
                "note": result["note"],
            },
        })
        candidate["demand"] = existing
        # Store.save_candidate validates source, timestamp and the entire payload.
        # It commits the transaction only after the insert succeeds.
        revision = store.save_candidate(candidate_id, candidate, source=source, observed_at=observed_at)
        return {"status": "assessed", "candidate_id": candidate_id,
                "previous_revision_id": previous["id"], "candidate_revision_id": revision,
                "score": result["score"], "passes_threshold": result["passes_threshold"],
                "note": "Стадия не изменена. Источники и решения не проверены автоматически."}
    except (StorageError, sqlite3.Error) as exc:
        connection.rollback()
        return {"status": "blocked", "issues": ["Не удалось сохранить оценку: " + str(exc)]}
    except BaseException:
        connection.rollback()
        raise
