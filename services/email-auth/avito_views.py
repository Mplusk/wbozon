"""Seller views over the existing account-bound read-only Avito operations."""
from datetime import datetime, timezone, timedelta


def contracts(raw):
    common={"mode":{"type":"string","enum":["summary","details"]},"limit":{"type":"integer","minimum":1,"maximum":100},
            "offset":{"type":"integer","minimum":0},"cursor":{"type":"string","maxLength":32},
            "date_from":{"type":"string","format":"date"},"date_to":{"type":"string","format":"date"},
            "item_ids":{"type":"array","items":{"type":"integer","minimum":1},"minItems":1,"maxItems":200}}
    specs={
        "avito_items":("Объявления, статус, категории, цены и ссылки.","status","avito_get_items_info"),
        "avito_stats":("Показы, контакты и конверсии объявлений.","grouping metrics version","avito_item_analytics"),
        "avito_spendings":("Расходы профиля по типам и периодам.","grouping spendingTypes","avito_account_spendings"),
        "avito_orders":("Заказы Авито Доставки с фильтрами по дате и статусу.","statuses","avito_get_orders"),
        "avito_stocks":("Остатки по идентификаторам объявлений.","","avito_get_stocks_info"),
        "avito_reviews":("Рейтинг продавца и отзывы с пагинацией.","","avito_get_reviews_v1"),
        "avito_balance":("Баланс и финансовые операции в интервалах до семи дней.","","avito_get_user_balance"),
        "avito_chats":("Чаты и непрочитанные сообщения по объявлениям.","unread_only","avito_get_chats_v2")}
    out={}
    for n,(title,keys,op) in specs.items():
        properties={key:dict(common[key]) for key in ("mode","limit","offset","cursor")}
        if n in ("avito_stats","avito_spendings","avito_orders","avito_balance"):
            properties.update({key:common[key] for key in ("date_from","date_to")})
        if n in ("avito_stats","avito_stocks","avito_chats","avito_spendings"):
            properties["item_ids"]=common["item_ids"]
        for key in keys.split():
            properties[key]={"type":"string","enum":["v1","v2"]} if key=="version" else raw[op]["properties"][key]
        out[n]={"description":title,"properties":properties,"required":["item_ids"] if n=="avito_stocks" else []}
    return out


def _items(data):
    if data is None:return []
    if isinstance(data,list):return data
    for k in ("resources","orders","reviews","chats","stocks","operations","items","groupings","spendings","result","data"):
        if isinstance(data.get(k),(dict,list)):return _items(data[k])
    return [data]


def _identify(name, rows, args, directory, get, metadata):
    from expansion_gateway import _seller_mappings
    targets=[]
    for record in rows:
        if name=="avito_orders":
            targets.extend((item,item.get("avitoId")) for item in record.get("items") or [])
        elif name=="avito_items":targets.append((record,record.get("id")))
        elif name=="avito_chats":targets.append((record,((record.get("context") or {}).get("value") or {}).get("id")))
        elif name=="avito_reviews":targets.append((record,(record.get("item") or {}).get("id")))
        elif name=="avito_stocks":targets.append((record,record.get("item_id")))
        elif name=="avito_stats" and ("itemId" in record or record.get("type")=="item" or args.get("grouping")=="item"):
            targets.append((record,record.get("itemId",record.get("id"))))
        elif name=="avito_spendings" and len(args.get("item_ids",[]))==1:
            targets.append((record,args["item_ids"][0]))
        else:record.setdefault("scope","account")
    mapped={}
    for mapping in _seller_mappings(directory).values():
        if mapping.get("avito_item_id") and mapping.get("avito_article"):
            mapped.setdefault(str(mapping["avito_item_id"]),set()).add(mapping["avito_article"])
    missing={str(i) for _,i in targets if i and str(i).isdecimal() and str(i) not in mapped}
    if missing:
        from market_composites import observed
        ordered=sorted(missing)
        for i in range(0,len(ordered),100):
            found=observed(lambda:get("avito_get_ad_ids_by_avito_ids",{"query":",".join(ordered[i:i+100])}))
            for item in (found.get("data") or {}).get("items",[]):
                if item.get("ad_id"):
                    mapped.setdefault(str(item.get("avito_id")),set()).add(str(item["ad_id"]))
    for record,ident in targets:
        if not ident:
            record.setdefault("scope","account")
            continue
        record["item_id"]=ident
        values=mapped.get(str(ident),set())
        if len(values)==1:
            record["article"]=next(iter(values))
        else:
            record["article_status"]="seller_article_unconfirmed"
            metadata["source_status"]="partial_product_identity"


def read(name,args,creds,directory,deadline):
    import avito_mcp
    from seller_views import public_values,period,spans
    first,last=period(args)
    now=datetime.now(timezone.utc).isoformat()
    size=min(args.get("limit",50),50 if args.get("mode","summary")=="summary" else 100)
    offset=int(args.get("cursor",args.get("offset",0)))
    if offset<0 or offset>10000000:raise ValueError("Invalid Avito cursor")
    ids=args.get("item_ids",[]);rows=[];stamps=[];metadata={};more=False
    def get(op,q):
        avito_mcp.validate(op,q)
        res=avito_mcp.read(op,q,creds,directory,deadline)
        stamps.append(res["data_as_of"])
        if res.get("status")=="partial":metadata["source_status"]="partial"
        return res
    if name=="avito_balance":
        metadata["current_balance"]=get("avito_get_user_balance",{})["data"]
        if "date_from" in args:
            for a,b in spans(first,last,7):
                q={"dateTimeFrom":a.isoformat()+"T00:00:00Z","dateTimeTo":b.isoformat()+"T23:59:59Z"}
                rows.extend(_items(get("avito_post_operations_history",q)["data"]))
        more=offset+size<len(rows);rows=rows[offset:offset+size]
    elif name=="avito_stats" and (args.get("version")=="v1" or ids and args.get("version") is None and args.get("grouping","day") in ("day","week","month")):
        if not ids:raise ValueError("Avito v1 statistics require item_ids")
        if (datetime.now(timezone.utc).date()-first).days>=270 or (last-first).days>=270:
            raise ValueError("Avito item statistics retain 270 days")
        q={"itemIds":ids,"dateFrom":first.isoformat(),"dateTo":last.isoformat(),"periodGrouping":args.get("grouping","day"),
           "fields":args.get("metrics",["uniqViews","uniqContacts","uniqFavorites"])}
        rows=_items(get("avito_item_stats_shallow",q)["data"]);more=offset+size<len(rows);rows=rows[offset:offset+size]
    elif name in ("avito_stocks","avito_spendings"):
        if name=="avito_stocks":op,q="avito_get_stocks_info",{"item_ids":ids}
        else:
            op,q="avito_account_spendings",{"dateFrom":first.isoformat(),"dateTo":last.isoformat(),"grouping":args.get("grouping","day"),
                                          "spendingTypes":args.get("spendingTypes",["all","promotion","presence","commission","rest"])}
            if ids:
                if len(ids)>100:raise ValueError("Avito spendings accept at most 100 item IDs")
                q["filter"]={"itemIDs":ids}
        rows=_items(get(op,q)["data"]);more=offset+size<len(rows);rows=rows[offset:offset+size]
        if name=="avito_spendings":
            metadata["monetary_coverage"]="provider_documents_conflicting_units"
            metadata["source_field"]="result.groupings[].spendings[].value"
            metadata["unit_documentation"]={"field":"RUB","operation_description":"kopeck"}
            metadata["total_basis"]="all_includes_the_individual_spending_types"
    else:
        if name=="avito_stats":
            op="avito_item_analytics"
            q={"grouping":args.get("grouping","day"),"metrics":args.get("metrics",["views","contacts","favorites"]),
               "dateFrom":first.isoformat(),"dateTo":last.isoformat(),"limit":size,"offset":offset}
            if ids and q["grouping"]!="item":
                raise ValueError("Use item grouping for v2 or v1 for dated item statistics")
        elif name=="avito_orders":
            size=min(size,20);op="avito_get_orders";q={"page":offset//size+1,"limit":size}
            if "statuses" in args:q["statuses"]=args["statuses"]
            if "date_from" in args:q["dateFrom"]=int(datetime.combine(first,datetime.min.time(),timezone.utc).timestamp())
        elif name=="avito_reviews":
            size=min(size,50);metadata["seller_rating"]=get("avito_get_ratings_info_v1",{})["data"]
            op,q="avito_get_reviews_v1",{"offset":offset,"limit":size}
        elif name=="avito_items":
            op,q="avito_get_items_info",{"page":offset//size+1,"per_page":size}
            if "status" in args:q["status"]=args["status"]
        else:
            op,q="avito_get_chats_v2",{"offset":offset,"limit":size,"unread_only":args.get("unread_only",False)}
            if ids:q["item_ids"]=ids
        if name in ("avito_orders","avito_items") and offset%size:
            raise ValueError("Keep page size when following Avito cursor")
        res=get(op,q);rows=_items(res["data"])
        more=(res.get("pagination") or {}).get("has_more")
        if more is None:more=len(rows)==size
        if name=="avito_stats" and ids:
            rows=[r for r in rows if str(r.get("id",r.get("itemId"))) in {str(i) for i in ids}]
        if name=="avito_orders" and "date_to" in args:
            selected=[]
            for record in rows:
                stamp=record.get("createdAt",record.get("created_at"))
                if stamp is None:
                    metadata["source_status"]="order_date_unavailable"
                else:
                    moment=(datetime.fromtimestamp(stamp,timezone.utc) if type(stamp) in (int,float) else
                            datetime.fromisoformat(str(stamp).replace("Z","+00:00")))
                    if first<=moment.date()<=last:selected.append(record)
            rows=selected
    for record in rows:
        if not isinstance(record,dict):raise ValueError("Unexpected Avito record")
    _identify(name,rows,args,directory,get,metadata)
    return public_values(dict(tool=name,items=rows,complete=not more and "source_status" not in metadata,
        next_cursor=str(offset+size) if more else None,status=metadata.get("source_status","ready"),observed_at=now,
        data_as_of=min(stamps,default=None),provisional_dates=[],read_only=True,untrusted_data=True,**metadata))
