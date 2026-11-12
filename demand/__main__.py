"""Assess sourced factor judgments from standard input as JSON."""
import json
import sys
from decimal import Decimal
from .assessment import assess


def main():
    try:
        data = json.load(sys.stdin, parse_float=Decimal)
    except (ValueError, OSError):
        print(json.dumps({"status": "blocked", "issues": ["Некорректный JSON"]}, ensure_ascii=False))
        return 2
    if not isinstance(data, dict) or set(data) - {"assessment", "weights", "threshold"}:
        print(json.dumps({"status": "blocked", "issues": ["Ожидается assessment и необязательные weights, threshold"]}, ensure_ascii=False))
        return 2
    kwargs = {key: data[key] for key in ("weights", "threshold") if key in data}
    result = assess(data.get("assessment"), **kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "assessed" else 2


if __name__ == "__main__":
    sys.exit(main())
