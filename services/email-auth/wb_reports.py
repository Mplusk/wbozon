"""Read-only Wildberries reports with distinct date, money and pagination contracts."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from market_specs import MOSCOW, iso, money, result, rows, select
import wb_expansion as wb


def _period(args):
    return {"start": args["date_from"], "end": args["date_to"]}


def _ids(args):
    return [int(n) for n in args.get("nm_ids", [args["nm_id"]] if "nm_id" in args else [])]


def funnel_history(args, request):
    ids = _ids(args)
    if not 1 <= len(ids) <= 20:
        return result([], complete=False, status="select_1_to_20_products")
    if date.fromisoformat(args["date_from"]) < datetime.now(MOSCOW).date() - timedelta(days=6):
        return result(
            [],
            complete=False,
            status="outside_funnel_history_window",
            retention_days=7,
            alternative="wb_funnel без group_by возвращает агрегат за выбранный период",
        )
    path = "/api/analytics/v3/sales-funnel/products/history"
    data = request(
        "POST",
        wb.ANALYTICS + path,
        body={"selectedPeriod": _period(args), "nmIds": ids, "aggregationLevel": args["group_by"]},
        interval=20,
        basic_interval=1800,
        ttl=1800,
    )
    items = []
    for record in rows(data):
        identity = wb._item(record["product"])
        if identity["nm_id"] not in ids:
            continue
        for point in rows(record["history"]):
            items.append(
                dict(
                    identity,
                    **select(
                        point,
                        "date openCount cartCount orderCount orderSum buyoutCount buyoutSum conversions",
                    ),
                    currency=record.get("currency"),
                )
            )
    return result(items, source=path, date_basis="order_date_moscow", aggregation=args["group_by"])


def retentions(args, request):
    page = wb._finance(
        {
            k: v
            for k, v in args.items()
            if k in ("date_from", "date_to", "article", "nm_id", "cursor", "limit")
        },
        request,
    )
    kinds = (
        ("penalty", "deduction")
        if args.get("retention_type", "all") == "all"
        else (args["retention_type"],)
    )
    selected = []
    candidates = page["items"] + (
        [] if "article" in args or "nm_id" in args else page.get("unallocated", [])
    )
    for row in candidates:
        for kind in kinds:
            amount = money(row.get(kind))
            if amount:
                selected.append(
                    dict(
                        article=row["article"],
                        nm_id=row["nm_id"],
                        date=row["date"],
                        reason=row.get("reason") or row["operation"],
                        kind=kind,
                        amount=amount,
                        currency=row["currency"],
                        rrd_id=row["rrd_id"],
                        report_id=row["report_id"],
                    )
                )
    return result(
        selected,
        page["next_cursor"],
        complete=page["complete"],
        issues=page.get("issues", []),
        source=wb.FINANCE,
        date_basis="rrDate",
        amount_basis="signed_provider_charge",
    )


def region(args, request):
    path = "/api/analytics/v1/order-feed"
    if date.fromisoformat(args["date_from"]) < datetime.now(MOSCOW).date() - timedelta(days=30):
        return result(
            [],
            complete=False,
            status="outside_provider_retention",
            retention_days=31,
            historical_source="wb_sales",
        )
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    pagination = {"limit": limit, "offset": offset}
    if offset:
        stamp = args.get("snapshot_time")
        if not stamp or not iso(stamp):
            raise ValueError("Continuation requires the initial snapshot time")
        pagination["snapshotTime"] = stamp
    period = {"start": args["date_from"] + "T00:00:00+03:00", "end": args["date_to"] + "T23:59:59.999999+03:00"}
    body = {"selectedPeriod": period, "pagination": pagination}
    ids = _ids(args)
    if ids:
        body["nmIds"] = ids
    data = request("POST", wb.ANALYTICS + path, body=body, interval=60, basic_interval=10800, ttl=1800)[
        "data"
    ]
    page = rows(data["orders"])
    stamp = iso(data.get("snapshotTime") or None)
    if offset and stamp != iso(args["snapshot_time"]):
        return result([], complete=False, status="snapshot_changed_or_missing", source=path)
    items, seen, groups = [], set(), {}
    for r in page:
        ident = r["srid"]
        if not ident or ident in seen:
            raise ValueError("Missing or duplicate order")
        seen.add(ident)
        if ids and r["nmId"] not in ids:
            continue
        row = dict(
            article=args.get("article"),
            nm_id=r["nmId"],
            region=r["destinationDistrict"],
            city=r.get("destinationCity"),
            status=r["status"],
            units=1,
            amount=r.get("sellerPrice"),
            currency=data.get("currency"),
            updated_at=iso(r["updatedAt"]),
            created_at=iso(r["createdAt"]),
        )
        items.append(row)
        key = row["region"], row["city"], row["status"], row["currency"], row["nm_id"]
        group = groups.setdefault(
            key,
            dict(
                article=row["article"],
                nm_id=row["nm_id"],
                region=row["region"],
                city=row["city"],
                status=row["status"],
                currency=row["currency"],
                units=0,
                amount=Decimal(0),
            ),
        )
        group["units"] += 1
        amount = money(row["amount"])
        group["amount"] = (
            group["amount"] + amount if amount is not None and group["amount"] is not None else None
        )
    more = len(page) == limit
    can_continue = more and stamp is not None
    return result(
        list(groups.values()) if args.get("mode") == "summary" else items,
        offset + limit if can_continue else None,
        complete=not more,
        snapshot_time=stamp,
        snapshot_status="reported" if stamp else "not_reported",
        **({"data_as_of": stamp} if stamp else {}),
        **({"status": "snapshot_required_for_continuation"} if more and not can_continue else {}),
        date_basis="latest_status_updated_at",
        source=path,
    )


def returns(args, request):
    path = "/api/analytics/v1/item-returns"
    limit, offset = args.get("limit", 100), args.get("offset", 0)
    q = {
        "dateFrom": args["date_from"],
        "dateTo": args["date_to"],
        "limit": limit,
        "offset": offset,
        "status": args.get("return_status", "archive"),
    }
    data = request("GET", wb.ANALYTICS + path, query=q, interval=60, basic_interval=3600, ttl=1800)
    if data is None:
        return result([], source=path)
    page = rows(data["report"])
    items = [
        dict(
            article=args.get("article"),
            nm_id=r.get("nmId"),
            barcode=r.get("sku"),
            **select(
                r, "orderId srid returnReason returnType returnStatus orderDt completedDt readyToReturnDt"
            ),
        )
        for r in page
        if "nm_id" not in args or str(r.get("nmId")) == str(args["nm_id"])
    ]
    count = int(data["count"])
    return result(
        items,
        offset + len(page) if page and offset + len(page) < count else None,
        complete=offset + len(page) >= count,
        source=path,
        date_basis="return_to_seller_report",
        quantities_and_sums_reported=False,
    )


def search(args, request):
    path = "/api/v2/search-report/product/search-texts"
    ids = _ids(args)
    if not ids:
        return result([], complete=False, status="article_or_nm_ids_required")
    limit = args.get("limit", 30)
    metric = args.get("search_limit", "orders")
    data = request(
        "POST",
        wb.ANALYTICS + path,
        body={
            "currentPeriod": _period(args),
            "nmIds": ids,
            "topOrderBy": metric,
            "orderBy": {"field": metric, "mode": "desc"},
            "limit": limit,
        },
        interval=20,
        basic_interval=1800,
        ttl=1800,
    )["data"]
    items = []
    for r in rows(data["items"]):
        if r.get("nmId") in ids:
            values = {
                k: (r.get(k) or {}).get("current")
                for k in ("frequency", "avgPosition", "medianPosition", "orders", "openCard", "addToCart")
            }
            items.append(
                dict(
                    article=args.get("article") or r.get("vendorCode"),
                    nm_id=r.get("nmId"),
                    query=r.get("text"),
                    **values,
                )
            )
    return result(
        items, source=path, scope="provider_top_queries", limit=limit, currency=data.get("currency")
    )


def ad_finance(args, request):
    kind = args.get("document", "spend")
    if kind == "documents":
        path = "/api/v1/documents/list"
        limit, offset = args.get("limit", 50), args.get("offset", 0)
        q = {
            "beginTime": args["date_from"],
            "endTime": args["date_to"],
            "limit": limit,
            "offset": offset,
            "locale": "ru",
        }
        data = request(
            "GET", "https://documents-api.wildberries.ru" + path, query=q, interval=10, ttl=1800
        )["data"]
        page = rows(data["documents"])
        words = ("реклам", "продвиж", "упд")
        items = [
            select(r, "serviceName name category extensions creationTime")
            for r in page
            if any(
                w in (str(r.get("name", "")) + " " + str(r.get("category", ""))).casefold() for w in words
            )
        ]
        return result(
            items,
            offset + limit if len(page) == limit else None,
            source=path,
            scope="advertising_or_upd_documents_by_title",
        )
    path = "/adv/v1/" + ("payments" if kind == "payments" else "upd")
    raw = request(
        "GET",
        wb.ADVERT + path,
        query={"from": args["date_from"], "to": args["date_to"]},
        interval=1,
        ttl=1800,
    )
    page = rows([] if raw is None else raw)
    campaigns = set(map(str, args.get("campaign_ids", [])))
    if kind == "payments" and campaigns:
        return result([], complete=False, status="payments_are_account_level")
    if kind == "payments":
        items = [
            dict(select(r, "id date sum type statusId cardStatus currency"), article=None) for r in page
        ]
    else:
        currency = None
        if page:
            import urllib.error

            try:
                currency = request("GET", wb.ADVERT + "/adv/v1/balance", interval=1, ttl=1800).get(
                    "currency"
                )
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 402, 403):
                    raise
                exc.close()
        items = [
            dict(
                select(r, "updNum updTime updSum advertId campName advertType paymentType advertStatus"),
                article=None,
                currency=currency,
            )
            for r in page
            if not campaigns or str(r.get("advertId")) in campaigns
        ]
    return wb._slice(items, args, source=path, allocation="campaign_or_account")


READERS = {
    "wb_retentions": retentions,
    "wb_region_sales": region,
    "wb_returns_report": returns,
    "wb_ads_finance": ad_finance,
    "wb_search_queries": search,
}
