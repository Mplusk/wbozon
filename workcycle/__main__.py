"""Run one local, read-only review of a candidate and save reproducible evidence."""
import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from calc import CalculationError
from calc.__main__ import DEFAULT_PRODUCTION, add_material_price
from pipeline.candidate import blockers
from storage import Store, StorageError


def run(data, db_path, production=DEFAULT_PRODUCTION):
    """Assess supplied facts; never advance a stage or execute marketplace actions.

    `source` and `observed_at` describe the accompanying candidate/economics
    inputs as a whole; callers must supply a composite reference when their
    inputs come from several sources. Source assertions are not verified here.
    """
    if not isinstance(data, dict):
        return {"status": "blocked", "reasons": ["Нужен объект JSON с кандидатом и входными данными"]}
    candidate = data.get("candidate")
    if not isinstance(candidate, dict):
        return {"status": "blocked", "reasons": ["candidate: требуется объект со стадией и данными"]}
    try:
        reasons = blockers(candidate)
    except (TypeError, ValueError):
        return {"status": "blocked", "reasons": ["candidate.stage: неизвестная стадия"]}
    economy = data.get("economics")
    material_info = None
    inputs = None
    limits = None
    if not isinstance(economy, dict):
        reasons.append("economics: требуются входы расчёта, источник и дата наблюдения")
    else:
        inputs = economy.get("inputs")
        limits = economy.get("limits")
        if not isinstance(inputs, dict):
            reasons.append("economics.inputs: требуется объект входных данных")
            inputs = None
        if not isinstance(economy.get("source"), str) or not economy["source"].strip():
            reasons.append("economics.source: требуется источник входных данных")
            inputs = None
        if not isinstance(economy.get("observed_at"), str) or not economy["observed_at"].strip():
            reasons.append("economics.observed_at: требуются дата и время с часовым поясом")
            inputs = None
        if inputs is not None and economy.get("material") is not None:
            if not isinstance(economy["material"], str) or not economy["material"].strip():
                reasons.append("economics.material: требуется название материала")
                inputs = None
            else:
                try:
                    inputs, material_info = add_material_price(inputs, economy["material"], Path(production))
                except CalculationError as exc:
                    reasons.append(str(exc))
                    inputs = None
    saved_candidate = dict(candidate)
    if material_info:
        saved_candidate["workcycle_material_provenance"] = material_info
    try:
        with Store(db_path) as db:
            revision = db.save_candidate(data.get("candidate_id"), saved_candidate,
                                         source=data.get("source"), observed_at=data.get("observed_at"))
            run_id = None
            result = None
            if inputs is not None:
                try:
                    run_id = db.save_calculation(revision, inputs, limits, source=economy["source"],
                                                 observed_at=economy["observed_at"])
                    result = db.calculation_history(revision)[-1]["result"]
                    if not result["passes_all"]:
                        failed = [key for key, passed in result["checks"].items() if not passed]
                        reasons.append("Расчёт не прошёл пороги: " + ", ".join(failed))
                except (CalculationError, StorageError) as exc:
                    reasons.append("Расчёт заблокирован: " + str(exc))
    except (StorageError, OSError, sqlite3.Error) as exc:
        return {"status": "blocked", "reasons": reasons + ["Не удалось полностью сохранить данные: " + str(exc)]}
    return {"status": "blocked" if reasons else "ready_for_review", "reasons": reasons,
            "candidate_revision_id": revision, "calculation_id": run_id, "calculation": result,
            "material_source": material_info,
            "note": "Локальная проверка; ссылки и решения не проверены, стадия не изменена, внешние действия не выполнялись."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локально проверить кандидата и записать версию и расчёт в SQLite")
    parser.add_argument("--db", type=Path, required=True, help="путь к локальному файлу базы")
    parser.add_argument("--production", type=Path, default=DEFAULT_PRODUCTION,
                        help="файл производственных данных (JSON-синтаксис YAML)")
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin, parse_float=Decimal)
    except (ValueError, OSError):
        print(json.dumps({"status": "blocked", "reasons": ["Некорректный JSON"]}, ensure_ascii=False))
        return 2
    result = run(data, args.db, args.production)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "ready_for_review" else 2


if __name__ == "__main__":
    sys.exit(main())
