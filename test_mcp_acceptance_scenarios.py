"""End-to-end scenario logic against a synthetic MCP tool catalogue, without seller data."""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from tools import mcp_acceptance as runner
from tools import mcp_acceptance_checks as checks
from test_mcp_acceptance import product_catalog, slots, SKU_1, SKU_2, WB_NM, WAREHOUSE


def page(rows, **extra):
    return {"items": rows, "complete": True, "next_cursor": None, **extra}


def warehouse_plan(ozon):
    key = str(WAREHOUSE) if ozon else "private-warehouse"
    base = {
        "available_units": 1,
        "inbound_units": 2 if ozon else None,
        "ordered_units": {"7": 2, "30": 2, "90": 2},
        "days_of_cover": "15",
        "orders_per_day": {str(d): str(2 / d) for d in (7, 30, 90)},
        "recommendation_without_inbound": 1,
        "recommendation_if_inbound_arrives": 0 if ozon else None,
        "confirmed_recommended_quantity": None,
    }
    return {
        "marketplace": "ozon" if ozon else "wb",
        "source_complete": {"stock": True, "orders": True},
        "complete": True,
        "scenario_recommendations_complete": True,
        "orders_through": "2026-11-17",
        "orders_date_basis": "posting_creation_utc" if ozon else "event_date_moscow",
        "fulfillment_scope": "Ozon FBO" if ozon else "Wildberries FBW",
        "locations": [
            {
                **base,
                "warehouse": key,
                "warehouse_id": key if ozon else None,
                "cluster_id": 41 if ozon else None,
            }
        ],
        "clusters": [{**base, "cluster_id": 41}] if ozon else [],
        "marketplace_total": {
            **base,
            "ship_without_transfers_without_inbound": 1,
            "ship_without_transfers_if_inbound_arrives": 0 if ozon else None,
        },
    }


class Reports:
    def __init__(self):
        self.calls = []
        self.change = None

    def rpc(self, method, params):
        assert method == "tools/call"
        name, args = params["name"], params["arguments"].copy()
        self.calls.append((name, args))
        result = self.result(name, args)
        if self.change:
            self.change(name, args, result)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    def result(self, name, a):
        oz = {"sku": SKU_1, "article": checks.OZ}
        wb = {"article": checks.WB, "nm_id": WB_NM, "barcode": "private-barcode"}
        if name == "ozon_product_info":
            return product_catalog()
        if name == "wb_products":
            return {"items": [{"nmID": WB_NM, "vendorCode": checks.WB}], "cursor": {"total": 1}}
        if name == "wb_supplies":
            return page([{"supply_id": 81}], period_basis="supply_creation_moscow")
        if name == "wb_supply_goods":
            return page([{**wb, "quantity": 8, "accepted_quantity": 7}])
        if name == "ozon_supply_orders":
            return page(
                [{"order_id": 82, "supplies": [{"supply_id": 83, "bundle_id": "private-bundle"}]}],
                period_basis="timeslot_start_utc",
            )
        if name == "ozon_supply_items":
            return page(
                [{**oz, "declared_quantity": 8}],
                acts=[
                    {
                        **oz,
                        "act_id": 91,
                        "type": "ACCEPTANCE",
                        "act_state": "ACCEPTED",
                        "fact_quantity": 7,
                        "approved_quantity": 7,
                    }
                ],
                agreement_completed=True,
            )
        if name == "wb_stocks":
            return page(
                [
                    {**wb, "warehouse": name, "kind": kind, "quantity": n}
                    for name, kind, n in (
                        ("private-warehouse", "warehouse", 1),
                        ("total", "provider_total", 1),
                        ("out", "in_transit_to_customer", 0),
                        ("back", "returning_from_customer", 0),
                    )
                ]
            )
        if name == "ozon_stocks_by_cluster" and "date_from" not in a:
            return page(
                [
                    {
                        **oz,
                        "sku": s,
                        "available_stock_count": n,
                        "transit_stock_count": 1,
                        "cluster_id": 41,
                        "warehouse_id": WAREHOUSE,
                    }
                    for s, n in ((SKU_1, 1), (SKU_2, 0))
                ]
            )
        if name == "ozon_stocks":
            return page(
                [
                    {**oz, "sku": s, "scheme": "fbo", "reserved": 0, "present": n}
                    for s, n in ((SKU_1, 1), (SKU_2, 0))
                ]
            )
        if name == "wb_orders":
            rows = [
                {
                    **wb,
                    "warehouse_type": "Склад WB",
                    "warehouse": "private-warehouse",
                    "srid": f"private-order-{i}",
                    "date": "2026-11-17T10:00:00+03:00",
                    "quantity": 1,
                }
                for i in (1, 2)
            ]
            return (
                page(rows[1:])
                if a.get("cursor")
                else page(rows[:1], complete=False, next_cursor="private-cursor")
            )
        if name == "ozon_fbo_postings_geo":
            return page(
                [
                    {
                        **oz,
                        "posting_number": "private-posting",
                        "warehouse_id": WAREHOUSE,
                        "created_at": "2026-11-17T05:00:00Z",
                        "quantity": 2,
                    }
                ]
            )
        if name == "ozon_clusters":
            return page([{"id": 41, "warehouses": [{"warehouse_id": WAREHOUSE}]}])
        if name == "supply_plan":
            assert a["mapping"] == {
                "ozon_offer_id": checks.OZ,
                "wb_vendor_code": checks.WB,
                "wb_nm_id": WB_NM,
            }
            return page(
                [warehouse_plan(True), warehouse_plan(False)], baseline_days=30, account_coverage="partial"
            )
        if name == "wb_acceptance_slots":
            return slots()
        if name == "wb_finance_report":
            fields = {
                "commission_ex_vat": 2,
                "commission_vat": 1,
                "delivery": 5,
                "rebilled_logistics": 1,
                "storage": 3,
                "acceptance": 2,
                "penalty": 0,
            }
            if a["mode"] == "summary":
                amounts = {**fields, "commission_including_vat": 3}
                return page(
                    [
                        {
                            **wb,
                            "currency": "RUB",
                            "sold_units": 2,
                            "returned_units": 0,
                            "amounts": amounts,
                            "per_sold_unit": {k: str(v / 2) for k, v in amounts.items()},
                        }
                    ],
                    summary_scope="requested_period",
                    unit_basis="sold_units_before_returns",
                    period_from=a["date_from"],
                    period_to=a["date_to"],
                    rows_observed=1,
                    unallocated_rows=0,
                )
            if a.get("cursor") == 5:
                return page([], status="end_of_report")
            return page(
                [
                    {
                        **wb,
                        **fields,
                        "rrd_id": 5,
                        "quantity": 2,
                        "document": "Продажа",
                        "operation": "Продажа",
                        "currency": "RUB",
                    }
                ],
                complete=False,
                next_cursor=5,
            )
        if name == "ozon_cash_flow":
            if a["mode"] == "summary":
                return page(
                    [],
                    summary_scope="requested_period",
                    period_from=a["date_from"],
                    period_to=a["date_to"],
                    payout_by_currency={"RUB": "93.20"},
                )
            return page(
                [{"currency_code": "RUB"}],
                details={
                    "services": {"total": "-6.8"},
                    "period": {"begin": a["date_from"] + "T00:00:00Z", "end": a["date_to"] + "T23:59:59Z"},
                },
                payments=[{"currency_code": "RUB", "payment": "93.20"}],
            )
        if name in ("wb_ads", "ozon_ads"):
            is_wb = name == "wb_ads"
            if "campaign_ids" not in a:
                return page([{"campaign_id" if is_wb else "id": 12}], status="choose_campaign_ids")
            item = wb if is_wb else oz
            return page(
                [
                    {
                        **item,
                        "campaign_id": 12,
                        "date": "2026-10-05",
                        "currency": "RUB",
                        "spend": 2,
                        "conversion_type": "direct",
                        "ordered_units": 1,
                        "orders": 1,
                    }
                ],
                order_share={
                    "scope": "selected_campaigns_separately",
                    "status": "ready",
                    "rows": [
                        {
                            **item,
                            "campaign_id": 12,
                            "direct_attributed": 1,
                            "total_orders_or_units": 4,
                            "unique_orders": None if is_wb else 4,
                            "ordered_units": 4,
                            "percent": "25",
                        }
                    ],
                },
            )
        if name in ("ozon_sku_daily", "ozon_stocks_by_cluster"):
            start, end = map(date.fromisoformat, (a["date_from"], a["date_to"]))
            dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
            if name == "ozon_sku_daily":
                return page(
                    [
                        {
                            **oz,
                            "sku": s,
                            "date": d,
                            "ordered_units": (64 if start.month == 9 else 19)
                            if (s, d) == (SKU_1, dates[0])
                            else 0,
                            "hits_view_pdp": 10,
                            "hits_tocart_pdp": 2,
                            "conv_tocart_pdp": 20,
                        }
                        for d in dates
                        for s in (SKU_1, SKU_2)
                    ]
                )
            return page(
                [
                    {**oz, "sku": s, "observation_date": d, "available_stock_count": 1}
                    for d in dates
                    for s in (SKU_1, SKU_2)
                ],
                source="locally_observed_ozon_warehouse_snapshots",
                missing_or_partial_dates=[],
            )
        raise AssertionError("unexpected tool " + name)


class ScenarioChecks(unittest.TestCase):
    def run_scenario(self, scenario, reports):
        options = runner.arguments(
            [
                "--scenario",
                str(scenario),
                "--output",
                "unused",
                "--as-of",
                "2026-11-18",
                "--warehouse-id",
                str(WAREHOUSE),
            ]
        )
        evidence = {"tools": {}, "checks": []}
        reader = runner.Reader(reports, options, evidence)
        checks.evaluate(reader, options)
        return evidence

    def test_nine_complete_synthetic_collections(self):
        for n in range(1, 10):
            with self.subTest(scenario=n):
                reports = Reports()
                evidence = self.run_scenario(n, reports)
                self.assertTrue(evidence["checks"])
                self.assertTrue(all(name in checks.SCENARIOS[n] for name, _ in reports.calls))
                self.assertNotIn("private-", json.dumps(evidence))
                if n in (3, 5):
                    self.assertEqual(evidence["tools"]["wb_orders"]["pages"], 2)
                if n == 6:
                    self.assertEqual(evidence["tools"]["wb_finance_report"]["pages"], 3)

    def test_a_contradiction_blocks_each_scenario(self):
        cases = {
            1: ("ozon_supply_items", lambda d: d.update(agreement_completed=False)),
            2: ("ozon_stocks_by_cluster", lambda d: d["items"].pop()),
            3: ("wb_orders", lambda d: d["items"][0].update(date="2026-11-18T09:00:00+03:00")),
            4: ("wb_acceptance_slots", lambda d: d["items"].pop()),
            5: (
                "supply_plan",
                lambda d: d["items"][0]["clusters"][0].update(recommendation_without_inbound=5),
            ),
            6: ("wb_finance_report", lambda d: d.update(rows_observed=99)),
            7: ("ozon_cash_flow", lambda d: d.update(payout_by_currency={"RUB": "9999"})),
            8: (
                "ozon_ads",
                lambda d: d["order_share"].update(status="incomparable_or_empty_counts")
                if "order_share" in d
                else None,
            ),
            9: ("ozon_stocks_by_cluster", lambda d: d["items"].pop()),
        }
        for scenario, (target, mutate) in cases.items():
            with self.subTest(scenario=scenario):
                reports = Reports()
                reports.change = lambda name, args, data: mutate(data) if name == target else None
                with self.assertRaises(checks.Stop):
                    self.run_scenario(scenario, reports)

    def test_repeated_cursor_missing_completion_and_provider_retention(self):
        cases = (
            (lambda data: data.update(complete=False, next_cursor="same-page"), "nonadvancing_cursor"),
            (lambda data: data.pop("complete"), "completion_flag_missing"),
            (lambda data: data.update(complete=False, next_cursor=None), "incomplete_terminal_page"),
            (
                lambda data: data.update(status="outside_provider_retention", retention_days=90),
                "outside_provider_retention",
            ),
        )
        for change, code in cases:
            with self.subTest(code=code):
                reports = Reports()
                reports.change = lambda name, args, data: change(data) if name == "wb_orders" else None
                opts = runner.arguments(["--output", "unused", "--scenario", "3"])
                evidence = {"tools": {}, "checks": []}
                with self.assertRaises(checks.Stop) as caught:
                    runner.Reader(reports, opts, evidence).rows("wb_orders")
                self.assertEqual(caught.exception.code, code)
                if code == "outside_provider_retention":
                    self.assertEqual(evidence["tools"]["wb_orders"]["retention_days"], 90)

    def test_summary_rate_limit_retains_http_date_and_unknown_delay_stops(self):
        for after, expected_delay in (("Thu, 01 Jan 1970 00:01:47 GMT", 47), ("unknown", None)):
            with self.subTest(after=after):
                reports = Reports()
                reports.change = lambda name, args, data: data.update(
                    status="pending", rate_limited=True, retry_after_seconds=after
                )
                opts = runner.arguments(["--output", "unused", "--scenario", "7", "--max-wait", "10"])
                evidence = {"tools": {}, "checks": []}
                with (
                    patch("time.time", return_value=60),
                    patch("time.sleep") as wait,
                    self.assertRaises(checks.Stop) as caught,
                ):
                    runner.Reader(reports, opts, evidence).rows(
                        "ozon_cash_flow", mode="summary", date_from="2026-10-01", date_to="2026-10-31"
                    )
                self.assertEqual((caught.exception.http, caught.exception.delay), (429, expected_delay))
                self.assertEqual(evidence["tools"]["ozon_cash_flow"]["last_pending_reason"], "rate_limited")
                self.assertEqual(len(reports.calls), 1)
                wait.assert_not_called()

    def test_statement_requires_breakdown_for_each_payment_period(self):
        for report_final_breakdown in (False, True):
            with self.subTest(report_final_breakdown=report_final_breakdown):
                reports = Reports()

                def change(name, args, data):
                    if name != "ozon_cash_flow":
                        return
                    if args["mode"] == "summary":
                        data["payout_by_currency"] = {"RUB": "186.40"}
                        return
                    first = args["page"] == 1
                    dates = {
                        "begin": ("2026-10-01" if first else "2026-10-16") + "T00:00:00Z",
                        "end": ("2026-10-15" if first else "2026-10-31") + "T23:59:59Z",
                    }
                    data.update(
                        details={"period": dates}, complete=not first, next_cursor=2 if first else None
                    )
                    data["items"] = (
                        [{"period": dates, "item_delivery_and_return_amount": 0}]
                        if first or report_final_breakdown
                        else []
                    )

                reports.change = change
                if report_final_breakdown:
                    self.assertEqual(self.run_scenario(7, reports)["statement_periods_reconciled"], 2)
                else:
                    with self.assertRaisesRegex(checks.Stop, "payments_not_reconciled"):
                        self.run_scenario(7, reports)

    def test_pending_download_reuses_issued_task_and_rejects_changed_identity(self):
        for returned in ("private-task", "different-task"):
            with self.subTest(returned=returned):
                reports = Reports()

                def change(name, args, data):
                    if name == "wb_stocks":
                        if "task_id" in args:
                            self.assertEqual(args["task_id"], "private-task")
                            data["task_id"] = returned
                        else:
                            data.update(
                                status="pending",
                                complete=False,
                                task_id="private-task",
                                retry_after_seconds=4,
                            )

                reports.change = change
                opts = runner.arguments(["--output", "unused", "--scenario", "2", "--max-wait", "4"])
                evidence = {"tools": {}, "checks": []}
                reader = runner.Reader(reports, opts, evidence)
                with patch("time.sleep") as wait:
                    if returned == "private-task":
                        self.assertTrue(reader.rows("wb_stocks"))
                    else:
                        with self.assertRaises(checks.Stop):
                            reader.rows("wb_stocks")
                    wait.assert_called_once_with(4)
                self.assertNotIn("private-task", json.dumps(evidence))
