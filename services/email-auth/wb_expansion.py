"""Bounded reads of Wildberries supplies, statistics, reports and promotion."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
import urllib.error

import finance_gate
from market_history import order_event
from market_specs import MOSCOW, direct_attribution, iso, matches, money, result, rows, select, validate

SUPPLIES = "https://supplies-api.wildberries.ru/api/v1/supplies"
ANALYTICS = "https://seller-analytics-api.wildberries.ru"
STAT = "https://statistics-api.wildberries.ru/api/v1/supplier/"
FINANCE = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
FEEDBACK = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
ADVERT = "https://advert-api.wildberries.ru"
COEFFICIENTS = "https://common-api.wildberries.ru/api/tariffs/v1/acceptance/coefficients"
REPORTS = {
    "wb_stocks": "warehouse_remains",
    "wb_paid_storage": "paid_storage",
    "wb_acceptance_report": "acceptance_report",
}
CATEGORIES = {
    "wb_supplies": "Поставки",
    "wb_supply_goods": "Поставки",
    "wb_stocks": "Аналитика",
    "wb_orders": "Статистика",
    "wb_sales": "Статистика",
    "wb_finance_report": "Финансы",
    "wb_paid_storage": "Аналитика",
    "wb_acceptance_report": "Аналитика",
    "wb_funnel": "Аналитика",
    "wb_ads": "Продвижение",
    "wb_feedbacks": "Вопросы и отзывы",
    "wb_acceptance_slots": "Любая категория WB",
    "wb_retentions": "Финансы",
    "wb_returns_report": "Аналитика",
    "wb_region_sales": "Аналитика",
    "wb_ads_finance": "Продвижение",
    "wb_search_queries": "Аналитика и подписка Джем",
}


def _item(row, article="vendorCode", nm="nmId"):
    return {
        "article": row.get(article),
        "vendor_code": row.get(article),
        "nm_id": row.get(nm),
        "barcode": row.get("barcode"),
        "sku": row.get("barcode"),
        "sku_basis": "wb_barcode",
    }


def _slice(items, args, **metadata):
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    return result(
        items[offset : offset + limit], offset + limit if len(items) > offset + limit else None, **metadata
    )


def supply_headers(args, request):
    limit, offset = args.get("limit", 10), args.get("offset", 0)
    params = {"dates": [{"from": args["date_from"], "till": args["date_to"], "type": "createDate"}]}
    if "status_ids" in args:
        params["statusIDs"] = args["status_ids"]
    page = rows(request("POST", SUPPLIES, query={"limit": limit, "offset": offset}, body=params,
                        interval=2, basic_interval=1800, ttl=1800))
    items = []
    for row in page:
        sid, pre = row.get("supplyID"), row.get("preorderID")
        if type(sid or pre) is not int or (sid or pre) <= 0:
            raise ValueError("Missing supply identity")
        items.append(dict(supply_id=sid, preorder_id=pre, status_id=row.get("statusID"),
                          created_at=iso(row.get("createDate"), MOSCOW), supply_date=iso(row.get("supplyDate"), MOSCOW),
                          actual_shipment_at=iso(row.get("factDate"), MOSCOW)))
    return result(items, offset + limit if len(page) == limit else None, period_basis="supply_creation_moscow")


def supply_detail(parent, request):
    sid = parent["supply_id"]
    ident = sid or parent["preorder_id"]
    data = request("GET", SUPPLIES + "/" + str(ident), query={"isPreorderID": "false" if sid else "true"},
                   group="fbw-detail", interval=2, basic_interval=1800, ttl=1800)
    planned, actual = data.get("warehouseName"), data.get("actualWarehouseName")
    # The receiving warehouse may differ from the planned destination.
    return dict(parent, status_id=data.get("statusID", parent.get("status_id")),
                warehouse_id=data.get("warehouseID"), warehouse=planned or actual,
                warehouse_basis="planned" if planned else "actual" if actual else "unreported",
                actual_warehouse_id=data.get("actualWarehouseID"), actual_warehouse=actual,
                transit_warehouse_id=data.get("transitWarehouseID"),
                paid_acceptance_coefficient=data.get("paidAcceptanceCoefficient"), accepted_at=None,
                quantity=data.get("quantity"), accepted_quantity=data.get("acceptedQuantity"),
                ready_for_sale_quantity=data.get("readyForSaleQuantity"))


def _supplies(args, request):
    page = supply_headers(args, request)
    items = []
    for index, parent in enumerate(page["items"]):
        try:
            row = supply_detail(parent, request)
        except (urllib.error.HTTPError, finance_gate.GateError) as exc:
            if not items or (isinstance(exc, urllib.error.HTTPError) and exc.code != 429):
                raise
            delay = exc.headers.get("Retry-After") if isinstance(exc, urllib.error.HTTPError) else exc.retry_after_seconds
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            return result(items, args.get("offset", 0) + index, complete=False,
                          issues=["more_supply_details_pending"], retry_after_seconds=delay)
        if "warehouse_id" not in args or str(args["warehouse_id"]) in (
                str(row["warehouse_id"]), str(row["actual_warehouse_id"])):
            items.append(row)
    return result(items, page["next_cursor"], period_basis="supply_creation_moscow")


def _goods(args, request, *, include_packages=True):
    limit, offset = args.get("limit", 100), args.get("offset", 0)
    base = SUPPLIES + "/" + str(args["supply_id"])
    page = rows(
        request(
            "GET",
            base + "/goods",
            query={
                "limit": limit,
                "offset": offset,
                "isPreorderID": str(args.get("is_preorder", False)).lower(),
            },
            group="fbw-goods",
            interval=2,
            basic_interval=1800,
            ttl=1800,
        )
    )
    items = [
        dict(
            _item(r, nm="nmID"),
            quantity=r.get("quantity"),
            accepted_quantity=r.get("acceptedQuantity"),
            ready_for_sale_quantity=r.get("readyForSaleQuantity"),
            unloading_quantity=r.get("unloadingQuantity"),
        )
        for r in page
    ]
    packs = []
    if include_packages and not args.get("is_preorder", False):
        for p in rows(
            request(
                "GET", base + "/package", group="fbw-package", interval=2, basic_interval=1800, ttl=1800
            )
        ):
            packs.append(
                {
                    "package_id": p.get("packageId"),
                    "package_code": p.get("packageCode"),
                    "quantity": p.get("quantity"),
                    "barcodes": [select(x, "barcode quantity") for x in rows(p.get("barcodes", []))],
                }
            )
    selected = [r for r in items if matches(r, args)]
    known = all(
        type(r[k]) is int and r[k] >= 0 for r in selected for k in ("quantity", "accepted_quantity")
    )
    cursor = offset + limit if len(page) == limit else None
    return result(
        selected,
        cursor,
        complete=cursor is None and known,
        issues=[] if known else ["unreported_receipt_quantity"],
        packages=packs,
        package_scope="whole_supply" if not args.get("is_preorder") else "unavailable_for_preorder",
    )


def feed_orders(args, request):
    from wb_reports import region
    today = datetime.now(MOSCOW).date()
    start = max(date.fromisoformat(args["date_from"]), today - timedelta(days=30))
    if start.isoformat() > args["date_to"]:
        return result([], complete=False, status="outside_provider_retention", retention_days=31)
    query = {k: v for k, v in args.items() if k in ("article", "nm_id", "limit")}
    if "article" in query and "nm_id" not in query:
        if hasattr(request, "gate_dir"):
            from expansion_gateway import _mapping
            mapping = _mapping(args, request.gate_dir)
            if mapping and mapping["wb_vendor_code"] == query["article"]:
                query["nm_id"] = mapping.get("wb_nm_id")
        if not query.get("nm_id"):
            return result([], complete=False, status="exact_nm_id_required")
    if "sku" in args:
        return result([], complete=False, status="barcode_unavailable_in_order_feed")
    # WB filters on the latest status change. Include all later changes for the creation cohort.
    query.update(date_from=start.isoformat(), date_to=today.isoformat(), mode="details")
    query["limit"] = min(query.get("limit", 100), 1000)
    if args.get("cursor"):
        tag, anchor, stamp, offset = args["cursor"].split("|")
        if tag != "feed" or not offset.isascii() or not offset.isdecimal() or len(offset)>8:
            raise ValueError("Invalid order feed cursor")
        if anchor != today.isoformat():
            return result([], complete=False, status="snapshot_period_expired")
        query.update(snapshot_time=stamp, offset=int(offset))
    page = region(query, request)
    out = []
    for r in page["items"]:
        when = datetime.fromisoformat(r["created_at"]).astimezone(MOSCOW)
        if not start.isoformat() <= when.date().isoformat() <= args["date_to"]:
            continue
        out.append(dict(r, date=when.isoformat(), quantity=r["units"], cancelled=r["status"]=="cancel",
                        last_change=r["updated_at"], price_with_discount=r["amount"]))
    next_page = page["next_cursor"]
    if next_page is not None:
        next_page = f"feed|{today.isoformat()}|{page['snapshot_time']}|{next_page}"
    full_period = start.isoformat() == args["date_from"] and args["date_to"] < today.isoformat()
    return result(out, next_page, complete=page["complete"] and full_period,
                  status=page.get("status", "ready" if full_period else "partial_retention"),
                  source="/api/analytics/v1/order-feed", coverage_from=start.isoformat(), coverage_to=args["date_to"],
                  source_exhausted=page["complete"], date_basis="order_created_at_moscow")


def _stats(name, args, request, *, exact_day=False, feed_start=None):
    sale = name == "wb_sales"
    start = date.fromisoformat(args["date_from"])
    if start < datetime.now(MOSCOW).date() - timedelta(days=90):
        return result([], complete=False, status="outside_provider_retention", retention_days=90)
    cursor = args.get("cursor", args["date_from"])
    if not isinstance(cursor, str):
        raise ValueError("Invalid statistics cursor")
    feed_args = dict(args, date_from=feed_start) if feed_start else args
    if not sale and cursor.startswith("feed|"):
        return feed_orders(feed_args, request)
    stamp, _, offset = cursor.partition("|")
    if not re.fullmatch(r"[0-9T:+.Z-]{10,40}", stamp):
        raise ValueError("Invalid statistics cursor")
    iso(stamp, MOSCOW)
    index = int(offset or 0)
    if index < 0:
        raise ValueError("Invalid row offset")
    try:
        data = request(
            "GET",
            STAT + ("sales" if sale else "orders"),
            query={"dateFrom": stamp, "flag": int(exact_day)},
            interval=60,
            basic_interval=7200 if sale else 10800,
            ttl=1800,
        )
    except finance_gate.GateError as exc:
        if sale or exc.reason != "retry_time_unknown" or "cursor" in args:
            raise
        fallback = feed_orders(feed_args, request)
        if fallback.get("status") in ("exact_nm_id_required", "barcode_unavailable_in_order_feed"):
            fallback.update(fallback_status=fallback["status"], status=exc.reason)
        return fallback
    page = rows(data)
    limit = args.get("limit", 100)
    items = []
    for r in page[index : index + limit]:
        sales_id = r.get("saleID")
        row = dict(
            _item(r, "supplierArticle"),
            date=iso(r.get("date"), MOSCOW),
            warehouse=r.get("warehouseName"),
            warehouse_type=r.get("warehouseType"),
            region=r.get("regionName"),
            srid=r.get("srid"),
            quantity=1,
            currency=None,
            currency_basis="seller_currency_not_in_response",
            price_with_discount=r.get("priceWithDisc"),
            last_change=iso(r.get("lastChangeDate"), MOSCOW),
        )
        if sale:
            row.update(
                sale_id=sales_id,
                event_type={"S": "sale", "R": "return"}.get(str(sales_id)[:1], "unknown"),
                for_pay=r.get("forPay"),
            )
        else:
            cancelled = r.get('isCancel')
            row.update(
                cancelled=cancelled if type(cancelled) is bool else None,
                cancel_date=iso(r["cancelDate"], MOSCOW)
                if cancelled is True and r.get("cancelDate")
                else None,
            )
        if row["date"] is None:
            raise ValueError("Missing operation date")
        row["date"] = datetime.fromisoformat(row["date"]).astimezone(MOSCOW).isoformat()
        if args["date_from"] <= row["date"][:10] <= args["date_to"] and matches(row, args):
            items.append(row)
    next_cursor = None
    if index + limit < len(page):
        next_cursor = stamp + "|" + str(index + limit)
    elif page and not exact_day:
        next_cursor = page[-1].get("lastChangeDate")
        if not next_cursor or next_cursor == stamp:
            return result(items, complete=False, issues=["nonadvancing_change_cursor"])
    groups = defaultdict(int)
    for r in items:
        groups[
            (
                r["date"][:10],
                r["warehouse"],
                r.get("event_type", order_event(r.get("cancelled"))),
            )
        ] += 1
    return result(
        items,
        next_cursor,
        summary=[{"date": d, "warehouse": w, "event": e, "units": n} for (d, w, e), n in groups.items()],
        summary_scope="returned_page",
        date_basis="event_date_moscow",
        statistics_status="preliminary",
    )


def _report(name, args, request):
    report = REPORTS[name]
    base = ANALYTICS + "/api/v1/" + report
    query = (
        {"locale": "ru", "groupBySa": "true", "groupByNm": "true", "groupByBarcode": "true"}
        if name == "wb_stocks"
        else {"dateFrom": args["date_from"], "dateTo": args["date_to"]}
    )
    if name == "wb_acceptance_report" and "article" in args and "nm_id" not in args:
        return result([], complete=False, status="exact_nm_id_required")
    slow = {
        "wb_stocks": (900, 900),
        "wb_paid_storage": (3600, 1800),
        "wb_acceptance_report": (10800, 1800),
    }[name]
    task = args.get("task_id")
    if task is None:
        created = request("GET", base, query=query, interval=60, basic_interval=slow[0], ttl=1800, job=True)
        task = created["data"]["taskId"]
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", task):
        raise ValueError("Invalid report task ID")
    if hasattr(request, "bind_job"):
        request.bind_job(base, query, task, existing="task_id" in args)
    path = base + "/tasks/" + task
    cached = request.cached_download(path + "/download") if hasattr(request, "cached_download") else None
    for poll in range(2 if cached is None else 0):
        try:
            state = request(
                "GET", path + "/status", group=report + "-status", interval=5, basic_interval=slow[1], ttl=0
            )["data"]
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            delay = exc.headers.get("Retry-After")
            exc.close()
            return result(
                [],
                complete=False,
                status="pending",
                task_id=task,
                retry_after_seconds=int(delay) if delay and delay.isdigit() else None,
            )
        if str(state.get("id")) != task:
            raise ValueError("Report identity mismatch")
        status = state.get("status")
        if status == "done":
            break
        if status not in ("new", "processing"):
            if status in ("purged", "canceled"):
                if hasattr(request, "finish_job"):
                    request.finish_job(base, query, task, failed=True)
                elif hasattr(request, "forget"):
                    request.forget("GET", base, query=query)
            return result(
                [], complete=False, status="report_unavailable", provider_status=status, task_id=task
            )
        if poll or not hasattr(request, "wait") or not request.wait(5):
            return result([], complete=False, status="pending", task_id=task, retry_after_seconds=5)
    try:
        downloaded = (
            cached
            if cached is not None
            else request(
                "GET",
                path + "/download",
                group=report + "-download",
                interval=60,
                basic_interval=slow[1],
                ttl=1800,
            )
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        delay = exc.headers.get("Retry-After")
        exc.close()
        return result(
            [],
            complete=False,
            status="pending",
            phase="download",
            task_id=task,
            retry_after_seconds=int(delay) if delay and delay.isdigit() else None,
        )
    items = []
    issues = set()
    for r in rows([] if downloaded is None else downloaded):
        if name == "wb_stocks":
            ident = _item(r)
            if not matches(ident, args):
                if not any(
                    ident[k] is not None and str(ident[k]) != str(args[k])
                    for k in ("nm_id", "article")
                    if k in args
                ):
                    issues.add("stock_identity_unconfirmed")
                continue
            kinds = {
                "Всего находится на складах": "provider_total",
                "В пути до получателей": "in_transit_to_customer",
                "В пути возвраты на склад WB": "returning_from_customer",
            }
            for w in rows(r.get("warehouses")):
                warehouse = w.get("warehouseName")
                if not warehouse:
                    raise ValueError("Missing warehouse")
                if type(w.get("quantity")) is not int or w["quantity"] < 0:
                    issues.add("unreported_stock_quantity")
                items.append(
                    dict(
                        ident,
                        warehouse=warehouse,
                        quantity=w.get("quantity"),
                        kind=kinds.get(warehouse, "warehouse"),
                    )
                )
        elif name == "wb_paid_storage":
            item = dict(
                _item(r),
                date=r.get("date"),
                warehouse=r.get("warehouse"),
                warehouse_id=r.get("officeId"),
                amount=r.get("warehousePrice"),
                warehouse_price=r.get("warehousePrice"),
                original_date=r.get("originalDate"),
                currency=r.get("currency") or "RUB",
                currency_basis="provider_row" if r.get("currency") else "documented_storage_report_rubles",
                quantity=r.get("barcodesCount"),
                delivery_id=r.get("giId"),
            )
            if matches(item, args):
                items.append(item)
        else:
            item = dict(
                _item(r, nm="nmID"),
                supply_id=r.get("incomeId"),
                quantity=r.get("count"),
                amount=r.get("total"),
                total=r.get("total"),
                currency="RUB",
                shk_create_date=r.get("shkCreateDate"),
                gi_create_date=r.get("giCreateDate"),
            )
            if "article" in args and "nm_id" not in args:
                continue
            if "nm_id" not in args or str(item["nm_id"]) == str(args["nm_id"]):
                items.append(item)
    if name == "wb_acceptance_report" and "article" in args and "nm_id" not in args:
        return result([], complete=False, status="exact_nm_id_required", task_id=task)
    if hasattr(request, "finish_job"):
        request.finish_job(base, query, task)
    if name == "wb_paid_storage" and args.get("mode") == "summary":
        totals, counts = {}, {}
        for item in items:
            currency = item["currency"]
            value = money(item["amount"])
            counts[currency] = counts.get(currency, 0) + 1
            if value is None:
                issues.add("missing_storage_amount")
                totals[currency] = None
            elif totals.get(currency, 0) is not None:
                totals[currency] = totals.get(currency, Decimal(0)) + value
        if not items:
            totals["RUB"], counts["RUB"] = Decimal(0), 0
        summary = [{"currency": code, "amount": str(total) if total is not None else None,
                    "source_rows": counts[code]} for code, total in sorted(totals.items())]
        return result(summary, complete=not issues, issues=sorted(issues), status="partial_report" if issues else "ready",
                      task_id=task, summary_scope="report_period", period=query,
                      currency_documentation="https://seller.wildberries.ru/instructions/ru/ru/material/paid-storage-report")
    report = _slice(items, args, status="ready", task_id=task, quantity_unit="pcs")
    if issues:
        report.update(complete=False, status="partial_report", issues=sorted(issues))
    return report


FIN_FIELDS = {
    "report_id": "reportId",
    "rrd_id": "rrdId",
    "quantity": "quantity",
    "document": "docTypeName",
    "operation": "sellerOperName",
    "currency": "currency",
    "commission_ex_vat": "vw",
    "commission_vat": "vwNds",
    "sales_commission": "ppvzSalesCommission",
    "delivery": "deliveryService",
    "rebilled_logistics": "rebillLogisticCost",
    "storage": "paidStorage",
    "acceptance": "paidAcceptance",
    "penalty": "penalty",
    "for_pay": "forPay",
    "deduction": "deduction",
    "reason": "bonusTypeName",
    "additional_payment": "additionalPayment",
    "gross_sales": "retailAmount",
    "date": "rrDate",
}


def finance_summary(items):
    groups = {}
    for row in items:
        key = row["article"], row["nm_id"], row["currency"]
        g = groups.setdefault(key, {"sold_units": 0, "returned_units": 0, "totals": {}, "missing": set()})
        if row["operation"] in ("Продажа", "Возврат") and row["document"] == row["operation"]:
            label = "sold_units" if row["operation"] == "Продажа" else "returned_units"
            if type(row["quantity"]) is not int:
                g["missing"].add(label)
            else:
                g[label] += abs(row["quantity"])
        for field in (
            "commission_ex_vat",
            "commission_vat",
            "delivery",
            "rebilled_logistics",
            "storage",
            "acceptance",
            "penalty",
            "for_pay",
            "deduction", "additional_payment", "gross_sales",
        ):
            n = money(row.get(field))
            if field in ("for_pay", "commission_ex_vat", "commission_vat", "gross_sales") and n:
                if row["document"] == "Возврат":
                    n = -n
                elif row["document"] != "Продажа":
                    n = None
            if n is None:
                g["missing"].add(field)
            else:
                g["totals"][field] = g["totals"].get(field, Decimal(0)) + n
    summary = []
    for (article, nm, currency), g in groups.items():
        amounts = {k: str(v) for k, v in g["totals"].items() if k not in g["missing"]}
        if "commission_ex_vat" in amounts and "commission_vat" in amounts:
            amounts["commission_including_vat"] = str(
                Decimal(amounts["commission_ex_vat"]) + Decimal(amounts["commission_vat"])
            )
        units = g["sold_units"] if "sold_units" not in g["missing"] else None
        summary.append(
            {
                "article": article,
                "nm_id": nm,
                "currency": currency,
                "sold_units": units,
                "returned_units": g["returned_units"] if "returned_units" not in g["missing"] else None,
                "amounts": amounts,
                "payout_basis": "sales_less_return_documents",
                "commission_basis": "vw_plus_vwNds_sales_less_return_documents",
                "unknown_fields": sorted(g["missing"]),
                "per_sold_unit": {
                    k: str(Decimal(v) / units) if units and currency and (article or nm) else None
                    for k, v in amounts.items()
                },
            }
        )
    return summary


def finance_rows(page, args):
    items, unallocated = [], []
    for r in page:
        row = dict(_item(r), **{k: r.get(v) for k, v in FIN_FIELDS.items()})
        row["barcode"] = row["sku"] = r.get("sku")
        if not row["article"] and not row["nm_id"]:
            unallocated.append(row)
        elif matches(row, args):
            items.append(row)
    return items, unallocated


def _finance(args, request):
    cursor = int(args.get("cursor", 0))
    if cursor < 0:
        raise ValueError("Invalid finance cursor")
    body = {
        "dateFrom": args["date_from"],
        "dateTo": args["date_to"],
        "period": "weekly",
        "limit": args.get("limit", 100),
        "rrdId": cursor,
    }
    raw = request("POST", FINANCE, body=body, interval=60, basic_interval=43200, ttl=1800)
    if raw is None:
        return result([], summary=[], status="end_of_report")
    page = rows(raw)
    items, unallocated = finance_rows(page, args)
    ids = [r.get("rrdId") for r in page]
    if (
        not ids
        or any(type(x) is not int or x <= cursor for x in ids)
        or any(a >= b for a, b in zip(ids, ids[1:]))
    ):
        return result(
            items, complete=False, issues=["nonadvancing_finance_cursor"], unallocated=unallocated
        )
    return result(
        items,
        ids[-1],
        complete=False,
        unallocated=unallocated,
        summary=finance_summary(items),
        summary_scope="returned_page",
        unit_basis="sold_units_before_returns",
    )


def _funnel(args, request):
    if "group_by" in args:
        from wb_reports import funnel_history
        return funnel_history(args, request)
    limit, offset = args.get("limit", 50), args.get("offset", 0)
    body = {
        "selectedPeriod": {"start": args["date_from"], "end": args["date_to"]},
        "limit": limit,
        "offset": offset,
    }
    if "nm_id" in args:
        body["nmIds"] = [int(args["nm_id"])]
    elif "nm_ids" in args:
        body["nmIds"] = list(map(int,args["nm_ids"]))
    data = request(
        "POST",
        ANALYTICS + "/api/analytics/v3/sales-funnel/products",
        body=body,
        interval=20,
        basic_interval=1800,
        ttl=1800,
    )["data"]
    page = rows(data.get("products"))
    items = []
    for r in page:
        selected = r["statistic"]["selected"]
        if selected.get("period") != body["selectedPeriod"]:
            return result([], complete=False, issues=["unexpected_funnel_period"])
        row = dict(
            _item(r["product"]),
            open_count=selected.get("openCount"),
            cart_count=selected.get("cartCount"),
            order_count=selected.get("orderCount"),
            order_sum=selected.get("orderSum"),
            buyout_count=selected.get("buyoutCount"), buyout_sum=selected.get("buyoutSum"),
            cancel_count=selected.get("cancelCount"),
            currency=data.get("currency"),
            conversions=selected.get("conversions"),
        )
        if matches(row, args):
            items.append(row)
    return result(items, offset + limit if len(page) == limit else None)


def _reviews(args, request):
    scope = args.get("report_scope", "unanswered")
    limit, offset = args.get("limit", 50), args.get("offset", 0)
    q = {"take": limit, "skip": offset, "order": "dateDesc"}
    endpoint = FEEDBACK
    if scope == "archive":
        endpoint += "/archive"
    else:
        q.update(
            isAnswered=str(scope == "answered").lower(),
            dateFrom=int(
                datetime.combine(date.fromisoformat(args["date_from"]), time.min, MOSCOW).timestamp()
            ),
            dateTo=int(datetime.combine(date.fromisoformat(args["date_to"]), time.max, MOSCOW).timestamp()),
        )
    if "nm_id" in args:
        q["nmId"] = int(args["nm_id"])
    raw = request("GET", endpoint, query=q, interval=1, basic_interval=720, group="feedbacks", ttl=1800)
    if raw.get("error"):
        raise ValueError("Feedback provider error")
    page = rows(raw["data"].get("feedbacks"))
    items = []
    for r in page:
        row = dict(
            _item(r["productDetails"], "supplierArticle"),
            id=r.get("id"),
            rating=r.get("productValuation"),
            date=iso(r.get("createdDate"), MOSCOW),
            text=r.get("text"),
            pros=r.get("pros"),
            cons=r.get("cons"),
        )
        if row["date"]:
            row["date"] = datetime.fromisoformat(row["date"]).astimezone(MOSCOW).isoformat()
        if row["date"] and args["date_from"] <= row["date"][:10] <= args["date_to"] and matches(row, args):
            items.append(row)
    ratings = [r["rating"] for r in items if type(r["rating"]) is int and 1 <= r["rating"] <= 5]
    return result(
        items,
        offset + limit if len(page) == limit else None,
        report_scope=scope,
        mean_rating=sum(ratings) / len(ratings) if ratings else None,
        rating_scope="returned_page",
    )


def _ads(args, request):
    ids = args.get("campaign_ids")
    report = args.get("report", "auto")
    if report == "spend_ledger":
        ledger = rows(
            request(
                "GET",
                ADVERT + "/adv/v1/upd",
                query={"from": args["date_from"], "to": args["date_to"]},
                interval=1,
                basic_interval=3600,
                ttl=1800,
            )
        )
        items = [
            {
                "campaign_id": r.get("advertId"),
                "operation_id": r.get("updNum"),
                "date": iso(r.get("updTime"), MOSCOW),
                "spend": r.get("updSum"),
                "payment_type": r.get("paymentType"),
                "currency": None,
                "sku": None,
                "article": None,
                "nm_id": None,
            }
            for r in ledger
            if not ids or str(r.get("advertId")) in set(map(str, ids))
        ]
        return _slice(
            items,
            args,
            scope="campaign_ledger_without_product_attribution",
            currency_basis="seller_profile_currency_not_reported",
        )
    if report == "statistics" and not ids:
        raise ValueError("Campaign IDs required for statistics")
    if (report=="campaigns" or not ids) and not args.get("offset"):
        path="/api/advert/v3/adverts"
        q={"advertIds":",".join(map(str,ids))} if ids else {"limit":min(50,args.get("limit",50))}
        if args.get("cursor"): q["next"]=args["cursor"]
        raw=request("GET",ADVERT+path,query=q,interval=1,basic_interval=3600,ttl=1800)
        records=[dict(campaign_id=r["advertId"],status=r.get("status"),type=None,
                      payment_type=(r.get("settings") or {}).get("paymentType"),currency=r.get("currency"),
                      nm_ids=[n["nmId"] for n in rows(r.get("nms",[]))]) for r in rows(raw["adverts"])]
        marker=raw["next"]
        if not isinstance(marker,str) or marker and marker==args.get("cursor"): raise ValueError("Invalid advertising cursor")
        return result(records,marker or None,status="choose_campaign_ids",source=path)
    if report == "campaigns" or not ids:
        raw = request("GET", ADVERT + "/adv/v1/promotion/count", interval=1, basic_interval=900, ttl=1800)
        campaigns = [
            {"campaign_id": c.get("advertId"), "status": g.get("status"), "type": g.get("type")}
            for g in rows(raw.get("adverts") or [])
            for c in rows(g.get("advert_list", []))
        ]
        return _slice(campaigns, args, status="choose_campaign_ids")
    raw = request(
        "GET",
        ADVERT + "/api/advert/v4/fullstats",
        query={
            "advertIds": ",".join(map(str, ids)),
            "beginDate": args["date_from"],
            "endDate": args["date_to"],
        },
        interval=20,
        basic_interval=3600,
        ttl=1800,
    )
    items = []
    for r in rows(raw.get("rows")):
        row = {
            "campaign_id": r.get("advertId"),
            "nm_id": r.get("nmId"),
            "article": None,
            "sku": None,
            "date": r.get("date"),
            "currency": r.get("currency"),
            "placement": r.get("placement"),
            "conversion_type": r.get("conversionType"),
            "spend": r.get("sum"),
            "views": r.get("views"),
            "clicks": r.get("clicks"),
            "orders": r.get("orders"),
            "ordered_units": r.get("shks"),
            "ordered_amount": r.get("sumPrice"),
        }
        if matches(row, args):
            items.append(row)
    extra = (
        {"direct_attribution": direct_attribution(items, "nm_id", "ordered_units")}
        if args.get("include_order_share")
        else {}
    )
    return _slice(items, args, scope="campaign_reported_attribution", **extra)


def _slots(args, request):
    q = {"warehouseIDs": str(args["warehouse_id"])} if "warehouse_id" in args else {}
    try:
        raw = request("GET", COEFFICIENTS, query=q, interval=10, basic_interval=3600, ttl=1800)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410, 503):
            raise
        exc.close()
        return _warehouse_directory(
            args,
            request,
            result(
                [],
                complete=False,
                status="upstream_disabled",
                horizon_days=14,
                source="https://dev.wildberries.ru/release-notes?id=570",
            ),
        )
    today = datetime.now(MOSCOW).date()
    items = []
    for r in rows(raw):
        day = date.fromisoformat(str(r["date"])[:10])
        if not today <= day < today + timedelta(days=14):
            continue
        if "warehouse_id" in args and str(r.get("warehouseID")) != str(args["warehouse_id"]):
            continue
        c = money(r.get("coefficient"))
        items.append(
            {
                "date": day.isoformat(),
                "warehouse_id": r.get("warehouseID"),
                "warehouse": r.get("warehouseName"),
                "coefficient": r.get("coefficient"),
                "allow_unload": r.get("allowUnload"),
                "box_type_id": r.get("boxTypeID"),
                "available": None
                if c is None or type(r.get("allowUnload")) is not bool
                else c >= 0 and r["allowUnload"],
            }
        )
    data = _warehouse_directory(
        args,
        request,
        result(items, horizon_days=14, status="reported", coverage="published_warehouse_dates_only"),
    )
    dates = [(today + timedelta(days=i)).isoformat() for i in range(14)]
    warehouses = {
        str(r["warehouse_id"]) for r in items + data.get("warehouses", []) if r["warehouse_id"] is not None
    }
    if "warehouse_id" in args:
        warehouses.add(str(args["warehouse_id"]))
    published = defaultdict(set)
    by_box = defaultdict(set)
    unknown = False
    for row in items:
        if row["available"] is None or row["box_type_id"] is None or row["warehouse_id"] is None:
            unknown = True
        else:
            warehouse = str(row["warehouse_id"])
            published[warehouse].add(row["date"])
            by_box[warehouse, str(row["box_type_id"])].add(row["date"])
    missing = {
        w: [d for d in dates if d not in published[w]]
        for w in sorted(warehouses)
        if len(published[w]) != len(dates)
    }
    missing_boxes = {}
    for (warehouse, box), available_dates in sorted(by_box.items()):
        if len(available_dates) != len(dates):
            missing_boxes.setdefault(warehouse, {})[box] = [d for d in dates if d not in available_dates]
    data.update(
        complete=data["complete"]
        and bool(warehouses)
        and not missing
        and not missing_boxes
        and not unknown,
        date_from=dates[0],
        date_to=dates[-1],
        missing_dates_by_warehouse=missing,
        missing_dates_by_box_type=missing_boxes,
        incomplete_slot_fields=unknown,
    )
    return data


def _warehouse_directory(args, request, data):
    if not args.get("include_warehouses"):
        return data
    try:
        catalog = request(
            "GET",
            "https://supplies-api.wildberries.ru/api/v1/warehouses",
            interval=10,
            basic_interval=43200,
            ttl=1800,
        )
    except urllib.error.HTTPError as exc:
        data.update(
            warehouses_status="unavailable",
            warehouse_catalog_http_status=exc.code,
            warehouse_category="Поставки",
            complete=False,
        )
        exc.close()
        return data
    data.update(
        warehouses_status="reported",
        warehouses=[
            {"warehouse_id": r.get("ID"), "name": r.get("name")}
            for r in rows(catalog)
            if "warehouse_id" not in args or str(r.get("ID")) == str(args["warehouse_id"])
        ],
    )
    return data


def read(name, args, request):
    validate(name, args)
    from wb_reports import READERS
    if name in READERS:
        return READERS[name](args,request)
    if name == "wb_supplies":
        return _supplies(args, request)
    if name == "wb_supply_goods":
        return _goods(args, request)
    if name in ("wb_orders", "wb_sales"):
        return _stats(name, args, request)
    if name in REPORTS:
        return _report(name, args, request)
    if name == "wb_acceptance_slots":
        return _slots(args, request)
    if name == "wb_finance_report":
        return _finance(args, request)
    if name == "wb_funnel":
        return _funnel(args, request)
    if name == "wb_ads":
        return _ads(args, request)
    if name == "wb_feedbacks":
        return _reviews(args, request)
    raise ValueError("Unknown WB report")
