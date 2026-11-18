"""Authenticated WSGI contract for the expansion. Provider responses and credentials are synthetic."""

import json
import os
import re
import time
import unittest
import urllib.error
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

import expansion_gateway
import market_transport
import ozon_expansion
import portal
import wb_expansion
import test_external_mcp_integration as integration
import test_market_ads_contracts as ads
import test_ozon_expansion_contracts as seller
import test_portal
import test_supply_plan_contracts as plan
import test_wb_expansion_contracts as wb


WINDOW = {"date_from": "2026-10-01", "date_to": "2026-10-03"}
PERIOD = {"begin": "2026-10-01T00:00:00Z", "end": "2026-10-03T23:59:59Z", "id": 9}
ACCOUNT = {
    "ozon": {"client_id": "7987", "api_key": "synthetic-seller-private"},
    "wb": {"api_token": "synthetic-wb-private"},
    "performance": {"client_id": "synthetic-ad-client", "client_secret": "synthetic-ad-private"},
}
CASES = {
    "wb_supplies": wb.PERIOD,
    "wb_supply_goods": {"supply_id": 77},
    "wb_stocks": {},
    "wb_orders": wb.PERIOD,
    "wb_sales": wb.PERIOD,
    "wb_acceptance_slots": {},
    "ozon_supply_orders": WINDOW,
    "ozon_supply_items": {"bundle_id": "B", "order_id": 42, "supply_id": 77},
    "ozon_stocks": {"article": "A"},
    "ozon_stocks_by_cluster": {"sku": 101},
    "ozon_sku_daily": dict(WINDOW, sku=101, include_funnel=True),
    "wb_finance_report": dict(wb.PERIOD, mode="summary"),
    "wb_paid_storage": wb.PERIOD,
    "wb_acceptance_report": wb.PERIOD,
    "ozon_cash_flow": dict(WINDOW, mode="summary"),
    "ozon_fbo_postings_geo": WINDOW,
    "ozon_clusters": {},
    "wb_funnel": wb.PERIOD,
    "wb_ads": dict(wb.PERIOD, campaign_ids=[12]),
    "ozon_ads": dict(WINDOW, campaign_ids=[12]),
    "wb_feedbacks": wb.PERIOD,
    "ozon_reviews": WINDOW,
    "ozon_rating": {},
    "supply_plan": plan.ARGS,
}


def report_routes():
    acct = {
        "sku_info": {"sku": 101, "offer_id": "A"},
        "declared_quantity": 5,
        "fact_quantity": 4,
        "approved_quantity": 4,
    }
    routes = seller.one_supply_wire(act_item=acct)
    day = wb.PERIOD["date_to"]
    finance = {
        "rrdId": 1,
        "nmId": 71,
        "vendorCode": "A",
        "currency": "RUB",
        "quantity": 2,
        "sellerOperName": "Продажа",
        "docTypeName": "Продажа",
        "vw": "3",
        "vwNds": "0.6",
        "deliveryService": "4",
        "rebillLogisticCost": "0",
        "paidStorage": "0.2",
        "paidAcceptance": "0.4",
        "penalty": "0",
        "forPay": "60",
    }
    routes.update(
        {
            "/v3/supply-order/list": {"order_ids": [42], "last_id": ""},
            "/v3/product/info/list": {
                "items": [
                    {"id": 99, "offer_id": "A", "sku": 101, "sources": [{"sku": 101, "source": "fbo"}]}
                ]
            },
            "/v4/product/info/stocks": {
                "items": [
                    {"offer_id": "A", "stocks": [{"sku": 101, "type": "fbo", "present": 8, "reserved": 2}]}
                ],
                "cursor": "",
            },
            "/v1/analytics/stocks": {
                "items": [
                    {
                        "sku": 101,
                        "offer_id": "A",
                        "warehouse_id": 8,
                        "cluster_id": 2,
                        "available_stock_count": 8,
                    }
                ]
            },
            "/v1/analytics/data": {
                "result": {
                    "data": [
                        {
                            "dimensions": [{"id": "101"}, {"id": "2026-10-03"}],
                            "metrics": [8, 40, 45, 15, 33.3],
                        }
                    ]
                }
            },
            "/v1/seller/info": {"company": {"currency": "KZT"}},
            "/v3/posting/fbo/list": {"postings": [], "has_next": False},
            "/v1/finance/cash-flow-statement/list": {
                "result": {
                    "page_count": 1,
                    "cash_flows": [{"period": PERIOD, "currency_code": "RUB", "orders_amount": 100}],
                    "details": {
                        "period": PERIOD,
                        "payments": {"payment": 81, "currency_code": "RUB", "bank_account": "not-public"},
                        "services": {
                            "total": -4,
                            "private": "not-public",
                            "items": [{"name": "Fee", "price": -4, "private": "not-public"}],
                        },
                    },
                }
            },
            "/v1/cluster/list": {"clusters": []},
            "/v2/review/list": {"reviews": [], "has_next": False},
            "/v1/rating/summary": {"groups": []},
            "/api/v1/supplies": [{"supplyID": 77, "createDate": day}],
            "/api/v1/supplies/77": {"quantity": 5, "acceptedQuantity": 4},
            "/api/v1/supplies/77/goods": [
                {"vendorCode": "A", "nmID": 71, "quantity": 5, "acceptedQuantity": 4}
            ],
            "/api/v1/supplies/77/package": [],
            "/api/v1/supplier/orders": [],
            "/api/v1/supplier/sales": [],
            "/api/tariffs/v1/acceptance/coefficients": [
                {
                    "date": datetime.now(
                        wb.MOSCOW if hasattr(wb, "MOSCOW") else timezone(timedelta(hours=3))
                    )
                    .date()
                    .isoformat(),
                    "warehouseID": 11,
                    "warehouseName": "Test",
                    "coefficient": 0,
                    "allowUnload": True,
                }
            ],
            "/api/finance/v1/sales-reports/detailed": lambda body: [finance]
            if body["rrdId"] == 0
            else None,
            "/api/analytics/v3/sales-funnel/products": {"data": {"currency": "RUB", "products": []}},
            "/api/advert/v4/fullstats": {
                "rows": [
                    {
                        "advertId": 12,
                        "nmId": 71,
                        "sum": 5,
                        "currency": "RUB",
                        "orders": 3,
                        "shks": 4,
                        "date": day,
                        "conversionType": "direct",
                        "placement": "search",
                    }
                ]
            },
            "/content/v2/get/cards/list": {
                "cards": [{"nmID": 71, "vendorCode": "A"}],
                "cursor": {"total": 1},
            },
            "/api/v1/feedbacks": {"error": False, "data": {"feedbacks": []}},
        }
    )
    for kind in ("warehouse_remains", "paid_storage", "acceptance_report"):
        base = "/api/v1/" + kind
        routes[base] = {"data": {"taskId": wb.TASK_ID}}
        routes[base + "/tasks/" + wb.TASK_ID + "/status"] = {"data": {"id": wb.TASK_ID, "status": "done"}}
        routes[base + "/tasks/" + wb.TASK_ID + "/download"] = []
    return routes


class ExpansionMCPTests(unittest.TestCase):
    request = test_portal.Tests.request
    create = integration.ExternalMCPIntegrationTests.create
    creation_form = integration.ExternalMCPIntegrationTests.creation_form
    mcp = integration.ExternalMCPIntegrationTests.mcp
    tearDown = test_portal.Tests.tearDown

    def setUp(self):
        test_portal.Tests.setUp(self)
        self.bearer = re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"', self.create()["body"]).group(1)
        for provider, creds in ACCOUNT.items():
            self.vault.save(provider, creds)
        self.app.gateway = lambda name, args, creds: expansion_gateway.execute(
            name, args, creds, self.app.finance_gate_dir, time.monotonic() + 20
        )

    def call(self, name, args):
        response = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
            self.bearer,
        )
        self.assertEqual(response["status"], "200 OK")
        rpc = json.loads(response["body"])["result"]
        data = json.loads(rpc["content"][0]["text"])
        self.assertNotIn("not-public", str(data))
        return rpc, data

    def test_all_24_tools_run_through_authenticated_mcp(self):
        listing = json.loads(
            self.mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.bearer)["body"]
        )
        directory = {t["name"]: t for t in listing["result"]["tools"]}
        self.assertEqual(len(directory), 46)
        self.assertTrue(set(CASES) <= directory.keys())
        routes = report_routes()

        class FixtureWire(market_transport.Wire):
            def __call__(self, method, url, *, query=None, body=None, **flags):
                parsed = urlsplit(url)
                if not market_transport.allowed_read(parsed.netloc, method, parsed.path):
                    raise AssertionError(parsed.path)
                if parsed.path not in routes:
                    raise AssertionError(parsed.path)
                value = routes[parsed.path]
                return value(body) if callable(value) else value

            def bind_job(self, *args, **kwargs):
                pass

            def finish_job(self, *args, **kwargs):
                pass

        def wire(provider, creds, directory, deadline):
            self.assertEqual(creds, ACCOUNT[provider])
            return (
                ads.PerformanceFixture()
                if provider == "performance"
                else FixtureWire(provider, creds, directory, deadline)
            )

        # POSIX file ownership/locking is exercised by the Linux CI suite.
        gate = (
            patch("finance_gate.run", side_effect=lambda root, client, read, secrets: read())
            if os.name == "nt"
            else nullcontext()
        )
        with (
            gate,
            patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")),
            patch("expansion_gateway.Wire", side_effect=wire),
        ):
            for name, args in CASES.items():
                with self.subTest(name=name):
                    self.assertTrue(directory[name]["annotations"]["readOnlyHint"])
                    rpc, data = self.call(name, args)
                    self.assertFalse(rpc.get("isError"), data)
                    self.assertEqual(data["tool"], name)
                    self.assertTrue(data["read_only"])
                    if name == "ozon_sku_daily":
                        self.assertEqual(data["items"][0]["hits_view_pdp"], 45)
                    if name == "wb_supply_goods":
                        self.assertEqual(data["items"][0]["accepted_quantity"], 4)
                    if name == "ozon_supply_items":
                        self.assertEqual(data["acts"][0]["fact_quantity"], 4)
                    if name == "wb_finance_report":
                        self.assertEqual(Decimal(data["items"][0]["per_sold_unit"]["delivery"]), 2)
                    if name == "ozon_cash_flow":
                        self.assertEqual(data["payout_by_currency"], {"RUB": "81"})
                    if name == "wb_ads":
                        self.assertEqual(data["items"][0]["article"], "A")
                    if name == "ozon_ads":
                        self.assertEqual(data["items"][0]["article"], "A")
                    if name == "supply_plan":
                        self.assertEqual(data["status"], "mapping_required")

    def test_different_article_codes_supply_plan_through_signed_session_and_private_alias(self):
        Path(self.tmp.name, "supply-articles.json").write_text(
            json.dumps({plan.ARGS["article"]: plan.MAPPING}), encoding="utf-8"
        )
        inputs = {
            "ozon": plan.ozon_wire(),
            "wb": plan.FixtureWire(
                [("GET", plan.WB_REPORT, {"data": {"taskId": plan.TASK_ID}})]
                + plan.wb_stock_download()
                + plan.wb_orders()
            ),
        }
        with patch("expansion_gateway.Wire", side_effect=lambda provider, *args: inputs[provider]):
            rpc, data = self.call("supply_plan", plan.ARGS)
        self.assertFalse(rpc.get("isError"), data)
        self.assertTrue(data["complete"], data)
        by_platform = {item["marketplace"]: item for item in data["items"]}
        self.assertEqual(by_platform["ozon"]["marketplace_total"]["ordered_units"]["90"], 21)
        self.assertEqual(set(by_platform["wb"]["marketplace_total"]["orders_per_day"]), {"7", "30", "90"})
        self.assertEqual(by_platform["ozon"]["clusters"][0]["recommended_quantity"], 0)

    def test_real_deadline_subprocess_reads_private_mapping_without_network(self):
        self.app.gateway = portal.provider_read
        rpc, data = self.call("supply_plan", plan.ARGS)
        self.assertFalse(rpc.get("isError"), data)
        self.assertEqual(data["status"], "mapping_required")

    def test_missing_wb_grant_reports_named_category_and_invalid_range_never_reads(self):
        with patch("expansion_gateway.Wire") as factory:
            failure, data = self.call("wb_paid_storage", dict(date_from="2026-10-01", date_to="2026-10-14"))
            self.assertTrue(failure["isError"])
            factory.assert_not_called()
            factory.return_value.side_effect = urllib.error.HTTPError("", 403, "private", {}, None)
            failure, data = self.call("wb_finance_report", wb.PERIOD)
        self.assertTrue(failure["isError"])
        self.assertEqual(data["required_category"], "Финансы")


class ReportHistoryTests(unittest.TestCase):
    def test_omitted_sku_does_not_become_an_observed_empty_stock_snapshot(self):
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "ozon", ACCOUNT["ozon"], Path(directory) / "gate", time.monotonic() + 5
            )
            path = "/v1/analytics/stocks"
            info = "/v3/product/info/list"
            article = "TEST-PAIR"
            fixture = seller.SellerFixture(
                {
                    info: {
                        "items": [
                            {
                                "id": 1,
                                "offer_id": article,
                                "name": article,
                                "sku": 101,
                                "sources": [{"source": "fbo", "sku": 102}],
                            }
                        ]
                    },
                    path: {
                        "items": [
                            {
                                "sku": 101,
                                "offer_id": article,
                                "warehouse_id": 7,
                                "cluster_id": 1,
                                "available_stock_count": 0,
                            }
                        ]
                    },
                }
            )
            with patch.object(market_transport.Wire, "__call__", side_effect=fixture):
                current = ozon_expansion.read("ozon_stocks_by_cluster", {"article": article}, wire)
            day = current["observed_at"][:10]
            period = {"date_from": day, "date_to": day}
            self.assertFalse(current["complete"])
            self.assertEqual(list(map(str, current["missing_skus"])), ["102"])
            self.assertEqual(current["items"][0]["available_stock_count"], 0)
            for selection in ({"sku": 102}, {"skus": [101, 102]}, {"article": article}):
                with self.subTest(selection=selection):
                    history = wire.stock_history({**period, **selection})
                    self.assertFalse(history["complete"])
                    self.assertEqual(history["missing_or_partial_dates"], [day])
            known = wire.stock_history({**period, "sku": 101})
            self.assertTrue(known["complete"])
            self.assertEqual(known["items"][0]["available_stock_count"], 0)
            for selection in ({"sku": 101, "article": "OTHER"}, {"sku": 101, "warehouse_id": 8}):
                narrowed = wire.stock_history({**period, **selection})
                self.assertFalse(narrowed["complete"])
                self.assertEqual(narrowed["missing_or_partial_dates"], [day])

    def test_legacy_unverified_daily_article_preserves_rows_without_claiming_full_coverage(self):
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "ozon", ACCOUNT["ozon"], Path(directory) / "gate", time.monotonic() + 5
            )
            day, stamp = "2026-10-10", "2026-10-10T12:00:00+00:00"
            key = market_transport._id([wire.scope, "stock-history-v1", "article", "A"])
            rows = [{"sku": 101, "article": "A", "warehouse_id": 7, "available_stock_count": 2}]
            nonce = os.urandom(12)
            blob = nonce + wire.aead.encrypt(nonce, json.dumps(rows).encode(), (key + day + stamp).encode())
            with wire.db() as db:
                db.execute("INSERT INTO stock_history VALUES(?,?,?,?)", (key, day, stamp, blob))
            older = wire.stock_history({"article": "A", "date_from": day, "date_to": day})
            self.assertEqual(older["items"][0]["available_stock_count"], 2)
            self.assertFalse(older["complete"])
            self.assertEqual(older["missing_or_partial_dates"], [day])

    def test_same_timestamp_disagreement_requires_a_later_stock_observation(self):
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "ozon", ACCOUNT["ozon"], Path(directory) / "gate", time.monotonic() + 5
            )
            day = "2026-10-10"
            args = {"sku": 101, "date_from": day, "date_to": day}
            for quantity, second, complete in [(1, 0, True), (2, 0, False), (1, 0, False), (3, 1, True)]:
                stamp = day + f"T12:00:0{second}+00:00"
                rows = [{"sku": 101, "article": "A", "warehouse_id": 7, "available_stock_count": quantity}]
                wire.record_stocks([101], "A", rows, stamp)
                self.assertEqual(wire.stock_history(args)["complete"], complete)
                self.assertEqual(
                    wire.stock_history({**args, "sku": 101, "article": "A"})["complete"], complete
                )

    def test_wb_http_null_cannot_end_a_report_like_http_204(self):
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "wb", ACCOUNT["wb"], Path(directory) / "gate", time.monotonic() + 5
            )
            endpoint = wb_expansion.FINANCE
            args = {"date_from": "2026-10-01", "date_to": "2026-10-02"}
            query = {
                "dateFrom": args["date_from"],
                "dateTo": args["date_to"],
                "period": "weekly",
                "limit": 100,
                "rrdId": 0,
            }
            wire.store(wire._key("POST", endpoint, None, query), b"null", 300)
            with patch.object(wire, "_admit"), patch.object(wire.opener, "open") as call:
                response = call.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = b"null"
                with self.assertRaises(ValueError):
                    wb_expansion.read("wb_finance_report", args, wire)
                response.status = 204
                response.read.return_value = b""
                end = wb_expansion.read("wb_finance_report", args, wire)
                self.assertTrue(end["complete"])
                self.assertEqual(end["status"], "end_of_report")
                self.assertEqual(call.call_count, 2)

    def test_wb_legacy_end_marker_does_not_finish_new_finance_summary(self):
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "wb", ACCOUNT["wb"], Path(directory) / "gate", time.monotonic() + 5
            )
            query = {"date_from": "2026-10-01", "date_to": "2026-10-02"}
            with wire.progress("wb-finance-v1", query) as (_, save):
                save({"cursor": None, "rows": [], "unallocated": 0, "done": True}, 1800)
            cooldown = urllib.error.HTTPError("", 429, "cooldown", {"Retry-After": "60"}, None)
            with patch.object(market_transport.Wire, "__call__", side_effect=cooldown) as read:
                result = expansion_gateway._finance_period(query, wire)
            self.assertFalse(result["complete"])
            self.assertEqual(result["status"], "pending")
            self.assertEqual(read.call_count, 1)

    def test_stock_history_is_account_scoped_and_handles_missing_days_without_current_stock(self):
        with TemporaryDirectory() as directory:

            def wire(secret):
                return market_transport.Wire(
                    "ozon",
                    {"client_id": "1", "api_key": secret},
                    Path(directory) / "gate",
                    time.monotonic() + 5,
                )

            owner, other = wire("synthetic-A"), wire("synthetic-B")
            day = date.today()
            stamp = day.isoformat() + "T12:00:00+00:00"
            args = {
                "article": "A",
                "date_from": (day - timedelta(days=1)).isoformat(),
                "date_to": day.isoformat(),
            }
            owner.record_stocks(
                [101],
                "A",
                [{"sku": 101, "article": "A", "warehouse_id": 7, "available_stock_count": 3}],
                stamp,
            )
            with patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")):
                history = ozon_expansion.read("ozon_stocks_by_cluster", args, owner)
                self.assertEqual(history["items"][0]["available_stock_count"], 3)
                self.assertFalse(history["complete"])
                self.assertEqual(history["missing_or_partial_dates"], [args["date_from"]])
                self.assertEqual(ozon_expansion.read("ozon_stocks_by_cluster", args, other)["items"], [])

    def test_read_allowlist_and_basic_daily_quota(self):
        self.assertFalse(
            market_transport.allowed_read(market_transport.SELLER, "POST", "/v1/supply-order/cancel")
        )
        self.assertFalse(market_transport.allowed_read("advert-api.wildberries.ru", "POST", "/adv/v1/upd"))
        with TemporaryDirectory() as directory:
            wire = market_transport.Wire(
                "ozon", ACCOUNT["ozon"], Path(directory) / "gate", time.monotonic() + 10
            )
            for _ in range(50):
                wire._analytics_budget()
            with self.assertRaises(urllib.error.HTTPError) as raised:
                wire._analytics_budget()
            self.assertEqual(raised.exception.code, 429)
            self.assertGreater(int(raised.exception.headers["Retry-After"]), 86000)
