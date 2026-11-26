import gc
import io
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

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

    def test_reference_code_edit_keeps_links_and_effective_costs(self):
        material = dict(name="Test black PLA", type="PLA", color="black", price_per_kg=1450)
        previous = self.store.at("300030", "2026-09-30")
        self.store.save_reference("material", "black", dict(material, code="NEW-01"), "2026-10-15")
        saved = next(r for r in self.store.references() if r["kind"] == "material" and r["id"] == "black")
        self.assertEqual((saved["code"], saved["effective_from"]), ("NEW-01", "2026-09-01"))
        self.assertEqual(self.store.at("300030", "2026-10-31"), previous)
        self.assertIsNotNone(self.store.uniform("300030", "2026-10-01", "2026-10-31"))

        with self.assertRaisesRegex(cost_catalog.CatalogError, "код уже используется"):
            self.store.save_reference("material", "NEW-01", material, "2026-10-20", create=True)
        new_ident = self.store.save_reference("material", "black", material, "2026-10-20", create=True)
        self.assertNotEqual(new_ident, "black")
        self.assertEqual(next(r["code"] for r in self.store.references() if r["id"] == new_ident), "black")

        self.store.save_reference("material", "black", dict(material, price_per_kg=1000), "2026-11-01")
        updated = self.store.at("300030", "2026-11-22")
        self.assertLess(Decimal(updated["calculation"]["amount"]), Decimal(previous["calculation"]["amount"]))
        self.assertEqual(updated["product"]["parts"][0]["materials"][0]["id"], "black")
        self.assertEqual(self.store.at("300030", "2026-10-31"), previous)
        copy = cost_catalog.Store(Path(self.folder.name) / "codes-copy.sqlite")
        copy.csv_import(self.store.csv_export(), "owner")
        self.assertEqual(next(r["code"] for r in copy.references() if r["id"] == "black"), "NEW-01")

    def test_lamp_components_are_costed_by_quantity_and_effective_price(self):
        refs = [("cable", "Кабель", "m", "58.40", "1,25"),
                ("socket", "Цоколь", "pcs", "46", "1"),
                ("bulb", "Лампочка", "pcs", "125", "1"),
                ("collet", "Цанга", "pcs", "17.50", "2")]
        for ident, name, unit, price, _ in refs:
            self.store.save_reference("component", ident, dict(name=name, unit=unit, price=price), "2026-10-01")
        base = self.store.at("300030", "2026-09-30", redact_exact=False)["calculation"]
        card = self.store.resolve("300030")
        card["components"] = [dict(id=ident, quantity=quantity) for ident, _, _, _, quantity in refs]
        self.store.save_product(card, "2026-10-01")

        october = cost_catalog.read(self.store, "cost_card", {"article": "300030", "date": "2026-10-31"})["items"][0]["calculation"]
        self.assertEqual(october["amount"], cost_catalog.display(Decimal(base["exact"]) + Decimal("279")))
        self.assertEqual(october["components"]["components"]["amount"], "279.00")
        self.assertEqual(october["components"]["defects"]["amount"], base["components"]["defects"]["amount"])
        self.assertEqual(october["plastic_grams"], base["plastic_grams"])
        self.assertEqual([(row["name"], row["unit"], row["quantity"], row["amount"]) for row in october["component_items"]],
                         [("Кабель", "m", "1.25", "73.00"), ("Цоколь", "pcs", "1", "46.00"),
                          ("Лампочка", "pcs", "1", "125.00"), ("Цанга", "pcs", "2", "35.00")])
        self.store.save_reference("component", "cable", dict(name="Кабель", unit="m", price="70.40"), "2026-11-01")
        november = self.store.at("300030", "2026-11-22")["calculation"]
        self.assertEqual(november["components"]["components"]["amount"], "294.00")
        self.assertEqual(self.store.at("300030", "2026-10-31")["calculation"], october)
        self.assertEqual(self.store.at("300030", "2026-09-30", redact_exact=False)["calculation"], base)

        card["components"][1]["quantity"] = "0.5"
        with self.assertRaisesRegex(cost_catalog.CatalogError, "целым количеством"):
            self.store.save_product(card, "2026-11-22")
        self.assertEqual(self.store.at("300030", "2026-11-22")["calculation"], november)
        copy = cost_catalog.Store(Path(self.folder.name) / "lamp-copy.sqlite")
        copy.csv_import(self.store.csv_export(), "owner")
        self.assertEqual(copy.at("300030", None)["calculation"]["component_items"], november["component_items"])

    def _owner_portal(self):
        login = LoginApp(str(Path(self.folder.name) / "login.sqlite3"), "s" * 40, "https://test.example")
        portal = Portal(login, Vault(self.vault_path, b"v" * 32), "f" * 64,
                        finance_gate_dir=str(Path(self.folder.name) / "gate"))
        sid, csrf = "d" * 43, "a" * 32
        conn = login.connect()
        try:
            with conn:
                conn.execute("INSERT INTO sessions(hash,expires,csrf) VALUES (?,?,?)", (login.digest(sid), login.clock() + 1800, csrf))
        finally:
            conn.close()

        def call(method="GET", cookie=None, origin=None, fields=None, query="tab=calculator&view=card&article=300030"):
            raw = urlencode(fields or {}, doseq=True).encode()
            env = {"PATH_INFO": "/settings", "QUERY_STRING": query,
                   "REQUEST_METHOD": method, "HTTP_HOST": "test.example", "HTTP_COOKIE": cookie or "",
                   "HTTP_ORIGIN": origin, "CONTENT_TYPE": "application/x-www-form-urlencoded",
                   "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
            captured = []
            output = portal(env, lambda status, headers: captured.append((status, headers)))
            status, headers = captured[0]
            return status, b"".join(output).decode(), dict(headers)

        return call, sid, csrf

    def test_browser_owner_session_and_origin_are_required(self):
        call, sid, csrf = self._owner_portal()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cost_products").fetchone()[0], 1)
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
        self.assertIn("Добавить комплектующее в справочник", page)
        reference = {"csrf": csrf, "action": "save_reference", "kind": "component", "ref_id": "cable",
                     "ref_name": "Кабель", "active": "1", "unit": "m", "price": "55", "effective": "2026-11-01"}
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", reference,
                              query="tab=calculator&view=references&kind=component")[0], "303 See Other")
        fields = {"csrf": csrf, "action": "save_product", "article": "draft-1", "name": "Test draft",
                  "status": "draft", "revision": "0", "effective": "2026-11-01",
                  "component_id": ["cable", ""], "component_quantity": ["1,25", ""]}
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", fields)[0], "303 See Other")
        self.assertEqual(self.store.resolve("draft-1")["status"], "draft")
        self.assertEqual(self.store.resolve("draft-1")["components"], [{"id": "cable", "quantity": "1.25"}])
        self.assertIn('value="cable" selected>Кабель · м', call(cookie="__Host-sid=" + sid,
                      query="tab=calculator&view=card&article=draft-1")[1])
        edit_material = {"csrf": csrf, "action": "save_reference", "kind": "material", "fixed_id": "black",
                         "ref_id": "PETG-NEW", "ref_name": "Тестовый PETG", "active": "1", "price_per_kg": "885",
                         "material_type": "PETG", "color": "Оранжевый", "supplier": "Тестовый поставщик",
                         "effective": "2026-11-01"}
        query = "tab=calculator&view=references&kind=material&id=black"
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", edit_material, query=query)[0], "303 See Other")
        status, page, _ = call(cookie="__Host-sid=" + sid, query=query)
        self.assertEqual(status, "200 OK")
        listing = page.split("</table>", 1)[0]
        for value in ("Цена за кг, ₽", "Тип пластика", "Цвет", "Поставщик", "Действует с", "Изменить",
                      "PETG-NEW", "885", "Оранжевый", "Тестовый поставщик", "01.11.2026"):
            self.assertIn(value, listing)
        self.assertIn('name="ref_id" value="PETG-NEW"', page)
        self.assertIn('value="saved:Оранжевый" selected>Оранжевый', page)
        self.assertIn('value="saved:white">white', page)

        color_card = dict(fields, article="test-colors", pack_ready="1", part_name=["body", "clip", "base", ""],
                          part_count=["1", "1", "1", "1"], part_printer=["f1", "f1", "f1", ""],
                          part_hours=["1", "1", "1", ""], part_material=["black", "white", "white", ""],
                          part_weight=["40", "20", "15", ""], part_color=["saved:Оранжевый", "new", "", ""],
                          part_color_new=["", "  Бордовый  ", "", ""])
        status, _, headers = call("POST", "__Host-sid=" + sid, "https://test.example",
                                  dict(color_card, part_color_new=["", "", "", ""]))
        self.assertEqual(status, "303 See Other")
        draft_query = urlsplit(headers["Location"]).query
        status, draft_page, _ = call(cookie="__Host-sid=" + sid, query=draft_query)
        self.assertEqual(status, "200 OK")
        self.assertIn('value="new" selected', draft_page)
        self.assertIn('Введённые данные сохранены в черновике', draft_page)
        color_card["draft_id"] = parse_qs(draft_query)["draft"][0]
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", color_card)[0], "303 See Other")
        parts = self.store.resolve("test-colors")["parts"]
        self.assertEqual([part["color"] for part in parts], ["Оранжевый", "Бордовый", ""])
        self.assertCountEqual(self.store.at("test-colors", None)["calculation"]["colors"], ["Оранжевый", "Бордовый", "white"])
        _, card_page, _ = call(cookie="__Host-sid=" + sid, query="tab=calculator&view=card&article=test-colors")
        self.assertIn('value="saved:Бордовый" selected>Бордовый', card_page)
        self.assertIn('value="" selected>Из материала', card_page)
        self.assertIn('value="new">Новый цвет…', card_page)
        self.assertIn('name="part_color_new"', card_page)

        new_material = dict(edit_material, fixed_id="", ref_id="new-material", color="saved:Бордовый", color_new="")
        self.assertEqual(call("POST", "__Host-sid=" + sid, "https://test.example", new_material, query=query)[0], "303 See Other")
        self.assertEqual(next(r["color"] for r in self.store.references() if r["id"] == "new-material"), "Бордовый")
        self.assertIn('value="saved:Бордовый">Бордовый', call(cookie="__Host-sid=" + sid, query=query)[1])

    def test_card_activation_with_details_and_packaging_but_missing_shared_settings(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        day = "2026-11-24"
        self.store.save_reference("settings", "default", {}, day)
        fields = {"csrf": csrf, "action": "save_product", "article": "coral-test", "name": "Чаша корал",
                  "status": "draft", "revision": "0", "effective": day, "pack_ready": "1", "pack_box": "box",
                  "part_name": ["Чаша корал", ""], "part_count": ["1", "1"], "part_printer": ["f1", ""],
                  "part_hours": ["5.45", ""], "part_material": ["black", ""], "part_weight": ["119", ""],
                  "part_color": ["saved:black", ""], "part_color_new": ["", ""]}
        query = "tab=calculator&view=card&article=coral-test"
        self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")

        with patch("cost_catalog.today", return_value=day):
            _, page, _ = call(cookie=cookie, query=query)
            details = page.split('data-cost-rows="parts">', 1)[1].split('<template id="cost-parts">', 1)[0]
            self.assertEqual(details.count('name="part_name"'), 1)
            self.assertIn('type="button" data-cost-remove', details)
            self.assertIn('data-cost-add="parts"', page)

            fields.update(status="active", existing="coral-test", revision="1", comment="Проверить упаковку")
            removed_parts = {key: value for key, value in fields.items() if not key.startswith("part_")}
            status, page, headers = call("POST", cookie, origin, removed_parts, query)
            self.assertEqual(status, "303 See Other")
            draft_query = urlsplit(headers["Location"]).query
            draft_id = parse_qs(draft_query)["draft"][0]
            status, page, headers = call(cookie=cookie, query=draft_query)
            self.assertEqual(status, "200 OK")
            self.assertEqual(headers["Content-Security-Policy"], CALCULATOR_CSP)
            self.assertIn('role="alert"', page)
            self.assertIn("Добавьте хотя бы одну деталь.", page)
            self.assertIn('value="active" selected>В работе', page)
            self.assertIn('name="comment" value="Проверить упаковку"', page)
            self.assertIn('value="box" selected', page)
            self.assertEqual(self.store.resolve("coral-test")["status"], "draft")
            self.assertEqual(len(self.store.history("coral-test")), 1)

            invalid_quantity = dict(fields, pack_id=["bag"], pack_quantity=["0.5"], draft_id=draft_id)
            status, _, headers = call("POST", cookie, origin, invalid_quantity, query)
            self.assertEqual(status, "303 See Other")
            status, page, _ = call(cookie=cookie, query=urlsplit(headers["Location"]).query)
            self.assertEqual(status, "200 OK")
            self.assertIn("Штучные расходники указываются целым количеством.", page)

            fields["draft_id"] = draft_id
            self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")
            _, page, _ = call(cookie=cookie, query=query)
            self.assertIn('value="active" selected>В работе', page)
            self.assertIn("тариф электроэнергии", page)
            self.assertIn("процент брака", page)
            self.assertIn('kind=settings">Открыть общие настройки', page)
        current = self.store.at("coral-test", day)
        self.assertEqual(current["product"]["status"], "active")
        self.assertEqual(current["calculation"]["missing"], ["electricity_per_kwh", "defect_percent"])
        self.assertEqual(current["calculation"]["status"], "incomplete")
        self.assertIsNone(self.store.uniform("coral-test", day, day))

        next_day = "2026-11-25"
        self.store.save_reference("settings", "default", {"electricity_per_kwh": "6.5", "defect_percent": "0"}, next_day)
        self.assertEqual(self.store.uniform("coral-test", next_day, next_day)["calculation"]["status"], "calculated")
        self.assertEqual(self.store.at("coral-test", day), current)

    def test_backdated_card_shows_its_calculation_and_can_apply_today(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        query = "tab=calculator&view=card&article=lamp-test"
        self.store.save_product(dict(article="lamp-test", name="Лампа", status="draft"), "2026-11-25")
        fields = {"csrf": csrf, "action": "save_product", "article": "lamp-test", "existing": "lamp-test",
                  "revision": "1", "name": "Лампа", "status": "active", "effective": "2026-10-26",
                  "part_name": ["Основание", "Плафон"], "part_count": ["1", "1"],
                  "part_printer": ["f1", "f1"], "part_hours": ["1.42", "5.45"],
                  "part_material": ["black", "black"], "part_weight": ["27", "27"],
                  "part_color": ["", ""], "part_color_new": ["", ""],
                  "pack_ready": "1", "pack_box": "box", "weight_g": "50", "length_cm": "20",
                  "width_cm": "20", "height_cm": "30", "pack_id": ["bag"], "pack_quantity": ["2"]}
        with patch("cost_catalog.today", return_value="2026-11-26"):
            self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")
            old_day = self.store.at("lamp-test", "2026-11-25")
            self.assertEqual(old_day["calculation"]["missing"], ["parts", "package"])
            _, page, _ = call(cookie=cookie, query=query)
            self.assertIn("Сегодня действует другая версия с 25.11.2026", page)
            self.assertIn("Расчёт последнего сохранения · на 26.10.2026", page)
            self.assertIn("235.64 ₽", page)
            self.assertIn('name="apply_today" value="1"', page)
            self.assertIn("Расчёт, действующий сегодня", page)

            invalid = dict(fields, revision="2", apply_today="1", part_hours=["ошибка", "5.45"])
            status, _, headers = call("POST", cookie, origin, invalid, query)
            self.assertEqual(status, "303 See Other")
            draft_query = urlsplit(headers["Location"]).query
            draft_id = parse_qs(draft_query)["draft"][0]
            _, page, _ = call(cookie=cookie, query=draft_query)
            self.assertIn('name="effective" value="2026-11-26"', page)
            self.assertIn('name="part_hours" value="ошибка"', page)
            self.assertEqual(self.store.at("lamp-test", "2026-11-25"), old_day)

            fields.update(revision="2", apply_today="1", draft_id=draft_id)
            self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")
            current = self.store.at("lamp-test", "2026-11-26")
            self.assertEqual(current["calculation"]["status"], "calculated")
            expected = (Decimal("78.30") + Decimal("6.87") * Decimal("18.975")) / Decimal(".95") + Decimal("16")
            self.assertEqual(current["calculation"]["amount"], cost_catalog.display(expected))
            self.assertEqual(self.store.at("lamp-test", "2026-11-25"), old_day)
            _, page, _ = call(cookie=cookie, query=query)
            self.assertNotIn('name="apply_today"', page)
            self.assertIn("235.64 ₽", page)

    def test_optional_rows_can_be_removed_and_no_blank_row_is_added(self):
        self.store.save_reference("component", "cable", dict(name="Кабель", unit="pcs", price="30"), "2026-09-01")
        card = self.store.resolve("300030")
        card["components"] = [{"id": "cable", "quantity": "1"}]
        self.store.save_product(card, "2026-11-24", expected=card["revision"])
        call, sid, _ = self._owner_portal()
        _, page, _ = call(cookie="__Host-sid=" + sid, query="tab=calculator&view=card&article=300030")
        for group, count, label in (("parts", 2, "детали"), ("links", 2, "привязки"),
                                    ("components", 1, "комплектующего"), ("package", 2, "расходника")):
            rows = page.split('data-cost-rows="' + group + '">', 1)[1].split('<template id="cost-' + group + '">', 1)[0]
            self.assertEqual(rows.count('data-cost-remove'), count)
            self.assertEqual(rows.count('aria-label="Удалить строку ' + label + '"'), count)
            self.assertIn('data-cost-add="' + group + '"', page)

    def test_invalid_card_is_kept_as_a_durable_draft_until_corrected(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        query = "tab=calculator&view=card&article=300030"
        original = self.store.resolve("300030")
        history = self.store.history("300030")
        fields = {"csrf": csrf, "action": "save_product", "article": "300030", "existing": "300030",
                  "revision": str(original["revision"]), "name": "Новая <чаша>", "status": "active",
                  "effective": "2026-11-24", "comment": "Не потерять & проверить",
                  "photos": "https://example.org/photo.jpg?x=1&y=2",
                  "link_market": ["ozon", "wb"], "link_code": ["300030", "3000030"],
                  "link_id": ["5215904379", "1294477629"],
                  "part_name": ["Корпус", "Крепление"], "part_count": ["1", "2"],
                  "part_printer": ["f1", "f1"], "part_hours": ["ошибка", "0.3"],
                  "part_material": ["black", "white"], "part_weight": ["80", "10"],
                  "part_color": ["new", "saved:black"], "part_color_new": ["Хаки", ""],
                  "pack_ready": "1", "pack_box": "box", "weight_g": "190", "length_cm": "19",
                  "width_cm": "10", "height_cm": "8", "pack_id": ["bag"], "pack_quantity": ["1"],
                  "marking_required": "0", "defect_percent": "5", "manual_minutes": "3", "other": "1",
                  "unexpected_secret": "must-not-be-stored"}
        status, _, headers = call("POST", cookie, origin, fields, query)
        self.assertEqual(status, "303 See Other")
        draft_query = urlsplit(headers["Location"]).query
        draft_id = parse_qs(draft_query)["draft"][0]
        self.assertEqual(self.store.resolve("300030"), original)
        self.assertEqual(self.store.history("300030"), history)
        draft = cost_catalog.Store(self.path).form_draft(draft_id)
        self.assertNotIn("csrf", draft["values"])
        self.assertNotIn("unexpected_secret", draft["values"])
        self.assertEqual(draft["values"]["part_hours"], ["ошибка", "0.3"])
        self.assertEqual(call(query=draft_query)[0], "401 Unauthorized")
        status, page, _ = call(cookie=cookie, query=draft_query)
        self.assertEqual(status, "200 OK")
        for value in ('name="name" value="Новая &lt;чаша&gt;"', 'value="Не потерять &amp; проверить"',
                      'name="part_hours" value="ошибка"', 'name="part_hours" value="0.3"',
                      'Деталь «Корпус», печать, ч: Укажите конечное число.',
                      'value="new" selected', 'name="part_color_new" value="Хаки"',
                      'name="weight_g" value="190"', 'name="pack_quantity" value="1"',
                      'Исправьте ошибку и сохраните снова'):
            self.assertIn(value, page)
        _, original_page, _ = call(cookie=cookie, query=query)
        self.assertIn("Продолжить редактирование", original_page)

        fields.update(part_hours=["1.5", "0.3"], draft_id=draft_id)
        self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")
        self.assertIsNone(self.store.form_draft(draft_id))
        saved = self.store.resolve("300030")
        self.assertEqual(saved["name"], "Новая <чаша>")
        self.assertEqual(saved["parts"][0]["color"], "Хаки")
        self.assertEqual(saved["package"]["weight_g"], "190")

    def test_reference_validation_keeps_values_and_clears_draft_on_success(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        query = "tab=calculator&view=references&kind=material&id=black"
        fields = {"csrf": csrf, "action": "save_reference", "kind": "material", "fixed_id": "black",
                  "ref_id": "black", "ref_name": "Новый материал", "active": "1", "material_type": "PETG",
                  "price_per_kg": "не число", "color": "new", "color_new": "Фисташковый",
                  "supplier": "Тестовый поставщик", "effective": "2026-11-24"}
        old = next(item for item in self.store.references() if item["id"] == "black")
        status, _, headers = call("POST", cookie, origin, fields, query)
        self.assertEqual(status, "303 See Other")
        draft_query = urlsplit(headers["Location"]).query
        draft_id = parse_qs(draft_query)["draft"][0]
        self.assertEqual(next(item for item in self.store.references() if item["id"] == "black"), old)
        status, page, _ = call(cookie=cookie, query=draft_query)
        self.assertEqual(status, "200 OK")
        for value in ('name="ref_name" value="Новый материал"', 'name="price_per_kg" value="не число"',
                      'Цена за кг: Укажите конечное число.',
                      'value="new" selected', 'name="color_new" value="Фисташковый"',
                      'name="supplier" value="Тестовый поставщик"', 'name="effective" value="2026-11-24"'):
            self.assertIn(value, page)
        fields.update(price_per_kg="885", draft_id=draft_id)
        self.assertEqual(call("POST", cookie, origin, fields, query)[0], "303 See Other")
        self.assertIsNone(self.store.form_draft(draft_id))
        saved = next(item for item in self.store.references() if item["id"] == "black")
        self.assertEqual((saved["name"], saved["color"], saved["price_per_kg"]), ("Новый материал", "Фисташковый", "885"))

    def test_stale_draft_does_not_overwrite_a_concurrent_edit(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        query = "tab=calculator&view=card&article=300030"
        original = self.store.resolve("300030")
        fields = {"csrf": csrf, "action": "save_product", "article": "300030", "existing": "300030",
                  "revision": str(original["revision"]), "name": "Мой вариант", "status": "draft",
                  "effective": "2026-11-24", "defect_percent": "100"}
        status, _, headers = call("POST", cookie, origin, fields, query)
        self.assertEqual(status, "303 See Other")
        draft_id = parse_qs(urlsplit(headers["Location"]).query)["draft"][0]
        other = dict(original, name="Изменено в другом окне")
        self.store.save_product(other, "2026-11-24", expected=original["revision"])
        fields.update(defect_percent="5", draft_id=draft_id)
        status, _, headers = call("POST", cookie, origin, fields, query)
        self.assertEqual(status, "303 See Other")
        _, page, _ = call(cookie=cookie, query=urlsplit(headers["Location"]).query)
        self.assertIn("Карточка уже изменилась", page)
        self.assertIn('name="name" value="Мой вариант"', page)
        self.assertEqual(self.store.resolve("300030")["name"], "Изменено в другом окне")
        self.assertIsNotNone(self.store.form_draft(draft_id))

    def test_draft_storage_failure_keeps_values_on_the_page_without_claiming_success(self):
        call, sid, csrf = self._owner_portal()
        cookie, origin = "__Host-sid=" + sid, "https://test.example"
        fields = {"csrf": csrf, "action": "save_product", "article": "new-test", "name": "Важное название",
                  "status": "draft", "revision": "0", "effective": "2026-11-24", "defect_percent": "100"}
        with patch("cost_catalog.Store.save_form_draft", side_effect=sqlite3.OperationalError("locked")):
            status, page, _ = call("POST", cookie, origin, fields, "tab=calculator&view=card")
        self.assertEqual(status, "503 Service Unavailable")
        self.assertIn('name="name" value="Важное название"', page)
        self.assertIn("Не удалось сохранить черновик на сервере", page)
        self.assertNotIn("Введённые данные сохранены в черновике", page)
        self.assertIsNone(self.store.resolve("new-test"))
