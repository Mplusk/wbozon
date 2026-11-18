"""Resumable warehouse-level coverage from full order pages and reported stock."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import urllib.error

import finance_gate
import ozon_expansion
import wb_expansion
from market_specs import MOSCOW, result


def _scan(state, section, page, provider, since, as_of):
    stock = section == "stock"
    for item in page["items"]:
        if provider == "wb" and not stock and item.get("warehouse_type") != "Склад WB":
            if item.get("warehouse_type") != "Склад продавца":
                state["issues"].add("unknown_fulfillment_scheme")
            else:
                state.setdefault("seller_warehouse_orders", {})[str(item.get("srid"))] = item["quantity"]
            continue
        if stock and provider == "wb" and item["kind"] != "warehouse":
            key = (item.get("article"), item.get("nm_id"), item.get("barcode"), item["kind"])
            state["transit"][str(key)] = {"kind": item["kind"], "quantity": item["quantity"]}
            continue
        loc = item.get("warehouse_id") if provider == "ozon" else item.get("warehouse")
        if loc is None:
            state["issues"].add("unknown_warehouse")
            continue
        loc = str(loc)
        group = state["locations"].setdefault(
            loc,
            {
                "warehouse": item.get("warehouse_name", item.get("warehouse")),
                "warehouse_id": loc if provider == "ozon" else None,
                "cluster_id": None,
                "cluster": None,
                "stock": {},
                "daily": {},
                "days_with_cancellations": {},
            },
        )
        if stock:
            key = item.get("sku") if provider == "ozon" else item.get("barcode")
            if key is None:
                state["issues"].add("stock_identity_missing")
                continue
            entry = {
                "available": item.get("available_stock_count")
                if provider == "ozon"
                else item.get("quantity"),
                "inbound": item.get("transit_stock_count") if provider == "ozon" else None,
                "transfers": item.get("inbound_replenishment"),
                "customer_returns": item.get("return_from_customer_stock_count"),
            }
            if str(key) in group["stock"]:
                state["issues"].add("duplicate_stock_position")
            group["stock"][str(key)] = entry
            cluster_id = item.get("cluster_id")
            if cluster_id is not None and not group.get("cluster_conflict"):
                if group["cluster_id"] is not None and str(group["cluster_id"]) != str(cluster_id):
                    group.update(cluster_id=None, cluster=None, cluster_conflict=True)
                    state["issues"].add("conflicting_warehouse_cluster")
                else:
                    group.update(
                        cluster_id=cluster_id, cluster=item.get("cluster_name") or group["cluster"]
                    )
            continue
        when = item.get("created_at") if provider == "ozon" else item.get("date")
        if not when or type(item.get("quantity")) is not int or item["quantity"] < 0:
            state["issues"].add("missing_order_date_or_units")
            continue
        day = (
            datetime.fromisoformat(when)
            .astimezone(timezone.utc if provider == "ozon" else MOSCOW)
            .date()
            .isoformat()
        )
        if not since <= day <= as_of:
            continue
        ident = item.get("posting_number") if provider == "ozon" else item.get("srid")
        if not ident:
            state["issues"].add("missing_order_id")
            continue
        # Warehouse and date belong to the event, not the identity. WB cursors may include an earlier row.
        key = sha256(str((ident, item.get("sku") if provider == "ozon" else None)).encode()).hexdigest()
        cancelled = (
            (item["status"] == "cancelled" if item.get("status") else None)
            if provider == "ozon"
            else item.get("cancelled")
        )
        event = [loc, day, item["quantity"], cancelled]
        if key in state["orders"]:
            if state["orders"][key] != event:
                state["issues"].add("inconsistent_order_revision")
            continue
        state["orders"][key] = event
        group["daily"][day] = group["daily"].get(day, 0) + item["quantity"]
        if cancelled:
            group["days_with_cancellations"][day] = (
                group["days_with_cancellations"].get(day, 0) + item["quantity"]
            )
        if type(cancelled) is not bool:
            group.setdefault("unknown_cancellation_days", {})[day] = True


def _locations(state, horizon, as_of):
    answer = []
    demand_verified = state["done"]["orders"] and not state["issues"].intersection(
        {
            "inconsistent_order_revision",
            "missing_order_id",
            "missing_order_date_or_units",
            "unknown_warehouse",
            "unknown_fulfillment_scheme",
            "pagination_did_not_finish",
            "order_volume_exceeds_planning_bound",
            "incomplete_cluster_assignment",
        }
    )
    for key, g in state["locations"].items():
        stock = list(g["stock"].values())
        available = (
            sum(s["available"] for s in stock)
            if stock and all(type(s["available"]) is int and s["available"] >= 0 for s in stock)
            else None
        )
        inbound = (
            sum(s["inbound"] for s in stock)
            if stock and all(type(s["inbound"]) is int and s["inbound"] >= 0 for s in stock)
            else None
        )
        avg, counts, cancelled, retained = {}, {}, {}, {}
        for days in (7, 30, 90):
            first = (as_of - timedelta(days=days - 1)).isoformat()
            units = sum(v for d, v in g["daily"].items() if first<=d<=as_of.isoformat())
            counts[str(days)] = units
            cancellations = sum(v for d, v in g.get("days_with_cancellations", {}).items() if first<=d<=as_of.isoformat())
            known = demand_verified and not any(d >= first for d in g.get("unknown_cancellation_days", {}))
            cancelled[str(days)] = cancellations if known else None
            retained[str(days)] = units - cancellations if known else None
            avg[str(days)] = str(Decimal(units) / days) if demand_verified else None
        rate = Decimal(avg["30"]) if avg["30"] is not None else None
        complete = state["done"]["stock"] and state["done"]["orders"] and not state["issues"]
        gross = (
            max(0, (counts["30"] * horizon + 29) // 30 - available)
            if complete and available is not None and rate is not None
            else None
        )
        net = max(0, gross - inbound) if gross is not None and inbound is not None else None
        planning=state.get("planning")
        coverage={}
        if planning is not None:
            known_days=set(g.get("known_stock_days", []))
            zero_days=set(g.get("stockout_days", [])) & known_days
            for days in (7,30,90):
                dates={(as_of-timedelta(days=i)).isoformat() for i in range(days)}
                observed=dates & known_days
                unavailable=dates & zero_days
                bulk=sum(g.get("wholesale_daily",{}).get(d,0) for d in dates-unavailable)
                ordinary=sum(g["daily"].get(d,0) for d in dates-unavailable)-bulk
                denominator=days-len(unavailable)
                consistent=all(0<=g.get("wholesale_daily",{}).get(d,0)<=g["daily"].get(d,0) for d in dates)
                avg[str(days)]=str(Decimal(ordinary)/denominator) if demand_verified and denominator and ordinary>=0 and consistent else None
                coverage[str(days)]={"stock_observed_days":len(observed),"stockout_days":len(unavailable),
                    "unknown_stock_days":days-len(observed),"excluded_wholesale_units":bulk,"demand_days":denominator,
                    "ordinary_ordered_units":ordinary}
            rate=Decimal(avg["30"]) if avg["30"] is not None else None
            # Keep the unrounded rational until the final ceiling.
            window=coverage["30"]
            expected=(Decimal(window["ordinary_ordered_units"])*(horizon+planning["lead_time_days"])*
                      (100+planning["safety_stock_percent"])/(100*window["demand_days"])) if rate is not None else None
            gross=max(0,int((expected-available).to_integral_value(rounding=ROUND_CEILING))) if complete and available is not None and expected is not None else None
            net=max(0,gross-inbound) if gross is not None and inbound is not None else None
        answer.append(
            {
                "warehouse": g["warehouse"] or key,
                "warehouse_id": g["warehouse_id"],
                "cluster_id": g["cluster_id"],
                "cluster": g["cluster"],
                "available_units": available,
                "inbound_units": inbound,
                "orders_per_day": avg,
                "ordered_units": counts if demand_verified else None,
                "order_basis": "gross_ordered_units_including_cancellations",
                "cancelled_units": cancelled,
                "ordered_units_after_known_cancellations": retained,
                "days_of_cover": str(Decimal(available)/rate)
                if complete and available is not None and rate
                else None,
                "transfer_units": sum(s["transfers"] for s in stock)
                if stock and all(type(s.get("transfers")) is int for s in stock)
                else None,
                "customer_return_units": sum(s["customer_returns"] for s in stock)
                if stock and all(type(s.get("customer_returns")) is int for s in stock)
                else None,
                "shortfall_before_inbound": gross,
                "recommended_quantity": net if net is not None else gross,
                "recommendation_without_inbound": gross,
                "recommendation_if_inbound_arrives": net,
                "confirmed_recommended_quantity": gross
                if not state.get("orders_preliminary") and (gross == 0 or inbound == 0) and (planning is None or coverage["30"]["unknown_stock_days"]==0)
                else None,
                "demand_coverage":coverage, "calendar_ordered_units":counts,
                "recommendation_note": (
                    "Расчёт на валовых заказах с отменами. Количество к поставке предполагает, что весь указанный транзит поступит в пределах горизонта."
                    if net is not None
                    else "Расчёт на валовых заказах с отменами. Входящая поставка по сроку и объёму не подтверждена."
                ),
                "inbound_assumption": "reported_inbound_arrives_within_horizon"
                if net is not None
                else "no_unconfirmed_inbound_counted",
            }
        )
    return answer


def _clusters(state, horizon, as_of, unmapped):
    groups = {}
    for warehouse, loc in state["locations"].items():
        cid = loc["cluster_id"]
        if cid is None:
            continue
        cluster = groups.setdefault(
            str(cid),
            dict(
                warehouse=None,
                warehouse_id=None,
                cluster_id=cid,
                cluster=loc["cluster"],
                daily={},
                stock={},
            ),
        )
        for day, count in loc["daily"].items():
            cluster["daily"][day] = cluster["daily"].get(day, 0) + count
        _merge_cancellations(cluster, loc)
        for sku, entry in loc["stock"].items():
            cluster["stock"][warehouse + ":" + sku] = entry
    issues = state["issues"] | ({"incomplete_cluster_assignment"} if unmapped else set())
    output = _locations(dict(state, locations=groups, issues=issues), horizon, as_of)
    for row in output:
        del row["warehouse"], row["warehouse_id"]
        row["coverage_complete"] = (
            all(state["done"].values()) and not issues and row["available_units"] is not None
        )
        row["stock_scope"] = "mapped_warehouses_only" if unmapped else "reported_warehouses"
    return output


def _market_total(state, horizon, as_of, missing_stock):
    group = {
        "warehouse": None,
        "warehouse_id": None,
        "cluster_id": None,
        "cluster": None,
        "daily": {},
        "stock": {},
    }
    for warehouse, location in state["locations"].items():
        for day, value in location["daily"].items():
            group["daily"][day] = group["daily"].get(day, 0) + value
        _merge_cancellations(group, location)
        for sku, stock in location["stock"].items():
            group["stock"][warehouse + ":" + sku] = stock
    row = _locations(dict(state, locations={"total": group}), horizon, as_of)[0]
    for key in ("warehouse", "warehouse_id", "cluster", "cluster_id"):
        row.pop(key)
    if missing_stock:
        for key in (
            "available_units",
            "inbound_units",
            "shortfall_before_inbound",
            "recommended_quantity",
            "days_of_cover",
            "recommendation_without_inbound",
            "recommendation_if_inbound_arrives",
            "confirmed_recommended_quantity",
        ):
            row[key] = None
    return row


def _merge_cancellations(group, location):
    for field in ("known_stock_days","stockout_days"):
        dates=set(location.get(field,[]))
        group[field]=sorted(dates if field not in group else dates & set(group[field]))
    wholesale=group.setdefault("wholesale_daily",{})
    for day,units in location.get("wholesale_daily",{}).items():
        wholesale[day]=wholesale.get(day,0)+units
    counts = group.setdefault("days_with_cancellations", {})
    for day, count in location.get("days_with_cancellations", {}).items():
        counts[day] = counts.get(day, 0) + count
    group.setdefault("unknown_cancellation_days", {}).update(location.get("unknown_cancellation_days", {}))


def _platform(provider, article, nm, wire, horizon, as_of, planning=None):
    since = (as_of - timedelta(days=89)).isoformat()
    key = [provider, article, nm, as_of.isoformat()]
    context = (
        wire.progress("supply-plan-v2", key)
        if hasattr(wire, "progress")
        else nullcontext(({}, lambda data, ttl: None))
    )
    with context as (saved, save):
        state = saved or {
            "locations": {},
            "transit": {},
            "orders": {},
            "done": {"stock": False, "orders": False},
            "cursors": {},
            "seen_cursors": {},
            "issues": [],
            "stock_task_id": None,
        }
        state["issues"] = set(state["issues"])
        state["orders_preliminary"] = provider == "wb"

        def checkpoint():
            save(
                dict(state, issues=sorted(state["issues"])), 1800 if all(state["done"].values()) else 86400
            )

        pending = []
        for section in ("stock", "orders"):
            for _ in range(10):
                if state["done"][section] or "order_volume_exceeds_planning_bound" in state["issues"]:
                    break
                args = (
                    {"article": article, "limit": 100}
                    if not (provider == "ozon" and section == "stock")
                    else {"article": article}
                )
                if provider == "wb" and nm is not None:
                    args["nm_id"] = nm
                if section == "orders":
                    args.update(date_from=since, date_to=as_of.isoformat())
                cursor = state["cursors"].get(section)
                if cursor is not None:
                    args["offset" if section == "stock" else "cursor"] = cursor
                if section == "stock" and state["stock_task_id"]:
                    args["task_id"] = state["stock_task_id"]
                try:
                    if provider == "ozon":
                        page = ozon_expansion.read(
                            "ozon_stocks_by_cluster" if section == "stock" else "ozon_fbo_postings_geo",
                            args,
                            wire,
                        )
                    else:
                        page = wb_expansion.read(
                            "wb_stocks" if section == "stock" else "wb_orders", args, wire
                        )
                except urllib.error.HTTPError as exc:
                    pending.append(
                        {
                            "source": section,
                            "http_status": exc.code,
                            "retry_after_seconds": exc.headers.get("Retry-After"),
                            "required_category": ("Аналитика" if section == "stock" else "Статистика")
                            if provider == "wb"
                            else None,
                        }
                    )
                    exc.close()
                    break
                except (
                    TimeoutError,
                    urllib.error.URLError,
                    finance_gate.GateError,
                    finance_gate.ProviderResponseTooLarge,
                ) as exc:
                    pending.append(
                        {"source": section, "status": getattr(exc, "reason", "provider_read_incomplete")}
                    )
                    break
                if page.get("task_id"):
                    state["stock_task_id"] = page["task_id"]
                if page.get("status") in ("pending", "report_unavailable", "outside_provider_retention"):
                    pending.append(
                        {
                            "source": section,
                            "status": page["status"],
                            "retry_after_seconds": page.get("retry_after_seconds"),
                        }
                    )
                    if section == "stock" and page.get("provider_status") in ("purged", "canceled"):
                        state["stock_task_id"] = None
                        state["transit"].clear()
                        state["cursors"].pop("stock", None)
                        state["seen_cursors"].pop("stock", None)
                        for location in state["locations"].values():
                            location["stock"].clear()
                    checkpoint()
                    break
                # Advance only once for a valid page. A failed final page remains available for inspection.
                if page.get("issues") or (not page["complete"] and page["next_cursor"] is None):
                    pending.append(
                        {
                            "source": section,
                            "status": "incomplete",
                            "issues": page.get("issues"),
                            **{k: page[k] for k in ("missing_skus", "invalid_stock_skus") if page.get(k)},
                        }
                    )
                    break
                next_cursor = page["next_cursor"]
                history = state["seen_cursors"].setdefault(section, [])
                if next_cursor is not None and (next_cursor in history or len(history) >= 1000):
                    state["issues"].add("pagination_did_not_finish")
                    break
                _scan(state, section, page, provider, since, as_of.isoformat())
                state["done"][section] = page["complete"]
                state["cursors"][section] = next_cursor
                if next_cursor is not None:
                    history.append(next_cursor)
                if len(state["orders"]) > 50000:
                    state["issues"].add("order_volume_exceeds_planning_bound")
                    checkpoint()
                    break
                checkpoint()
        if planning is not None:
            state["planning"]=planning
            for loc in state["locations"].values():
                loc.update(wholesale_daily={},known_stock_days=[],stockout_days=[])
            from statistics import median
            event_units=[event[2] for event in state["orders"].values() if event[2]>0]
            ordinary_size=median(event_units) if len(event_units)>=5 else None
            for loc,day,units,_ in state["orders"].values():
                outlier=(planning.get("bulk_explicit") or (ordinary_size is not None and units>=4*ordinary_size))
                if units >= planning["bulk_order_min_units"] and outlier and loc in state["locations"]:
                    by_day=state["locations"][loc]["wholesale_daily"]
                    by_day[day]=by_day.get(day,0)+units
            if hasattr(wire,"gate_dir"):
                import market_history
                records=market_history.History(wire.gate_dir).verified_days(provider,wire.creds,provider+"_availability",since,as_of.isoformat())
                for day,(_,items) in records.items():
                    groups={}
                    for item in items:
                        if item.get("article")==article and item.get("kind","warehouse")=="warehouse":
                            location=str(item.get("warehouse_id") if provider=="ozon" else item.get("warehouse"))
                            groups.setdefault(location,[]).append((item.get("available_stock_count",item.get("quantity")),item.get("availability_basis")))
                    for loc,positions in groups.items():
                        values=[v for v,_ in positions]
                        if loc in state["locations"] and all(type(v) is int and v>=0 for v in values):
                            whole_day=all(b=="whole_day" for _,b in positions)
                            if any(values) or whole_day:
                                state["locations"][loc]["known_stock_days"].append(day)
                            if not any(values) and whole_day and not state["locations"][loc]["daily"].get(day):
                                state["locations"][loc]["stockout_days"].append(day)
        locations = _locations(state, horizon, as_of)
        unmapped = [
            key
            for key, location in state["locations"].items()
            if provider == "ozon" and location["cluster_id"] is None
        ]
        missing_stock = [
            r["warehouse_id"] or r["warehouse"] for r in locations if r["available_units"] is None
        ]
        sources_complete = all(state["done"].values()) and not state["issues"]
        complete = sources_complete and bool(locations) and not missing_stock and not unmapped
        clusters = _clusters(state, horizon, as_of, unmapped) if provider == "ozon" else []
        destinations = clusters if provider == "ozon" else locations
        total = _market_total(state, horizon, as_of, missing_stock)
        if provider=="wb" and planning is not None and hasattr(wire,"deadline") and state["done"]["stock"]:
            import supply_history
            from market_composites import observed
            cutoff=(as_of+timedelta(days=1+horizon+planning["lead_time_days"])).isoformat()
            arrivals=observed(lambda:supply_history.report("wb_supplies",{
                "article":article,"date_from":(as_of-timedelta(days=179)).isoformat(),
                "date_to":(as_of+timedelta(days=1)).isoformat(),"status_ids":[4,6],"limit":100},wire))
            incoming={}
            usable=arrivals["complete"]
            for shipment in arrivals["items"]:
                declared,accepted=shipment.get("declared_units"),shipment.get("accepted_units")
                day=(shipment.get("supply_date") or "")[:10]
                if not day or not shipment.get("warehouse") or type(declared) is not int or type(accepted) is not int or not 0<=accepted<=declared:
                    usable=False
                    continue
                if day<=cutoff:
                    name=shipment["warehouse"]
                    incoming[name]=incoming.get(name,0)+declared-accepted
            if usable:
                for rec in [*locations,total]:
                    inbound=incoming.get(rec["warehouse"],0) if "warehouse" in rec else sum(incoming.values())
                    rec["inbound_units"]=inbound
                    rec["inbound_assumption"]="unaccepted_units_on_reported_receiving_or_gate_supplies_created_in_180_days"
                    gross=rec["recommendation_without_inbound"]
                    if gross is not None:
                        rec["recommended_quantity"]=rec["recommendation_if_inbound_arrives"]=max(0,gross-inbound)
            else:
                pending.append({"source":"wb_supplier_inbound","status":arrivals.get("status","partial")})
        total["stock_pooling_assumption"] = (
            "Общий расчёт площадки предполагает, что весь запас доступен любому складу."
        )

        def shipment_total(field):
            values = [d.get(field) for d in destinations]
            return (
                sum(values) if complete and values and all(value is not None for value in values) else None
            )

        total["destination_basis"] = "clusters" if provider == "ozon" else "warehouses"
        total["ship_without_transfers_without_inbound"] = shipment_total("recommendation_without_inbound")
        total["ship_without_transfers_if_inbound_arrives"] = shipment_total(
            "recommendation_if_inbound_arrives"
        )
        return {
            "marketplace": provider,
            "article": article,
            "complete": complete,
            "source_complete": state["done"],
            "issues": sorted(
                state["issues"]
                | ({"no_warehouse_stock_observed"} if state["done"]["stock"] and not locations else set())
            ),
            "pending": pending,
            "complete_scope": "marketplace_warehouse_plan_only",
            "fulfillment_scope": "Ozon FBO" if provider == "ozon" else "Wildberries FBW",
            "orders_date_basis": "posting_creation_utc" if provider == "ozon" else "event_date_moscow",
            "statistics_status": "reported_fbo_postings" if provider == "ozon" else "preliminary",
            "statistics_limitations": []
            if provider == "ozon"
            else ["orders_without_confirmed_payment_may_be_absent", "rolling_90_day_retention"],
            "account_coverage": {
                "status": "partial",
                "excluded": ["FBS", "rFBS"]
                if provider == "ozon"
                else ["seller_fulfilled_orders_and_stock"],
            },
            "unassigned_warehouse_ids": unmapped,
            "unknown_stock_warehouses": missing_stock,
            "cluster_coverage_complete": complete if provider == "ozon" else None,
            "unmapped_cluster_order_units_90d": sum(
                sum(state["locations"][k]["daily"].values()) for k in unmapped
            )
            if provider == "ozon" and total["ordered_units"] is not None and state["done"]["stock"]
            else None,
            "scenario_recommendations_complete": complete,
            "recommendations_complete": complete
            and all(row["confirmed_recommended_quantity"] is not None for row in destinations),
            "excluded_seller_warehouse_units": sum(state.get("seller_warehouse_orders", {}).values())
            if provider == "wb" and total["ordered_units"] is not None
            else None,
            "locations": locations,
            "clusters": clusters,
            "marketplace_total": total,
            "reported_transit": [r for r in state["transit"].values() if r["kind"] != "provider_total"],
            "reported_stock_totals": [
                r for r in state["transit"].values() if r["kind"] == "provider_total"
            ],
        }


def read(args, wires, mapping):
    horizon = args["horizon_days"]
    if not mapping:
        return result(
            [],
            complete=False,
            status="mapping_required",
            required_mapping=["ozon_offer_id", "wb_vendor_code"],
            article=args["article"],
        )
    platforms = []
    for provider, field in (("ozon", "ozon_offer_id"), ("wb", "wb_vendor_code")):
        if provider not in wires:
            platforms.append({"marketplace": provider, "complete": False, "status": "not_configured"})
            continue
        as_of = datetime.now(timezone.utc if provider == "ozon" else MOSCOW).date() - timedelta(days=1)
        try:
            report = _platform(
                provider, mapping[field], mapping.get("wb_nm_id"), wires[provider], horizon, as_of,
                dict({k:args.get(k,v) for k,v in (("lead_time_days",0),("safety_stock_percent",0),("bulk_order_min_units",10))},
                     bulk_explicit="bulk_order_min_units" in args)
            )
        except finance_gate.GateError as exc:
            report = {"marketplace": provider, "complete": False, "status": exc.reason}
        except (ValueError, KeyError, TypeError):
            report = {"marketplace": provider, "complete": False, "status": "invalid_provider_report"}
        report["orders_through"] = as_of.isoformat()
        platforms.append(report)
    return result(
        platforms,
        complete=all(p["complete"] for p in platforms),
        horizon_days=horizon,
        article=args["article"],
        mapping=mapping,
        quantity_unit="pcs",
        baseline_days=30,
        complete_scope="marketplace_warehouse_plan_only",
        account_coverage="partial",
        scope_note="План пополнения складов Ozon FBO и Wildberries FBW. Продажи со склада продавца вынесены за пределы расчёта. Темп рассчитан по заказанным штукам с отменами.",
        order_basis="gross_ordered_units_including_cancellations; Ozon FBO UTC and WB Moscow completed calendar days",
        lead_time_days=args.get("lead_time_days",0),safety_stock_percent=args.get("safety_stock_percent",0),
        bulk_order_min_units=args.get("bulk_order_min_units",10),
        recommendation_basis="max(0,ceil(d*(lead_time_days+horizon_days)*(1+safety_stock_percent/100)-available-inbound)); unobserved stock days yield a conditional estimate",
    )
