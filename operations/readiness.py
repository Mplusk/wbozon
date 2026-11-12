"""Inspect locally available prerequisites without using the network or printing secrets.

This is a snapshot of this process's environment and local configuration, not a
verification of credentials, a marketplace connection, or running services.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "production.yaml"


def _nonnegative(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _credential_present(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def build_report(config, environ):
    """Return fixed labels/statuses only; never return config or credential values."""
    if not isinstance(config, dict):
        raise ValueError("Некорректный формат производственной конфигурации")
    printers = config.get("printers")
    materials = config.get("materials")
    printer_ok = (isinstance(printers, list) and len(printers) > 0 and all(
        isinstance(p, dict) and _text(p.get("model"))
        and isinstance(p.get("count"), int) and not isinstance(p.get("count"), bool) and p["count"] > 0
        and _nonnegative(p.get("purchase_price_rub"), positive=True)
        and _nonnegative(p.get("useful_life_hours"), positive=True)
        and _nonnegative(p.get("power_kw"), positive=True)
        and _nonnegative(p.get("maintenance_rub_per_hour")) for p in printers))
    material_ok = (isinstance(materials, list) and len(materials) > 0 and all(
        isinstance(m, dict) and _text(m.get("name"))
        and _nonnegative(m.get("price_rub_per_kg"), positive=True)
        and _text(m.get("price_source")) for m in materials))
    tax_rate = config.get("tax_rate")
    tax_ok = (_text(config.get("tax_regime")) and _nonnegative(tax_rate)
              and tax_rate <= 1)
    checks = {
        "ozon_selected": config.get("marketplace") == "ozon",
        "material_prices_and_sources": material_ok,
        "printers": printer_ok,
        "electricity_tariff": _nonnegative(config.get("electricity_rub_per_kwh")),
        "operator_rate": _nonnegative(config.get("operator_rub_per_hour")),
        "packaging_cost": _nonnegative(config.get("packaging_rub_per_unit")),
        "tax_regime_and_rate": tax_ok,
        "fulfillment_scheme": _text(config.get("fulfillment_scheme")),
        "warehouse": _text(config.get("warehouse")),
        "ozon_credentials_present_in_this_process": all(
            _credential_present(environ.get(name)) for name in ("OZON_CLIENT_ID", "OZON_API_KEY")),
    }
    return {
        "checks": {key: "provided" if value else "missing_or_invalid" for key, value in checks.items()},
        "unverified": ["ozon_permissions_and_connectivity", "marketplace_tariffs", "market_demand_source",
                       "vps_deployment", "scheduler", "telegram", "independent_reviewer", "action_gateway"],
        "note": "Локальная проверка наличия данных; права доступа и работа сервисов не проверялись. Сетевых запросов нет.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локально проверить наличие исходных данных, не выводя секреты и не обращаясь к сети.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Путь к производственной конфигурации в JSON-синтаксисе")
    args = parser.parse_args(argv)
    try:
        with args.config.open(encoding="utf-8") as stream:
            config = json.load(stream)
        report = build_report(config, os.environ)
    except (OSError, ValueError, TypeError):
        # Paths and exception text may contain private information; do not echo them.
        print("Не удалось прочитать производственную конфигурацию в ожидаемом формате.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
