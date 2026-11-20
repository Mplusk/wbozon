"""Seller-facing report families backed exclusively by reviewed read operations."""

import csv
import io
import itertools
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
import re
import time
import zipfile


GROUPS = {
    "ozon_settlement_reports": ("Выкупы, баланс, готовые отчёты о компенсациях и обратных начислениях.", {
        "buyouts": "ozon_post_v1_finance_products_buyout", "balance": "ozon_post_v1_finance_balance",
        "compensations": "ozon_get_list", "decompensations": "ozon_get_list", "report_info": "ozon_get_report_details"}),
    "ozon_fbo_reports": ("Макрокластеры, приёмка, расхождения, слоты, план отгрузок, вывоз, списания и зоны размещения.", {
        "macroclusters": "ozon_post_v2_cluster_list", "wrong_dimensions": "ozon_post_v1_product_info_wrong_volume",
        "acceptance_summary": "ozon_post_v1_supply_order_act_summary_get", "accepted_products": "ozon_post_v1_supply_order_act_product_get",
        "removal_from_stock": "ozon_post_v1_removal_from_stock_list", "removal_from_supply": "ozon_post_v1_removal_from_supply_list",
        "decommissioned_goods": "ozon_post_v1_analytics_decommissioned_goods", "warehouse_workload": "ozon_get_warehouse_workload",
        "timeslots": "ozon_get_supply_timeslots", "shipment_compliance": "ozon_post_v1_supply_order_shipment_plan_compliance_get",
        "placement_zones": "ozon_post_v1_product_placement_zone_info"}),
    "ozon_price_and_promotion_reports": ("Акции площадки и продавца, товары и кандидаты, ценовые стратегии и цены конкурентов.", {
        "promotions": "ozon_get_available_promotions", "participating_products": "ozon_actions_products_v2",
        "candidate_products": "ozon_actions_candidates_v2", "seller_promotions": "ozon_post_v1_seller_actions_list",
        "pricing_strategies": "ozon_list_10", "competitor_prices": "ozon_get_competitor_price"}),
    "ozon_product_reports": ("Контент-рейтинг, видимость, сравнение с категорией и поисковые запросы.", {
        "content_rating": "ozon_product_rating", "visibility": "ozon_post_v1_product_visibility_info",
        "category_comparison": "ozon_post_v1_analytics_category_comparison", "top_queries": "ozon_post_v1_search_queries_top",
        "product_queries": "ozon_post_v1_analytics_product_queries_details"}),
    "ozon_seller_information": ("Разрешённые ключу методы, вопросы покупателей и список чатов.", {
        "roles": "ozon_post_v1_roles", "questions": "ozon_post_v1_question_list", "chats": "ozon_list_4"}),
    "ozon_fbs_reports": ("Заказы к сборке, остатки на складе продавца и индекс ошибок FBS.", {
        "unfulfilled": "ozon_fbs_unfulfilled_v4", "warehouse_stock": "ozon_post_v1_product_info_warehouse_stocks",
        "warehouses": "ozon_post_v2_warehouse_list",
        "stock_by_warehouse": "ozon_post_v2_product_info_stocks_by_warehouse_fbs", "fbs_rating": "ozon_post_v1_rating_index_fbs_info"}),
    "wb_inventory_reports": ("Текущие остатки FBW, аналитика остатков по офисам и товарам за интервал.", {
        "stock_now": "wb_stats_stocks", "offices": "wb_post_api_stocks_report_offices",
        "products": "wb_post_api_stocks_report_products_products"}),
    "wb_tariff_reports": ("Коэффициенты приёмки и тарифы на возврат, паллеты и транзит.", {
        "acceptance": "wb_get_api_tariffs_acceptance_coefficients", "returns": "wb_get_api_tariffs_return",
        "pallets": "wb_get_api_tariffs_pallet", "transit": "wb_get_api_transit_tariffs"}),
    "wb_settlement_reports": ("Баланс, эквайринг, удержания, штрафы за габариты, замеры, антифрод и закрывающие документы.", {
        "balance": "wb_finance_balance", "acquiring": "wb_post_api_finance_acquiring_list",
        "measurement_penalties": "wb_get_api_analytics_measurement_penalties", "warehouse_measurements": "wb_get_api_analytics_warehouse_measurements",
        "deductions": "wb_get_api_analytics_deductions", "antifraud": "wb_get_api_analytics_antifraud_details",
        "documents": "wb_get_api_documents_list"}),
    "wb_product_reports": ("Блокировка, скрытые карточки, карантин цен, рекомендации по карточке и рейтинг товара.", {
        "blocked": "wb_get_api_analytics_banned_products_blocked", "shadowed": "wb_post_api_analytics_v2_item_rating",
        "item_rating": "wb_post_api_analytics_v2_item_rating", "price_quarantine": "wb_get_api_quarantine_goods",
        "content_recommendations": "wb_post_api_content_recommendations_list"}),
    "wb_promotion_reports": ("Календарь и состав акций, поисковые кластеры, рекомендуемые ставки, рекламный баланс.", {
        "promotions": "wb_promotions_calendar", "details": "wb_get_api_calendar_promotions_details",
        "products": "wb_get_api_calendar_promotions_nomenclatures", "search_clusters": "wb_post_adv_normquery_stats_2",
        "bid_recommendations": "wb_get_api_advert_bids_recommendations", "ad_balance": "wb_get_adv_balance"}),
    "wb_seller_information": ("Подписка Джем, рейтинг продавца, новости, вопросы, непросмотренные отзывы, чаты и заявки на возврат.", {
        "jam_subscription": "wb_get_api_common_subscriptions", "seller_rating": "wb_get_api_common_rating",
        "news": "wb_get_api_communications_news", "questions": "wb_get_api_questions",
        "unread": "wb_get_api_new_feedbacks_questions", "chats": "wb_get_api_seller_chats", "return_claims": "wb_get_api_claims"}),
    "wb_csv_reports": ("Готовые CSV-отчёты WB. Получите список выгрузок или строки готового файла по его downloadId.", {
        "list": "wb_get_api_nm_report_downloads", "rows": "wb_get_api_nm_report_downloads_file_downloadid"}),
}

CAPABILITIES = {"ozon_roles": ("ozon_seller_information", "roles"), "wb_jam": ("wb_seller_information", "jam_subscription")}
REPORT_TYPES = {"compensations": "COMPENSATION_REPORT", "decompensations": "DECOMPENSATION_REPORT"}
SUSPENDED_WB_TARIFFS = {"acceptance", "transit"}
INPUT_NOTES = {
    ("ozon_settlement_reports","balance"): "Ozon принимает календарные даты YYYY-MM-DD, параметрам date_from и date_to время не требуется.",
    ("ozon_product_reports","category_comparison"): "Для группы SELLER доступны limit и offset. Для других групп опустите оба поля.",
    ("wb_promotion_reports","products"): "WB возвращает товары для обычных акций типа regular. Возьмите ID из календаря продавца.",
    ("wb_promotion_reports","bid_recommendations"): "Выберите nmId из состава указанной рекламной кампании.",
    ("wb_inventory_reports","stock_now"): "Текущий снимок FBW. История остатков по складам доступна в STOCK_HISTORY_DAILY_CSV.",
}


def schema(name):
    options = GROUPS[name][1]
    fields = {"report": {"type": "string", "enum": list(options)},
              "input": {"type": "object", "description": "Параметры API в body, query или path. Пустой объект вызывает метод без параметров."}}
    if name == "wb_csv_reports":
        fields.update(offset={"type":"integer","minimum":0}, limit={"type":"integer","minimum":1,"maximum":500})
    if name == "ozon_fbo_reports":
        fields["cluster_id"] = {"type":"integer","minimum":1,"description":"ID макрокластера для отчёта macroclusters."}
    return fields, ["report"]


def request(name, args):
    import market_catalog
    fields, _ = schema(name)
    if not isinstance(args, dict) or set(args) - fields.keys() or args.get("report") not in GROUPS[name][1]:
        raise ValueError("Unknown report or parameter")
    choice = args["report"]
    operation = GROUPS[name][1][choice]
    route = market_catalog.find_route(operation)
    if not route or route["category"] != "read":
        raise ValueError("Report has no verified read contract")
    if "cluster_id" in args and (choice != "macroclusters" or type(args["cluster_id"]) is not int or args["cluster_id"] < 1):
        raise ValueError("Invalid macrocluster selector")
    if "offset" in args or "limit" in args:
        if (choice != "rows" or type(args.get("offset", 0)) is not int or args.get("offset",0)<0
                or type(args.get("limit",100)) is not int or not 1<=args.get("limit",100)<=500):
            raise ValueError("Invalid CSV page")
    if "input" not in args:
        return route, None
    inputs = args["input"]
    if not isinstance(inputs,dict):
        raise ValueError("Expected API input")
    if (name, choice) == ("ozon_settlement_reports", "balance") and isinstance(inputs.get("body"),dict):
        body = dict(inputs["body"])
        for key in ("date_from","date_to"):
            if key in body and isinstance(body[key],str):
                day = body[key]
                body[key] = (datetime.fromisoformat(day.replace("Z","+00:00")).date() if "T" in day else date.fromisoformat(day)).isoformat()
        inputs = dict(inputs, body=body)
    if (name,choice) == ("ozon_product_reports","category_comparison"):
        body=inputs.get("body") or {}
        if isinstance(body,dict) and body.get("group")!="SELLER" and {"limit","offset"}.intersection(body):
            raise ValueError("Ozon only permits paging seller comparisons")
    fixed = ({"report_type": REPORT_TYPES[choice]} if choice in REPORT_TYPES else
             {"onlyShadowedNms": True} if choice == "shadowed" else {})
    if fixed:
        body = inputs.get("body", {})
        if not isinstance(body, dict) or any(k in body and body[k] != v for k,v in fixed.items()):
            raise ValueError("Conflicting report selector")
        if choice in REPORT_TYPES:
            body = {"page":1, "page_size":100, **body}
        inputs = dict(inputs, body={**body, **fixed})
    market_catalog.validate_inputs(route, inputs)
    return route, inputs


def execute(name, args, creds, directory, deadline):
    import market_catalog
    route, inputs = request(name, args)
    choice = args["report"]
    generation = None
    if choice in REPORT_TYPES:
        generation = {"operation_id":"ozon_post_v1_finance_" + ("compensation" if choice=="compensations" else "decompensation"),
                      "input":{"body":{"date":"YYYY-MM"}}}
    elif name == "wb_csv_reports" and choice == "list":
        generation = {"operation_id":"wb_post_api_nm_report_downloads","funnel_type":"DETAIL_HISTORY_REPORT",
                      "inventory_type":"STOCK_HISTORY_DAILY_CSV"}
    if generation:
        generation.update(approval_tool="market_api_propose",key_scope="catalog_operations",requires_owner_confirmation=True)
    if inputs is None:
        detail = dict(market_catalog.describe(route["operation_id"]), report=choice, status="schema",
                      call="Передайте input согласно схеме. Для запроса без параметров передайте пустой объект.")
        if name == "wb_tariff_reports" and choice in SUSPENDED_WB_TARIFFS:
            detail.update(provider_notice="temporarily_disabled", documentation="https://dev.wildberries.ru/release-notes?id=570")
        if note := INPUT_NOTES.get((name,choice)):
            detail["usage"] = note
        if generation:
            detail["generation"] = generation
        return detail
    query={"operation_id":route["operation_id"], "input":inputs}
    if name == "wb_csv_reports" and choice == "rows":
        return csv_rows(query, args.get("offset",0), args.get("limit",100), creds, directory, deadline)
    import urllib.error
    try:
        result=market_catalog.transfer("market_api_read",query,creds,directory,deadline)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or name != "wb_tariff_reports" or choice not in SUSPENDED_WB_TARIFFS:
            raise
        exc.close()
        return {"report":choice, "status":"temporarily_disabled_by_provider","http_status":404, "complete":False,
                "source":route["path"], "documentation":"https://dev.wildberries.ru/release-notes?id=570"}
    data=result.get("data")
    failed=isinstance(data,dict) and (data.get("error") is True or data.get("success") is False)
    result.update(report=choice, read_only=True, untrusted_data=True, source=route["path"],
                  status="provider_error" if failed else "ready", data_as_of=result.get("fetched_at") or result.get("observed_at"))
    if choice == "macroclusters":
        directory_rows = []
        for cluster in result["data"]["result"]:
            cluster_id = int(cluster["macrolocal_cluster_id"])
            if "cluster_id" in args and args["cluster_id"] != cluster_id:
                continue
            info = cluster["data"]
            directory_rows.append({"cluster_id":cluster_id, "name":info["macrolocal_cluster"]["name"],
                                   "warehouse_count":len(info.get("fulfillments") or [])})
        result["data"] = {"clusters":directory_rows}
    if choice in REPORT_TYPES:
        result["report_type"] = REPORT_TYPES[choice]
    if generation:
        result["generation"] = generation
    return result


def capabilities(vault, directory, refresh, read):
    import urllib.error
    import finance_gate
    import market_catalog
    from market_transport import Wire
    results = {}
    for key, (group, choice) in CAPABILITIES.items():
        route, params = request(group, {"report":choice, "input":{}})
        creds = market_catalog.read_credentials(vault, route)
        if not creds:
            results[key] = {"status":"not_configured"}
            continue
        wire = Wire(route["market"], creds, directory, time.monotonic()+12)
        identity = [route["operation_id"]]
        old = wire.progress_snapshot("seller-capabilities-v2",identity)
        row = old or {"status":"not_checked"}
        if refresh:
            try:
                fetched = read("market_api_read", {"operation_id":route["operation_id"],"input":params}, creds)
                data, http = fetched.get("data"), fetched.get("http_status")
                if http != 200:
                    row=dict(row, refresh_status="provider_error", http_status=http)
                elif key == "ozon_roles" and isinstance(data,dict) and isinstance(data.get("roles"),list):
                    roles=data["roles"]
                    if not all(isinstance(r,dict) and isinstance(r.get("name"),str) and isinstance(r.get("methods"),list) for r in roles):
                        raise ValueError("Unexpected roles")
                    methods=sorted({m for r in roles for m in r["methods"] if isinstance(m,str)})
                    row={"status":"ready","role_names":[r["name"] for r in roles],
                         "allowed_method_count":len(methods),"allowed_methods":methods,
                         "expires_at":data.get("expires_at")}
                elif key == "wb_jam" and (data is None or isinstance(data,dict)):
                    details=(data or {}).get("data",data) or {}
                    if not details:
                        row={"status":"ready","state":"never_connected"}
                    else:
                        if not isinstance(details,dict) or details.get("state") not in ("active","inactive"):
                            raise ValueError("Unexpected subscription")
                        row={"status":"ready",**{k:details.get(k) for k in ("state","activationSource","level","since","till")}}
                else:
                    raise ValueError("Unexpected reply")
                if row.get("status")=="ready" and "refresh_status" not in row:
                    row.update(checked_at=fetched.get("fetched_at") or datetime.now(timezone.utc).isoformat(),http_status=http)
                    with wire.progress("seller-capabilities-v2",identity) as (_,save):
                        save(row,7*86400)
            except (finance_gate.GateError, TimeoutError, urllib.error.URLError, ValueError) as exc:
                row=dict(row, refresh_status="unavailable", http_status=getattr(exc,"code",None),
                         retry_after_seconds=getattr(exc,"retry_after_seconds",None))
                if isinstance(exc,urllib.error.HTTPError):
                    if key == "wb_jam" and exc.code == 403:
                        row.update(status="access_denied",state=None,reason="WB only exposes the subscription to an eligible service token")
                    if exc.code==429:row["retry_after_seconds"]=finance_gate.parse_retry_after(exc.headers)[0]
                    exc.close()
                if row.get("status")=="access_denied":
                    row["checked_at"] = datetime.now(timezone.utc).isoformat()
                    with wire.progress("seller-capabilities-v2",identity) as (_,save):
                        save(row, 3600)
        results[key]=dict(row, source=route["path"])
    return results


CSV_PATH = "/api/v2/nm-report/downloads/file/{downloadId}"


def _download(download_id, creds, directory, deadline):
    import market_catalog
    query = {"operation_id": GROUPS["wb_csv_reports"][1]["rows"], "input":{"path":{"downloadId":download_id}}}
    return market_catalog.transfer("market_api_read", query, creds, directory, deadline, csv_download=True)


def _records(blob, creds, deadline, required=()):
    import finance_gate
    tokens = tuple(value for key,value in creds.items() if key in ("api_key","api_token") and value)
    try:
        with ExitStack() as stack:
            if blob.startswith(b"PK"):
                archive = stack.enter_context(zipfile.ZipFile(io.BytesIO(blob)))
                files = [f for f in archive.infolist() if not f.is_dir() and f.filename.lower().endswith(".csv")]
                if not files or sum(f.file_size for f in files) > 128*1024*1024:
                    raise ValueError("Invalid CSV archive")
                streams = (archive.open(f) for f in files)
            else:
                streams = [io.BytesIO(blob)]
            for stream in streams:
                with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as fp:
                    first = fp.readline(65536)
                    if not first or len(first) >= 65536:
                        raise ValueError("Invalid CSV header")
                    dialect = csv.Sniffer().sniff(first, delimiters=",;\t") if any(c in first for c in ",;\t") else csv.excel
                    reader = csv.DictReader(itertools.chain([first], fp), dialect=dialect)
                    headers = reader.fieldnames
                    if (not headers or len(headers) != len(set(headers)) or not set(required) <= set(headers)
                            or any(token in first for token in tokens)):
                        raise ValueError("Unexpected CSV columns")
                    for index,row in enumerate(reader):
                        if index % 256 == 0 and time.monotonic() >= deadline:
                            raise TimeoutError("CSV read deadline")
                        if (None in row or any(v is None for v in row.values())
                                or any(token in v for v in row.values() for token in tokens)):
                            raise ValueError("Invalid CSV record")
                        yield row
    except (csv.Error, UnicodeError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
        raise finance_gate.ProviderResponseInvalid("Invalid seller CSV report") from exc


def _page(records, offset, limit):
    import finance_gate
    page, size = [], 0
    for row in itertools.islice(records, offset, None):
        row_size = sum(len(str(k).encode()) + len(str(v).encode()) + 12 for k,v in row.items())
        if row_size > 180000:
            raise finance_gate.ProviderResponseTooLarge("Single CSV row exceeds response size")
        if len(page) >= limit or size + row_size > 180000:
            return page, offset + len(page)
        page.append(row)
        size += row_size
    return page, None


def csv_rows(query, offset, limit, creds, directory, deadline):
    download_id = query["input"]["path"]["downloadId"]
    blob, stamp = _download(download_id, creds, directory, deadline)
    items, following = _page(_records(blob, creds, deadline), offset, limit)
    return {"items":items, "next_offset":following, "complete":following is None, "download_id":download_id,
            "source":CSV_PATH, "data_as_of":stamp, "read_only":True, "untrusted_data":True}


def funnel_csv(args, wire):
    from market_specs import result
    download_id = args["download_id"]
    creds = getattr(wire,"category_keys",{}).get("wb_analytics", wire.creds)
    blob, stamp = _download(download_id, creds, wire.gate_dir, wire.deadline)
    names = {"openCardCount":"open_count", "addToCartCount":"cart_count", "ordersCount":"order_count",
             "ordersSumRub":"order_sum", "buyoutsCount":"buyout_count", "buyoutsSumRub":"buyout_sum",
             "cancelCount":"cancel_count", "cancelSumRub":"cancel_sum"}
    ids = {str(n) for n in args.get("nm_ids", [args["nm_id"]] if "nm_id" in args else [])}
    def selected():
        for row in _records(blob, creds, wire.deadline, ("nmID","dt","ordersCount","currency")):
            if ids and row["nmID"] not in ids:
                continue
            day = date.fromisoformat(row["dt"]).isoformat()
            if args["date_from"] <= day <= args["date_to"]:
                yield {"nm_id":int(row["nmID"]), "date":day, "currency":row["currency"],
                       **{key:int(row[col]) if row.get(col) else None for col,key in names.items()}}
    cursor = str(args.get("cursor", args.get("offset",0)))
    if not cursor.isascii() or not cursor.isdecimal() or len(cursor)>8:
        raise ValueError("Invalid CSV cursor")
    items, following = _page(selected(), int(cursor), args.get("limit",100))
    return result(items, following, aggregation="day", date_basis="order_date_moscow", source=CSV_PATH,
                  report_type="DETAIL_HISTORY_REPORT", data_as_of=stamp, download_id=download_id, cursor_parameter="cursor")


def stock_csv(download_id, article, nm, first, last, wire):
    creds = getattr(wire,"category_keys",{}).get("wb_analytics", wire.creds)
    blob, stamp = _download(download_id, creds, wire.gate_dir, wire.deadline)
    quantities = {}
    sizes = set()
    for row in _records(blob, creds, wire.deadline, ("VendorCode","NmID","ChrtID","OfficeName")):
        columns = [k for k in row if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", k)]
        if not columns:
            raise ValueError("Expected WB daily stock columns")
        if row["VendorCode"] != article or str(nm) != row["NmID"]:
            continue
        office = row["OfficeName"].strip()
        key = (office, row["ChrtID"])
        if not office or key in sizes:
            raise ValueError("Missing or repeated warehouse size")
        sizes.add(key)
        for col in columns:
            day = datetime.strptime(col,"%d.%m.%Y").date().isoformat()
            if first <= day <= last:
                value = row[col].strip()
                if value and not value.isdecimal():
                    raise ValueError("Invalid WB stock quantity")
                quantities.setdefault(office,{}).setdefault(day,[]).append(int(value) if value else None)
    offices = {}
    for office, days in sorted(quantities.items()):
        offices[office] = {day:sum(values) if all(v is not None for v in values) else None
                           for day,values in sorted(days.items())}
    return {"status":"ready" if offices else "no_matching_rows", "source":CSV_PATH,
            "report_type":"STOCK_HISTORY_DAILY_CSV", "download_id":download_id, "data_as_of":stamp,
            "observation":"stock_at_23_59_moscow", "offices":offices}


def availability_days(article, nm, since, through, wire):
    """Resolve ambiguous duration reports by progressively bisecting closed periods."""
    import market_catalog
    from market_composites import observed
    from market_transport import Wire
    creds = getattr(wire,"category_keys",{}).get("wb_analytics",wire.creds)
    scoped = Wire("wb",creds,wire.gate_dir,min(wire.deadline-2,time.monotonic()+6))
    path = "/api/v2/stocks-report/products/products"
    with scoped.progress("wb-fbw-product-availability-v1", [article,str(nm)]) as (saved, save):
        state = saved or {"days":{}, "pending":[], "through":None}
        days = {d:s for d,s in state["days"].items() if since<=d<=through}
        pending = [(max(a,since), min(b,through)) for a,b in state["pending"] if a<=through and b>=since]
        previous = state["through"]
        if previous is None or previous < since or through < previous:
            pending=[(since,through)]
        elif through > previous:
            pending.append(((date.fromisoformat(previous)+timedelta(days=1)).isoformat(),through))
        data = {}
        if pending and time.monotonic()+2 < scoped.deadline:
            a,b = pending[0]
            query = {"currentPeriod":{"start":a,"end":b},"stockType":"wb","nmIDs":[int(nm)],"skipDeletedNm":False,
                     "availabilityFilters":["deficient","actual","balanced","nonActual","nonLiquid","invalidData"],
                     "orderBy":{"field":"ordersCount","mode":"desc"},"limit":2,"offset":0}
            data = observed(lambda:market_catalog.transfer("market_api_read",{
                "operation_id":GROUPS["wb_inventory_reports"][1]["products"], "input":{"body":query}},
                creds,wire.gate_dir,scoped.deadline))
            if data.get("http_status")==200:
                items = data["data"]["data"]["items"]
                match = [r for r in items if str(r.get("nmID"))==str(nm) and r.get("vendorCode")==article]
                if len(match)==1:
                    value=match[0]["metrics"]["officeMissingTime"]
                    d,h=value["days"],value["hours"]
                    first,last=date.fromisoformat(a),date.fromisoformat(b)
                    length=(last-first).days+1
                    if type(d) is not int or type(h) is not int:
                        raise ValueError("Invalid stock duration")
                    hours=d*24+h if d>=0 and h>=0 else None
                    full=h==-4 or hours==length*24
                    available=h==-2 or hours==0
                    pending.pop(0)
                    if full or available or (length==1 and hours is not None and 0<hours<24):
                        for i in range(length):
                            days[(first+timedelta(days=i)).isoformat()] = "stockout" if full else "available"
                    elif length>1 and hours is not None and 0<hours<length*24:
                        mid=first+timedelta(days=(length-1)//2)
                        pending.extend([((mid+timedelta(days=1)).isoformat(),b),(a,mid.isoformat())])
                    else:
                        data["status"]="duration_unavailable"
                else:
                    data["status"]="product_not_reported"
                    pending.pop(0)
        save({"days":days,"pending":pending,"through":through}, 7*86400)
        return {"source":path, "scope":"all_fbw_warehouses_for_product", "status":data.get("status") or ("collecting" if pending else "ready"),
                "known_days":sorted(days),"whole_day_stockouts":sorted(d for d,s in days.items() if s=="stockout"),
                "pending_periods":len(pending), "retry_after_seconds":data.get("retry_after_seconds")}
