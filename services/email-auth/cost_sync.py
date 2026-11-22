"""Read marketplace cards into the calculator's owner-reviewed inbox."""

from datetime import datetime, timezone
from collections import defaultdict
from difflib import SequenceMatcher
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error

import cost_catalog as catalog
import finance_gate
from market_specs import MOSCOW

MARKETS = {"ozon": "ozon_products", "wb": "wb_products"}
MAX_PAGES = 180
DIMENSION_FIELDS = ("length_cm", "width_cm", "height_cm", "weight_g")


def _measurement(raw, scale=1):
    try:
        value = Decimal(str(raw)) * scale
        if not value.is_finite() or not Decimal("0.000001") <= value <= Decimal("1000000000000"):
            return None
    except (ArithmeticError, ValueError, TypeError):
        return None
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


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
        dimensions = {key + "_cm": _measurement(source.get(key)) for key in ("length", "width", "height")}
        dimensions["weight_g"] = _measurement(source.get("weightBrutto"), 1000)
        dimensions = {key: value for key, value in dimensions.items() if value is not None}
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
                issues[field] = {"calculator": _measurement(before), "marketplace": _measurement(after)}
    return issues


def normalize_ozon_dimensions(raw):
    length = {"mm": Decimal("0.1"), "cm": 1, "m": 100}.get(str(raw.get("dimension_unit")).lower())
    mass = {"mg": Decimal("0.001"), "g": 1, "kg": 1000}.get(str(raw.get("weight_unit")).lower())
    result = {}
    if length:
        result.update(length_cm=_measurement(raw.get("length") or raw.get("depth"), length),
                      width_cm=_measurement(raw.get("width"), length),
                      height_cm=_measurement(raw.get("height"), length))
    if mass:
        result["weight_g"] = _measurement(raw.get("weight"), mass)
    return {key: value for key, value in result.items() if value is not None}


def _title(card):
    def tokens(value):
        value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        return tuple(re.findall(r"[^\W_]+", value))

    name = tokens(card["name"])
    return name if name != tokens(card["code"]) else ()


def _catalog_groups(snapshots):
    ozon, wb = ({code: card for code, card in snapshots.get(market, {}).items() if not card["archived"]}
                for market in MARKETS)
    exact = ozon.keys() & wb.keys()
    for code in sorted(exact):
        yield "same_marketplace_code", {"ozon": ozon[code], "wb": wb[code]}
    titles = defaultdict(lambda: defaultdict(list))
    for market, cards in (("ozon", ozon), ("wb", wb)):
        for code, card in cards.items():
            if title := _title(card):
                titles[title][market].append(code)
    used = {"ozon": set(exact), "wb": set(exact)}
    for found in titles.values():
        if len(found["ozon"]) == len(found["wb"]) == 1:
            left, right = found["ozon"][0], found["wb"][0]
            if left not in used["ozon"] and right not in used["wb"]:
                yield "same_catalog_title", {"ozon": ozon[left], "wb": wb[right]}
                used["ozon"].add(left)
                used["wb"].add(right)
    for market, cards in (("ozon", ozon), ("wb", wb)):
        for code in sorted(cards.keys() - used[market]):
            yield "create_article", {market: cards[code]}


def _source_keys(links):
    return {(link["marketplace"], link["code"]) for link in links}


def _reconcile_new(conn):
    snapshots = {market: {} for market in MARKETS}
    for market, code, raw in conn.execute("SELECT market,code,data FROM cost_market_snapshots"):
        card = json.loads(raw)
        card["dimensions"] = {key: value for key in DIMENSION_FIELDS
                              if (value := _measurement(card["dimensions"].get(key))) is not None}
        snapshots[market][code] = card
    products = [json.loads(row["data"]) for row in conn.execute("SELECT data FROM cost_products")]
    owners = {key: product["article"] for product in products for key in _source_keys(product["links"])}
    declined, pending = set(), {}
    for row in conn.execute("SELECT id,fingerprint,status,data FROM cost_inbox WHERE status IN ('pending','ignore','reject')"):
        data = json.loads(row["data"])
        if data["kind"] == "new":
            if row["status"] == "pending":
                pending[row["fingerprint"]] = row["id"]
            else:
                declined.update(_source_keys(data["product"]["links"]))
    counts = dict.fromkeys(MARKETS, 0)
    for basis, group in _catalog_groups(snapshots):
        group = {market: card for market, card in group.items() if (market, card["code"]) not in declined}
        keys = {(market, card["code"]) for market, card in group.items()}
        if not keys - owners.keys():
            continue
        primary, card = next(iter(group.items()))
        targets = {owners[key] for key in keys & owners.keys()}
        # An existing binding takes precedence over resemblance to another article.
        target = next(iter(targets)) if targets else None
        conflict = False
        if target is None:
            matches = {_suggest(remote, products) for remote in group.values()}
            exact = {article for article, reason in matches if article and reason == "exact_article"}
            if len(exact) == 1:
                target = next(iter(exact))
            elif len(exact) > 1:
                conflict = True
            elif len(group) == 1:
                target, _ = next(iter(matches))
            if len(group) == 1:
                basis = next(iter(matches))[1] if target else "create_article"
        if conflict:
            basis = "conflicting_articles"
        links = [link for remote in group.values() for link in remote["links"]]
        product = dict(card, links=links, photos=list(dict.fromkeys(
            photo for remote in group.values() for photo in remote["photos"]))[:10])
        value = dict(marketplace=primary, marketplaces=list(group), kind="new",
                     suggested_article=None if conflict else target or card["code"], product=product, fields={}, basis=basis,
                     marketplace_products={m: remote for m, remote in group.items()})
        key = hashlib.sha256(catalog._json(["calculator-new", sorted(keys)]).encode()).hexdigest()
        data = catalog._json(value)
        if key in pending:
            conn.execute("UPDATE cost_inbox SET data=? WHERE id=?", (data, pending.pop(key)))
        else:
            counts[primary] += conn.execute("""INSERT INTO cost_inbox(fingerprint,market,status,at,data)
                VALUES(?,?,'pending',?,?) ON CONFLICT(fingerprint) DO UPDATE SET data=excluded.data,status='pending'
                WHERE cost_inbox.status='superseded'""", (key, primary, datetime.now(timezone.utc).isoformat(), data)).rowcount
    conn.executemany("UPDATE cost_inbox SET status='superseded' WHERE id=?", ((ident,) for ident in pending.values()))
    return counts


def _offer(conn, market, kind, article, card, fields, basis):
    value = dict(marketplace=market, kind=kind, suggested_article=article, product=card, fields=fields, basis=basis)
    serialized = catalog._json(value)
    key = hashlib.sha256(serialized.encode()).hexdigest()
    return conn.execute("""INSERT OR IGNORE INTO cost_inbox(fingerprint,market,status,at,data)
        VALUES(?,?,'pending',?,?)""", (key, market, datetime.now(timezone.utc).isoformat(), serialized)).rowcount


def publish(store, conn, market, records, *, reconcile=True):
    by_code = {code: json.loads(raw) for code, raw in conn.execute(
        "SELECT code,data FROM cost_market_snapshots WHERE market=?", (market,))}
    products = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM cost_products")]
    owners = {p["code"]: card for card in products for p in card["links"] if p["marketplace"] == market}
    counts = {"new": 0, "changed": 0, "missing": 0, "dimensions": 0}
    for code, card in records.items():
        owner = owners.get(code)
        if not owner:
            continue
        old = by_code.get(code)
        if old:
            fields = {key: {"previous": old.get(key), "current": card[key]} for key in
                      ("name", "photos", "archived", "links") if old.get(key) != card[key]}
            if fields:
                counts["changed"] += _offer(conn, market, "changed", owner["article"], card, fields, "catalog_change")
        measurements = card.get("measured_dimensions") or card["dimensions"]
        if differences := _differences(owner["package"], measurements):
            counts["dimensions"] += _offer(conn, market, "dimensions", owner["article"], dict(card, dimensions=measurements),
                                           differences, "package_comparison")
        elif card.get("wrong_volume_reported") and not card.get("measured_dimensions"):
            counts["dimensions"] += _offer(conn, market, "dimensions", owner["article"], card,
                                           {"units_unavailable": True}, "provider_wrong_volume")
    for code in owners.keys() & by_code.keys() - records.keys():
        counts["missing"] += _offer(conn, market, "missing", owners[code]["article"], by_code[code], {}, "absent_from_complete_scan")
    conn.execute("DELETE FROM cost_market_snapshots WHERE market=?", (market,))
    conn.executemany("INSERT INTO cost_market_snapshots VALUES(?,?,?)",
                     ((market, code, catalog._json(card)) for code, card in records.items()))
    if reconcile:
        counts["new"] = _reconcile_new(conn)[market]
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
            if action == "accept" and offer["basis"] == "conflicting_articles" and not article:
                raise catalog.CatalogError("Для этого предложения укажите внутренний артикул вручную.")
            target = article or offer["suggested_article"] or remote["code"]
            if action == "accept":
                existing = conn.execute("SELECT data,revision FROM cost_products WHERE article=?", (target,)).fetchone()
                current = json.loads(existing["data"]) if existing else {
                    "article": target, "status": "draft", "name": remote["name"], "photos": remote["photos"], "links": []}
                market = offer["marketplace"]
                if offer["kind"] == "missing":
                    current["links"] = [p for p in current["links"] if (p["marketplace"], p["code"]) != (market, remote["code"])]
                elif offer["kind"] == "dimensions":
                    current["package"].update({key: remote["dimensions"][key] for key in offer["fields"] if key in remote["dimensions"]})
                elif offer["kind"] == "new":
                    links = {(p["marketplace"], p["code"], p["marketplace_id"]): p
                             for p in (*current["links"], *remote["links"])}
                    current["links"] = list(links.values())
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
    result = []
    public = {"pages", "scanned", "error", "counts", "state", "dimension_check", "catalog_dimensions_status",
              "catalog_dimensions_error", "catalog_products_with_dimensions"}
    for row in rows:
        payload = json.loads(row["data"])
        result.append(dict(id=row["id"], day=row["day"], started_at=row["at"], finished_at=payload.get("finished_at"),
                           trigger=row["trigger"], status=row["status"],
                           marketplaces={market: {key: value for key, value in state.items() if key in public}
                                         for market, state in payload.get("marketplaces", {}).items()}))
    return result


def sync_summary(store):
    recent = runs(store, 5)
    return dict(recent[0] if recent else {"status": "waiting_for_first_sync"},
                time_zone="Europe/Moscow", recent_runs=recent)


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
        return None, None, result.get("status") or "provider_response_invalid"
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
            card["measured_dimensions"] = normalize_ozon_dimensions(item.get("provider_dimensions") or {})
    if status == "pending":
        state["dimension_task"] = result.get("task_id")
        delay = result.get("retry_after_seconds")
        return delay if type(delay) is int and delay > 0 else 10
    return None


def _catalog_dimensions(portal, creds, state):
    """Fetch the declared package attributes separately from Ozon's discrepancy report."""
    if state.get("catalog_dimensions_status") in ("ready", "partial"):
        return None
    codes = sorted(state["cards"])
    offset = state.get("catalog_dimensions_offset", 0)
    batch = codes[offset:offset + 50]
    if batch:
        try:
            response = portal.read_gateway("market_api_read", {
                "operation_id": "ozon_product_attributes",
                "input": {"body": {"filter": {"offer_id": batch, "visibility": "ALL"}, "limit": len(batch)}},
            }, creds)
            rows = (response.get("data") or {}).get("result")
            if response.get("http_status") != 200 or not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Unexpected Ozon product attributes")
        except urllib.error.HTTPError as exc:
            error = f"http_{exc.code}"
            delay = finance_gate.parse_retry_after(exc.headers)[0] or 60
            exc.close()
        except finance_gate.GateError as exc:
            error, delay = exc.reason, exc.retry_after_seconds or 60
        except (OSError, TypeError, ValueError):
            error, delay = "catalog_dimensions_unavailable", 60
        else:
            error, delay = None, None
            attributes = defaultdict(list)
            for row in rows:
                code = row.get("offer_id")
                if isinstance(code, str) and code in batch:
                    attributes[code].append(normalize_ozon_dimensions(row))
            for code, choices in attributes.items():
                if len(choices) == 1:
                    state["cards"][code]["dimensions"] = choices[0]
        if error:
            state["catalog_dimensions_error"] = error
            state["catalog_dimensions_attempts"] = state.get("catalog_dimensions_attempts", 0) + 1
            if error not in ("http_401", "http_403", "http_404") and state["catalog_dimensions_attempts"] < 3:
                state["catalog_dimensions_status"] = "pending"
                return delay
        state["catalog_dimensions_offset"] = offset + len(batch)
        state["catalog_dimensions_attempts"] = 0
        if state["catalog_dimensions_offset"] < len(codes):
            state["catalog_dimensions_status"] = "pending"
            return 1
    count = sum(all(key in card["dimensions"] for key in DIMENSION_FIELDS) for card in state["cards"].values())
    state["catalog_products_with_dimensions"] = count
    state["catalog_dimensions_status"] = "ready" if count == len(codes) else "partial"
    if count == len(codes):
        state.pop("catalog_dimensions_error", None)
    return None


def _worker(portal, store, run_id, payload):
    for market, state in payload["marketplaces"].items():
        if state.get("state") in ("ready", "failed"):
            continue
        if state.get("retry_at", 0) > time.time():
            continue
        error, resume = None, None
        wait = 0
        try:
            if not state.get("catalog_complete"):
                rows, following, error = _page(portal, market, state)
                if rows is not None:
                    for card in [_record(market, row) for row in rows]:
                        _merge_variant(state["cards"], card)
                    state.update(pages=state["pages"] + 1, cursor=following, catalog_complete=following is None)
                    if following:
                        state["cursors"].append(following)
            if not error and state.get("catalog_complete") and market == "ozon":
                from expansion_gateway import credentials
                resume = _catalog_dimensions(portal, credentials(portal.vault, "ozon_products"), state)
                if resume is None:
                    try:
                        retry = _read_dimensions(portal, state)
                        state["dimension_attempts"] = state.get("dimension_attempts", 0) + 1
                        if retry and state["dimension_attempts"] < 4:
                            resume = retry
                        elif retry:
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
            conn.execute("BEGIN IMMEDIATE")
            if error:
                attempts = state.get("attempts", 0) + 1
                state.update(error=error, attempts=attempts, retry_at=time.time() + wait,
                             state="failed" if not wait or attempts >= 4 else "pending")
            else:
                state.update(scanned=len(state["cards"]), attempts=0, error=None, retry_at=time.time() + resume if resume else 0)
                if state.get("catalog_complete") and resume is None:
                    state.update(state="ready", counts=publish(store, conn, market, state["cards"], reconcile=False), cards={})
            conn.execute("UPDATE cost_sync SET data=? WHERE id=?", (catalog._json(payload), run_id))
    states = {entry["state"] for entry in payload["marketplaces"].values()}
    payload["lease"] = 0
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        status = "pending"
        if "pending" not in states:
            counts = _reconcile_new(conn)
            incomplete = "failed" in states
            for market, state in payload["marketplaces"].items():
                if state["state"] == "ready":
                    state["counts"]["new"] = counts[market]
                incomplete |= (state.get("catalog_dimensions_status") == "partial"
                               or state.get("dimension_check") not in (None, "ready"))
            status = "partial" if incomplete else "ready"
            payload["finished_at"] = datetime.now(MOSCOW).isoformat()
        conn.execute("UPDATE cost_sync SET status=?,data=? WHERE id=?", (status, catalog._json(payload), run_id))


def kick(portal, *, manual=False, scheduled=False):
    path = catalog.database_path(portal.vault.path)
    if not (manual or scheduled or catalog.Store.exists(path)):
        return
    now = datetime.now(MOSCOW)
    store = catalog.Store(path)
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
        pending = [p for p in payload["marketplaces"].values() if p["state"] == "pending"]
        if pending and not any(p.get("retry_at", 0) <= time.time() for p in pending):
            return run_id
        payload["lease"] = time.time() + 300
        conn.execute("UPDATE cost_sync SET data=? WHERE id=?", (catalog._json(payload), run_id))
    threading.Thread(target=_worker, args=(portal, store, run_id, payload), name="cost-catalog-sync", daemon=True).start()
    return run_id
