"""Period grouping for the existing WB sales tool and exact seller-article price lookup."""

from datetime import date, timedelta
import time
import wb_mcp
from market_specs import result, rows, select


def sales(args, wire):
    start, end = (date.fromisoformat(args[k]) for k in ("date_from", "date_to"))
    group = args["group_by"]
    interval = start
    output = []
    with wire.progress("wb-sales-groups-v2", args) as (saved, save):
        state = saved or {
            "date": start.isoformat(),
            "offset": 0,
            "rows": [],
            "currency": None,
            "totals": {},
        }
        for _ in range(15):
            interval = date.fromisoformat(state["date"])
            if interval > end or time.monotonic() + 2 >= wire.deadline:
                break
            stop = (
                min(end, interval + timedelta(days=6 - interval.weekday())) if group == "week" else interval
            )
            body = {
                "selectedPeriod": {"start": interval.isoformat(), "end": stop.isoformat()},
                "limit": 100,
                "offset": state["offset"],
                "skipDeletedNm": False,
            }
            if "nm_ids" in args:
                body["nmIds"] = list(map(int, args["nm_ids"]))
            data = wire(
                "POST",
                "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products",
                body=body,
                interval=20,
                basic_interval=1800,
                ttl=1800,
            )["data"]
            page = rows(data["products"])
            currency = data["currency"]
            if not currency or state["currency"] not in (None, currency):
                raise ValueError("Currency mismatch")
            state["currency"] = currency
            for row in page:
                product = row["product"]
                if "vendor_codes" in args and product["vendorCode"] not in args["vendor_codes"]:
                    continue
                if "nm_ids" in args and str(product["nmId"]) not in set(map(str, args["nm_ids"])):
                    continue
                selected = row["statistic"]["selected"]
                if selected.get("period") != body["selectedPeriod"]:
                    raise ValueError("Period mismatch")
                bucket = state["totals"].setdefault(product["vendorCode"], {})
                for field in (
                    "orderCount",
                    "orderSum",
                    "buyoutCount",
                    "buyoutSum",
                    "cancelCount",
                    "cancelSum",
                ):
                    value = selected[field]
                    if type(value) is not int or value < 0:
                        raise ValueError("Invalid sales figure")
                    bucket[field] = bucket.get(field, 0) + value
            if len(page) == 100:
                state["offset"] += 100
            else:
                state["rows"].extend(
                    dict(
                        article=k,
                        period_from=interval.isoformat(),
                        period_to=stop.isoformat(),
                        currency=currency,
                        **v,
                    )
                    for k, v in state["totals"].items()
                )
                state.update(date=(stop + timedelta(days=1)).isoformat(), offset=0, totals={})
            save(state, 1800 if date.fromisoformat(state["date"]) > end else 7 * 86400)
            if date.fromisoformat(state["date"]) <= end:
                break
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        finished = date.fromisoformat(state["date"]) > end
        more = offset + limit < len(state["rows"])
        return result(
            state["rows"][offset : offset + limit],
            offset + limit if more else None,
            complete=finished and not more,
            status="ready" if finished else "pending",
            resume="repeat_same_arguments",
            next_date=None if finished else state["date"],
            data_as_of=None,
            currency=state["currency"],
            date_basis="order_date_moscow",
        )


def prices(args, wire):
    article = args["vendor_code"]
    offset, limit = args.get("offset", 0), args.get("limit", 20)
    body = {"settings": {"cursor": {"limit": 100}, "filter": {"textSearch": article, "withPhoto": -1}}}
    raw = wire("POST", "https://content-api.wildberries.ru/content/v2/get/cards/list", body=body, ttl=1800)
    matches = sorted({int(c["nmID"]) for c in rows(raw["cards"]) if c.get("vendorCode") == article})
    items = []
    for nm in matches[offset : offset + min(limit, 10)]:
        data = wire(
            "GET",
            "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter",
            query={"limit": 1, "offset": 0, "filterNmID": nm},
            ttl=600,
        )
        for r in rows(data["data"]["listGoods"]):
            if str(r["nmID"]) == str(nm) and r["vendorCode"] == article:
                items.append(
                    dict(
                        select(r, "nmID vendorCode discount clubDiscount currencyIsoCode4217"),
                        sizes=[
                            select(s, "sizeID techSizeName price discountedPrice clubDiscountedPrice")
                            for s in rows(r["sizes"])
                        ],
                    )
                )
    next_offset = offset + min(limit, 10) if offset + min(limit, 10) < len(matches) else None
    complete = next_offset is None and raw.get("cursor", {}).get("total", len(raw["cards"])) < 100
    return result(
        items,
        wb_mcp._cursor("wb_prices", args, {"offset": next_offset}) if next_offset is not None else None,
        complete=complete,
        next_offset=next_offset,
        offset=offset,
        matched_card_ids=matches,
        article=article,
    )
