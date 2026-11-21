"""Cross-marketplace reports. Identity, monetary basis and unavailable sources stay explicit."""

import calendar
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
import hashlib
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
        return result([], complete=False, status=exc.reason, retry_after_seconds=exc.retry_after_seconds,
                      http_status=None,reason="local_limiter" if exc.reason in ("cooldown","busy","retry_time_unknown") else exc.reason)
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
                    "source": card.get("identity_source", "/v3/product/info/list"),
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


def unit_row(provider, article, currency, units, returned, amounts, cost=None, cost_currency=None, *,
             unreconciled=(), settlement_adjustments=None):
    with localcontext() as ctx:
        ctx.prec = 128
        components = (*COMPONENTS, "last_mile") if "last_mile" in amounts else COMPONENTS
        numeric = {k: money(amounts.get(k)) for k in components}
        denominator = units if type(units) is int and units > 0 and currency else None
        def per_unit(amount, subtract=Decimal(0)):
            if amount is None or not denominator:
                return None
            with localcontext() as display:
                display.clear_flags()
                value = amount / denominator - subtract
                return format(value, ".4f") if display.flags[Inexact] or value.as_tuple().exponent < -4 else str(value)
        rates = {k: per_unit(v) for k, v in numeric.items()}
        cost = money(cost)
        margin = None
        expense_fields = ("logistics", "storage", "acceptance", "ads", "retentions")
        if "last_mile" in numeric:
            expense_fields += ("last_mile",)
        expenses_known = all(numeric[k] is not None and numeric[k] >= 0 for k in expense_fields)
        included = [k for k in expense_fields if numeric[k] is not None and
                    (numeric[k] >= 0 or (provider == "wb" and k == "retentions"))]
        missing = [k for k in expense_fields if k not in included]
        missing.extend(unreconciled)
        signed = {k: money(v) for k, v in (settlement_adjustments or {}).items()}
        missing.extend(k for k, v in signed.items() if v is None)
        known_net = None
        if numeric["for_pay"] is not None and currency and included:
            known_net = numeric["for_pay"] - sum(numeric[k] for k in included) + sum(v for v in signed.values() if v is not None)
        if (
            all(v is not None for v in numeric.values())
            and expenses_known
            and not missing
            and denominator
            and cost is not None
            and cost >= 0
            and currency == cost_currency
        ):
            margin = per_unit(known_net, cost)
        return dict(
            marketplace=provider,
            article=article,
            currency=currency,
            sold_units=units,
            returned_units=returned,
            amounts={k: str(v) if v is not None else None for k, v in numeric.items()},
            per_sold_unit=rates,
            net_after_expenses={"amount": str(known_net) if known_net is not None else None,
                                "per_sold_unit": per_unit(known_net), "currency":currency,
                                "basis":"for_pay_less_attributed_expenses_before_cost_of_goods",
                                "complete":known_net is not None and not missing, "deducted":included,
                                "signed_settlement_adjustments":{k:str(v) if v is not None else None for k,v in signed.items()},
                                "unreconciled":missing, "commission_included_in_for_pay":True},
            bank_payout_per_unit=None,
            margin_per_unit=margin,
            denominator="gross_sold_units_before_returns",
            recurring_unit_rate_decimal_places=4,
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
        selector = {"article": article, "nm_id": mapping["wb_nm_id"]}
        history = market_history.History(wires["wb"].gate_dir)
        finance_creds = getattr(wires["wb"], "category_keys", {}).get("wb_finance", wires["wb"].creds)
        days = history.verified_days(
            "wb", finance_creds, "wb_finance_report", period["date_from"], period["date_to"]
        )
        if len(days) == (end - start).days + 1:
            archived = [r for _, records in days.values() for r in records]
            data = result(
                wb_expansion.finance_summary(
                    [r for r in archived if r.get("article") == article and str(r.get("nm_id")) == str(selector["nm_id"])]
                ),
                status="archived",
                unallocated_rows=sum(not r.get("article") and not r.get("nm_id") for r in archived),
            )
        else:
            data = observed(lambda: wb_finance(dict(period, **selector, limit=100), wires["wb"]))
        sources["wb"] = {
            k: data.get(k)
            for k in (
                "status",
                "complete",
                "issues",
                "unallocated_rows",
                "source",
                "summary_scope",
                "rows_observed",
                "reports_completed",
                "reports_listed",
                "missing_dates",
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
        storage = {}
        if data.get("items") and hasattr(wires["wb"], "progress"):
            import wb_reports
            storage = observed(lambda: wb_reports.article_storage_period(period, selector, wires["wb"]))
        sources["wb"]["storage"] = storage
        if not data.get("items"):
            row = unit_row("wb", article, None, None, None, {})
            row.update(source_period_complete=bool(data.get("complete")),
                       status="no_attributed_financial_rows" if data.get("complete") else data.get("status", "pending"))
            items.append(row)
        for row in data.get("items", []):
            a = row["amounts"]
            gaps = []
            if not data.get("complete"):
                gaps.append("partial_settlement")
            if data.get("unallocated_rows") is None or data["unallocated_rows"] > 0:
                gaps.append("unallocated_settlement_rows")
            ads = money(ad.get("spend_by_currency", {}).get(row["currency"],
                        "0" if ad.get("complete") and ad.get("status") == "no_spend_records" else None))
            penalty, deduction = (money(a.get(k)) for k in ("penalty", "deduction"))
            if ads is not None and ads > 0 and deduction:
                identified = money(a.get("advertising_deduction"))
                if identified is None or deduction != identified:
                    gaps.append("deductions_with_unreconciled_advertising_overlap")
                if identified:
                    gaps.append("advertising_payment_document_unreconciled")
                # The product's advertising cost is already included in ads. Unknown overlaps
                # stay separately visible until the document and payment can be matched.
                deduction = Decimal(0)
            if money(a.get("additional_payment")) != 0:
                gaps.append("unreconciled_remuneration_adjustment")
            if money(a.get("payment_schedule")) != 0:
                gaps.append("unreconciled_payment_schedule")
            points, fee = money(a.get("loyalty_points")), money(a.get("loyalty_fee"))
            amounts = dict(
                gross_sales=a.get("gross_sales"),
                commission=a.get("commission_including_vat"),
                logistics=a.get("delivery"),
                storage=storage.get("amount") if storage.get("complete") and storage.get("currency") == row["currency"] else None,
                acceptance=a.get("acceptance"),
                retentions=penalty + deduction if penalty is not None and deduction is not None else None,
                ads=ads,
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
                    unreconciled=gaps,
                    settlement_adjustments={"loyalty_points":-points if points is not None else None,
                                            "loyalty_fee":-fee if fee is not None else None,
                                            "loyalty_compensation":a.get("loyalty_compensation")},
                )
            )
            items[-1]["source_period_complete"] = bool(data.get("complete"))
            items[-1]["settlement_components"] = {
                "commission_basis":"vw_plus_vwNds_sales_less_returns",
                "sales_commission_before_agent_services_ex_vat":a.get("sales_commission"),
                "service_remuneration_adjustment_including_vat":a.get("service_remuneration_including_vat"),
                "rebilled_logistics_informational":a.get("rebilled_logistics"),
                "storage_attributed_in_settlement":a.get("storage"),
                "additional_payment_unclassified":a.get("additional_payment"),
                "payment_schedule_adjustment":a.get("payment_schedule"),
                "settlement_deductions":a.get("deduction"), "identified_advertising_deductions":a.get("advertising_deduction"),
            }
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
            "last_mile": None,
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
            for category in ("logistics", "last_mile"):
                lines = [r for r in accrual.get("observed_category_breakdown", [])
                         if r.get("category") == category and r.get("currency") == data.get("currency")]
                if lines and not accrual.get("unreturned_posting_numbers") and all(
                    r.get("incomplete") is not True and r.get("observed_row_sum") is not None for r in lines
                ):
                    amounts[category] = -sum(money(r["observed_row_sum"]) for r in lines)
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
            unreconciled=["account_level_charges_not_attributed"] + ([] if data.get("observed_return_rows") == 0 else ["return_leg_reconciliation"]),
        )
        row["basis"] = "realization_month_delivery_leg_only"
        row["reported_return_rows"] = data.get("observed_return_rows")
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
            accrual_http_status=accrual.get("http_status"),
            accrual_data_as_of=accrual.get("data_as_of"), accrual_refresh_status=accrual.get("refresh_status"),
            accrual_refresh_http_status=accrual.get("refresh_http_status"),
            accrual_postings_returned=len(accrual["returned_posting_numbers"]) if "returned_posting_numbers" in accrual else None,
            accrual_date_basis=accrual.get("period_basis"),
        )
        if units is None and data.get("status") in ("retry_time_unknown","cooldown","provider_access_denied","source_unavailable"):
            sources["ozon"]["delivered_postings"] = observed(
                lambda: ozon_expansion.delivered_economics(period, article, wires["ozon"]))
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
    funnel = page.get("funnel")
    if not wb and funnel and funnel.get("complete"):
        rows = [r for r in funnel["items"] if args["date_from"] <= r["date"] <= args["date_to"]]
        for key, field in (("views", "hits_view_pdp"), ("carts", "hits_tocart_pdp")):
            values = [money(r.get(field)) for r in rows]
            value = sum(values) if all(n is not None and n >= 0 and n == n.to_integral_value() for n in values) else None
            if key == "views":
                views = value
            else:
                carts = value
        funnel = {k: v for k, v in funnel.items() if k != "items"}
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
        funnel=funnel,
        rows=len(items),
        **{k: args[k] for k in ("date_from", "date_to")},
    )


def _ozon_comparison_page(query, wire):
    first, last = query["date_from"], query["date_to"]
    dates = market_history.days_between(first, last)
    page = None
    if hasattr(wire,"gate_dir"):
        history=market_history.History(wire.gate_dir)
        pages=history.verified_days("ozon",wire.creds,"ozon_posting_sku_daily",first,last)
        if all(d in pages for d in dates):
            records=[r for d in dates for r in pages[d][1] if r.get("article")==query["article"]]
            page = result([dict(r,ordered_units=r.get("posting_units"),revenue=r.get("posting_item_value")) for r in records],
                          source="ozon_posting_sku_daily", date_basis="posting_creation_utc",
                          amount_basis="posting_item_value", archived_dates=dates)
    if len(dates) > 90:
        return page or result([], complete=False, status="period_exceeds_analytics_window")
    funnel = _comparison_extra(wire, lambda: _ozon_comparison_funnel(query, dates, wire), budget=8)
    if page is None:
        page = dict(funnel, complete=True) if funnel.get("complete") or funnel.get("base_metrics_complete") else observed(
            lambda: ozon_expansion._daily(dict(query, limit=1000), wire))
    page["funnel"] = {k: funnel.get(k) for k in ("complete", "status", "http_status", "retry_after_seconds")}
    page["funnel"].update(funnel.get("funnel", {}))
    page["funnel"].update(source="ozon_sku_daily", date_basis="seller_api_reporting_day",
                          items=funnel.get("items", []) if page["funnel"]["complete"] else [])
    return page


def _ozon_comparison_funnel(query, dates, wire):
    first, last = dates[0], dates[-1]
    history = None
    if hasattr(wire, "gate_dir"):
        history = market_history.History(wire.gate_dir)
        dataset = "ozon_sku_funnel_" + hashlib.sha256(query["article"].encode()).hexdigest()
        pages = history.verified_days("ozon", wire.creds, dataset, first, last)
        if len(pages) == len(dates):
            return result([row for day in dates for row in pages[day][1]], status="archived")
    page = ozon_expansion._daily(dict(query, include_funnel=True, limit=1000), wire)
    if history and page.get("complete"):
        owner = market_history.scope("ozon", wire.creds)
        days = {day: [] for day in dates}
        for row in page["items"]:
            days[row["date"]].append(row)
        stamp = market_history.utc()
        for day, rows in days.items():
            history.save_day(owner, dataset, day, rows, stamp)
        history.confirm(wire.creds, owner)
    return page


def _comparison_extra(wire, read, *, budget=4):
    deadline = getattr(wire, "deadline", None)
    if deadline is None:
        return observed(read)
    now = time.monotonic()
    if deadline - now <= 3:
        return result([], complete=False, status="request_budget_exhausted")
    try:
        wire.deadline = min(deadline - 1.5, now + budget)
        return observed(read)
    finally:
        wire.deadline = deadline


def verified_accounts(args, wires, seller_mapping, identity_report):
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
    return verified_wires, verified_ids, unavailable


def verified_comparison(args, wires, seller_mapping, identity_report):
    if identity_report["complete"]:
        return comparison(args, wires, identity_report["mapping"])
    verified_wires, verified_ids, unavailable = verified_accounts(args, wires, seller_mapping, identity_report)
    anchor = None
    issue = unavailable.get("ozon", {})
    retryable = (issue.get("status") in ("busy", "cooldown", "retry_time_unknown", "source_unavailable", "pending")
                 or issue.get("http_status") in (429, 500, 502, 503, 504))
    ozon = wires.get("ozon")
    if retryable and seller_mapping and "mapping" not in args and getattr(ozon, "gate_dir", None):
        offer = seller_mapping.get("ozon_offer_id")
        archive = market_history.History(ozon.gate_dir)
        for first, last in (("previous_from", "previous_to"), ("date_from", "date_to")):
            days = archive.verified_days("ozon", ozon.creds, "ozon_posting_sku_daily", args[first], args[last])
            if offer and any(r.get("article") == r.get("offer_id") == offer and r.get("sku")
                             for _, records in days.values() for r in records):
                anchor = "seller_mapping_and_authenticated_posting_article_offer_id_sku"
                verified_wires["ozon"], verified_ids["ozon_offer_id"] = ozon, offer
                if "performance" in wires:verified_wires["performance"] = wires["performance"]
                unavailable.pop("ozon")
                break
    if not verified_wires:
        return result([], complete=False, status="unverified_mapping", unavailable_marketplaces=unavailable,
                      catalog_matches=identity_report["items"], basis="calendar_period_order_analytics",
                      causal_diagnosis={"observations": [], "status": "insufficient_comparable_data"})
    data = comparison(args, verified_wires, verified_ids)
    if unavailable:
        data.update(complete=False, status="partial_catalog_identity", unavailable_marketplaces=unavailable)
    if anchor:
        data["historical_identity_sources"] = {"ozon": anchor}
    return data


def comparison(args, wires, mapping):
    import market_archive
    import supply_plan

    data = []
    periods_by_market = {}
    wholesale_pending = False
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
        wholesale = {}
        wholesale_read = {}
        if provider == "ozon":
            first = min(q["date_from"] for q in queries)
            last = max(q["date_to"] for q in queries)
            if (date.fromisoformat(last) - date.fromisoformat(first)).days < 90:
                shared = _ozon_comparison_page(dict(identity, date_from=first, date_to=last, limit=100), wire)
            if hasattr(wire, "progress") and hasattr(wire, "gate_dir"):
                wholesale_read = _comparison_extra(
                    wire, lambda: market_archive.collect_fbo_order_range(wire, first, last), budget=15)
        periods = []
        for q in queries:
            page = shared
            if page is None:
                if provider=="ozon":page=_ozon_comparison_page(q,wire)
                else:
                    page=None
                    if hasattr(wire,"gate_dir"):
                        db=market_history.History(wire.gate_dir)
                        daily=db.verified_days("wb",wire.creds,"wb_funnel_daily",q["date_from"],q["date_to"])
                        per_article=db.verified_days("wb",wire.creds,"wb_funnel_nm_"+str(identity["nm_id"]),q["date_from"],q["date_to"])
                        daily={**per_article,**daily}
                        days=market_history.days_between(q["date_from"],q["date_to"])
                        if all(d in daily for d in days):
                            records=[r for d in days for r in daily[d][1] if str(r.get("nm_id"))==str(identity["nm_id"])]
                            page=result(records,source="wb_funnel_daily",archived_dates=days)
                        elif time.monotonic()+5<wire.deadline:
                            import seller_views
                            account=dict(wire.creds,category_keys=getattr(wire,"category_keys",{}))
                            query={k:q[k] for k in ("date_from","date_to","nm_id","article") if k in q}
                            recent=observed(lambda:seller_views.execute("wb_funnel_history",
                                dict(query,mode="details",limit=100),account,wire.gate_dir,min(wire.deadline-2,time.monotonic()+3)))
                            if recent.get("complete"):
                                page=result(recent["items"],source="wb_funnel_daily",archived_dates=days)
                    if page is None:page=observed(lambda:wb_expansion.read("wb_funnel",q,wire))
            metrics=_period_metrics(provider,q,page)
            if page.get("source") in ("ozon_posting_sku_daily","wb_funnel_daily"):
                metrics.update(source=page["source"],date_basis=page.get("date_basis","order_date_moscow"),
                               archived_days=len(page["archived_dates"]),amount_basis=page.get("amount_basis"))
                if not metrics.get("rows") and page.get("complete"):
                    metrics.update(ordered_units=0,ordered_amount=0)
            if provider == "ozon":
                key = q["date_from"], q["date_to"]
                if key not in wholesale:
                    wholesale[key] = supply_plan.ozon_wholesale(wire, identity["article"], *key)
                evidence = wholesale[key]
                gross = metrics.get("ordered_units")
                covered = metrics.get("source") == "ozon_posting_sku_daily" and gross is not None and evidence is not None
                if covered and evidence["gross_fbo_units"] <= gross and evidence["units"] <= evidence["gross_fbo_units"]:
                    metrics.update(ordered_units_gross=gross, wholesale_units=evidence["units"],
                                   ordered_units_excluding_wholesale=gross - evidence["units"],
                                   wholesale={"status": "verified_fbo_order_groups", **evidence})
                else:
                    metrics.update(ordered_units_gross=gross, wholesale_units=None, ordered_units_excluding_wholesale=None,
                                   wholesale={"status": "inconsistent_fbo_order_scope" if covered else
                                              wholesale_read.get("status", "fbo_order_coverage_unavailable"),
                                              "source": "/v3/posting/fbo/list"})
                    wholesale_pending |= wholesale_read.get("status") in ("pending", "cooldown", "busy")
            periods.append(metrics)
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
                        "observed_at": stamp,
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
                    for d, (stamp, records) in hist.items()
                ]
                matched_days = []
                empty_days = []
                for d, (_, records) in hist.items():
                    selected = [p.get("available_stock_count", p.get("quantity")) for p in records
                                if p.get("article") == identity["article"] and p.get("kind", "warehouse") == "warehouse"]
                    if selected and all(type(v) is int and v >= 0 for v in selected):
                        matched_days.append(d)
                        if not any(selected):
                            empty_days.append(d)
                metrics["stock_evidence"] = {"source": provider + "_availability", "basis": "observed_snapshots",
                                             "observed_days": len(matched_days), "zero_stock_days": len(empty_days),
                                             "zero_stock_dates": empty_days}
            metrics["metrics_complete"] = metrics["complete"]
            metrics["complete"] = bool(metrics["metrics_complete"] and ad.get("complete") and review.get("complete")
                                       and metrics.get("stock_days_observed", 0) ==
                                       (date.fromisoformat(query["date_to"]) - date.fromisoformat(query["date_from"])).days+1)
        changes = {}
        for k in ("views", "carts", "ordered_units", "ordered_units_excluding_wholesale", "cart_to_view_percent",
                  "ordered_amount_per_unit"):
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
    hypotheses=[]
    for market in data:
        demand = market["changes"].get("ordered_units_excluding_wholesale") or market["changes"].get("ordered_units", {})
        change = demand.get("absolute")
        if change is None:continue
        if change<0:
            views=market["changes"].get("views",{}).get("absolute")
            reason="lower_card_traffic" if views is not None and views<0 else "fewer_ordered_units"
            hypotheses.append({"marketplace":market["marketplace"],"hypothesis":reason,"ordered_units_change":change,
                "views_change":views,"evidence":[market["previous"]["source"],market["current"]["source"]],
                "certainty":"observed_change_cause_unconfirmed"})
            previous, current = market["previous"], market["current"]
            price = market["changes"].get("ordered_amount_per_unit", {})
            if price.get("absolute", 0) > 0:
                hypotheses.append(dict(marketplace=market["marketplace"], hypothesis="higher_order_value_per_unit",
                    previous=previous["ordered_amount_per_unit"], current=current["ordered_amount_per_unit"],
                    percent=price["percent"], currency=current["currency"], source=current["source"],
                    certainty="observed_change_cause_unconfirmed"))
            ad_before, ad_after = previous["ads"], current["ads"]
            if ad_before["complete"] and ad_after["complete"]:
                older, newer = ad_before["spend_by_currency"] or {}, ad_after["spend_by_currency"] or {}
                for currency in sorted(older.keys() & newer.keys()):
                    a, b = money(older[currency]), money(newer[currency])
                    if a is not None and b is not None and a != b:
                        hypotheses.append(dict(marketplace=market["marketplace"], hypothesis="changed_ad_spend",
                            previous=a, current=b, currency=currency, percent=100 * (b - a) / a if a else None,
                            certainty="observed_change_cause_unconfirmed"))
            before, after = previous.get("stock_evidence") or {}, current.get("stock_evidence") or {}
            n_a, n_b = before.get("observed_days", 0), after.get("observed_days", 0)
            if n_a and n_b and after["zero_stock_days"] * n_a > before["zero_stock_days"] * n_b:
                hypotheses.append(dict(marketplace=market["marketplace"], hypothesis="more_zero_stock_snapshots",
                    previous_zero_days=before["zero_stock_days"], previous_observed_days=n_a,
                    current_zero_days=after["zero_stock_days"], current_observed_days=n_b,
                    dates=after["zero_stock_dates"], source=after["source"],
                    certainty="observed_snapshot_change_cause_unconfirmed"))
        else:hypotheses.append({"marketplace":market["marketplace"],"observation":"no_decrease_in_ordered_units","ordered_units_change":change})
    return result(
        data,
        complete=all(r["previous"].get("complete") and r["current"].get("complete") for r in data),
        **({"status": "pending"} if wholesale_pending else {}),
        basis="calendar_period_order_analytics",
        causal_diagnosis={"observations":hypotheses,"status":"reported" if hypotheses else "insufficient_comparable_data"},
    )
