"""Typed read-only seller tools and their credential-scoped source pages."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
import time
import urllib.error

import finance_gate
import seller_reports


VIEWS = {
    "wb_stocks_history": ("Дневные наблюдения остатков FBW по складам, транзит и покрытие истории.", "nm_id article warehouse_id date_from date_to group_by download_id", {}),
    "wb_tariffs": ("Тариф возврата, паллеты или транзита по дате и складу.", "date warehouse warehouse_id kind", {"kind":["return","pallet","transit"]}),
    "wb_balance": ("Баланс WB и эквайринг за выбранный период.", "date_from date_to", {}),
    "wb_funnel_history": ("Дневная воронка по готовому CSV, архиву и календарным дням WB.", "nm_id article date_from date_to download_id", {}),
    "wb_card_issues": ("Блокировки, скрытие, карантин цен, рейтинг и перекрёстные рекомендации WB.", "nm_id article date_from date_to", {}),
    "wb_promotions": ("Календарь акций и участие товара WB.", "nm_id article date_from date_to in_action", {}),
    "wb_questions": ("Вопросы покупателей, наличие новых вопросов и отзывов, сведения о чатах WB.", "nm_id article date_from date_to", {}),
    "wb_claims": ("Заявки покупателей WB на возврат по периодам и товарам.", "nm_id article date_from date_to", {}),
    "wb_seller_info": ("Рейтинг продавца, новости с фильтром по слову и закрывающие документы WB.", "date_from date_to query", {}),
    "ozon_compensations": ("Готовые отчёты о компенсациях и декомпенсациях за месяц.", "article sku date_from date_to month", {}),
    "ozon_buyout": ("Товары, выкупленные самим Ozon, по данным УПД.", "article sku date_from date_to month", {}),
    "ozon_balance": ("Баланс Ozon, выплаты и движение денег с валютой. Данные бета-метода.", "date_from date_to month", {}),
    "ozon_wrong_volume": ("Измеренные Ozon габариты и вес товаров с расхождением.", "article sku", {}),
    "ozon_supply_acts": ("Заявленное и принятое по SKU, расхождения и причины в актах поставок.", "order_id supply_id article sku date_from date_to", {}),
    "ozon_removals": ("Вывоз товара и списания по дате, SKU и артикулу.", "article sku date_from date_to kind", {"kind":["from_stock","from_supply","decommissioned","all"]}),
    "ozon_promotions": ("Акции Ozon, товары-участники, кандидаты и цены.", "article sku date_from date_to", {}),
    "ozon_competitor_prices": ("Ценовые стратегии, привязка товара и цена конкурента.", "article sku", {}),
    "ozon_card_quality": ("Контент-рейтинг, его составляющие и видимость SKU на витринах.", "article sku", {}),
    "ozon_supply_slots": ("Загрузка складов, зоны размещения и доступные слоты по заявке.", "article sku order_id cluster_id", {}),
    "ozon_questions": ("Вопросы продавцу и непрочитанные чаты Ozon.", "article sku date_from date_to", {}),
    "ozon_market": ("Сравнение категорий и поисковые запросы Ozon с учётом подписки.", "article sku date_from date_to", {}),
    "ozon_fbs_orders": ("Отправления FBS к сборке, количество и сроки отгрузки.", "article sku date_from date_to warehouse_id", {}),
    "ozon_fbs_stocks": ("Свободные, зарезервированные и физические остатки FBS.", "article sku warehouse_id", {}),
    "ozon_fbs_errors": ("Индекс ошибок FBS и rFBS, составляющие индекса по датам.", "", {}),
}


def schema(name):
    from market_specs import FIELDS, DATE, TEXT, choices
    fields=dict(FIELDS,kind=TEXT,date=DATE,warehouse=TEXT,in_action={"type":"boolean"},group_by=choices("day","warehouse"))
    _, keys, enums=VIEWS[name]
    result={k:fields[k] for k in (*keys.split(),"mode","limit","offset","cursor","task_id")}
    result.update({k:choices(*values) for k,values in enums.items()})
    return result,[]


def validate(name,args):
    from market_specs import _valid
    props,_=schema(name)
    if not isinstance(args,dict) or set(args)-set(props) or not all(_valid(v,props[k]) for k,v in args.items()):
        raise ValueError("Invalid seller report arguments")
    if ("date_from" in args)!=("date_to" in args) or "cursor" in args and "offset" in args or "month" in args and "date_from" in args:
        raise ValueError("Invalid report interval or pagination")
    if "date_from" in args and not 0<=(date.fromisoformat(args["date_to"])-date.fromisoformat(args["date_from"])).days<366:
        raise ValueError("Invalid date range")


def period(args):
    from market_specs import MOSCOW
    now=datetime.now(MOSCOW).date()
    if "month" in args:
        a=date.fromisoformat(args["month"]+"-01")
        b=(a.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)
    else:
        a=date.fromisoformat(args.get("date_from",now.isoformat()))
        b=date.fromisoformat(args.get("date_to",now.isoformat()))
    return a,b


def spans(start,end,days):
    while start<=end:
        last=min(start+timedelta(days=days-1),end)
        yield start,last
        start=last+timedelta(days=1)


def value_at(value,path):
    for key in path.split(".") if path else ():
        if not isinstance(value,dict) or key not in value:
            raise ValueError("Missing "+path)
        value=value[key]
    return value


def num(value):
    n=Decimal(str(value))
    if not n.is_finite():raise ValueError("Invalid numeric value")
    rounded=round(n,4)
    return format(rounded,"f").rstrip("0").rstrip(".") if rounded%1 else str(int(rounded))


def amount(value):
    number=Decimal(str(value))
    if not number.is_finite():raise ValueError("Invalid monetary amount")
    return format(number,"f")


def public_values(value):
    if isinstance(value,dict):return {k:public_values(v) for k,v in value.items()}
    if isinstance(value,list):return [public_values(i) for i in value]
    if isinstance(value,(float,Decimal)):return num(value)
    return value


def _coerce(v,s,defs):
    if "$ref" in s:return _coerce(v,defs[s["$ref"].rsplit("/",1)[-1]],defs)
    for part in s.get("allOf",[]):v=_coerce(v,part,defs)
    if isinstance(v,dict):return {k:_coerce(x,s.get("properties",{}).get(k,{}),defs) for k,x in v.items()}
    if isinstance(v,list):return [_coerce(x,s.get("items",{}),defs) for x in v]
    if type(v) is int and s.get("type")=="string":return str(v)
    if isinstance(v,str) and v.isascii() and v.isdecimal() and s.get("type")=="integer":return int(v)
    return v


class SourceIssue(Exception):
    def __init__(self, **info):
        self.info=info
        super().__init__(info.get("status"))


class Reader:
    def __init__(self,name,args,creds,directory,deadline):
        import expansion_gateway
        self.name,self.args,self.directory,self.deadline=name,args,directory,deadline
        self.market=name.split("_")[0]
        self.wire=expansion_gateway.make_wire(self.market,creds,directory,deadline)
        self.scopes={}
        self.issues=[]
        self.stamps=[]
        self.meta={}
        self.first,self.last=period(args)
        self.article=args.get("article")
        self.ids={str(args[k]) for k in ("nm_id","sku") if k in args}
        self.products={}
        if self.market=="wb" and self.article:
            from expansion_gateway import _seller_mappings
            ids={str(m["wb_nm_id"]) for m in _seller_mappings(directory).values()
                 if m.get("wb_vendor_code")==self.article and m.get("wb_nm_id")}
            if self.ids and ids and self.ids.isdisjoint(ids):raise ValueError("Conflicting nm_id and article")
            if not self.ids:self.ids=ids

    def cached(self,op,inputs,ttl=300):
        import market_catalog
        from market_transport import Wire
        row=market_catalog.find_route(op)
        if not row or row["category"]!="read" or row["market"]!=self.market:
            raise ValueError("Unexpected read operation")
        creds=getattr(self.wire,"category_keys",{}).get(market_catalog.category_key(row["host"]),self.wire.creds)
        if len(self.scopes)==0 or row["host"] not in self.scopes:
            self.scopes[row["host"]]=Wire(self.market,creds,self.directory,self.deadline)
        wire=self.scopes[row["host"]]
        spec=row["contract"]
        inputs=dict(inputs)
        if "body" in inputs:
            inputs["body"]=_coerce(inputs["body"],next(iter(spec["media"].values())),spec["definitions"])
        key=[op,inputs]
        with wire.progress("seller-view-source-v1",key) as (hit,save):
            if not hit:
                if time.monotonic()+1>=self.deadline:raise SourceIssue(source=row["path"],status="pending",reason="request_deadline")
                try:
                    fetched=market_catalog.transfer("market_api_read",{"operation_id":op,"input":inputs},creds,self.directory,self.deadline)
                except urllib.error.HTTPError as exc:
                    http=exc.code
                    wait=finance_gate.parse_retry_after(exc.headers)[0] if http==429 else None
                    exc.close()
                    info=dict(source=row["path"],status="provider_access_denied" if http in (401,402,403) else "rate_limited" if http==429 else "provider_error",http_status=http)
                    if http in (401,402,403):
                        info.update(required_category=row.get("section"),required_subscription={
                            "ozon_post_v1_analytics_category_comparison":"Premium Pro","ozon_post_v1_search_queries_top":"Premium",
                            "ozon_post_v1_question_list":"Premium Plus"}.get(op))
                    if http==429:info["retry_after_seconds"]=wait if wait is not None else 60
                    if http==404 and op in ("wb_get_api_transit_tariffs","wb_get_api_tariffs_acceptance_coefficients"):
                        info.update(status="temporarily_disabled_by_provider",documentation="https://dev.wildberries.ru/release-notes?id=570")
                    raise SourceIssue(**info) from None
                except finance_gate.GateError as exc:
                    raise SourceIssue(source=row["path"],status="pending",http_status=None,reason="local_limiter",
                                      retry_after_seconds=exc.retry_after_seconds) from None
                raw=fetched.get("data")
                if isinstance(raw,dict) and raw.get("error") is True:
                    raise SourceIssue(source=row["path"],status="provider_error",http_status=fetched.get("http_status"))
                hit={"data":raw,"stamp":fetched.get("fetched_at") or fetched.get("observed_at"),"http":fetched.get("http_status",200)}
                save(hit,ttl)
        if hit.get("stamp"):self.stamps.append(hit["stamp"])
        return hit["data"]

    def fetch(self,group,choice,inputs=None,ttl=300):
        return self.cached(seller_reports.GROUPS[group][1][choice],inputs or {},ttl)

    def pages(self,group,choice,inputs,path,paging=None,ttl=300):
        import copy
        query=copy.deepcopy(inputs)
        seen=set()
        while True:
            raw=self.fetch(group,choice,query,ttl)
            records=value_at(raw,path) if raw is not None else []
            if not isinstance(records,list):raise ValueError("Expected rows at "+path)
            yield from records
            if not paging:return
            loc,param,marker,step=paging
            target=query.setdefault(loc,{})
            if (raw or {}).get("has_next") is False:return
            if marker:
                following=value_at(raw,marker)
                if not following or not records:return
            else:
                if len(records)<step:return
                following=target.get(param,1 if param=="page" else 0)+(1 if param=="page" else step)
            if str(following) in seen or target.get(param)==following:raise ValueError("Non-advancing "+param)
            seen.add(str(following))
            target[param]=following

    def section(self,source,compute):
        collected=[]
        try:
            for item in compute():collected.append(item)
        except SourceIssue as exc:self.issues.append(exc.info)
        except finance_gate.GateError as exc:self.issues.append(dict(source=source,status="pending",reason="local_limiter",http_status=None,retry_after_seconds=exc.retry_after_seconds))
        except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
            field=str(exc)[:120] if isinstance(exc,(ValueError,KeyError)) and re.fullmatch(r"[\w .'/:-]{1,120}",str(exc)) else "response"
            self.issues.append(dict(source=source,status="provider_response_invalid",field=field))
        except urllib.error.HTTPError as exc:
            wait=finance_gate.parse_retry_after(exc.headers)[0] if exc.code==429 else None
            self.issues.append(dict(source=source,http_status=exc.code,
                status="rate_limited" if exc.code==429 else "provider_access_denied" if exc.code in (401,402,403) else "provider_error",
                retry_after_seconds=(wait if wait is not None else 60) if exc.code==429 else None))
            exc.close()
        except (TimeoutError,urllib.error.URLError):self.issues.append(dict(source=source,status="source_unavailable"))
        return collected

    def product(self,row,article=None,identifier=None):
        field="nm_id" if self.market=="wb" else "sku"
        if self.article and article and str(article)!=self.article:return []
        if self.ids and identifier is not None and str(identifier) not in self.ids:return []
        if self.ids and identifier is None and (self.args.get(field) is not None or not self.article or article!=self.article):
            self.meta["unresolved_product_rows"]=self.meta.get("unresolved_product_rows",0)+1
            return []
        if not article and identifier is not None:
            article=self.products.get(str(identifier)) or (self.article if str(identifier) in self.ids else None)
            if not article and str(identifier) not in self.products:
                if self.market=="wb":
                    from expansion_gateway import _wb_identity
                    item=_wb_identity(self.wire,nm=identifier)
                    article=(item or {}).get("article")
                else:
                    import ozon_details
                    raw=self.cached("ozon_product_info_list",{"body":{"sku":[str(identifier)]}},86400)
                    match=ozon_details._products({"skus":[str(identifier)]},raw,None)
                    article=match["items"][0]["offer_id"] if len(match["items"])==1 and not match["conflicts"] else None
                self.products[str(identifier)]=article
        if self.article and article and str(article)!=self.article:return []
        if not article:
            self.meta["unresolved_product_rows"]=self.meta.get("unresolved_product_rows",0)+1
            return []
        return [dict(row,article=str(article),**({field:identifier} if identifier is not None else {}))]


def execute(name,args,creds,directory,deadline):
    validate(name,args)
    r=Reader(name,args,creds,directory,deadline)
    return render(r)


def render(r,read=None):
    from market_specs import MOSCOW
    import seller_ozon_views, seller_wb_views
    args,name=r.args,r.name
    identity={k:v for k,v in args.items() if k not in ("cursor","offset","task_id","limit","mode")}
    task=hashlib.sha256(json.dumps([r.wire.scope,name,identity],sort_keys=True).encode()).hexdigest()[:24]
    if args.get("task_id",task)!=task:raise ValueError("Task belongs to a different request")
    cursor=str(args.get("cursor",args.get("offset",0)))
    if not cursor.isascii() or not cursor.isdecimal() or len(cursor)>8:raise ValueError("Invalid cursor")
    backend=seller_wb_views if r.market=="wb" else seller_ozon_views
    items=r.section(name,lambda:(read or backend.read)(r))
    total=len(items)
    offset=int(cursor)
    limit=args.get("limit",50 if args.get("mode","summary")=="summary" else 100)
    if args.get("mode","summary")=="summary":limit=min(50,limit)
    now=datetime.now(timezone.utc).isoformat()
    r.stamps.extend(datetime.fromtimestamp(t,timezone.utc).isoformat() for t in getattr(r.wire,"fetch_times",[]))
    pending=any(i["status"] in ("pending","rate_limited","source_unavailable") for i in r.issues)
    if r.meta.get("unresolved_product_rows"):r.issues.append({"status":"article_mapping_unresolved","source":name})
    if not r.issues and name in ("ozon_compensations","ozon_buyout","wb_retentions","ozon_supply_acts","ozon_removals","ozon_fbs_orders"):
        totals={}
        for row in items:
            key=(row.get("kind") or row.get("act_type") or name,row.get("currency"))
            bucket=totals.setdefault(key,{"kind":key[0],"currency":key[1],"rows":0})
            bucket["rows"]+=1
            for field in ("quantity","declared","received","difference","amount"):
                v=row.get(field)
                if v is not None and (field!="amount" or key[1]):
                    bucket[field]=bucket.get(field,0)+Decimal(str(v)) if field=="amount" else bucket.get(field,0)+int(v)
        r.meta["period_totals"]=[dict(v,**({"amount":amount(v["amount"])} if "amount" in v else {})) for v in totals.values()]
        if name in ("ozon_buyout","ozon_compensations","wb_retentions") and any(
                row.get("amount") is None for row in items if row.get("kind")!="measurement"):
            r.meta["monetary_coverage"]="amount_missing"
        else:
            r.meta["monetary_coverage"]="currency_unconfirmed" if any(row.get("amount") is not None and not row.get("currency") for row in items) else "reported"
    status="pending" if pending else "partial" if r.issues and items else r.issues[0]["status"] if r.issues else "ready"
    next_page=offset+limit if offset+limit<total else None
    # A failed source can change any later row number. Resume collection first.
    if pending:next_page=None
    recent=datetime.now(MOSCOW).date()-timedelta(days=1)
    provisional=[(r.first+timedelta(days=i)).isoformat() for i in range((r.last-r.first).days+1)
                 if recent<=r.first+timedelta(days=i)<=datetime.now(MOSCOW).date()] if "date_to"in args else []
    payload=dict(tool=name,items=items[offset:offset+limit],mode=args.get("mode","summary"),status=status,complete=not r.issues and next_page is None,
                 next_cursor=next_page,observed_at=now,data_as_of=min(r.stamps,default=None),provisional_dates=provisional,
                 source_complete=not r.issues,source_issues=r.issues,total_rows=total if not r.issues else None,
                 read_only=True,untrusted_data=True,**r.meta)
    if pending:payload["task_id"]=task
    if r.issues and not items:payload.update({k:v for k,v in r.issues[0].items() if k in ("http_status","required_subscription","required_category","retry_after_seconds","field","reason")})
    return public_values(payload)
