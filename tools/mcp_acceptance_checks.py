"""Scenario checks for the isolated MCP runner. Only fixed codes and counts enter evidence."""

import calendar
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from email.utils import parsedate_to_datetime

OZ, WB = "300030", "3000030"
MOSCOW = timezone(timedelta(hours=3))
IDS = ("ozon_product_info", "wb_products")
SCENARIOS = {
    1: (*IDS, "wb_supplies", "wb_supply_goods", "ozon_supply_orders", "ozon_supply_items"),
    2: (*IDS, "wb_stocks", "ozon_stocks_by_cluster", "ozon_stocks", "supply_plan"),
    3: (*IDS, "wb_orders", "ozon_fbo_postings_geo", "ozon_clusters"),
    4: ("wb_acceptance_slots",),
    5: (*IDS, "supply_plan", "wb_stocks", "ozon_stocks_by_cluster", "wb_orders", "ozon_fbo_postings_geo"),
    6: ("wb_products", "wb_finance_report"),
    7: ("ozon_cash_flow",),
    8: (*IDS, "wb_ads", "ozon_ads"),
    9: ("ozon_product_info", "ozon_sku_daily", "ozon_stocks_by_cluster"),
}
CURSORS = dict.fromkeys(
    (
        "wb_supply_goods",
        "wb_stocks",
        "wb_supplies",
        "ozon_stocks_by_cluster",
        "ozon_sku_daily",
        "wb_ads",
        "ozon_ads",
    ),
    "offset",
)
CURSORS.update(
    dict.fromkeys(
        (
            "ozon_supply_orders",
            "ozon_supply_items",
            "ozon_fbo_postings_geo",
            "ozon_stocks",
            "wb_orders",
            "wb_finance_report",
        ),
        "cursor",
    )
)
CURSORS["ozon_cash_flow"] = "page"
ASYNC_TOOLS = {"wb_stocks", "ozon_ads"}
CATEGORIES = {
    "wb_products": "Контент",
    "wb_supplies": "Поставки",
    "wb_supply_goods": "Поставки",
    "wb_stocks": "Аналитика",
    "wb_orders": "Статистика",
    "wb_acceptance_slots": "Любая категория WB",
    "wb_finance_report": "Финансы",
    "wb_ads": "Продвижение",
    "ozon_ads": "Ozon Performance",
    "ozon_product_info": "Каталог Ozon Seller",
    "supply_plan": "Ozon FBO / WB FBW",
}


def category(tool):
    return CATEGORIES.get(tool, "Ozon Seller API")


class Stop(Exception):
    def __init__(self, status, code, delay=None, *, http=None, category=None):
        super().__init__(code)
        self.status, self.code, self.delay, self.http, self.category = status, code, delay, http, category


def need(value, code):
    if not value:
        raise Stop("partial", code)


def positive(value):
    n = int(value)
    if n <= 0:
        raise ValueError("positive integer required")
    return n


def num(value):
    if isinstance(value, bool) or value is None:
        raise Stop("partial", "numeric_value_unknown")
    n = Decimal(str(value))
    need(n.is_finite(), "nonfinite_value")
    return n


def units(value):
    need(type(value) is int and value >= 0, "quantity_unknown")
    return value


def retry_seconds(value):
    if type(value) not in (int, str):
        return None
    text = str(value).strip()
    if text.isascii() and text.isdigit() and len(text) <= 10:
        return int(text)
    if len(text) <= 128:
        try:
            when = parsedate_to_datetime(text)
            if when.tzinfo:
                return max(0, math.ceil(when.timestamp() - time.time()))
        except (ValueError, OverflowError, TypeError, OSError):
            pass
    return None


def source_outcome(data, error=False):
    status = data.get("status")
    tag = data.get("error") if error or "error" in data else status
    http = data.get("http_status")
    if data.get("warehouse_catalog_http_status") in (401, 403):
        return Stop(
            "blocked",
            "provider_access_denied",
            http=data["warehouse_catalog_http_status"],
            category="Поставки",
        )
    delay = retry_seconds(data.get("retry_after_seconds"))
    if tag in ("provider_access_denied", "access_denied", "not_configured") or http in (401, 403):
        grant = data.get("required_category")
        return Stop(
            "blocked",
            "not_configured" if tag == "not_configured" else "provider_access_denied",
            http=http if http in (401, 403) else None,
            category=grant if grant in CATEGORIES.values() or grant == "Ozon Seller API" else None,
        )
    # Aggregate tools can embed a provider cooldown or refusal in the progress of a single source.
    if tag == "pending" or data.get("complete") is False:
        nested = [*data.get("issues", [])]
        for entry in data.get("items", []):
            if isinstance(entry, dict) and "marketplace" in entry:
                nested += entry.get("pending", [])
                if entry.get("status") == "not_configured":
                    return Stop("blocked", "not_configured")
        for item in nested:
            if isinstance(item, dict):
                reason = source_outcome(
                    {
                        k: item[k]
                        for k in ("http_status", "required_category", "retry_after_seconds", "status")
                        if k in item
                    }
                    | ({"status": "rate_limited"} if item.get("code") == "method_cooldown" else {})
                )
                if reason:
                    return reason
    limited = tag == "rate_limited" or http == 429 or data.get("rate_limited") is True
    if limited or tag == "pending":
        return Stop(
            "pending",
            "rate_limited" if limited else "report_pending",
            delay,
            http=429 if limited else None,
        )
    if tag == "report_unavailable":
        return Stop(
            "blocked",
            "report_expired"
            if data.get("provider_status") in ("purged", "canceled", "EXPIRED")
            else "report_unavailable",
        )
    if tag in ("outside_provider_retention", "upstream_disabled"):
        return Stop("blocked", tag)
    if error or tag not in (
        None,
        "ready",
        "reported",
        "observed_history",
        "choose_campaign_ids",
        "end_of_report",
    ):
        return Stop("partial", "history_missing" if tag == "partial_history" else "source_incomplete")
    return None


def period(start, end):
    return {"date_from": start.isoformat(), "date_to": end.isoformat()}


def shifted_month(day, count):
    y, m = divmod(day.year * 12 + day.month - 1 + count, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def mapping(r, opts):
    identity = {}
    if "ozon_product_info" in r.names:
        data = r.call("ozon_product_info", {"offer_ids": [OZ]})
        r.evidence["tools"]["ozon_product_info"].update(pages=1, rows=len(data.get("items", [])))
        need(
            all(data.get(k) == [] for k in ("conflicts", "unmatched_identifiers", "unmatched_row_indices"))
            and len(data["items"]) == 1,
            "ambiguous_ozon_article",
        )
        card = data["items"][0]
        need(card["offer_id"] == OZ and not card["incomplete_fields"], "incomplete_ozon_article")
        skus = {str(positive(s)) for s in card["sku_candidates"]}
        need(skus and set(map(str, opts.expect_ozon_sku)) <= skus, "expected_sku_missing")
        schemes = defaultdict(set)
        for source in card["sources"] + card.get("stock_sources", []):
            schemes[str(source["sku"])].add(source["source"].upper())
        identity.update(skus=skus, fbo={s for s in skus if schemes[s] != {"FBS"}})
        r.evidence["catalog_sku_count"] = len(skus)
    if "wb_products" in r.names:
        args, seen, nm_ids = {"limit": 100}, set(), set()
        while True:
            data = r.call("wb_products", args)
            if not isinstance(data.get("items"), list):
                raise Stop("partial", "wb_catalog_incomplete")
            stat = r.evidence["tools"]["wb_products"]
            stat["pages"] += 1
            stat["rows"] += len(data["items"])
            for row in data["items"]:
                if row.get("vendorCode") == WB:
                    nm_ids.add(positive(row["nmID"]))
            cursor = data.get("cursor") or {}
            count = cursor.get("total")
            need(
                type(count) is int and count == len(data["items"]) and count <= args["limit"],
                "wb_catalog_completion_unknown",
            )
            if count < args["limit"]:
                break
            nxt = (cursor.get("updatedAt"), cursor.get("nmID"))
            need(all(nxt) and nxt not in seen, "wb_catalog_cursor_invalid")
            seen.add(nxt)
            args.update(updated_at=nxt[0], nm_id=nxt[1])
        need(len(nm_ids) == 1, "wb_article_not_unique")
        identity["nm"] = nm_ids.pop()
    if identity:
        r.evidence["checks"].append("exact_catalog_identity")
    return identity


def supplies(r, opts, ids):
    dates = period(shifted_month(opts.as_of, -6), opts.as_of - timedelta(days=1))
    r.evidence["period"] = {**dates, "wb": "creation_moscow", "ozon": "timeslot_utc"}
    wb_matched = oz_matched = 0
    seen = set()
    # A one-supply page preserves Retry-After when WB throttles its separate detail endpoint.
    for page in r.pages("wb_supplies", **dates, mode="details", limit=1):
        need(page.get("period_basis") == "supply_creation_moscow", "supply_period_mismatch")
        for entry in page["items"]:
            pre = not entry.get("supply_id")
            sid = positive(entry.get("preorder_id") if pre else entry["supply_id"])
            need((pre, sid) not in seen, "duplicate_supply")
            seen.add((pre, sid))
            for row in r.rows(
                "wb_supply_goods",
                supply_id=sid,
                is_preorder=pre,
                article=WB,
                nm_id=ids["nm"],
                mode="details",
            ):
                need(
                    row.get("article") == WB and str(row.get("nm_id")) == str(ids["nm"]), "wb_item_mismatch"
                )
                units(row["quantity"])
                units(row["accepted_quantity"])
                wb_matched += 1
    seen.clear()
    for page in r.pages("ozon_supply_orders", **dates, mode="details"):
        need(page.get("period_basis") == "timeslot_start_utc", "supply_period_mismatch")
        for order in page["items"]:
            for supply in order["supplies"]:
                key = (positive(order["order_id"]), positive(supply["supply_id"]), supply["bundle_id"])
                need(key not in seen, "duplicate_supply")
                seen.add(key)
                declared, acts = set(), {}
                for part in r.pages(
                    "ozon_supply_items",
                    order_id=key[0],
                    supply_id=key[1],
                    bundle_id=key[2],
                    article=OZ,
                    mode="details",
                ):
                    for row in part["items"]:
                        sku = str(row["sku"])
                        need(
                            row["article"] == OZ and sku in ids["skus"] and sku not in declared,
                            "supply_sku_mismatch",
                        )
                        units(row["declared_quantity"])
                        declared.add(sku)
                    page_acts = set()
                    for act in part["acts"]:
                        sku = str(act["sku"])
                        need(
                            sku in ids["skus"]
                            and act["article"] == OZ
                            and part.get("agreement_completed") is True
                            and act["act_state"] == "ACCEPTED",
                            "receipt_unconfirmed",
                        )
                        identity = (str(act["act_id"]), act["type"], sku)
                        values = (units(act["fact_quantity"]), units(act["approved_quantity"]))
                        need(
                            identity not in page_acts
                            and (identity not in acts or acts[identity] == values),
                            "conflicting_receipt",
                        )
                        page_acts.add(identity)
                        acts[identity] = values
                received = {s for _, kind, s in acts if kind == "ACCEPTANCE"}
                need(
                    declared <= received
                    and len(received) == sum(1 for _, kind, _ in acts if kind == "ACCEPTANCE"),
                    "receipt_missing_or_ambiguous",
                )
                oz_matched += len(declared)
    need(wb_matched and oz_matched, "no_matched_supply_data")
    r.evidence["matched_article_positions"] = {"wb": wb_matched, "ozon": oz_matched}
    r.evidence["checks"].append("declared_and_accepted_positions_observed")


def stock_rows(r, ids):
    wb = r.rows("wb_stocks", article=WB, nm_id=ids["nm"], mode="details")
    oz = r.rows("ozon_stocks_by_cluster", article=OZ, mode="details")
    need(wb and oz, "stock_not_observed")
    need(ids["fbo"] <= {str(x.get("sku")) for x in oz}, "expected_sku_missing")
    positions = set()
    stock = {"wb": defaultdict(int), "ozon": defaultdict(int)}
    totals, by_variant, transit = {}, defaultdict(int), defaultdict(set)
    for x in wb:
        need(x.get("article") == WB and str(x.get("nm_id")) == str(ids["nm"]), "wb_item_mismatch")
        n = units(x["quantity"])
        key = (x.get("barcode"), x["kind"], x["warehouse"])
        need(key[0] and key not in positions, "stock_identity_ambiguous")
        positions.add(key)
        if x["kind"] == "warehouse":
            stock["wb"][x["warehouse"]] += n
            by_variant[key[0]] += n
        elif x["kind"] == "provider_total":
            totals[key[0]] = n
        else:
            need(x["kind"] in ("in_transit_to_customer", "returning_from_customer"), "unknown_transit")
            transit[key[0]].add(x["kind"])
    need(stock["wb"] and by_variant == totals, "wb_stock_total_unverified")
    if r.options.scenario == 2:
        need(all(len(transit[variant]) == 2 for variant in totals), "wb_customer_transit_not_reported")
    positions.clear()
    clusters, inbound = {}, defaultdict(int)
    for x in oz:
        key = (str(x["sku"]), str(positive(x["warehouse_id"])))
        need(x["article"] == OZ and key[0] in ids["skus"] and key not in positions, "ozon_stock_mismatch")
        cid = str(positive(x["cluster_id"]))
        need(key[1] not in clusters or clusters[key[1]] == cid, "cluster_assignment_conflict")
        clusters[key[1]] = cid
        inbound[key[1]] += units(x["transit_stock_count"])
        stock["ozon"][key[1]] += units(x["available_stock_count"])
        positions.add(key)
    return stock, clusters, inbound


def order_counts(r, opts, ids):
    levels = (7, 30, 90)
    counts = {"wb": defaultdict(lambda: [0, 0, 0]), "ozon": defaultdict(lambda: [0, 0, 0])}
    r.evidence["order_period"] = {
        "windows_days": list(levels),
        "wb_statistics": "preliminary_rolling_90_days",
    }
    for provider, tool, selector in (
        ("wb", "wb_orders", {"article": WB, "nm_id": ids["nm"]}),
        ("ozon", "ozon_fbo_postings_geo", {"article": OZ}),
    ):
        end = opts.order_as_of[provider] - timedelta(days=1)
        window = period(end - timedelta(days=89), end)
        r.evidence["order_period"][provider] = {
            **window,
            "basis": "posting_creation_utc" if provider == "ozon" else "event_date_moscow",
        }
        seen = {}
        for page in r.pages(tool, **window, **selector, mode="details"):
            for x in page["items"]:
                if provider == "wb":
                    need(x["article"] == WB and str(x["nm_id"]) == str(ids["nm"]), "wb_item_mismatch")
                    if x["warehouse_type"] == "Склад продавца":
                        continue
                    need(x["warehouse_type"] == "Склад WB", "unknown_fulfillment_scheme")
                    ident, location, stamp, zone = x["srid"], x["warehouse"], x["date"], MOSCOW
                else:
                    need(x["article"] == OZ and str(x["sku"]) in ids["skus"], "ozon_order_sku_mismatch")
                    ident = (x["posting_number"], str(x["sku"]))
                    need(ident[0], "order_id_missing")
                    location, stamp, zone = str(positive(x["warehouse_id"])), x["created_at"], timezone.utc
                need(ident and location, "order_identity_missing")
                t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                need(t.tzinfo, "order_timezone_missing")
                day, qty = t.astimezone(zone).date(), units(x["quantity"])
                age = (end - day).days
                need(0 <= age < 90, "order_outside_window")
                event = (location, day, qty)
                if ident in seen:
                    need(seen[ident] == event, "conflicting_order_revision")
                    continue
                seen[ident] = event
                for i, days in enumerate(levels):
                    if age < days:
                        counts[provider][location][i] += qty
        need(seen, "no_observed_order_evidence")
        warehouse_counts = counts[provider]
        for i, days in enumerate(levels):
            expected = sum(qty for _, day, qty in seen.values() if (end - day).days < days)
            need(sum(v[i] for v in warehouse_counts.values()) == expected, "order_window_total_mismatch")
        r.evidence["order_period"][provider].update(
            observed_warehouses=len(warehouse_counts),
            verified_warehouse_windows=len(warehouse_counts) * len(levels),
        )
    return counts


def plan(r, opts, ids, stocks, counts=None):
    stock, clusters, inbound_by_wh = stocks
    (page,) = r.pages(
        "supply_plan",
        article="Вершина",
        horizon_days=21,
        mapping={"ozon_offer_id": OZ, "wb_vendor_code": WB, "wb_nm_id": ids["nm"]},
    )
    need(
        page.get("account_coverage") == "partial" and page.get("baseline_days") == 30, "plan_scope_mismatch"
    )
    platforms = {p["marketplace"]: p for p in page["items"]}
    need(len(page["items"]) == 2 and set(platforms) == {"wb", "ozon"}, "marketplace_missing")
    for provider, p in platforms.items():
        need(
            p.get("complete") is True
            and p.get("scenario_recommendations_complete") is True
            and p.get("orders_through") == (opts.order_as_of[provider] - timedelta(days=1)).isoformat()
            and p.get("orders_date_basis")
            == ("posting_creation_utc" if provider == "ozon" else "event_date_moscow")
            and p.get("fulfillment_scope") == ("Ozon FBO" if provider == "ozon" else "Wildberries FBW"),
            "plan_period_or_scope_incomplete",
        )
        locations, local_rows = {}, {}
        for row in p["locations"]:
            loc = str(row["warehouse_id"]) if provider == "ozon" else row["warehouse"]
            need(loc not in locations, "duplicate_warehouse")
            locations[loc] = units(row["available_units"])
            local_rows[loc] = row
            if provider == "ozon":
                need(
                    str(row["cluster_id"]) == clusters[loc] and row["inbound_units"] == inbound_by_wh[loc],
                    "warehouse_cluster_or_inbound_mismatch",
                )
            if counts is not None:
                need(
                    [row["ordered_units"][d] for d in ("7", "30", "90")]
                    == counts[provider].get(loc, [0] * 3),
                    "order_totals_mismatch",
                )
        need(locations and locations == stock[provider], "warehouse_stock_mismatch")
        r.evidence.setdefault("plan_orders_through", {})[provider] = p["orders_through"]
        r.evidence.setdefault("warehouses_reconciled", {})[provider] = len(locations)
        if counts is not None:
            need(counts[provider].keys() <= locations.keys(), "order_warehouse_missing")
        destinations = p["clusters"] if provider == "ozon" else p["locations"]
        need(destinations, "plan_destination_missing")
        rollups = [(p["marketplace_total"], list(local_rows.values()))]
        if provider == "ozon":
            need(
                {str(c["cluster_id"]) for c in destinations} == set(clusters.values())
                and len(destinations) == len(set(clusters.values())),
                "plan_cluster_coverage_missing",
            )
            for c in destinations:
                rollups.append(
                    (c, [v for loc, v in local_rows.items() if clusters[loc] == str(c["cluster_id"])])
                )
        for aggregate, origins in rollups:
            need(
                units(aggregate["available_units"]) == sum(x["available_units"] for x in origins)
                and all(
                    units(aggregate["ordered_units"][str(d)])
                    == sum(units(x["ordered_units"][str(d)]) for x in origins)
                    for d in (7, 30, 90)
                ),
                "warehouse_rollup_mismatch",
            )
            arrivals = [x["inbound_units"] for x in origins]
            need(
                aggregate["inbound_units"] == (None if None in arrivals else sum(map(units, arrivals))),
                "inbound_rollup_mismatch",
            )
        for row in [*p["locations"], *p["clusters"], p["marketplace_total"]]:
            avail, n = units(row["available_units"]), units(row["ordered_units"]["30"])
            gross = max(0, (21 * n + 29) // 30 - avail)
            inbound = row["inbound_units"]
            net = None if inbound is None else max(0, gross - units(inbound))
            for d in (7, 30, 90):
                need(
                    abs(num(row["orders_per_day"][str(d)]) * d - units(row["ordered_units"][str(d)]))
                    < Decimal("0.0000001"),
                    "demand_rate_mismatch",
                )
            need(
                row["recommendation_without_inbound"] == gross
                and row["recommendation_if_inbound_arrives"] == net,
                "recommendation_mismatch",
            )
            need(
                (
                    row["days_of_cover"] is None
                    if not n
                    else abs(num(row["days_of_cover"]) * n - avail * 30) < Decimal("0.0000001")
                ),
                "coverage_mismatch",
            )
            if provider == "wb" or (inbound != 0 and gross):
                need(row["confirmed_recommended_quantity"] is None, "inbound_scenario_claimed_as_confirmed")
        need(
            sum(x["recommendation_without_inbound"] for x in destinations)
            == p["marketplace_total"]["ship_without_transfers_without_inbound"],
            "destination_sum_mismatch",
        )
        net = [x["recommendation_if_inbound_arrives"] for x in destinations]
        need(
            p["marketplace_total"]["ship_without_transfers_if_inbound_arrives"]
            == (None if None in net else sum(net)),
            "destination_inbound_sum_mismatch",
        )
    r.evidence["checks"].append("warehouse_stock_coverage_and_scenarios_reconciled")


def slots(r, opts):
    if not opts.warehouse_id:
        raise Stop("blocked", "warehouse_required")
    days = {(opts.as_of + timedelta(days=i)).isoformat() for i in range(14)}
    r.evidence["period"] = {
        **period(opts.as_of, opts.as_of + timedelta(days=13)),
        "timezone": "Europe/Moscow",
    }
    for warehouse in set(opts.warehouse_id):
        (page,) = r.pages(
            "wb_acceptance_slots", warehouse_id=warehouse, include_warehouses=True, mode="details"
        )
        need(
            page.get("horizon_days") == 14
            and page.get("warehouses_status") == "reported"
            and any(str(x.get("warehouse_id")) == str(warehouse) for x in page["warehouses"]),
            "slot_directory_unverified",
        )
        boxes = defaultdict(set)
        for x in page["items"]:
            coefficient = num(x["coefficient"])
            box = positive(x["box_type_id"])
            need(
                str(x["warehouse_id"]) == str(warehouse)
                and x["date"] in days
                and x["date"] not in boxes[box]
                and type(x["allow_unload"]) is bool
                and x["available"] is (coefficient >= 0 and x["allow_unload"]),
                "slot_observation_invalid",
            )
            boxes[box].add(x["date"])
        need(boxes and all(observed == days for observed in boxes.values()), "slot_dates_missing")
    r.evidence["checks"].append("fourteen_dates_for_each_reported_package_type")
    r.evidence["verified_warehouses"] = len(set(opts.warehouse_id))


def finance(r, opts, ids):
    window = period(opts.month_start, opts.month_end)
    r.evidence["period"] = {**window, "basis": "wb_finance_reporting_period"}
    args = {**window, "article": WB, "nm_id": ids["nm"]}
    (summary,) = r.pages("wb_finance_report", **args, mode="summary")
    need(
        summary.get("summary_scope") == "requested_period"
        and summary.get("unit_basis") == "sold_units_before_returns"
        and summary.get("period_from") == window["date_from"]
        and summary.get("period_to") == window["date_to"],
        "finance_scope_mismatch",
    )
    fields = (
        "commission_ex_vat",
        "commission_vat",
        "delivery",
        "rebilled_logistics",
        "storage",
        "acceptance",
        "penalty",
    )
    costs = defaultdict(lambda: defaultdict(Decimal))
    sold, returned, seen, unallocated = defaultdict(int), defaultdict(int), set(), 0
    last = None
    for page in r.pages("wb_finance_report", **args, mode="details"):
        last = page.get("status")
        unallocated += len(page.get("unallocated", []))
        for row in page["items"]:
            rid, currency = positive(row["rrd_id"]), row["currency"]
            need(
                rid not in seen
                and row["article"] == WB
                and str(row["nm_id"]) == str(ids["nm"])
                and currency,
                "finance_row_mismatch",
            )
            seen.add(rid)
            doc = row["document"]
            if doc == row["operation"] and doc in ("Продажа", "Возврат"):
                qty = row["quantity"]
                need(type(qty) is int, "finance_unit_count_unknown")
                (sold if doc == "Продажа" else returned)[currency] += abs(qty)
            for f in fields:
                value = num(row[f])
                if f.startswith("commission_") and value:
                    need(doc in ("Продажа", "Возврат"), "commission_sign_unknown")
                    value *= -1 if doc == "Возврат" else 1
                costs[currency][f] += value
    need(
        last == "end_of_report"
        and seen
        and costs
        and summary.get("rows_observed") == len(seen)
        and summary.get("unallocated_rows") == unallocated,
        "finance_end_or_row_count_unverified",
    )
    compared = set()
    for x in summary["items"]:
        c = x["currency"]
        need(
            c in costs
            and c not in compared
            and x["article"] == WB
            and str(x["nm_id"]) == str(ids["nm"])
            and x["sold_units"] == sold[c] > 0
            and x["returned_units"] == returned[c],
            "finance_denominator_mismatch",
        )
        compared.add(c)
        costs[c]["commission_including_vat"] = costs[c]["commission_ex_vat"] + costs[c]["commission_vat"]
        for f, total in costs[c].items():
            need(
                num(x["amounts"][f]) == total
                and abs(num(x["per_sold_unit"][f]) * sold[c] - total) < Decimal("0.0000001"),
                "per_unit_cost_mismatch",
            )
    need(compared == set(costs), "finance_currency_missing")
    r.evidence.update(currency_groups_reconciled=len(compared), unallocated_rows=unallocated)
    r.evidence["checks"].append("finance_end_currency_costs_and_gross_sold_units_reconciled")


def payouts(r, opts):
    window = period(opts.month_start, opts.month_end)
    r.evidence["period"] = {**window, "basis": "ozon_statement_payment_period"}
    (summary,) = r.pages("ozon_cash_flow", **window, mode="summary")
    blocks, explained_periods = {}, set()
    for page in r.pages("ozon_cash_flow", **window, page=1, mode="details"):
        d = page["details"]
        key = statement_dates(d["period"])
        values = defaultdict(Decimal)
        need(isinstance(page["payments"], list), "payments_missing")
        for payment in page["payments"]:
            need(payment.get("currency_code"), "payment_currency_missing")
            values[payment["currency_code"]] += num(payment["payment"])
        need(key not in blocks or blocks[key] == values, "statement_period_conflict")
        blocks[key] = values
        # A payment register alone cannot explain the composition of the statement.
        for row in page["items"]:
            for field in (
                "orders_amount",
                "returns_amount",
                "services_amount",
                "commission_amount",
                "item_delivery_and_return_amount",
            ):
                if row.get(field) is not None:
                    num(row[field])
                    if row.get("period"):
                        explained_periods.add(statement_dates(row["period"]))
        for field in ("services", "others", "delivery", "return", "rfbs"):
            block = d.get(field)
            if isinstance(block, dict) and block.get("total") is not None:
                num(block["total"])
                explained_periods.add(key)
    total, next_day = defaultdict(Decimal), opts.month_start
    for (start, end), values in sorted(blocks.items()):
        need(start == next_day and start <= end, "statement_period_gap")
        next_day = end + timedelta(days=1)
        for c, value in values.items():
            total[c] += value
    need(
        next_day == opts.month_end + timedelta(days=1)
        and total
        and blocks.keys() <= explained_periods
        and summary.get("summary_scope") == "requested_period"
        and summary.get("period_from") == window["date_from"]
        and summary.get("period_to") == window["date_to"]
        and {k: num(v) for k, v in summary["payout_by_currency"].items()} == total,
        "payments_not_reconciled",
    )
    r.evidence["checks"].append("statement_periods_breakdown_and_payments_by_currency")
    r.evidence.update(statement_periods_reconciled=len(blocks), currency_groups_reconciled=len(total))


def statement_dates(period):
    start, end = (datetime.fromisoformat(period[k].replace("Z", "+00:00")) for k in ("begin", "end"))
    need(start.tzinfo and end.tzinfo, "statement_timezone_missing")
    return start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()


def advertising(r, opts, ids):
    window = period(opts.month_start, opts.month_end)
    r.evidence["period"] = {**window, "basis": "selected_campaign_reporting_dates"}
    r.evidence["attribution_scope"] = "per_campaign_no_cross_campaign_deduplication"
    for tool, id_key, chosen, limit in (
        ("wb_ads", "campaign_id", opts.wb_campaign_id, 50),
        ("ozon_ads", "id", opts.ozon_campaign_id, 10),
    ):
        wb = tool == "wb_ads"
        catalog = r.rows(tool, **window, **({"report": "campaigns"} if wb else {}))
        available = {positive(c[id_key]) for c in catalog}
        wanted = sorted(set(chosen) if chosen else available)
        need(wanted and set(wanted) <= available, "campaign_selection_incomplete")
        observed = set()
        for i in range(0, len(wanted), limit):
            selected = wanted[i : i + limit]
            claimed, direct, direct_rows = {}, defaultdict(int), set()
            args = {"article": WB, "nm_id": ids["nm"], "report": "statistics"} if wb else {"article": OZ}
            for page in r.pages(
                tool, **window, **args, campaign_ids=selected, include_order_share=True, mode="details"
            ):
                share = page.get("order_share", {})
                need(
                    share.get("status") == "ready"
                    and share.get("scope") == "selected_campaigns_separately",
                    "comparable_attribution_missing",
                )
                product_key = "nm_id" if wb else "sku"
                expected = {str(ids["nm"])} if wb else ids["skus"]
                ratios = set()
                for line in share["rows"]:
                    campaign = positive(line["campaign_id"])
                    need(
                        campaign in selected and str(line[product_key]) in expected,
                        "campaign_product_mismatch",
                    )
                    numerator, denominator = (
                        num(line["direct_attributed"]),
                        num(line["total_orders_or_units"]),
                    )
                    need(
                        0 <= numerator <= denominator
                        and denominator > 0
                        and num(line["ordered_units"]) == denominator
                        and (wb or num(line["unique_orders"]) == denominator)
                        and abs(num(line["percent"]) * denominator - 100 * numerator)
                        < Decimal("0.0000001"),
                        "ad_share_mismatch",
                    )
                    pair = (campaign, str(line[product_key]))
                    need(
                        pair not in claimed or claimed[pair] == numerator,
                        "ad_attribution_changed_between_pages",
                    )
                    claimed[pair] = numerator
                    ratios.add(pair)
                for row in page["items"]:
                    key = positive(row["campaign_id"]), str(row[product_key])
                    need(
                        key in ratios
                        and row.get("article") == args["article"]
                        and row.get("currency")
                        and row.get("date")
                        and window["date_from"] <= row["date"][:10] <= window["date_to"],
                        "ad_spend_scope_unknown",
                    )
                    num(row["spend"])
                    observed.add(key)
                    if wb and row.get("conversion_type") in ("associated", "multicard"):
                        continue
                    need(not wb or row.get("conversion_type") == "direct", "ad_conversion_type_unknown")
                    position = (key, row["date"], row.get("placement"))
                    need(position not in direct_rows, "duplicate_ad_observation")
                    direct_rows.add(position)
                    value = num(row["ordered_units"] if wb else row["orders"])
                    need(value >= 0 and value == value.to_integral(), "ad_order_count_unknown")
                    direct[key] += int(value)
            need(all(direct[k] == n for k, n in claimed.items()), "direct_attribution_mismatch")
        need(observed and {c for c, _ in observed} == set(wanted), "selected_campaign_evidence_missing")
        r.evidence.setdefault("campaigns_verified", {})[tool] = len(wanted)
    r.evidence["checks"].append("observed_spend_and_direct_ratios_by_campaign")


def trend(r, opts, ids):
    current = opts.month_start
    prior = shifted_month(current, -1)
    months = ((prior, current - timedelta(days=1)), (current, opts.month_end))
    r.evidence["periods"] = [period(start, end) for start, end in months]
    amounts = []
    for start, end in months:
        dates = period(start, end)
        sku_days, total = set(), 0
        for x in r.rows("ozon_sku_daily", **dates, article=OZ, include_funnel=True, mode="details"):
            sku, day = str(x["sku"]), date.fromisoformat(x["date"])
            need(
                x["article"] == OZ
                and sku in ids["skus"]
                and start <= day <= end
                and (sku, day) not in sku_days,
                "analytics_observation_mismatch",
            )
            for f in ("hits_view_pdp", "hits_tocart_pdp", "conv_tocart_pdp"):
                need(num(x[f]) >= 0, "funnel_metric_missing")
            n = num(x["ordered_units"])
            need(n >= 0 and n == n.to_integral(), "ordered_units_invalid")
            total += int(n)
            sku_days.add((sku, day))
        need(len(sku_days) == len(ids["skus"]) * ((end - start).days + 1), "sku_days_not_observed")
        amounts.append(total)
        observed = set()
        for page in r.pages("ozon_stocks_by_cluster", **dates, article=OZ, mode="details"):
            need(
                page.get("source") == "locally_observed_ozon_warehouse_snapshots"
                and page.get("missing_or_partial_dates") == [],
                "stock_history_incomplete",
            )
            for x in page["items"]:
                day, sku = date.fromisoformat(x["observation_date"]), str(x["sku"])
                need(
                    start <= day <= end and x["article"] == OZ and sku in ids["skus"],
                    "history_row_mismatch",
                )
                units(x["available_stock_count"])
                observed.add((sku, day))
        need(
            ids["fbo"]
            and all(
                (sku, start + timedelta(days=n)) in observed
                for sku in ids["fbo"]
                for n in range((end - start).days + 1)
            ),
            "warehouse_observations_missing",
        )
    r.evidence["checks"].append("daily_funnel_orders_and_observed_stock_coverage")
    r.evidence["causality"] = "not_established_by_observational_data"
    if opts.month == "2026-10":
        r.evidence["reported_change_reproduced_in_ordered_units"] = amounts == [64, 19]
        need(amounts == [64, 19], "reported_change_not_reproduced")


def evaluate(r, opts):
    ids = mapping(r, opts)
    with localcontext() as ctx:
        ctx.prec = 128
        if opts.scenario == 1:
            supplies(r, opts, ids)
        elif opts.scenario == 2:
            stock = stock_rows(r, ids)
            schemes = r.rows("ozon_stocks", article=OZ, mode="details")
            need(ids["skus"] == {str(x.get("sku")) for x in schemes}, "scheme_sku_coverage_missing")
            for x in schemes:
                need(x["article"] == OZ and x.get("scheme"), "stock_scheme_unknown")
                units(x["present"])
                units(x["reserved"])
            plan(r, opts, ids, stock)
        elif opts.scenario == 3:
            counts = order_counts(r, opts, ids)
            assigned = {}
            for cluster_type in ("CLUSTER_TYPE_OZON", "CLUSTER_TYPE_CIS"):
                for c in r.rows("ozon_clusters", cluster_type=cluster_type):
                    for w in c["warehouses"]:
                        wid, cid = str(positive(w["warehouse_id"])), positive(c["id"])
                        need(wid not in assigned or assigned[wid] == cid, "cluster_assignment_conflict")
                        assigned[wid] = cid
                if counts["ozon"].keys() <= assigned.keys():
                    break
            need(counts["ozon"].keys() <= assigned.keys(), "order_cluster_unknown")
            grouped = defaultdict(lambda: [0, 0, 0])
            for warehouse, windows in counts["ozon"].items():
                cluster = grouped[assigned[warehouse]]
                for i, value in enumerate(windows):
                    cluster[i] += value
            for i in range(3):
                need(
                    sum(row[i] for row in grouped.values())
                    == sum(row[i] for row in counts["ozon"].values()),
                    "order_cluster_window_mismatch",
                )
            r.evidence["order_period"]["ozon"].update(
                observed_clusters=len(grouped), verified_cluster_windows=3 * len(grouped)
            )
            r.evidence["checks"].append("unique_gross_units_by_warehouse_and_cluster_windows_7_30_90")
        elif opts.scenario == 4:
            slots(r, opts)
        elif opts.scenario == 5:
            plan(r, opts, ids, stock_rows(r, ids), order_counts(r, opts, ids))
        elif opts.scenario == 6:
            finance(r, opts, ids)
        elif opts.scenario == 7:
            payouts(r, opts)
        elif opts.scenario == 8:
            advertising(r, opts, ids)
        elif opts.scenario == 9:
            trend(r, opts, ids)
