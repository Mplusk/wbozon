"""Owner-facing confirmation for the narrow factory workflow migration."""
import html
import json
import re


def h(value): return html.escape(str(value),quote=True)


def _proposal(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',value): raise ValueError()
    return h(value)


def _receipt(value):
    raw=json.dumps(value,separators=(',',':'),sort_keys=True)
    if len(raw)>4096: raise ValueError()
    return h(raw)


def pause_preview(csrf, proposal, version):
    return ('<h1>Подтвердите временную остановку</h1><p>Сценарий фабрики версии ' + h(version) +
            ' будет остановлен: новые запуски по расписанию прекратятся. Другие сценарии не изменятся. '
            'После завершения настройки проверенную версию можно опубликовать отдельно.</p>'
            '<form method="post" action="/settings/content-factory/ai/cutover/pause/confirm">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<label><input type="checkbox" name="ack_pause" value="understood" required> '
            'Не буду вручную запускать, редактировать или публиковать фабрику в n8n до окончания настройки</label>'
            '<button type="submit">Временно остановить фабрику</button></form>')


def pause_done(csrf,receipt):
    return ('<h1>Фабрика остановлена</h1><p>Версия ' + h(receipt.get('version')) +
            ' сохранена в истории. Не запускайте и не редактируйте сценарий вручную до завершения. '
            'При наличии ожидающих выполнений следующий шаг будет отклонён до их завершения.</p>'
            '<form method="post" action="/settings/content-factory/ai/cutover/draft/prepare">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="pause_receipt" value="' + _receipt(receipt) + '">'
            '<button type="submit">Проверить изменения сценария</button></form>'
            '<form method="post" action="/settings/content-factory/ai/cutover/restore/prepare">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="receipt" value="' + _receipt(receipt) + '">'
            '<button type="submit">Проверить возврат прежней версии</button></form>')


def restore_preview(csrf,proposal,receipt):
    return ('<h1>Вернуть прежнюю версию?</h1><p>Будет опубликована версия ' + h(receipt.get('version')) +
            '. Проверка состояния сценария выполнится повторно. Данные, обработанные после остановки, не откатываются.</p>'
            '<form method="post" action="/settings/content-factory/ai/cutover/restore/confirm">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<input type="hidden" name="receipt" value="' + _receipt(receipt) + '">'
            '<button type="submit">Подтвердить возврат</button></form>')


def draft_preview(csrf, proposal, version, changes, pause_receipt):
    if not isinstance(changes,list) or len(changes)>20: raise ValueError()
    items=''.join('<li>' + h(c.get('node_id')) + ': ' + h(c.get('provider')) +
                  (' — ' + h(c.get('old_id')) + ' → ' + h(c.get('new_id')) if 'old_id' in c else '') + '</li>'
                  for c in changes if isinstance(c,dict))
    return ('<h1>Проверка изменений сценария</h1><p>Текущая версия: ' + h(version) + '. '
            'Изменятся только перечисленные узлы, подключения двух триггеров и параметры двух моделей. '
            'Новая версия будет сохранена как черновик; фабрика останется остановлена до публикации.</p>'
            '<p>Не редактируйте этот сценарий и его credentials в n8n одновременно с настройкой: '
            'изменения могут перезаписать друг друга. Стоимость моделей не проверена автоматически.</p><ul>' + items + '</ul>'
            '<form method="post" action="/settings/content-factory/ai/cutover/draft/confirm">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<input type="hidden" name="pause_receipt" value="' + _receipt(pause_receipt) + '">'
            '<button type="submit">Подтвердить сохранение черновика</button></form>')


def draft_saved(csrf, receipt):
    return ('<h1>Черновик сохранён</h1><p>Сценарий остаётся остановлен. Перед публикацией '
            'проверьте черновик в n8n и убедитесь, что ключ чтения и бюджет настроены.</p>'
            '<form method="post" action="/settings/content-factory/ai/cutover/publish/prepare">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="receipt" value="' + _receipt(receipt) + '">'
            '<button type="submit">Проверить публикацию</button></form>')


def publish_preview(csrf, proposal, receipt):
    return ('<h1>Подтвердите публикацию</h1><p>Будет опубликована проверенная версия '
            + h(receipt.get('draft_version')) + '. Сценарий сможет использовать сохранённые модели; '
            'стоимость зависит от поставщика, бюджет оценочный и не гарантирует остановку списаний. '
            'Не редактируйте сценарий или credentials в n8n одновременно.</p>'
            '<form method="post" action="/settings/content-factory/ai/cutover/publish/confirm">'
            '<input type="hidden" name="csrf" value="' + h(csrf) + '">'
            '<input type="hidden" name="proposal_id" value="' + _proposal(proposal) + '">'
            '<input type="hidden" name="receipt" value="' + _receipt(receipt) + '">'
            '<label><input type="checkbox" name="ack_cost" value="understood" required> '
            'Понимаю, что цена моделей не проверена, а бюджет оценочный</label>'
            '<button type="submit">Опубликовать проверенную версию</button></form>')
