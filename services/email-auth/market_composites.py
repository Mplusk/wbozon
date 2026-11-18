"""Cross-marketplace reports. Identity, monetary basis and unavailable sources stay explicit."""

import calendar
from datetime import date, timedelta
from decimal import Decimal, localcontext
import time
import urllib.error

import finance_gate
import market_history
from market_specs import money, result
import ozon_expansion
import ozon_performance
import wb_expansion


def observed(fn):
    try:
        return fn()
    except urllib.error.HTTPError as exc:
        delay, _ = finance_gate.parse_retry_after(exc.headers)
        status = exc.code
        exc.close()
        return result(
            [],
            complete=False,
            status="provider_access_denied" if status in (401, 402, 403) else "provider_http_error",
            http_status=status,
            retry_after_seconds=delay,
        )
    except finance_gate.GateError as exc:
        return result([], complete=False, status=exc.reason, retry_after_seconds=exc.retry_after_seconds)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return result([], complete=False, status="provider_response_invalid")
    except (
        urllib.error.URLError,
        TimeoutError,
        finance_gate.ProviderResponseTooLarge,
        finance_gate.ProviderResponseInvalid,
    ):
        return result([], complete=False, status="source_unavailable")


def identity(args, wires, mapping):
    query = args["article"]
    wb, oz = wires.get("wb"), wires.get("ozon")

    def cards():
        code = mapping["wb_vendor_code"] if mapping else query
        body = {"settings": {"filter": {"textSearch": code, "withPhoto": -1}, "cursor": {"limit": 100}}}
        raw = wb(
            "POST", "https://content-api.wildberries.ru/content/v2/get/cards/list", body=body, ttl=1800
        )
        cards = raw["cards"]
        return result(
            [
                {"article": c.get("vendorCode"), "nm_id": c.get("nmID"), "name": c.get("title")}
                for c in cards
                if (
                    c.get("vendorCode") == code
                    or (not mapping and query.casefold() in str(c.get("title", "")).casefold())
                )
                and (
                    not mapping
                    or not mapping.get("wb_nm_id")
                    or str(c.get("nmID")) == str(mapping["wb_nm_id"])
                )
            ],
            complete=(raw.get("cursor") or {}).get("total", len(cards)) < 100,
        )

    wb_data = observed(cards) if wb else result([], complete=False, status="not_configured")
    offer = mapping["ozon_offer_id"] if mapping else query

    def ozon():
        card = ozon_expansion._article_card(offer, oz)
        return result(
            [
                {
                    "article": card["offer_id"],
                    "product_id": card["product_id"],
                    "name": card["name"],
                    "skus": card["sku_candidates"],
                }
            ]
            if card
            else []
        )

    oz_data = observed(ozon) if oz else result([], complete=False, status="not_configured")
    confirmed = bool(
        oz_data.get("complete")
        and wb_data.get("complete")
        and len(wb_data["items"]) == len(oz_data["items"]) == 1
        and (mapping or wb_data["items"][0]["article"] == oz_data["items"][0]["article"])
    )
    verified = (
        {
            "ozon_offer_id": oz_data["items"][0]["article"],
            "wb_vendor_code": wb_data["items"][0]["article"],
            "wb_nm_id": wb_data["items"][0]["nm_id"],
        }
        if confirmed
        else None
    )
    return result(
        [{"ozon": oz_data, "wb": wb_data}],
        complete=confirmed,
        mapping=verified,
        query=query,
        status="confirmed" if confirmed else "unverified_mapping",
        basis="seller_mapping_and_both_catalogs" if mapping else "exact_common_seller_code_or_candidates",
    )


COMPONENTS = (
    "gross_sales",
    "commission",
    "logistics",
    "storage",
    "acceptance",
    "ads",
    "retentions",
    "for_pay",
)


def unit_row(provider, article, currency, units, returned, amounts, cost=None, cost_currency=None):
    with localcontext() as ctx:
        ctx.prec = 128
        numeric = {k: money(amounts.get(k)) for k in COMPONENTS}
        denominator = units if type(units) is int and units > 0 and currency else None
        rates = {
            k: str(v / denominator) if v is not None and denominator else None for k, v in numeric.items()
        }
        cost = money(cost)
        margin = None
        expenses_known = all(
            numeric[k] is not None and numeric[k] >= 0
            for k in ("logistics", "storage", "acceptance", "ads", "retentions")
        )
        if (
            all(v is not None for v in numeric.values())
            and expenses_known
            and denominator
            and cost is not None
            and cost >= 0
            and currency == cost_currency
        ):
            expenses = sum(numeric[k] for k in ("logistics", "storage", "acceptance", "ads", "retentions"))
            margin = str((numeric["for_pay"] - expenses) / denominator - cost)
        return dict(
            marketplace=provider,
            article=article,
            currency=currency,
            sold_units=units,
            returned_units=returned,
            amounts={k: str(v) if v is not None else None for k, v in numeric.items()},
            per_sold_unit=rates,
            bank_payout_per_unit=None,
            margin_per_unit=margin,
            denominator="gross_sold_units_before_returns",
            unavailable=[k for k, v in numeric.items() if v is None],
        )


def advertising(provider, period, selector, wires):
    if provider == "wb":
        wire = wires["wb"]
        directory = wb_expansion._ads(dict(period, report="spend_ledger", limit=100), wire)
        ids = sorted({str(r["campaign_id"]) for r in directory["items"] if r.get("campaign_id")})
        if not directory["complete"] or len(ids) > 50:
            return result([], complete=False, status="campaign_coverage_partial")
        if not ids:
            return result([], status="no_spend_records", spend_by_currency={})
        report = wb_expansion._ads(dict(period, nm_id=selector["nm_id"], campaign_ids=ids, limit=100), wire)
        selected = [r for r in report["items"] if r.get("conversion_type") == "direct"]
    else:
        if "performance" not in wires:
            return result([], complete=False, status="performance_not_configured")
        wire = wires["performance"]
        directory = ozon_performance.read(dict(period, limit=100), wire, wire.creds)
        ids = [str(r["id"]) for r in directory["items"]]
        if not directory["complete"] or len(ids) > 10:
            return result([], complete=False, status="campaign_coverage_partial")
        if not ids:
            return result([], status="empty_campaign_directory", spend_by_currency={})
        skus = set(map(str, ozon_expansion.lookup(selector["article"], wires["ozon"])))
        if not skus:
            return result([], complete=False, status="identity_unavailable")
        report = ozon_performance.read(
            dict(period, campaign_ids=ids, limit=100), wire, wire.creds, allowed_skus=skus
        )
        selected = report["items"]
    if not report["complete"]:
        return result(
            [],
            complete=False,
            status=report.get("status", "partial_ad_report"),
            task_id=report.get("task_id"),
        )
    amounts = {}
    for row in selected:
        c, n = row.get("currency"), money(row.get("spend"))
        if not c or n is None:
            return result([], complete=False, status="unconfirmed_ad_currency_or_amount")
        amounts[c] = amounts.get(c, Decimal(0)) + n
    return result([], spend_by_currency=amounts, scope="campaign_reported_product_costs")


def accounting(args, wires, mapping, wb_finance, ozon_finance):
    start = date.fromisoformat(args["month"] + "-01")
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    period = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    items = []
    sources = {}
    if "wb" in wires:
        article = mapping["wb_vendor_code"]
        history = market_history.History(wires["wb"].gate_dir)
        days = history.verified_days(
            "wb", wires["wb"].creds, "wb_finance_report", period["date_from"], period["date_to"]
        )
        if len(days) == (end - start).days + 1:
            data = result(
                wb_expansion.finance_summary(
                    [r for _, records in days.values() for r in records if r.get("article") == article]
                ),
                status="archived",
            )
        else:
            data = observed(lambda: wb_finance(dict(period, article=article, limit=100), wires["wb"]))
        sources["wb"] = {
            k: data.get(k)
            for k in (
                "status",
                "complete",
                "issues",
                "unallocated_rows",
                "http_status",
                "retry_after_seconds",
            )
        }
        ad = (
            observed(
                lambda: advertising("wb", period, {"article": article, "nm_id": mapping["wb_nm_id"]}, wires)
            )
            if data.get("items")
            else {}
        )
        sources["wb"]["advertising"] = {
            k: ad.get(k) for k in ("status", "complete", "scope", "http_status")
        }
        for row in data.get("items", []):
            a = row["amounts"]

            def combined(*names):
                v = [money(a.get(k)) for k in names]
                return sum(v) if all(x is not None for x in v) else None

            amounts = dict(
                gross_sales=a.get("gross_sales"),
                commission=a.get("commission_including_vat"),
                logistics=combined("delivery", "rebilled_logistics"),
                storage=a.get("storage"),
                acceptance=a.get("acceptance"),
                retentions=combined("penalty", "deduction"),
                ads=ad.get("spend_by_currency", {}).get(row["currency"]),
                for_pay=a.get("for_pay"),
            )
            items.append(
                unit_row(
                    "wb",
                    article,
                    row["currency"],
                    row["sold_units"],
                    row["returned_units"],
                    amounts,
                    args.get("cost_per_unit"),
                    args.get("currency"),
                )
            )
    if "ozon" in wires:
        article = mapping["ozon_offer_id"]
        data = observed(
            lambda: ozon_finance(
                "ozon_realization_article_month",
                {"offer_id": article, "year": start.year, "month": start.month, "view": "details"},
                wires["ozon"],
            )
        )
        units = data.get("observed_delivery_units")
        legs = [
            r["delivery_commission"]
            for r in data.get("selected_rows", [])
            if r.get("delivery_commission") is not None
        ]

        def total(field):
            values = [money(leg.get(field)) for leg in legs]
            return sum(values) if values and all(v is not None for v in values) else None

        amounts = {
            "for_pay": total("total"),
            "commission": total("standard_fee"),
            "gross_sales": total("amount"),
        }
        selected = data.get("selected_rows", [])
        postings = sorted({r["posting_number"] for r in selected if r.get("posting_number")})
        skus = sorted({r["sku"] for r in selected if r.get("sku")})
        accrual = {}
        if postings and skus and len(postings) <= 200:
            accrual = observed(
                lambda: ozon_finance(
                    "ozon_posting_accruals",
                    dict(period, posting_numbers=postings, skus=skus, view="summary", include_types=True),
                    wires["ozon"],
                )
            )
            lines = [
                r
                for r in accrual.get("observed_category_breakdown", [])
                if r.get("category") in ("logistics", "last_mile")
                and r.get("currency") == data.get("currency")
            ]
            if lines and all(
                r.get("incomplete") is not True and r.get("observed_row_sum") is not None for r in lines
            ):
                amounts["logistics"] = -sum(money(r["observed_row_sum"]) for r in lines)
        ad = observed(lambda: advertising("ozon", period, {"article": article}, wires)) if units else {}
        amounts["ads"] = ad.get("spend_by_currency", {}).get(data.get("currency"))
        row = unit_row(
            "ozon",
            article,
            data.get("currency"),
            units,
            data.get("observed_return_units_signed"),
            amounts,
            args.get("cost_per_unit"),
            args.get("currency"),
        )
        row["basis"] = "realization_month_delivery_leg_only"
        items.append(row)
        sources["ozon"] = {
            k: data.get(k)
            for k in (
                "source",
                "status",
                "matched_rows",
                "financial_completeness",
                "http_status",
                "retry_after_seconds",
            )
        }
        sources["ozon"].update(
            posting_costs=accrual.get("observed_category_breakdown"),
            postings_in_realization=len(postings),
            ads_status=ad.get("status"),
            accrual_status=accrual.get("status"),
        )
    return result(
        items,
        complete=False,
        period=period,
        sources=sources,
        component_basis={
            "wb": "rrDate, allocated detail of realization",
            "ozon": "monthly realization, delivery and return legs separate",
        },
        limitations=[
            "bank_transfers_not_attributed_to_article",
            "advertising_requires_confirmed_product_costs",
            "account_level_charges_not_allocated",
        ],
    )


def _period_metrics(provider, args, page):
    wb = provider == "wb"
    name = "wb_funnel" if wb else "ozon_sku_daily"
    if not page.get("complete") or page.get("issues"):
        return dict(
            complete=False,
            status=page.get("status", "incomplete"),
            next_cursor=page.get("next_cursor"),
            issues=page.get("issues", []),
            http_status=page.get("http_status"),
            retry_after_seconds=page.get("retry_after_seconds"),
            source=name,
        )
    items = [r for r in page["items"] if wb or args["date_from"] <= r["date"] <= args["date_to"]]

    def total(field, *, count=False):
        try:
            values = [money(r.get(field)) for r in items]
        except (ValueError, ArithmeticError):
            return None
        if any(v is not None and (v < 0 or (count and v != v.to_integral_value())) for v in values):
            return None
        return sum(values) if values and all(v is not None for v in values) else None

    orders = total("order_count" if wb else "ordered_units", count=True)
    amount = total("order_sum" if wb else "revenue")
    views = total("open_count" if wb else "hits_view_pdp", count=True)
    carts = total("cart_count" if wb else "hits_tocart_pdp", count=True)
    currencies = {r.get("currency") for r in items}
    return dict(
        complete=all(v is not None for v in (orders, amount, views, carts)),
        source=name,
        views=views,
        carts=carts,
        ordered_units=orders,
        ordered_amount=amount,
        cart_to_view_percent=100 * carts / views if carts is not None and views else None,
        ordered_amount_per_unit=amount / orders if amount is not None and orders else None,
        currency=currencies.pop() if len(currencies) == 1 else None,
        funnel=page.get("funnel"),
        rows=len(items),
        **{k: args[k] for k in ("date_from", "date_to")},
    )


def _ozon_comparison_page(query, wire):
    if (date.fromisoformat(query["date_to"]) - date.fromisoformat(query["date_from"])).days >= 90:
        return result([], complete=False, status="period_exceeds_analytics_window")
    page = observed(lambda: ozon_expansion.read("ozon_sku_daily", query, wire))
    if not page.get("complete") or not page.get("items"):
        return page
    # Every seller can read ordered_units and revenue. Keep that observation
    # when Premium Plus metrics or the next API quota slot are unavailable.
    funnel = _comparison_extra(wire, lambda: ozon_expansion.read("ozon_sku_daily", dict(query, include_funnel=True), wire))
    page["funnel"] = {k: funnel.get(k) for k in ("complete", "status", "http_status", "retry_after_seconds")}
    if funnel.get("complete"):
        positions = {(str(r["sku"]), r["date"]): r for r in funnel["items"]}
        if positions.keys() != {(str(r["sku"]), r["date"]) for r in page["items"]} or any(
            any(positions[str(r["sku"]), r["date"]].get(k) != r.get(k) for k in ("ordered_units", "revenue"))
            for r in page["items"]
        ):
            page["funnel"].update(complete=False, status="inconsistent_analytics_observations")
            return page
        for row in page["items"]:
            extra = positions[str(row["sku"]), row["date"]]
            row.update({k: extra.get(k) for k in ("hits_view_pdp", "hits_tocart_pdp")})
    return page


def _comparison_extra(wire, read):
    deadline = getattr(wire, "deadline", None)
    if deadline is None:
        return observed(read)
    now = time.monotonic()
    if deadline - now <= 3:
        return result([], complete=False, status="request_budget_exhausted")
    try:
        wire.deadline = min(deadline - 1.5, now + 4)
        return observed(read)
    finally:
        wire.deadline = deadline


def verified_comparison(args, wires, seller_mapping, identity_report):
    if identity_report["complete"]:
        return comparison(args, wires, identity_report["mapping"])

    catalogs = identity_report["items"][0]
    verified_wires, verified_ids, unavailable = {}, {}, {}
    for provider, field in (("ozon", "ozon_offer_id"), ("wb", "wb_vendor_code")):
        catalog = catalogs[provider]
        rows = catalog["items"]
        expected = (seller_mapping or {}).get(field, args["article"])
        if not (catalog["complete"] and len(rows) == 1 and rows[0].get("article") == expected
                and (provider != "wb" or rows[0].get("nm_id"))):
            failure = {k: catalog[k] for k in ("http_status", "retry_after_seconds") if k in catalog}
            failure["status"] = catalog.get("status") or "exact_catalog_match_unconfirmed"
            unavailable[provider] = failure
            continue
        verified_wires[provider] = wires[provider]
        verified_ids[field] = expected
        if provider == "wb":
            verified_ids["wb_nm_id"] = rows[0]["nm_id"]
        elif "performance" in wires:
            verified_wires["performance"] = wires["performance"]
    if not verified_wires:
        return identity_report
    data = comparison(args, verified_wires, verified_ids)
    data.update(complete=False, status="partial_catalog_identity", unavailable_marketplaces=unavailable)
    return data


def comparison(args, wires, mapping):
    data = []
    periods_by_market = {}
    for provider in ("ozon", "wb"):
        if provider not in wires:
            continue
        wire = wires[provider]
        identity = (
            {"article": mapping["wb_vendor_code"], "nm_id": mapping["wb_nm_id"]}
            if provider == "wb"
            else {"article": mapping["ozon_offer_id"]}
        )
        queries = [dict(identity, date_from=args[a], date_to=args[b], limit=100)
                   for a,b in (("previous_from","previous_to"),("date_from","date_to"))]
        shared = None
        if provider == "ozon":
            first = min(q["date_from"] for q in queries)
            last = max(q["date_to"] for q in queries)
            if (date.fromisoformat(last) - date.fromisoformat(first)).days < 90:
                shared = _ozon_comparison_page(dict(identity, date_from=first, date_to=last, limit=100), wire)
        periods = []
        for q in queries:
            page = shared
            if page is None:
                page = (_ozon_comparison_page(q, wire) if provider == "ozon"
                        else observed(lambda: wb_expansion.read("wb_funnel", q, wire)))
            periods.append(_period_metrics(provider, q, page))
        periods_by_market[provider] = identity, queries, periods

    for provider, (identity, queries, periods) in periods_by_market.items():
        wire = wires[provider]
        for query, metrics in zip(queries, periods):
            ad = _comparison_extra(wires.get("performance", wire) if provider == "ozon" else wire,
                lambda: advertising(
                    provider, {k: query[k] for k in ("date_from", "date_to")}, identity, wires
                )
            )
            review = _comparison_extra(wire,
                lambda: wb_expansion._reviews(dict(query, report_scope="answered"), wire)
                if provider == "wb"
                else ozon_expansion.read("ozon_reviews", query, wire)
            )
            metrics["ads"] = {k: ad.get(k) for k in ("status", "complete", "spend_by_currency", "task_id", "http_status", "retry_after_seconds")}
            metrics["reviews"] = {
                k: review.get(k)
                for k in ("complete", "mean_rating", "rating_scope", "status", "http_status", "retry_after_seconds")
            }
            if hasattr(wire, "gate_dir"):
                archive = market_history.History(wire.gate_dir)
                hist = archive.verified_days(
                    provider, wire.creds, provider + "_availability", query["date_from"], query["date_to"]
                )
                metrics["stock_days_observed"] = len(hist)
                metrics["stock_by_day"] = [
                    {
                        "date": d,
                        "warehouses": [
                            {
                                k: r.get(k)
                                for k in (
                                    "warehouse",
                                    "warehouse_name",
                                    "warehouse_id",
                                    "cluster_id",
                                    "quantity",
                                    "available_stock_count",
                                )
                            }
                            for r in records
                            if r.get("article") == identity["article"]
                        ],
                    }
                    for d, (_, records) in hist.items()
                ]
            metrics["metrics_complete"] = metrics["complete"]
            metrics["complete"] = bool(metrics["metrics_complete"] and ad.get("complete") and review.get("complete")
                                       and metrics.get("stock_days_observed", 0) ==
                                       (date.fromisoformat(query["date_to"]) - date.fromisoformat(query["date_from"])).days+1)
        changes = {}
        for k in ("views", "carts", "ordered_units", "cart_to_view_percent", "ordered_amount_per_unit"):
            a, b = (r.get(k) for r in periods)
            if k == "ordered_amount_per_unit" and (
                not periods[0].get("currency") or periods[0].get("currency") != periods[1].get("currency")
            ):
                continue
            if a is not None and b is not None:
                changes[k] = {"absolute": b - a, "percent": 100 * (b - a) / a if a else None}
        data.append(
            dict(
                marketplace=provider,
                article=identity["article"],
                previous=periods[0],
                current=periods[1],
                changes=changes,
                historical_card_rating=None,
                rating_basis="reviews_published_in_period_in_returned_sample",
            )
        )
    return result(
        data,
        complete=all(r["previous"].get("complete") and r["current"].get("complete") for r in data),
        basis="calendar_period_order_analytics",
        causal_diagnosis=None,
    )
