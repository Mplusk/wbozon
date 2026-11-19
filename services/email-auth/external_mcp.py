"""Stateless, bearer-authenticated MCP Streamable HTTP transport.

The caller supplies key lookup and marketplace execution callbacks. This module
does not store credentials, authenticate browser sessions, or contact a seller
API itself. The transport supports 2026-07-28 and the 2025 Streamable HTTP
versions; it returns JSON responses, not SSE streams, and never issues sessions.
"""
import base64
import json
import re

import ozon_mcp
import wb_mcp
import market_specs
import avito_mcp
import external_keys
import market_catalog

SUPPORTED = ('2025-03-26', '2025-06-18', '2025-11-25')
LATEST = SUPPORTED[-1]
MODERN = '2026-07-28'
SUPPORTED_ALL = SUPPORTED + (MODERN,)
MAX_BODY = 65536
MAX_UPLOAD = 4 * 1024 * 1024
MAX_OUTPUT = 524288
TOKEN_PATTERN = re.compile(r'mcp_[A-Za-z0-9_-]{43}\Z')

ANNOTATIONS = {'readOnlyHint': True, 'destructiveHint': False,
               'idempotentHint': True, 'openWorldHint': True}

def _integer(minimum, maximum):
    return {'type':'integer', 'minimum':minimum, 'maximum':maximum}

def _text(maximum):
    return {'type':'string', 'maxLength':maximum}

def _tool(name, description, properties, required=()):
    return {'name':name, 'description':description,
            'inputSchema':{'type':'object', 'properties':properties,
                           'required':list(required), 'additionalProperties':False},
            'annotations':dict(ANNOTATIONS),
            'securitySchemes':[{'type':'oauth2','scopes':['mcp:read']}],
            '_meta':{'securitySchemes':[{'type':'oauth2','scopes':['mcp:read']}]}}

_DOCUMENT_DATE = {'type':'string', 'format':'date'}
_SUPPLY_EXPENSE = {'type':'object', 'additionalProperties':False,
    'description':'Одна строка предоставленного документа. Укажите её собственные дату, ссылку, сумму со знаком, валюту и явно указанную поставку.',
    'properties':{'document_id':_text(128), 'line_id':_text(128), 'document_date':_DOCUMENT_DATE,
                  'period_from':_DOCUMENT_DATE, 'period_to':_DOCUMENT_DATE, 'supply_id':_text(19),
                  'scope':{'type':'string', 'enum':['single_supply','shared_or_unknown']},
                  'amount':{'type':'string', 'description':'Сумма со знаком и исходными десятичными разрядами.'},
                  'currency':_text(16), 'accrual_id':{'type':'string', 'maxLength':19,
                      'description':'Если основание — начисление Ozon, укажите его ID. Без документированной связи API расчёт будет неполным.'}}}

TOOLS = [
    _tool('ozon_unallocated_accruals', 'Одна страница по дате начисления для типов 12, 133, 134, 77 и 46. Возвращает исходный знак, валюту, accrual_id, unit_number и возможный номер отправления для категории POSTING. Ссылка на отправление требует сверки. Строки с явным SKU перечислены отдельно. Справочник названий запрашивается у Ozon по умолчанию. Не приписывайте строки без SKU артикулу или поставке. Для следующей страницы используйте ту же дату, учитывайте повторы курсоров и ID. Соблюдайте Retry-After.',
          {'date':_DOCUMENT_DATE, 'last_id':_text(512), 'include_types':{'type':'boolean'}}, ('date',)),
    _tool('ozon_fbo_supply_orders', 'Страница идентификаторов заявок на поставку FBO, начиная с новых. По умолчанию запрашивает все документированные статусы. Для продолжения передайте next_cursor как last_id с теми же фильтрами. Для состава сначала получите сведения о заявках.',
          {'states':{'type':'array','minItems':1,'maxItems':len(ozon_mcp.ozon_fbo.STATES),'uniqueItems':True,
                     'items':{'type':'string','enum':list(ozon_mcp.ozon_fbo.STATES)}},
           'order_number':_text(128), 'limit':_integer(1,100), 'last_id':_text(512)}),
    _tool('ozon_fbo_supply_details', 'Сведения по ID заявок FBO. Для каждой заявки показывает статусы, даты и все её поставки с supply_id и bundle_id. Используйте bundle_id конкретной поставки для чтения состава.',
          {'order_ids':{'type':'array','minItems':1,'maxItems':50,'uniqueItems':True,'items':_text(19)}}, ('order_ids',)),
    _tool('ozon_fbo_supply_items', 'Одна страница состава по точному bundle_id. Каждая строка сохраняет SKU, артикул, название, количество, объём единицы и объём строки в литрах. При has_next=true продолжите по next_cursor. Количество относится к составу заявки. Проверяйте повторы SKU по всем страницам.',
          {'bundle_id':_text(512), 'limit':_integer(1,100), 'last_id':_text(512)}, ('bundle_id',)),
    _tool('ozon_fbo_volume_allocation', 'Распределение одной строки документа по объёму товаров указанной поставки FBO. Читает все доступные страницы её состава. Для оценки передайте expense с document_id, line_id, document_date, суммой, валютой, supply_id из документа и scope=single_supply. Неполный состав, отсутствующие объёмы и неподтверждённая привязка начисления к поставке блокируют расчёт. Результат — модель по данным документа. Не добавляйте её к фактическим начислениям по артикулу.',
          {'order_id':_text(19), 'supply_id':_text(19), 'expense':_SUPPLY_EXPENSE}, ('order_id','supply_id')),
    _tool('ozon_products', 'Одна страница товаров Ozon. По умолчанию дополнительно получает название и все наблюдаемые SKU из карточки, сверяя product_id и точный артикул; проверяйте identity_status и identity_enrichment. При несовпадении данные не прикрепляются. Полнота SKU, исторические связи и совпадения между страницами не подтверждены. include_identity=false отключает дополнительный запрос.',
          {'limit':_integer(1,100), 'last_id':_text(512), 'include_identity':{'type':'boolean'}}),
    _tool('ozon_product_info', 'Названия, артикулы и все SKU, перечисленные Ozon в основных полях, sources и stocks.stocks выбранных товаров. Передайте ровно один массив идентификаторов; сохраняйте все SKU и учитывайте conflicts, incomplete_fields и unmatched_identifiers. Полнота источника не подтверждена.',
          {'offer_ids':{'type':'array','minItems':1,'maxItems':100,'items':_text(256)},
           'product_ids':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)},
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}}),
    _tool('ozon_accrual_types', 'Справочник видов начислений Ozon по ID. Названия и описания исходят от Ozon; известные точные пары названия и описания распределены на commission, logistics, last_mile и returns. Other означает не классифицировано и может содержать любую из этих статей. Классификация и итоги по категориям неполны. Returns означает начисления, связанные с возвратами, со знаком Ozon, а не количество товаров.', {}),
    _tool('ozon_accrual_day', 'Одна страница начислений за дату по переданным SKU. После 429 соблюдайте Retry-After. Фильтр posting_number ищет точное значение unit_number в категории POSTING. Ozon использует это поле для заказов и услуг. Данные отправления доступны отдельно в ozon_posting_accruals. Общего accrual_id для привязки дневной строки эти ответы не дают. Получите все SKU через ozon_product_info, типы через ozon_accrual_types. В summary суммы сгруппированы по SKU, виду и валюте. В details доступны цены, sale_amount, скидки и общий итог доставки. Проверяйте повтор курсоров и ID. Независимость итогов и составляющих, связь расходов без SKU с артикулом и полнота периода неизвестны.',
          {'date':{'type':'string','format':'date'}, 'last_id':_text(512),
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}, 'posting_number':_text(128),
           'view':{'type':'string','enum':['summary','details']}}, ('date','skus')),
    _tool('ozon_posting_accruals', 'Начисления Ozon по указанным отправлениям за период по дате начисления. view=summary разделяет SKU, вид начисления, валюту и отправления. В breakdown поле observed_unit_rate показывает начисление на единицу в строках одного вида услуги. Сохраняйте виды отдельно: один товар может участвовать в нескольких услугах. Справочник выделяет commission, logistics, last_mile и returns. Returns относится к услугам возврата, other — к неклассифицированным видам. include_types=false отключает справочник. После 429 соблюдайте Retry-After. При response_too_large разбивайте отправления на непересекающиеся партии. Учитывайте все SKU из ozon_product_info и проверяйте конфликты. Число выкупов, выплата и полнота финансового итога неизвестны.',
          {'posting_numbers':{'type':'array','minItems':1,'maxItems':200,'items':_text(128)},
           'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'view':{'type':'string','enum':['details','summary']}, 'include_types':{'type':'boolean'},
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}}, ('posting_numbers',)),
    _tool('ozon_article_summary', 'Частичная сводка артикула по переданным отправлениям за период по дате начисления. Проверяет артикул, название и все наблюдаемые SKU в обоих направлениях. observations.article_unit_rates даёт среднюю ставку по всем сопоставленным SKU артикула отдельно для каждого вида услуги и валюты. В observations.breakdown доступны суммы и ставки отдельных SKU. Для категорий используется ozon_accrual_types. include_types=false отключает справочник. Номера-кандидаты доступны через ozon_posting_candidates. Период их отбора не подтверждает полноту начислений. Расходы без SKU остаются нераспределёнными. Полнота текущих и исторических SKU, уникальность и аддитивность строк не подтверждены. Полное число выкупов и банковская выплата неизвестны.',
          {'offer_id':_text(256), 'posting_numbers':{'type':'array','minItems':1,'maxItems':200,'items':_text(128)},
           'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'include_types':{'type':'boolean'}},
          ('offer_id','posting_numbers','date_from','date_to')),
    _tool('ozon_realization_article_month', 'Месячный отчёт реализации Ozon по точному артикулу. Проверяет все пары SKU и артикулов в ответе. Конфликт или пропуск идентификатора даёт status=blocked. В summary показаны количества и observed_article_unit_rates по артикулу. В by_sku доступны ставки по каждому SKU. Доставки и возвраты посчитаны отдельно. total — итого к начислению, standard_fee — базовое вознаграждение Ozon. Знаменатель относится к единицам в строках соответствующей части отчёта, знак сумм сохранён. Пробелы указываются в issues. В details доступны исходные строки. База дат — месяц реализации. Уникальность и аддитивность исходных строк не подтверждены. Более поздние возвраты, полнота продаж и банковская выплата неизвестны.',
          {'offer_id':_text(256), 'year':_integer(2023,9999), 'month':_integer(1,12),
           'view':{'type':'string','enum':['summary','details']}},
          ('offer_id','year','month')),
    _tool('ozon_return_events', 'Одна страница событий возврата Ozon по точному артикулу и дате logistic.return_date в UTC, период не более 90 дней. Запрашивайте отдельно FBO и FBS, все страницы по next_last_id_candidate; проверяйте повторы и изменения статусов. Тип Cancellation, отказы и ClientReturn различаются, а текущий статус может быть незавершённым. Это не подтверждённый финансовый возврат и не дата заказа: не вычитайте события за неделю из заказов этой недели. Полнота и терминальная граница API не подтверждены; отсутствующие даты исключаются и учитываются отдельно.',
          {'offer_id':_text(256),'date_from':{'type':'string','format':'date'},
           'date_to':{'type':'string','format':'date'},'scheme':{'type':'string','enum':['FBO','FBS']},
           'last_id':_text(19),'limit':_integer(1,100),'view':{'type':'string','enum':['summary','details']}},
          ('offer_id','date_from','date_to','scheme')),
    _tool('ozon_prices', 'Чтение цен и комиссий товаров Ozon.',
          {'limit':_integer(1,100), 'cursor':_text(512),
           'offer_ids':{'type':'array','maxItems':100,'items':_text(256)}}),
    _tool('ozon_orders_analytics', 'Аналитика заказов Ozon за период включительно, по дням, максимум 90 дней. Суммы относятся только к возвращённым дням; полнота периода и отсутствие заказов в пропущенные дни не подтверждены, пустой ответ не означает ноль. Это стоимость заказанных товаров, а не выкуп, прибыль или выплаты. Сравнивайте с суммой заказов WB, не с выкупами. Валюта компании company_currency получена из Ozon отдельно: если она совпадает с валютой WB, можно привести условную общую сумму, прямо указав предположение, что валюта отчёта Ozon совпадает с валютой компании; сам отчёт этого не подтверждает. Иначе не складывайте суммы.',
          {'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'}}, ('date_from','date_to')),
    _tool('ozon_posting_status', 'Текущие статусы отправлений Ozon за период отбора API (UTC). Отдельно запросите FBO и FBS и все страницы: delivered — доставлено, cancelled — отменено; это не окончательные выкупы или возвраты. Период отбора может не совпадать с датами заказов аналитики.',
          {'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'scheme':{'type':'string','enum':['FBO','FBS']}, 'cursor':_text(512), 'limit':_integer(1,100)},
          ('date_from','date_to','scheme')),
    _tool('ozon_posting_candidates', 'Номера отправлений одной страницы FBO или FBS для дальнейшей частичной сводки. Период запроса — не более 90 дней; более длинный период разделите на интервалы. Проверьте обе схемы, страницы и повторы между ними. Запоминайте все посещённые курсоры; при любом повторе остановитесь и отметьте обход неполным. Период отбора отправлений Ozon не подтверждён как дата заказа или начисления; он не гарантирует полноту начислений за период. Покупательские данные не возвращаются.',
          {'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'scheme':{'type':'string','enum':['FBO','FBS']}, 'cursor':_text(512), 'limit':_integer(1,100)},
          ('date_from','date_to','scheme')),
    _tool('wb_products', 'Чтение страницы собственных карточек Wildberries.',
          {'limit':_integer(1,100), 'updated_at':_text(64), 'nm_id':_integer(1,9007199254740991)}),
    _tool('wb_prices', 'Чтение страницы собственных цен Wildberries.',
          {'limit':_integer(1,100), 'offset':_integer(0,1000000)}),
    _tool('wb_commissions', 'Чтение и фильтрация таблицы комиссий Wildberries.',
          {'limit':_integer(1,200), 'subject_id':_integer(1,9007199254740991), 'search':_text(100)}),
    _tool('wb_box_tariffs', 'Чтение тарифов Wildberries на короба для указанной даты.',
          {'date':{'type':'string','pattern':r'^\d{4}-\d{2}-\d{2}$'},
           'limit':_integer(1,200), 'warehouse':_text(100)}, ('date',)),
    _tool('wb_sales_summary', 'Аналитика Wildberries за период включительно: сумма и количество заказов, выкупов и отмен. Выкупы привязаны к дате заказа. Получите все страницы и не выдавайте неполный итог за полный; сравнивайте сумму заказов с Ozon, не смешивая её с выкупами. Не складывайте суммы площадок без подтверждения одинаковой валюты.',
          {'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'limit':_integer(1,100), 'offset':_integer(0,1000000)}, ('date_from','date_to')),
]
_EXTRA_ARGUMENTS={
    'ozon_realization_article_month': {'all_articles':{'type':'boolean'}},
    'ozon_posting_status': {'include_geography':{'type':'boolean'},'article':_text(256)},
    'ozon_posting_candidates': {'include_geography':{'type':'boolean'},'article':_text(256)},
    'ozon_posting_accruals': {'offer_id':_text(256),'discover_postings':{'type':'boolean'},'batch':_integer(1,500)},
    'ozon_article_summary': {'discover_postings':{'type':'boolean'},'batch':_integer(1,500)},
    'ozon_orders_analytics': {k:market_specs.FIELDS[k] for k in ('article','sku','limit','offset','include_funnel','mode')},
    'wb_sales_summary': {k:market_specs.FIELDS[k] for k in ('nm_ids','vendor_codes','group_by')},
    'wb_box_tariffs': {'nm_id':_integer(1,9007199254740991),'rf_mgt':{'type':'boolean','description':'Расчёт для малогабаритного товара, прямая доставка FBW по РФ.'}},
    'wb_products': {'vendor_code':_text(256),'cursor':_text(1024)},
    'wb_prices': {'vendor_code':_text(256),'cursor':_text(1024)},
}
_EXTRA_ARGUMENTS['ozon_orders_analytics']['dimension']={'type':'string','enum':['sku']}
for tool in TOOLS:
    name=tool['name']
    if name in _EXTRA_ARGUMENTS:
        schema=tool['inputSchema']
        schema['properties'].update(_EXTRA_ARGUMENTS[name])
        if name in ('ozon_article_summary','ozon_posting_accruals'):
            schema['required']=[k for k in schema.get('required',[]) if k!='posting_numbers']
            tool['description']+=' Для автоматического поиска передайте discover_postings=true, offer_id, date_from и date_to.'
        elif name=='ozon_realization_article_month':
            schema['required']=['year','month']
            tool['description']+=' Для сводки по всем артикулам месяца передайте all_articles=true.'
        elif name=='ozon_orders_analytics':
            tool['description']+=' Для штук и суммы по SKU задайте dimension=sku и при необходимости article.'
TOOLS.extend(_tool(name, contract[0], *market_specs.schema(name)) for name, contract in market_specs.TOOLS.items())
TOOL_NAMES = frozenset(tool['name'] for tool in TOOLS)

_ERRORS = {
    'not_configured':'Ключ маркетплейса не сохранён в кабинете.',
    'provider_access_denied':'Маркетплейс отклонил доступ. Проверьте права и срок действия ключа, а также доступность метода в вашей подписке.',
    'rate_limited':'Превышен лимит запросов. Повторите позднее.',
    'provider_timeout':'Маркетплейс не ответил вовремя.',
    'unavailable':'Не удалось получить данные маркетплейса.',
    'invalid_arguments':'Недопустимые параметры запроса.',
    'response_too_large':'Готовый ответ слишком велик и не был обрезан. Если метод поддерживает размер страницы, список SKU или партию отправлений, уменьшите их. Если разделение не поддерживается, этот вызов не может безопасно вернуть полный ответ.',
    'provider_response_too_large':'Исходный ответ Ozon слишком велик и не был обрезан. Для запроса по отправлениям уменьшите их партию. Фильтр SKU или артикула не уменьшает исходный отчёт за день или месяц; если сам ответ превышает предел, этот вызов не может его загрузить.',
    'provider_response_invalid':'Маркетплейс вернул неполный или некорректный ответ; данные не использованы.',
}

class MCPToolError(Exception):
    """Only fixed, non-sensitive error codes may cross the client boundary."""
    def __init__(self, code, retry_after_seconds=None, reason=None):
        if code not in _ERRORS: raise ValueError('Invalid tool error code')
        if retry_after_seconds is not None and (code != 'rate_limited' or type(retry_after_seconds) is not int or
                                                 not 0 <= retry_after_seconds <= 2147483647):
            raise ValueError('Invalid retry interval')
        if reason is not None and reason not in ('busy','cooldown','retry_time_unknown','storage_unavailable','retry_interval_unavailable'):
            raise ValueError('Invalid error reason')
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason
        super().__init__(code)

def _tool_error(code, retry_after_seconds=None, reason=None, diagnostic=None):
    details={'error':code, 'message':_ERRORS[code]}
    if code=='provider_timeout' and diagnostic: details.update(diagnostic)
    if code == 'rate_limited':
        details['retry_after_seconds']=retry_after_seconds
        if reason == 'retry_time_unknown':
            details['message']='Время повтора неизвестно; автоматический повтор остановлен.'
        elif reason == 'retry_interval_unavailable':
            details['message']='Время повтора нельзя безопасно показать.'
    if reason is not None: details['reason']=reason
    return {'content':[{'type':'text','text':json.dumps(details, ensure_ascii=False)}], 'isError':True}

def handle(environ, start_response, authenticate, execute, expected_origin):
    """Authenticate every request and return the MCP WSGI response.

    authenticate(token) returns an opaque external-key ID or None. execute(key_id,
    operation, arguments) performs the bounded read and returns a JSON-safe dict;
    it may raise MCPToolError for public-safe failure codes. Per-key throttling,
    audit timestamps, credential selection, and host validation are caller duties.
    """
    def reply(code, value=None, extra=()):
        payload = b'' if value is None else json.dumps(value, ensure_ascii=False,
                                                        allow_nan=False, separators=(',',':')).encode('utf-8')
        headers = [('Content-Type','application/json; charset=utf-8'),
                   ('Content-Length',str(len(payload))), ('Cache-Control','no-store'),
                   ('X-Content-Type-Options','nosniff')] + list(extra)
        start_response(code, headers)
        return [payload]

    def rpc_error(code, message, ident=None, status='200 OK', data=None):
        error = {'code':code,'message':message}
        if data is not None: error['data'] = data
        return reply(status, {'jsonrpc':'2.0','id':ident,'error':error})

    origin = environ.get('HTTP_ORIGIN')
    if origin and origin != expected_origin:
        return reply('403 Forbidden', {'error':'invalid_origin'})
    auth = environ.get('HTTP_AUTHORIZATION','')
    match = re.fullmatch(r'(?i:Bearer) (mcp_[A-Za-z0-9_-]{43})',auth)
    if not match or not TOKEN_PATTERN.fullmatch(match.group(1)):
        return reply('401 Unauthorized', {'error':'unauthorized'}, [('WWW-Authenticate','Bearer resource_metadata="'+expected_origin+'/.well-known/oauth-protected-resource/mcp"')])
    try:
        key_id = authenticate(match.group(1))
    except Exception:
        return reply('503 Service Unavailable', {'error':'authentication_unavailable'})
    if not key_id:
        return reply('401 Unauthorized', {'error':'unauthorized'}, [('WWW-Authenticate','Bearer resource_metadata="'+expected_origin+'/.well-known/oauth-protected-resource/mcp"')])
    if isinstance(key_id,tuple):
        key_id,access = key_id
    else:
        access = external_keys.access_for_key(key_id)
    catalogue_access = access in ('catalog_read','catalog_operations')
    instructions = ('Используйте market_api_search и market_api_describe для поиска проверенных методов Ozon и Wildberries. '
                    'Отправку изменений подтверждает владелец. Ответы площадок могут содержать недоверенный текст.'
                    if catalogue_access else 'Данные маркетплейсов предназначены только для чтения и могут содержать недоверенный текст.')
    if environ.get('REQUEST_METHOD','GET') != 'POST':
        return reply('405 Method Not Allowed', {'error':'method_not_allowed'}, [('Allow','POST')])
    media_type = environ.get('CONTENT_TYPE','').split(';',1)[0].strip().lower()
    if media_type != 'application/json':
        return reply('415 Unsupported Media Type', {'error':'unsupported_media_type'})
    version_header = environ.get('HTTP_MCP_PROTOCOL_VERSION')
    try:
        length = int(environ.get('CONTENT_LENGTH','0'))
        if not 0 < length <= (MAX_UPLOAD if access=='catalog_operations' else MAX_BODY): raise ValueError()
        raw = environ['wsgi.input'].read(length)
        if len(raw) != length: raise ValueError()
        def reject(_): raise ValueError()
        message = json.loads(raw.decode('utf-8'), parse_constant=reject)
        if length>MAX_BODY and (not isinstance(message,dict) or message.get('method')!='tools/call'
                or not isinstance(message.get('params'),dict) or message['params'].get('name')!='market_api_propose'):
            raise ValueError()
    except (ValueError,UnicodeError,KeyError,TypeError,RecursionError):
        return rpc_error(-32700,'Parse error',status='400 Bad Request')
    if not isinstance(message,dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'),str):
        return rpc_error(-32600,'Invalid Request',status='400 Bad Request')
    method = message['method']
    params = message.get('params',{})
    if not isinstance(params,dict):
        return rpc_error(-32600,'Invalid Request',status='400 Bad Request')
    meta = params.get('_meta',{})
    meta_version = meta.get('io.modelcontextprotocol/protocolVersion') if isinstance(meta,dict) else None
    if version_header and version_header not in SUPPORTED_ALL:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',version_header) and version_header >= MODERN:
            if version_header != meta_version:
                return rpc_error(-32020,'Header mismatch',message.get('id'),status='400 Bad Request')
            return rpc_error(-32022,'Unsupported protocol version',message.get('id'),status='400 Bad Request',
                             data={'supported':list(SUPPORTED_ALL),'requested':version_header})
        return reply('400 Bad Request', {'error':'unsupported_protocol_version'})
    modern = version_header == MODERN or meta_version == MODERN
    if modern and 'id' in message:
        # Modern request headers mirror the JSON-RPC body; never dispatch when
        # a proxy and the application could interpret two different operations.
        ident = message.get('id')
        if version_header != MODERN or meta_version != MODERN or environ.get('HTTP_MCP_METHOD') != method:
            return rpc_error(-32020,'Header mismatch',ident,status='400 Bad Request')
        if not isinstance(meta,dict) or not isinstance(meta.get('io.modelcontextprotocol/clientCapabilities'),dict):
            return rpc_error(-32602,'Invalid params',ident,status='400 Bad Request')
        if method in ('tools/call','prompts/get','resources/read'):
            header_name = environ.get('HTTP_MCP_NAME')
            try:
                if isinstance(header_name,str) and header_name.startswith('=?base64?') and header_name.endswith('?='):
                    header_name=base64.b64decode(header_name[9:-2],validate=True).decode('utf-8')
                expected_name=params.get('uri') if method=='resources/read' else params.get('name')
                if not isinstance(header_name,str) or not isinstance(expected_name,str) or header_name != expected_name:
                    raise ValueError()
            except (ValueError,UnicodeError):
                return rpc_error(-32020,'Header mismatch',ident,status='400 Bad Request')
    if 'id' not in message:
        # Notifications do not execute tools and receive an empty HTTP 202.
        if method.startswith('notifications/'):
            return reply('202 Accepted')
        return rpc_error(-32600,'Invalid Request',status='400 Bad Request')
    ident = message['id']
    if not (isinstance(ident,str) or type(ident) is int):
        return rpc_error(-32600,'Invalid Request',status='400 Bad Request')
    if modern and method == 'server/discover':
        result = {'supportedVersions':list(SUPPORTED_ALL), 'capabilities':{'tools':{'listChanged':False}},
                  'instructions':instructions}
    elif method == 'initialize' and not modern:
        requested = params.get('protocolVersion')
        if not isinstance(requested,str) or not requested:
            return rpc_error(-32602,'Invalid params',ident)
        version = requested if requested in SUPPORTED else LATEST
        result = {'protocolVersion':version, 'capabilities':{'tools':{'listChanged':False}},
                  'serverInfo':{'name':'sidzen-marketplace-readonly','version':'1.0.0'},
                  'instructions':instructions}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        if params.get('cursor') not in (None,''):
            return rpc_error(-32602,'Invalid cursor',ident)
        catalog = list(TOOLS) if access in ('marketplaces','all') else []
        for name,(description,fields,required) in market_catalog.PUBLIC_TOOLS.items():
            if name not in market_catalog.available(access):
                continue
            item = _tool(name,description,fields,required)
            if name=='market_api_propose':
                item['annotations'].update(readOnlyHint=False,openWorldHint=False)
            if name in ('market_api_propose','market_api_status'):
                item['securitySchemes'][0]['scopes']=['mcp:read','mcp:propose']
                item['_meta']['securitySchemes']=item['securitySchemes']
            catalog.append(item)
        if access in ('avito','all'):
            catalog.extend(_tool(name, op['description'], op['properties'], op.get('required', ()))
                           for name, op in avito_mcp.CONTRACTS.items())
        result = {'tools':catalog}
        if modern:
            result.update(ttlMs=0,cacheScope='private')
    elif method == 'tools/call':
        name, args = params.get('name'), params.get('arguments',{})
        if not isinstance(name,str) or not (
                (access in ('marketplaces','all') and name in TOOL_NAMES) or
                name in market_catalog.available(access) or
                (access in ('avito','all') and name in avito_mcp.CONTRACTS)):
            return rpc_error(-32602,'Unknown tool',ident)
        try:
            if name in market_catalog.PUBLIC_TOOLS: market_catalog.validate(name,args)
            elif name in avito_mcp.CONTRACTS: avito_mcp.validate(name,args)
            elif name in market_specs.TOOLS: market_specs.validate(name,args)
            elif name in ozon_mcp.OPERATIONS: ozon_mcp.validate_args(name,args)
            else:
                wb_mcp.validate_args(name,args)
                if name == 'wb_prices' and 'nm_id' in args: raise ValueError()
        except (TypeError,ValueError):
            result = _tool_error('invalid_arguments')
        else:
            try:
                data = execute(key_id,name,args)
                if not isinstance(data,dict): raise ValueError()
                text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',',':'))
                result = (_tool_error('response_too_large') if len(text.encode('utf-8')) > MAX_OUTPUT
                          else {'content':[{'type':'text','text':text}]})
                if name in market_specs.TOOLS and data.get('status')=='provider_access_denied':
                    result['isError']=True
                if (name in market_catalog.PUBLIC_TOOLS or name in market_specs.seller_reports.GROUPS) and data.get('status') in ('action_unavailable','provider_error'):
                    result['isError']=True
            except MCPToolError as exc:
                result = _tool_error(exc.code,exc.retry_after_seconds,exc.reason,getattr(exc,'diagnostic',None))
            except Exception:
                result = _tool_error('unavailable')
    else:
        return rpc_error(-32601,'Method not found',ident,status='404 Not Found' if modern else '200 OK')
    if modern:
        result['resultType']='complete'
        result['_meta']={'io.modelcontextprotocol/serverInfo':{'name':'sidzen-marketplace-readonly','version':'1.0.0'}}
    return reply('200 OK', {'jsonrpc':'2.0','id':ident,'result':result})
