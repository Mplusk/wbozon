"""Unit economics from section 7 of the project specification.

All inputs and results are per item unless the name says otherwise. Monetary
calculations use Decimal and are not rounded until presented to the caller.
Rates are fractions (0.25 = 25%), not percentage numbers.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext


class CalculationError(ValueError):
    """The inputs are incomplete or outside the supported domain."""


INPUTS = {
    "mass_g", "print_hours", "waste_rate", "plastic_rub_per_kg",
    "power_kw", "electricity_rub_per_kwh", "depreciation_rub_per_hour",
    "maintenance_rub_per_hour", "manual_minutes", "labor_rub_per_hour",
    "defect_rate", "packaging_rub", "inbound_delivery_rub", "price_rub",
    "commission_rate", "outbound_logistics_rub", "return_logistics_rub",
    "buyout_rate", "storage_acceptance_rub", "acquiring_rate", "ad_rate",
    "tax_rate",
}
RATES = {"waste_rate", "defect_rate", "commission_rate", "buyout_rate",
         "acquiring_rate", "ad_rate", "tax_rate"}
DEFAULT_LIMITS = {
    "margin_rate": "0.20", "profit_rub": "150", "profit_per_hour_rub": "60",
    "break_even_ad_multiple": "1.5", "stress_price_reduction": "0.15",
    "stress_commission_increase_pp": "0.05", "stress_buyout_decrease_pp": "0.10",
    "minimum_price_margin_rate": "0.10",
}


def _number(value, name):
    if isinstance(value, bool) or value is None:
        raise CalculationError(f"{name}: ожидается конечное неотрицательное число")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CalculationError(f"{name}: ожидается конечное неотрицательное число") from None
    if not result.is_finite() or result < 0:
        raise CalculationError(f"{name}: ожидается конечное неотрицательное число")
    return result


def _parse(values, limits):
    if not isinstance(values, dict):
        raise CalculationError("inputs должен быть объектом")
    missing, unknown = INPUTS - values.keys(), values.keys() - INPUTS
    if missing:
        raise CalculationError("Не заданы поля: " + ", ".join(sorted(missing)))
    if unknown:
        raise CalculationError("Неизвестные поля: " + ", ".join(sorted(unknown)))
    x = {key: _number(value, key) for key, value in values.items()}
    for key in RATES:
        if x[key] > 1:
            raise CalculationError(f"{key}: доля должна быть от 0 до 1")
    if x["defect_rate"] >= 1:
        raise CalculationError("defect_rate должен быть меньше 1")
    for key in ("print_hours", "price_rub", "buyout_rate"):
        if x[key] <= 0:
            raise CalculationError(f"{key} должен быть больше 0")
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise CalculationError("limits должен быть объектом")
    unknown = limits.keys() - DEFAULT_LIMITS.keys()
    if unknown:
        raise CalculationError("Неизвестные пороги: " + ", ".join(sorted(unknown)))
    settings = {key: _number(limits.get(key, default), key)
                for key, default in DEFAULT_LIMITS.items()}
    for key in ("margin_rate", "stress_price_reduction", "stress_commission_increase_pp",
                "stress_buyout_decrease_pp", "minimum_price_margin_rate"):
        if settings[key] > 1:
            raise CalculationError(f"{key}: доля должна быть от 0 до 1")
    return x, settings


def _logistics(x, buyout):
    return (x["outbound_logistics_rub"] + (1 - buyout) * x["return_logistics_rub"]) / buyout


def _profit(x, printing, price, commission, buyout):
    return (price * (1 - commission - x["acquiring_rate"] - x["ad_rate"] - x["tax_rate"])
            - printing - x["packaging_rub"] - x["inbound_delivery_rub"]
            - _logistics(x, buyout) - x["storage_acceptance_rub"])


def _price_for_margin(fixed_costs, variable_rate, target):
    denominator = 1 - variable_rate - target
    if denominator <= 0:
        return None
    # A floor price must not fall below the mathematical threshold because of rounding.
    return (fixed_costs / denominator).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def calculate(values, limits=None):
    """Return calculations and pass/fail decisions without substituting source data.

    None for a price means the requested margin cannot be reached with these
    variable rates. None for stress profit means the specified stress scenario
    leaves buyout <= 0 or commission > 100%; such a scenario does not pass.
    """
    with localcontext() as ctx:
        ctx.prec = 28
        x, s = _parse(values, limits)
        material = x["mass_g"] * (1 + x["waste_rate"]) * x["plastic_rub_per_kg"] / 1000
        energy = x["print_hours"] * x["power_kw"] * x["electricity_rub_per_kwh"]
        equipment = x["print_hours"] * (x["depreciation_rub_per_hour"] + x["maintenance_rub_per_hour"])
        labor = x["manual_minutes"] * x["labor_rub_per_hour"] / 60
        printing = (material + energy + equipment + labor) / (1 - x["defect_rate"])
        logistics = _logistics(x, x["buyout_rate"])
        fixed = printing + x["packaging_rub"] + x["inbound_delivery_rub"] + logistics + x["storage_acceptance_rub"]
        variable_rate = x["commission_rate"] + x["acquiring_rate"] + x["ad_rate"] + x["tax_rate"]
        profit = _profit(x, printing, x["price_rub"], x["commission_rate"], x["buyout_rate"])
        margin = profit / x["price_rub"]
        per_hour = profit / x["print_hours"]
        break_even_ad = 1 - x["commission_rate"] - x["acquiring_rate"] - x["tax_rate"] - fixed / x["price_rub"]
        stress_buyout = x["buyout_rate"] - s["stress_buyout_decrease_pp"]
        stress_commission = x["commission_rate"] + s["stress_commission_increase_pp"]
        stress_profit = None
        if stress_buyout > 0 and stress_commission <= 1:
            stress_profit = _profit(x, printing, x["price_rub"] * (1 - s["stress_price_reduction"]),
                                    stress_commission, stress_buyout)
        checks = {
            "margin": margin >= s["margin_rate"],
            "profit": profit >= s["profit_rub"],
            "profit_per_hour": per_hour >= s["profit_per_hour_rub"],
            "break_even_ad": break_even_ad >= s["break_even_ad_multiple"] * x["ad_rate"],
            "stress": stress_profit is not None and stress_profit >= 0,
        }
        return {
            "material_rub": material, "energy_rub": energy, "equipment_rub": equipment,
            "labor_rub": labor, "printing_per_good_item_rub": printing,
            "logistics_per_buyout_rub": logistics, "fixed_costs_rub": fixed,
            "profit_rub": profit, "margin_rate": margin, "profit_per_print_hour_rub": per_hour,
            "break_even_ad_rate": break_even_ad, "stress_profit_rub": stress_profit,
            "minimum_price_rub": _price_for_margin(fixed, variable_rate, s["minimum_price_margin_rate"]),
            "price_for_target_margin_rub": _price_for_margin(fixed, variable_rate, s["margin_rate"]),
            "checks": checks, "passes_all": all(checks.values()),
        }
