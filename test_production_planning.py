import unittest
from decimal import Decimal

from production import PlanningError, allocate_capacity, first_batch, replenishment


class ProductionPlanningTests(unittest.TestCase):
    def test_first_batch_limits_and_rounding(self):
        self.assertEqual(first_batch("0.5")["proposed_quantity"], 10)
        self.assertEqual(first_batch("1.1")["proposed_quantity"], 16)
        self.assertEqual(first_batch("3")["proposed_quantity"], 30)
        self.assertTrue(first_batch("1.1")["owner_approval_required"])

    def test_reorder_uses_lead_time_stock_and_in_transit(self):
        result = replenishment("1.2", "5", 10, 2)
        self.assertEqual(result["reorder_point"], Decimal("14.4"))
        self.assertTrue(result["below_reorder_point"])
        self.assertEqual(result["suggested_quantity"], 14)
        self.assertFalse(replenishment(1, 3, 10, 50)["below_reorder_point"])
        self.assertEqual(replenishment(1, 3, 10, 50)["suggested_quantity"], 0)

    def test_capacity_orders_by_profit_and_reports_unplanned_time(self):
        products = [{"sku": "low", "quantity": 3, "print_hours": 4, "profit_per_hour": 60},
                    {"sku": "high", "quantity": 3, "print_hours": 5, "profit_per_hour": 100}]
        result = allocate_capacity(1, 1, products)
        self.assertEqual(result["capacity_hours"], Decimal("16.8"))
        self.assertEqual(result["products"][0]["sku"], "high")
        self.assertEqual(result["products"][0]["planned_quantity"], 3)
        self.assertEqual(result["products"][1]["planned_quantity"], 0)
        self.assertEqual(result["unplanned_hours"], Decimal("12"))
        self.assertEqual(result["available_hours_after_plan"], Decimal("1.8"))

    def test_capacity_is_never_rounded_up_to_fit_a_longer_print(self):
        exact = {"sku": "equal", "quantity": 1, "print_hours": "16.8", "profit_per_hour": 1}
        too_long = {"sku": "long", "quantity": 1,
                    "print_hours": "16.8000000000000000000000000001", "profit_per_hour": 1}
        self.assertEqual(allocate_capacity(1, 1, [exact])["products"][0]["planned_quantity"], 1)
        result = allocate_capacity(1, 1, [too_long])
        self.assertEqual(result["products"][0]["planned_quantity"], 0)
        self.assertEqual(result["available_hours_after_plan"], Decimal("16.8"))
        self.assertEqual(result["unplanned_hours"], Decimal(too_long["print_hours"]))

    def test_reorder_threshold_does_not_round_away_small_difference(self):
        result = replenishment("1.0000000000000000000000000001", 3, 10, 0)
        self.assertTrue(result["below_reorder_point"])
        self.assertGreater(result["reorder_point"], Decimal(10))

    def test_bad_or_missing_inputs_do_not_become_a_plan(self):
        for fn, args in [(first_batch, (None,)), (first_batch, ("1e-1001",)), (replenishment, (1, 2, -1, 0)),
                         (replenishment, (1, 2, 1.5, 0)), (allocate_capacity, (True, 1, [])),
                         (allocate_capacity, (1, 1, [{"sku": "x", "quantity": 1, "print_hours": 0, "profit_per_hour": 1}]))]:
            with self.subTest(fn=fn.__name__, args=args), self.assertRaises(PlanningError):
                fn(*args)
        with self.assertRaises(PlanningError):
            allocate_capacity(1, 1, [{"sku": "x", "quantity": 1, "print_hours": 1, "profit_per_hour": 1},
                                     {"sku": "x", "quantity": 1, "print_hours": 1, "profit_per_hour": 1}])


if __name__ == "__main__":
    unittest.main()
