import copy
import base64
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import expansion_gateway
import finance_gate
import market_archive
import market_cooldown
import market_health
import market_specs
import ozon_expansion
import supply_history
import supply_plan
import wb_expansion
import wb_reports


class ResumeWire:
    deadline = float("inf")

    def __init__(self):
        self.records = {}

    @contextmanager
    def progress(self, name, args):
        key = (name, json.dumps(args, sort_keys=True))
        yield copy.deepcopy(self.records.get(key)), lambda value, ttl: self.records.update({key: copy.deepcopy(value)})


class Recovery(unittest.TestCase):
    def test_general_token_only_marks_claimed_categories(self):
        token = base64.urlsafe_b64encode(json.dumps({"s": 1 << 2}).encode()).decode().rstrip("=")
        vault = SimpleNamespace(read=lambda name: {"api_token": "x." + token + ".x"} if name == "wb" else None)
        with patch.object(market_health.market_history, "History"):
            data = market_health.read(vault, "/fixture", {}, [], lambda *args: None)
        categories = {r["category"]: r for r in data["tokens"]}
        self.assertTrue(categories["Аналитика"]["configured"])
        self.assertEqual(categories["Аналитика"]["credential_source"], "wb")
        self.assertFalse(categories["Финансы"]["configured"])

    def test_storage_summary_includes_every_row_of_the_completed_report(self):
        from test_wb_expansion_contracts import report_download, PERIOD
        wire = report_download("paid_storage", [{"warehousePrice": 2.5}, {"warehousePrice": -0.25}])
        data = wb_expansion.read("wb_paid_storage", dict(PERIOD, mode="summary", limit=1), wire)
        self.assertEqual(data["items"], [{"currency":"RUB", "amount":"2.25", "source_rows":2}])
        self.assertTrue(data["complete"])

    def test_two_ozon_catalogs_confirm_all_skus(self):
        def wire(method, url, **kwargs):
            if url.endswith("/info/list"):
                raise market_cooldown.MethodLimited("retry_time_unknown")
            if url.endswith("/attributes"):
                return {"result": [{"id": 81, "offer_id": "001", "name": "Товар", "sku": 17}]}
            return {"items": [{"product_id": 81, "offer_id": "001", "stocks": [
                {"sku": 17, "type": "fbo"}, {"sku": 22, "type": "fbs"}]}]}
        self.assertEqual(ozon_expansion._article_card("001", wire)["sku_candidates"], ["17", "22"])
        self.assertIsNone(ozon_expansion._article_card("1", wire))

    def test_wholesale_across_postings_requires_shared_order_identity(self):
        state = {"locations": {"W": {"wholesale_daily": {}}}, "orders": {}, "order_groups": {}}
        for i in range(20):
            key = str(i)
            state["orders"][key] = ["W", "2026-09-09", 1, False]
            state["order_groups"][key] = "bulk" if i < 13 else key
        supply_plan._wholesale(state, {"bulk_order_min_units": 10})
        self.assertEqual(state["locations"]["W"]["wholesale_daily"], {"2026-09-09": 13})
        state["locations"]["W"]["wholesale_daily"].clear()
        state["order_groups"].clear()
        supply_plan._wholesale(state, {"bulk_order_min_units": 10})
        self.assertFalse(state["locations"]["W"]["wholesale_daily"])

    def test_exact_directory_join_and_bounded_catalog(self):
        warehouses = [{"warehouse_id": i, "name": str(i)} for i in range(200)]
        wire = lambda *a, **k: {"clusters": [{"id": 8, "name": "Центр", "logistic_clusters": [{"warehouses": warehouses}]}]}
        state = {"locations": {"111": {"cluster_id": None}}, "issues": set()}
        supply_plan._assign_clusters(state, wire)
        self.assertEqual(state["locations"]["111"], {"cluster_id": 8, "cluster": "Центр"})
        group, = ozon_expansion.read("ozon_clusters", {}, wire)["items"]
        self.assertEqual((group["warehouse_count"], len(group["warehouses"])), (200, 3))
        page = ozon_expansion.read("ozon_clusters", {"mode": "details", "offset": 100, "limit": 10}, wire)
        self.assertEqual((page["items"][0]["warehouse_id"], page["next_cursor"]), (100, 110))

    def test_order_feed_covers_creation_cohort_and_marks_older_days_unknown(self):
        today = datetime.now(market_specs.MOSCOW).date()
        yesterday, earlier = today - timedelta(days=1), today - timedelta(days=33)
        def wire(method, url, **kwargs):
            self.assertEqual(kwargs["body"]["selectedPeriod"]["end"][:10], today.isoformat())
            def order(i, created):
                return dict(srid=str(i), nmId=12, destinationDistrict="Центр", status="buyout", isMp=False,
                            warehouseName="Склад WB", createdAt=str(created)+"T08:00:00+03:00", updatedAt=str(today)+"T09:00:00+03:00")
            return {"data": {"orders": [order(1, yesterday), order(2, earlier)], "currency": "RUB", "snapshotTime": str(today)+"T09:01:00+03:00"}}
        args = dict(article="ABC", nm_id=12, date_from=str(today-timedelta(days=89)), date_to=str(yesterday))
        page = wb_expansion.feed_orders(args, wire)
        self.assertEqual([r["srid"] for r in page["items"]], ["1"])
        self.assertTrue(page["source_exhausted"])
        self.assertFalse(page["complete"])
        self.assertEqual(page["coverage_from"], str(today-timedelta(days=30)))

    def test_summary_resumes_goods_before_metadata_and_never_requests_packages(self):
        wire = ResumeWire()
        headers = market_specs.result([dict(supply_id=i, preorder_id=None) for i in range(1,5)])
        wait = finance_gate.GateError("cooldown", 60)
        page = market_specs.result([{"quantity": 77, "accepted_quantity": 76}])
        detail = lambda row, wire: dict(row, warehouse="Склад A", warehouse_id=1, actual_warehouse_id=None)
        args = dict(article="A", date_from="2026-10-01", date_to="2026-10-31")
        with patch.object(wb_expansion, "supply_headers", return_value=headers), patch.object(
                wb_expansion, "supply_detail", side_effect=detail), patch.object(
                wb_expansion, "_goods", side_effect=[page, wait, page, page, page]) as goods:
            first = supply_history.report("wb_supplies", args, wire)
            self.assertFalse(first["complete"])
            self.assertEqual(first["items"][0]["declared_units"], 77)
            last = supply_history.report("wb_supplies", args, wire)
            self.assertTrue(last["complete"])
            self.assertEqual([(r["supply_id"], r["warehouse"]) for r in last["items"]], [(i, "Склад A") for i in range(1, 5)])
            self.assertTrue(all(c.kwargs == {"include_packages": False} for c in goods.call_args_list))

    def test_weekly_funnel_fetches_calendar_intersections(self):
        def wire(method, url, **kwargs):
            self.assertTrue(url.endswith("/sales-funnel/products"))
            period = kwargs["body"]["selectedPeriod"]
            return {"data": {"currency": "RUB", "products": [{"product":{"nmId":12, "vendorCode":"A"},
                    "statistic":{"selected":{"period":period, "orderCount":3}}}]}}
        q = dict(date_from="2026-09-09", date_to="2026-09-18", group_by="week", nm_id=12)
        first = wb_reports.funnel_history(q, wire)
        last = wb_reports.funnel_history(dict(q, cursor=first["next_cursor"]), wire)
        self.assertEqual([(p["items"][0]["week_from"], p["items"][0]["week_to"], p["items"][0]["order_count"])
                          for p in (first,last)], [("2026-09-09","2026-09-13",3),("2026-09-14","2026-09-18",3)])
        self.assertTrue(last["complete"])

    def test_by_id_finance_requires_end_marker_and_complete_date_coverage(self):
        wire = ResumeWire()
        wire.creds, wire.gate_dir = {}, "/fixture"
        args = dict(article="A", date_from="2026-10-01", date_to="2026-10-02")
        listed = {"http_status":200, "data":[{"reportId":7,"dateFrom":"2026-09-28","dateTo":"2026-10-04"}]}
        line = {"reportId":7, "rrdId":1, "vendorCode":"A", "nmId":2, "rrDate":"2026-10-01",
                "currency":"RUB", "quantity":1, "docTypeName":"Продажа", "sellerOperName":"Продажа", "forPay":90}
        with patch.object(expansion_gateway,"Wire",return_value=wire), patch("market_catalog.transfer",
                side_effect=[listed,{"http_status":200,"data":[line]},finance_gate.GateError("cooldown",60),
                             {"http_status":204,"data":None}]):
            first = expansion_gateway._finance_by_report(args, wire)
            self.assertFalse(first["complete"])
            last = expansion_gateway._finance_by_report(args, wire)
        self.assertTrue(last["complete"])
        self.assertEqual((last["rows_observed"],last["items"][0]["sold_units"]), (1,1))

    def test_seven_archived_ozon_days_come_from_one_complete_page(self):
        wire, saved = ResumeWire(), {}
        wire.provider, wire.creds = "ozon", {"client_id":"1"}
        day = "2026-11-15"
        history = SimpleNamespace(save_day=lambda owner, name, day, rows, stamp: saved.update({day: rows}), confirm=lambda *a: None)
        with patch.object(ozon_expansion, "_daily", return_value=market_specs.result([{"date":day,"article":"A","sku":1}] )) as report:
            self.assertEqual(market_archive.collect(wire, history, "ozon_sku_daily", day), "complete")
        self.assertEqual(report.call_args.args[0]["date_from"], "2026-11-09")
        self.assertEqual((len(saved),len(saved[day]),saved["2026-11-10"]), (7,1,[]))


if __name__ == "__main__":
    unittest.main()
