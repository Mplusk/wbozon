"""Owner dashboard for the content factory; secret values are never rendered."""

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


def render(snapshot=None, csrf=None, *, ai_gateway_ready=False, budget_gateway_ready=False, budget_settings=None, write_token_setup=False, write_token_present=False, budget_gateway_configured=False, ai_gateway_configured=False, model_config_enabled=False, cutover_configured=False, budget_vault_feature=False, budget_vault_enabled=False):
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
    models_body += ('<p>Выше показаны модели действующего сценария n8n. '
                    'Настройки ниже сохраняются в кабинете как черновик: '
                    'конвейер пока не использует их.</p>')
    settings = data.get("ai_settings") if isinstance(data.get("ai_settings"), dict) else {}
    if isinstance(csrf, str) and csrf:
        token = h(csrf)
        models_body += ('<details class="details"><summary>Выбрать модели</summary>'
                        '<form class="save-form" method="post" action="/settings/content-factory/models">'
                        '<input type="hidden" name="csrf" value="' + token + '">')
        for field, label, current in (("text_model", "Модель текста", "text"),
                                      ("image_model", "Модель изображений", "image"),
                                      ("video_model", "Модель видео", "video")):
            value = settings.get(field) if isinstance(settings.get(field), str) else ''
            required = ' required' if field != 'video_model' else ''
            active_value = configured_models.get(current)
            options = ('<option value="' + h(active_value) + '"></option>'
                       if isinstance(active_value, str) and active_value.strip() else '')
            models_body += ('<label for="ai-' + field + '">' + label + '</label>'
                            '<input type="text" id="ai-' + field + '" name="' + field +
                            '" value="' + h(value) + '" maxlength="80" '
                            'placeholder="ID модели" list="ai-options-' + field + '"' + required + '>'
                            '<datalist id="ai-options-' + field + '">' + options + '</datalist>')
        models_body += ('<p>Выберите текущую модель из подсказки или введите идентификатор из кабинета поставщика. '
                        'Поле видео можно оставить пустым.</p>'
                        '<button type="submit">Сохранить черновик моделей</button></form></details>')
    else:
        models_body += '<p>Для сохранения настроек требуется защищённый сеанс.</p>'
    if ai_gateway_ready is True and isinstance(csrf, str) and csrf:
        import content_factory_ai_controls
        models_body += content_factory_ai_controls.render_credentials(csrf)
    else:
        models_body += ('<details class="details"><summary>Настроить ключи ИИ</summary>'
                        '<p>Ключи OpenAI и Gemini для действующего конвейера настраиваются в '
                        '<a href="https://n8n.gamelife.shop/">n8n</a> в разделе Credentials. '
                        'Настройка ключей через этот кабинет пока не подключена.</p></details>')
    if write_token_setup and isinstance(csrf, str) and csrf:
        if not budget_vault_feature:
            import content_factory_gateway_token
            models_body += content_factory_gateway_token.render(csrf, write_token_present, budget_enabled=budget_gateway_configured, ai_enabled=ai_gateway_configured, cutover_enabled=cutover_configured)
        scope = ('выделенные учётные данные и оценочный бюджет фабрики' if ai_gateway_configured and budget_gateway_configured else
                 'оценочный бюджет фабрики' if budget_gateway_configured else 'выделенные учётные данные фабрики')
        models_body += ('<p>Шлюз позволяет изменять ' + scope + '. '
                        'Бюджет n8n является оценочным и не гарантирует остановку списаний поставщика. '
                        'При ошибке или неопределённом ответе изменения блокируются до сверки. '
                        'Не редактируйте одновременно этот сценарий и его credentials в n8n: '
                        'изменения могут перезаписать друг друга или попасть в другую версию.</p>'
                        '<form method="post" action="/settings/content-factory/ai/gateway">'
                        '<input type="hidden" name="csrf" value="' + h(csrf) + '">' +
                        ('<p>После перезапуска процесса кабинета управление может автоматически остановиться: перед повторным включением проверьте состояние n8n. '
                         'После восстановления данных или сбоя остановите операции, проверьте последнее изменение в n8n и дождитесь завершения прежнего запроса. '
                         'Если результат неизвестен, не включайте управление до выяснения. Откат данных автоматически не обнаруживается.</p>'
                         '<label><input type="checkbox" name="ack_recovery" value="understood"> '
                         'Я проверил состояние n8n и подтверждаю, что нет незавершённого или невыясненного изменения</label>' if budget_vault_feature else '') +
                        '<button name="action" value="arm" type="submit">Включить управление фабрикой</button> '
                        '<button name="action" value="halt" type="submit">Остановить управление фабрикой</button></form>')
    if model_config_enabled is True and isinstance(csrf, str) and csrf:
        models_body += ('<details><summary>Ключ чтения настроек моделей для n8n</summary>'
                        '<p>Сайт создаст отдельный случайный ключ и сохранит его в защищённом хранилище. '
                        'Сценарий n8n сможет читать конфигурацию только после отдельного подключения.</p>'
                        '<form method="post" action="/settings/content-factory/ai/reader-token" autocomplete="off">'
                        '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
                        '<button type="submit">Создать ключ чтения</button></form></details>')
        if ai_gateway_ready is True:
            models_body += ('<form method="post" action="/settings/content-factory/ai/reader-credential/prepare">'
                            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
                            '<button type="submit">Подключить ключ чтения к n8n</button></form>')
            import content_factory_model_config
            models_body += ('<details><summary>Сохранить конфигурацию моделей</summary>'
                            '<p>Конфигурация будет доступна сценарию только после его подключения. '
                            'Для введённых моделей совместимость и цена автоматически не проверяются. '
                            'Указанный лимит является оценочным и не гарантирует остановку списаний поставщика.</p>'
                            '<form method="post" action="/settings/content-factory/ai/models/prepare">'
                            '<input type="hidden" name="csrf" value="' + h(csrf) + '">')
            for field,label in (('text','Модель текста'),('image','Модель изображения')):
                models_body += '<label for="published-' + field + '">' + label + '</label><input id="published-' + field + '" name="' + field + '_model" list="published-options-' + field + '" maxlength="80" required><datalist id="published-options-' + field + '">'
                for value in sorted(content_factory_model_config.ALLOWED[field]):
                    models_body += '<option value="' + h(value) + '"></option>'
                models_body += '</datalist>'
            models_body += ('<label><input type="checkbox" name="ack_estimate" value="understood" required> '
                            'Понимаю, что цена и совместимость не проверяются, а лимит расходов оценочный</label>'
                            '<button type="submit">Проверить настройки</button></form></details>')
            if cutover_configured is True:
                models_body += ('<form method="post" action="/settings/content-factory/ai/cutover/pause/prepare">'
                                '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
                                '<button type="submit">Подготовить подключение и остановку фабрики</button></form>')
    result += _card("Модели и ключи ИИ", models_body)
    if budget_vault_feature is True and isinstance(csrf,str) and csrf:
        import content_factory_budget_vault_ui
        result += content_factory_budget_vault_ui.render(csrf,budget_vault_enabled)
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
    import content_factory_budget_ui
    budget_editor = content_factory_budget_ui.render_budget_editor(budget_settings, csrf, ready=budget_gateway_ready)
    result += _card("Бюджет", '<p>Оценочный месячный бюджет n8n: <strong>' + monthly_text +
                    '</strong>.</p><p>Настройка бюджета в n8n: <strong>' + enabled_text +
                    '</strong>.</p><p>Расчёт основан на оценках. Проверенное ограничение фактических '
                    'списаний не подключено; расходы уточняйте у поставщиков ИИ.</p>' + budget_editor)
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
