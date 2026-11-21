"""Read marketplace cards into the calculator's owner-reviewed inbox."""

from datetime import datetime, timezone
from difflib import SequenceMatcher
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading
import time
import urllib.error

import cost_catalog as catalog
import finance_gate
from market_specs import MOSCOW

MARKETS = {"ozon": "ozon_products", "wb": "wb_products"}
MAX_PAGES = 180


def _record(market, row):
    if market == "ozon":
        code = row.get("offer_id")
        ids = row.get("sku_candidates") or []
        name = row.get("name") or row.get("catalog_name")
        photos = row.get("images") or []
        dimensions = {}
    else:
        code = row.get("vendorCode")
        ids = [row["nmID"]] if row.get("nmID") else []
        name = row.get("title")
        photos = [photo.get("big") or photo.get("c516x688") for photo in row.get("photos", [])]
        source = row.get("dimensions") or {}
        dimensions = {key + "_cm": str(source[key]) for key in ("length", "width", "height") if source.get(key)}
        if source.get("weightBrutto"):
            dimensions["weight_g"] = str(Decimal(str(source["weightBrutto"])) * 1000)
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Missing catalog article")
    images = []
    for url in photos[:10]:
        try:
            images.append(catalog._photo(url))
        except (ValueError, TypeError):
            pass
    links = [catalog._link({"marketplace": market, "code": code, "marketplace_id": i}) for i in (ids or [None])]
    return dict(code=code, name=(name or code)[:200], photos=images, dimensions=dimensions, links=links,
                archived=row.get("is_archived") is True)


def _merge_variant(cards, card):
    code = card["code"]
    existing = cards.get(code)
    if not existing:
        cards[code] = card
        return
    links = {catalog._json(link): link for link in existing["links"] + card["links"]}
    existing["links"] = [links[key] for key in sorted(links)]
    existing["photos"] = sorted(set(existing["photos"] + card["photos"]))[:10]
    if existing["name"] != card["name"]:
        existing["name"] = code
    if existing["dimensions"] != card["dimensions"]:
        existing["dimensions"] = {}
    existing["archived"] = existing["archived"] and card["archived"]


def _suggest(card, products):
    query = card["code"].casefold()
    exact = [p for p in products if p["article"].casefold() == query or
             any(link["code"].casefold() == query for link in p["links"])]
    if len(exact) == 1:
        return exact[0]["article"], "exact_article"
    near = []
    for p in products:
        if p["status"] == "archived":
            continue
        similarities = [SequenceMatcher(None, query, p["article"].casefold()).ratio()]
        if card["name"] != card["code"]:
            similarities.append(SequenceMatcher(None, card["name"].casefold(), p["name"].casefold()).ratio())
        near.append((max(similarities), p["article"], "similar_title" if len(similarities) == 2 and
                     similarities[-1] >= similarities[0] else "similar_article"))
    near.sort(reverse=True)
    if near and near[0][0] >= .86 and (len(near) == 1 or near[0][0] - near[1][0] >= .06):
        return near[0][1], near[0][2]
    return None, "no_unique_match"


def _differences(local, remote):
    issues = {}
    for field, value in remote.items():
        if local.get(field) in (None, ""):
            continue
        before, after = Decimal(local[field]), Decimal(value)
        if before > 0 and after > 0:
            limit = before / 10 if field == "weight_g" else 1
            if abs(before - after) > limit:
                issues[field] = {"calculator": str(before), "marketplace": str(after)}
    return issues


def normalize_ozon_dimensions(raw):
    length = {"mm": Decimal("0.1"), "cm": Decimal(1), "m": Decimal(100)}.get(raw.get("dimension_unit"))
    mass = {"g": Decimal(1), "kg": Decimal(1000)}.get(raw.get("weight_unit"))
    result = {}
    if length:
        result.update({key + "_cm": format(Decimal(str(raw[key])) * length, "f")
                       for key in ("length", "width", "height") if raw.get(key)})
    if mass and raw.get("weight"):
        result["weight_g"] = format(Decimal(str(raw["weight"])) * mass, "f")
    return result


def _offer(conn, market, kind, article, card, fields, basis):
    value = dict(marketplace=market, kind=kind, suggested_article=article, product=card, fields=fields, basis=basis)
    serialized = catalog._json(value)
    key = hashlib.sha256(serialized.encode()).hexdigest()
    return conn.execute("""INSERT OR IGNORE INTO cost_inbox(fingerprint,market,status,at,data)
        VALUES(?,?,'pending',?,?)""", (key, market, datetime.now(timezone.utc).isoformat(), serialized)).rowcount


def publish(store, conn, market, records):
    by_code = {code: json.loads(raw) for code, raw in conn.execute(
        "SELECT code,data FROM cost_market_snapshots WHERE market=?", (market,))}
    products = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM cost_products")]
    owners = {p["code"]: card for card in products for p in card["links"] if p["marketplace"] == market}
    counts = {"new": 0, "changed": 0, "missing": 0, "dimensions": 0}
    for code, card in records.items():
        owner = owners.get(code)
        if not owner:
            target, basis = _suggest(card, products)
            counts["new"] += _offer(conn, market, "new", target, card, {}, basis)
            continue
        old = by_code.get(code)
        if old:
            fields = {key: {"previous": old.get(key), "current": card[key]} for key in
                      ("name", "photos", "archived", "links") if old.get(key) != card[key]}
            if fields:
                counts["changed"] += _offer(conn, market, "changed", owner["article"], card, fields, "catalog_change")
        if differences := _differences(owner["package"], card["dimensions"]):
            counts["dimensions"] += _offer(conn, market, "dimensions", owner["article"], card, differences, "package_comparison")
        elif card.get("wrong_volume_reported") and not card["dimensions"]:
            counts["dimensions"] += _offer(conn, market, "dimensions", owner["article"], card,
                                           {"units_unavailable": True}, "provider_wrong_volume")
    for code in owners.keys() & by_code.keys() - records.keys():
        counts["missing"] += _offer(conn, market, "missing", owners[code]["article"], by_code[code], {}, "absent_from_complete_scan")
    conn.execute("DELETE FROM cost_market_snapshots WHERE market=?", (market,))
    conn.executemany("INSERT INTO cost_market_snapshots VALUES(?,?,?)",
                     ((market, code, catalog._json(card)) for code, card in records.items()))
    return counts


def apply(store, ids, action, article=None, actor="owner"):
    if action not in ("accept", "reject", "ignore") or not ids or len(ids) > 100:
        raise catalog.CatalogError("Выберите предложения и действие.")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for ident in set(ids):
            row = conn.execute("SELECT data FROM cost_inbox WHERE id=? AND status='pending'", (ident,)).fetchone()
            if not row:
                continue
            offer = json.loads(row["data"])
            remote = offer["product"]
            target = article or offer["suggested_article"] or remote["code"]
            if action == "accept":
                existing = conn.execute("SELECT data,revision FROM cost_products WHERE article=?", (target,)).fetchone()
                current = json.loads(existing["data"]) if existing else {
                    "article": target, "status": "draft", "name": remote["name"], "photos": remote["photos"], "links": []}
                market = offer["marketplace"]
                if offer["kind"] == "missing":
                    current["links"] = [p for p in current["links"] if (p["marketplace"], p["code"]) != (market, remote["code"])]
                elif offer["kind"] == "dimensions":
                    current["package"].update(remote["dimensions"])
                else:
                    if offer["kind"] == "changed":
                        for field in ("name", "photos"):
                            if field in offer["fields"]:
                                current[field] = remote[field]
                    kept = [p for p in current["links"] if (p["marketplace"], p["code"]) != (market, remote["code"])]
                    current["links"] = kept + ([] if remote["archived"] else remote["links"])
                store._save_product(conn, current, catalog.today(), actor, existing["revision"] if existing else 0)
            conn.execute("UPDATE cost_inbox SET status=? WHERE id=?", (action, ident))
            store.audit(conn, actor, "inbox", str(ident), action, {"target": target})


def runs(store, limit=40):
    with store.connect() as conn:
        rows = conn.execute("SELECT id,day,at,trigger,status,data FROM cost_sync ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(id=r["id"], day=r["day"], started_at=r["at"], trigger=r["trigger"], status=r["status"],
                 marketplaces={m: {k: v for k, v in state.items() if k in ("pages", "scanned", "error", "counts", "state", "dimension_check")}
                               for m, state in json.loads(r["data"]).get("marketplaces", {}).items()}) for r in rows]


def _page(portal, market, state):
    from expansion_gateway import credentials
    operation = MARKETS[market]
    creds = credentials(portal.vault, operation)
    if not creds:
        return None, None, "not_configured"
    params = {"limit": 50}
    if market == "ozon":
        params["include_identity"] = True
        if state["cursor"]:
            params["last_id"] = state["cursor"]
    elif state["cursor"]:
        params["cursor"] = state["cursor"]
    result = portal.read_gateway(operation, params, creds)
    if not isinstance(result.get("items"), list):
        return None, None, result.get("status", "provider_response_invalid")
    following = result.get("next_cursor")
    if market == "ozon" and not following and not result.get("complete"):
        return None, None, "catalog_not_exhausted"
    if following and (following in state["cursors"] or state["pages"] >= MAX_PAGES):
        return None, None, "catalog_pagination_incomplete"
    return result["items"], following, None


def _read_dimensions(portal, state):
    from expansion_gateway import credentials
    params = {"mode": "details", "limit": 100}
    if state.get("dimension_task"):
        params["task_id"] = state["dimension_task"]
    result = portal.read_gateway("ozon_wrong_volume", params, credentials(portal.vault, "ozon_wrong_volume"))
    status = result.get("status") or ("ready" if result.get("complete") else "partial")
    state["dimension_check"] = status
    for item in result.get("items", []):
        if item.get("article") in state["cards"]:
            card = state["cards"][item["article"]]
            card["wrong_volume_reported"] = True
            card["dimensions"] = normalize_ozon_dimensions(item.get("provider_dimensions") or {})
    if status == "pending":
        state["dimension_task"] = result.get("task_id")
        delay = result.get("retry_after_seconds")
        return delay if type(delay) is int and delay > 0 else 10
    return None


def _worker(portal, store, run_id, payload):
    for market, state in payload["marketplaces"].items():
        if state.get("state") in ("ready", "failed"):
            continue
        if state.get("retry_at", 0) > time.time():
            continue
        rows, following, error = None, None, None
        wait = 0
        try:
            if state.get("catalog_complete"):
                rows = []
            else:
                rows, following, error = _page(portal, market, state)
            if rows is not None:
                for row in rows:
                    _merge_variant(state["cards"], _record(market, row))
                if market == "ozon" and not following:
                    state["catalog_complete"] = True
                    try:
                        retry = _read_dimensions(portal, state)
                        state["dimension_attempts"] = state.get("dimension_attempts", 0) + 1
                        if retry and state["dimension_attempts"] < 4:
                            state.update(scanned=len(state["cards"]), retry_at=time.time() + retry)
                            with store.connect() as conn:
                                conn.execute("UPDATE cost_sync SET data=? WHERE id=?", (catalog._json(payload), run_id))
                            continue
                        if retry:
                            state["dimension_check"] = "partial"
                    except (OSError, ValueError, KeyError, finance_gate.GateError):
                        state["dimension_check"] = "unavailable"
        except urllib.error.HTTPError as exc:
            error = f"http_{exc.code}"
            wait = finance_gate.parse_retry_after(exc.headers)[0] or 60
            exc.close()
        except finance_gate.GateError as exc:
            error, wait = exc.reason, exc.retry_after_seconds or 60
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            error, wait = "catalog_read_incomplete", 60
        with store.connect() as conn:
            if error:
                attempts = state.get("attempts", 0) + 1
                state.update(error=error, attempts=attempts, retry_at=time.time() + wait,
                             state="failed" if not wait or attempts >= 4 else "pending")
            else:
                state.update(pages=state["pages"] + bool(rows), scanned=len(state["cards"]), attempts=0, error=None,
                             cursor=following, retry_at=0)
                if following:
                    state["cursors"].append(following)
                else:
                    state.update(state="ready", counts=publish(store, conn, market, state["cards"]), cards={})
            conn.execute("UPDATE cost_sync SET data=? WHERE id=?", (catalog._json(payload), run_id))
    states = {entry["state"] for entry in payload["marketplaces"].values()}
    payload["lease"] = 0
    status = "pending" if "pending" in states else "partial" if "failed" in states else "ready"
    with store.connect() as conn:
        conn.execute("UPDATE cost_sync SET status=?,data=? WHERE id=?", (status, catalog._json(payload), run_id))


def kick(portal, *, manual=False, scheduled=False):
    if not catalog.Store.exists(portal.vault.path):
        if not manual and not scheduled:
            return
    now = datetime.now(MOSCOW)
    store = catalog.Store(portal.vault.path)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM cost_sync WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row:
            payload = json.loads(row["data"])
            if payload.get("lease", 0) > time.time():
                return row["id"]
            run_id = row["id"]
        else:
            if not manual and (not scheduled or conn.execute(
                    "SELECT 1 FROM cost_sync WHERE day=? AND trigger='nightly'", (now.date().isoformat(),)).fetchone()):
                return
            payload = {"marketplaces": {market: dict(state="pending", pages=0, scanned=0,
                                                     cursor=None, cursors=[], cards={}) for market in MARKETS}}
            run_id = conn.execute("INSERT INTO cost_sync(day,at,trigger,status,data) VALUES(?,?,?,?,?)",
                                  (now.date().isoformat(), now.isoformat(), "manual" if manual else "nightly",
                                   "pending", catalog._json(payload))).lastrowid
        if not any(p["state"] == "pending" and p.get("retry_at", 0) <= time.time()
                   for p in payload["marketplaces"].values()):
            return run_id
        payload["lease"] = time.time() + 120
        conn.execute("UPDATE cost_sync SET data=? WHERE id=?", (catalog._json(payload), run_id))
    threading.Thread(target=_worker, args=(portal, store, run_id, payload), name="cost-catalog-sync", daemon=True).start()
    return run_id
