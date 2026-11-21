"""Resolve marketplace bindings and apply versioned production costs after provider reads."""

from datetime import date
from decimal import Decimal, localcontext

import cost_catalog
from market_specs import money, result

COMPOSITES = {"article_map", "unit_economics", "supply_plan", "sales_drop_check"}


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
    if card["status"] != "active" and name in COMPOSITES:
        return args, card, result([], complete=False, status="cost_product_not_active", internal_article=card["article"])
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


def _finance(store, request, card, data):
    if "cost_per_unit" in request:
        return
    from seller_views import period

    first, last = (d.isoformat() for d in period(request))
    version = store.uniform(card["article"], first, last)
    if not version:
        data["production_cost_status"] = "no_uniform_effective_version"
        return
    cost = Decimal(version["calculation"]["exact"])
    data["production_cost"] = {"version": version["version"], "effective_from": version["effective_from"],
                               "amount": cost_catalog.display(cost), "currency": "RUB"}
    for row in data.get("items", []):
        net = row.get("net_after_expenses") or {}
        total, gross = money(net.get("amount")), money((row.get("amounts") or {}).get("gross_sales"))
        units = row.get("sold_units")
        if total is None or type(units) is not int or units <= 0 or row.get("currency") != "RUB":
            continue
        profit = total - cost * units
        detail = {"margin_per_unit": cost_catalog.display(profit / units),
                  "margin_percent": cost_catalog.display(profit * 100 / gross) if gross and gross > 0 else None,
                  "profit_for_period": cost_catalog.display(profit), "cost_version": version["version"],
                  "production_cost_per_unit": cost_catalog.display(cost), "profit_currency": "RUB",
                  "profit_basis": "reported_net_after_marketplace_expenses_less_production_cost_per_gross_sold_unit",
                  "profit_status": "calculated" if net.get("complete") else "estimate_from_partial_settlement"}
        row.update(detail)
        if "month" in request:
            store.save_margin(card["article"], row["marketplace"], request["month"], detail)


def enrich(store, name, request, card, data):
    data["internal_article"] = card["article"]
    if data.get("status") == "pending":
        return data
    if name not in COMPOSITES:
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
            version = store.at(card["article"], None, redact_exact=False)
            if not version or version["product"]["status"] != "active" or version["calculation"]["status"] != "calculated":
                return data
            calc = version["calculation"]
            for row in data.get("items", []):
                total = row.get("marketplace_total") or {}
                count = total.get("ship_by_destination_after_min_batch", total.get("recommended_quantity"))
                if type(count) is int and count >= 0:
                    row["production_requirements"] = {
                        "quantity": count, "version": version["version"], "currency": "RUB",
                        "cost": cost_catalog.display(Decimal(calc["exact"]) * count),
                        "print_hours": format(Decimal(calc["print_hours"]) * count, "f"),
                        "materials": [{"name": m["name"], "kg": format(Decimal(m["grams"]) * count / 1000, "f")}
                                      for m in calc["materials"]],
                    }
        elif name == "sales_drop_check":
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
