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


def _seller_mappings(directory):
    # Alias to seller codes is private deployment data. MCP input cannot select a filename.
    file = Path(directory).parent / "supply-articles.json"
    try:
        with file.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(raw) > 65536:
        raise ValueError("Invalid mapping file")
    return json.loads(raw)


def _mapping(args, directory):
    if "mapping" in args:
        return args["mapping"]
    mappings = _seller_mappings(directory)
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

    def recover_interrupted_statistics(self, name="wb_orders"):
        source = self.sources.get("wb_statistics")
        return source.recover_interrupted_statistics(name) if source else super().recover_interrupted_statistics(name)

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
    if name == "ozon_posting_accruals" and args.get("include_types", True):
        # Both reads share the seller's quota. Checkpoint the posting amounts before
        # requesting the dictionary so a short 429 cannot discard or replay that read.
        amounts = regulated_ozon(name, dict(args, include_types=False), wire)
        dictionary = regulated_ozon("ozon_accrual_types", {}, wire)
        return ozon_details.label_posting_accruals(amounts, dictionary)
    if wire.deadline-time.monotonic()<2:
        raise TimeoutError("Insufficient finance read time")
    def read():
        with market_cooldown.worker(wire.gate_dir,wire.creds):
            return portal._provider_read(name,args,wire.creds,absolute_deadline=wire.deadline)
    portal.finance_request_preflight(name,args,wire.creds)
    with wire.progress("composite-finance-v2",[name,args]) as (cached,save):
        if cached: return cached
        one_read = name in ("ozon_realization_article_month", "ozon_accrual_types", "ozon_posting_accruals")
        value=portal._finance_read_with_retry(wire.gate_dir,wire.creds,read,wire.deadline,allow_retry=one_read)
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
            try:
                raw = request("POST", ozon_expansion.BASE + "/v3/product/info/list", body={"sku": subset}, ttl=1800)
                info = ozon_details._products({"skus": subset}, raw, None)
                if info["conflicts"] or info["unmatched_row_indices"]:
                    return
                mapping = {sku: card["offer_id"] for card in info["items"] for sku in card["sku_candidates"]}
            except finance_gate.GateError as exc:
                if exc.reason != "retry_time_unknown":
                    raise
                raw = request("POST", ozon_expansion.BASE + "/v4/product/info/attributes",
                              body={"filter": {"sku": subset, "visibility": "ALL"}, "limit": 100}, ttl=1800)
                mapping = {}
                for card in market_specs.rows(raw.get("result")):
                    sku, offer = str(card.get("sku")), card.get("offer_id")
                    if sku not in subset or not offer or (sku in mapping and offer != mapping[sku]):
                        return
                    mapping[sku] = offer
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


def _finance_by_report(args, wire):
    import market_catalog
    creds = getattr(wire, "category_keys", {}).get("wb_finance", wire.creds)
    src = Wire("wb", creds, wire.gate_dir, wire.deadline)
    start, end = (date.fromisoformat(args[k]) for k in ("date_from", "date_to"))
    monday = start - timedelta(days=start.weekday())
    sunday = min(end + timedelta(days=6-end.weekday()), datetime.now(market_specs.MOSCOW).date())
    def read(op, inputs):
        return market_catalog.transfer("market_api_read", {"operation_id": op, "input": inputs},
                                       creds, src.gate_dir, src.deadline)

    with src.progress("wb-finance-by-report-v1", [args.get("article"), args.get("nm_id"),
                                                  args.get("sku"), start.isoformat(), end.isoformat()]) as (previous, save):
        now = time.time()
        refresh_interval = 86400 if end < datetime.now(market_specs.MOSCOW).date()-timedelta(days=7) else 1800
        initial = {"reports": None, "index": 0, "cursor": 0, "rows": [], "unallocated": 0}
        state = previous or initial
        if state.get("verified_at") and now >= state["verified_at"] + refresh_interval:
            state = dict(initial, last_ready=state["last_ready"])
            save(state, 30*86400)
        def refreshing(status, delay=None):
            snapshot = state.get("last_ready")
            if snapshot:
                return dict(snapshot, refresh_status="pending", refresh_reason=status, retry_after_seconds=delay)
            return market_specs.result([], complete=False, status=status, retry_after_seconds=delay)
        if state["reports"] is None:
            result = market_composites.observed(lambda: read("wb_post_api_finance_sales_reports_list",
                        {"body": {"dateFrom": monday.isoformat(), "dateTo": sunday.isoformat(), "limit": 1000, "period": "weekly"}}))
            if result.get("http_status") not in (200, 204) or "data" not in result:
                return refreshing(result.get("status", "report_list_unavailable"), result.get("retry_after_seconds"))
            records = market_specs.rows(result["data"] or [])
            if len(records) == 1000:
                return market_specs.result([], complete=False, status="report_list_truncated")
            reports = {}
            for row in records:
                rid = row.get("reportId")
                lo, hi = (date.fromisoformat(row[k]) for k in ("dateFrom", "dateTo"))
                if type(rid) is not int or rid <= 0 or hi < lo or rid in reports:
                    raise ValueError("Invalid WB report identity or interval")
                if hi >= start and lo <= end:
                    reports[rid] = [rid, lo.isoformat(), hi.isoformat()]
            state["reports"] = list(reports.values())
            save(state, 30*86400 if state.get("last_ready") else 86400)
        failure = {}
        for _ in range(8):
            if state["index"] == len(state["reports"]) or time.monotonic() + 2 >= src.deadline:
                break
            rid, lo, hi = state["reports"][state["index"]]
            result = market_composites.observed(lambda: read("wb_post_api_finance_sales_reports_detailed_reportid", {
                "path": {"reportId": rid}, "body": {"rrdId": state["cursor"], "limit": 250,
                "fields": sorted(set(wb_expansion.FIN_FIELDS.values()) | {"vendorCode", "sku", "nmId"})}}))
            if result.get("http_status") == 204:
                state.update(index=state["index"]+1, cursor=0)
            elif result.get("http_status") == 200:
                records = market_specs.rows(result["data"])
                cursor = state["cursor"]
                selected = []
                for row in records:
                    if row.get("reportId") != rid or type(row.get("rrdId")) is not int or row["rrdId"] <= cursor:
                        raise ValueError("Unexpected or repeated WB financial row")
                    cursor = row["rrdId"]
                    if start <= date.fromisoformat(row["rrDate"][:10]) <= end:
                        selected.append(row)
                if not records:
                    failure = {"status": "empty_page_without_end_marker"}
                    break
                items, unallocated = wb_expansion.finance_rows(selected, args)
                state["rows"].extend(items)
                state["unallocated"] += len(unallocated)
                state["cursor"] = cursor
            else:
                failure = {k: result[k] for k in ("status", "http_status", "retry_after_seconds") if k in result}
                break
            if len(state["rows"]) > 20000:
                return market_specs.result([], complete=False, status="narrow_period_or_select_article")
            save(state, 30*86400 if state.get("last_ready") else 86400)
        covered = set()
        for _, lo, hi in state["reports"][:state["index"]]:
            covered.update(market_history.days_between(max(start.isoformat(),lo), min(end.isoformat(),hi)))
        wanted = market_history.days_between(start.isoformat(), end.isoformat())
        missing = [d for d in wanted if d not in covered]
        done = state["index"] == len(state["reports"])
        complete = done and not missing
        if complete and state.get("verified_at"):
            return state["last_ready"]
        if not complete and state.get("last_ready"):
            if done:
                state["reports"] = None
                state.update(index=0, cursor=0, rows=[], unallocated=0)
                save(state, 30*86400)
            return refreshing(failure.get("status", "partial_report_coverage" if done else "pending"),
                              failure.get("retry_after_seconds"))
        response = market_specs.result(wb_expansion.finance_summary(state["rows"]), complete=complete,
                    status="ready" if complete else failure.get("status", "partial_report_coverage" if done else "pending"),
                    source="wb_financial_reports_by_id", summary_scope="requested_period" if complete else "observed_rows_only",
                    rows_observed=len(state["rows"]), reports_completed=state["index"], reports_listed=len(state["reports"]),
                    missing_dates=missing, unallocated_rows=state["unallocated"],
                    retry_after_seconds=failure.get("retry_after_seconds"), period_from=start.isoformat(), period_to=end.isoformat())
        if complete:
            response.update(data_as_of=market_history.utc(), refresh_status="ready")
            state.update(verified_at=now, last_ready=response)
            save(state, 30*86400)
        elif done:
            save(state, 1800)
        return response


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
            except finance_gate.GateError as exc:
                if exc.reason != "retry_time_unknown":
                    raise
                return _finance_by_report(args, wire)
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
        # Older saved rows do not distinguish a missing isCancel from false.
        # A newer revision read during rollback must also be verified again.
        known_cancellations = state.setdefault('observed_cancellations', {}) if name == 'wb_orders' else {}
        status="pending"
        delay=None
        for _ in range(12):
            if state["done"] or time.monotonic()+2>=wire.deadline: break
            cursor=state["next"]
            source=dict(query,limit=2000)
            if cursor is not None: source["cursor"]=cursor
            try:
                page=wb_expansion._stats(name,source,wire)
            except finance_gate.GateError as exc:
                if exc.reason not in ('busy', 'cooldown', 'retry_time_unknown'):
                    raise
                status, delay = exc.reason, exc.retry_after_seconds
                break
            except TimeoutError:
                if not state['events']:
                    raise
                status = 'provider_timeout'
                break
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
                if name == 'wb_orders' and type(row.get('cancelled')) is bool:
                    known_cancellations[key] = [value[-1], value[4]]
                else:
                    known_cancellations.pop(key, None)
            marker=page["next_cursor"]
            if page.get("issues") or (marker is not None and marker in state["seen"]):
                status="nonadvancing_source"
                break
            if len(state["events"])>50000: return market_specs.result([],complete=False,status="narrow_period")
            state.update(next=marker,done=page["complete"])
            if marker is not None: state["seen"].append(marker)
            save(state,1800 if state["done"] else 86400)
            if marker is None and not state["done"]:
                status = page.get("status", "incomplete")
                break
        def observed_events():
            for key, (article, day, warehouse, region, event, units, revision) in state['events'].items():
                if name == 'wb_orders' and known_cancellations.get(key) != [revision, event]:
                    event = 'cancellation_unknown'
                yield article, day, warehouse, region, event, units

        if "group_by" in args:
            events = ((a,d,e,q) for a,d,_,_,e,q in observed_events())
            through = min(datetime.now(market_specs.MOSCOW).date().isoformat(), args["date_to"])
            days = market_history.days_between(args["date_from"], through) if state["done"] else []
            items = market_history.grouped_orders(args, events, days)
        else:
            groups={}
            for article,day,warehouse,region,event,units in observed_events():
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
    with wire.progress("ozon-order-denominator-v3", query) as (saved, save):
        state = saved or {
            "next": {"fbo": "", "fbs": ""},
            "ids": {sku: {} for sku in skus},
            "units": {sku: {} for sku in skus},
            "posting_orders": {},
            "seen_cursors": {"fbo":[],"fbs":[]},
            "issue": None,
        }
        for scheme in ("fbo", "fbs"):
            for _ in range(8):
                cursor = state["next"][scheme]
                if cursor is None or state["issue"]:
                    break
                import ozon_posting_status
                body = {"filter": {"since": start, "to": end}, "limit": 100,"cursor":cursor,"sort_dir":"ASC"}
                payload = wire("POST",ozon_posting_status.ENDPOINTS[scheme.upper()],body=body,ttl=1800)
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
                next_cursor = payload.get("cursor")
                if type(more) is not bool or (more and (not postings or not isinstance(next_cursor,str) or next_cursor in ("", cursor))):
                    state["issue"] = "unconfirmed_order_pagination"
                if more and not state["issue"]:
                    history = state["seen_cursors"][scheme]
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
    if data.get("summary_scope") == "report_period":
        return
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
                if name=="sales_drop_check":
                    data=market_composites.verified_comparison(args,wires,mapping,identity)
                elif name=="article_map":
                    data=identity
                else:
                    verified, selectors, missing = market_composites.verified_accounts(args, wires, mapping, identity)
                    catalog_issues = dict(missing)
                    # Historical settlement lines identify the seller's own codes, including retired cards.
                    # Only the private seller mapping can supply a selector when the current catalog is unavailable.
                    if mapping and "mapping" not in args:
                        for p, field in (("ozon", "ozon_offer_id"), ("wb", "wb_vendor_code")):
                            if p in missing and p in wires and mapping.get(field) and (p != "wb" or mapping.get("wb_nm_id")):
                                verified[p], selectors[field] = wires[p], mapping[field]
                                if p == "wb":
                                    selectors["wb_nm_id"] = mapping["wb_nm_id"]
                                elif "performance" in wires:
                                    verified["performance"] = wires["performance"]
                                missing.pop(p)
                    data = market_composites.accounting(args, verified, selectors, _finance_period, regulated_ozon)
                    data.update(report="unit_economics", mapping=selectors or None, unavailable_marketplaces=missing)
                    data["catalog_verification_issues"] = catalog_issues
                    data["mapping_basis"] = "seller_configured_codes_and_financial_row_identifiers" if catalog_issues and selectors and mapping and "mapping" not in args else "verified_catalogs"
                    if missing:
                        data["status"] = "partial_catalog_identity" if verified else "catalog_identity_unavailable"
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
                elif name == "ozon_sku_daily":
                    try:
                        data = ozon_expansion.read(name, args, wire)
                    except (finance_gate.GateError, urllib.error.HTTPError) as exc:
                        if isinstance(exc, urllib.error.HTTPError) and exc.code != 429:
                            raise
                        history = market_history.History(directory)
                        owner = market_history.scope("ozon", wire.creds)
                        if not history.authorized(wire.creds, owner):
                            raise
                        archived = market_history.historical_read(history, owner, name, args)
                        if not archived["archived_days"] and not archived.get("posting_archive_days"):
                            raise
                        if isinstance(exc, urllib.error.HTTPError):
                            exc.close()
                        archived.update(live_analytics_status=exc.reason if isinstance(exc, finance_gate.GateError) else "rate_limited",
                                        read_only=True, tool=name, untrusted_data=True)
                        return archived
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
