"""Resumable warehouse-level coverage from full order pages and reported stock."""

from contextlib import nullcontext
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
import copy
from pathlib import Path
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
        if provider == "ozon" and item.get("order_id"):
            state.setdefault("order_groups", {})[key] = str(item["order_id"])
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
    demand_valid = not state["issues"].intersection(
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
    def verified_window(first):
        coverage = state.get("order_coverage")
        return demand_valid and (state["done"]["orders"] or bool(
            coverage and coverage[0] <= first and coverage[1] >= as_of.isoformat()))

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
            demand_verified = verified_window(first)
            if state.get("anonymous_wb_demand") and not g.get("marketplace_total") and key != "Склад WB":
                demand_verified = False
            units = sum(v for d, v in g["daily"].items() if first<=d<=as_of.isoformat())
            counts[str(days)] = units if demand_verified else None
            cancellations = sum(v for d, v in g.get("days_with_cancellations", {}).items() if first<=d<=as_of.isoformat())
            known = demand_verified and not any(d >= first for d in g.get("unknown_cancellation_days", {}))
            cancelled[str(days)] = cancellations if known else None
            retained[str(days)] = units - cancellations if known else None
            avg[str(days)] = str(Decimal(units) / days) if demand_verified else None
        rate = Decimal(avg["30"]) if avg["30"] is not None else None
        complete = state["done"]["stock"] and avg["30"] is not None and not state["issues"]
        if state.get("anonymous_wb_demand") and not g.get("marketplace_total"):
            complete = False
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
                demand_verified = counts[str(days)] is not None
                observed=dates & known_days
                unavailable=dates & zero_days
                bulk=sum(g.get("wholesale_daily",{}).get(d,0) for d in dates-unavailable)
                ordinary=sum(g["daily"].get(d,0) for d in dates-unavailable)-bulk
                denominator=days-len(unavailable)
                consistent=all(0<=g.get("wholesale_daily",{}).get(d,0)<=g["daily"].get(d,0) for d in dates)
                avg[str(days)]=str(Decimal(ordinary)/denominator) if demand_verified and denominator and ordinary>=0 and consistent else None
                coverage[str(days)]={"stock_observed_days":len(observed),"stockout_days":len(unavailable),
                    "unknown_stock_days":days-len(observed),"excluded_wholesale_units":bulk if demand_verified else None,"demand_days":denominator,
                    "ordinary_ordered_units":ordinary if demand_verified else None}
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
                "ordered_units": counts if any(v is not None for v in counts.values()) else None,
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
        "marketplace_total": True,
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


def _assign_clusters(state, wire):
    missing = {k: loc for k, loc in state["locations"].items()
               if loc["cluster_id"] is None and not loc.get("cluster_conflict")}
    if not missing:
        return
    directory = defaultdict(dict)
    for cluster in ozon_expansion.cluster_directory(wire):
        if cluster.get("id") is None:
            continue
        for w in cluster["warehouses"]:
            directory[str(w.get("warehouse_id"))][str(cluster["id"])] = (cluster["id"], cluster.get("name"))
    for ident, loc in missing.items():
        choices = directory[ident]
        if len(choices) == 1:
            loc["cluster_id"], loc["cluster"] = next(iter(choices.values()))
        elif len(choices) > 1:
            state["issues"].add("conflicting_warehouse_cluster")


def _wholesale(state, planning):
    from statistics import median
    grouped = defaultdict(list)
    for key, event in state["orders"].items():
        group_id = state.get("order_groups", {}).get(key)
        grouped[("order", group_id) if group_id else ("line", key)].append(event)
    totals = [sum(e[2] for e in events) for events in grouped.values()]
    ordinary = median(totals) if len(totals) >= 5 else None
    for events, units in zip(grouped.values(), totals):
        if units < planning["bulk_order_min_units"]:
            continue
        if not planning.get("bulk_explicit") and (ordinary is None or units < 4 * ordinary):
            continue
        for loc, day, count, _ in events:
            by_day = state["locations"][loc]["wholesale_daily"]
            by_day[day] = by_day.get(day, 0) + count


def _seller_wholesale(wire, state, provider, article, since, through):
    if not hasattr(wire, "gate_dir"):
        return []
    path = Path(wire.gate_dir).parent / "supply-order-adjustments.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return []
    if len(raw) > 65536:
        raise ValueError("Supply adjustments exceed the configured bound")
    config = json.loads(raw)
    if config.get("version") != 1 or not isinstance(config.get("entries"), list):
        raise ValueError("Invalid supply adjustments")
    evidence, seen = [], set()
    for entry in config["entries"]:
        if entry["marketplace"] != provider or entry["article"] != article or not since <= entry["date"] <= through:
            continue
        location, day, units = str(entry["warehouse"]), entry["date"], entry["units"]
        if (location, day) in seen or type(units) is not int or units <= 0 or not entry.get("source"):
            raise ValueError("Invalid seller wholesale observation")
        seen.add((location, day))
        loc = state["locations"].get(location)
        gross = loc["daily"].get(day, 0) if loc else 0
        covered = state["done"]["orders"] or (state.get("order_coverage") and state["order_coverage"][0] <= day <= state["order_coverage"][1])
        verified = bool(covered and gross >= units)
        previous = loc["wholesale_daily"].get(day, 0) if loc else 0
        if verified:
            # The same wholesale purchase may already have been recognized by order ID.
            loc["wholesale_daily"][day] = max(units, previous)
        evidence.append({"warehouse": location, "date": day, "units": units, "source": entry["source"],
                         "observed_units": gross, "already_classified": previous, "applied": verified})
    return evidence


def _archived_stock(provider, article, nm, wire, day):
    if not hasattr(wire, "gate_dir"):
        return None
    import market_history
    history = market_history.History(wire.gate_dir)
    archived = history.verified_days(provider, wire.creds, provider + "_availability", day, day)
    if day not in archived:
        return None
    stamp, records = archived[day]
    # A missing seller code could conceal another position of this product.
    if any(not r.get("article") and (provider == "ozon" or nm is None or r.get("nm_id") is None
                                     or str(r["nm_id"]) == str(nm)) for r in records):
        return None
    selected = [r for r in records if r.get("article") == article and
                (provider == "ozon" or nm is None or str(r.get("nm_id")) == str(nm))]
    if not selected:
        return None
    return result(selected, stock_source="verified_daily_availability", stock_observed_at=stamp)


def _reuse_completed_orders(state, wire, key):
    if state["done"]["orders"] or state["orders"] or not hasattr(wire, "progress_snapshot"):
        return
    prior = wire.progress_snapshot("supply-plan-v2", key)
    if not prior or not prior["done"]["orders"] or prior.get("issues"):
        return
    state["orders"] = copy.deepcopy(prior["orders"])
    for name in ("seller_warehouse_orders", "order_groups"):
        if name in prior:
            state[name] = copy.deepcopy(prior[name])
    for ident, previous in prior["locations"].items():
        if not previous.get("daily"):
            continue
        loc = state["locations"].setdefault(ident, {
            "warehouse": previous.get("warehouse"), "warehouse_id": previous.get("warehouse_id"),
            "cluster_id": None, "cluster": None, "stock": {},
        })
        for field in ("daily","days_with_cancellations","unknown_cancellation_days"):
            loc[field] = copy.deepcopy(previous.get(field, {}))
    state["done"]["orders"] = True
    state["orders_source"] = "previous_complete_snapshot_same_account_article_and_day"


def _use_wb_order_archive(state, wire, article, nm, since, through):
    if state["done"]["orders"] or not hasattr(wire, "gate_dir"):
        return
    import market_history
    history = market_history.History(wire.gate_dir)
    owner = market_history.scope("wb", wire.creds)
    required = market_history.days_between(since, through)
    if not history.authorized(wire.creds, owner) or not set(required) <= history.coverage(owner, "wb_orders").keys():
        return
    rows = []
    for offset in range(0, len(required), 7):
        week = required[offset:offset+7]
        pages = history.read_days(owner, "wb_orders", week[0], week[-1])
        if len(pages) != len(week):
            return
        for _, records in pages.values():
            for row in records:
                code = row.get("article")
                same_nm = nm is not None and str(row.get("nm_id")) == str(nm)
                if (code == article and (nm is None or same_nm)) or (same_nm and not code):
                    rows.append(row)
                elif not code and (nm is None or not row.get("nm_id")):
                    return
        if len(rows) > 50000:
            return
    state["locations"] = {k: v for k, v in state["locations"].items() if v["stock"]}
    for location in state["locations"].values():
        location.update(daily={}, days_with_cancellations={}, unknown_cancellation_days={})
    state.update(orders={}, seller_warehouse_orders={}, order_coverage=[since, through],
                 orders_source="verified_daily_order_archive")
    state["issues"].difference_update({"inconsistent_order_revision", "missing_order_id", "unknown_fulfillment_scheme",
                                       "missing_order_date_or_units", "order_volume_exceeds_planning_bound"})
    state["anonymous_wb_demand"] = any(row.get("warehouse") == "Склад WB" and
                                       row.get("warehouse_type") == "Склад WB" for row in rows)
    _scan(state, "orders", {"items": rows}, "wb", since, through)
    state["done"]["orders"] = True


def _platform(provider, article, nm, wire, horizon, as_of, planning=None):
    since = (as_of - timedelta(days=89)).isoformat()
    key = [provider, article, nm, as_of.isoformat()]
    context = (
        wire.progress("supply-plan-v3", key)
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
        _reuse_completed_orders(state, wire, key)
        if provider == "wb":
            _use_wb_order_archive(state, wire, article, nm, since, as_of.isoformat())

        def checkpoint():
            save(
                dict(state, issues=sorted(state["issues"])), 1800 if all(state["done"].values()) else 86400
            )

        pending = []
        for section in ("stock", "orders"):
            for _ in range(10):
                if state["done"][section] or (section == "orders" and state.get("order_coverage")) or "order_volume_exceeds_planning_bound" in state["issues"]:
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
                    snapshot = None
                    if section == "stock" and cursor is None:
                        snapshot = _archived_stock(provider, article, nm, wire, (as_of + timedelta(days=1)).isoformat())
                    if snapshot is not None:
                        page = snapshot
                    elif provider == "ozon":
                        page = ozon_expansion.read(
                            "ozon_stocks_by_cluster" if section == "stock" else "ozon_fbo_postings_geo",
                            args,
                            wire,
                        )
                    elif section == "orders":
                        page = wb_expansion._stats("wb_orders", dict(args, limit=1000), wire)
                    else:
                        page = wb_expansion.read("wb_stocks", args, wire)
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
                if section == "stock" and page.get("complete"):
                    state["stock_source"] = page.get("stock_source", "marketplace_report")
                    state["stock_observed_at"] = page.get("stock_observed_at") or page.get("observed_at")
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
                if page.get("issues") or (not page["complete"] and page["next_cursor"] is None and not page.get("source_exhausted")):
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
                if section == "orders" and page.get("source") == "/api/analytics/v1/order-feed":
                    if any(r.get("warehouse") == "Склад WB" and r.get("warehouse_type") == "Склад WB" for r in page["items"]):
                        state["anonymous_wb_demand"] = True
                    if page.get("source_exhausted"):
                        state["order_coverage"] = [page["coverage_from"], page["coverage_to"]]
                state["done"][section] = page["complete"]
                state["cursors"][section] = next_cursor
                if next_cursor is not None:
                    history.append(next_cursor)
                if len(state["orders"]) > 50000:
                    state["issues"].add("order_volume_exceeds_planning_bound")
                    checkpoint()
                    break
                checkpoint()
        wholesale_evidence = []
        if planning is not None:
            state["planning"]=planning
            for loc in state["locations"].values():
                loc.update(wholesale_daily={},known_stock_days=[],stockout_days=[])
            _wholesale(state, planning)
            wholesale_evidence = _seller_wholesale(wire, state, provider, article, since, as_of.isoformat())
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
        if provider == "ozon":
            try:
                _assign_clusters(state, wire)
            except (finance_gate.GateError, TimeoutError, urllib.error.URLError, finance_gate.ProviderResponseTooLarge) as exc:
                pending.append({"source": "cluster_directory", "status": exc.reason if isinstance(exc, finance_gate.GateError) else "unavailable"})
            checkpoint()
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
        missing_total_stock = [k for k in missing_stock if not (state.get("anonymous_wb_demand") and k == "Склад WB")]
        if state.get("stock_source") == "verified_daily_availability":
            # The collector only publishes an exhausted account-wide inventory. All its rows
            # contribute to the seller total even when some order warehouses have no stock row.
            missing_total_stock = []
        total = _market_total(state, horizon, as_of, missing_total_stock)
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
        all_wb_orders = None
        if provider == "wb" and nm and not state["done"]["orders"] and hasattr(wire, "deadline"):
            from market_composites import observed
            wide = observed(lambda: wb_expansion._funnel(
                {"article": article, "nm_id": nm, "date_from": since, "date_to": as_of.isoformat(), "limit": 100}, wire))
            rows = wide.get("items", [])
            count = rows[0].get("order_count") if wide.get("complete") and len(rows) == 1 else None
            all_wb_orders = {"ordered_units": count if type(count) is int and count >= 0 else None,
                             "period_from": since, "period_to": as_of.isoformat(), "fulfillment": "all",
                             "source": "/api/analytics/v3/sales-funnel/products",
                             "status": wide.get("status", "reported" if count is not None else "unknown"),
                             "retry_after_seconds": wide.get("retry_after_seconds")}
        return {
            "marketplace": provider,
            "article": article,
            "complete": complete,
            "source_complete": state["done"],
            "stock_source": state.get("stock_source"), "stock_observed_at": state.get("stock_observed_at"),
            "orders_coverage": state.get("order_coverage") or ([since, as_of.isoformat()] if state["done"]["orders"] else None),
            "orders_source": state.get("orders_source", "marketplace_report"),
            **({"all_wb_orders_90_days": all_wb_orders} if all_wb_orders is not None else {}),
            "seller_wholesale_observations": wholesale_evidence,
            "warehouse_demand_attribution": "marketplace_only" if state.get("anonymous_wb_demand") else "reported_warehouses",
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
        for row in [*report.get("locations", []), *report.get("clusters", []), report.get("marketplace_total", {})]:
            def display(value):
                return format(Decimal(value), ".4f").rstrip("0").rstrip(".") if value is not None else None
            if "days_of_cover" in row:
                row["days_of_cover"] = display(row["days_of_cover"])
                row["orders_per_day"] = {period: display(rate) for period, rate in row["orders_per_day"].items()}
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
