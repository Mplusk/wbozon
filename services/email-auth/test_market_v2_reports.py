"""Contract regressions for the v2 provider projections using published response shapes."""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import market_specs, ozon_expansion, wb_reports, wb_mcp
import market_history


class Reports(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(market_specs.MOSCOW).date().isoformat()
        self.dates = dict(date_from=self.today, date_to=self.today)

    def test_locality_dates_and_unknown_duration(self):
        def provider(method, url, **kwargs):
            self.assertEqual(kwargs["body"]["filter"]["period"], {"from": self.today, "to": self.today})
            return {
                "items": [
                    {
                        "cluster_to_id": "7",
                        "item": {"offer_id": "A", "sku": "17"},
                        "metrics": {"local_data": {"local_quantity": 2, "total_quantity": 3, "index": 67}},
                    }
                ],
                "total": 1,
            }

        (row,) = ozon_expansion.read("ozon_delivery_time", dict(self.dates, cluster_id=7), provider)["items"]
        self.assertEqual((row["article"], row["local_units"]), ("A", 2))
        self.assertIsNone(row["average_delivery_hours"])

    def test_locality_by_sku_uses_the_product_first_contract(self):
        def provider(method, url, *, body, ttl):
            self.assertTrue(url.endswith("/items-clusters/info"))
            self.assertEqual(body["filter"]["skus"], ["17"])
            self.assertNotIn("macrolocal_cluster_to_ids", body["filter"])
            return {"items": [{"sku": "17", "macrolocal_cluster_to_id": "7",
                               "metrics": {"local_data": {"index": 67, "local_quantity": 2, "total_quantity": 3}}}],
                    "total": 1}
        (row,) = ozon_expansion.read("ozon_delivery_time", dict(self.dates, sku=17), provider)["items"]
        self.assertEqual((row["sku"], row["cluster_id"], row["local_units"]), ("17", "7", 2))

    def test_unfiltered_locality_returns_account_total_without_inventing_clusters(self):
        def provider(method, url, *, body, ttl):
            self.assertTrue(url.endswith("/local-sale/total"))
            self.assertEqual(body, {"period": {"from": self.today, "to": self.today}})
            return {"local_data": {"local_quantity": 3, "total_quantity": 4, "index": 75}}
        answer = ozon_expansion.read("ozon_delivery_time", self.dates, provider)
        self.assertFalse(answer["complete"])
        self.assertEqual(answer["items"][0]["local_units"], 3)
        self.assertIsNone(answer["items"][0]["cluster_id"])

    def test_grouped_wb_orders_only_zero_fill_confirmed_days(self):
        args = {"article": "A", "group_by": "day"}
        events = [("A", "2026-11-01", "order", 3), ("A", "2026-11-01", "cancelled", 1)]
        partial = market_history.grouped_orders(args, events, [])
        self.assertEqual([(r["ordered_units"], r["cancelled_units"]) for r in partial], [(4, 1)])
        full = market_history.grouped_orders(args, events, ["2026-11-01", "2026-11-02"])
        self.assertEqual([(r["date"], r["ordered_units"]) for r in full], [("2026-11-01", 4), ("2026-11-02", 0)])

    def test_retention_both_fields_and_unallocated_account_row(self):
        raw = [
            {
                "rrdId": 1,
                "rrDate": self.today,
                "currency": "RUB",
                "penalty": 10,
                "deduction": 2,
                "bonusTypeName": "test",
            },
            {
                "rrdId": 2,
                "rrDate": self.today,
                "currency": "RUB",
                "vendorCode": "A",
                "nmId": 8,
                "penalty": -2,
            },
        ]
        page = wb_reports.retentions(self.dates, lambda *a, **kw: raw)
        self.assertEqual(sorted(r["amount"] for r in page["items"]), [-2, 2, 10])
        self.assertEqual(
            [
                r["amount"]
                for r in wb_reports.retentions(dict(self.dates, article="A"), lambda *a, **k: raw)["items"]
            ],
            [-2],
        )
        self.assertEqual(page["next_cursor"], 2)

    def test_region_destination_and_status_snapshot(self):
        stamp = self.today + "T09:00:00+03:00"

        def feed(method, url, **kwargs):
            self.assertEqual(kwargs["body"]["pagination"]["snapshotTime"], stamp)
            return {
                "data": {
                    "orders": [
                        dict(
                            srid="a",
                            nmId=4,
                            destinationDistrict="destination",
                            warehouseRegion="warehouse",
                            status="return",
                            sellerPrice=30,
                            updatedAt=stamp,
                            createdAt=stamp,
                        )
                    ],
                    "snapshotTime": stamp,
                    "currency": "RUB",
                }
            }

        (row,) = wb_reports.region(dict(self.dates, offset=2, snapshot_time=stamp, mode="summary"), feed)[
            "items"
        ]
        self.assertEqual((row["region"], row["units"], row["status"]), ("destination", 1, "return"))

    def test_return_to_seller_does_not_invent_sale_price_or_quantity(self):
        page = wb_reports.returns(
            self.dates,
            lambda *a, **kw: {
                "count": 1,
                "report": [{"nmId": 19, "sku": "barcode", "returnStatus": "active"}],
            },
        )
        self.assertEqual(page["items"][0]["barcode"], "barcode")
        self.assertFalse(page["quantities_and_sums_reported"])

    def test_funnel_history_and_search_are_separate_contracts(self):
        def wire(method, url, **kw):
            self.assertIn(12, kw["body"]["nmIds"])
            if url.endswith("history"):
                return [
                    {
                        "product": {"nmId": 12, "vendorCode": "A"},
                        "history": [{"date": self.today, "openCount": 4}],
                        "currency": "RUB",
                    }
                ]
            self.assertEqual(kw["body"]["topOrderBy"], "orders")
            return {
                "data": {
                    "currency": "RUB",
                    "items": [{"nmId": 12, "text": "q", "avgPosition": {"current": 3}}],
                }
            }

        self.assertEqual(
            wb_reports.funnel_history(dict(self.dates, nm_id=12, group_by="day"), wire)["items"][0][
                "openCount"
            ],
            4,
        )
        self.assertEqual(wb_reports.search(dict(self.dates, nm_id=12), wire)["items"][0]["avgPosition"], 3)

    def test_warehouse_coefficient_is_not_applied_twice(self):
        fees = wb_mcp._unit_tariff(
            {
                "boxDeliveryBase": "52",
                "boxDeliveryLiter": "12,5",
                "boxDeliveryCoefExpr": 200,
                "boxStorageBase": "1",
                "boxStorageLiter": "0,5",
            },
            {"length": 20, "width": 9, "height": 10},
            "RUB",
        )
        self.assertEqual(Decimal(fees["delivery_per_unit"]), Decimal("62"))
        self.assertEqual(Decimal(fees["storage_per_unit_per_day"]), Decimal("1.4"))
