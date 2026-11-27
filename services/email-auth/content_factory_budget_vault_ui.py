"""Owner-confirmed budget-only opt-in; never renders token values."""
import html
import re


def render(csrf,enabled):
    if not isinstance(csrf,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf): raise ValueError('Invalid session')
    token=html.escape(csrf,quote=True)
    if enabled is True:
        return ('<section class="market-card"><h2>Подключение бюджета</h2>'
                '<p>Настройка сохранена. Остановка блокирует новые изменения; запрос, '
                'уже отправленный в n8n, может завершиться позднее.</p>'
                '<form method="post" action="/settings/content-factory/budget/vault-disable">'
                '<input type="hidden" name="csrf" value="'+token+'">'
                '<button type="submit">Отключить управление бюджетом</button></form></section>')
    return ('<section class="market-card"><h2>Подключить управление бюджетом</h2>'
            '<p>Введите отдельный API-ключ n8n с правами dataTableRow:read и dataTableRow:update. '
            'Эти права могут распространяться на другие таблицы, доступные владельцу ключа.</p>'
            '<p>Бюджет n8n является оценочным и не гарантирует остановку списаний поставщиков ИИ.</p>'
            '<form method="post" action="/settings/content-factory/budget/vault-enable" autocomplete="off">'
            '<input type="hidden" name="csrf" value="'+token+'">'
            '<label for="budget-only-key">Отдельный API-ключ n8n</label>'
            '<input id="budget-only-key" type="password" name="api_key" maxlength="2048" required autocomplete="off">'
            '<label><input type="checkbox" name="ack_scope" value="understood" required> '
            'Понимаю объём прав ключа</label>'
            '<label><input type="checkbox" name="ack_estimate" value="understood" required> '
            'Понимаю, что бюджет оценочный</label>'
            '<button type="submit">Сохранить ключ и подключить бюджет</button></form></section>')
