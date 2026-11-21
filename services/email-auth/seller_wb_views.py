"""Wildberries report periods, product identity, warehouse observations and currencies."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
import sqlite3
import time

import market_history
import seller_reports
from market_specs import MOSCOW
from seller_views import SourceIssue, num, spans

STOCK="wb_inventory_reports"
FIN="wb_settlement_reports"
INFO="wb_seller_information"
CARD="wb_product_reports"
PROMO="wb_promotion_reports"
WB_AGGREGATE_NAMES = frozenset(("склад wb рф", "склад wb", "склады wb рф"))


def _nm(r):
    return int(next(iter(r.ids))) if len(r.ids)==1 else None


def _history(r,dataset):
    return market_history.History(r.directory).verified_days("wb",r.wire.creds,dataset,r.first.isoformat(),r.last.isoformat())


def _inventory(r):
    today=datetime.now(MOSCOW).date().isoformat()
    nm=_nm(r); history=_history(r,"wb_warehouse_daily")
    office_dataset="wb_warehouse_offices_nm_"+str(nm) if nm else None
    office_history=_history(r,office_dataset) if office_dataset else {}
    current_offices_stamp=None
    if today not in history and r.first.isoformat()<=today<=r.last.isoformat():
        params={"nmIds":[nm]} if nm else {}
        current=list(r.pages(STOCK,"stock_now",{"body":{"limit":1000,"offset":0,**params}},"data.items",("body","offset",None,1000)))
        stamp=r.stamps[-1] if r.stamps else None
        observed_day=datetime.fromisoformat(stamp).astimezone(MOSCOW).date().isoformat() if stamp else None
        if observed_day == today:
            history[today]=(stamp,current)
    rows=[]
    for day,(stamp,positions) in sorted(history.items()):
        if stamp:r.stamps.append(stamp)
        for p in positions:
            rows.extend(r.product({"date":day,"warehouse_id":p.get("warehouseId"),"warehouse":p.get("warehouseName"),
                "size_id":p.get("chrtId"),"stock":int(p["quantity"]),"to_buyer":p.get("inWayToClient"),
                "from_buyer":p.get("inWayFromClient"),"basis":"observed_snapshot","observed_at":stamp},p.get("vendorCode"),p.get("nmId")))
    current_rows=[row for row in rows if row["date"]==today]
    articles={row.get("article") for row in current_rows}
    if (nm and current_rows and len(articles)==1 and None not in articles and
            all(str(row.get("warehouse") or "").casefold() in WB_AGGREGATE_NAMES for row in current_rows)):
        article=next(iter(articles))
        def offices():
            params={"currentPeriod":{"start":today,"end":today},"stockType":"wb","skipDeletedNm":False,"nmIDs":[nm]}
            for region in r.fetch(STOCK,"offices",{"body":params})["data"]["regions"]:
                if not isinstance(region.get("offices"),list):raise ValueError("data.regions.offices")
                region_count=region["metrics"].get("stockCount")
                if type(region_count) is not int or region_count<0:raise ValueError("region.stockCount")
                if not region["offices"]:
                    m=region["metrics"]
                    if m["stockCount"]:
                        yield dict(date=today,warehouse_id=None,warehouse=region["regionName"],article=article,nm_id=nm,
                                   stock=m["stockCount"],to_buyer=m.get("toClientCount"),from_buyer=m.get("fromClientCount"),
                                   basis="current_region_aggregate")
                office_total=0
                for office in region["offices"]:
                    m=office["metrics"]
                    if type(office.get("officeID")) is not int or not isinstance(office.get("officeName"),str) or not office["officeName"]:
                        raise ValueError("office.identity")
                    if type(m.get("stockCount")) is not int or m["stockCount"]<0:raise ValueError("office.stockCount")
                    office_total+=m["stockCount"]
                    aggregate = office["officeID"] <= 0 or office["officeName"].casefold() in WB_AGGREGATE_NAMES
                    yield dict(date=today,warehouse_id=None if aggregate else office["officeID"],warehouse=office["officeName"],
                               article=article,nm_id=nm,stock=m["stockCount"],to_buyer=m.get("toClientCount"),
                               from_buyer=m.get("fromClientCount"),basis="current_region_aggregate" if aggregate else "current_office_metrics")
                if region["offices"] and office_total!=region_count:raise ValueError("region.stockCount mismatch")
        old_issues=len(r.issues)
        old_stamps=len(r.stamps)
        detail=r.section("offices",offices)
        office_ids=[d["warehouse_id"] for d in detail if d["warehouse_id"] is not None]
        if len(office_ids)!=len(set(office_ids)):
            r.issues.append({"source":"offices","status":"provider_response_invalid","field":"officeID"})
        if detail and len(r.issues)==old_issues:
            aggregate=sum(d["stock"] for d in current_rows)
            detailed=sum(d["stock"] for d in detail)
            r.meta["warehouse_snapshot_comparison"]={"stock_now":aggregate,"offices":detailed,
                "totals_match":aggregate==detailed,"note":"Источники обновляются с разной периодичностью."}
            current_offices_stamp=r.stamps[-1] if len(r.stamps)>old_stamps else None
            if current_offices_stamp:
                for d in detail:d["observed_at"]=current_offices_stamp
            r.meta["office_snapshot_source"]="current_response"
            rows=[d for d in rows if d["date"]!=today]+detail
            r.meta["office_detail_status"] = "reported" if office_ids else "provider_aggregate"
            # Save only an authenticated, dated observation of today's complete response.
            if office_ids:
                _save_offices(r, office_dataset, today, current_offices_stamp, detail)
        elif len(r.issues)==old_issues:
            r.issues.append({"source":"offices","status":"warehouse_detail_unavailable"})
        r.meta["office_data_scope"]="current_day_only"
        r.meta["office_refresh_interval_hours"]=2
        r.meta["warehouse_reporting_notice"]="https://dev.wildberries.ru/release-notes?id=605"
    # Prefer the most recent saved office observation to an account-wide aggregate.
    observed_office_days=set()
    for day,(stamp,positions) in sorted(office_history.items()):
        if day==today and current_offices_stamp:
            try:
                saved_at=datetime.fromisoformat(stamp.replace("Z","+00:00"))
                current_at=datetime.fromisoformat(current_offices_stamp.replace("Z","+00:00"))
                if saved_at.utcoffset() is None or current_at.utcoffset() is None or saved_at<=current_at:continue
            except (TypeError,ValueError,AttributeError):continue
        selected=[]
        for position in positions:
            selected.extend(r.product(dict(position, observed_at=position.get("observed_at") or stamp),
                                      position.get("article"),position.get("nm_id")))
        if selected:
            if stamp:r.stamps.append(stamp)
            rows=[row for row in rows if row["date"]!=day]+selected
            observed_office_days.add(day)
            if day==today:r.meta["office_snapshot_source"]="newer_saved_observation"
    if observed_office_days or r.meta.get("office_snapshot_archived"):
        r.meta["office_history_basis"]="observed_snapshots_only_no_backfill"
    observation_dates=set(history)|observed_office_days
    if r.args.get("download_id") and r.article and nm:
        archive=seller_reports.stock_csv(r.args["download_id"],r.article,nm,r.first.isoformat(),r.last.isoformat(),r.wire)
        if archive.get("data_as_of"):r.stamps.append(archive["data_as_of"])
        for office,days in archive["offices"].items():
            archive_id=None
            if r.args.get("warehouse_id"):
                matching={str(line["warehouse_id"]) for line in rows if line.get("warehouse")==office and line.get("warehouse_id") is not None}
                if matching!={str(r.args["warehouse_id"])}:continue
                archive_id=r.args["warehouse_id"]
            for d,stock in days.items():
                if d not in history and stock is not None:
                    rows.append(dict(date=d,warehouse=office,warehouse_id=archive_id,article=r.article,nm_id=nm,
                                     stock=stock,basis="23_59_moscow",observed_at=archive.get("data_as_of")))
                    observation_dates.add(d)
    if r.args.get("warehouse_id"):
        if any(row.get("warehouse_id") is None for row in rows):
            r.issues.append({"source":"warehouse_filter","status":"aggregate_not_attributable"})
        rows=[row for row in rows if str(row.get("warehouse_id"))==str(r.args["warehouse_id"])]
        # An account-wide snapshot does not establish an observation for this warehouse.
        observation_dates={row["date"] for row in rows}
    r.meta["observed_days"]=len(observation_dates)
    r.meta["missing_dates"]=[(r.first+timedelta(days=i)).isoformat() for i in range((r.last-r.first).days+1)
                            if (r.first+timedelta(days=i)).isoformat() not in observation_dates]
    if r.meta["missing_dates"]:r.issues.append({"source":"daily_inventory_archive","status":"history_not_observed"})
    if rows:
        last=max(row["date"] for row in rows)
        closing=[row for row in rows if row["date"]==last]
        r.meta["closing_observed_stock"]={"date":last,"quantity":sum(row["stock"] for row in closing),
            "observed_at":min((row["observed_at"] for row in closing if row.get("observed_at")),default=None)}
        aggregate=[x for x in rows if x.get("basis")=="current_region_aggregate" or str(x.get("warehouse") or "").casefold() in WB_AGGREGATE_NAMES]
        r.meta["warehouse_scope"]="provider_aggregate" if len(aggregate)==len(rows) else "mixed_aggregate_and_warehouses" if aggregate else "reported_warehouses"
    if nm and r.article:
        evidence=seller_reports.availability_days(r.article,nm,r.first.isoformat(),min(r.last,datetime.now(MOSCOW).date()-timedelta(days=1)).isoformat(),r.wire) if r.first < datetime.now(MOSCOW).date() else {}
        r.meta["stockout_days"]=evidence.get("whole_day_stockouts",[])
        r.meta["stockout_coverage_days"]=len(evidence.get("known_days",[]))
    if r.args.get("group_by")=="warehouse":
        grouped={}
        for d in rows:
            key=(d["article"],d.get("warehouse_id"),d.get("warehouse"))
            values=grouped.setdefault(key,{})
            values.setdefault(d["date"],[]).append(d)
        for (article,wid,office),values in grouped.items():
            day=max(values)
            positions=values[day]
            def total(field):
                vals=[p.get(field) for p in positions]
                return sum(vals) if all(type(v) is int for v in vals) else None
            yield {"article":article,**({"nm_id":nm} if nm else {}),"warehouse_id":wid,"warehouse":office,"stock":total("stock"),"date":day,
                   "observed_at":min((p["observed_at"] for p in positions if p.get("observed_at")),default=None),
                   "to_buyer":total("to_buyer"),"from_buyer":total("from_buyer"),
                   "observed_days":len(values),"zero_on_observation_dates":sorted(d for d,rs in values.items() if sum(r["stock"] for r in rs)==0)}
    else:yield from rows


def _save_offices(reader, dataset, today, stamp, rows):
    try:
        observed = datetime.fromisoformat(stamp.replace("Z","+00:00")) if stamp else None
        creds = reader.wire.creds
        analytics_creds = getattr(reader.wire,"category_keys",{}).get("wb_analytics",creds)
        owner = market_history.scope("wb",creds)
        if (not observed or observed.utcoffset() is None or observed.astimezone(MOSCOW).date().isoformat() != today
                or market_history.scope("wb",analytics_creds) != owner):
            raise ValueError("unverified observation identity or timestamp")
        archive = market_history.History(reader.directory)
        archive.save_day(owner,dataset,today,rows,stamp)
        archive.confirm(creds,owner)
        reader.meta["office_snapshot_archived"] = True
    except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, AttributeError):
        reader.meta["office_snapshot_archived"] = False
        reader.issues.append({"source":"offices_history","status":"observation_not_saved"})


def _tariff_number(value):
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]+)?", value):
            return value
        value = value.translate(str.maketrans({",": ".", " ": "", "\u00a0": "", "\u202f": ""}))
    return num(value) if type(value) in (str, int, float, Decimal) else value


def _tariffs(r):
    kind=r.args.get("kind","return");day=r.args.get("date",r.first.isoformat())
    office=r.args.get("warehouse")
    transit=r.fetch("wb_tariff_reports","transit",ttl=86400) if kind=="transit" else None
    if r.args.get("warehouse_id"):
        from wb_expansion import _warehouse_directory, verified_supply_warehouse_name
        ident = r.args["warehouse_id"]
        directory = _warehouse_directory({"include_warehouses": True, "warehouse_id": ident}, r.wire, {}, required=True)
        names = {w["name"].strip() for w in directory.get("warehouses", []) if isinstance(w.get("name"), str) and w["name"].strip()}
        if len(names) > 1:
            raise SourceIssue(source="/api/v1/warehouses", status="warehouse_name_ambiguous", warehouse_id=ident)
        confirmed = next(iter(names), None)
        if confirmed is None:
            confirmed = verified_supply_warehouse_name(r.wire, ident)
            if not confirmed:
                code = directory.get("warehouse_catalog_http_status")
                raise SourceIssue(source="/api/v1/warehouses", status="provider_access_denied" if code in (401,403)
                                  else "warehouse_name_unavailable", http_status=code, required_category="Поставки", warehouse_id=ident)
            r.meta["warehouse_name_source"] = "verified_supply_office_id"
        if office and office.strip().casefold() != confirmed.casefold():
            raise SourceIssue(source="warehouse_id", status="warehouse_name_mismatch", warehouse_id=ident)
        office = confirmed
    if transit is not None:
        for row in transit:
            if not office or office in (row.get("transitWarehouseName"),row.get("destinationWarehouseName")):
                yield dict(scope="warehouse",kind=kind,date=row.get("activeFrom"),**row)
        return
    raw=r.fetch("wb_tariff_reports","returns" if kind=="return" else "pallets",{"query":{"date":day}},86400)["response"]["data"]
    for d in raw["warehouseList"]:
        if office and office.casefold()!=str(d.get("warehouseName","")).casefold():continue
        yield {"scope":"warehouse","warehouse":d["warehouseName"],"kind":kind,"effective_on":day,"currency":raw.get("currency"),
               "tariffs":{k:_tariff_number(v) if k.startswith(("deliveryDump","palletDelivery","palletStorage")) else v
                          for k,v in d.items() if k!="warehouseName"}}


def _balance(r):
    def balance():
        d=r.fetch(FIN,"balance")
        yield {"scope":"account","kind":"balance","current":num(d["current"]),"available_to_withdraw":num(d["for_withdraw"]),"currency":d["currency"]}
    yield from r.section("balance",balance)
    if "date_from" in r.args:
        for a,b in spans(r.first,r.last,31):
            for d in r.pages(FIN,"acquiring",{"body":{"dateFrom":a.isoformat(),"dateTo":b.isoformat(),"limit":100,"offset":0}},"",("body","offset",None,100)):
                yield {"scope":"account","kind":"acquiring","date_from":d.get("dateFrom"),"date_to":d.get("dateTo"),"report_id":d.get("reportId"),
                       "amount":num(d["acquiringFeeSum"]),"vat":num(d["acquiringFeeVatSum"]) if d.get("acquiringFeeVatSum") is not None else None,"currency":d.get("currency")}


def _funnel(r):
    import wb_expansion
    nm=_nm(r)
    if not nm or not r.article:raise SourceIssue(source="product_identity",status="product_selector_required")
    dates=market_history.days_between(r.first.isoformat(),r.last.isoformat())
    history=_history(r,"wb_funnel_daily")
    product_days=_history(r,"wb_funnel_nm_"+str(nm))
    observed={**product_days,**history}
    rows={}
    for d,(stamp,items) in observed.items():
        rows[d]=[dict(item,date=d) for item in items if str(item.get("nm_id"))==str(nm) and item.get("article")==r.article]
        if stamp:r.stamps.append(stamp)
    missing=[day for day in dates if day not in rows]
    archive=market_history.History(r.directory)
    owner=market_history.scope("wb",r.wire.creds)
    download=r.args.get("download_id")
    if missing and not download:
        listing=r.section("csv_directory",lambda:r.fetch("wb_csv_reports","list").get("data") or [])
        # WB does not return nmIDs or report type in this listing. Inspect the CSV header and rows.
        files=[row["id"] for row in listing if row.get("status")=="SUCCESS" and str(row.get("startDate",""))[:10]<=r.first.isoformat()
               and str(row.get("endDate",""))[:10]>=r.last.isoformat()]
    else:files=[download] if download else []
    for file in files:
        if not missing:break
        try:
            args={"date_from":r.first.isoformat(),"date_to":r.last.isoformat(),"nm_id":nm,"download_id":file,"limit":100}
            seen=set();parsed={}
            while True:
                page=seller_reports.funnel_csv(args,r.wire)
                if page.get("data_as_of"):r.stamps.append(page["data_as_of"])
                for item in page["items"]:
                    if item["date"] in parsed:raise ValueError("Repeated daily product in CSV")
                    parsed[item["date"]]=dict(item,article=r.article)
                if page["complete"]:break
                cur=page["next_cursor"]
                if cur in seen:raise ValueError("Repeated CSV cursor")
                seen.add(cur);args["cursor"]=cur
            for d in missing:
                if d in parsed:
                    stamp=page.get("data_as_of") or market_history.utc()
                    rows[d]=[parsed[d]]
                    archive.save_day(owner,"wb_funnel_nm_"+str(nm),d,rows[d],stamp);archive.confirm(r.wire.creds,owner)
            missing=[d for d in missing if d not in rows]
        except (ValueError,KeyError):
            if download:raise
    for d in missing:
        if time.monotonic()+2>=r.deadline:
            r.issues.append({"source":"daily_funnel","status":"pending","reason":"request_deadline"});break
        try:
            page=wb_expansion._funnel({"date_from":d,"date_to":d,"nm_id":nm,"article":r.article,"limit":100},r.wire)
            if not page["complete"]:raise SourceIssue(source="daily_funnel",status="pending")
            rows[d]=[dict(item,date=d) for item in page["items"]]
            stamp=market_history.utc();r.stamps.append(stamp)
            archive.save_day(owner,"wb_funnel_nm_"+str(nm),d,rows[d],stamp);archive.confirm(r.wire.creds,owner)
        except Exception as exc:
            import finance_gate, urllib.error
            if isinstance(exc,urllib.error.HTTPError):
                delay=finance_gate.parse_retry_after(exc.headers)[0];code=exc.code;exc.close()
                r.issues.append({"source":"daily_funnel","status":"rate_limited" if code==429 else "provider_error","http_status":code,"retry_after_seconds":delay})
            elif isinstance(exc,finance_gate.GateError):r.issues.append({"source":"daily_funnel","status":"pending","reason":"local_limiter","http_status":None,"retry_after_seconds":exc.retry_after_seconds})
            else:raise
            break
    r.meta.update(verified_days=len(rows),requested_days=len(dates),source_basis="verified_csv_or_single_day_aggregate",missing_dates=[d for d in dates if d not in rows])
    totals={}
    if len(rows)==len(dates):
        flat=[item for bucket in rows.values() for item in bucket]
        for metric in ("open_count","cart_count","order_count","buyout_count"):
            totals[metric]=sum(item[metric] for item in flat) if all(type(item.get(metric)) is int for item in flat) else None
    r.meta["period_totals"]=totals or None
    for day in sorted(rows):yield from rows[day]


def _card_issues(r):
    nm=_nm(r);counts={}
    for choice in ("blocked","item_rating","price_quarantine","content_recommendations"):
        def collect():
            if choice=="blocked":
                batch=r.fetch(CARD,choice,{"query":{"sort":"nmId","order":"asc"}})["report"]
            elif choice=="item_rating":
                today = datetime.now(MOSCOW).date()
                if r.first > today:
                    raise SourceIssue(source="item_rating", status="rating_period_not_started")
                end = min(r.last, today-timedelta(days=1))
                start = min(r.first, end)
                period = {"start":start.isoformat(), "end":end.isoformat()}
                r.meta["rating_period"] = period
                if (start, end) != (r.first, r.last):
                    r.meta["rating_period_requested"] = {"start":r.first.isoformat(), "end":r.last.isoformat()}
                body={"currentPeriod":period,"offset":0,"limit":100,
                      "orderBy":{"field":"feedbackRating","mode":"desc"},**({"nmIds":[nm]} if nm else {})}
                batch=r.pages(CARD,choice,{"body":body},"data.items",("body","offset",None,100))
            elif choice=="price_quarantine":
                raw=r.fetch(CARD,choice,{"query":{"limit":1000,"offset":0}})
                batch=(raw.get("data") or {}).get("quarantineGoods",[])
                if len(batch)==1000:batch=r.pages(CARD,choice,{"query":{"limit":1000,"offset":0}},"data.quarantineGoods",("query","offset",None,1000))
            else:
                body = {"limit":100}
                if nm:
                    body["search"] = str(nm)
                batch=r.pages(CARD,choice,{"body":body},"data",("body","next","next",100))
            count=0;shadow=0
            for d in batch:
                if choice=="item_rating" and type(d.get("isShadowed")) is not bool:
                    raise ValueError("data.items.isShadowed")
                ident=d.get("nmId",d.get("nmID"))
                row=r.product({"kind":choice,**({"currency":d.get("currencyIsoCode4217")} if choice=="price_quarantine" else {}),
                              **{k:d[k] for k in ("reason","isShadowed","rating","feedbackRating","recomNms","recomCount","newPrice","oldPrice") if k in d}},
                              d.get("vendorCode"),ident)
                count+=len(row);shadow+=int(bool(row and d.get("isShadowed")))
                yield from row
            counts[choice]=count
            if choice=="item_rating":counts["shadowed"]=shadow
        before=len(r.issues)
        yield from r.section(choice,collect)
        for issue in r.issues[before:]:
            if choice=="item_rating" and issue.get("http_status")==400:
                issue.update(reason="provider_rejected_request",period=r.meta.get("rating_period"),
                             documentation="https://dev.wildberries.ru/docs/openapi/analytics#tag/Ocenka-tovara/operation/postV2ItemRating")
    r.meta["counts"]=counts


def _promotions(r):
    start,end=r.first.isoformat()+"T00:00:00Z",r.last.isoformat()+"T23:59:59Z"
    for p in r.pages(PROMO,"promotions",{"query":{"startDateTime":start,"endDateTime":end,"allPromo":True,"limit":100,"offset":0}},
                     "data.promotions",("query","offset",None,100)):
        pid=p["id"]
        base={"promotion_id":pid,"name":p.get("name"),"date_from":p.get("startDateTime"),"date_to":p.get("endDateTime"),"type":p.get("type")}
        if not r.ids:
            yield dict(base,scope="promotion")
            continue
        if p.get("type")!="regular":continue
        for state in ([r.args["in_action"]] if "in_action" in r.args else [True,False]):
            def members():
                q={"query":{"promotionID":pid,"inAction":state,"limit":1000,"offset":0}}
                for d in r.pages(PROMO,"products",q,"data.nomenclatures",("query","offset",None,1000)):
                    yield from r.product(dict(base,in_action=state,plan_price=d.get("planPrice"),currency=d.get("currencyCode")),d.get("vendorCode"),d.get("id",d.get("nmId")))
            yield from r.section("promotion_products",members)


def _chat_timestamp(value):
    if type(value) is not int:
        return None
    try:
        stamp=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=value)
    except OverflowError:
        return None
    return stamp.isoformat(timespec="milliseconds").replace("+00:00","Z")


def _questions(r):
    nm=_nm(r)
    def questions():
        q={"isAnswered":False,"take":100,"skip":0,**({"nmId":nm} if nm else {})}
        for d in r.pages(INFO,"questions",{"query":q},"data.questions",("query","skip",None,100)):
            product=d.get("productDetails") or {}
            yield from r.product({"kind":"question","id":d["id"],"date":d.get("createdDate"),"text":d.get("text"),"answered":bool(d.get("answer"))},
                                 product.get("supplierArticle"),product.get("nmId"))
    yield from r.section("questions",questions)
    for d in r.section("unread",lambda:[r.fetch(INFO,"unread")["data"]]):r.meta["unread"]=d
    def chats():
        for d in r.fetch(INFO,"chats")["result"] or []:
            last=d.get("lastMessage") or {}
            row={"kind":"chat","chat_id":d.get("chatID"),"last_message_at":_chat_timestamp(last.get("addTimestamp"))}
            item=(d.get("goodCard") or {}).get("nmID")
            if item:yield from r.product(row,None,item)
            elif not r.ids:yield dict(row,scope="chat")
    yield from r.section("chats",chats)


class _NewsText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
        self.hidden=None

    def handle_starttag(self, tag, attrs):
        if tag in ("script","style"):
            self.hidden=tag
        elif tag in ("br","p","li","div"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag==self.hidden:
            self.hidden=None
        elif tag in ("p","li","div"):
            self.parts.append(" ")

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(data)


def _news_text(value):
    reader=_NewsText()
    reader.feed(value or "")
    reader.close()
    return " ".join(re.sub(r"https?://\S+"," ","".join(reader.parts),flags=re.IGNORECASE).split())


def _seller_info(r):
    def rating():
        raw=r.fetch(INFO,"seller_rating")
        yield {"scope":"account","kind":"seller_rating","valuation":raw.get("valuation"),"feedback_count":raw.get("feedbackCount")}
    yield from r.section("seller_rating",rating)
    query=" ".join(r.args.get("query","").split())
    word=re.compile(r"(?<!\w)"+re.escape(query)+r"(?!\w)",re.IGNORECASE) if query else None
    def news():
        for d in r.fetch(INFO,"news",{"query":{"from":r.first.isoformat()}})["data"] or []:
            if not r.first.isoformat()<=str(d.get("date",""))[:10]<=r.last.isoformat():
                continue
            row={"scope":"portal","kind":"news","date":d["date"],"header":d.get("header"),"news_id":d.get("id")}
            if word and not word.search(_news_text(d.get("header"))):
                content=_news_text(d.get("content"))
                match=word.search(content)
                if not match:
                    continue
                row["content_excerpt"]=content[max(0,match.start()-50):match.end()+90]
            yield row
    yield from r.section("news",news)
    yield from r.section("documents",lambda:(dict(kind="document",scope="account",**d) for d in r.pages(FIN,"documents",{"query":{
        "beginTime":r.first.isoformat(),"endTime":r.last.isoformat(),"limit":50,"offset":0}},"data.documents",("query","offset",None,50))))


def retentions(r):
    selected=r.args.get("retention_type","all")
    for kind,choice in (("measurement_penalty","measurement_penalties"),("measurement","warehouse_measurements"),("deduction","deductions"),("antifraud","antifraud")):
        if selected not in ("all",kind):continue
        def collect():
            start = r.first-timedelta(days=r.first.weekday()) if kind=="antifraud" else r.first
            periods=spans(start,r.last,7 if kind=="antifraud" else 31)
            for first,last in periods:
                if kind=="antifraud":
                    # WB selects a week by a day from that week.
                    data=r.fetch(FIN,choice,{"query":{"date":first.isoformat()}},86400)
                    records=data.get("details") or []
                else:
                    params={"dateFrom":first.isoformat()+"T00:00:00Z","dateTo":last.isoformat()+"T23:59:59Z","limit":100,"offset":0}
                    records=r.pages(FIN,choice,{"query":params},"data.reports",("query","offset",None,100))
                for d in records:
                    stamp=d.get("dtBonus") or d.get("dt") or d.get("dateFrom")
                    end=d.get("dateTo") or stamp
                    if str(end)[:10]<r.first.isoformat() or str(stamp)[:10]>r.last.isoformat():continue
                    amount=d.get({"measurement_penalty":"penaltyAmount","deduction":"bonusSumm","antifraud":"sum","measurement":"amount"}[kind])
                    row={"date":stamp,"date_to":end,"kind":kind,"reason":d.get("bonusType") or kind,
                         "amount":num(amount) if amount is not None else None,"currency":d.get("currency"),
                         "measurement_id":d.get("dimId"),"supply_id":d.get("supplyId"),"source":choice}
                    yield from r.product(row,None,d.get("nmId",d.get("nmID")))
        yield from r.section(choice,collect)


def advertising(r):
    import wb_expansion
    nm=_nm(r);args=r.args;report=args.get("report","auto")
    if report=="balance":
        d=r.fetch(PROMO,"ad_balance")
        yield {"scope":"advertising_account","balance":num(d["balance"]),"net":num(d["net"]),"currency":d.get("currency")}
        return
    if r.article and not nm:
        from expansion_gateway import _wb_identity
        ident=_wb_identity(r.wire,article=r.article)
        if ident:nm=int(ident["nm_id"]);r.ids={str(nm)}
    ids=list(map(int,args.get("campaign_ids",[])))
    if not ids and nm:
        cursor=""
        while True:
            page=r.cached("wb_get_api_advert_v3_adverts",{"query":{"limit":50,**({"next":cursor} if cursor else {})}})
            ids.extend(int(i["advertId"]) for i in page["adverts"] if any(str(n.get("nmId"))==str(nm) for n in i.get("nms",[])))
            following=page.get("next")
            if not following:break
            if following==cursor:raise ValueError("Advertising next")
            cursor=following
    ids=list(dict.fromkeys(ids))
    r.meta["matched_campaign_ids"]=ids
    if not ids:return
    if report in ("bids","clusters"):
        if not nm:raise SourceIssue(source="campaign_products",status="product_selector_required")
        for cid in ids:
            key={"advertId":cid,"nmId":nm}
            if report=="bids":
                d=r.fetch(PROMO,"bid_recommendations",{"query":key})
                yield from r.product({"campaign_id":cid,"kind":"bids","base":d.get("base"),"norm_queries":d.get("normQueries")},None,nm)
            else:
                for a,b in spans(r.first,r.last,7):
                    raw=r.fetch(PROMO,"search_clusters",{"body":{"from":a.isoformat(),"to":b.isoformat(),"items":[key]}})
                    for item in raw.get("items") or []:
                        for day in item.get("dailyStats") or []:
                            yield from r.product({"campaign_id":cid,"date":day["date"],"statistics":day["stat"]},None,nm)
        return
    for idx in range(0,len(ids),50):
        page=wb_expansion._ads(dict(date_from=r.first.isoformat(),date_to=r.last.isoformat(),campaign_ids=ids[idx:idx+50],
                                    report="statistics",limit=100,**({"nm_id":nm} if nm else {})),r.wire)
        while True:
            for row in page["items"]:
                yield from r.product(row,row.get("article"),row.get("nm_id"))
            if page["complete"] or page.get("next_cursor") is None:break
            page=wb_expansion._ads(dict(date_from=r.first.isoformat(),date_to=r.last.isoformat(),campaign_ids=ids[idx:idx+50],
                                  report="statistics",limit=100,offset=page["next_cursor"],**({"nm_id":nm} if nm else {})),r.wire)


def read(r):
    import expansion_gateway
    if (r.article or r.ids) and (not r.article or not r.ids):
        identity=expansion_gateway._wb_identity(r.wire,r.article,_nm(r))
        if not identity:raise SourceIssue(status="product_identity_unavailable",source="wb_products")
        r.article=identity["article"];r.ids={str(identity["nm_id"])}
    if r.name=="wb_claims":
        for archived in (False,True):
            query={"is_archive":archived,"limit":100,"offset":0,**({"nm_id":_nm(r)} if _nm(r) else {})}
            for d in r.pages(INFO,"return_claims",{"query":query},"claims",("query","offset",None,100)):
                day=str(d.get("dt",d.get("createdAt","")))[:10]
                if r.first.isoformat()<=day<=r.last.isoformat():
                    yield from r.product({"id":d.get("id"),"date":day,"status":d.get("status"),"reason":d.get("user_comment"),"archived":archived},
                                         d.get("vendor_code"),d.get("nm_id"))
        return
    fn={"wb_stocks_history":_inventory,"wb_tariffs":_tariffs,"wb_balance":_balance,"wb_funnel_history":_funnel,
        "wb_card_issues":_card_issues,"wb_promotions":_promotions,"wb_questions":_questions,"wb_seller_info":_seller_info}[r.name]
    yield from fn(r)
