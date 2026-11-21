"""Join verified seller reports without changing their time, stock or payment basis."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import time

import market_composites
import market_history
import seller_views


def snapshot(page):
    return {key: page[key] for key in (
        "status", "complete", "source_complete", "source_issues", "observed_at", "data_as_of",
        "task_id", "retry_after_seconds", "next_cursor", "period_totals", "items", "missing_dates",
        "warehouse_scope", "closing_observed_stock", "stockout_days", "stockout_coverage_days", "match_status"
    ) if key in page}


def display_unit_amounts(row):
    """Format financial fields after reconciliation, preserving the seller's identifiers."""
    net = row.get("net_after_expenses") or {}
    compensation = row.get("compensation_income") or {}
    fields = ((row.get("amounts") or {}, None), (row.get("per_sold_unit") or {}, None),
              (net.get("signed_settlement_adjustments") or {}, None),
              (row.get("settlement_components") or {}, ("service_remuneration_adjustment_including_vat",)),
              (net, ("amount", "per_sold_unit", "amount_before_compensations", "per_sold_unit_before_compensations")),
              (compensation, ("confirmed_adjustment",)), (row, ("margin_per_unit", "bank_payout_per_unit")))
    for record, names in fields:
        for field in record if names is None else names:
            value = record.get(field)
            if isinstance(value, (str, Decimal)) and Decimal(value).as_tuple().exponent < -4:
                record[field] = seller_views.num(value)


def _account_expense_estimate(row, page, period):
    from market_specs import money

    first, last = seller_views.period(period)
    currency, units = row.get("currency"), row.get("sold_units")
    net = row.get("net_after_expenses") or {}
    estimate = dict(status="estimate_unavailable", currency=currency, date_from=first.isoformat(),
                    date_to=last.isoformat(), source="/v1/finance/balance", estimate=True,
                    allocation_basis="article_gross_realization_sales_share_of_account_gross_sales",
                    amount=None, per_sold_unit=None)
    row["net_after_account_expenses_estimate"] = estimate
    if page.get("source_complete") is not True or page.get("next_cursor") is not None:
        estimate["reason"] = page.get("status") or "account_balance_incomplete"
        return
    gross = money(row["amounts"].get("gross_sales"))
    net_amount = money(net.get("amount"))
    if not currency or not gross or gross < 0 or net_amount is None or not units:
        estimate["reason"] = "article_settlement_incomplete"
        return
    windows = {(a.isoformat(), b.isoformat()) for a, b in seller_views.spans(first, last, 30)}
    sales = {}
    names = {"cross_docking", "promotion_with_cost_per_order", "pay_per_click"}
    skipped = set()
    if "ads" in net.get("deducted", []):
        skipped = {"promotion_with_cost_per_order", "pay_per_click"}
    costs = {}
    for item in page.get("items", []):
        kind, name = item.get("kind"), item.get("name")
        if kind != "sales" and (kind != "service" or name not in names - skipped):
            continue
        window = item.get("date_from"), item.get("date_to")
        value = money(item.get("amount"))
        if window not in windows or item.get("currency") != currency or value is None:
            estimate["reason"] = "account_currency_amount_or_period_unconfirmed"
            return
        if kind == "sales":
            if window in sales or value < 0:
                estimate["reason"] = "account_sales_not_additive"
                return
            sales[window] = value
        else:
            costs[name] = costs.get(name, Decimal(0)) - value
    account_gross = sum(sales.values())
    if sales.keys() != windows or gross > account_gross or not costs or any(v < 0 for v in costs.values()):
        estimate["reason"] = "account_sales_or_expenses_unconfirmed"
        return
    share = gross / account_gross
    allocated = {name: value * share for name, value in costs.items()}
    amount = net_amount - sum(allocated.values())
    estimate.update(status="estimated", amount=seller_views.num(amount), per_sold_unit=seller_views.num(amount / units),
                    article_gross_sales=seller_views.num(gross), account_gross_sales=seller_views.num(account_gross),
                    share_percent=seller_views.num(share * 100), article_net_before_allocation=seller_views.num(net_amount),
                    account_costs={k: seller_views.num(v) for k, v in costs.items()},
                    allocated_costs={k: seller_views.num(v) for k, v in allocated.items()},
                    excluded_due_to_possible_overlap=sorted(skipped),
                    note="Оценка по указанным расходам кабинета. Остальные неподтверждённые расходы остаются в исходном расчёте.")


def _compensation_adjustment(row, report, ledger):
    net=row["net_after_expenses"]
    info=row["compensation_income"]
    if "amount_before_compensations" in net:
        net["amount"]=net["amount_before_compensations"]
        net["per_sold_unit"]=net["per_sold_unit_before_compensations"]
        net.get("signed_settlement_adjustments",{}).pop("item_compensations",None)
    info.pop("confirmed_adjustment",None)
    info["included_in_net"]=False
    entries=report.get("items") or []
    if report.get("complete") and not entries:
        info.update(reconciliation="complete_exports_no_matching_lines",confirmed_adjustment="0")
        return
    if not report.get("complete") or not ledger or not net.get("amount") or not row.get("currency"):
        return
    if not all(e.get("operation_id") and e.get("sku") and e.get("date") and e.get("amount") is not None
               and e.get("currency")==row["currency"] for e in entries):
        return
    types=ledger("ozon_accrual_types",{}).get("types") or []
    fee_ids={t["id"] for t in types if any(term in ((t.get("name") or "")+" "+(t.get("description") or "")).casefold()
                                             for term in ("компенсац","возмещен","compensation","reimbursement"))}
    matched={}
    for day in sorted({e["date"] for e in entries}):
        expected=[e for e in entries if e["date"]==day]
        args={"date":day,"skus":sorted({str(e["sku"]) for e in expected}),"view":"details"}
        source=ledger("ozon_accrual_day",args)
        cursors=set()
        while True:
            if (not isinstance(source.get("matching_rows"),list) or source.get("date")!=day or
                source.get("requested_cursor")!=args.get("last_id","") or not isinstance(source.get("next_cursor"),str)):
                return
            for line in source.get("matching_rows") or []:
                if line.get("category")!="ITEM":continue
                for e in expected:
                    key=(str(e["operation_id"]),str(e["sku"]))
                    if key!=(str(line.get("accrual_id")),str(line.get("sku"))):continue
                    amounts=[f["accrued"]["amount"] for f in line.get("fees") or [] if f.get("type_id") in fee_ids
                             and (f.get("accrued") or {}).get("currency")==row["currency"]]
                    if len(amounts)!=1 or amounts[0] is None or key in matched:return
                    value=Decimal(str(amounts[0]))
                    documented=Decimal(e["amount"])
                    if e["kind"]=="COMPENSATION_REPORT" and documented<0:return
                    if abs(value)!=abs(documented) or (value<0)!=(e["kind"]=="DECOMPENSATION_REPORT"):return
                    matched[key]=value
            marker=source.get("next_cursor")
            if not marker:break
            if marker in cursors:return
            cursors.add(marker)
            args["last_id"]=marker
            source=ledger("ozon_accrual_day",args)
    if len(matched)!=len(entries):return
    adjustment=sum(matched.values(),Decimal(0))
    revised=Decimal(net["amount"])+adjustment
    info.update(included_in_net=True,confirmed_adjustment=str(adjustment),reconciliation="exact_ITEM_accrual_id_sku_day_currency_signed_fee")
    net["amount_before_compensations"]=net["amount"]
    net["per_sold_unit_before_compensations"]=net.get("per_sold_unit")
    net["amount"]=str(revised)
    if row.get("sold_units"):
        net["per_sold_unit"]=seller_views.num(revised/row["sold_units"])
    net.setdefault("signed_settlement_adjustments",{})["item_compensations"]=str(adjustment)


def _units(row, read, period, ledger=None):
    if row["marketplace"] != "ozon":
        row["return_tariff_basis"] = "seller_stock_withdrawal_tariff_is_separate_from_actual_customer_return_cost"
        _,last=seller_views.period(period)
        row["seller_return_tariff"]=snapshot(read("wb_tariffs",{"date":last.isoformat(),"kind":"return","limit":5}))
        return
    buyouts = read("ozon_buyout", period)
    row["ozon_own_purchases"] = dict(snapshot(buyouts), retail_realization_units=row.get("sold_units"),
        basis="UPD purchases by Ozon; retail gross sold units come from monthly realization")
    compensation = read("ozon_compensations", period)
    row["compensation_income"] = dict(snapshot(compensation),
        included_in_net=False, reconciliation="match_each_operation_to_existing_accruals_before_adding")
    net=row["net_after_expenses"]
    net["complete"]=net.setdefault("complete_before_compensations",net.get("complete",False))
    if "compensation_reconciliation" in net.get("unreconciled",[]):
        net["unreconciled"].remove("compensation_reconciliation")
    _compensation_adjustment(row,compensation,ledger)
    if row["compensation_income"].get("confirmed_adjustment") is None:
        net=row["net_after_expenses"]
        net["complete"]=False
        if "compensation_reconciliation" not in net.setdefault("unreconciled",[]):
            net["unreconciled"].append("compensation_reconciliation")
    volume = read("ozon_wrong_volume", {})
    row["wrong_volume"] = True if volume.get("items") else False if volume.get("complete") else None
    row["wrong_volume_source"] = snapshot(volume)
    first,last = seller_views.period(period)
    removals = read("ozon_removals", dict(date_from=first.isoformat(),date_to=last.isoformat()))
    row["removal_losses"] = dict(snapshot(removals), included_in_net=False,
        basis="disposal fees and reimbursements require matching to the settlement ledger")


def _fbs_orders(wire, article, end, horizon, stock, args):
    end = date.fromisoformat(end)
    first = end - timedelta(days=89)
    dates = market_history.History(wire.gate_dir).verified_days(
        "ozon", wire.creds, "ozon_posting_sku_daily", first.isoformat(), end.isoformat())
    complete = len(dates) == 90
    units = sum(r["posting_units"] for _, rows in dates.values() for r in rows
                if r.get("scheme") == "FBS" and r.get("offer_id") == article)
    recommendation = None
    if complete and stock is not None:
        needed = Decimal(units) / 90 * (horizon + args.get("lead_time_days", 0))
        needed *= 1 + Decimal(str(args.get("safety_stock_percent", 0))) / 100
        target = max(0, int(needed.to_integral_value(rounding=ROUND_CEILING)) - stock)
        recommendation = target if target >= args.get("min_batch_units", 2) else 0
    return dict(ordered_units=units if complete else None, observed_days=len(dates), date_from=first.isoformat(),
                date_to=end.isoformat(), recommended_quantity=recommendation, date_basis="in_process_at_utc",
                source="ozon_posting_sku_daily", recommendation_basis="90_day_gross_FBS_demand_less_free_seller_stock")


def _slots(row, wire, read, deadline):
    import ozon_expansion
    if time.monotonic() + 2 >= deadline:
        return {"status":"pending", "source":"eligible_supply_orders"}
    orders = market_composites.observed(lambda: ozon_expansion._orders(
        {"states":["READY_TO_SUPPLY"], "limit":20}, wire))
    slots = []
    for order in orders.get("items", []):
        if time.monotonic() + 2 >= deadline:
            return dict(status="partial", items=slots)
        page = read("ozon_supply_slots", {"order_id":str(order["order_id"])})
        dates = sorted(str(s["from"]) for s in page.get("items", []) if s.get("scope") == "supply_order" and s.get("from"))
        if not dates:
            continue
        # The slot belongs to the drop-off warehouse specified in this order.
        warehouse = order.get("dropoff_warehouse_id")
        entry = dict(order_id=order["order_id"], warehouse_id=warehouse, nearest_slot=dates[0])
        slots.append(entry)
        by_warehouse = {str(loc.get("warehouse_id")):loc.get("cluster_id") for loc in row.get("locations", [])}
        cluster = by_warehouse.get(str(warehouse)) if warehouse else None
        for dest in row.get("clusters", []):
            if cluster and str(dest.get("cluster_id")) == str(cluster):
                dest["nearest_supply_slot"] = min(dest.get("nearest_supply_slot") or dates[0], dates[0])
    return dict(status="observed" if slots else "no_slots_observed" if orders.get("complete") else "partial",
                items=slots, order_list_complete=orders.get("complete", False))


def _plan(row, read, wire, args, deadline):
    through = row.get("orders_through")
    if not through:
        return
    if row["marketplace"] == "wb":
        first = (date.fromisoformat(through)-timedelta(days=29)).isoformat()
        current = read("wb_stocks_history", {"group_by":"warehouse"})
        history = read("wb_stocks_history", {"date_from":first,"date_to":through,"group_by":"warehouse"})
        row["warehouse_inventory"] = snapshot(current)
        row["warehouse_availability_history"] = snapshot(history)
        row["transit_tariffs"]=snapshot(read("wb_tariffs",{"kind":"transit","limit":10}))
        return
    stock = read("ozon_fbs_stocks", {})
    positions = stock.get("items", [])
    complete = stock.get("complete") and all(type(p.get("free_stock")) is int and p["free_stock"] >= 0 for p in positions)
    available = sum(p["free_stock"] for p in positions) if complete else None
    row["seller_fulfillment"] = dict(stock=snapshot(stock), free_stock=available, scheme="FBS",
        stock_status="reported" if complete else stock.get("status", "stock_unconfirmed"),
        demand=_fbs_orders(wire, row["article"], through, args.get("horizon_days", 14), available, args))
    if complete:
        row["account_coverage"] = dict(status="partial", excluded=["rFBS"], included=["FBO", "FBS_stock"],
            fbs_demand_covered=row["seller_fulfillment"]["demand"]["ordered_units"] is not None)
    first=(date.fromisoformat(through)-timedelta(days=29)).isoformat()
    removals=read("ozon_removals",dict(date_from=first,date_to=through))
    row["removal_adjustments"] = dict(snapshot(removals),basis="completed_disposals_already_in_sellable_stock",
        pending_unconfirmed_stock_adjustment=None, deducted_again=0)
    row["supply_slots"] = _slots(row, wire, read, deadline)


def _avito(name, args, mapping, creds, directory, deadline):
    import avito_mcp
    if not creds:
        return {"status":"not_configured"}
    def get(op, query):
        return market_composites.observed(lambda:avito_mcp.read(op,query,creds,directory,deadline))
    item = (mapping or {}).get("avito_item_id")
    if not item:
        article=(mapping or {}).get("avito_article") or args["article"]
        lookup = get("avito_get_avito_ids_by_ad_ids", {"query":article})
        pairs = (lookup.get("data") or {}).get("items", [])
        matches = {p.get("avito_id",p.get("avitoId")) for p in pairs if isinstance(p,dict) and
                   str(p.get("ad_id",p.get("adId"))) == article}
        if len(matches) == 1:
            item = matches.pop()
    if not item:
        return {"status":"article_mapping_unresolved"}
    page = get("avito_get_item_info", {"item_id":int(item)})
    card = page.get("data") or {}
    published_article=card.get("autoload_item_id")
    if (not card.get("status") or ("id" in card and str(card["id"])!=str(item)) or
        published_article and (mapping or {}).get("avito_article") and published_article!=mapping["avito_article"]):
        return {"status":page.get("status","item_identity_unconfirmed")}
    data = dict(status="confirmed", item_id=item, article=published_article or (mapping or {}).get("avito_article") or args["article"],
                source="authenticated_seller_and_item_path_avito_get_item_info")
    if name == "sales_drop_check":
        for key, prefix in (("previous","previous"),("current","current")):
            start,end=(args.get("date_from"),args.get("date_to")) if key=="current" else (args.get(prefix+"_from"),args.get(prefix+"_to"))
            if start and end:
                data[key]=get("avito_stats", dict(item_ids=[int(item)],date_from=start,date_to=end,version="v1"))
    if name == "unit_economics":
        first,last=seller_views.period(args)
        dates=dict(dateFrom=first.isoformat(),dateTo=last.isoformat())
        spending_metrics={"promotion":"promoSpending","presence":"presenceSpending","commission":"commission","rest":"restSpending"}
        query=dict(dates,grouping="item",metrics=["deliveredItems","deliveredItemsPrice",*spending_metrics.values()],limit=100,offset=0)
        metrics={}
        while time.monotonic()+1<deadline:
            page=get("avito_item_analytics",query)
            groups=((page.get("data") or {}).get("result") or {}).get("groupings") or []
            matches=[g for g in groups if isinstance(g,dict) and str(g.get("id"))==str(item)
                     and g.get("type") in (None,"item")]
            if len(matches)==1:
                lines=matches[0].get("metrics") or []
                for slug in query["metrics"]:
                    values=[m.get("value") for m in lines if isinstance(m,dict) and m.get("slug")==slug]
                    if len(values)!=1 or values[0] is None:
                        continue
                    try:
                        value=Decimal(str(values[0]))
                    except InvalidOperation:
                        continue
                    if value.is_finite():
                        metrics[slug]=value
                break
            if len(matches)>1:break
            nxt=(page.get("pagination") or {}).get("next")
            if type(nxt) is not int or nxt<=query["offset"]:break
            query["offset"]=nxt
        expenses={k:metrics[m]/100 for k,m in spending_metrics.items() if m in metrics}
        quantity=metrics.get("deliveredItems")
        delivered=metrics["deliveredItemsPrice"]/100 if "deliveredItemsPrice" in metrics else None
        units=int(quantity) if quantity is not None and quantity>=0 and quantity==quantity.to_integral_value() else None
        components=("promotion","presence","rest")
        net=delivered-sum(expenses[k] for k in components) if delivered is not None and all(k in expenses for k in components) else None
        data.update(sold_units=units,delivered_payout=str(delivered) if delivered is not None else None,
            expenses={k:str(v) for k,v in expenses.items()},currency="RUB",status="reported" if net is not None and units is not None else "partial_settlement",
            net_after_expenses={"amount":str(net) if net is not None else None,"per_sold_unit":seller_views.num(net/units) if net is not None and units else None},
            source="avito_item_analytics",source_unit="kopeck",
            basis="deliveredItemsPrice_after_commission_less_item_attributed_promotion_presence_rest")
    return data


def enrich(name,args,data,wires,mapping,creds,directory,deadline):
    if "avito" in creds and name in ("article_map","unit_economics","sales_drop_check"):
        data["avito"] = market_composites.observed(lambda:_avito(name,args,mapping,creds["avito"],directory,
            min(deadline-1,time.monotonic()+6)))
        if data["avito"].get("status") == "confirmed" and name == "article_map" and data.get("mapping"):
            data["mapping"]["avito_item_id"] = data["avito"]["item_id"]
    if name == "article_map":
        return
    for row in data.get("items", []):
        platform = row["marketplace"]
        wire = wires.get(platform)
        if not row.get("article") or not getattr(wire,"gate_dir",None):
            continue
        selector = {"article":row["article"]}
        if platform == "wb" and (mapping or {}).get("wb_nm_id"):
            selector["nm_id"]=mapping["wb_nm_id"]
        def read(tool, query, *, budget=4):
            if time.monotonic() + 1 >= deadline:
                return dict(status="pending", complete=False, source=tool, reason="request_deadline")
            keys=seller_views.VIEWS[tool][1].split()
            product={k:v for k,v in selector.items() if k in keys}
            return market_composites.observed(lambda:seller_views.execute(tool, dict(product, **query),
                creds[platform], directory, min(deadline-0.5,time.monotonic()+budget)))
        if name == "unit_economics":
            import expansion_gateway
            def ledger(tool, query):
                if time.monotonic()+2>=deadline:return {}
                return market_composites.observed(lambda:expansion_gateway.regulated_ozon(tool,query,wire))
            period = {k: args[k] for k in ("month", "date_from", "date_to") if k in args}
            if platform == "ozon":
                balance = read("ozon_balance", dict(period, mode="details", limit=100), budget=12)
            _units(row, read, period, ledger)
            if platform == "ozon":
                _account_expense_estimate(row, balance, period)
        elif name == "supply_plan":
            _plan(row, read, wire, args, deadline)
        elif name == "sales_drop_check":
            tools = (("wb_card_issues","wb_promotions") if platform == "wb" else
                     ("ozon_card_quality","ozon_promotions","ozon_competitor_prices"))
            row["current_diagnostics"] = {tool:snapshot(read(tool, {})) for tool in tools}
            row["diagnostic_time_basis"] = "current_observation; historical causation unconfirmed"
