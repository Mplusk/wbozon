import unittest

import content_factory_ui


class ContentFactoryUiTests(unittest.TestCase):
    def test_unknown_data_is_not_reported_as_zero_or_connected(self):
        page = content_factory_ui.render()
        self.assertIn("Источник n8n: <strong>неизвестно</strong>", page)
        self.assertIn("Очередь недоступна", page)
        self.assertIn("Черновики недоступны", page)
        self.assertIn("<dt>Заказы</dt><dd>Недоступно</dd>", page)
        self.assertIn('type="button" disabled', page)
        self.assertNotIn("<form", page)

    def test_live_snapshot_is_escaped_and_zero_is_not_missing(self):
        page = content_factory_ui.render({
            "status": "ok",
            "workflow": {"status": "active"},
            "queue": {"status": "available", "items": [
                {"media_type": "image", "scheduled_at": "<soon>", "status": "ready"}]},
            "ideas": {"status": "available", "items": [{"title": "<script>alert(1)</script>"}]},
            "budget": {"status": "available", "monthly_budget_usd": 10},
            "metrics": {"views": 0},
        })
        self.assertIn("Источник n8n: <strong>доступен (отдельные данные могут быть недоступны)</strong>", page)
        self.assertNotIn("&lt;soon&gt;", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("<dt>Просмотры</dt><dd>0</dd>", page)
        self.assertIn("<dt>Заказы</dt><dd>Недоступно</dd>", page)
        self.assertIn("10 USD", page)
        self.assertIn('Настройка бюджета в n8n: <strong>неизвестно</strong>', page)

    def test_queue_dates_are_converted_to_moscow_and_invalid_values_hidden(self):
        page = content_factory_ui.render({"queue": {"status": "available", "items": [
            {"scheduled_at": "2026-11-27T09:00:00.000Z"},
            {"scheduled_at": "2026-11-27T09:00:00"},
            {"scheduled_at": "<script>"}]}})
        self.assertIn('27.11.2026 12:00 МСК', page)
        self.assertEqual(page.count(' · Недоступно · '), 2)
        self.assertNotIn('<script>', page)

    def test_budget_state_is_not_inferred_from_amount(self):
        for enabled, label in ((True, 'включён'), (False, 'выключен'), (None, 'неизвестно'), ('true', 'неизвестно')):
            page = content_factory_ui.render({"budget": {"status": "available", "monthly_budget_usd": 10, "enabled": enabled}})
            self.assertIn('Настройка бюджета в n8n: <strong>' + label + '</strong>', page)
            self.assertIn('Проверенное ограничение фактических', page)

    def test_unavailable_tables_do_not_display_old_items(self):
        page = content_factory_ui.render({"queue": {"status": "unavailable",
                                                   "items": [{"status": "ready"}]}})
        self.assertIn("Очередь недоступна", page)
        self.assertNotIn("ready", page)

    def test_connection_form_has_csrf_and_never_inserts_stored_key(self):
        page = content_factory_ui.render(
            {"api_key": "a-secret-token", "status": "ok"}, csrf='abc"><script>')
        self.assertIn('action="/settings/content-factory/connect"', page)
        self.assertIn('name="csrf" value="abc&quot;&gt;&lt;script&gt;"', page)
        self.assertIn('type="password" name="api_key"', page)
        self.assertIn('autocomplete="off"', page)
        self.assertIn('workflow:read', page)
        self.assertIn('dataTableRow:read', page)
        self.assertIn('пяти минут после входа', page)
        self.assertNotIn('a-secret-token', page)
        self.assertNotIn('<script>', page)

    def test_ai_drafts_and_active_models_are_separate_without_key_storage(self):
        page = content_factory_ui.render({
            'workflow': {'config': {'status': 'verified', 'models': {'text': 'gpt-current'}}},
            'ai_settings': {'text_model': 'gpt-test', 'image_model': '<bad>', 'video_model': ''},
            'api_key': 'never-print-this',
        }, csrf='safe-token')
        self.assertIn('action="/settings/content-factory/models"', page)
        self.assertNotIn('action="/settings/content-factory/ai-key"', page)
        self.assertIn('value="gpt-test"', page)
        self.assertIn('<option value="gpt-current"></option>', page)
        self.assertIn('value="&lt;bad&gt;"', page)
        self.assertIn('Настройка ключей через этот кабинет пока не подключена', page)
        self.assertIn('конвейер пока не использует', page)
        self.assertNotIn('never-print-this', page)
        self.assertEqual(page.count('name="csrf" value="safe-token"'), 2)

    def test_only_verified_or_partial_published_config_is_rendered(self):
        config = {"status": "partial", "generation_time": "10:00", "timezone": "Europe/Moscow",
                  "poll_cron": "*/5 * * * *",
                  "models": {"text": "gpt-model", "image": "<model>", "video": None}}
        page = content_factory_ui.render({"workflow": {"config": config},
                                          "queue": {"status": "available", "items": [
                                              {"status": "ready"}], "has_more": True},
                                          "ideas": {"status": "available", "items": [
                                              {"title": "Тест"}], "has_more": True}})
        self.assertIn("<dt>Создание контента</dt><dd>10:00</dd>", page)
        self.assertIn("<dt>Публикация</dt><dd>Недоступно</dd>", page)
        self.assertIn("Каждые 5 минут", page)
        self.assertIn("gpt-model", page)
        self.assertIn("&lt;model&gt;", page)
        self.assertEqual(page.count("Есть и другие записи"), 2)
        config["status"] = "unavailable"
        page = content_factory_ui.render({"workflow": {"config": config}})
        self.assertNotIn("10:00", page)
        self.assertNotIn("gpt-model", page)

    def test_budget_only_gateway_is_described_as_budget_control(self):
        page=content_factory_ui.render({},csrf='c'*32,write_token_setup=True,budget_gateway_configured=True)
        self.assertIn('оценочный бюджет фабрики',page)
        self.assertIn('dataTableRow:update',page)
        self.assertIn('не гарантирует остановку списаний',page)
        self.assertNotIn('credential:update',page)
        self.assertIn('Включить управление фабрикой',page)


if __name__ == "__main__":
    unittest.main()
