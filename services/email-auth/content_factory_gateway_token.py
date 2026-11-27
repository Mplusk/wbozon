"""Narrow encrypted-vault slot for the separate n8n write API token.

The portal route is responsible for owner authentication, fresh login,
CSRF, origin checking, body limits and rate limits. Saving a token does not
verify scopes or enable the gateway.
"""
import html
import re

SLOT = 'n8n_content_factory_write'


def save(vault, key):
    if not isinstance(key, str) or not re.fullmatch(r'[!-~]{16,2048}', key):
        raise ValueError('Invalid token')
    vault.save(SLOT, {'api_key':key})


def render(csrf, present, *, budget_enabled=False, ai_enabled=True, cutover_enabled=False):
    if not isinstance(csrf, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', csrf):
        raise ValueError('Invalid session')
    status = 'сохранён' if present is True else 'не сохранён'
    return ('<form method="post" action="/settings/content-factory/ai/write-token" autocomplete="off">'
            '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
            '<label for="cf-n8n-write">Отдельный API-ключ n8n для управления — ' + status + '</label>'
            '<input id="cf-n8n-write" name="api_key" type="password" maxlength="2048" required autocomplete="off">'
            '<button type="submit">Сохранить ключ управления n8n</button></form>'
            + ('<p>Для изменения ключей ИИ нужны права <code>workflow:list</code>, <code>workflow:read</code>, '
            '<code>credential:read</code>, <code>credential:update</code> и, для создания отдельных credentials, '
            '<code>credential:create</code>. Эти разрешения могут распространяться на другие доступные '
            'этому владельцу сценарии и учётные данные n8n. Используйте ключ владельца только если понимаете этот объём доступа.</p>' if ai_enabled is True else '')
            + ('<p>Для управления бюджетом дополнительно нужны права <code>dataTableRow:read</code> '
               'и <code>dataTableRow:update</code>; они также могут распространяться на другие доступные таблицы.</p>' if budget_enabled is True else '') +
            ('<p>Для подключения сценария дополнительно нужны <code>workflow:update</code>, '
             '<code>workflow:activate</code>, <code>workflow:deactivate</code> и <code>execution:list</code>. '
             'Эти права могут действовать на другие доступные сценарии, '
             'поэтому используйте отдельный ключ и подтвердите объём доступа.</p>' if cutover_enabled is True else '') +
            '<p>Выданные права могут распространяться на другие доступные владельцу проекты n8n.</p>'
            '<p>Введите ключ самостоятельно. Сохранение не включает шлюз и не проверяет права. '
            'Ключи не отображаются после сохранения.</p>')
