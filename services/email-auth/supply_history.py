"""Resume supply composition scans with a separate cursor for the list and each supply."""

import calendar
import math
import time
from datetime import date, datetime, timezone
import urllib.error
import finance_gate
import wb_expansion
import ozon_expansion
from market_specs import MOSCOW, result


def _filters(args):
    return {
        k: v
        for k, v in args.items()
        if k in ("date_from", "date_to", "states", "warehouse_id", "status_ids")
    }


def _wb_listing_filters(filters):
    first, last = (date.fromisoformat(filters[k]) for k in ("date_from", "date_to"))
    today = datetime.now(MOSCOW).date()
    month_end = last.replace(day=calendar.monthrange(last.year, last.month)[1])
    return dict(date_from=first.replace(day=1).isoformat(), date_to=max(last, min(month_end, today)).isoformat())


def _wb_article_scan_key(article, filters):
    return ["wb_supplies", article, _wb_listing_filters(filters)]


def saved_wb_page(args, wire):
    """Read a previously completed page without holding the live refresh lock."""
    if "article" not in args:
        query = {k: v for k, v in args.items() if k != "mode"}
        saved = wire.progress_snapshot("wb-supply-list-snapshot-v1", query)
        if saved.get("page", {}).get("status") == "ready":
            return dict(saved["page"], refresh_status="pending")
        return None
    filters = _filters(args)
    state = wire.progress_snapshot("wb-supplies-scan-v3", _wb_article_scan_key(args["article"], filters))
    finished = bool(state.get("finished_at"))
    if not finished and not state.get("last_ready"):
        return None
    return _supply_page(False, args, filters, state, finished, finished, [], refreshing=True)


def report(name, args, wire):
    oz = name == "ozon_supply_orders"
    article = args["article"]
    filters = _filters(args)
    key = [name, article, filters] if oz else _wb_article_scan_key(article, filters)
    with wire.progress("article-supplies-v2" if oz else "wb-supplies-scan-v3", key) as (saved, save):
        empty = {
            "list_cursor": None,
            "last_page": False,
            "supplies": [],
            "queue": [],
            "seen": [],
            "item_cursor": None,
            "parts": [],
            "seen_items": [],
        }
        now = time.time()
        state = saved or empty
        if not oz and state.get("finished_at") and now >= state["finished_at"] + 1800:
            state = dict(empty, last_ready={"items": state["supplies"], "scanned": state.get("scanned"),
                                           "at": state["finished_at"]})
            save(state, 30 * 86400)
        delays = []
        def identifiers(parent):
            return [field + ":" + str(parent[field]) for field in ("supply_id", "preorder_id") if parent.get(field)]
        seen_supplies = set(state.get("seen_wb_supply_ids", []))
        if not oz:
            for saved_parent in state["supplies"] + state["queue"]:
                seen_supplies.update(identifiers(saved_parent))
        def fetch(call, *params, **options):
            try:
                return call(*params, **options)
            except finance_gate.GateError as exc:
                delays.append({"status": exc.reason, "retry_after_seconds": exc.retry_after_seconds})
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                delay = getattr(exc, "retry_after_seconds", None)
                if delay is None:
                    delay, _ = finance_gate.parse_retry_after(exc.headers)
                delays.append({"status": "method_cooldown", "http_status": 429,
                               "retry_after_seconds": delay if delay is not None else 60})
                exc.close()
            except (TimeoutError, urllib.error.URLError):
                delays.append({"status": "provider_read_incomplete"})
            return None
        for _ in range(12):
            if time.monotonic() + 2 >= wire.deadline:
                break
            if state["queue"]:
                parent = state["queue"][0]
                q = {"article": article, "limit": 100}
                marker = state["item_cursor"]
                if oz:
                    q.update({k: parent[k] for k in ("supply_id", "order_id", "bundle_id")})
                    if marker:
                        q["cursor"] = marker
                    page = fetch(ozon_expansion._items, q, wire)
                else:
                    q.update(
                        supply_id=parent["supply_id"] or parent["preorder_id"],
                        is_preorder=not parent["supply_id"],
                        offset=marker or 0,
                    )
                    ttl = 86400 if parent.get("status_id") == 5 else 1800
                    page = fetch(wb_expansion._goods, q, wire, include_packages=False, snapshot_ttl=ttl)
                if page is None:
                    break
                state["parts"].extend(page["items"])
                marker = page["next_cursor"]
                if marker is not None:
                    if marker in state["seen_items"]:
                        return result(state["supplies"], complete=False, status="nonadvancing_items")
                    state["seen_items"].append(marker)
                    state["item_cursor"] = marker
                else:
                    parts = state["parts"]
                    if parts:
                        qty = [r.get("declared_quantity" if oz else "quantity") for r in parts]
                        accepted = [r.get("accepted_quantity") for r in parts]
                        if oz:
                            accepted = [
                                r.get("approved_quantity")
                                for r in page.get("acts", [])
                                if r.get("article") == article
                            ]
                            if not page.get("agreement_completed") or page.get("issues"):
                                accepted = []
                        total = (
                            lambda xs: sum(xs)
                            if xs and all(type(x) is int and x >= 0 for x in xs)
                            else None
                        )
                        state["supplies"].append(
                            dict(
                                parent,
                                article=article,
                                declared_units=total(qty),
                                accepted_units=total(accepted),
                                acceptance_status=page.get(
                                    "acceptance_status",
                                    "reported" if total(accepted) is not None else "unknown",
                                ),
                            )
                        )
                    state["queue"].pop(0)
                    state["scanned"] = state.get("scanned", 0) + 1
                    state.update(parts=[], item_cursor=None, seen_items=[])
            elif not state["last_page"]:
                q = dict(key[2], limit=5)
                marker = state["list_cursor"]
                if marker is not None:
                    q["cursor" if oz else "offset"] = marker
                if not oz:
                    q["limit"] = 100
                page = fetch(ozon_expansion._orders if oz else wb_expansion.supply_headers, q, wire)
                if page is None:
                    break
                if page.get("status") == "pending" and not page.get("next_cursor"):
                    break
                if page.get("issues") and not page.get("next_cursor"):
                    return result(state["supplies"], complete=False, issues=page["issues"])
                for parent in page["items"]:
                    if oz:
                        state["queue"].extend(
                            dict(
                                order_id=parent["order_id"], **{k: s[k] for k in ("bundle_id", "supply_id")}
                            )
                            for s in parent.get("supplies") or []
                            if s.get("bundle_id")
                        )
                    else:
                        ids = identifiers(parent)
                        if any(ident in seen_supplies for ident in ids):
                            continue
                        seen_supplies.update(ids)
                        state["queue"].append(
                            {
                                k: parent.get(k)
                                for k in (
                                    "supply_id",
                                    "preorder_id",
                                    "warehouse",
                                    "warehouse_id",
                                    "created_at",
                                    "supply_date",
                                    "status_id",
                                )
                            }
                        )
                marker = page["next_cursor"]
                if marker is not None and marker in state["seen"]:
                    return result(state["supplies"], complete=False, status="nonadvancing_list")
                state["last_page"] = marker is None
                if marker is not None:
                    state["seen"].append(marker)
                state["list_cursor"] = marker
                if not oz:
                    state["seen_wb_supply_ids"] = sorted(seen_supplies)
            else:
                break
            if len(state["supplies"]) > 5000:
                return result([], complete=False, status="narrow_period")
            save(state, 30 * 86400 if not oz else 1800 if state["last_page"] and not state["queue"] else 7 * 86400)
        # Names are enriched separately. A waiting detail must not discard goods or block discovery.
        if not oz:
            catalog = None
            directory_attempted = False
            for index, match in enumerate(state["supplies"]):
                retry_name = match.get("warehouse_name_status") == "retry_pending"
                if (not retry_name and (match.get("warehouse") or match.get("detail_checked"))) or time.monotonic() + 2 >= wire.deadline:
                    continue
                detail = dict(match, warehouse=None) if retry_name else fetch(wb_expansion.supply_detail, match, wire)
                if detail is None:
                    break
                warehouse_id = detail["warehouse_id"] or detail["actual_warehouse_id"]
                if not detail["warehouse"] and warehouse_id:
                    if not directory_attempted:
                        directory_attempted = True
                        wait = math.ceil(state.get("warehouse_directory_retry_at", 0) - time.time())
                        if wait > 0:
                            delays.append(dict(status="cooldown", reason="local_limiter", http_status=None, retry_after_seconds=wait))
                        else:
                            catalog = fetch(wb_expansion._warehouse_directory,
                                            {"include_warehouses": True, "include_fbs_offices": True}, wire, {}, required=True)
                            if catalog is None:
                                retry = delays[-1].get("retry_after_seconds") if delays else None
                                state["warehouse_directory_retry_at"] = time.time() + (retry if type(retry) is int else 60)
                            else:
                                state.pop("warehouse_directory_retry_at", None)
                    matching = [r for r in (catalog or {}).get("warehouses", [])
                                if str(r["warehouse_id"]) == str(warehouse_id) and r["name"]]
                    names = {r["name"] for r in matching}
                    if len(names) == 1:
                        name_from_id = names.pop()
                        detail.update(warehouse=name_from_id, warehouse_name=name_from_id, warehouse_basis="directory_by_id",
                                      warehouse_name_source=matching[0].get("source"))
                        detail.pop("warehouse_name_status", None)
                    else:
                        detail["warehouse_name_status"] = "retry_pending" if catalog is None else catalog.get("warehouses_status", "unavailable")
                state["supplies"][index] = dict(wb_expansion.display_supply_warehouse(detail),
                                                detail_checked=detail.get("warehouse_name_status") != "retry_pending")
                save(state, 30 * 86400)
        done = state["last_page"] and not state["queue"]
        details_done = oz or all((s.get("warehouse") or s.get("detail_checked")) and
                                 s.get("warehouse_name_status") != "retry_pending" for s in state["supplies"])
        ready = done and details_done
        if not oz and ready and not state.get("finished_at"):
            state["finished_at"] = time.time()
            state.pop("last_ready", None)
            save(state, 30 * 86400)
        return _supply_page(oz, args, filters, state, done, details_done, delays)


def _supply_page(oz, args, filters, state, done, details_done, delays, *, refreshing=False):
    ready = done and details_done
    snapshot = state.get("last_ready") if not ready else None
    records = snapshot["items"] if snapshot else state["supplies"]
    as_of = snapshot["at"] if snapshot else state.get("finished_at")
    candidates = records
    if not oz:
        candidates = [row for row in records if row.get("created_at") and
                      filters["date_from"] <= datetime.fromisoformat(row["created_at"]).astimezone(MOSCOW).date().isoformat()
                      <= filters["date_to"] and ("status_ids" not in filters or row.get("status_id") in filters["status_ids"])]
        if any(not row.get("created_at") for row in records):
            delays = [*delays, dict(status="supply_creation_date_missing", source="/api/v1/supplies")]
            ready = False
    selected = [s for s in candidates if oz or "warehouse_id" not in args or str(args["warehouse_id"]) in
                (str(s.get("warehouse_id")), str(s.get("actual_warehouse_id")))]
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    more = offset + limit < len(selected)
    return result(
        selected[offset : offset + limit], offset + limit if more else None,
        complete=(ready or bool(snapshot)) and not more,
        status="ready" if ready or snapshot else "partial" if done and details_done else "pending",
        scanned_supplies=snapshot.get("scanned") if snapshot else state.get("scanned"), matched_supplies=len(selected),
        composition_complete=done or bool(snapshot), warehouses_checked=details_done or bool(snapshot), pending_sources=delays,
        **({"warehouse_names_complete":bool(ready or snapshot) and all(r.get("warehouse_name") for r in selected),
            "warehouse_ids_without_name":sorted({str(r.get("warehouse_id") or r.get("actual_warehouse_id")) for r in selected
                                                if not r.get("warehouse_name") and (r.get("warehouse_id") or r.get("actual_warehouse_id"))})}
           if not oz else {}),
        **({"data_as_of": datetime.fromtimestamp(as_of, timezone.utc).isoformat()} if as_of else {}),
        refresh_status="pending" if snapshot or refreshing else "ready" if ready else "initial_scan",
        refresh_matched_supplies=len(state["supplies"]), resume="repeat_same_arguments",
        period=filters, article=args["article"], quantity_unit="pcs",
    )
