"""Read an input JSON object on stdin and print a calculation as JSON."""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .economics import CalculationError, calculate


DEFAULT_PRODUCTION = Path(__file__).resolve().parent.parent / "config" / "production.yaml"


def add_material_price(inputs, material, path):
    """Apply one explicitly selected material price, retaining its provenance."""
    if not isinstance(inputs, dict):
        raise CalculationError("inputs должен быть объектом")
    if "plastic_rub_per_kg" in inputs:
        raise CalculationError("plastic_rub_per_kg уже задан: удалите его или не выбирайте материал")
    try:
        with path.open(encoding="utf-8") as f:
            config = json.load(f, parse_float=Decimal)
    except OSError as exc:
        raise CalculationError(f"Не удалось прочитать файл производства: {exc.strerror}") from None
    except ValueError:
        raise CalculationError("Файл производства должен быть в JSON-синтаксисе YAML") from None
    materials = config.get("materials") if isinstance(config, dict) else None
    if not isinstance(materials, list):
        raise CalculationError("В файле производства отсутствует список materials")
    matches = [item for item in materials if isinstance(item, dict) and item.get("name") == material]
    if len(matches) != 1:
        raise CalculationError(f"Материал {material!r} отсутствует или указан несколько раз")
    entry = matches[0]
    source = entry.get("price_source")
    if not isinstance(source, str) or not source.strip() or entry.get("price_rub_per_kg") is None:
        raise CalculationError(f"Для {material} нужны цена и её источник")
    updated = dict(inputs)
    updated["plastic_rub_per_kg"] = entry["price_rub_per_kg"]
    return updated, {"name": material, "price_rub_per_kg": entry["price_rub_per_kg"], "source": source}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Рассчитать экономику из JSON в стандартном вводе")
    parser.add_argument("--material", help="название материала из config/production.yaml, например PLA или PETG")
    parser.add_argument("--production", type=Path, help="путь к файлу производства в JSON-синтаксисе YAML")
    args = parser.parse_args(argv)
    if args.production and not args.material:
        parser.error("--production требует --material")
    try:
        data = json.load(sys.stdin, parse_float=Decimal)
        if not isinstance(data, dict) or "inputs" not in data or data.keys() - {"inputs", "limits"}:
            raise CalculationError("Ожидается объект с полями inputs и необязательным limits")
        inputs = data["inputs"]
        material_info = None
        if args.material:
            inputs, material_info = add_material_price(inputs, args.material, args.production or DEFAULT_PRODUCTION)
        result = calculate(inputs, data.get("limits"))
        if material_info is not None:
            result["material_input"] = material_info
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (CalculationError, ValueError) as exc:
        print(f"Ошибка расчёта: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
