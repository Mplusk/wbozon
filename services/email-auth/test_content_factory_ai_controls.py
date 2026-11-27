import unittest

import content_factory_ai_controls as controls


class AiControlUiTests(unittest.TestCase):
    def test_first_step_never_accepts_secrets(self):
        page = controls.render_controls('token', {'text': '<script>', 'image': 'models/example'},
                                        {'text': ['gpt-model'], 'image': ['models/example']})
        self.assertNotIn('<script>', page)
        self.assertIn('Выберите поддерживаемую модель', page)
        self.assertNotIn('name="api_key"', page)
        self.assertIn('/ai/models/prepare', page)
        self.assertIn('/ai/credentials/prepare', page)
        self.assertIn('видеомодели пока не подключён', page)
        self.assertIn('<option value="models/example" selected>', page)
        self.assertIn('Цены выбранных моделей здесь не проверяются', page)
        self.assertIn('Применение моделей недоступно до обеспечения контроля расходов', page)
        self.assertIn('<button type="submit" disabled>Проверить изменения', page)
        ready = controls.render_controls('token', {}, {'text': ['gpt-model'], 'image': ['models/example']},
                                         cost_control_ready=True)
        self.assertIn('<button type="submit">Проверить изменения', ready)
        self.assertNotIn('Применение моделей недоступно', ready)
        with self.assertRaises(ValueError):
            controls.render_controls('token', {}, {'text': ['<bad>'], 'image': ['models/example']})
        with self.assertRaises(ValueError):
            controls.render_controls('token', {}, {'text': ['gpt-model'], 'image': ['models/aIZaFakeKey']})

    def test_model_confirmation_displays_exact_escaped_delta(self):
        page = controls.render_model_confirmation('token', 'x' * 32, {
            'text': {'before': 'old', 'after': '<new>'},
            'image': {'before': 'a', 'after': 'b'},
        })
        self.assertIn('old → &lt;new&gt;', page)
        self.assertNotIn('<new>', page)
        self.assertNotIn('name="api_key"', page)
        self.assertIn('name="proposal_id"', page)
        with self.assertRaises(ValueError):
            controls.render_model_confirmation('token', 'x', {})
        with self.assertRaises(ValueError):
            controls.render_model_confirmation('token', 'x' * 32, {
                'text': {'before': 'old', 'after': 'sk-fakekey'},
                'image': {'before': 'a', 'after': 'b'},
            })

    def test_key_entry_after_confirmation_has_no_value_and_rejects_invalid_provider(self):
        page = controls.render_key_confirmation('token', 'x' * 32, 'openai')
        self.assertIn('type="password"', page)
        self.assertIn('name="api_key"', page)
        self.assertNotIn('name="api_key" value=', page)
        self.assertIn('name="provider" value="openai"', page)
        with self.assertRaises(ValueError):
            controls.render_key_confirmation('token', 'x' * 32, '<script>')
        with self.assertRaises(ValueError):
            controls.render_key_confirmation('', 'x' * 32, 'gemini')

    def test_result_never_represents_unknown_as_success_or_failure(self):
        page = controls.render_result('unknown')
        self.assertIn('неизвестен', page)
        self.assertIn('Не отправляйте повторно', page)
        self.assertNotIn('применена', page)
        with self.assertRaises(ValueError):
            controls.render_result('success')


if __name__ == '__main__':
    unittest.main()
