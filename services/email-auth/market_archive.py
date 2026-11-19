"""Incrementally archive complete seller reporting days, respecting the interactive transport gates."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import market_history
from market_specs import MOSCOW
import wb_expansion
import ozon_expansion


DATASETS = (
    "wb_orders",
    "wb_sales",
    "wb_finance_report",
    "ozon_sku_daily",
    "ozon_posting_sku_daily",
    "wb_availability",
    "ozon_availability",
)


def collect_posting_days(wire, history, day):
    import ozon_posting_status
    from market_specs import money
    start = (datetime.fromisoformat(day).date() - timedelta(days=6)).isoformat()
    owner = market_history.scope("ozon", wire.creds)
    with wire.progress("ozon-posting-sku-days-v1", [start, day]) as (saved, save):
        state = saved or {"scheme": 0, "cursor": "", "cursors": [], "seen": [], "groups": {}}
        for _ in range(8):
            if state["scheme"] == 2:
                break
            scheme = ("FBO", "FBS")[state["scheme"]]
            args = dict(date_from=start, date_to=day, scheme=scheme, cursor=state["cursor"], limit=100)
            req = ozon_posting_status.build_request(args, wire.creds)
            body = json.loads(req.data)
            body["sort_dir"] = "ASC"
            raw = wire("POST", req.full_url, body=body)
            postings, more, cursor = ozon_posting_status._page(args, raw, wire.creds)
            seen = set(state["seen"])
            for posting in postings:
                ident = posting.get("posting_number")
                if not ident or ident in seen:
                    return "duplicate_or_missing_posting"
                seen.add(ident)
                field = "created_at" if scheme == "FBO" else "in_process_at"
                stamp = datetime.fromisoformat(posting[field].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    return "posting_date_without_timezone"
                date_key = stamp.astimezone(timezone.utc).date().isoformat()
                if not start <= date_key <= day:
                    return "posting_date_outside_period"
                products = posting.get("products")
                if not isinstance(products, list) or not products:
                    return "posting_product_identity_unavailable"
                for item in products:
                    sku, article, units = item.get("sku"), item.get("offer_id"), item.get("quantity")
                    if not sku or not article or type(units) is not int or units <= 0:
                        return "posting_product_identity_unavailable"
                    price = item.get("price") or {}
                    currency = price.get("currency") or None
                    amount = money(price.get("amount"))
                    key = json.dumps([date_key, scheme, str(sku), article, currency])
                    row = state["groups"].setdefault(key, dict(date=date_key, sku=str(sku), article=article,
                        offer_id=article, scheme=scheme, date_basis=field+"_utc", source=req.selector,
                        currency=currency, posting_units=0, posting_item_value="0", priced_units=0,
                        ordered_units=None, revenue=None))
                    row["posting_units"] += units
                    if amount is not None and currency:
                        row["posting_item_value"] = str(Decimal(row["posting_item_value"]) + amount*units)
                        row["priced_units"] += units
            if len(seen) > 20000 or len(state["groups"]) > 30000:
                return "posting_volume_exceeds_bound"
            if more:
                if cursor in state["cursors"]:
                    return "nonadvancing_cursor"
                state.update(cursor=cursor, seen=sorted(seen))
                state["cursors"].append(cursor)
            else:
                state.update(scheme=state["scheme"]+1, cursor="", cursors=[], seen=[])
            save(state, 7*86400)
        if state["scheme"] < 2:
            return "pending"
        groups = {d: [] for d in market_history.days_between(start, day)}
        for row in state["groups"].values():
            if row["priced_units"] != row["posting_units"]:
                row["posting_item_value"] = None
            groups[row["date"]].append(row)
        stamp = market_history.utc()
        for d, records in groups.items():
            history.save_day(owner, "ozon_posting_sku_daily", d, records, stamp)
        history.confirm(wire.creds, owner)
        save({}, 1)
        return "complete"


def target_day(history, owner, name, today):
    coverage = history.coverage(owner, name)
    if name.endswith("availability"):
        return today.isoformat() if today.isoformat() not in coverage else None
    for n in range(1, 90):
        day = (today - timedelta(days=n)).isoformat()
        if day not in coverage:
            return day
    for n in (3, 7, 14):
        day = today - timedelta(days=n)
        stamp = coverage.get(day.isoformat())
        mature = (today - timedelta(days=1)).isoformat()
        if stamp[:10] < mature:
            return day.isoformat()
    return None


def collect(wire, history, name, day):
    if name == "ozon_posting_sku_daily":
        return collect_posting_days(wire, history, day)
    owner = market_history.scope(wire.provider, wire.creds)
    batch = name in ("wb_orders", "ozon_sku_daily")
    week_start = (datetime.fromisoformat(day).date() - timedelta(days=6)).isoformat()
    namespace = "archive-wb-order-week-v2" if name == "wb_orders" else "archive-week-v1" if batch else "archive-v2"
    with wire.progress(namespace, [name, day]) as (progress, save):
        state = progress or {"cursor": None, "offset": 0, "records": [], "seen": []}
        query = {"date_from": day, "date_to": day, "limit": 100}
        cursor = state["cursor"]
        if name in ("wb_orders", "wb_sales"):
            if cursor:
                query["cursor"] = cursor
            if name == "wb_orders":
                oldest = (datetime.now(MOSCOW).date() - timedelta(days=89)).isoformat()
                query.update(date_from=max(week_start, oldest), limit=1000)
            page = wb_expansion._stats(name, query, wire, exact_day=name == "wb_sales")
            if page.get("source") == "/api/analytics/v1/order-feed":
                query["date_from"] = page["coverage_from"]
                page["complete"] = page.get("source_exhausted", False)
                if hasattr(wire, "gate_dir"):
                    from expansion_gateway import _seller_mappings
                    by_nm = {}
                    for mapping in _seller_mappings(wire.gate_dir).values():
                        if mapping.get("wb_nm_id") and mapping.get("wb_vendor_code"):
                            by_nm.setdefault(str(mapping["wb_nm_id"]), set()).add(mapping["wb_vendor_code"])
                    for row in page["items"]:
                        articles = by_nm.get(str(row["nm_id"]), set())
                        if len(articles) == 1:
                            row.update(article=next(iter(articles)), article_basis="seller_nm_id_mapping")
        elif name == "wb_finance_report":
            target = datetime.fromisoformat(day).date()
            monday = target - timedelta(days=target.weekday())
            query.update(date_from=monday.isoformat(), date_to=target.isoformat(), limit=5000)
            if cursor:
                query["cursor"] = cursor
            page = wb_expansion._finance(query, wire)
            page["items"].extend(page.get("unallocated", []))
        elif name == "wb_availability":
            query = {"offset": state["offset"], "limit": 100}
            if state.get("task_id"):
                query["task_id"] = state["task_id"]
            page = wb_expansion.read("wb_stocks", query, wire)
        elif name == "ozon_sku_daily":
            query.update(date_from=week_start, limit=1000)
            page = ozon_expansion._daily(dict(query, offset=state["offset"]), wire)
            if page["items"]:
                import expansion_gateway

                expansion_gateway._enrich(name, query, page, wire)
        else:
            q = {"cursor": cursor} if cursor else {}
            catalog = ozon_expansion.read("ozon_stocks", dict(q, limit=50), wire)
            skus = sorted(
                {
                    str(r["sku"])
                    for r in catalog["items"]
                    if r.get("sku") and str(r.get("scheme", "")).lower() != "fbs"
                }
            )
            page = {"items": [], "complete": catalog["complete"], "next_cursor": catalog["next_cursor"]}
            for offset in range(0, len(skus), 100):
                stocks = ozon_expansion._stocks(
                    "ozon_stocks_by_cluster", {"skus": skus[offset : offset + 100]}, wire
                )
                if not stocks["complete"]:
                    return "partial"
                page["items"].extend(stocks["items"])
        if page.get("task_id"):
            state["task_id"] = page["task_id"]
        if page.get("issues") or (not page.get("complete") and page.get("next_cursor") is None):
            save(state, 86400)
            return "pending"
        records = state["records"] + page["items"]
        if len(records) > 100000:
            return "day_exceeds_row_bound"
        if page["complete"]:
            if (
                name.endswith("availability")
                and day
                != datetime.now(timezone.utc if wire.provider == "ozon" else MOSCOW).date().isoformat()
            ):
                return "snapshot_day_changed"
            if name == "wb_finance_report" or batch:
                groups = {d: [] for d in market_history.days_between(query["date_from"], query["date_to"])}
                if name == "wb_orders":
                    # Different change pages can refer to the same order. Keep the last
                    # version supplied by WB, including cancellations, before sealing a day.
                    latest = {}
                    for record in records:
                        if not record.get("srid") or not record.get("last_change"):
                            return "order_identity_unavailable"
                        sid = record["srid"]
                        previous = latest.get(sid)
                        stamp = datetime.fromisoformat(record["last_change"])
                        old_stamp = datetime.fromisoformat(previous["last_change"]) if previous else None
                        if old_stamp is None or old_stamp < stamp:
                            latest[sid] = record
                        elif old_stamp == stamp and previous != record:
                            return "conflicting_order_version"
                    records = latest.values()
                for record in records:
                    group = str(record.get("date", ""))[:10]
                    if group not in groups:
                        return "date_outside_requested_archive_period"
                    groups[group].append(record)
                stamp = market_history.utc()
                for financial_day, items in groups.items():
                    history.save_day(owner, name, financial_day, items, stamp)
            else:
                history.save_day(owner, name, day, records, market_history.utc())
            history.confirm(wire.creds, owner)
            save({}, 1)
            return "complete"
        marker = page["next_cursor"]
        if marker in state["seen"]:
            return "nonadvancing_cursor"
        state["seen"].append(marker)
        state.update(cursor=marker, offset=marker if type(marker) is int else 0, records=records)
        save(state, 7 * 86400)
        return "pending"


def tick(name, credentials, directory, deadline):
    import expansion_gateway

    if name not in DATASETS:
        raise ValueError("Invalid dataset")
    db = market_history.History(directory)
    provider = name.split("_")[0]
    wire = expansion_gateway.make_wire(provider, credentials, directory, deadline)
    today = datetime.now(MOSCOW if provider == "wb" else timezone.utc).date()
    day = target_day(db, market_history.scope(provider, wire.creds), name, today)
    status = collect(wire, db, name, day) if day else "up_to_date"
    db.record(credentials, "archive_" + name, status)
    return {"dataset": name, "day": day, "status": status, "read_only": True}
