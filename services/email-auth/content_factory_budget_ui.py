"""Budget editor presentation only; no routes or budget enforcement.

The caller must authenticate the owner, validate Origin/CSRF, parse the amount
as decimal cents and use an audited, one-use proposal bound to the revision.
Never render this editor as a connected control until its backend is verified.
"""
import html
import re

MAX_CENTS = 100_000_000


def _esc(value):
    return html.escape(str(value), quote=True)


def _money(value):
    if type(value) is not int or not 0 <= value <= MAX_CENTS:
        raise ValueError('invalid amount')
    return f'{value // 100}.{value % 100:02d}'


def _csrf(value):
    if not isinstance(value, str) or not value:
        raise ValueError('missing CSRF')
    return _esc(value)


def render_budget_editor(settings, csrf=None, *, ready=False):
    """Render a draft budget form only if its audited backend is ready.

    ``settings`` is a trusted local settings snapshot, separate from n8n's
    published budget. ``can_run`` does not establish an applied n8n limit.
    """
    if not ready:
        return ('<p>Изменение бюджета через кабинет пока не подключено. '
                'Действующую настройку n8n и её статус смотрите выше.</p>')
    if not isinstance(settings, dict):
        raise ValueError('invalid settings')
    revision = settings.get('revision')
    limit = settings.get('monthly_limit_cents')
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid revision')
    live = settings.get('source') == 'n8n_live'
    refresh = ('<form method="post" action="/settings/content-factory/budget/refresh">'
               '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
               '<button type="submit">Проверить бюджет n8n</button></form>') if live else ''
    if live and limit is None:
        return ('<p>Текущий бюджет n8n ещё не проверен.</p>' + refresh)
    amount = _money(limit)
    reserved = settings.get('estimated_reserved_cents')
    reserved_text = _money(reserved) + ' USD' if type(reserved) is int and 0 <= reserved <= MAX_CENTS else 'неизвестно'
    explanation = ('<p>Будет изменён оценочный месячный лимит n8n. '
                   'Показано значение последней проверки, оно может устареть. '
                   'Это не ограничение фактических списаний поставщиков.</p>' + refresh if live else
                   '<p>Значение сохраняется как настройка кабинета. Само по себе оно не включает '
                   'проверенное ограничение расходов в n8n.</p>')
    return ('<details class="details"><summary>Изменить месячный бюджет</summary>'
            + explanation +
            '<p>Оценка зарезервированных расходов: ' + reserved_text + '. '
            'Фактические списания у поставщиков: неизвестно.</p>'
            '<form method="post" action="/settings/content-factory/budget/prepare" autocomplete="off">'
            '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
            '<input type="hidden" name="revision" value="' + str(revision) + '">'
            '<label for="budget-usd">Месячный бюджет, USD</label>'
            '<input type="text" inputmode="decimal" id="budget-usd" name="monthly_limit_usd" '
            'maxlength="16" value="' + amount + '" required>'
            '<p>Например: 10.00. Изменение не включает ограничение автоматически.</p>'
            '<button type="submit">Проверить изменение</button></form></details>')


def render_budget_confirmation(csrf, proposal_id, before_cents, after_cents, *, source=None):
    if not isinstance(proposal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', proposal_id):
        raise ValueError('invalid proposal')
    before, after = _money(before_cents), _money(after_cents)
    explanation = ('<p>Оценочный лимит n8n: ' + before + ' → ' + after + ' USD.</p>'
                   '<p>Лимит учитывает оценку, а не фактические списания поставщиков. '
                   'Статус включения не изменится.</p>' if source == 'n8n_live' else
                   '<p>Настройка кабинета: ' + before + ' → ' + after + ' USD.</p>'
                   '<p>Сохранение не включает ограничение n8n автоматически. '
                   'Оценка расходов не является суммой фактических списаний.</p>')
    return ('<h1>Подтвердите месячный бюджет</h1>' + explanation +
            '<form method="post" action="/settings/content-factory/budget/confirm">'
            '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _esc(proposal_id) + '">'
            '<button type="submit">Подтвердить бюджет</button></form>')
