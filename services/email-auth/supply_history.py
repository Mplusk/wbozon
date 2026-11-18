"""Resume supply composition scans with a separate cursor for the list and each supply."""

import time
import wb_expansion
import ozon_expansion
from market_specs import result


def report(name, args, wire):
    oz = name == "ozon_supply_orders"
    article = args["article"]
    filters = {
        k: v
        for k, v in args.items()
        if k in ("date_from", "date_to", "states", "warehouse_id", "status_ids")
    }
    with wire.progress("article-supplies-v2", [name, article, filters]) as (saved, save):
        state = saved or {
            "list_cursor": None,
            "last_page": False,
            "supplies": [],
            "queue": [],
            "seen": [],
            "item_cursor": None,
            "parts": [],
            "seen_items": [],
        }
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
                    page = ozon_expansion._items(q, wire)
                else:
                    q.update(
                        supply_id=parent["supply_id"] or parent["preorder_id"],
                        is_preorder=not parent["supply_id"],
                        offset=marker or 0,
                    )
                    page = wb_expansion._goods(q, wire)
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
                    state.update(parts=[], item_cursor=None, seen_items=[])
            elif not state["last_page"]:
                q = dict(filters, limit=5)
                marker = state["list_cursor"]
                if marker is not None:
                    q["cursor" if oz else "offset"] = marker
                page = ozon_expansion._orders(q, wire) if oz else wb_expansion._supplies(q, wire)
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
                        state["queue"].append(
                            {
                                k: parent.get(k)
                                for k in (
                                    "supply_id",
                                    "preorder_id",
                                    "warehouse",
                                    "warehouse_id",
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
            else:
                break
            if len(state["supplies"]) > 5000:
                return result([], complete=False, status="narrow_period")
            save(state, 1800 if state["last_page"] and not state["queue"] else 7 * 86400)
        done = state["last_page"] and not state["queue"]
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        more = offset + limit < len(state["supplies"])
        return result(
            state["supplies"][offset : offset + limit],
            offset + limit if more else None,
            complete=done and not more,
            status="ready" if done else "pending",
            resume="repeat_same_arguments",
            period=filters,
            article=article,
            quantity_unit="pcs",
        )
