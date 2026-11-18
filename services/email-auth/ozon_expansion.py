"""Read-only Seller API reports. Provider and report identifiers are kept separate."""

from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import urllib.error

import ozon_fbo
import ozon_details
from market_specs import iso, matches, money, result, rows, select, validate

BASE = "https://api-seller.ozon.ru"


def _page(raw, previous):
    more, cursor = raw.get("has_next"), raw.get("cursor", raw.get("last_id"))
    if more is False or (more is None and cursor == ""):
        return None, True
    if cursor and cursor != previous:
        return cursor, False
    return None, False


def _identity(row):
    return {
        "offer_id": row.get("offer_id"),
        "article": row.get("offer_id"),
        "sku": row.get("sku"),
        "nm_id": None,
    }


def _range(args):
    return args["date_from"] + "T00:00:00Z", args["date_to"] + "T23:59:59.999999Z"


def _article_card(article, request):
    raw = request("POST", BASE + "/v3/product/info/list", body={"offer_id": [article]}, ttl=1800)
    identity = ozon_details._products({"offer_ids": [article]}, raw, None)
    cards = identity["items"]
    if (
        len(cards) != 1
        or identity["conflicts"]
        or identity["unmatched_identifiers"]
        or identity["unmatched_row_indices"]
    ):
        return None
    if set(cards[0]["incomplete_fields"]) & {"sku", "sources", "stock_sources"}:
        return None
    return cards[0]


def lookup(article, request):
    card = _article_card(article, request)
    return [int(v) for v in card["sku_candidates"]] if card else []


def _report_skus(args, request):
    if "article" not in args:
        return [int(args["sku"])] if "sku" in args else []
    known = lookup(args["article"], request)
    if "sku" not in args:
        return known
    requested = int(args["sku"])
    return [requested] if requested in known else []


def _orders(args, request):
    limit = min(args.get("limit", 20), 50)
    states = args.get("states", list(ozon_fbo.STATES))
    if not set(states) <= set(ozon_fbo.STATES):
        raise ValueError("Invalid FBO state")
    filters = {"states": states}
    if "date_from" in args:
        start, end = _range(args)
        filters["timeslot_from_range"] = {"from": start, "to": end, "timeslot_filter_type": "BY_UTC_TIME"}
    cursor = args.get("cursor", "")
    raw = request(
        "POST",
        BASE + ozon_fbo.ENDPOINTS[ozon_fbo.LIST],
        body={
            "filter": filters,
            "last_id": cursor,
            "limit": limit,
            "sort_by": "ORDER_CREATION",
            "sort_dir": "DESC",
        },
        interval=2,
        ttl=120,
    )
    listed = ozon_fbo.project_response(ozon_fbo.LIST, {"last_id": cursor, "limit": limit}, raw)
    orders, issues = [], list(listed["issues"])
    if listed["order_ids"]:
        body = {"order_ids": listed["order_ids"]}
        detail = request("POST", BASE + ozon_fbo.ENDPOINTS[ozon_fbo.GET], body=body, interval=2, ttl=120)
        parsed = ozon_fbo.project_response(ozon_fbo.GET, body, detail)
        orders, issues = parsed["orders"], issues + parsed["issues"]
    return result(
        orders,
        listed["next_cursor"],
        complete=not issues and not listed["next_cursor"],
        issues=issues,
        period_basis="timeslot_start_utc",
    )


def _items(args, request):
    bundle_args = {
        "bundle_id": args["bundle_id"],
        "limit": args.get("limit", 50),
        "last_id": args.get("cursor", ""),
    }
    params = {
        "bundle_ids": [args["bundle_id"]],
        "limit": bundle_args["limit"],
        "last_id": bundle_args["last_id"],
        "sort_field": "SKU",
        "is_asc": True,
    }
    raw = request("POST", BASE + ozon_fbo.ENDPOINTS[ozon_fbo.BUNDLE], body=params)
    data = ozon_fbo.project_response(ozon_fbo.BUNDLE, bundle_args, raw)
    items = [
        dict(row, **{"article": row["offer_id"], "nm_id": None, "declared_quantity": row["quantity"]})
        for row in data["items"]
    ]
    output = result(
        [r for r in items if matches(r, args)],
        data["next_cursor"],
        complete=data["has_next"] is False and not data["issues"],
        issues=data["issues"],
        acceptance_status="not_requested",
        acts=[],
    )
    if "supply_id" not in args:
        return output
    sid, oid = str(args["supply_id"]), str(args["order_id"])
    detail_args = {"order_ids": [oid]}
    raw = request("POST", BASE + ozon_fbo.ENDPOINTS[ozon_fbo.GET], body=detail_args, interval=2, ttl=120)
    orders = ozon_fbo.project_response(ozon_fbo.GET, detail_args, raw)["orders"]
    supplies = [s for order in orders for s in order["supplies"] or [] if str(s["supply_id"]) == sid]
    if len(supplies) != 1 or supplies[0]["bundle_id"] != args["bundle_id"]:
        output.update(acceptance_status="supply_bundle_mismatch", complete=False)
        return output
    summary = request("POST", BASE + "/v1/supply-order/act/summary/get", body={"order_id": int(oid)})
    observed = [r for r in rows(summary.get("supplies_acts")) if str(r.get("supply_id")) == sid]
    states = {}
    if len(observed) == 1:
        for act in rows(observed[0].get("supply_acts", [])):
            aid = str(act.get("act_id"))
            if act.get("act_id") is None or aid in states:
                raise ValueError("Missing or duplicate act identity")
            states[aid] = (act.get("act_state"), act.get("type"))
    actual = request("POST", BASE + "/v1/supply-order/act/product/get", body={"supply_id": int(sid)})
    if str(actual.get("supply_id")) != sid:
        output.update(complete=False, acceptance_status="act_supply_mismatch")
        return output
    seen = set()
    for act in rows(actual.get("supply_acts")):
        aid = act.get("act_id")
        if aid is None or str(aid) in seen:
            output["issues"].append("missing_or_duplicate_act_id")
        seen.add(str(aid))
        state, act_type = states.get(str(aid), (None, None))
        if not state or not act_type or act_type != act.get("type"):
            output["issues"].append("act_type_or_state_mismatch")
        for item in rows(act.get("items")):
            row = dict(
                _identity(item.get("sku_info") or {}),
                **select(item, "declared_quantity fact_quantity approved_quantity"),
                act_id=aid,
                type=act.get("type"),
                act_state=state,
            )
            for key in ("declared_quantity", "fact_quantity", "approved_quantity"):
                if row[key] is not None and (type(row[key]) is not int or row[key] < 0):
                    raise ValueError("Invalid acceptance quantity")
            if row["sku"] is None or row["article"] is None or row["fact_quantity"] is None:
                output["issues"].append("incomplete_act_item")
            if matches(row, args):
                output["acts"].append(row)
    output.update(
        acceptance_status="observed_acts" if output["acts"] else "no_observed_items",
        agreement_completed=observed[0].get("is_agreement_completed") if len(observed) == 1 else None,
    )
    if len(observed) != 1 or set(states) != seen or any(r["act_state"] is None for r in output["acts"]):
        output.update(complete=False, acceptance_status="act_status_incomplete")
        output["issues"].append("unmatched_act_summary")
    elif (
        output["agreement_completed"] is not True
        or not states
        or any(state != "ACCEPTED" for state, _ in states.values())
    ):
        output.update(complete=False, acceptance_status="agreement_pending")
        output["issues"].append("receipt_agreement_unconfirmed")
    if output["issues"]:
        output["complete"] = False
        output["issues"] = sorted(set(output["issues"]))
    return output


def _stocks(name, args, request):
    if name == "ozon_stocks":
        offers = args.get("offer_ids", [args["article"]] if "article" in args else [])
        if "sku" in args and not offers:
            info = request(
                "POST", BASE + "/v3/product/info/list", body={"sku": [str(args["sku"])]}, ttl=1800
            )
            offers = [r["offer_id"] for r in rows(info.get("items")) if r.get("offer_id")]
            if not offers:
                return result([], complete=False, issues=["sku_identity_unavailable"])
        raw = request(
            "POST",
            BASE + "/v4/product/info/stocks",
            body={
                "filter": {"visibility": "ALL", "offer_id": offers},
                "cursor": args.get("cursor", ""),
                "limit": args.get("limit", 50),
            },
        )
        data = []
        for card in rows(raw.get("items")):
            for stock in rows(card.get("stocks")):
                row = dict(
                    _identity(card),
                    sku=stock.get("sku"),
                    product_id=card.get("product_id"),
                    scheme=stock.get("type"),
                    **select(stock, "present reserved warehouse_ids shipment_type"),
                )
                if matches(row, args):
                    data.append(row)
        cursor, done = _page(raw, args.get("cursor"))
        return result(data, cursor, complete=done, quantity_unit="pcs")
    if "date_from" in args:
        return request.stock_history(args)
    skus = args.get("skus", [args["sku"]] if "sku" in args else [])
    fbs_only = set()
    if not skus and "article" in args:
        card = _article_card(args["article"], request)
        if card:
            skus = card["sku_candidates"]
            schemes = defaultdict(set)
            for source in card["sources"] + card["stock_sources"]:
                schemes[str(source["sku"])].add(source["source"].upper())
            fbs_only = {sku for sku, sources in schemes.items() if sources == {"FBS"}}
    if not skus:
        return result([], complete=False, issues=["sku_identity_required"])
    params = {"skus": [int(s) for s in skus]}
    for key in ("warehouse_id", "cluster_id"):
        if key in args:
            params[key + "s"] = [int(args[key])]
    observed = datetime.now(timezone.utc).isoformat()
    data = request("POST", BASE + "/v1/analytics/stocks", body=params)
    fields = "warehouse_id warehouse_name cluster_id cluster_name macrolocal_cluster_id available_stock_count valid_stock_count transit_stock_count inbound_replenishment return_from_customer_stock_count ads ads_cluster idc idc_cluster"
    items = []
    expected = set(map(str, skus))
    seen_skus, invalid_skus, positions = set(), set(), set()
    for stock in rows(data.get("items")):
        row = dict(_identity(stock), **select(stock, fields))
        sku = str(row["sku"])
        if sku not in expected:
            raise ValueError("Unexpected SKU in stock report")
        if any(str(row[k]) != str(args[k]) for k in ("warehouse_id", "cluster_id") if k in args):
            raise ValueError("Unexpected stock location")
        position = (sku, str(row["warehouse_id"]))
        quantity = row["available_stock_count"]
        seen_skus.add(sku)
        if (
            row["warehouse_id"] is None
            or position in positions
            or type(quantity) is not int
            or quantity < 0
            or ("article" in args and row["article"] != args["article"])
        ):
            invalid_skus.add(sku)
        positions.add(position)
        items.append(row)
    missing_skus = sorted(expected - seen_skus - fbs_only, key=int)
    complete = bool(seen_skus) and not missing_skus and not invalid_skus
    if not ("cluster_id" in args or "warehouse_id" in args) and hasattr(request, "record_stocks"):
        verified = seen_skus - invalid_skus
        article = args.get("article") if complete and not ("sku" in args or "skus" in args) else None
        if verified:
            request.record_stocks(
                verified, article, [r for r in items if str(r["sku"]) in verified], observed
            )
    items = [r for r in items if matches(r, args)]
    return result(
        items,
        complete=complete,
        fulfillment_scope="FBO",
        excluded_fbs_skus=sorted(fbs_only - seen_skus, key=int),
        missing_skus=missing_skus,
        invalid_stock_skus=sorted(invalid_skus, key=int),
        issues=(["stock_skus_not_observed"] if missing_skus else [])
        + (["invalid_stock_positions"] if invalid_skus else [])
        + (["article_only_has_fbs_skus"] if not seen_skus and expected == fbs_only else []),
        sales_window_days=28,
        quantity_unit="pcs",
        stock_observed_at=observed,
        source="ozon_current_stock",
    )


def _daily(args, request):
    metrics = ["ordered_units", "revenue"]
    if args.get("include_funnel"):
        metrics += ["hits_view_pdp", "hits_tocart_pdp", "conv_tocart_pdp"]
    skus = _report_skus(args, request)
    if "article" in args and not skus:
        return result([], complete=False, issues=["article_identity_unavailable"])
    filters = [{"key": "sku", "op": "EQ", "value": str(skus[0])}] if len(skus) == 1 else []
    limit, offset = args.get("limit", 50), args.get("offset", 0)
    raw = request(
        "POST",
        BASE + "/v1/analytics/data",
        body={
            "date_from": args["date_from"],
            "date_to": args["date_to"],
            "dimension": ["sku", "day"],
            "metrics": metrics,
            "filters": filters,
            "sort": [],
            "limit": limit,
            "offset": offset,
        },
        interval=60,
        ttl=1800,
    )
    values = rows(raw["result"].get("data"))
    items = []
    for r in values:
        dims = rows(r.get("dimensions"))
        if len(dims) != 2 or len(r["metrics"]) != len(metrics):
            raise ValueError("Analytics dimension mismatch")
        sku = dims[0]["id"]
        if skus and str(sku) not in set(map(str, skus)):
            continue
        items.append(
            dict(
                zip(metrics, r["metrics"]),
                sku=sku,
                date=dims[1]["id"],
                article=args.get("article"),
                offer_id=args.get("article"),
                nm_id=None,
                currency=None,
            )
        )
    return result(
        items,
        offset + limit if len(values) == limit else None,
        report_timestamp=raw.get("timestamp"),
        currency_basis="not_in_analytics_response",
        period_basis="seller_api_reporting_day",
    )


def _geo(args, request):
    start, end = _range(args)
    raw = request(
        "POST",
        BASE + "/v3/posting/fbo/list",
        body={
            "filter": {"since": start, "to": end},
            "cursor": args.get("cursor", ""),
            "limit": args.get("limit", 100),
            "sort_dir": "ASC",
            "with": {"analytics_data": True, "financial_data": True, "legal_info": False},
        },
    )
    items, issues = [], set()
    article_skus = None
    for p in rows(raw.get("postings")):
        a, f = p.get("analytics_data") or {}, p.get("financial_data") or {}
        posting_skus = set()
        for product in rows(p.get("products")):
            row = dict(
                _identity(product),
                **select(product, "quantity"),
                **select(a, "warehouse_id warehouse_name city"),
                **select(f, "cluster_from cluster_to"),
                status=p.get("status"),
                posting_number=p.get("posting_number"),
                created_at=iso(p.get("created_at")),
                in_process_at=iso(p.get("in_process_at")),
            )
            if "article" in args and not row["article"]:
                if row["sku"] is None:
                    issues.add("posting_product_identity_unknown")
                    continue
                if article_skus is None:
                    article_skus = set(map(str, lookup(args["article"], request)))
                if not article_skus:
                    issues.add("posting_product_identity_unknown")
                    continue
                if str(row["sku"]) in article_skus:
                    row.update(
                        article=args["article"], offer_id=args["article"], article_basis="catalog_sku"
                    )
            if row["sku"] is None and ("article" not in args or row["article"] == args["article"]):
                issues.add("posting_product_identity_unknown")
            if matches(row, args):
                sku = str(row["sku"])
                if sku in posting_skus:
                    issues.add("duplicate_posting_sku")
                posting_skus.add(sku)
                items.append(row)
    cursor, done = _page(raw, args.get("cursor"))
    return result(
        items,
        cursor,
        complete=done and not issues,
        issues=sorted(issues),
        scope="FBO",
        period_basis="posting_creation_utc",
    )


def _cash_detail(value, shape):
    """Project only the documented breakdown, including the provider's one-or-many item shape."""
    if value is None:
        return None
    if shape == "number":
        money(value)
        return str(value) if isinstance(value, (Decimal, float)) else value
    if shape == "text":
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError("Invalid statement label")
        return value
    if shape == "date":
        return iso(value)
    if isinstance(shape, list):
        if isinstance(value, dict):
            return _cash_detail(value, shape[0])
        return [_cash_detail(item, shape[0]) for item in rows(value)]
    if not isinstance(value, dict):
        raise ValueError("Invalid cash-flow object")
    return {key: _cash_detail(value[key], child) for key, child in shape.items() if key in value}


STATEMENT_PERIOD = {"begin": "date", "end": "date", "id": "number"}
STATEMENT_SERVICES = {"total": "number", "items": [{"name": "text", "price": "number"}]}
STATEMENT_DETAIL = {
    **dict.fromkeys(("begin_balance_amount", "end_balance_amount", "loan", "invoice_transfer"), "number"),
    "period": STATEMENT_PERIOD,
    "services": STATEMENT_SERVICES,
    "others": STATEMENT_SERVICES,
    "delivery": {"total": "number", "amount": "number", "delivery_services": STATEMENT_SERVICES},
    "return": {"total": "number", "amount": "number", "return_services": STATEMENT_SERVICES},
    "rfbs": dict.fromkeys(
        (
            "total",
            "transfer_delivery",
            "transfer_delivery_return",
            "compensation_delivery_return",
            "partial_compensation",
            "partial_compensation_return",
        ),
        "number",
    ),
}


def _cash(args, request):
    start, end = _range(args)
    page = args.get("page", 1)
    raw = request(
        "POST",
        BASE + "/v1/finance/cash-flow-statement/list",
        body={
            "date": {"from": start, "to": end},
            "page": page,
            "page_size": args.get("limit", 50),
            "with_details": True,
        },
    )
    data = raw["result"]
    fields = {
        "period": STATEMENT_PERIOD,
        "currency_code": "text",
        **dict.fromkeys(
            (
                "orders_amount",
                "returns_amount",
                "commission_amount",
                "services_amount",
                "item_delivery_and_return_amount",
            ),
            "number",
        ),
    }
    items = [_cash_detail(r, fields) for r in rows(data.get("cash_flows"))]
    detail = data.get("details") or {}
    payments = detail.get("payments")
    if isinstance(payments, dict):
        payments = [payments]
    if payments is not None:
        payments = [_cash_detail(r, {"currency_code": "text", "payment": "number"}) for r in rows(payments)]
    totals = defaultdict(Decimal)
    incomplete = (
        payments is None
        or not isinstance(detail.get("period"), dict)
        or not all(detail["period"].get(k) for k in ("begin", "end"))
    )
    seen = set()
    issues = []
    with localcontext() as ctx:
        ctx.prec = 128
        for payment in rows(payments or []):
            amount, code = money(payment.get("payment")), payment.get("currency_code")
            if amount is None or not code:
                incomplete = True
            else:
                if (code, amount) in seen:
                    issues.append("payment_rows_without_unique_ids")
                seen.add((code, amount))
                totals[code] += amount
    count = data.get("page_count", raw.get("page_count"))
    if count is not None and type(count) is not int:
        raise ValueError("Invalid page count")
    return result(
        items,
        page + 1 if count is not None and page < count else None,
        complete=count is not None and page >= count and not incomplete and not issues,
        payments=payments,
        page_count=count,
        issues=sorted(
            set(issues + (["incomplete_payment_amount_currency_or_period"] if incomplete else []))
        ),
        payout_by_currency={} if incomplete or issues else {k: str(v) for k, v in totals.items()},
        details=_cash_detail(detail, STATEMENT_DETAIL),
        payout_basis="payments_in_returned_reporting_period",
    )


def _cash_summary(args, request):
    query = {k: v for k, v in args.items() if k != "mode"}
    scan = (
        request.progress("ozon-cash-period-v1", query)
        if hasattr(request, "progress")
        else nullcontext(({}, lambda *args: None))
    )
    with scan as (saved, save):
        state = saved or {
            "page": 1,
            "finished": False,
            "periods": {},
            "flows": {},
            "issues": [],
            "page_count": None,
        }
        wait = None
        rate_limited = False
        for _ in range(8):
            if state["finished"]:
                break
            try:
                page = _cash(dict(query, page=state["page"]), request)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                wait = exc.headers.get("Retry-After")
                rate_limited = True
                exc.close()
                break
            for row in page["items"]:
                period = row.get("period") or {}
                key = str((period.get("begin"), period.get("end"), row.get("currency_code")))
                if key in state["flows"] and state["flows"][key] != row:
                    state["issues"].append("conflicting_cash_flow_rows")
                else:
                    state["flows"][key] = row
            period = page["details"].get("period") or {}
            if period.get("begin") and period.get("end"):
                start, end = (
                    datetime.fromisoformat(period[k]).astimezone(timezone.utc).date().isoformat()
                    for k in ("begin", "end")
                )
                key = start + "/" + end
                statement = {
                    "period_from": start,
                    "period_to": end,
                    "payments": page["payments"],
                    "payout_by_currency": page["payout_by_currency"],
                    "details": page["details"],
                }
                if key in state["periods"] and state["periods"][key] != statement:
                    state["issues"].append("conflicting_statement_period")
                else:
                    state["periods"][key] = statement
            state["issues"].extend(page["issues"])
            if page["page_count"] is None:
                state["issues"].append("unknown_page_count")
            if state.get("page_count") is not None and state["page_count"] != page["page_count"]:
                state["issues"].append("page_count_changed_during_scan")
            state["page_count"] = page["page_count"]
            state["issues"] = sorted(set(state["issues"]))
            state["page"] = page["next_cursor"]
            state["finished"] = state["page"] is None
            save(state, 1800 if state["finished"] else 86400)
        periods = sorted(state["periods"].values(), key=lambda p: p["period_from"])
        issues = list(state["issues"])
        expected = date.fromisoformat(args["date_from"])
        totals = defaultdict(Decimal)
        for block in periods:
            first, last = (date.fromisoformat(block[k]) for k in ("period_from", "period_to"))
            if first != expected or last < first:
                issues.append("overlapping_or_missing_reporting_days")
            expected = last + timedelta(days=1)
            with localcontext() as ctx:
                ctx.prec = 128
                for code, value in block["payout_by_currency"].items():
                    totals[code] += money(value)
        if expected != date.fromisoformat(args["date_to"]) + timedelta(days=1):
            issues.append("incomplete_period_coverage")
        complete = state["finished"] and not issues
        return result(
            list(state["flows"].values()),
            complete=complete,
            status="ready" if complete else "incomplete_statement" if state["finished"] else "pending",
            progress_page=state["page"],
            retry_after_seconds=wait,
            rate_limited=rate_limited,
            summary_scope="requested_period",
            statements=periods,
            issues=sorted(set(issues)),
            payout_by_currency={k: str(v) for k, v in totals.items()} if complete else {},
            payout_basis="explicit_payments_in_nonoverlapping_reporting_periods",
            period_from=args["date_from"],
            period_to=args["date_to"],
        )


def read(name, args, request):
    validate(name, args)
    if name == "ozon_supply_orders":
        return _orders(args, request)
    if name == "ozon_supply_items":
        return _items(args, request)
    if name in ("ozon_stocks", "ozon_stocks_by_cluster", "ozon_stocks_analytics"):
        return _stocks(name, args, request)
    if name == "ozon_turnover":
        skus = args.get("skus") or _report_skus(args, request)
        if "article" in args and "skus" in args and not set(map(str,skus))<=set(map(str,lookup(args["article"],request))):
            return result([],complete=False,issues=["article_sku_conflict"])
        if "article" in args and not skus:
            return result([], complete=False, issues=["article_identity_unavailable"])
        limit, offset = args.get("limit", 100), args.get("offset", 0)
        path = "/v1/analytics/turnover/stocks"
        raw = request("POST", BASE + path, body={"sku":list(map(str,skus)), "limit":limit, "offset":offset}, ttl=1800)
        page = rows(raw.get("items"))
        items = [dict(_identity(row), **select(row,"name ads current_stock idc idc_grade turnover turnover_grade"))
                 for row in page if ("article" not in args or row.get("offer_id")==args["article"])
                 and (not skus or str(row.get("sku")) in set(map(str,skus)))]
        return result(items, offset+limit if len(page)==limit else None, source=path)
    if name == "ozon_delivery_time":
        skus = _report_skus(args, request)
        if "article" in args and not skus:
            return result([], complete=False, issues=["article_identity_unavailable"])
        filters = {"period":{"from":args["date_from"],"to":args["date_to"]}, "delivery_schema":"ALL"}
        if skus:
            filters["skus"] = list(map(str,skus))
        if "cluster_id" in args:
            filters["macrolocal_cluster_to_ids"] = [str(args["cluster_id"])]
        limit, offset = args.get("limit",100), args.get("offset",0)
        path = "/v1/analytics/local-sale/clusters-items/info"
        raw = request("POST", BASE+path,body={"filter":filters,"limit":limit,"offset":offset,
                      "sort_by":"LOCALITY","sort_dir":"DESC"},ttl=1800)
        page = rows(raw.get("items"))
        items = []
        for row in page:
            product, metrics = row["item"], row["metrics"]["local_data"]
            items.append(dict(_identity(product), cluster_id=row.get("cluster_to_id"),
                              local_units=metrics.get("local_quantity"), total_units=metrics.get("total_quantity"),
                              locality_index=metrics.get("index"), average_delivery_hours=None))
        total = raw.get("total")
        if type(total) is not int or total < 0:
            raise ValueError("Invalid Ozon locality total")
        return result(items,offset+len(page) if page and offset+len(page)<total else None,
                      complete=offset+len(page)>=total,source=path, time_coverage="actual_delivery_duration_unavailable_in_current_seller_schema")
    if name == "ozon_sku_daily":
        return _daily(args, request)
    if name == "ozon_fbo_postings_geo":
        return _geo(args, request)
    if name == "ozon_cash_flow":
        if args.get("mode") == "summary" and "page" not in args:
            return _cash_summary(args, request)
        return _cash(args, request)
    if name == "ozon_clusters":
        body = {"cluster_type": args.get("cluster_type", "CLUSTER_TYPE_OZON")}
        if "cluster_id" in args:
            body["cluster_ids"] = [int(args["cluster_id"])]
        data = request("POST", BASE + "/v1/cluster/list", body=body, ttl=1800)
        items = []
        for c in rows(data.get("clusters")):
            warehouses = [
                select(w, "warehouse_id name type")
                for group in rows(c.get("logistic_clusters", []))
                for w in rows(group.get("warehouses", []))
            ]
            items.append(dict(select(c, "id name type macrolocal_cluster_id"), warehouses=warehouses))
        return result(items)
    if name == "ozon_reviews":
        start = (
            date.fromisoformat(args["date_from"]) - timedelta(days=1)
        ).isoformat() + "T23:59:59.999999Z"
        end = (date.fromisoformat(args["date_to"]) + timedelta(days=1)).isoformat() + "T00:00:00Z"
        filters = {"published_from": start, "published_to": end}
        skus = _report_skus(args, request)
        if "article" in args and not skus:
            return result([], complete=False, issues=["article_identity_unavailable"])
        if skus:
            filters["skus"] = skus
        raw = request(
            "POST",
            BASE + "/v2/review/list",
            body={
                "filters": filters,
                "last_id": args.get("cursor", ""),
                "limit": args.get("limit", 50),
                "sort_dir": "DESC",
            },
        )
        data = []
        for r in rows(raw.get("reviews")):
            if skus and str(r.get("sku")) not in set(map(str, skus)):
                continue
            stamp = iso(r.get("published_at"))
            if not stamp:
                raise ValueError("Missing review date")
            day = datetime.fromisoformat(stamp).astimezone(timezone.utc).date().isoformat()
            if args["date_from"] <= day <= args["date_to"]:
                data.append(
                    dict(
                        select(r, "id sku rating text status published_at"),
                        article=args.get("article"),
                        nm_id=None,
                    )
                )
        rated = [r["rating"] for r in data if type(r["rating"]) is int and 1 <= r["rating"] <= 5]
        cursor, done = _page(raw, args.get("cursor"))
        return result(
            data,
            cursor,
            complete=done,
            mean_rating=sum(rated) / len(rated) if rated else None,
            rating_scope="returned_page",
        )
    if name == "ozon_rating":
        raw = request("POST", BASE + "/v1/rating/summary", body={})
        data = [
            {
                "group_name": g.get("group_name"),
                "items": [
                    select(r, "name rating current_value past_value status value_type rating_direction")
                    for r in rows(g.get("items", []))
                ],
            }
            for g in rows(raw.get("groups"))
        ]
        return result(
            data,
            localization_index=[
                select(r, "calculation_date localization_percentage")
                for r in rows(raw.get("localization_index", []))
            ],
        )
    raise ValueError("Unknown Ozon report")
