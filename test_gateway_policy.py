import unittest
from gateway.policy import evaluate


class GatewayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.base = {'owner_approved': False, 'autonomy_enabled': True, 'first_month_completed': True}
        self.price = {**self.base, 'current_price': '100', 'minimum_price': '80',
                      'now': '2026-11-12T12:00:00+00:00',
                      'last_price_change_at': '2026-11-11T12:00:00+00:00'}
        self.ads = {**self.base, 'product_spent': 20, 'current_product_budget': 100,
                    'approved_product_budget': 120, 'today_spent': 5, 'daily_budget': 20}
        self.prints = {**self.base, 'monthly_used': 8, 'monthly_limit': 10,
                       'is_first_batch': False}

    def test_unknown_missing_and_invalid_fail_closed(self):
        for kind in ('price', 'advertising', 'review', 'print', 'publish'):
            with self.subTest(kind=kind):
                self.assertFalse(evaluate(kind, {}, {})['allowed'])
        self.assertFalse(evaluate('other', {}, self.base)['allowed'])
        self.assertFalse(evaluate('review', {'review_id': 'r1'}, {**self.base, 'review_id': 'r1', 'rating': True})['allowed'])
        self.assertFalse(evaluate('price', {'new_price': 'NaN'}, self.price)['allowed'])
        no_timestamp = dict(self.price)
        del no_timestamp['last_price_change_at']
        self.assertFalse(evaluate('price', {'new_price': 100}, no_timestamp)['allowed'])

    def test_first_month_requires_approval_despite_passing_other_limits(self):
        self.assertFalse(evaluate('review', {'review_id': 'r1'}, {'review_id': 'r1', 'rating': 5})['allowed'])
        self.assertFalse(evaluate('review', {'review_id': 'r1'}, {**self.base, 'review_id': 'r1', 'rating': 5, 'first_month_completed': False})['allowed'])
        self.assertTrue(evaluate('review', {'review_id': 'r1'}, {**self.base, 'review_id': 'r1', 'rating': 5})['allowed'])
        self.assertTrue(evaluate('review', {'review_id': 'r1'}, {'owner_approved': True, 'review_id': 'r1', 'rating': 5})['allowed'])

    def test_price_corridor_floor_and_cooldown(self):
        self.assertTrue(evaluate('price', {'new_price': 105}, self.price)['allowed'])
        self.assertFalse(evaluate('price', {'new_price': '105.0000000000000000000000000001'}, self.price)['allowed'])
        self.assertFalse(evaluate('price', {'new_price': 79}, {**self.price, 'owner_approved': True})['allowed'])
        self.assertFalse(evaluate('price', {'new_price': 100}, {**self.price, 'last_price_change_at': '2026-11-12T00:00:00Z', 'owner_approved': True})['allowed'])
        self.assertTrue(evaluate('price', {'new_price': 106}, {**self.price, 'owner_approved': True})['allowed'])
        self.assertFalse(evaluate('price', {'new_price': 100}, {**self.price, 'last_price_change_at': '2026-11-13T00:00:00Z'})['allowed'])

    def test_advertising_stays_within_both_trusted_caps(self):
        self.assertTrue(evaluate('advertising', {'spend': 15, 'new_product_budget': 100}, self.ads)['allowed'])
        self.assertFalse(evaluate('advertising', {'spend': 16, 'new_product_budget': 100}, self.ads)['allowed'])
        self.assertFalse(evaluate('advertising', {'spend': 1, 'new_product_budget': 130}, {**self.ads, 'owner_approved': True})['allowed'])
        self.assertFalse(evaluate('advertising', {'spend': 1, 'new_product_budget': 110}, self.ads)['allowed'])
        self.assertTrue(evaluate('advertising', {'spend': 1, 'new_product_budget': 110}, {**self.ads, 'owner_approved': True})['allowed'])
        self.assertFalse(evaluate('advertising', {'spend': 1, 'new_product_budget': 20}, self.ads)['allowed'])

    def test_review_and_first_batch_require_specific_approval(self):
        self.assertFalse(evaluate('review', {'review_id': 'r1'}, {**self.base, 'review_id': 'r1', 'rating': 3})['allowed'])
        self.assertTrue(evaluate('review', {'review_id': 'r1'}, {**self.base, 'owner_approved': True, 'review_id': 'r1', 'rating': 3})['allowed'])
        self.assertTrue(evaluate('print', {'quantity': 2}, self.prints)['allowed'])
        self.assertFalse(evaluate('print', {'quantity': 3}, {**self.prints, 'owner_approved': True})['allowed'])
        self.assertFalse(evaluate('print', {'quantity': 2}, {**self.prints, 'is_first_batch': True})['allowed'])
        self.assertTrue(evaluate('print', {'quantity': 2}, {**self.prints, 'is_first_batch': True, 'owner_approved': True})['allowed'])

    def test_rating_is_trusted_and_bound_to_review_id(self):
        state = {**self.base, 'review_id': 'actual', 'rating': 1}
        self.assertFalse(evaluate('review', {'review_id': 'actual', 'rating': 5}, state)['allowed'])
        self.assertFalse(evaluate('review', {'review_id': 'other'}, {**state, 'rating': 5})['allowed'])
        self.assertFalse(evaluate('review', {'review_id': 'actual'}, state)['allowed'])
        self.assertTrue(evaluate('review', {'review_id': 'actual'}, {**state, 'rating': 5})['allowed'])

    def test_publish_needs_all_three_checks(self):
        full = {'owner_approved': True, 'reviewer_pass': True, 'rights_settled': True}
        self.assertTrue(evaluate('publish', {}, full)['allowed'])
        for key in full:
            with self.subTest(key=key):
                self.assertFalse(evaluate('publish', {}, {**full, key: False})['allowed'])
        self.assertFalse(evaluate('publish', {}, {**full, 'owner_approved': 1})['allowed'])


if __name__ == '__main__':
    unittest.main()
