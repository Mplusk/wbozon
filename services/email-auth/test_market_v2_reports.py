"""Contract regressions for the v2 provider projections using published response shapes."""

import unittest
from unittest.mock import patch
from contextlib import contextmanager
from types import SimpleNamespace
from datetime import datetime, timedelta
from decimal import Decimal

import market_specs, ozon_expansion, wb_reports, wb_mcp
import market_history
import expansion_gateway
import finance_gate
import market_composites


class Reports(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(market_specs.MOSCOW).date().isoformat()
        self.dates = dict(date_from=self.today, date_to=self.today)

    def test_wb_daily_retains_observed_orders_during_unknown_cooldown(self):
        class Wire:
            deadline = float('inf')

            @contextmanager
            def progress(self, name, query):
                yield {'next': None, 'done': False, 'seen': [], 'events': {
                    'id': ['A', self_day, 'warehouse', 'region', 'order', 2, None]}}, lambda *_: None

            def __call__(self, *args, **kwargs):
                raise finance_gate.GateError('retry_time_unknown')

        self_day = self.today
        start = (datetime.now(market_specs.MOSCOW).date() - timedelta(days=1)).isoformat()
        query = dict(date_from=start, date_to=self_day, article='A', group_by='day')
        result = expansion_gateway._wb_daily_totals('wb_orders', query, Wire())
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'retry_time_unknown')
        self.assertIsNone(result['retry_after_seconds'])
        self.assertEqual([(r['date'], r['ordered_units']) for r in result['items']], [(self_day, 2)])
        self.assertIsNone(result['items'][0]['cancelled_units'])
        self.assertEqual(result['items'][0]['unknown_cancellation_units'], 2)

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
            if url.endswith("/v3/product/info/list"):
                self.assertEqual(body, {"sku":[17]})
                return {"items":[{"id":2,"offer_id":"A","sku":17}]}
            self.assertTrue(url.endswith("/items-clusters/info"))
            self.assertEqual(body["filter"]["skus"], ["17"])
            self.assertNotIn("macrolocal_cluster_to_ids", body["filter"])
            return {"items": [{"sku": "17", "macrolocal_cluster_to_id": "7",
                               "metrics": {"local_data": {"index": 67, "local_quantity": 2, "total_quantity": 3}}}],
                    "total": 1}
        (row,) = ozon_expansion.read("ozon_delivery_time", dict(self.dates, sku=17), provider)["items"]
        self.assertEqual((row["article"], row["sku"], row["cluster_id"], row["local_units"]), ("A", "17", "7", 2))

    def test_unfiltered_locality_returns_account_total_without_inventing_clusters(self):
        def provider(method, url, *, body, ttl):
            self.assertTrue(url.endswith("/local-sale/total"))
            self.assertEqual(body, {"period": {"from": self.today, "to": self.today}})
            return {"local_data": {"local_quantity": 3, "total_quantity": 4, "index": 75}}
        answer = ozon_expansion.read("ozon_delivery_time", self.dates, provider)
        self.assertFalse(answer["complete"])
        self.assertEqual(answer["items"][0]["local_units"], 3)
        self.assertIsNone(answer["items"][0]["cluster_id"])
        self.assertNotIn("article", answer["items"][0])

    def test_supply_slots_by_sku_does_not_request_account_warehouse_workload(self):
        from seller_ozon_views import _slots

        def fetch(group, report, inputs):
            self.assertEqual(report, "placement_zones")
            self.assertEqual(inputs, {"body": {"skus": ["17"]}})
            return {"products_placement": [{"sku": 17, "placement_zone": "zone"}]}

        reader = SimpleNamespace(args={"sku":17}, ids={"17"}, meta={}, fetch=fetch,
                                 section=lambda _, action: list(action()),
                                 product=lambda row, _, sku: [dict(row, article="A", sku=sku)])
        (row,) = _slots(reader)
        self.assertEqual((row["article"], row["sku"]), ("A", 17))
        self.assertEqual(reader.meta["warehouse_workload_status"], "product_eligibility_not_reported")

    def test_grouped_wb_orders_only_zero_fill_confirmed_days(self):
        args = {"article": "A", "group_by": "day"}
        events = [("A", "2026-11-01", "order", 3), ("A", "2026-11-01", "cancelled", 1)]
        partial = market_history.grouped_orders(args, events, [])
        self.assertEqual([(r["ordered_units"], r["cancelled_units"]) for r in partial], [(4, 1)])
        full = market_history.grouped_orders(args, events, ["2026-11-01", "2026-11-02"])
        self.assertEqual([(r["date"], r["ordered_units"]) for r in full], [("2026-11-01", 4), ("2026-11-02", 0)])

    def test_archive_unknown_cancellations_remain_unknown_in_daily_and_weekly_totals(self):
        day = self.today
        rows = [dict(article='A', date=day, quantity=1, cancelled=True),
                dict(article='A', date=day, quantity=2), dict(article='A', date=day, quantity=3, cancelled=False)]
        history = SimpleNamespace(read_days=lambda *_: {day: (day+'T12:00:00+03:00',rows)})
        for group in ('day', 'week'):
            report = market_history.historical_read(history, 'owner', 'wb_orders', dict(self.dates, article='A', group_by=group))
            row, = report['items']
            self.assertEqual((row['ordered_units'], row['confirmed_cancelled_units'], row['unknown_cancellation_units']), (6, 1, 2))
            self.assertIsNone(row['cancelled_units'])

    def test_only_complete_requested_weeks_have_confirmed_totals(self):
        args = dict(article='A', group_by='week', date_from='2026-11-16', date_to='2026-11-18')
        for covered, expected in ((['2026-11-16'], None), (['2026-11-16', '2026-11-17', '2026-11-18'], 0)):
            row, = market_history.grouped_orders(args, [], covered)
            self.assertEqual((row['ordered_units'], row['cancelled_units']), (expected, expected))
            self.assertEqual((row['period_start'], row['period_end'], row['covered_days'], row['requested_days']),
                             ('2026-11-16', '2026-11-18', len(covered), 3))
            self.assertEqual(row['observed_ordered_units'], 0)

    def test_archive_article_keeps_known_rows_and_distinguishes_unmapped_skus_from_zero(self):
        day = self.today
        rows = [dict(article='A', sku='11', ordered_units=2), dict(article=None, sku='17', ordered_units=0)]
        history = SimpleNamespace(read_days=lambda *_: {day: (day+'T12:00:00Z', rows)})

        def read(**query):
            return market_history.historical_read(history, 'owner', 'ozon_sku_daily', dict(self.dates, **query))

        for article, observed in [('A', [rows[0]]), ('X', [])]:
            report = read(article=article)
            self.assertEqual(report['items'], observed)
            self.assertEqual(report['unresolved_article_rows'], 1)
            self.assertIn('archived_article_identity_missing', report['issues'])
            self.assertFalse(report['complete'])
        self.assertFalse(read(article='X', sku=17)['complete'])
        self.assertTrue(read(article='X', sku=11)['complete'])
        self.assertTrue(read(sku=17)['complete'])
        self.assertEqual(read(sku=17)['items'], [rows[1]])

    def test_comparison_uses_exact_seller_posting_during_a_catalog_cooldown(self):
        args={"article":"WB-A","previous_from":"2026-09-01","previous_to":"2026-09-30",
              "date_from":"2026-10-01","date_to":"2026-10-31"}
        offer={"ozon_offer_id":"O-A","wb_vendor_code":"WB-A"}
        wires={"ozon":SimpleNamespace(gate_dir="fixture",creds={}),"wb":object()}
        identity=market_specs.result([{"ozon":market_specs.result([],complete=False,status="cooldown"),
                                      "wb":market_specs.result([{"article":"WB-A","nm_id":7}])}],complete=False)
        with patch.object(market_history,"History") as history, patch.object(market_composites,"comparison",return_value={}) as compare:
            history.return_value.verified_days.return_value={"2026-09-10":("stamp",[{"article":"O-A","offer_id":"O-A","sku":"19"}])}
            result=market_composites.verified_comparison(args,wires,offer,identity)
            self.assertIn("ozon",result["historical_identity_sources"])
            self.assertEqual(compare.call_args.args[2]["ozon_offer_id"],"O-A")
            other=dict(offer,ozon_offer_id="other")
            result=market_composites.verified_comparison(args,wires,other,identity)
            self.assertIn("ozon",result["unavailable_marketplaces"])

    def test_comparison_uses_only_a_verified_exact_marketplace_when_the_other_catalog_fails(self):
        args = {"article": "napkin"}
        wires = {"ozon": object(), "wb": object(), "performance": object()}
        mapping = {"wb_vendor_code": "WB-A", "ozon_offer_id": "O-A"}
        failed = market_specs.result([], complete=False, status="retry_time_unknown", http_status=429,
                                     retry_after_seconds=None)
        wb = market_specs.result([{"article": "WB-A", "nm_id": 7}])
        def identity(ozon, wildberries):
            return market_specs.result([{"wb": wildberries, "ozon": ozon}], complete=False)

        with patch.object(market_composites, "comparison", side_effect=lambda *a: market_specs.result([{"observed": 1}])) as compare:
            result = market_composites.verified_comparison(args, wires, mapping, identity(failed, wb))
            compare.assert_called_once_with(args, {"wb": wires["wb"]}, {"wb_vendor_code": "WB-A", "wb_nm_id": 7})
            self.assertFalse(result["complete"])
            self.assertEqual(result["unavailable_marketplaces"],
                             {"ozon": {"status": "retry_time_unknown", "http_status": 429, "retry_after_seconds": None}})
            compare.reset_mock()
            unresolved = identity(failed, wb)
            empty=market_composites.verified_comparison(args, wires, None, unresolved)
            self.assertFalse(empty["complete"])
            self.assertEqual((empty["items"],empty["status"]),([],"unverified_mapping"))
            self.assertEqual(empty["catalog_matches"],unresolved["items"])
            compare.assert_not_called()

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
            self.assertEqual(kwargs["body"]["selectedPeriod"], {
                "start": self.dates["date_from"] + "T00:00:00+03:00",
                "end": self.dates["date_to"] + "T23:59:59.999999+03:00",
            })
            self.assertEqual((kwargs["interval"], kwargs["basic_interval"]), (60, 10800))
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

    def test_feed_keeps_a_short_final_page_with_empty_snapshot_and_never_pages_without_one(self):
        raw = {'data': {'snapshotTime': '', 'currency': 'RUB', 'orders': [dict(
            srid='one', nmId=1, destinationDistrict='region', status='ordered', sellerPrice=0,
            updatedAt=self.today+'T01:00:00+03:00', createdAt=self.today+'T00:00:00+03:00')]}}
        wire = lambda *a, **kw: raw
        done = wb_reports.region(dict(self.dates, limit=2), wire)
        self.assertTrue(done['complete'])
        self.assertEqual(len(done['items']), 1)
        self.assertNotIn('data_as_of', done)
        partial = wb_reports.region(dict(self.dates, limit=1), wire)
        self.assertFalse(partial['complete'])
        self.assertIsNone(partial['next_cursor'])
        self.assertEqual(partial['status'], 'snapshot_required_for_continuation')
        later = wb_reports.region(dict(self.dates, limit=2, offset=1, snapshot_time=self.today+'T01:00:00Z'), wire)
        self.assertFalse(later['complete'])
        self.assertEqual(later['items'], [])

    def test_rating_accepts_single_localization_snapshot_and_history(self):
        index = {"calculation_date": "2026-10-01T00:00:00Z", "localization_percentage": 0}
        for source, expected in ((index, [index]), ([index], [index]), (None, []), ({}, [])):
            with self.subTest(source=source):
                result = ozon_expansion.read("ozon_rating", {}, lambda *a, **kw: {
                    "groups": [{"group_name": "service", "items": []}], "localization_index": source})
                self.assertEqual(result["localization_index"], expected)
                self.assertEqual(len(result["items"]), 1)
        partial = ozon_expansion.read('ozon_rating', {}, lambda *a, **kw: {'localization_index': index})
        self.assertFalse(partial['complete'])
        self.assertEqual(partial['groups_status'], 'not_reported')
        self.assertEqual(partial['localization_index'], [index])

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
