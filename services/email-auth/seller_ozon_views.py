"""Ozon seller-facing projections with supplier, date and financial scope preserved."""
from datetime import date, timedelta
from decimal import Decimal
import ozon_details
import ozon_expansion
from seller_views import SourceIssue, num, spans

FBO="ozon_fbo_reports"
PAY="ozon_settlement_reports"
PROMO="ozon_price_and_promotion_reports"
CARD="ozon_product_reports"
FBS="ozon_fbs_reports"
INFO="ozon_seller_information"


def _identity(r):
    if not r.article and not r.ids:return None
    if r.article:
        card=ozon_expansion._article_card(r.article,r.wire)
    else:
        sku=next(iter(r.ids))
        raw=r.wire("POST",ozon_expansion.BASE+"/v3/product/info/list",body={"sku":[sku]},ttl=1800)
        exact=ozon_details._products({"skus":[sku]},raw,None)
        card=exact["items"][0] if len(exact["items"])==1 and not exact["conflicts"] else None
    if not card:raise SourceIssue(status="product_identity_unavailable",source="/v3/product/info/list")
    ids=set(map(str,card["sku_candidates"]))
    if r.ids and not r.ids<=ids:raise SourceIssue(status="sku_article_mismatch",source="/v3/product/info/list")
    r.ids=r.ids or ids
    r.article=card["offer_id"]
    r.products.update({sku:r.article for sku in r.ids})
    return card


def _times(r):
    return r.first.isoformat()+"T00:00:00Z",r.last.isoformat()+"T23:59:59.999999Z"


def _money(value):
    from seller_views import amount
    if isinstance(value,dict):
        raw=value.get("value",value.get("amount",value.get("price")))
        return {"amount":amount(raw) if raw is not None else None,"currency":value.get("currency_code",value.get("currency"))}
    return {"amount":amount(value) if value is not None else None,"currency":None}


def _compensations(r):
    import seller_workbooks
    for month in sorted({(r.first+timedelta(days=n)).strftime("%Y-%m") for n in range((r.last-r.first).days+1)}):
        for kind in ("compensations","decompensations"):
            def collect():
                typ="COMPENSATION_REPORT" if kind=="compensations" else "DECOMPENSATION_REPORT"
                reports=r.pages(PAY,kind,{"body":{"page":1,"page_size":100,"report_type":typ}},
                                "result.reports",("body","page",None,100))
                yield from seller_workbooks.settlements(r,reports,month,typ)
            issue_start=len(r.issues)
            yield from r.section(kind,collect)
            for issue in r.issues[issue_start:]:
                if issue.get("status") in ("no_confirmed_export_for_month","export_not_ready"):
                    issue["guidance"]={
                        "where":"Ozon Seller → Финансы → Документы → Компенсации и прочие начисления",
                        "what":"Выберите указанный месяц и найдите отчёт о компенсациях или декомпенсациях; проверьте, готов ли он. Если документа нет, уточните его доступность у Ozon.",
                        "retry":"После появления готового отчёта повторите этот запрос; отсутствие выгрузки не означает отсутствие начислений.",
                        "source":"https://seller.ozon.ru/media/news/fbs-o-novyh-statusah-kompensacij/",
                    }


def _buyouts(r):
    import re
    import time
    import urllib.error
    import finance_gate
    import ozon_mcp

    def currency(*values):
        present=[v for v in values if v is not None and v!=""]
        if any(not isinstance(v,str) or not re.fullmatch(r"[A-Z]{3}",v) for v in present):
            raise ValueError("Invalid buyout currency")
        if len(set(present))>1:raise ValueError("Conflicting buyout currency")
        creds=getattr(r.wire,"creds",{})
        if isinstance(creds,dict) and any(secret and secret in value for value in present
                                         for secret in creds.values() if isinstance(secret,str)):
            raise ValueError("Unsafe buyout currency")
        return present[0] if present else None

    r.meta.update(unit_basis="products_purchased_by_ozon_under_upd",currency_basis="explicit_report_currency_only")
    missing_currency=False
    for a,b in spans(r.first,r.last,31):
        for item in r.fetch(PAY,"buyouts",{"body":{"date_from":a.isoformat(),"date_to":b.isoformat()}})["products"]:
            total=_money(item.get("amount"))
            price=_money(item.get("buyout_price"))
            common=currency(item.get("currency_code"),item.get("currency"))
            total_currency=currency(common,total["currency"])
            price_currency=currency(common,price["currency"])
            selected=r.product({"posting_number":item.get("posting_number"),"quantity":int(item["quantity"]),
                                "amount":total["amount"],"currency":total_currency,
                                "buyout_price":price["amount"],"buyout_price_currency":price_currency},
                               item.get("offer_id"),item.get("sku"))
            if selected and total["amount"] is not None and not total_currency:missing_currency=True
            yield from selected
    if missing_currency:
        r.meta["currency_note"]="API выкупов не указал валюту для части сумм; суммы с неизвестной валютой не складываются. Валюта компании, если доступна, приведена отдельно и не подтверждает валюту отчёта."
        r.meta["company_currency_status"]="unavailable"
        original_deadline=getattr(r.wire,"deadline",None)
        now=time.monotonic()
        if isinstance(original_deadline,(int,float)) and now+3<min(original_deadline,r.deadline):
            try:
                r.wire.deadline=min(original_deadline-1.5,r.deadline-1.5,now+3)
                profile=r.wire("POST",ozon_expansion.BASE+"/v1/seller/info",body={},ttl=1800)
                creds=getattr(r.wire,"creds",{})
                value=ozon_mcp.project_company_currency(profile,creds if isinstance(creds,dict) else {})
                if value:
                    r.meta.update(company_currency=value,company_currency_status="provided_unverified_for_buyout",
                                  company_currency_source="/v1/seller/info company.currency")
            except urllib.error.HTTPError as exc:
                exc.close()
            except (SourceIssue,finance_gate.GateError,finance_gate.ProviderResponseTooLarge,
                    TimeoutError,urllib.error.URLError,OSError,KeyError,ValueError,TypeError):
                pass
            finally:
                r.wire.deadline=original_deadline


def _balance(r):
    r.meta["beta"]=True
    for a,b in spans(r.first,r.last,30):
        data=r.fetch(PAY,"balance",{"body":{"date_from":a.isoformat(),"date_to":b.isoformat()}})
        total=data["total"];flow=data["cashflows"]
        base={"scope":"account","date_from":a.isoformat(),"date_to":b.isoformat()}
        for kind in ("opening_balance","closing_balance","accrued"):
            yield dict(base,kind=kind,**_money(total[kind]))
        for payment in total.get("payments") or []:yield dict(base,kind="payment",**_money(payment))
        for kind in ("sales","returns"):
            if flow.get(kind):
                yield dict(base,kind=kind,**_money(flow[kind].get("amount")),fee=_money(flow[kind].get("fee")))
        for service in flow.get("services") or []:yield dict(base,kind="service",name=service.get("name"),**_money(service.get("amount")))


def _removals(r):
    r.meta.update(beta=True,quantity_basis="provider_operations",stock_adjustment="completed_disposals_already_reflected_in_current_stock")
    for name in ("from_stock","from_supply","decommissioned"):
        if r.args.get("kind","all") not in ("all",name):continue
        def collect():
            for a,b in spans(r.first,r.last,30):
                if name!="decommissioned":
                    batch=r.pages(FBO,"removal_"+name,{"body":{"date_from":a.isoformat(),"date_to":b.isoformat(),"limit":100}},
                                  "returns_summary_report_rows",("body","last_id","last_id",100))
                    for line in batch:
                        yield from r.product({"kind":name,"date":line.get("return_created_at"),"quantity":line.get("quantity_for_return"),
                            "return_id":line.get("return_id"),"status":line.get("return_state"),"utilization_date":line.get("utilization_date"),
                            "estimated_delivery":_money(line.get("preliminary_delivery_price"))},line.get("offer_id"),line.get("sku"))
                else:
                    for scheme in ("FBO","FBS"):
                        body={"filter":{"date_from":a.isoformat()+"T00:00:00Z","date_to":b.isoformat()+"T23:59:59Z","delivery_schema":scheme},
                              "page":1,"page_size":99}
                        if r.ids:body["filter"]["skus"]=list(r.ids)
                        for line in r.pages(FBO,"decommissioned_goods",{"body":body},"items",("body","page",None,99)):
                            yield from r.product(dict(kind=name,date=line.get("date"),quantity=line.get("quantity"),scheme=scheme,
                                reason=line.get("dispose_reason"),posting_number=line.get("posting_number"),supply_id=line.get("supply_id"),
                                disposal_fee=_money(line.get("disposal_fee")),compensation=_money(line.get("compensation_price")),
                                compensation_type=line.get("compensation_type")),line.get("offer_id"),line.get("sku"))
        yield from r.section(name,collect)


def _acts(r):
    a=r.args
    supplies={}
    if "supply_id" in a:supplies[str(a["supply_id"])]=a.get("order_id")
    else:
        orders=[a["order_id"]] if "order_id" in a else []
        if not orders:
            start,end=_times(r)
            query={"filter":{"states":["COMPLETED","REPORTS_CONFIRMATION_AWAITING","REPORT_REJECTED"],
                             "timeslot_from_range":{"from":start,"to":end,"timeslot_filter_type":"BY_UTC_TIME"}},
                   "limit":100,"sort_by":"ORDER_CREATION","sort_dir":"DESC"}
            seen=set()
            while True:
                page=r.cached("ozon_post_v3_supply_order_list",{"body":query})
                orders.extend(page["order_ids"])
                marker=page.get("last_id")
                if not marker or not page["order_ids"]:break
                if marker in seen:raise ValueError("Repeated supply order last_id")
                seen.add(marker);query["last_id"]=marker
        for order in orders:
            response=r.fetch(FBO,"acceptance_summary",{"body":{"order_id":order}},86400)
            for section in response["supplies_acts"]:supplies[str(section["supply_id"])]=order
    for sid,order in supplies.items():
        doc=r.fetch(FBO,"accepted_products",{"body":{"supply_id":sid}},86400)
        defects={str(i["sku"]):i.get("defect_reasons") for i in doc.get("skus_defects") or []}
        for act in doc["supply_acts"]:
            for line in act.get("items") or []:
                details=line["sku_info"];sku=details.get("sku")
                expected=line.get("declared_quantity");count=line.get("fact_quantity")
                yield from r.product({"order_id":order,"supply_id":sid,"act_id":act.get("act_id"),"act_type":act.get("type"),
                    "declared":expected,"received":count,"approved":line.get("approved_quantity"),
                    "difference":int(count)-int(expected) if count is not None and expected is not None else None,
                    "reasons":defects.get(str(sku),[])},details.get("offer_id"),sku)


def _promotions(r,card):
    promos=r.fetch(PROMO,"promotions",ttl=300)["result"]
    if not card:
        for p in promos:yield {"scope":"promotion","promotion_id":p["id"],"title":p.get("title"),"date_from":p.get("date_start"),"date_to":p.get("date_end"),"type":p.get("action_type")}
        for p in r.pages(PROMO,"seller_promotions",{"body":{"limit":100,"offset":0}},"actions",("body","offset",None,100)):
            d=p.get("action_parameters") or {};yield {"scope":"seller_promotion","promotion_id":p["action_id"],"title":d.get("title"),"date_from":d.get("date_start"),"date_to":d.get("date_end")}
        return
    for p in promos:
        for kind in ("participating_products","candidate_products"):
            def collect():
                for item in r.pages(PROMO,kind,{"body":{"action_id":p["id"],"limit":100}},"products",("body","last_id","last_id",100)):
                    if str(item["id"])==str(card["product_id"]):
                        yield {"article":r.article,"sku_candidates":sorted(r.ids),"product_id":item["id"],"promotion_id":p["id"],
                               "title":p.get("title"),"kind":kind,**_money(item.get("action_price")),
                               "date_from":p.get("date_start"),"date_to":p.get("date_end")}
            yield from r.section(kind,collect)


def _slots(r):
    cluster=r.args.get("cluster_id")
    if r.ids:
        r.meta["warehouse_workload_status"] = "product_eligibility_not_reported"
    elif "order_id" not in r.args or cluster:
        def capacity():
            warehouses = None
            if cluster:
                raw = r.fetch(FBO,"macroclusters",ttl=86400)["result"]
                warehouses = {str(w["warehouse_id"]) for c in raw if str(c["macrolocal_cluster_id"]) == str(cluster)
                              for w in c["data"].get("fulfillments") or []}
            for entry in r.fetch(FBO,"warehouse_workload",ttl=900)["result"]:
                w = entry["warehouse"]
                if warehouses is None or str(w["id"]) in warehouses:
                    yield {"scope":"warehouse","warehouse_id":w["id"],"warehouse":w.get("name"),
                           "capacity_schedule":entry.get("schedule")}
        capacity_rows = r.section("warehouse_workload",capacity)
        r.meta["warehouse_workload_scope"] = "cluster" if cluster else "account"
        r.meta["warehouse_workload_count"] = len(capacity_rows)
        if r.args.get("mode") == "details":
            yield from capacity_rows
    if r.ids:
        for d in r.section("placement_zones",lambda:r.fetch(FBO,"placement_zones",{"body":{"skus":list(r.ids)}})["products_placement"]):
            yield from r.product({"placement_zone":d.get("placement_zone")},None,d.get("sku"))
    if "order_id" in r.args:
        replies=r.section("timeslots",lambda:[r.fetch(FBO,"timeslots",{"body":{"order_id":r.args["order_id"]}})])
        for result in replies:
            r.meta["slot_restrictions"]=result.get("timeslot_change_forbidden")
            for slot in (result.get("timeslots_info") or {}).get("timeslots") or []:
                yield dict(scope="supply_order",order_id=r.args["order_id"],**slot)
    else:r.meta["slot_status"]="requires_supply_order_id"
    if not r.ids and "order_id" not in r.args:
        rows=r.section("shipment_compliance",lambda:[r.fetch(FBO,"shipment_compliance")])
        if rows:r.meta["shipment_compliance"]=rows[0]


def read(r):
    name=r.name
    if name=="ozon_balance":yield from _balance(r);return
    if name=="ozon_fbs_errors":
        data=r.fetch(FBS,"fbs_rating")
        r.meta.update(index=data["index"],period_from=data.get("period_from"),period_to=data.get("period_to"),currency=data.get("currency_code"))
        yield from (dict(scope="seller_fbs",**line) for line in data["defects"]);return
    card=_identity(r)
    if name=="ozon_compensations":yield from _compensations(r)
    elif name=="ozon_buyout":yield from _buyouts(r)
    elif name=="ozon_removals":yield from _removals(r)
    elif name=="ozon_supply_acts":yield from _acts(r)
    elif name=="ozon_promotions":yield from _promotions(r,card)
    elif name=="ozon_supply_slots":yield from _slots(r)
    elif name=="ozon_wrong_volume":
        for d in r.pages(FBO,"wrong_dimensions",{"body":{"limit":100}},"products",("body","cursor","cursor",100)):
            yield from r.product({"product_id":d.get("product_id"),"name":d.get("name"),"provider_dimensions":{
                k:d.get(k) for k in ("height","width","length","weight")},"wrong_volume":True},d.get("offer_id"),d.get("sku"))
    elif name=="ozon_competitor_prices":
        if card:
            raw=r.fetch(PROMO,"competitor_prices",{"body":{"product_id":card["product_id"]}})["result"]
            r.meta["match_status"]="found" if raw.get("strategy_product_price") else "not_found"
            if raw.get("strategy_product_price"):
                yield dict(article=r.article,product_id=card["product_id"],sku_candidates=sorted(r.ids),
                           **_money(raw.get("strategy_product_price")),competitor_url=raw.get("strategy_competitor_product_url"),strategy_id=raw.get("strategy_id"))
        else:yield from (dict(scope="strategy",**d) for d in r.pages(PROMO,"pricing_strategies",{"body":{"page":1,"limit":100}},"strategies",("body","page",None,100)))
    elif name=="ozon_card_quality":
        if not r.ids:raise SourceIssue(status="product_selector_required",source=name)
        for choice,path in (("content_rating","products"),("visibility","items")):
            def collect():
                found=set()
                for line in r.fetch(CARD,choice,{"body":{"skus":sorted(r.ids)}})[path]:
                    found.add(str(line["sku"]))
                    yield from r.product(dict(kind=choice,**{k:v for k,v in line.items() if k!="sku"}),None,line.get("sku"))
                if r.ids-found:
                    r.issues.append(dict(source=choice,status="sku_missing_in_response",missing_skus=sorted(r.ids-found)))
            yield from r.section(choice,collect)
    elif name=="ozon_market":
        start,end=_times(r)
        yield from r.section("category_comparison",lambda:(dict(scope="category",**i) for i in
            r.fetch(CARD,"category_comparison",{"body":{"period":"MONTH","group":"CATEGORY_3","metric":"GMV","sort":"DESC"}})["items"]))
        if r.ids:
            def products():
                for d in r.pages(CARD,"product_queries",{"body":{"date_from":start,"date_to":end,"skus":list(r.ids),"limit_by_sku":10,"page":1,"page_size":100}},
                                 "queries",("body","page",None,100)):
                    yield from r.product(d,None,d.get("sku"))
            yield from r.section("product_queries",products)
        else:
            yield from r.section("top_queries",lambda:(dict(scope="market",**d) for d in r.pages(CARD,"top_queries",
                {"body":{"offset":0,"limit":100}},"search_queries",("body","offset",None,100))))
    elif name=="ozon_questions":
        start,end=_times(r)
        def questions():
            for d in r.pages(INFO,"questions",{"body":{"filter":{"status":"NEW","date_from":start,"date_to":end},"limit":100}},
                             "questions",("body","last_id","last_id",100)):
                yield from r.product({k:d.get(k) for k in ("id","text","status","published_at","answers_count")},None,d.get("sku"))
        yield from r.section("questions",questions)
        yield from r.section("chats",lambda:(dict(scope="chat",chat=d.get("chat"),unread_count=d.get("unread_count"))
            for d in r.pages(INFO,"chats",{"body":{"filter":{"unread_only":True},"limit":100}},"chats",("body","cursor","cursor",100))))
    elif name=="ozon_fbs_orders":
        start,end=_times(r);filters={"cutoff_from":start,"cutoff_to":end}
        if "warehouse_id" in r.args:filters["warehouse_ids"]=[r.args["warehouse_id"]]
        for p in r.pages(FBS,"unfulfilled",{"body":{"filter":filters,"limit":100,"with":{"analytics_data":True,"financial_data":True}}},"postings",("body","cursor","cursor",100)):
            for d in p["products"]:
                yield from r.product({"posting_number":p.get("posting_number"),"order_number":p.get("order_number"),"status":p.get("status"),
                    "shipment_date":p.get("shipment_date"),"quantity":int(d["quantity"]),"warehouse_id":(p.get("delivery_method") or {}).get("warehouse_id")},d.get("offer_id"),d.get("sku"))
    elif name=="ozon_fbs_stocks":
        wh=r.args.get("warehouse_id")
        body={"warehouse_id":wh,"limit":100} if wh else {"limit":100,**({"sku":sorted(r.ids)} if r.ids else {})}
        for d in r.pages(FBS,"warehouse_stock" if wh else "stock_by_warehouse",{"body":body},"stocks" if wh else "products",("body","cursor","cursor",100)):
            yield from r.product({k:d.get(k) for k in ("product_id","warehouse_id","warehouse_name","free_stock","present","reserved")},d.get("offer_id"),d.get("sku"))
