"""Independent synthetic tests for V2 planning and realized unit calculations."""

from datetime import date, timedelta
from decimal import Decimal, localcontext
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import market_composites
import market_history
import supply_plan


AS_OF = date(2026, 4, 5)


def day(ago):
    return (AS_OF - timedelta(days=ago)).isoformat()


def planning_state(*, stockout=(0,), known=None, wholesale=None, available=3, inbound=4):
    """Thirty synthetic completed days and one explicitly identified bulk order."""
    location = {
        "warehouse": "synthetic warehouse", "warehouse_id": "77", "cluster": "synthetic cluster",
        "cluster_id": 9,
        "daily": {day(1): 2, day(2): 3, day(5): 14},
        "days_with_cancellations": {},
        "stock": {"101": {"available": available, "inbound": inbound}},
        "stockout_days": [day(i) for i in stockout],
        "known_stock_days": [day(i) for i in range(30)] if known is None else list(known),
        "wholesale_daily": {day(5): 10} if wholesale is None else dict(wholesale),
    }
    return {
        "locations": {"77": location}, "done": {"stock": True, "orders": True}, "issues": set(),
        "planning": {"lead_time_days": 2, "safety_stock_percent": 25, "bulk_order_min_units": 10},
    }


def amounts(**changes):
    # Every number is synthetic and denominated in the explicitly supplied currency.
    result = {key: Decimal(value) for key, value in {
        "gross_sales": "400", "commission": "40", "logistics": "20", "storage": "8",
        "acceptance": "4", "ads": "12", "retentions": "4", "for_pay": "360",
    }.items()}
    result.update(changes)
    return result


class OfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for method in ("connect", "connect_ex"):
            restriction = patch.object(socket.socket, method, side_effect=AssertionError("network prohibited"))
            restriction.start()
            cls.addClassCleanup(restriction.stop)


class SupplyPlanV2Contracts(OfflineTest):
    def test_stockout_and_wholesale_are_removed_without_losing_normal_units(self):
        state = planning_state()
        row, = supply_plan._locations(state, 27, AS_OF)
        # 2 + 3 + (14 - 10) = 9 units over 29 confirmed in-stock days.
        # ceil(9/29 * (27+2) * 1.25 - 3 - 4) = 5.
        self.assertEqual(row["calendar_ordered_units"]["30"], 19)
        with localcontext() as ctx:
            ctx.prec = 50
            self.assertLess(abs(Decimal(str(row["orders_per_day"]["30"])) - Decimal(9) / 29), Decimal("1e-25"))
        self.assertEqual(row["recommendation_without_inbound"], 9)
        self.assertEqual(row["recommendation_if_inbound_arrives"], 5)
        self.assertEqual(row["recommended_quantity"], 5)

    def test_unknown_inbound_is_not_assumed_to_arrive(self):
        row, = supply_plan._locations(planning_state(inbound=None), 27, AS_OF)
        self.assertEqual(row["recommendation_without_inbound"], 9)
        self.assertIsNone(row["recommendation_if_inbound_arrives"])
        self.assertIsNone(row["confirmed_recommended_quantity"])

    def test_unobserved_day_cannot_confirm_a_recommendation(self):
        state = planning_state(known=[day(i) for i in range(29)])
        row, = supply_plan._locations(state, 27, AS_OF)
        self.assertIsNone(row["confirmed_recommended_quantity"])

    def test_all_stockout_days_do_not_make_unknown_rate_zero(self):
        state = planning_state(stockout=range(30), wholesale={})
        state["locations"]["77"]["daily"] = {}
        row, = supply_plan._locations(state, 27, AS_OF)
        self.assertIsNone(row["orders_per_day"]["30"])
        self.assertIsNone(row["confirmed_recommended_quantity"])

    def test_older_and_future_dates_are_outside_the_calculation_window(self):
        state = planning_state()
        state["locations"]["77"]["daily"].update({day(30): 900, day(-1): 700})
        row, = supply_plan._locations(state, 27, AS_OF)
        self.assertEqual(row["calendar_ordered_units"]["30"], 19)
        self.assertEqual(row["recommendation_if_inbound_arrives"], 5)

    def test_bulk_exclusions_cannot_exceed_the_day_total(self):
        state = planning_state(wholesale={day(5): 15})  # Observed total for this day is only 14.
        try:
            row, = supply_plan._locations(state, 27, AS_OF)
        except ValueError:
            return
        self.assertIsNone(row["confirmed_recommended_quantity"])
        self.assertIsNone(row["orders_per_day"]["30"])

    def test_duplicate_observation_day_does_not_dilute_demand(self):
        state = planning_state()
        known = state["locations"]["77"]["known_stock_days"]
        known.append(known[2])
        known.reverse()
        try:
            row, = supply_plan._locations(state, 27, AS_OF)
        except ValueError:
            return
        with localcontext() as ctx:
            ctx.prec = 50
            self.assertLess(abs(Decimal(str(row["orders_per_day"]["30"])) - Decimal(9) / 29), Decimal("1e-25"))

    def test_one_zero_snapshot_does_not_prove_an_entire_day_without_stock(self):
        class Wire:
            gate_dir = "unused-by-synthetic-archive"
            creds = {"client_id": "synthetic"}
            def __call__(self, method, url, *, body, **kwargs):
                if url.endswith('/v3/product/info/list'):
                    return {'items': [{'id': 1, 'offer_id': 'A', 'name': 'A', 'sku': 101,
                                      'sources': [{'sku': 101, 'source': 'FBO'}]}]}
                if url.endswith('/v1/analytics/stocks'):
                    return {'items': [{'offer_id': 'A', 'sku': 101, 'warehouse_id': 77,
                                       'cluster_id': 9, 'available_stock_count': 0, 'transit_stock_count': 0}]}
                if url.endswith('/v3/posting/fbo/list'):
                    return {'has_next': False, 'postings': [{'posting_number': 'synthetic-1',
                        'created_at': day(0) + 'T08:00:00Z', 'status': 'delivered',
                        'analytics_data': {'warehouse_id': 77},
                        'products': [{'offer_id': 'A', 'sku': 101, 'quantity': 3}]}]}
                raise AssertionError('unexpected synthetic request')
        # One point-in-time snapshot per day: the zero at 20:00 does not show
        # whether stock existed earlier when the 08:00 order was placed.
        observations = {day(i): (day(i) + 'T20:00:00Z', [{'article': 'A', 'sku': 101,
            'warehouse_id': 77, 'available_stock_count': 0 if i == 0 else 5}]) for i in range(30)}
        archive = SimpleNamespace(verified_days=lambda *_args: observations)
        with patch.object(market_history, 'History', return_value=archive), \
             patch.object(market_history, 'scope', return_value='synthetic'):
            report = supply_plan._platform('ozon', 'A', None, Wire(), 27, AS_OF,
                {'lead_time_days': 2, 'safety_stock_percent': 25, 'bulk_order_min_units': 10})
        row, = report['locations']
        self.assertEqual(row['calendar_ordered_units']['30'], 3)
        self.assertIsNone(row['confirmed_recommended_quantity'])


class UnitEconomicsV2Contracts(OfflineTest):
    def row(self, *, provider="wb", units=4, returned=1, figures=None,
            cost=Decimal("20"), currency="RUB", cost_currency="RUB"):
        return market_composites.unit_row(provider, "synthetic-article", currency, units, returned,
                                          amounts() if figures is None else figures,
                                          cost=cost, cost_currency=cost_currency)

    def test_margin_uses_gross_sold_units_and_does_not_subtract_commission_twice(self):
        # for_pay=360 already includes commission; other expenses total 48.
        # (360-20-8-4-12-4)/4 - 20 = 58 per gross sold unit.
        for provider in ("wb", "ozon"):
            with self.subTest(provider=provider):
                row = self.row(provider=provider)
                self.assertEqual(Decimal(str(row["per_sold_unit"]["for_pay"])), Decimal("90"))
                self.assertEqual(Decimal(str(row["per_sold_unit"]["commission"])), Decimal("10"))
                self.assertEqual(Decimal(str(row["margin_per_unit"])), Decimal("58"))
                self.assertIsNone(row["bank_payout_per_unit"])

    def test_returns_are_not_silently_subtracted_from_the_gross_sold_denominator(self):
        for returned in (0, 1, 4, 7):
            with self.subTest(returned=returned):
                row = self.row(returned=returned)
                self.assertEqual(Decimal(str(row["per_sold_unit"]["for_pay"])), Decimal("90"))
                self.assertEqual(Decimal(str(row["margin_per_unit"])), Decimal("58"))
                self.assertIsNone(row["bank_payout_per_unit"])

    def test_missing_any_component_does_not_create_a_confirmed_margin(self):
        for missing in amounts():
            with self.subTest(missing=missing):
                row = self.row(figures=amounts(**{missing: None}))
                self.assertIsNone(row["margin_per_unit"])
                self.assertIsNone(row["per_sold_unit"].get(missing))
                self.assertIsNone(row["bank_payout_per_unit"])

    def test_missing_or_different_cost_currency_does_not_create_a_margin(self):
        for cost, currency, cost_currency in ((None, "RUB", "RUB"), (Decimal("20"), "RUB", None),
                                               (Decimal("20"), "RUB", "KZT"), (Decimal("20"), None, "RUB")):
            with self.subTest(cost=cost, currency=currency, cost_currency=cost_currency):
                row = self.row(cost=cost, currency=currency, cost_currency=cost_currency)
                self.assertIsNone(row["margin_per_unit"])
                self.assertIsNone(row["bank_payout_per_unit"])

    def test_explicit_zero_cost_is_distinct_from_missing_cost(self):
        row = self.row(cost=Decimal(0))
        self.assertEqual(Decimal(str(row["margin_per_unit"])), Decimal("78"))
        self.assertIsNone(row["bank_payout_per_unit"])

    def test_explicit_zero_receipt_can_yield_a_negative_margin(self):
        row = self.row(figures=amounts(for_pay=Decimal(0)))
        self.assertEqual(Decimal(str(row["margin_per_unit"])), Decimal("-32"))

    def test_zero_units_do_not_create_per_unit_amounts(self):
        row = self.row(units=0, returned=0)
        self.assertIsNone(row["margin_per_unit"])
        self.assertIsNone(row["bank_payout_per_unit"])
        self.assertTrue(all(value is None for value in row["per_sold_unit"].values()))

    def test_decimal_aggregation_preserves_a_small_amount(self):
        large = Decimal("10000000000000000000000000000.01")
        figures = amounts(gross_sales=large, commission=Decimal(0), logistics=Decimal(0),
                          storage=Decimal(0), acceptance=Decimal(0), ads=Decimal(0),
                          retentions=Decimal(0), for_pay=large)
        try:
            row = self.row(units=1, returned=0, figures=figures, cost=Decimal(0))
        except (ArithmeticError, ValueError):
            return  # A documented numeric bound is safer than a rounded financial total.
        self.assertEqual(Decimal(str(row["per_sold_unit"]["for_pay"])), large)
        self.assertEqual(Decimal(str(row["margin_per_unit"])), large)
        self.assertIsNone(row["bank_payout_per_unit"])

    def test_nonfinite_cost_does_not_produce_a_finite_margin(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=value):
                try:
                    row = self.row(cost=value)
                except (ArithmeticError, ValueError):
                    continue
                self.assertIsNone(row["margin_per_unit"])

    def test_invalid_units_and_negative_cost_cannot_generate_a_margin(self):
        for units, cost in ((True, Decimal("20")), (-1, Decimal("20")),
                            (Decimal("1.5"), Decimal("20")), (4, Decimal("-0.01"))):
            with self.subTest(units=units, cost=cost):
                try:
                    row = self.row(units=units, cost=cost)
                except (TypeError, ArithmeticError, ValueError):
                    continue
                self.assertIsNone(row["margin_per_unit"])

    def test_nonfinite_reported_amount_cannot_generate_a_margin(self):
        for field in ("for_pay", "gross_sales", "ads"):
            with self.subTest(field=field):
                try:
                    row = self.row(figures=amounts(**{field: Decimal("NaN")}))
                except (ArithmeticError, ValueError):
                    continue
                self.assertIsNone(row["margin_per_unit"])

    def test_negative_expense_does_not_masquerade_as_an_increase_in_profit(self):
        for field in ("logistics", "storage", "acceptance", "ads", "retentions"):
            with self.subTest(field=field):
                try:
                    row = self.row(figures=amounts(**{field: Decimal("-1")}))
                except (ArithmeticError, ValueError):
                    continue
                self.assertIsNone(row["margin_per_unit"])


if __name__ == "__main__":
    unittest.main()
