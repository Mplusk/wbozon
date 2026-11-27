"""Presentation of an enabled AI gateway. No secrets are retained here.

Callers must verify authentication, CSRF, freshness, proposal integrity and
gateway availability. Rendering this module grants no authority to write.
"""
import html
import re


def _h(value):
    return html.escape(str(value), quote=True)


def _proposal(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', value):
        raise ValueError('Invalid proposal')
    return _h(value)


def _csrf(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Missing CSRF')
    return _h(value)


def render_controls(csrf, models, available_models, *, cost_control_ready=False):
    token = _csrf(csrf)
    models = models if isinstance(models, dict) else {}
    if not isinstance(available_models, dict) or set(available_models) != {'text', 'image'}:
        raise ValueError('Missing model catalog')
    result = ('<details class="details"><summary>Выбрать модели</summary>'
              '<p>Изменение применяется после отдельного подтверждения. '
              'Выбор видеомодели пока не подключён.</p>'
              '<p>Цены выбранных моделей здесь не проверяются; оценки не '
              'гарантируют лимит списаний поставщика.</p>' +
              ('' if cost_control_ready is True else '<p>Применение моделей недоступно до обеспечения контроля расходов.</p>') +
              '<form class="save-form" method="post" action="/settings/content-factory/ai/models/prepare">'
              '<input type="hidden" name="csrf" value="' + token + '">')
    for field, label, current in (('text_model', 'Модель текста', 'text'),
                                  ('image_model', 'Модель изображений', 'image')):
        value = models.get(current)
        value = value if isinstance(value, str) and len(value) <= 80 else ''
        options = available_models[current]
        if not isinstance(options, (list, tuple)) or not options or len(options) > 100:
            raise ValueError('Invalid model catalog')
        if any(not isinstance(option, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}', option)
               or any(part.lower().startswith(('sk-', 'aiza')) for part in option.split('/')) for option in options):
            raise ValueError('Invalid model catalog')
        result += ('<label for="ai-' + field + '">' + label + '</label>'
                   '<select id="ai-' + field + '" name="' + field + '" required>')
        if value not in options:
            result += '<option value="" selected>Выберите поддерживаемую модель</option>'
        for option in options:
            result += ('<option value="' + _h(option) + '"' +
                       (' selected' if option == value else '') + '>' + _h(option) + '</option>')
        result += '</select>'
    result += ('<button type="submit"' + ('' if cost_control_ready is True else ' disabled') +
               '>Проверить изменения</button></form></details>')
    return result + render_credentials(csrf)


def render_credentials(csrf):
    token = _csrf(csrf)
    result = ('<details class="details"><summary>Настроить ключи ИИ</summary>'
               '<p>Для фабрики используются отдельные учётные данные n8n. '
               'После подтверждения вы введёте ключ в защищённую форму.</p>')
    for provider, label in (('openai', 'OpenAI'), ('gemini', 'Google Gemini')):
        result += ('<form class="save-form" method="post" action="/settings/content-factory/ai/credentials/prepare">'
                   '<input type="hidden" name="csrf" value="' + token + '">'
                   '<input type="hidden" name="provider" value="' + provider + '">'
                   '<button type="submit">Настроить ключ ' + label + '</button></form>')
    result += ('<p>Если отдельные учётные данные фабрики ещё не созданы, создайте их ниже. '
               'Создание само по себе не подключает их к сценарию n8n.</p>')
    for provider, label in (('openai', 'OpenAI'), ('gemini', 'Google Gemini')):
        result += ('<form class="save-form" method="post" action="/settings/content-factory/ai/credentials/provision/prepare">'
                   '<input type="hidden" name="csrf" value="' + token + '">'
                   '<input type="hidden" name="provider" value="' + provider + '">'
                   '<button type="submit">Создать отдельные credentials ' + label + '</button></form>')
    return result + '</details>'


def render_model_confirmation(csrf, proposal, changes, *, revision=None):
    token, proposal_id = _csrf(csrf), _proposal(proposal)
    if not isinstance(changes, dict) or set(changes) != {'text', 'image'}:
        raise ValueError('Invalid changes')
    result = ('<h1>Подтвердите модели</h1><p>Будет сохранена конфигурация фабрики. '
              'Сценарий n8n начнёт её читать только после отдельного подключения; '
              'стоимость вызовов зависит от поставщика и является оценочной.</p><dl>')
    for field, label in (('text', 'Текст'), ('image', 'Изображения')):
        change = changes[field]
        if not isinstance(change, dict) or set(change) != {'before', 'after'}:
            raise ValueError('Invalid change')
        if any(not isinstance(change[k], str) or len(change[k]) > 80 for k in ('before', 'after')):
            raise ValueError('Invalid model')
        if any(any(part.lower().startswith(('sk-', 'aiza')) for part in change[k].split('/')) for k in ('before', 'after')):
            raise ValueError('Invalid model')
        result += ('<dt>' + label + '</dt><dd>' + _h(change['before']) +
                   ' → ' + _h(change['after']) + '</dd>')
    if revision is not None and (type(revision) is not int or revision < 0): raise ValueError('Invalid revision')
    fields = '' if revision is None else ('<input type="hidden" name="revision" value="' + str(revision) + '">'
             '<input type="hidden" name="text_model" value="' + _h(changes['text']['after']) + '">'
             '<input type="hidden" name="image_model" value="' + _h(changes['image']['after']) + '">')
    return (result + '</dl><form method="post" action="/settings/content-factory/ai/models/confirm">'
            '<input type="hidden" name="csrf" value="' + token + '">'
            '<input type="hidden" name="proposal_id" value="' + proposal_id + '">'
            + fields + '<label><input type="checkbox" name="ack_estimate" value="understood" required> '
              'Подтверждаю: стоимость и совместимость не проверены, лимит оценочный</label>'
              '<button type="submit">Подтвердить изменение моделей</button></form>')


def render_key_confirmation(csrf, proposal, provider, *, provision=False):
    token, proposal_id = _csrf(csrf), _proposal(proposal)
    labels = {'openai': 'OpenAI', 'gemini': 'Google Gemini'}
    if provider not in labels:
        raise ValueError('Invalid provider')
    action = '/settings/content-factory/ai/credentials/provision/commit' if provision else '/settings/content-factory/ai/credentials/commit'
    title = 'создание учётных данных ' if provision else 'замену ключа '
    return ('<h1>Подтвердите ' + title + labels[provider] + '</h1>'
            '<p>Ключ будет передан в отдельные учётные данные этой фабрики в n8n. '
            'После отправки значение не отображается. Созданные учётные данные будут использоваться '
            'только после подключения к сценарию. Не редактируйте одновременно этот сценарий '
            'или его credentials в n8n: изменения могут повлиять друг на друга.</p>'
            '<form method="post" action="' + action + '" autocomplete="off">'
            '<input type="hidden" name="csrf" value="' + token + '">'
            '<input type="hidden" name="proposal_id" value="' + proposal_id + '">'
            '<input type="hidden" name="provider" value="' + provider + '">'
            '<label for="new-ai-key">Новый API-ключ ' + labels[provider] + '</label>'
            '<input type="password" id="new-ai-key" name="api_key" maxlength="2048" autocomplete="off" required>'
            '<button type="submit">Подтвердить и сохранить ключ</button></form>')


def render_reader_confirmation(csrf, proposal):
    return ('<h1>Подтвердите передачу ключа в n8n</h1>'
            '<p>Созданный сайтом секретный ключ чтения будет передан в отдельные credentials n8n. '
            'Сценарий начнёт использовать их только после отдельного подключения. '
            'Не редактируйте одновременно этот сценарий и его credentials в n8n.</p>'
            '<form method="post" action="/settings/content-factory/ai/reader-credential/confirm">'
            '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<button type="submit">Подтвердить передачу в n8n</button></form>')


def render_result(outcome, csrf=None, proposal=None):
    """Render only a server-verified outcome; unknown must never imply failure."""
    messages = {
        'verified': 'Настройка применена; состояние подтверждено проверкой n8n.',
        'accepted_unconfirmed': ('n8n принял изменение учётных данных. '
                                 'Работоспособность ключа и его точное значение не проверены.'),
        'rejected_before_send': 'Изменение не отправлено в n8n.',
        'unknown': ('Результат изменения неизвестен. Не отправляйте повторно '
                    'до сверки состояния n8n.'),
    }
    if outcome not in messages:
        raise ValueError('Unknown gateway outcome')
    text = '<h1>Результат изменения</h1><p>' + messages[outcome] + '</p>'
    if outcome == 'unknown' and csrf is not None and proposal is not None:
        text += ('<form method="post" action="/settings/content-factory/ai/reconcile/inspect">'
                 '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
                 '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
                 '<button type="submit">Остановить шлюз и проверить состояние</button></form>')
    return text


def render_reconciliation(csrf, proposal, observation):
    if observation != 'credential_exists_unverified': raise ValueError('Unsupported observation')
    return ('<h1>Сверка результата</h1><p>n8n показывает, что учётные данные существуют. '
            'Сохранённое значение ключа и работоспособность не проверены. Предыдущий запрос мог '
            'продолжить выполняться после ошибки связи.</p>'
            '<p>Подтверждение фиксирует, что вы ознакомились с результатом; шлюз останется остановлен. '
            'Не повторяйте изменение, пока причина не выяснена.</p>'
            '<form method="post" action="/settings/content-factory/ai/reconcile/confirm">'
            '<input type="hidden" name="csrf" value="' + _csrf(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<label><input type="checkbox" name="risk" value="understood" required> Я понимаю, что '
            'значение ключа неизвестно и предыдущий запрос может завершиться позднее</label>'
            '<button type="submit">Подтвердить ознакомление</button></form>')
