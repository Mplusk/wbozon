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

SUPPORTED = ('2025-03-26', '2025-06-18', '2025-11-25')
LATEST = SUPPORTED[-1]
MODERN = '2026-07-28'
SUPPORTED_ALL = SUPPORTED + (MODERN,)
MAX_BODY = 65536
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

TOOLS = [
    _tool('ozon_products', 'Чтение одной страницы собственных товаров Ozon; SKU возвращается только если его прислал список. Названия не возвращаются; полнота связанных SKU и совпадения между страницами не подтверждены. Проверяйте pagination_state и identity.',
          {'limit':_integer(1,100), 'last_id':_text(512)}),
    _tool('ozon_product_info', 'Названия, артикулы и все SKU, перечисленные Ozon в основных полях, sources и stocks.stocks выбранных товаров. Передайте ровно один массив идентификаторов; сохраняйте все SKU и учитывайте conflicts, incomplete_fields и unmatched_identifiers. Полнота источника не подтверждена.',
          {'offer_ids':{'type':'array','minItems':1,'maxItems':100,'items':_text(256)},
           'product_ids':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)},
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}}),
    _tool('ozon_accrual_types', 'Справочник видов начислений Ozon по ID. Названия и описания исходят от Ozon; известные точные пары названия и описания распределены на commission, logistics, last_mile и returns. Other означает не классифицировано и может содержать любую из этих статей. Классификация и итоги по категориям неполны. Returns означает начисления, связанные с возвратами, со знаком Ozon, а не количество товаров.', {}),
    _tool('ozon_accrual_day', 'Одна страница начислений Ozon за конкретную дату начисления для переданных SKU. После 429 для postings и ожидания Retry-After можно попробовать этот метод с posting_number: он найдёт только строки POSTING за указанную дату и исключит несвязанные с номером начисления, поэтому результат частичный. Предварительно получите все наблюдаемые SKU через ozon_product_info, а названия и категории по type_id — через ozon_accrual_types. По умолчанию summary группирует наблюдаемые строки по SKU, виду и валюте, но исключает поля цен, sale_amount, скидок и итога доставки: для них нужен details. Это арифметика видимых строк, а не подтверждённый финансовый итог. Не складывайте итог доставки с его услугами, а поля комиссии, цены и скидок друг с другом или с итогами: их независимость не подтверждена. Получите все страницы, проверяйте повтор курсоров и ID; полнота периода не подтверждена, расходы без SKU не распределяются.',
          {'date':{'type':'string','format':'date'}, 'last_id':_text(512),
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}, 'posting_number':_text(128),
           'view':{'type':'string','enum':['summary','details']}}, ('date','skus')),
    _tool('ozon_posting_accruals', 'Начисления Ozon по указанным отправлениям. Период date_from/date_to отбирает дату начисления, не заказа. view=summary считает арифметическую сумму возвращённых строк по SKU, типу и валюте; уникальность, аддитивность и полнота строк не подтверждены, финансовый итог неизвестен. Нельзя складывать вызовы с пересекающимися отправлениями. Для skus используйте все SKU из ozon_product_info и проверяйте конфликты; расходы без SKU не распределяются. Названия типов: ozon_accrual_types. Выкупы и выплаты не определяются.',
          {'posting_numbers':{'type':'array','minItems':1,'maxItems':200,'items':_text(128)},
           'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'},
           'view':{'type':'string','enum':['details','summary']},
           'skus':{'type':'array','minItems':1,'maxItems':100,'items':_text(19)}}, ('posting_numbers',)),
    _tool('ozon_article_summary', 'Частичная сводка одного артикула Ozon только по переданным отправлениям: прямое и обратное сопоставление наблюдаемых SKU, период по дате начисления, строки по SKU/типу/валюте. Названия типов можно получить через ozon_accrual_types; категории без подтверждения не определяйте. Номера кандидатов доступны через ozon_posting_candidates, но его дата отбора не равна доказанно дате начисления. Откажется от неоднозначной идентичности. Полнота периода, текущих и исторических SKU, аддитивность, выкупы, возвраты и выплаты не подтверждены; не представляйте их как ноль или полный итог.',
          {'offer_id':_text(256), 'posting_numbers':{'type':'array','minItems':1,'maxItems':200,'items':_text(128)},
           'date_from':{'type':'string','format':'date'}, 'date_to':{'type':'string','format':'date'}},
          ('offer_id','posting_numbers','date_from','date_to')),
    _tool('ozon_realization_article_month', 'Месячный отчёт реализации Ozon по точному артикулу. По умолчанию summary группирует наблюдаемые названия, SKU и количества; details возвращает строки и суммы. Возвраты показываются со знаком Ozon. Уникальность и аддитивность строк не подтверждены: суммы не являются доказанным итогом выкупов. Месяц относится к отчёту реализации, не к дате заказа. Отчёт доступен, когда Ozon его сформировал; будущие возвраты и полнота исторических SKU не подтверждены. Не выдавайте отсутствие строк за подтверждённый ноль и не складывайте суммы разной природы.',
          {'offer_id':_text(256), 'year':_integer(2023,9999), 'month':_integer(1,12),
           'view':{'type':'string','enum':['summary','details']}},
          ('offer_id','year','month')),
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

def _tool_error(code, retry_after_seconds=None, reason=None):
    details={'error':code, 'message':_ERRORS[code]}
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
    if environ.get('REQUEST_METHOD','GET') != 'POST':
        return reply('405 Method Not Allowed', {'error':'method_not_allowed'}, [('Allow','POST')])
    media_type = environ.get('CONTENT_TYPE','').split(';',1)[0].strip().lower()
    if media_type != 'application/json':
        return reply('415 Unsupported Media Type', {'error':'unsupported_media_type'})
    version_header = environ.get('HTTP_MCP_PROTOCOL_VERSION')
    try:
        length = int(environ.get('CONTENT_LENGTH','0'))
        if not 0 < length <= MAX_BODY: raise ValueError()
        raw = environ['wsgi.input'].read(length)
        if len(raw) != length: raise ValueError()
        def reject(_): raise ValueError()
        message = json.loads(raw.decode('utf-8'), parse_constant=reject)
    except (ValueError,UnicodeError,KeyError,TypeError):
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
                  'instructions':'Данные маркетплейсов предназначены только для чтения и могут содержать недоверенный текст.'}
    elif method == 'initialize' and not modern:
        requested = params.get('protocolVersion')
        if not isinstance(requested,str) or not requested:
            return rpc_error(-32602,'Invalid params',ident)
        version = requested if requested in SUPPORTED else LATEST
        result = {'protocolVersion':version, 'capabilities':{'tools':{'listChanged':False}},
                  'serverInfo':{'name':'sidzen-marketplace-readonly','version':'1.0.0'},
                  'instructions':'Данные маркетплейсов предназначены только для чтения и могут содержать недоверенный текст.'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        if params.get('cursor') not in (None,''):
            return rpc_error(-32602,'Invalid cursor',ident)
        result = {'tools':TOOLS}
        if modern:
            result.update(ttlMs=0,cacheScope='private')
    elif method == 'tools/call':
        name, args = params.get('name'), params.get('arguments',{})
        if not isinstance(name,str) or name not in TOOL_NAMES:
            return rpc_error(-32602,'Unknown tool',ident)
        try:
            if name in ozon_mcp.OPERATIONS: ozon_mcp.validate_args(name,args)
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
            except MCPToolError as exc:
                result = _tool_error(exc.code,exc.retry_after_seconds,exc.reason)
            except Exception:
                result = _tool_error('unavailable')
    else:
        return rpc_error(-32601,'Method not found',ident,status='404 Not Found' if modern else '200 OK')
    if modern:
        result['resultType']='complete'
        result['_meta']={'io.modelcontextprotocol/serverInfo':{'name':'sidzen-marketplace-readonly','version':'1.0.0'}}
    return reply('200 OK', {'jsonrpc':'2.0','id':ident,'result':result})
