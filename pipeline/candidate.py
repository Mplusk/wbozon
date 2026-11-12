"""Local product-stage checks. Evidence references are claims, not verified access control.

This module never contacts Telegram, a reviewer, a marketplace, or a database.
An authenticated gateway must independently enforce publication permissions.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from enum import Enum


class Stage(str, Enum):
    IDEA = "idea"
    DEMAND = "demand"
    PRELIMINARY_ECONOMICS = "preliminary_economics"
    SAMPLE_APPROVAL = "sample_approval"
    SAMPLE = "sample"
    FINAL_ECONOMICS = "final_economics"
    LAUNCH_APPROVAL = "launch_approval"
    LISTING = "listing"
    PUBLICATION_APPROVAL = "publication_approval"
    FIRST_BATCH = "first_batch"
    LAUNCH = "launch"
    OPERATIONS = "operations"


STAGES = tuple(Stage)


def _get(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value, *, minimum=Decimal(0), maximum=None):
    if isinstance(value, bool) or value is None:
        return False
    try:
        n = Decimal(str(value))
        return n.is_finite() and n >= minimum and (maximum is None or n <= maximum)
    except (InvalidOperation, ValueError):
        return False


def blockers(candidate, demand_threshold=60):
    """List unmet conditions for leaving the current stage; never fabricate facts.

    All source/evidence values are nonempty references supplied by the caller.
    Approvals are recorded assertions, not authenticated Telegram confirmations.
    Sample evidence is intended to reference actual inspection and measurement;
    generated listing images do not establish those facts. This prototype cannot
    verify the authenticity or content of any evidence reference.
    """
    stage = Stage(candidate.get("stage"))
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    def text(*path):
        require(_text(_get(candidate, *path)), ".".join(path) + ": требуется заполненное поле")

    def evidence(*path):
        require(_get(candidate, *path, "passed") is True, ".".join(path) + ": требуется pass")
        text(*path, "evidence")

    def approval(name):
        decision = _get(candidate, "approvals", name)
        require(isinstance(decision, dict) and decision.get("approved") is True,
                "approvals." + name + ": требуется решение владельца")
        require(isinstance(decision, dict) and decision.get("channel") == "telegram",
                "approvals." + name + ": требуется подтверждение в Telegram")
        text("approvals", name, "evidence")

    def rights():
        require(_get(candidate, "rights", "status") == "settled", "rights.status: требуется отметка владельца «улажено»")
        text("rights", "owner_evidence")

    if stage == Stage.IDEA:
        for key in ("model_url", "author_url", "license", "photo_url", "print_profile_source"):
            text("idea", key)
        for key in ("mass_g", "print_hours"):
            require(_number(_get(candidate, "idea", key), minimum=Decimal("0.000001")), f"idea.{key}: требуется положительное число")
    elif stage == Stage.DEMAND:
        score = _get(candidate, "demand", "score")
        require(_number(demand_threshold, maximum=Decimal(100)), "demand_threshold: требуется значение от 0 до 100")
        require(_number(score, maximum=Decimal(100)), "demand.score: требуется число от 0 до 100")
        if _number(score, maximum=Decimal(100)) and _number(demand_threshold, maximum=Decimal(100)):
            require(Decimal(str(score)) >= Decimal(str(demand_threshold)), "demand.score: ниже порога")
        low, high = _get(candidate, "demand", "price_min"), _get(candidate, "demand", "price_max")
        valid = _number(low, minimum=Decimal("0.01")) and _number(high, minimum=Decimal("0.01"))
        require(valid and Decimal(str(low)) <= Decimal(str(high)), "demand: требуется корректный диапазон цены")
        text("demand", "source")
    elif stage == Stage.PRELIMINARY_ECONOMICS:
        # Filters are explicit: absence of an inspection cannot mean a pass.
        for key, limit in (("mass_g", 300), ("print_hours", 6)):
            val = _get(candidate, "idea", key)
            require(_number(val, minimum=Decimal("0.000001")) and Decimal(str(val)) <= limit,
                    f"idea.{key}: отсутствует или превышает стартовый предел {limit}")
        volume = _get(candidate, "screening", "packaging_liters")
        require(_number(volume, minimum=Decimal("0.000001")) and Decimal(str(volume)) <= 5,
                "screening.packaging_liters: отсутствует или превышает 5 л")
        require(_get(candidate, "screening", "significant_postprocessing") is False,
                "screening.significant_postprocessing: требуется отсутствие заметной обработки")
        for key in ("restricted_category", "third_party_brand"):
            val = _get(candidate, "screening", key)
            override = _get(candidate, "screening", key + "_owner_override")
            require(val is False or (val is True and _text(override)),
                    f"screening.{key}: требуется отсутствие ограничения или решение владельца по товару")
        text("screening", "evidence")
        evidence("preliminary_economics", "calculation")
        evidence("preliminary_economics", "review")
    elif stage == Stage.SAMPLE_APPROVAL:
        approval("sample")
    elif stage == Stage.SAMPLE:
        require(_get(candidate, "sample", "acceptable") is True, "sample.acceptable: образец должен быть годен")
        for key in ("mass_g", "print_hours"):
            require(_number(_get(candidate, "sample", key), minimum=Decimal("0.000001")), f"sample.{key}: требуется положительное число")
        defect_rate = _get(candidate, "sample", "defect_rate")
        require(_number(defect_rate) and Decimal(str(defect_rate)) < 1,
                "sample.defect_rate: требуется доля от 0 включительно до 1 не включительно")
        # Listing visuals can be created from references and AI. They are not
        # evidence that a physical sample was printed or measured.
        text("sample", "evidence")
    elif stage == Stage.FINAL_ECONOMICS:
        evidence("final_economics", "calculation")
        evidence("final_economics", "review")
        for key in ("price", "first_batch_quantity"):
            require(_number(_get(candidate, "final_economics", key), minimum=Decimal("0.000001")), f"final_economics.{key}: требуется положительное число")
        quantity = _get(candidate, "final_economics", "first_batch_quantity")
        if _number(quantity, minimum=Decimal("0.000001")):
            require(Decimal(str(quantity)) == Decimal(str(quantity)).to_integral_value(), "final_economics.first_batch_quantity: требуется целое число")
    elif stage == Stage.LAUNCH_APPROVAL:
        approval("launch")
        rights()
        text("compliance", "required_document")
        for key in ("first_batch_budget", "advertising_budget"):
            require(_number(_get(candidate, "launch_plan", key)), f"launch_plan.{key}: требуется неотрицательное число")
    elif stage == Stage.LISTING:
        evidence("listing", "api_validation")
        evidence("listing", "quality_review")
    elif stage == Stage.PUBLICATION_APPROVAL:
        approval("publication")
        rights()
        # Recheck the listing review at the publication gate.
        evidence("listing", "api_validation")
        evidence("listing", "quality_review")
    elif stage == Stage.FIRST_BATCH:
        ready = _get(candidate, "first_batch", "fbs_ready") is True
        stock = _get(candidate, "first_batch", "marketplace_stock")
        require(_get(candidate, "first_batch", "print_job_completed") is True,
                "first_batch.print_job_completed: задание на печать должно быть выполнено")
        require(ready or _number(stock, minimum=Decimal(1)), "first_batch: нужен остаток на площадке или готовность к FBS")
        text("first_batch", "evidence")
    elif stage == Stage.LAUNCH:
        days = _get(candidate, "launch", "sales_days")
        exhausted = _get(candidate, "launch", "test_budget_exhausted") is True
        require(_number(days) and Decimal(str(days)) >= 14 or exhausted,
                "launch: требуется 14 дней продаж или исчерпание тестового бюджета")
        text("launch", "evidence")
    return errors


def advance(candidate, demand_threshold=60):
    """Return a copy advanced by one stage, or raise ValueError with blockers."""
    stage = Stage(candidate.get("stage"))
    if stage == Stage.OPERATIONS:
        raise ValueError("Ведение — конечная стадия данного прототипа")
    errors = blockers(candidate, demand_threshold)
    if errors:
        raise ValueError("Переход заблокирован: " + "; ".join(errors))
    updated = deepcopy(candidate)
    updated["stage"] = STAGES[STAGES.index(stage) + 1].value
    return updated
