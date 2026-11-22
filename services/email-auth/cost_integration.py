"""Resolve marketplace bindings and apply versioned production costs after provider reads."""

from decimal import Decimal, localcontext

import cost_catalog
from market_specs import money, result

COMPOSITES = {"article_map", "unit_economics", "supply_plan", "sales_drop_check"}
COST_REPORTS = {"unit_economics", "supply_plan"}
COST_REASONS = {
    "cost_card_not_found": "Артикул ещё не привязан к карточке калькулятора.",
    "cost_product_not_active": "На нужную дату карточка не в статусе «В работе».",
    "cost_version_not_effective": "На начало периода ещё не действовала версия себестоимости.",
    "cost_card_incomplete": "В выбранной версии не заполнены данные для расчёта себестоимости.",
    "multiple_cost_versions": "За период действовало несколько версий себестоимости.",
    "sales_units_unavailable": "Количество продаж за период не подтверждено.",
    "no_sold_units": "Продаж за период не было.",
    "net_after_expenses_unavailable": "Нет подтверждённой суммы после расходов площадки.",
    "settlement_currency_mismatch": "Нет подтверждения, что валюта отчёта совпадает с валютой себестоимости.",
    "marketplace_expenses_incomplete": "Площадка не подтвердила все расходы.",
    "recommended_quantity_unavailable": "Рекомендуемое количество для поставки не подтверждено.",
}


def prepare(store, name, args):
    provider = name.split("_", 1)[0]
    selector = next((k for k in ("article", "offer_id", "vendor_code") if k in args), None)
    aliases = {"ozon": "offer_ids", "wb": "vendor_codes"}.get(provider)
    if selector is None and aliases in args:
        codes, products = [], {}
        for value in args[aliases]:
            known = store.resolve(value)
            if known and value == known["article"]:
                linked = [p["code"] for p in known["links"] if p["marketplace"] == provider]
                if linked:
                    products[known["article"]] = known
                    codes.extend(linked)
                    continue
            codes.append(value)
        selected = list(dict.fromkeys(codes))
        if not selected:
            return args, None, None
        if len(selected) > (100 if provider == "ozon" else 50):
            return args, None, result([], complete=False, status="too_many_marketplace_bindings")
        card = next(iter(products.values())) if len(products) == 1 and len(args[aliases]) == 1 else None
        return dict(args, **{aliases: selected}), card, None
    if not selector or (name not in COMPOSITES and provider not in ("ozon", "wb")):
        return args, None, None
    card = store.resolve(args[selector])
    if not card:
        return args, None, None
    if name == "article_map":
        return args, card, result([{"internal_article": card["article"], "name": card["name"],
                                    "links": card["links"], "status": card["status"]}],
                                  internal_article=card["article"], mapping_source="cost_calculator",
                                  read_only=True, untrusted_data=True)
    choices = {market: [p for p in card["links"] if p["marketplace"] == market] for market in ("ozon", "wb")}
    if name in COMPOSITES:
        updated = dict(args)
        fields = {"ozon": "ozon_offer_id", "wb": "wb_vendor_code"}
        mapping = dict(args.get("mapping") or {})
        for market, field in fields.items():
            codes = {p["code"] for p in choices[market]}
            if field in mapping and mapping[field] not in codes:
                return args, card, result([], complete=False, status="binding_conflict", internal_article=card["article"])
            if not codes:
                return args, card, result([], complete=False, status="marketplace_binding_missing", marketplace=market,
                                          internal_article=card["article"])
            if field not in mapping:
                if len(codes) != 1:
                    return args, card, result([], complete=False, status="multiple_marketplace_bindings",
                                              internal_article=card["article"], links=card["links"])
                mapping[field] = next(iter(codes))
        nms = {p["marketplace_id"] for p in choices["wb"]
               if p["code"] == mapping["wb_vendor_code"] and p["marketplace_id"]}
        if "wb_nm_id" in mapping and str(mapping["wb_nm_id"]) not in nms:
            return args, card, result([], complete=False, status="binding_conflict", internal_article=card["article"])
        if "wb_nm_id" not in mapping and len(nms) == 1:
            mapping["wb_nm_id"] = next(iter(nms))
        elif "wb_nm_id" not in mapping and len(nms) > 1:
            return args, card, result([], complete=False, status="multiple_marketplace_bindings",
                                      internal_article=card["article"], links=card["links"])
        updated["mapping"] = mapping
        return updated, card, None
    sources = choices[provider]
    entity = "sku" if provider == "ozon" else "nm_id"
    if entity in args:
        sources = [p for p in sources if p["marketplace_id"] in (None, str(args[entity]))]
    explicit = {p["code"] for p in sources if args[selector] == p["code"] and
                (selector != "article" or args[selector] != card["article"])}
    codes = explicit or {p["code"] for p in sources}
    if len(codes) != 1:
        return args, card, result([], complete=False,
                                  status="multiple_marketplace_bindings" if codes else "marketplace_binding_missing",
                                  internal_article=card["article"], links=choices[provider])
    return dict(args, **{selector: next(iter(codes))}), card, None


def _version(store, card, first=None, last=None):
    if card is None:
        return None, "cost_card_not_found", []
    version = store.at(card["article"], first, redact_exact=False)
    if version is None:
        return None, "cost_version_not_effective", []
    if version["product"]["status"] != "active":
        return None, "cost_product_not_active", []
    if version["calculation"]["status"] != "calculated":
        return None, "cost_card_incomplete", version["calculation"].get("missing", [])
    if last and any(row["effective_from"] > first for row in store.history(card["article"], first, last)):
        return None, "multiple_cost_versions", []
    return version, "calculated", []


def _set_cost_status(data, status, missing):
    data.update(production_cost_status=status, production_cost_reason=COST_REASONS.get(status))
    if missing:
        data["production_cost_missing_fields"] = missing


def _finance(store, request, card, data):
    from seller_views import period

    first, last = (d.isoformat() for d in period(request))
    supplied = "cost_per_unit" in request
    version, status, missing = (None, "provided", []) if supplied else _version(store, card, first, last)
    _set_cost_status(data, status, missing)
    if not version:
        for row in data.get("items", []):
            if "marketplace" not in row:
                continue
            reason = status
            if supplied:
                reason = _margin_source_status(row, request["currency"])
                if reason == "calculated" and row.get("margin_per_unit") is None:
                    reason = "marketplace_expenses_incomplete"
            row.update(margin_status=reason, margin_reason=COST_REASONS.get(reason))
            row.setdefault("margin_per_unit", None)
        return
    cost = Decimal(version["calculation"]["exact"])
    data["production_cost"] = {"version": version["version"], "effective_from": version["effective_from"],
                               "amount": cost_catalog.display(cost), "currency": "RUB"}
    for row in data.get("items", []):
        net = row.get("net_after_expenses") or {}
        total, gross = money(net.get("amount")), money((row.get("amounts") or {}).get("gross_sales"))
        units = row.get("sold_units")
        if "marketplace" not in row:
            continue
        source_status = _margin_source_status(row, "RUB")
        if source_status != "calculated":
            row.update(margin_per_unit=None, margin_status=source_status, margin_reason=COST_REASONS[source_status])
            continue
        profit = total - cost * units
        detail = {"margin_per_unit": cost_catalog.display(profit / units),
                  "margin_percent": cost_catalog.display(profit * 100 / gross) if gross and gross > 0 else None,
                  "profit_for_period": cost_catalog.display(profit), "cost_version": version["version"],
                  "production_cost_per_unit": cost_catalog.display(cost), "profit_currency": "RUB",
                  "profit_basis": "reported_net_after_marketplace_expenses_less_production_cost_per_gross_sold_unit",
                  "profit_status": "calculated" if net.get("complete") else "estimate_from_partial_settlement",
                  "margin_status": "calculated" if net.get("complete") else "estimate_from_partial_settlement",
                  "margin_reason": None if net.get("complete") else COST_REASONS["marketplace_expenses_incomplete"]}
        row.update(detail)
        if "month" in request:
            store.save_margin(card["article"], row["marketplace"], request["month"], detail)


def _margin_source_status(row, currency):
    units = row.get("sold_units")
    if type(units) is not int or units < 0:
        return "sales_units_unavailable"
    if units == 0:
        return "no_sold_units"
    if row.get("currency") != currency:
        return "settlement_currency_mismatch"
    if money((row.get("net_after_expenses") or {}).get("amount")) is None:
        return "net_after_expenses_unavailable"
    return "calculated"


def _production_plan(store, card, data):
    version, status, missing = _version(store, card)
    _set_cost_status(data, status, missing)
    calc = version["calculation"] if version else None
    for row in data.get("items", []):
        if "marketplace" not in row:
            continue
        total = row.get("marketplace_total") or {}
        count = total.get("ship_by_destination_after_min_batch", total.get("recommended_quantity"))
        count = count if type(count) is int and count >= 0 else None
        plan_status = status if not calc or count is not None else "recommended_quantity_unavailable"
        requirements = {"status": plan_status, "reason": COST_REASONS.get(plan_status),
                        "quantity": count, "currency": "RUB", "version": version["version"] if version else None,
                        "cost": None, "cost_per_unit": None, "plastic_g_per_unit": None, "plastic_kg": None,
                        "print_hours": None, "materials": None}
        if calc:
            requirements.update(cost_per_unit=calc["amount"], plastic_g_per_unit=calc["plastic_grams"])
            if count is not None:
                requirements.update(
                    cost=cost_catalog.display(Decimal(calc["exact"]) * count),
                    print_hours=format(Decimal(calc["print_hours"]) * count, "f"),
                    plastic_kg=format(Decimal(calc["plastic_grams"]) * count / 1000, "f"),
                    materials=[{"name": material["name"], "kg": format(Decimal(material["grams"]) * count / 1000, "f")}
                               for material in calc["materials"]],
                )
        row["production_requirements"] = requirements


def enrich(store, name, request, card, data):
    if card:
        data["internal_article"] = card["article"]
    if data.get("status") == "pending":
        return data
    if name not in COMPOSITES:
        if not card:
            return data
        first = request.get("date_from") or request.get("date") or cost_catalog.today()
        last = request.get("date_to", first)
        if version := store.uniform(card["article"], first, last):
            data["production_cost"] = {"version": version["version"], "amount": version["calculation"]["amount"],
                                       "currency": "RUB", "effective_from": version["effective_from"]}
        return data
    with localcontext() as ctx:
        ctx.prec = 52
        if name == "unit_economics":
            _finance(store, request, card, data)
        elif name == "supply_plan":
            _production_plan(store, card, data)
        elif name == "sales_drop_check" and card:
            for row in data.get("items", []):
                margins = {}
                for key, a, b in (("previous", "previous_from", "previous_to"), ("current", "date_from", "date_to")):
                    version = store.uniform(card["article"], request[a], request[b])
                    period = row.get(key, {})
                    price = money(period.get("ordered_amount_per_unit"))
                    if version and price is not None and period.get("currency") == "RUB":
                        cost = Decimal(version["calculation"]["exact"])
                        margins[key] = dict(cost_version=version["version"], cost=cost_catalog.display(cost),
                                            price=cost_catalog.display(price), margin=cost_catalog.display(price - cost))
                if len(margins) == 2:
                    row["production_margin_change"] = dict(margins, currency="RUB",
                        basis="order_value_less_production_cost_before_marketplace_expenses",
                        difference=cost_catalog.display(Decimal(margins["current"]["margin"]) -
                                                       Decimal(margins["previous"]["margin"])))
    return data
