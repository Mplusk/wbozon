"""Dispatch authenticated marketplace reads inside the portal's deadline worker."""

from decimal import Decimal, localcontext
import json
import hashlib
from pathlib import Path
import urllib.error
import time
from datetime import date,datetime,timedelta,timezone
from urllib.parse import urlsplit

import finance_gate
import market_specs
import market_health
import market_history
import market_composites
from market_transport import Wire
import ozon_details
import ozon_expansion
import ozon_mcp
import ozon_performance
import supply_plan
import wb_expansion


def provider(name):
    if name == "ozon_ads":
        return "performance"
    return "wb" if name.startswith("wb_") else "ozon"


def credentials(vault, name):
    if name in market_health.COMPOSITE:
        keys={p:c for p in ("wb","ozon","performance") if (c:=vault.read(p))}
        extras={p:c for p in market_health.WB_CATEGORIES if (c:=vault.read(p))}
        if extras:
            keys["wb"]=dict(keys.get("wb") or next(iter(extras.values())),category_keys=extras)
        return keys
    if name.startswith("wb_"):
        preferred=market_health.WB_TOOLS.get(name)
        creds=(vault.read(preferred) if preferred else None) or vault.read("wb")
        extras={p:c for p in market_health.WB_CATEGORIES if (c:=vault.read(p))}
        if extras:
            return dict(creds or next(iter(extras.values())),category_keys=extras)
        return creds
    creds = vault.read(provider(name))
    if name == "ozon_ads" and creds:
        seller = vault.read("ozon")
        if seller:
            return dict(creds, seller_lookup=seller)
    return creds


def _mapping(args, directory):
    if "mapping" in args:
        return args["mapping"]
    # Alias to seller codes is private deployment data. MCP input cannot select a filename.
    file = Path(directory).parent / "supply-articles.json"
    try:
        with file.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return None
    if len(raw) > 65536:
        raise ValueError("Invalid mapping file")
    mappings = json.loads(raw)
    needle=args["article"].casefold().strip()
    candidates=[v for k,v in mappings.items() if k.casefold().strip()==needle or
                needle in (str(v.get("ozon_offer_id","")).casefold(),str(v.get("wb_vendor_code","")).casefold())]
    mapping=candidates[0] if candidates and all(v==candidates[0] for v in candidates) else None
    if mapping is not None:
        market_specs.validate("article_map", {"article":args["article"],"mapping":mapping})
    return mapping


class CategoryWire(Wire):
    def __init__(self,provider,creds,directory,deadline):
        super().__init__(provider,{k:v for k,v in creds.items() if k!="category_keys"},directory,deadline)
        self.category_keys=creds.get("category_keys",{})
        self.sources={}

    def __call__(self,method,url,**params):
        category=market_health.HOST_KEYS.get(urlsplit(url).hostname.split(".")[0])
        chosen=self.category_keys.get(category)
        if chosen and chosen!=self.creds:
            if category not in self.sources:
                self.sources[category]=Wire("wb",chosen,self.gate_dir,self.deadline)
                self.sources[category].fetch_times=self.fetch_times
            self.sources[category].deadline = self.deadline
            return self.sources[category](method,url,**params)
        return super().__call__(method,url,**params)


def make_wire(p,c,directory,deadline):
    return CategoryWire(p,c,directory,deadline) if p=="wb" and "category_keys" in c else Wire(p,c,directory,deadline)


def regulated_ozon(name,args,wire):
    import portal,market_cooldown
    if wire.deadline-time.monotonic()<2:
        raise TimeoutError("Insufficient finance read time")
    def read():
        with market_cooldown.worker(wire.gate_dir,wire.creds):
            return portal._provider_read(name,args,wire.creds,absolute_deadline=wire.deadline)
    portal.finance_request_preflight(name,args,wire.creds)
    with wire.progress("composite-finance-v2",[name,args]) as (cached,save):
        if cached: return cached
        value=portal._finance_read_with_retry(wire.gate_dir,wire.creds,read,wire.deadline,allow_retry=False)
        save(value,1800)
        return value


def _wb_identity(wire, article=None, nm=None):
    body = {
        "settings": {
            "cursor": {"limit": 100},
            "filter": {"textSearch": str(nm) if nm is not None else article, "withPhoto": -1},
        }
    }
    data = wire(
        "POST",
        "https://content-api.wildberries.ru/content/v2/get/cards/list",
        body=body,
        interval=1,
        ttl=1800,
    )
    cards = market_specs.rows(data.get("cards"))
    found = [
        r
        for r in cards
        if (nm is None or str(r.get("nmID")) == str(nm))
        and (article is None or r.get("vendorCode") == article)
    ]
    if len(found) != 1 or (nm is None and (data.get("cursor") or {}).get("total", len(cards)) >= 100):
        return None
    return {"article": found[0].get("vendorCode"), "nm_id": found[0].get("nmID")}


def _enrich(name, args, data, request):
    if not data.get("items"):
        return
    if name not in ("ozon_ads", "ozon_sku_daily", "ozon_reviews", "wb_ads", "wb_acceptance_report"):
        return
    try:
        if name.startswith("wb_"):
            unresolved = {r.get("nm_id") for r in data["items"] if r.get("nm_id") and not r.get("article")}
            for nm in sorted(unresolved, key=str)[:5]:
                identity = _wb_identity(request, nm=nm)
                if identity:
                    for row in data["items"]:
                        if row.get("nm_id") == nm:
                            row["article"] = row["vendor_code"] = identity["article"]
        else:
            unresolved = list(
                dict.fromkeys(
                    str(r["sku"])
                    for r in data["items"]
                    if r.get("sku") is not None and not r.get("article")
                )
            )
            if not unresolved:
                return
            subset = unresolved[:100]
            raw = request(
                "POST", ozon_expansion.BASE + "/v3/product/info/list", body={"sku": subset}, ttl=1800
            )
            info = ozon_details._products({"skus": subset}, raw, None)
            if info["conflicts"] or info["unmatched_row_indices"]:
                return
            mapping = {sku: card["offer_id"] for card in info["items"] for sku in card["sku_candidates"]}
            for row in data["items"]:
                sku = str(row.get("sku"))
                if not row.get("article") and sku in mapping:
                    row["article"] = row["offer_id"] = mapping[sku]
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        finance_gate.GateError,
        finance_gate.ProviderResponseTooLarge,
    ):
        data["identity_enrichment"] = "unavailable"
    if any(r.get("article") is None and (r.get("nm_id") or r.get("sku")) for r in data["items"]):
        data["identity_enrichment"] = "incomplete"


def _finance_period(args, wire):
    """Resume at the last confirmed rrdId; totals only become final after HTTP 204."""
    query = {k: v for k, v in args.items() if k not in ("mode", "cursor")}
    with wire.progress("wb-finance-v2", query) as (state, save):
        state = state or {"cursor": 0, "rows": [], "unallocated": 0, "done": False}
        issues = []
        for _ in range(12):
            if state["done"]:
                break
            try:
                page = wb_expansion._finance(dict(query, cursor=state["cursor"],limit=5000), wire)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                issues.append(
                    {"code": "method_cooldown", "retry_after_seconds": exc.headers.get("Retry-After")}
                )
                exc.close()
                break
            if page.get("issues"):
                issues.extend(page["issues"])
                break
            state["rows"].extend(page["items"])
            state["unallocated"] += len(page.get("unallocated", []))
            state["done"] = page["complete"]
            state["cursor"] = page["next_cursor"]
            if len(state["rows"]) > 20000:
                return market_specs.result([], complete=False, issues=["narrow_period_or_select_article"])
            save(state, 1800 if state["done"] else 7 * 86400)
        data = wb_expansion.finance_summary(state["rows"]) if state["done"] else []
        return market_specs.result(
            data,
            complete=state["done"],
            status="ready" if state["done"] else "pending",
            issues=issues,
            progress_cursor=state["cursor"],
            rows_observed=len(state["rows"]),
            unallocated_rows=state["unallocated"],
            unit_basis="sold_units_before_returns",
            summary_scope="requested_period" if state["done"] else "pending_period",
            cost_coverage="allocated_rows_only",
            period_from=args["date_from"],
            period_to=args["date_to"],
        )


def _wb_daily_totals(name,args,wire):
    query={k:v for k,v in args.items() if k not in ("limit","offset","mode","archive","group_by")}
    with wire.progress("wb-period-summary-v2",[name,query]) as (saved,save):
        state=saved or {"next":None,"events":{},"done":False,"seen":[]}
        status="pending"
        delay=None
        for _ in range(12):
            if state["done"] or time.monotonic()+2>=wire.deadline: break
            cursor=state["next"]
            source=dict(query,limit=2000)
            if cursor is not None: source["cursor"]=cursor
            try:
                page=wb_expansion._stats(name,source,wire)
            except urllib.error.HTTPError as exc:
                if exc.code!=429: raise
                delay,_=finance_gate.parse_retry_after(exc.headers)
                exc.close()
                break
            for row in page["items"]:
                key=row.get("sale_id" if name=="wb_sales" else "srid")
                if not key: return market_specs.result([],complete=False,status="missing_event_id")
                value=[row.get("article"),row["date"][:10],row.get("warehouse"),row.get("region"),row.get("event_type","cancelled" if row.get("cancelled") else "order"),row["quantity"],row.get("last_change")]
                prior=state["events"].get(key)
                if prior and prior!=value and (not value[-1] or not prior[-1] or value[-1]<=prior[-1]):
                    return market_specs.result([],complete=False,status="conflicting_event_revision")
                state["events"][key]=value
            marker=page["next_cursor"]
            if page.get("issues") or (marker is not None and marker in state["seen"]):
                status="nonadvancing_source"
                break
            if len(state["events"])>50000: return market_specs.result([],complete=False,status="narrow_period")
            state.update(next=marker,done=page["complete"])
            if marker is not None: state["seen"].append(marker)
            save(state,1800 if state["done"] else 86400)
        if "group_by" in args:
            events = ((a,d,e,q) for a,d,_,_,e,q,_ in state["events"].values())
            through = min(datetime.now(market_specs.MOSCOW).date().isoformat(), args["date_to"])
            days = market_history.days_between(args["date_from"], through) if state["done"] else []
            items = market_history.grouped_orders(args, events, days)
        else:
            groups={}
            for article,day,warehouse,region,event,units,_ in state["events"].values():
                key=article,day,warehouse,region,event
                groups[key]=groups.get(key,0)+units
            items=[{"article":a,"date":d,"warehouse":w,"region":r,"event":e,"units":q} for (a,d,w,r,e),q in sorted(groups.items(),key=str)]
        offset,limit=args.get("offset",0),args.get("limit",100)
        more=offset+limit<len(items)
        return market_specs.result(items[offset:offset+limit],offset+limit if more else None,
             complete=state["done"] and not more,status="ready" if state["done"] else status,
             retry_after_seconds=delay,summary_scope="observed_period",date_basis="event_date_moscow")


def _ozon_order_counts(wire, args, skus):
    """Count unique orders containing each SKU across FBO and FBS, including split postings."""
    query = [args["date_from"], args["date_to"], sorted(skus)]
    start, end = ozon_expansion._range(args)
    with wire.progress("ozon-order-denominator-v2", query) as (saved, save):
        state = saved or {
            "next": {"fbo": "", "fbs": 0},
            "ids": {sku: {} for sku in skus},
            "units": {sku: {} for sku in skus},
            "posting_orders": {},
            "seen_cursors": [],
            "issue": None,
        }
        for scheme in ("fbo", "fbs"):
            for _ in range(8):
                cursor = state["next"][scheme]
                if cursor is None or state["issue"]:
                    break
                body = {"filter": {"since": start, "to": end}, "limit": 100}
                if scheme == "fbo":
                    body.update(cursor=cursor, sort_dir="ASC")
                else:
                    body.update(offset=cursor, dir="asc")
                raw = wire(
                    "POST", ozon_expansion.BASE + "/v3/posting/" + scheme + "/list", body=body, ttl=1800
                )
                payload = raw if scheme == "fbo" else raw["result"]
                postings = market_specs.rows(payload.get("postings"))
                for post in postings:
                    products = market_specs.rows(post.get("products"))
                    if any(row.get("sku") is None for row in products):
                        state["issue"] = "unidentified_order_product"
                    matching = {str(r["sku"]) for r in products if str(r.get("sku")) in skus}
                    if not matching:
                        continue
                    ident = str(post.get("order_id", ""))
                    if not ident.isdigit() or int(ident) <= 0:
                        state["issue"] = "unidentified_order"
                        break
                    key = hashlib.sha256(ident.encode()).hexdigest()
                    posting = post.get("posting_number")
                    for sku in matching:
                        state["ids"][sku][key] = True
                        quantities = [r.get("quantity") for r in products if str(r.get("sku")) == sku]
                        known = (
                            isinstance(posting, str)
                            and posting
                            and all(type(n) is int and n > 0 for n in quantities)
                        )
                        posting_key = hashlib.sha256(str((scheme, posting)).encode()).hexdigest()
                        if posting:
                            owners = state.setdefault("posting_orders", {})
                            if posting_key in owners and owners[posting_key] != key:
                                state["issue"] = "posting_order_mismatch"
                            owners[posting_key] = key
                        quantity = sum(quantities) if known else None
                        units = state["units"][sku]
                        if posting_key in units and units[posting_key] != quantity:
                            state["issue"] = "inconsistent_order_quantity"
                        units[posting_key] = quantity
                more = payload.get("has_next")
                next_cursor = payload.get("cursor") if scheme == "fbo" else cursor + len(postings)
                if type(more) is not bool or (more and (not postings or next_cursor in (None, "", cursor))):
                    state["issue"] = "unconfirmed_order_pagination"
                if scheme == "fbo" and more:
                    history = state.setdefault("seen_cursors", [])
                    if next_cursor in history or len(history) >= 1000:
                        state["issue"] = "nonadvancing_order_cursor"
                    history.append(next_cursor)
                if sum(len(v) for v in state["ids"].values()) > 50000:
                    state["issue"] = "narrow_period"
                state["next"][scheme] = next_cursor if more else None
                save(state, 1800 if all(c is None for c in state["next"].values()) else 86400)
        if state["issue"] or any(c is not None for c in state["next"].values()):
            return {}, state["issue"] or "pending_order_pages"
        return {
            sku: {
                "orders": len(ids),
                "units": sum(state["units"][sku].values())
                if all(n is not None for n in state["units"][sku].values())
                else None,
            }
            for sku, ids in state["ids"].items()
        }, None


def _order_share(name, args, data, wire):
    facts = data.pop("direct_attribution", None)
    if facts is None:
        return
    data["order_share"] = {
        "scope": "selected_campaigns_separately",
        "rows": [],
        "period_from": args["date_from"],
        "period_to": args["date_to"],
    }
    output = data["order_share"]
    wb = name == "wb_ads"
    output["basis"] = (
        "direct_ad_units_over_funnel_ordered_units"
        if wb
        else "CPC_export_orders_compared_to_unique_FBO_FBS_orders_and_ordered_units"
    )
    output["interpretation"] = (
        "Атрибуция площадки по выбранным отчётным датам. Окно после рекламного контакта может выходить за границы периода заказов."
    )
    if not facts:
        output["status"] = "no_product_attribution_rows"
        return
    if wire is None:
        output["status"] = "seller_api_required_for_denominator"
        return
    try:
        if wb:
            key = "nm_id"
            total = wb_expansion.read(
                "wb_funnel", {k: args[k] for k in ("date_from", "date_to", "nm_id")}, wire
            )
            items = total["items"]
            counts = {str(r["nm_id"]): r["order_count"] for r in items}
            issue = None if total["complete"] and len(items) == len(counts) else "incomplete_funnel"
        else:
            key = "sku"
            counts, issue = _ozon_order_counts(wire, args, {r[key] for r in facts})
    except (
        urllib.error.URLError,
        TimeoutError,
        finance_gate.GateError,
        ValueError,
        finance_gate.ProviderResponseTooLarge,
    ) as exc:
        output.update(status="denominator_unavailable")
        if isinstance(exc, urllib.error.HTTPError):
            output.update(http_status=exc.code, retry_after_seconds=exc.headers.get("Retry-After"))
            exc.close()
        return
    output["status"] = issue or "ready"
    for fact in facts:
        count = counts.get(fact[key])
        unit_count = count if wb else (count or {}).get("units")
        if not wb:
            count = (count or {}).get("orders")
        numerator = fact["direct_attributed"]
        valid = (
            not issue
            and type(count) is int
            and count > 0
            and numerator is not None
            and numerator <= count
            and count == unit_count
        )
        output["rows"].append(
            dict(
                fact,
                total_orders_or_units=count,
                ordered_units=unit_count,
                unique_orders=None if wb else count,
                percent=str(Decimal(numerator) * 100 / count) if valid else None,
            )
        )
        if not valid and not issue:
            output["status"] = "incomparable_or_empty_counts"


def _summary(name, data):
    """Keep provider pagination visible in both representations; totals cover this page."""
    items = data.get("items", [])
    if not items:
        return
    if data.get("status") == "choose_campaign_ids":
        data["summary_scope"] = "campaign_directory"
        return
    if name in ("wb_funnel", "ozon_sku_daily"):
        data["summary_scope"] = "returned_products" if name == "wb_funnel" else "returned_sku_days"
        return
    if name in (
        "wb_stocks",
        "wb_paid_storage",
        "wb_acceptance_report",
        "wb_ads",
        "ozon_ads",
        "ozon_fbo_postings_geo",
    ):
        values = {
            "wb_stocks": ("quantity",),
            "wb_paid_storage": ("amount",),
            "wb_acceptance_report": ("amount", "quantity"),
            "wb_ads": ("spend", "orders", "ordered_units", "ordered_amount", "views", "clicks"),
            "ozon_ads": (
                "spend",
                "orders",
                "model_orders",
                "ordered_amount",
                "model_ordered_amount",
                "views",
                "clicks",
            ),
            "ozon_fbo_postings_geo": ("quantity",),
        }[name]
        groups = {}
        keys = ("article", "nm_id", "sku", "currency") + {
            "wb_stocks": ("warehouse", "kind"),
            "wb_paid_storage": ("date", "warehouse_id", "warehouse", "delivery_id"),
            "wb_acceptance_report": ("supply_id", "gi_create_date"),
            "wb_ads": ("campaign_id", "placement", "conversion_type", "payment_type", "date"),
            "ozon_ads": ("campaign_id", "tax_basis", "date"),
            "ozon_fbo_postings_geo": ("date", "warehouse_id", "cluster_from", "cluster_to", "city"),
        }[name]
        for row in items:
            if name == "ozon_fbo_postings_geo" and row.get("created_at"):
                row = dict(row, date=row["created_at"][:10])
            group = tuple(row.get(k) for k in keys)
            if group not in groups:
                groups[group] = {k: Decimal(0) for k in values}
            for k in values:
                n = market_specs.money(row.get(k))
                if n is None or groups[group][k] is None:
                    groups[group][k] = None
                else:
                    groups[group][k] += n
        data["items"] = [dict(zip(keys, group), **totals) for group, totals in groups.items()]
    elif name in ("ozon_reviews", "wb_feedbacks"):
        data["items"] = [
            {
                "reviews_in_page": len(items),
                "rated": sum(r.get("rating") in (1, 2, 3, 4, 5) for r in items),
                "mean_rating": data.get("mean_rating"),
            }
        ]
    elif name in ("wb_orders", "wb_sales"):
        data["items"] = data.get("summary", [])
    elif name == "wb_finance_report" and "summary" in data:
        data["items"] = data["summary"]
        data["cost_coverage"] = "allocated_rows_only"
    elif name in ("wb_supply_goods", "ozon_supply_items"):
        fields = (
            ("quantity", "accepted_quantity", "ready_for_sale_quantity")
            if name == "wb_supply_goods"
            else ("declared_quantity",)
        )
        data["page_quantities"] = {
            field: sum(row[field] for row in items)
            if all(type(row.get(field)) is int for row in items)
            else None
            for field in fields
        }
        data["positions_in_page"] = len(items)
    data.setdefault("summary_scope", "returned_page")


def execute(name, args, creds, directory, deadline):
    market_specs.validate(name, args)
    with localcontext() as ctx:
        ctx.prec = 128
        if name in market_health.COMPOSITE:
            wires = {p: make_wire(p,c,directory,deadline) for p,c in creds.items() if p!="performance"}
            if name in ("unit_economics","sales_drop_check") and "performance" in creds:
                wires["performance"]=Wire("performance",creds["performance"],directory,deadline)
            mapping=_mapping(args,directory)
            if name=="supply_plan":
                data=supply_plan.read(args,wires,mapping)
            else:
                identity=market_composites.identity(args,wires,mapping)
                if name=="article_map" or not identity["complete"]:
                    data=identity
                elif name=="supply_plan":
                    data=supply_plan.read(args,wires,identity["mapping"])
                elif name=="unit_economics":
                    data=market_composites.accounting(args,wires,identity["mapping"],_finance_period,regulated_ozon)
                else:
                    data=market_composites.comparison(args,wires,identity["mapping"])
            used_wires=list(wires.values())
        else:
            p = provider(name)
            account = {k: v for k, v in creds.items() if k != "seller_lookup"}
            wire = make_wire(p, account, directory, deadline)
            used_wires=[wire]
            seller_wire = None
            if name in ("wb_orders","wb_sales","ozon_sku_daily","wb_finance_report"):
                first=date.fromisoformat(args["date_from"])
                last=date.fromisoformat(args["date_to"])
                if (args.get("archive") or (last-first).days>=90 or
                    (p=="wb" and name!="wb_finance_report" and first<datetime.now(market_specs.MOSCOW).date()-timedelta(days=90))):
                    archive=market_history.History(directory)
                    owner=market_history.scope(p,wire.creds)
                    if not archive.authorized(wire.creds,owner):
                        return market_specs.result([],complete=False,data_as_of=None,status="archive_access_not_yet_verified")
                    data=market_history.historical_read(archive,owner,name,args)
                    data.update(tool=name,read_only=True,untrusted_data=True)
                    return data
            try:
                if name in ("wb_supplies","ozon_supply_orders") and "article" in args:
                    import supply_history
                    data=supply_history.report(name,args,wire)
                elif p == "wb":
                    if name in ("wb_acceptance_report","wb_ads","wb_region_sales","wb_returns_report","wb_search_queries","wb_funnel") and "article" in args:
                        if name == "wb_ads" and (
                            not args.get("campaign_ids")
                            or args.get("report") in ("campaigns", "spend_ledger")
                        ):
                            return market_specs.result(
                                [], complete=False, status="campaign_statistics_required_for_article"
                            )
                        try:
                            exact = _wb_identity(wire, args["article"], args.get("nm_id"))
                        except urllib.error.HTTPError as exc:
                            exc.required_category = "Контент"
                            raise
                        if not exact:
                            return market_specs.result(
                                [], complete=False, status="unverified_article_mapping"
                            )
                        args = dict(args, nm_id=exact["nm_id"])
                    if name in ("wb_orders","wb_sales") and (args.get("mode")=="summary" or "group_by" in args) and "cursor" not in args and ("article" in args or "nm_id" in args):
                        data=_wb_daily_totals(name,args,wire)
                    elif (
                        name == "wb_finance_report"
                        and args.get("mode") == "summary"
                        and "cursor" not in args
                    ):
                        data = _finance_period(args, wire)
                    else:
                        source_args = (
                            {k: v for k, v in args.items() if k != "article"} if name == "wb_ads" else args
                        )
                        data = wb_expansion.read(name, source_args, wire)
                        if name in ("wb_acceptance_report", "wb_ads") and "article" in args:
                            for row in data["items"]:
                                if str(row.get("nm_id")) == str(args["nm_id"]):
                                    row["article"] = row["vendor_code"] = args["article"]
                elif p == "performance":
                    seller_wire = (
                        Wire("ozon", creds["seller_lookup"], directory, deadline)
                        if "seller_lookup" in creds
                        else None
                    )
                    selected = None
                    if "article" in args:
                        if not seller_wire:
                            return market_specs.result(
                                [], complete=False, status="seller_catalog_access_required_for_article"
                            )
                        try:
                            selected = set(map(str, ozon_expansion._report_skus(args, seller_wire)))
                        except urllib.error.HTTPError as exc:
                            exc.required_category = "Каталог Ozon Seller"
                            raise
                        if not selected:
                            return market_specs.result(
                                [], complete=False, status="unverified_article_mapping"
                            )
                    data = ozon_performance.read(args, wire, account, allowed_skus=selected)
                    if selected:
                        for row in data["items"]:
                            if str(row.get("sku")) in selected:
                                row["article"] = row["offer_id"] = args["article"]
                else:
                    data = ozon_expansion.read(name, args, wire)
            except urllib.error.HTTPError as exc:
                if exc.code not in (401,402,403):
                    raise
                exc.close()
                category = (
                    wb_expansion.CATEGORIES.get(name)
                    if p == "wb"
                    else (
                        "Ozon Performance"
                        if p == "performance"
                        else "Управление отзывами или Premium Pro"
                        if name == "ozon_reviews"
                        else "Ozon Seller API"
                    )
                )
                return market_specs.result(
                    [],
                    complete=False,
                    status="provider_access_denied",
                    http_status=exc.code,
                    required_category=getattr(exc, "required_category", category),
                    message="Проверьте доступ текущего ключа к категории "
                    + str(getattr(exc, "required_category", category) or "WB API"),
                )
            if name == "ozon_cash_flow" and data.get("rate_limited"):
                delay = data.get("retry_after_seconds")
                raise urllib.error.HTTPError(
                    "",
                    429,
                    "Statement is rate limited",
                    {"Retry-After": str(delay)} if delay is not None else {},
                    None,
                )
            if p != "performance":
                _enrich(name, args, data, wire)
            elif seller_wire:
                _enrich(name, args, data, seller_wire)
            if args.get("include_order_share"):
                _order_share(name, args, data, seller_wire if p == "performance" else wire)
            if name in ("ozon_stocks_by_cluster","ozon_stocks_analytics") and "date_from" not in args:
                start, limit = args.get("offset", 0), args.get("limit", 100)
                items = data["items"]
                data["items"] = items[start : start + limit]
                if start + limit < len(items):
                    data.update(complete=False, next_cursor=start + limit)
            if name == "ozon_sku_daily" and data.get("items"):
                original_deadline = wire.deadline
                try:
                    now = time.monotonic()
                    if original_deadline - now <= 3:
                        raise TimeoutError()
                    wire.deadline = min(original_deadline - 1.5, now + 3)
                    profile = wire("POST", ozon_expansion.BASE + "/v1/seller/info", body={}, ttl=1800)
                    data["company_currency"] = ozon_mcp.project_company_currency(profile, account)
                    data["company_currency_source"] = "/v1/seller/info company.currency"
                except (
                    TimeoutError,
                    urllib.error.URLError,
                    ValueError,
                    finance_gate.GateError,
                    finance_gate.ProviderResponseTooLarge,
                ):
                    data["company_currency"] = None
                finally:
                    wire.deadline = original_deadline
            if args.get("mode") == "summary" and data.get("summary_scope")!="observed_period":
                _summary(name, data)
        stamps=[t for w in used_wires for t in getattr(w,"fetch_times",())]
        fetched=datetime.fromtimestamp(min(stamps),timezone.utc).isoformat() if stamps and None not in stamps else None
        report_stamp = data.get("report_timestamp")
        try:
            reported = datetime.fromisoformat(report_stamp) if isinstance(report_stamp, str) else None
        except ValueError:
            reported = None
        data.setdefault("data_as_of", reported.isoformat() if reported and reported.utcoffset() is not None else fetched)
        data.update(read_only=True, untrusted_data=True, tool=name)
        text = json.dumps(data, ensure_ascii=False, default=str, allow_nan=False)

        def secrets(payload):
            for k, v in payload.items():
                if isinstance(v, dict):
                    yield from secrets(v)
                elif k in ("api_key", "api_token", "client_secret") and isinstance(v, str) and v:
                    yield v

        sensitive = list(secrets(creds))
        if name == "ozon_ads" and getattr(wire, "token", None):
            sensitive.append(wire.token)
        for secret in sensitive:
            if json.dumps(secret, ensure_ascii=False)[1:-1] in text:
                raise finance_gate.ProviderResponseInvalid("Unsafe report response")
        return json.loads(text)
