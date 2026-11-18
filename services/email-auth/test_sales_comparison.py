"""Offline integration regressions for sales-period comparison.

All provider responses, dates and product identifiers are synthetic. These
tests exercise the real provider adapters but replace their HTTP transport.
"""

import socket
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

import market_composites


ARGS = {
    "previous_from": "2026-03-25", "previous_to": "2026-03-31",
    "date_from": "2026-04-01", "date_to": "2026-04-07",
}
MAPPING = {"wb_vendor_code": "A", "wb_nm_id": 71, "ozon_offer_id": "A"}


def denied(url):
    return HTTPError(url, 403, "synthetic denial", {}, None)


class Wildberries:
    def __init__(self, order_count=5, view_count=100, reported_period=None, ad_status=403):
        self.order_count = order_count
        self.view_count = view_count
        self.reported_period = reported_period
        self.ad_status = ad_status

    def __call__(self, method, url, **options):
        if url.endswith("/api/analytics/v3/sales-funnel/products"):
            selected = {"period": self.reported_period or options["body"]["selectedPeriod"],
                        "openCount": self.view_count, "cartCount": 10,
                        "orderCount": self.order_count, "orderSum": 500}
            return {"data": {"currency": "RUB", "products": [{
                "product": {"nmId": 71, "vendorCode": "A"},
                "statistic": {"selected": selected},
            }]}}
        if url.endswith("/adv/v1/upd") and self.ad_status == 429:
            raise HTTPError(url, 429, "synthetic limit", {"Retry-After": "120"}, None)
        if url.endswith("/adv/v1/upd") or url.endswith("/api/v1/feedbacks"):
            raise denied(url)
        raise AssertionError("unexpected synthetic Wildberries request")


class Ozon:
    def __init__(self, duplicate=False, wrong_day=False):
        self.duplicate = duplicate
        self.wrong_day = wrong_day

    def __call__(self, method, url, **options):
        if url.endswith("/v3/product/info/list"):
            return {"items": [{"id": 1, "offer_id": "A", "name": "A", "sku": 101,
                               "sources": [{"sku": 101, "source": "FBO"}]}]}
        if url.endswith("/v1/analytics/data"):
            body = options["body"]
            values = {"ordered_units": 5, "revenue": 500, "hits_view_pdp": 100,
                      "hits_tocart_pdp": 10, "conv_tocart_pdp": 10}
            points = [{
                "dimensions": [{"id": "101"}, {"id": "2026-01-01" if self.wrong_day else day}],
                "metrics": [values[metric] for metric in body["metrics"]],
            } for day in (ARGS["previous_from"], ARGS["date_from"])
                      if body["date_from"] <= day <= body["date_to"]]
            if self.duplicate and points:
                points.append(points[0].copy())
            return {"result": {"data": points}}
        if url.endswith("/v2/review/list"):
            raise denied(url)
        raise AssertionError("unexpected synthetic Ozon request")


class SalesComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in ("connect", "connect_ex"):
            restriction = patch.object(socket.socket, name, side_effect=AssertionError("network prohibited"))
            restriction.start()
            cls.addClassCleanup(restriction.stop)

    @staticmethod
    def compare(wb=None, ozon=None):
        wires = {}
        if wb is not None:
            wires["wb"] = wb
        if ozon is not None:
            wires["ozon"] = ozon
        return market_composites.comparison(ARGS, wires, MAPPING)

    def test_missing_ads_and_reviews_are_partial_but_core_metrics_remain(self):
        report = self.compare(Wildberries(), Ozon())
        self.assertFalse(report["complete"])
        by_market = {row["marketplace"]: row for row in report["items"]}
        self.assertEqual(set(by_market), {"wb", "ozon"})
        for row in by_market.values():
            for period in (row["previous"], row["current"]):
                self.assertEqual(Decimal(str(period["ordered_units"])), 5)
                self.assertEqual(Decimal(str(period["views"])), 100)
                self.assertFalse(period["ads"]["complete"])
                self.assertIsNone(period["ads"].get("spend_by_currency"))
                self.assertFalse(period["reviews"]["complete"])
                self.assertIsNone(period["reviews"].get("mean_rating"))

    def test_missing_order_count_is_unknown_and_not_full_coverage(self):
        report = self.compare(Wildberries(order_count=None))
        row, = report["items"]
        for period in (row["previous"], row["current"]):
            self.assertIsNone(period.get("ordered_units"))
            self.assertEqual(Decimal(str(period["views"])), 100)
            self.assertFalse(period["complete"])
        self.assertNotIn("ordered_units", row["changes"])

    def test_nonnumeric_order_count_does_not_discard_other_provider(self):
        report = self.compare(Wildberries(order_count="unknown"), Ozon())
        self.assertFalse(report["complete"])
        by_market = {row["marketplace"]: row for row in report["items"]}
        self.assertEqual(set(by_market), {"wb", "ozon"})
        self.assertIsNone(by_market["wb"]["current"].get("ordered_units"))
        self.assertFalse(by_market["wb"]["current"]["complete"])
        self.assertEqual(Decimal(str(by_market["ozon"]["current"]["ordered_units"])), 5)
        self.assertNotIn("ordered_units", by_market["wb"]["changes"])

    def test_negative_counts_do_not_produce_negative_orders_or_conversion(self):
        report = self.compare(Wildberries(order_count=-1, view_count=-5))
        row, = report["items"]
        for period in (row["previous"], row["current"]):
            self.assertFalse(period["complete"])
            self.assertIsNone(period.get("ordered_units"))
            self.assertIsNone(period.get("views"))
            self.assertIsNone(period.get("cart_to_view_percent"))

    def test_response_for_a_different_period_is_not_compared(self):
        wrong = {"start": "2026-01-01", "end": "2026-01-07"}
        report = self.compare(Wildberries(reported_period=wrong))
        row, = report["items"]
        self.assertFalse(row["previous"]["complete"])
        self.assertFalse(row["current"]["complete"])
        self.assertNotIn("ordered_units", row["changes"])

    def test_advertising_rate_limit_retains_retry_information(self):
        report = self.compare(Wildberries(ad_status=429))
        row, = report["items"]
        ad = row["current"]["ads"]
        self.assertFalse(ad["complete"])
        self.assertEqual(ad.get("http_status"), 429)
        self.assertEqual(ad.get("retry_after_seconds"), 120)
        self.assertEqual(Decimal(str(row["current"]["ordered_units"])), 5)

    def test_ozon_duplicate_sku_day_is_not_counted_as_twice_the_orders(self):
        report = self.compare(ozon=Ozon(duplicate=True))
        row, = report["items"]
        self.assertFalse(row["current"]["complete"])
        self.assertNotIn("ordered_units", row["changes"])

    def test_ozon_outside_period_is_not_compared(self):
        report = self.compare(ozon=Ozon(wrong_day=True))
        row, = report["items"]
        self.assertFalse(row["current"]["complete"])
        self.assertNotIn("ordered_units", row["changes"])


if __name__ == "__main__":
    unittest.main()
