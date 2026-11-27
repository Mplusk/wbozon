"""Owner dashboard for the content factory; no secrets or mutations in this module."""

from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def h(value):
    return escape(str(value if value is not None else ""), quote=True)


def _text(value, fallback="Недоступно"):
    return h(value) if isinstance(value, str) and value.strip() else fallback


def _status(value):
    return {
        "active": "Работает",
        "inactive": "Остановлен",
        "not_configured": "Не подключено",
        "unavailable": "Нет связи",
        "unknown": "Неизвестно",
    }.get(value, "Неизвестно")


def _moscow_datetime(value):
    if not isinstance(value, str):
        return 'Недоступно'
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.utcoffset() is None:
            return 'Недоступно'
        return dt.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return 'Недоступно'


def _card(title, body):
    return '<section class="market-card"><h2>' + h(title) + '</h2>' + body + '</section>'


def render(snapshot=None, csrf=None):
    """Render trusted snapshot data; data absence is never represented as zero."""
    data = snapshot if isinstance(snapshot, dict) else {}
    workflow = data.get("workflow") if isinstance(data.get("workflow"), dict) else {}
    queue = data.get("queue") if isinstance(data.get("queue"), dict) else {}
    ideas = data.get("ideas") if isinstance(data.get("ideas"), dict) else {}
    budget = data.get("budget") if isinstance(data.get("budget"), dict) else {}
    raw_config = workflow.get("config")
    config = (raw_config if isinstance(raw_config, dict) and
              raw_config.get("status") in ("partial", "verified") else {})
    configured_models = config.get("models") if isinstance(config.get("models"), dict) else {}
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    state = _status(workflow.get("status", "unknown"))
    connection = {"ok": "доступен (отдельные данные могут быть недоступны)", "not_configured": "не настроено",
                  "unavailable": "нет связи"}.get(data.get("status"), "неизвестно")
    result = ('<main id="content"><p class="eyebrow">Контент и публикации</p>'
              '<h1>Контент-фабрика</h1><p class="intro">Управление подготовкой материалов, '
              'расписанием и публикациями из одного кабинета.</p>'
              '<p class="status">Состояние конвейера: <strong>' + state + '</strong>. '
              'Источник n8n: <strong>' + connection + '</strong>.</p>'
              '<div class="market-grid">')
    schedule_body = ('<p>Время указано в часовом поясе, полученном из настройки конвейера.</p><dl>'
                     '<dt>Создание контента</dt><dd>' + _text(config.get("generation_time")) + '</dd>'
                     '<dt>Публикация</dt><dd>Недоступно</dd>'
                     '<dt>Проверка очереди</dt><dd>' +
                     ('Каждые 5 минут' if config.get("poll_cron") == '*/5 * * * *' else 'Недоступно') +
                     '</dd><dt>Часовой пояс</dt><dd>' + _text(config.get("timezone")) + '</dd></dl>'
                     '<button type="button" disabled>Изменить расписание</button>'
                     '<p>Изменение расписания пока не подключено.</p>')
    result += _card("Расписание", schedule_body)
    account_rows = ''.join('<li>' + _text(item.get("platform")) + ' · ' + _text(item.get("name")) +
                           ' — ' + _text(item.get("status"), "статус неизвестен") + '</li>'
                           for item in accounts[:50] if isinstance(item, dict))
    accounts_body = (('<ul>' + account_rows + '</ul>') if account_rows else
                     '<p class="status">Список аккаунтов недоступен.</p>')
    accounts_body += ('<button type="button" disabled>Добавить аккаунт</button> '
                      '<button type="button" disabled>Управлять аккаунтами</button>'
                      '<p>Добавление, выбор и удаление аккаунтов пока не подключены.</p>')
    result += _card("Социальные сети", accounts_body)
    model_rows = (''.join('<li>' + h(label) + ': ' + _text(configured_models.get(key)) + '</li>'
                          for key, label in (("text", "Текст"), ("image", "Изображения"), ("video", "Видео")))
                  if any(isinstance(configured_models.get(key), str) and configured_models[key].strip()
                         for key in ("text", "image", "video")) else "")
    models_body = (('<ul>' + model_rows + '</ul>') if model_rows else
                   '<p class="status">Сведения о моделях недоступны.</p>')
    models_body += ('<button type="button" disabled>Выбрать модели</button> '
                    '<button type="button" disabled>Настроить ключи ИИ</button>'
                    '<p>Выбор моделей и ввод ключей через этот кабинет пока не подключены.</p>')
    result += _card("Модели и ключи ИИ", models_body)
    metric_labels = (("views", "Просмотры"), ("reach", "Охват"),
                     ("clicks", "Переходы"), ("orders", "Заказы"))
    metric_body = '<dl>'
    for key, label in metric_labels:
        value = metrics.get(key)
        shown = str(value) if type(value) is int and value >= 0 else 'Недоступно'
        metric_body += '<dt>' + h(label) + '</dt><dd>' + shown + '</dd>'
    metric_body += ('</dl><p>Метрики появятся после подключения их источников. '
                    'Переходы и заказы требуют отдельной настройки аналитики.</p>')
    result += _card("Результаты публикаций", metric_body)
    def rows_for(block):
        rows = block.get("items") if block.get("status") == "available" else None
        return [item for item in rows[:50] if isinstance(item, dict)] if isinstance(rows, list) else None
    queue_rows = rows_for(queue)
    if queue_rows is None:
        queue_body = '<p class="status">Очередь недоступна.</p>'
    elif not queue_rows:
        queue_body = ('<p class="status">На этой странице записей нет; есть следующие страницы.</p>'
                      if queue.get("has_more") is True else '<p class="status">В очереди нет записей.</p>')
    else:
        queue_body = '<ul>' + ''.join(
            '<li>' + _text(row.get("media_type")) + ' · ' +
            _moscow_datetime(row.get("scheduled_at")) + ' · ' + _text(row.get("status")) + '</li>'
            for row in queue_rows) + '</ul>'
        queue_body += '<p>Показаны последние записи, не более 50.' + (' Есть и другие записи.' if queue.get("has_more") is True else '') + '</p>'
    result += _card("Очередь публикаций", queue_body)
    idea_rows = rows_for(ideas)
    if idea_rows is None:
        idea_body = '<p class="status">Черновики недоступны.</p>'
    elif not idea_rows:
        idea_body = ('<p class="status">На этой странице записей нет; есть следующие страницы.</p>'
                     if ideas.get("has_more") is True else '<p class="status">Черновиков пока нет.</p>')
    else:
        idea_body = '<ul>' + ''.join(
            '<li>' + _text(row.get("title")) + ' · ' + _text(row.get("status")) + '</li>'
            for row in idea_rows) + '</ul>'
        idea_body += '<p>Показаны последние записи, не более 50.' + (' Есть и другие записи.' if ideas.get("has_more") is True else '') + '</p>'
    result += _card("Идеи и черновики", idea_body)
    monthly = budget.get("monthly_budget_usd") if budget.get("status") == "available" else None
    monthly_text = (h(monthly) + ' USD') if type(monthly) in (int, float) and 0 <= monthly < 1000000 else 'Недоступно'
    enabled = budget.get("enabled") if budget.get("status") == "available" else None
    enabled_text = ('включён' if enabled is True else 'выключен' if enabled is False else 'неизвестно')
    result += _card("Бюджет", '<p>Оценочный месячный лимит: <strong>' + monthly_text +
                    '</strong>.</p><p>Состояние ограничения: <strong>' + enabled_text +
                    '</strong>.</p><p>Это настройка конвейера; фактические списания уточняйте у поставщиков ИИ.</p>')
    connection_body = ('<p>Для чтения конвейера и таблиц нужен отдельный API-ключ n8n '
                       'только с разрешениями workflow:read и dataTableRow:read. '
                       'Это не ключ OpenAI или Gemini. Изменение настроек конвейера '
                       'через этот кабинет пока не подключено.</p>')
    if isinstance(csrf, str) and csrf:
        connection_body += ('<form class="save-form" method="post" '
                            'action="/settings/content-factory/connect" autocomplete="off">'
                            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
                            '<label for="content-n8n-key">API-ключ n8n</label>'
                            '<input id="content-n8n-key" type="password" name="api_key" '
                            'autocomplete="off" maxlength="2048" required>'
                            '<button type="submit">Сохранить ключ n8n</button></form>'
                            '<p>После сохранения значение ключа не отображается. Сохранить или '
                            'заменить ключ можно в течение пяти минут после входа по письму. '
                            'Затем потребуется войти снова. Ключ необходимо обновить до '
                            'истечения срока его действия в n8n.</p>')
    else:
        connection_body += '<p class="status">Для сохранения ключа требуется защищённый сеанс.</p>'
    result += _card("Подключение n8n", connection_body)
    result += ('</div><p>Для просмотра существующего конвейера откройте '
               '<a href="https://n8n.gamelife.shop/">n8n</a>.</p></main>')
    return result
