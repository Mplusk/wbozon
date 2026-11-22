import gc
import io
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import calculator_ui
import cost_catalog
import cost_integration
import cost_sync
from ui import CALCULATOR_CSP, CSP
import market_specs
from portal import LoginApp, Portal, Vault


def seed(store):
    start = "2026-09-01"
    store.save_reference("settings", "default", dict(electricity_per_kwh="6.50", defect_percent=5, labor_per_hour=120), start)
    store.save_reference("material", "black", dict(name="Test black PLA", type="PLA", color="black", price_per_kg=1450), start)
    store.save_reference("material", "white", dict(name="Test white PLA", type="PLA", color="white", price_per_kg=800), start)
    store.save_reference("printer", "f1", dict(name="Test printer", purchase_price=75000, life_hours=5000,
                         service_cost=15000, power_w=150), start)
    store.save_reference("packaging", "bag", dict(name="Test bag", unit="pcs", price="3.5"), start)
    store.save_reference("packaging", "roll", dict(name="Test roll", unit="m", price=6), start)
    store.save_reference("box", "box", dict(name="Test box", price=18, length_cm=20, width_cm=10,
                         height_cm=8, units_per_box=2), start)
    store.save_reference("marking", "mark", dict(name="Test label", code_price=".6", application_price=".5"), start)
    return store.save_product(dict(article="300030", name="Test product", status="active",
        links=[dict(marketplace="ozon", code="300030", marketplace_id="5215904379"),
               dict(marketplace="wb", code="3000030", marketplace_id="1294477629")],
        parts=[dict(name="body", quantity=1, printer="f1", hours="2.5", materials=[dict(id="black", grams=85)]),
               dict(name="clip", quantity=2, printer="f1", hours=".5", materials=[dict(id="white", grams=15)])],
        package=dict(configured=True, box="box", length_cm=19, width_cm=5, height_cm=7, weight_g=120,
                     consumables=[dict(id="bag", quantity=1), dict(id="roll", quantity=".3")]),
        marking_required=True, marking="mark", manual_minutes=3, other=1), start)


class CalculatorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(gc.collect)
        self.vault_path = str(Path(self.folder.name) / "vault.sqlite3")
        self.path = cost_catalog.database_path(self.vault_path)
        self.store = cost_catalog.Store(self.path)
        self.initial = seed(self.store)

    def test_two_parts_exact_cost_and_immutable_historical_price(self):
        version = self.store.at("300030", "2026-09-30")
        result = version["calculation"]
        self.assertEqual(result["plastic_grams"], "115")
        self.assertEqual(result["colors"], ["black", "white"])
        self.assertEqual(result["print_hours"], "3.5")
        expected = (Decimal("147.25") + Decimal("66.4125")) / Decimal(".95") + Decimal("22.4")
        self.assertEqual(result["amount"], cost_catalog.display(expected))
        self.assertEqual(result["components"]["packaging"]["amount"], "14.30")
        self.assertEqual(result["components"]["marking"]["amount"], "1.10")
        self.store.save_reference("material", "black", dict(name="Test black PLA", type="PLA", color="black",
                                  price_per_kg=1000), "2026-10-01")
        sept = self.store.at("300030", "2026-09-30")
        octo = self.store.at("300030", "2026-10-31")
        self.assertEqual(sept["version"], self.initial)
        self.assertNotEqual(sept["version"], octo["version"])
        self.assertLess(Decimal(octo["calculation"]["amount"]), Decimal(sept["calculation"]["amount"]))
        self.assertIsNone(self.store.uniform("300030", "2026-09-01", "2026-10-31"))
        self.assertGreater(len(self.store.journal()), 8)

    def test_tool_resolves_wb_code_and_margins_use_monthly_version(self):
        args = {"article": "3000030", "marketplace": "wb", "date": "2026-09-15"}
        market_specs.validate("cost_card", args)
        card_response = cost_catalog.read(self.store, "cost_card", args)
        self.assertEqual(card_response["internal_article"], "300030")
        request, card, early = cost_integration.prepare(self.store, "unit_economics",
                                                        {"article": "300030", "month": "2026-09"})
        self.assertIsNone(early)
        self.assertEqual(request["mapping"]["wb_vendor_code"], "3000030")
        rows = {"items": [{"marketplace": market, "sold_units": 10, "currency": "RUB",
                          "net_after_expenses": {"amount": "3000", "complete": True},
                          "amounts": {"gross_sales": "4000"}} for market in ("ozon", "wb")]}
        out = cost_integration.enrich(self.store, "unit_economics", request, card, rows)
        for row in out["items"]:
            self.assertEqual(row["cost_version"], self.initial)
            self.assertEqual(row["profit_status"], "calculated")
            self.assertIsNotNone(row["margin_percent"])
        self.assertEqual(len(self.store.margins("2026-09")), 2)

    def test_multiple_binding_codes_are_explicit_and_can_be_selected(self):
        card = self.store.resolve("300030")
        card["links"].append({"marketplace": "ozon", "code": "second", "marketplace_id": "5"})
        self.store.save_product(card, "2026-10-01")
        _, _, ambiguous = cost_integration.prepare(self.store, "ozon_sku_daily", {"article": "300030"})
        self.assertEqual(ambiguous["status"], "multiple_marketplace_bindings")
        _, _, report = cost_integration.prepare(self.store, "article_map", {"article": "3000030"})
        self.assertEqual(len(report["items"][0]["links"]), 3)
        chosen, _, ambiguous = cost_integration.prepare(self.store, "ozon_sku_daily", {"article": "300030", "sku": "5"})
        self.assertIsNone(ambiguous)
        self.assertEqual(chosen["article"], "second")
        selected, _, status = cost_integration.prepare(self.store, "ozon_product_info", {"offer_ids": ["300030"]})
        self.assertIsNone(status)
        self.assertEqual(selected["offer_ids"], ["300030", "second"])
        _, _, ambiguous = cost_integration.prepare(self.store, "supply_plan", {"article": "300030", "horizon_days": 10})
        self.assertEqual(ambiguous["status"], "multiple_marketplace_bindings")

    def test_sync_only_proposes_new_card_and_dimension_change(self):
        current = cost_sync._record("wb", {"vendorCode": "3000030", "nmID": 1294477629, "title": "Test",
                                    "dimensions": {"length": 21, "weightBrutto": ".147"}})
        unknown = cost_sync._record("wb", {"vendorCode": "absent", "nmID": 66, "title": "New thing"})
        with self.store.connect() as conn:
            counts = cost_sync.publish(self.store, conn, "wb", {i["code"]: i for i in (current, unknown)})
        self.assertEqual((counts["new"], counts["dimensions"]), (1, 1))
        self.assertIsNone(self.store.resolve("absent"))
        detected = self.store.inbox()
        dimensions = next(p for p in detected if p["kind"] == "dimensions")
        self.assertEqual(dimensions["fields"]["length_cm"]["marketplace"], "21")
        new = next(p for p in detected if p["kind"] == "new")
        cost_sync.apply(self.store, [new["id"]], "accept")
        self.assertEqual(self.store.resolve("absent")["status"], "draft")
        self.assertEqual(self.store.at("absent", None)["calculation"]["status"], "incomplete")

    def test_cross_market_proposals_replace_pending_duplicates_and_preserve_owner_decisions(self):
        store = cost_catalog.Store(Path(self.folder.name) / "incoming.sqlite")
        ozon = {code: cost_sync._record("ozon", dict(offer_id=code, name=name, sku_candidates=[sku]))
                for code, name, sku in (("300200", "Ёлка", "12"), ("300030", "Салфетница «Вершина»", "13"),
                                        ("a1", "Чаша", "14"), ("a2", "Чаша", "15"), ("ignored", "Скрытая", "16"))}
        wb = {code: cost_sync._record("wb", dict(vendorCode=code, title=name, nmID=nm))
              for code, name, nm in (("300200", "Ёлочка", 22), ("3000030", 'салфетница "Вершина"', 23),
                                     ("b1", "Чаша", 24))}
        with store.connect() as conn:
            for market, cards in (("ozon", ozon), ("wb", wb)):
                for card in cards.values():
                    cost_sync._offer(conn, market, "new", None, card, {}, "no_unique_match")
                cost_sync.publish(store, conn, market, cards, reconcile=False)
        ignored = next(p for p in store.inbox() if p["product"]["code"] == "ignored")
        cost_sync.apply(store, [ignored["id"]], "ignore")
        with store.connect() as conn:
            self.assertEqual(sum(cost_sync._reconcile_new(conn).values()), 5)
        offers = store.inbox()
        groups = {p["suggested_article"]: p for p in offers if len(p["marketplaces"]) == 2}
        self.assertEqual({article: p["basis"] for article, p in groups.items()},
                         {"300030": "same_catalog_title", "300200": "same_marketplace_code"})
        self.assertEqual(len(offers), 5)
        self.assertEqual(store.products(), [])
        self.assertEqual([p["id"] for p in store.inbox("ignore")], [ignored["id"]])
        self.assertEqual(len(store.inbox("superseded")), 7)
        with store.connect() as conn:
            self.assertEqual(sum(cost_sync._reconcile_new(conn).values()), 0)
        self.assertEqual({p["id"] for p in store.inbox()}, {p["id"] for p in offers})
        cost_sync.apply(store, [groups["300030"]["id"]], "accept")
        self.assertEqual(store.resolve("3000030", "wb")["article"], "300030")
        self.assertEqual(store.resolve("300030")["status"], "draft")
        self.assertEqual(len(store.products()), 1)
        self.assertEqual({(link["marketplace"], link["code"]) for link in store.resolve("300030")["links"]},
                         {("ozon", "300030"), ("wb", "3000030")})

    def test_provider_package_units_comparison_and_sync_status(self):
        ozon = {"offer_id": "300030", "name": "Test product", "sku_candidates": ["5215904379"]}
        wb = {"vendorCode": "3000030", "title": "Test product", "nmID": 1294477629,
              "dimensions": {"length": 19, "width": "5.00", "height": 7, "weightBrutto": ".1000"}}
        def gateway(name, args, credentials):
            if name == "market_api_read":
                self.assertEqual(args["operation_id"], "ozon_product_attributes")
                self.assertEqual(args["input"]["body"]["filter"]["offer_id"], ["300030"])
                return {"http_status": 200, "data": {"result": [
                    dict(offer_id="300030", depth=190, width=55, height=70, dimension_unit="mm",
                         weight="85.00", weight_unit="g"), dict(offer_id="other", weight=125, weight_unit="g")]}}
            if name == "ozon_wrong_volume":
                return {"items": [], "status": "ready", "complete": True}
            return {"items": [ozon if name == "ozon_products" else wb], "complete": True}

        app = SimpleNamespace(vault=object(), read_gateway=gateway)
        started = "2026-11-22T03:00:11+03:00"
        payload = {"marketplaces": {market: dict(state="pending", pages=0, scanned=0, cursor=None, cursors=[], cards={})
                                    for market in cost_sync.MARKETS}}
        with self.store.connect() as conn:
            ident = conn.execute("INSERT INTO cost_sync(day,at,trigger,status,data) VALUES(?,?,?,?,?)",
                ("2026-11-22", started, "nightly", "pending", cost_catalog._json(payload))).lastrowid
        with patch("expansion_gateway.credentials", return_value={"api_key": "test"}):
            cost_sync._worker(app, self.store, ident, payload)
        published = cost_catalog.read(self.store, "cost_inbox", {"limit": 20})
        self.assertEqual(published["calculator_sync"]["started_at"], started)
        self.assertIsNotNone(published["calculator_sync"]["finished_at"])
        self.assertEqual(published["calculator_sync"]["status"], "ready")
        self.assertEqual(published["calculator_sync"]["marketplaces"]["ozon"]["catalog_products_with_dimensions"], 1)
        plans = {p["marketplace"]: p for p in published["items"]}
        self.assertEqual(plans["ozon"]["product"]["dimensions"], {
            "length_cm": "19", "width_cm": "5.5", "height_cm": "7", "weight_g": "85"})
        self.assertEqual(plans["wb"]["fields"]["weight_g"], {"calculator": "120", "marketplace": "100"})
        self.assertEqual(plans["ozon"]["fields"]["weight_g"]["marketplace"], "85")

    def test_margin_and_supply_report_show_cost_availability_at_normal_entry_point(self):
        login = LoginApp(str(Path(self.folder.name) / "login.sqlite3"), "s" * 40, "https://test.example")
        def gateway(name, args, creds):
            return {"items": [
                {"marketplace": "ozon", "sold_units": 4, "currency": "RUB",
                 "amounts": {"gross_sales": "1500"}, "net_after_expenses": {"amount": "1300", "complete": True},
                 "marketplace_total": {"ship_by_destination_after_min_batch": 4}},
                {"marketplace": "wb", "sold_units": 0, "currency": "RUB",
                 "marketplace_total": {"ship_by_destination_after_min_batch": None}},
            ]}
        portal = Portal(login, Vault(self.vault_path, b"v" * 32), "f" * 64, gateway=gateway,
                        finance_gate_dir=str(Path(self.folder.name) / "gate"))
        report = portal.read_gateway("unit_economics", {"article": "unknown-1", "month": "2026-10"}, {})
        self.assertEqual(report["production_cost_status"], "cost_card_not_found")
        self.assertEqual(report["items"][0]["margin_status"], "cost_card_not_found")
        self.assertIsNone(report["items"][0]["margin_per_unit"])
        plan = portal.read_gateway("supply_plan", {"article": "unknown-1", "horizon_days": 15}, {})
        self.assertEqual(plan["items"][0]["production_requirements"]["status"], "cost_card_not_found")
        self.assertIsNone(plan["items"][0]["production_requirements"]["plastic_kg"])
        request = {"article": "300030", "horizon_days": 15}
        plan = portal.read_gateway("supply_plan", request, {})
        self.assertEqual(plan["items"][0]["production_requirements"]["plastic_kg"], "0.46")
        self.assertEqual(plan["items"][1]["production_requirements"]["status"], "recommended_quantity_unavailable")
        self.assertEqual(plan["items"][1]["production_requirements"]["plastic_g_per_unit"], "115")

    def test_csv_migrates_references_and_card_together(self):
        new = cost_catalog.Store(str(Path(self.folder.name) / "copy.sqlite3"))
        self.assertEqual(new.csv_import(self.store.csv_export(), "owner"), 9)
        self.assertEqual(new.resolve("3000030")["article"], "300030")
        self.assertIsNotNone(new.at("300030", None)["calculation"]["amount"])

    def test_browser_owner_session_and_origin_are_required(self):
        login = LoginApp(str(Path(self.folder.name) / "login.sqlite3"), "s" * 40, "https://test.example")
        portal = Portal(login, Vault(self.vault_path, b"v" * 32), "f" * 64,
                        finance_gate_dir=str(Path(self.folder.name) / "gate"))
        sid, csrf = "d" * 43, "a" * 32
        with cost_catalog.Store(self.path).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cost_products").fetchone()[0], 1)
        conn = login.connect()
        try:
            with conn:
                conn.execute("INSERT INTO sessions(hash,expires,csrf) VALUES (?,?,?)", (login.digest(sid), login.clock() + 1800, csrf))
        finally:
            conn.close()

        def call(method="GET", cookie=None, origin=None, fields=None, query="tab=calculator&view=card&article=300030"):
            raw = urlencode(fields or {}).encode()
            env = {"PATH_INFO": "/settings", "QUERY_STRING": query,
                   "REQUEST_METHOD": method, "HTTP_HOST": "test.example", "HTTP_COOKIE": cookie or "",
                   "HTTP_ORIGIN": origin, "CONTENT_TYPE": "application/x-www-form-urlencoded",
                   "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
            captured = []
            output = portal(env, lambda status, headers: captured.append((status, headers)))
            status, headers = captured[0]
            return status, b"".join(output).decode(), dict(headers)

        self.assertEqual(call()[0], "401 Unauthorized")
        self.assertEqual(call()[2]["Content-Security-Policy"], CSP)
        status, page, headers = call(cookie="__Host-sid=" + sid)
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Security-Policy"], CALCULATOR_CSP)
        self.assertEqual(call(cookie="__Host-sid=" + sid, query="")[2]["Content-Security-Policy"], CSP)
        self.assertIn("История себестоимости", page)
        self.assertIn("Test black PLA", page)
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://another.example", {"csrf": csrf})[0], "403 Forbidden")
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", {"csrf": "wrong"})[0], "403 Forbidden")
        fields = {"csrf": csrf, "action": "save_product", "article": "draft-1", "name": "Test draft",
                  "status": "draft", "revision": "0", "effective": "2026-11-01"}
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", fields)[0], "303 See Other")
        self.assertEqual(self.store.resolve("draft-1")["status"], "draft")
