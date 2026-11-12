"""Production planning calculations based on specification section 9.2.

Nothing here sends a print order or verifies an owner's decision. Calculations
use explicitly supplied data; missing or malformed inputs are rejected.
"""
from decimal import Decimal, InvalidOperation
from fractions import Fraction


class PlanningError(ValueError):
    """A production planning input is absent or invalid."""


def _number(value, name, *, positive=False, integer=False, allow_negative=False):
    if isinstance(value, bool) or value is None:
        raise PlanningError(f"{name}: требуется число")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PlanningError(f"{name}: требуется число") from None
    if not number.is_finite() or (not allow_negative and number < 0) or (positive and number <= 0):
        raise PlanningError(f"{name}: недопустимое значение")
    if len(number.as_tuple().digits) > 1000 or abs(number.as_tuple().exponent) > 1000:
        raise PlanningError(f"{name}: слишком большая точность или порядок")
    if integer and number != number.to_integral_value():
        raise PlanningError(f"{name}: требуется целое число")
    return number


def _to_decimal(value):
    """Convert a terminating rational to Decimal without context rounding."""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise AssertionError("Expected a terminating decimal")
    scale = max(twos, fives)
    coefficient = abs(numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = tuple(int(char) for char in str(coefficient))
    return Decimal((int(numerator < 0), digits, -scale))


def _ceil(value):
    return -(-value.numerator // value.denominator)


def first_batch(daily_sales):
    """Return an unapproved first-batch proposal from the 14-day forecast.

    Fractional demand is rounded up to a whole item, then constrained to the
    specification's 10–30 item range. Owner approval remains mandatory.
    """
    demand = Fraction(_number(daily_sales, "daily_sales"))
    forecast = demand * 14
    units = _ceil(forecast)
    return {"forecast_14_days": _to_decimal(forecast), "proposed_quantity": min(30, max(10, units)),
            "owner_approval_required": True}


def replenishment(daily_sales, lead_time_days, stock, in_transit):
    """Return reorder point and a nonnegative integer replenishment proposal.

    The reorder trigger compares stock with the reorder point, as in the spec;
    in-transit goods reduce the suggested quantity. Forecast quantities round
    upward; negative quantities are clamped to zero.
    """
    sales = Fraction(_number(daily_sales, "daily_sales"))
    lead = Fraction(_number(lead_time_days, "lead_time_days"))
    current = Fraction(_number(stock, "stock", integer=True))
    transit = Fraction(_number(in_transit, "in_transit", integer=True))
    point = sales * (lead + 7)
    requested = max(Fraction(0), sales * 21 - current - transit)
    return {"reorder_point": _to_decimal(point), "below_reorder_point": current < point,
            "suggested_quantity": _ceil(requested)}


def allocate_capacity(printers, planning_days, products):
    """Allocate whole items by descending profit per print hour for a horizon.

    Capacity is printers × 24 × 0.7 × planning days. A product is a proposal
    with SKU, requested units, hours per unit and profit per hour. Items that
    do not fit are reported as a deficit; no print task is sent or approved.
    """
    count = Fraction(_number(printers, "printers", integer=True))
    days = Fraction(_number(planning_days, "planning_days", positive=True))
    if not isinstance(products, list):
        raise PlanningError("products: требуется список")
    seen, validated = set(), []
    for product in products:
        if not isinstance(product, dict):
            raise PlanningError("products: каждый товар должен быть объектом")
        sku = product.get("sku")
        if not isinstance(sku, str) or not sku.strip() or sku in seen:
            raise PlanningError("sku: требуется уникальный непустой артикул")
        seen.add(sku)
        qty = int(_number(product.get("quantity"), f"{sku}.quantity", integer=True))
        hours = Fraction(_number(product.get("print_hours"), f"{sku}.print_hours", positive=True))
        profit = _number(product.get("profit_per_hour"), f"{sku}.profit_per_hour", allow_negative=True)
        validated.append((sku, qty, hours, profit))
    capacity = count * 24 * Fraction(7, 10) * days
    remaining = capacity
    allocated = []
    total_requested = sum((quantity * hours for _, quantity, hours, _ in validated), Fraction(0))
    for sku, quantity, hours, profit in sorted(validated, key=lambda p: (-Fraction(p[3]), p[0])):
        count_fitted = min(quantity, remaining // hours)
        remaining -= count_fitted * hours
        allocated.append({"sku": sku, "requested_quantity": quantity, "planned_quantity": count_fitted,
                          "unplanned_quantity": quantity - count_fitted, "profit_per_hour": profit})
    return {"capacity_hours": _to_decimal(capacity), "requested_hours": _to_decimal(total_requested),
            "planned_hours": _to_decimal(capacity - remaining),
            "unplanned_hours": _to_decimal(total_requested - (capacity - remaining)),
            "available_hours_after_plan": _to_decimal(remaining), "products": allocated}
