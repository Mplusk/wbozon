import unittest

import content_factory_budget_ui as budget_ui


class BudgetUiTests(unittest.TestCase):
    def test_closed_by_default_even_if_settings_and_csrf_supplied(self):
        page = budget_ui.render_budget_editor({'revision': 1, 'monthly_limit_cents': 1000}, 'csrf')
        self.assertIn('пока не подключено', page)
        self.assertNotIn('<form', page)

    def test_ready_form_uses_exact_revision_and_no_enforcement_claim(self):
        page = budget_ui.render_budget_editor({'revision': 3, 'monthly_limit_cents': 1000,
                                              'estimated_reserved_cents': None}, 'token<&', ready=True)
        self.assertIn('value="10.00"', page)
        self.assertIn('name="revision" value="3"', page)
        self.assertIn('token&lt;&amp;', page)
        self.assertIn('Фактические списания у поставщиков: неизвестно', page)
        self.assertIn('не включает ограничение автоматически', page)
        self.assertNotIn('включено', page)

    def test_bad_snapshots_fail_closed(self):
        for bad in ({'revision': True, 'monthly_limit_cents': 1000},
                    {'revision': 1, 'monthly_limit_cents': True},
                    {'revision': 1, 'monthly_limit_cents': -1},
                    {'revision': 1, 'monthly_limit_cents': 100_000_001}):
            with self.assertRaises(ValueError):
                budget_ui.render_budget_editor(bad, 'csrf', ready=True)

    def test_confirmation_binds_proposal_and_displays_exact_amounts(self):
        page = budget_ui.render_budget_confirmation('csrf', 'x' * 24, 1000, 1234)
        self.assertIn('10.00 → 12.34 USD', page)
        self.assertIn('не включает ограничение n8n автоматически', page)
        self.assertIn('name="proposal_id" value="' + 'x' * 24 + '"', page)
        with self.assertRaises(ValueError):
            budget_ui.render_budget_confirmation('csrf', '<script>', 1000, 1234)

    def test_live_unchecked_requires_refresh_and_live_confirmation_is_honest(self):
        page = budget_ui.render_budget_editor({'source':'n8n_live','revision':0,'monthly_limit_cents':None},'csrf',ready=True)
        self.assertIn('Текущий бюджет n8n ещё не проверен',page)
        self.assertIn('/settings/content-factory/budget/refresh',page)
        self.assertNotIn('name="monthly_limit_usd"',page)
        confirmation = budget_ui.render_budget_confirmation('csrf','x'*24,1000,1250,source='n8n_live')
        self.assertIn('Оценочный лимит n8n: 10.00 → 12.50 USD',confirmation)
        self.assertIn('не фактические списания',confirmation)


if __name__ == '__main__':
    unittest.main()
