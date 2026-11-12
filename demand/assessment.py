"""Demand scoring under the five-factor framework in section 6.1.

A normalized factor is an analyst's documented judgment, not an inferred
measurement. This module cannot validate the quality or accuracy of sources.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext


DEFAULT_WEIGHTS = {"volume": 25, "accessibility": 20, "newcomers": 20,
                   "price": 20, "stability": 15}
DEFAULT_THRESHOLD = 60


def _decimal(value, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if (not number.is_finite() or len(number.as_tuple().digits) > 64
            or number.as_tuple().exponent < -64
            or (minimum is not None and number < minimum) or (maximum is not None and number > maximum)):
        return None
    return number


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _date(value):
    if not _text(value):
        return False
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                return False
        return True
    except ValueError:
        return False


def assess(data, *, weights=None, threshold=DEFAULT_THRESHOLD):
    """Assess five explicitly scored factors with evidence and a price range.

    Each factor needs a score_fraction in [0,1], source, observed_at and
    rationale. Missing or malformed information yields blocked and no score.
    Caller-supplied evidence is recorded, not verified. Weights must add to
    100; a change in weights or threshold must be decided outside this module.
    """
    issues = []
    if not isinstance(data, dict):
        return {"status": "blocked", "issues": ["Требуется объект с factors и price_range"],
                "score": None, "passes_threshold": None}
    if weights is None:
        weights = DEFAULT_WEIGHTS
    normalized_weights = {}
    if not isinstance(weights, dict) or set(weights) != set(DEFAULT_WEIGHTS):
        issues.append("weights: нужны все пять факторов без дополнительных ключей")
    else:
        for name, weight in weights.items():
            number = _decimal(weight, Decimal(0), Decimal(100))
            if number is None:
                issues.append(f"weights.{name}: требуется число от 0 до 100")
            else:
                normalized_weights[name] = number
        if len(normalized_weights) == 5 and sum(normalized_weights.values()) != 100:
            issues.append("weights: сумма весов должна быть равна 100")
    cutoff = _decimal(threshold, Decimal(0), Decimal(100))
    if cutoff is None:
        issues.append("threshold: требуется число от 0 до 100")

    factors = data.get("factors")
    normalized = {}
    if not isinstance(factors, dict):
        issues.append("factors: требуются оценки пяти факторов")
        factors = {}
    extra = factors.keys() - DEFAULT_WEIGHTS.keys()
    if extra:
        issues.append("factors: неизвестные факторы: " + ", ".join(sorted(map(str, extra))))
    for name in DEFAULT_WEIGHTS:
        item = factors.get(name)
        if not isinstance(item, dict):
            issues.append(f"factors.{name}: нет оценки и её обоснования")
            continue
        fraction = _decimal(item.get("score_fraction"), Decimal(0), Decimal(1))
        if fraction is None:
            issues.append(f"factors.{name}.score_fraction: нужна оценка от 0 до 1")
        if not _text(item.get("source")):
            issues.append(f"factors.{name}.source: нужен источник")
        if not _date(item.get("observed_at")):
            issues.append(f"factors.{name}.observed_at: нужна дата ISO или время с часовым поясом")
        if not _text(item.get("rationale")):
            issues.append(f"factors.{name}.rationale: нужно обоснование оценки")
        normalized[name] = fraction

    price_range = data.get("price_range")
    if not isinstance(price_range, dict):
        issues.append("price_range: нужен подтверждённый диапазон цен")
    else:
        low = _decimal(price_range.get("min_rub"), Decimal("0.000001"))
        high = _decimal(price_range.get("max_rub"), Decimal("0.000001"))
        if low is None or high is None or low > high:
            issues.append("price_range: нужны положительные min_rub и max_rub, min не больше max")
        if not _text(price_range.get("source")):
            issues.append("price_range.source: нужен источник")
        if not _date(price_range.get("observed_at")):
            issues.append("price_range.observed_at: нужна дата ISO или время с часовым поясом")

    if issues:
        return {"status": "blocked", "issues": issues, "score": None, "passes_threshold": None}
    with localcontext() as ctx:
        ctx.prec = 140  # Exact for the accepted 64-digit factors and weights.
        points = {name: normalized[name] * normalized_weights[name] for name in DEFAULT_WEIGHTS}
        score = sum(points.values(), Decimal(0))
    return {"status": "assessed", "score": score, "threshold": cutoff,
            "passes_threshold": score >= cutoff, "points": points,
            "weights": normalized_weights, "factors": factors, "price_range": price_range,
            "note": "Оценки и источники предоставлены аналитиком; достоверность источников не проверена автоматически."}
