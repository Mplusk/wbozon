"""Discover and invoke only the marketplace operations in the reviewed, bundled catalogue."""
import base64
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

import finance_gate
import market_cooldown
import market_health
import market_history
import market_transport
import ozon_review_reply


PUBLIC_TOOLS = {
    'market_api_search': ('Поиск методов Ozon и WB по названию, пути и назначению. Возвращает operation_id и курсор следующей страницы.',
        {'market':{'enum':['ozon','wb']},'category':{'enum':['read','write','superseded','retired','unverified']},
         'search':{'type':'string','maxLength':160},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50}}, []),
    'market_api_describe': ('Назначение метода и схема параметров из документации площадки. Для вызова передайте параметры в input.',
        {'operation_id':{'type':'string','maxLength':160}}, ['operation_id']),
    'market_api_read': ('Выполняет проверенный метод чтения Ozon или WB. Сначала запросите схему через market_api_describe.',
        {'operation_id':{'type':'string','maxLength':160},'input':{'type':'object'}}, ['operation_id','input']),
    'market_api_propose': ('Создаёт запрос владельцу на одну операцию. Владелец проверяет точные параметры и подтверждает отправку в кабинете. Сохраняйте request_id при повторном вызове.',
        {'operation_id':{'type':'string','maxLength':160},'input':{'type':'object'},
         'request_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{16,80}$'}}, ['operation_id','input','request_id']),
    'market_api_status': ('Статус ранее предложенной операции по её идентификатору.',
        {'proposal_id':{'type':'string','pattern':'^[0-9a-f]{32}$'}}, ['proposal_id']),
    ozon_review_reply.TOOL: (
        'После явной команды на отправку создаёт заявку с точным review_id и текстом ответа Ozon. '
        'Покажите черновик в чате до вызова. Передайте владельцу approval_url для подтверждения в кабинете. '
        'После подтверждения получите результат по proposal_id через market_api_status.',
        {'review_id':{'type':'string','minLength':1,'maxLength':ozon_review_reply.MAX_ID},
         'text':{'type':'string','minLength':1,'maxLength':ozon_review_reply.MAX_TEXT}}, ['review_id','text']),
}
READ_TOOLS = frozenset(('market_api_search','market_api_describe','market_api_read'))
PROPOSAL_TOOLS = frozenset(('market_api_propose',ozon_review_reply.TOOL))
MAX_BYTES = 360 * 1024
ROUTES = Path(__file__).with_name('market_routes.jsonl')
DIGITAL_HOST = 'devapi-digital.wildberries.ru'
WB_SELLER_HOSTS = market_transport.WB_HOSTS | {
    name+'.wildberries.ru' for name in ('marketplace-api','buyer-chat-api','advert-media-api','dp-calendar-api',
                                         'returns-api','user-management-api')}
WB_EXTRA = {'marketplace-api':('wb_marketplace_key','Маркетплейс'),
            'buyer-chat-api':('wb_chat_key','Чаты с покупателями'),
            'user-management-api':('wb_users_key','Управление пользователями'),
            'returns-api':('wb_returns_key','Возвраты')}
WB_TOKEN_TYPES = {1: 'Базовый', 2: 'Тестовый', 3: 'Персональный', 4: 'Сервисный'}
WB_READ_REQUIREMENTS = {
    'wb_get_api_common_rating': {
        'category': 'wb_feedback',
        'token_types': (4,),
        'denial_reason': 'seller_rating_access_required',
        'documentation': 'https://dev.wildberries.kg/docs/openapi/api-information',
    },
    'wb_post_api_content_recommendations_list': {
        'category': 'wb_content',
        'token_types': (3, 4),
        'denial_reason': 'content_recommendations_access_required',
        'subscription': 'Джем — Продвинутый или Премиальный',
        'documentation': 'https://dev.wildberries.kg/docs/openapi/item-management',
    },
}


def category_key(host):
    label=host.split('.')[0]
    return WB_EXTRA[label][0] if label in WB_EXTRA else market_health.HOST_KEYS.get(label)


@lru_cache(maxsize=1)
def routes():
    with ROUTES.open(encoding='utf-8') as f:
        rows = [json.loads(line) for line in f]
    result = {r['operation_id']:r for r in rows}
    if len(result)!=len(rows):
        raise ValueError('Duplicate operation')
    return result


@lru_cache(maxsize=64)
def find_route(operation_id):
    # A short-lived provider worker requests one method. The compiler writes
    # the identifier first on every line so only that contract needs decoding.
    prefix=b'{"operation_id":'+json.dumps(operation_id,ensure_ascii=True).encode('ascii')+b','
    with ROUTES.open('rb') as source:
        for line in source:
            if line.startswith(prefix):
                return json.loads(line)
    return None


def available(access):
    if access=='catalog_operations':
        return PUBLIC_TOOLS.keys()
    return READ_TOOLS if access=='catalog_read' else ()


def _public(row):
    return {k:v for k,v in row.items() if k not in ('contract','official_path','source','schema_sha256')}


def search(arguments):
    term = arguments.get('search','').strip().casefold()
    matched = [_public(r) for r in routes().values()
               if all(key not in arguments or arguments[key]==r[key] for key in ('market','category'))
               and (not term or term in ' '.join(str(r.get(k,'')) for k in
                    ('operation_id','title','path','section','legacy_route','alias_of')).casefold())]
    start, size = arguments.get('offset',0),arguments.get('limit',20)
    end = start+size
    return {'operations':matched[start:end],'total':len(matched),'next_offset':end if end<len(matched) else None}


def describe(operation_id):
    row = routes()[operation_id]
    result = _public(row)
    if requirements := _wb_access_requirements(row):
        result['access_requirements'] = requirements
    if spec := row.get('contract'):
        result.update(input_parameters=spec['parameters'],required=spec['required'],
                      body_by_content_type=spec['media'],definitions=spec['definitions'],
                      schema_sha256=row['schema_sha256'])
        if spec['media']:
            result['body_encoding'] = ('JSON for application/json. For multipart, pass binary fields as base64. '
                'Optional files maps each binary field to {name,type} or an array of these for multiple files. '
                'The entire MCP proposal including base64 must fit in 4 MiB. A read response is limited to 360 KiB.')
    return result


def _check(value, schema, definitions, depth=0):
    if depth>42:
        raise ValueError('Input nesting limit')
    if '$ref' in schema:
        return _check(value,definitions[schema['$ref'].removeprefix('#/$defs/')],definitions,depth+1)
    if value is None and schema.get('nullable'):
        return
    for item in schema.get('allOf',[]):
        _check(value,item,definitions,depth+1)
    for mode in ('oneOf','anyOf'):
        if mode not in schema:
            continue
        matches = 0
        for item in schema[mode]:
            try:
                _check(value,item,definitions,depth+1)
                matches += 1
            except ValueError:
                pass
        if not matches or (mode=='oneOf' and matches!=1):
            raise ValueError('Input does not match an alternative')
    kind = schema.get('type')
    types = {'array':list,'object':dict,'boolean':bool,'integer':int,'number':(int,float),'string':str,'null':type(None)}
    if kind and (not isinstance(value,types[kind]) or (kind in ('number','integer') and type(value) is bool)):
        raise ValueError('Input type mismatch')
    if schema.get('enum') is not None and value not in schema['enum']:
        raise ValueError('Unexpected enum value')
    if isinstance(value,dict):
        fields = schema.get('properties',{})
        if set(schema.get('required',[]))-value.keys():
            raise ValueError('Missing fields')
        for key, child in value.items():
            if key in fields:
                _check(child,fields[key],definitions,depth+1)
            elif schema.get('additionalProperties') is False:
                raise ValueError('Unknown field')
            elif isinstance(schema.get('additionalProperties'),dict):
                _check(child,schema['additionalProperties'],definitions,depth+1)
    elif isinstance(value,list):
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',8192):
            raise ValueError('Array size outside contract')
        for child in value:
            _check(child,schema.get('items',{}),definitions,depth+1)
    elif isinstance(value,str):
        limit=4*1024*1024 if schema.get('format') in ('binary','byte') else 65536
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',limit):
            raise ValueError('String size outside contract')
    elif type(value) in (int,float):
        if not math.isfinite(value) or value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf):
            raise ValueError('Number outside contract')


def _media(spec, inputs):
    media = inputs.get('media_type',next(iter(spec['media']),None))
    if (media is not None and media not in spec['media']) or ('body' in inputs and media is None):
        raise ValueError('Unsupported body format')
    return media


def validate_inputs(row, inputs):
    spec = row['contract']
    if not isinstance(inputs,dict) or inputs.keys()-{'path','query','body','header','media_type','files'} or set(spec['required'])-inputs.keys():
        raise ValueError('Invalid request fields')
    for group,fields in row.get('required_values',{}).items():
        values=inputs.get(group)
        if not isinstance(values,dict) or any(type(values.get(k)) is not type(v) or values[k]!=v for k,v in fields.items()):
            raise ValueError('Use the documented filter for this operation')
    for kind in ('path','query','header'):
        if kind in inputs:
            if kind not in spec['parameters']:
                raise ValueError('Unexpected parameters')
            _check(inputs[kind],spec['parameters'][kind],spec['definitions'])
    for key,value in inputs.get('path',{}).items():
        if key=='uri' and row['host']==DIGITAL_HOST and row['path']=='/api/v1/content/download/{uri}':
            if (not isinstance(value,str) or len(value)>1024 or any(
                    not re.fullmatch(r'[A-Za-z0-9_.-]{1,255}',part) or part in ('.','..') for part in value.split('/'))):
                raise ValueError('Invalid Digital content URI')
            continue
        if not isinstance(value,(str,int)) or not re.fullmatch(r'[A-Za-z0-9_:@.-]{1,256}',str(value)) or str(value) in ('.','..'):
            raise ValueError('Invalid path segment')
    for name,value in inputs.get('header',{}).items():
        if name.lower() in ('authorization','host','cookie','api-key','client-id','connection','content-length','transfer-encoding',
                            'proxy-authorization','forwarded','content-type') or not re.fullmatch(r'[A-Za-z0-9_-]+',name) or not re.fullmatch(r'[ -~]{1,512}',str(value)):
            raise ValueError('Invalid header')
    media = _media(spec,inputs)
    if 'files' in inputs and (media!='multipart/form-data' or not isinstance(inputs['files'],dict) or not isinstance(inputs.get('body'),dict)
                              or inputs['files'].keys()-inputs['body'].keys()):
        raise ValueError('Invalid file metadata')
    if 'body' in inputs:
        _check(inputs['body'],spec['media'][media],spec['definitions'])
    if ozon_review_reply.is_reply_route(row):
        body = inputs['body']
        if body.keys() - {'review_id','text','mark_review_as_processed'} or body.get('mark_review_as_processed',False) is not False:
            raise ValueError('Only a direct reply without a review status change is supported')
        ozon_review_reply.validate(body['review_id'],body['text'])


def normalized(row, inputs):
    result={k:v for k,v in inputs.items() if k not in ('path','query','header','media_type') or v}
    if 'body' in inputs:
        result['media_type']=_media(row['contract'],inputs)
    else:
        result.pop('media_type',None)
    return [row['market'],row['host'],row['method'],row.get('official_path',row['path']),result]


def validate(tool, arguments):
    _,fields,required = PUBLIC_TOOLS[tool]
    _check(arguments,{'type':'object','properties':fields,'required':required,'additionalProperties':False},{})
    if tool == ozon_review_reply.TOOL:
        ozon_review_reply.validate(arguments['review_id'],arguments['text'])
    for name in ('proposal_id','request_id'):
        if name in arguments and not re.fullmatch(fields[name]['pattern'],arguments[name]):
            raise ValueError('Invalid reference')
    if 'operation_id' not in arguments:
        return
    row = find_route(arguments['operation_id'])
    if row is None:
        raise ValueError('Unknown operation')
    if tool in ('market_api_read','market_api_propose'):
        if row['category'] != ('read' if tool=='market_api_read' else 'write'):
            raise ValueError('Operation not allowed in this mode')
        validate_inputs(row,arguments['input'])


def _wb_access_requirements(row):
    spec = WB_READ_REQUIREMENTS.get(row['operation_id'])
    if not spec:
        return None
    result = dict(required_category=market_health.WB_CATEGORIES[spec['category']],
                  supported_token_types=[WB_TOKEN_TYPES[kind] for kind in spec['token_types']],
                  documentation=spec['documentation'])
    if 'subscription' in spec:
        result['required_subscription'] = spec['subscription']
    return result


def wb_read_access_issue(row, credentials, *, http_status=None):
    spec = WB_READ_REQUIREMENTS.get(row['operation_id'])
    if not spec:
        return None
    claims = market_history.claims(credentials.get('api_token'))
    kind = claims.get('acc')
    token_type = WB_TOKEN_TYPES.get(kind) if type(kind) is int else None
    mask = claims.get('s')
    category = (bool(mask & (1 << market_health.WB_CATEGORY_BITS[spec['category']]))
                if type(mask) is int and mask >= 0 else None)
    expiry = claims.get('exp')
    if http_status is not None:
        reason = spec['denial_reason']
    elif type(expiry) is int and expiry <= time.time():
        reason = 'wb_token_expired'
    elif claims.get('t') is True or (token_type and kind not in spec['token_types']):
        reason = 'wb_token_type_required'
    elif category is False:
        reason = 'wb_token_category_required'
    else:
        return None

    result = dict(status='provider_access_denied', reason=reason, source=row['path'],
                  http_status=http_status, credential_type=token_type, category_allowed_by_token=category,
                  **_wb_access_requirements(row))
    if 'subscription' in spec:
        result['subscription_status'] = 'unverified'
    return result


def select_wb_read_credentials(row, dedicated, general):
    if (not dedicated or not general or not wb_read_access_issue(row, dedicated)
            or wb_read_access_issue(row, general)):
        return dedicated or general
    # Never fall back to another cabinet for a read of private seller data.
    seller = market_history.claims(dedicated.get('api_token')).get('sid')
    return general if seller and seller == market_history.claims(general.get('api_token')).get('sid') else dedicated


def read_credentials(vault,row):
    if row['market']=='ozon':
        return vault.read('ozon')
    if row['host']==DIGITAL_HOST:
        return {'public_catalog':True} if row.get('authentication')=='public_catalog' else vault.read('wb_digital')
    category = category_key(row['host'])
    dedicated = vault.read(category) if category else None
    if dedicated and row['operation_id'] not in WB_READ_REQUIREMENTS:
        return dedicated
    return select_wb_read_credentials(row,dedicated,vault.read('wb'))


def _scalar(value):
    if type(value) is bool:
        return str(value).lower()
    if isinstance(value,(int,float,str)):
        return str(value)
    raise ValueError('Expected scalar')


def _multipart(body,schema,defs,files):
    from email.message import EmailMessage
    boundary = 'sidzen'+secrets.token_hex(16)
    parts = []
    while '$ref' in schema:
        schema = defs[schema['$ref'].split('/')[-1]]
    for field,values in body.items():
        if not re.fullmatch(r'[\w.-]+',field):
            raise ValueError('Invalid form field')
        prop = schema.get('properties',{}).get(field,{})
        items=values if isinstance(values,list) else [values]
        meta=files.get(field,[{} for _ in items])
        if isinstance(meta,dict):
            meta=[meta]
        if not isinstance(meta,list) or len(meta)!=len(items):
            raise ValueError('Invalid multipart file count')
        for value,info in zip(items,meta):
            content = EmailMessage()
            binary = prop.get('items',prop).get('format') in ('binary','byte')
            if not isinstance(info,dict) or info.keys()-{'name','type'} or (info and not binary):
                raise ValueError('Invalid multipart metadata')
            filename=info.get('name','upload.bin')
            mime=info.get('type','application/octet-stream')
            if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_. -]{0,120}',filename) or not isinstance(mime,str) or not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+',mime):
                raise ValueError('Invalid multipart filename or media type')
            header = 'form-data; name="'+field+'"'+('; filename="'+filename+'"' if binary else '')
            content['Content-Disposition'] = header
            data = base64.b64decode(value,validate=True) if binary else _scalar(value).encode()
            parts.append(b'--'+boundary.encode()+b'\r\nContent-Disposition: '+str(content['Content-Disposition']).encode('ascii')+
                         (b'\r\nContent-Type: '+mime.encode() if binary else b'')+b'\r\n\r\n'+data+b'\r\n')
    return b''.join(parts)+b'--'+boundary.encode()+b'--\r\n', 'multipart/form-data; boundary='+boundary


def build_request(row, inputs, credentials):
    digital = row['market']=='wb' and row['host']==DIGITAL_HOST
    if not (digital or (row['market']=='ozon' and row['host']==market_transport.SELLER) or
            (row['market']=='wb' and row['host'] in WB_SELLER_HOSTS)):
        raise ValueError('Unrecognised seller credential destination')
    validate_inputs(row,inputs)
    path = row.get('official_path',row['path'])
    for key,value in inputs.get('path',{}).items():
        # Digital returns a volume-relative file path, for example vol19/924/.../content.
        safe='/' if digital and path=='/api/v1/content/download/{uri}' and key=='uri' else ''
        path = path.replace('{'+key+'}',quote(_scalar(value),safe=safe))
    if '{' in path or '}' in path or not path.startswith('/') or path.startswith('//'):
        raise ValueError('Incomplete route')
    headers = {k:_scalar(v) for k,v in inputs.get('header',{}).items()}
    if digital and row['path']=='/api/v1/content/download/{uri}':
        requested=headers.setdefault('Range',f'bytes=0-{MAX_BYTES-1}')
        match=re.fullmatch(r'bytes=([0-9]{1,19})-([0-9]{1,19})',requested)
        if not match or not 0<=int(match[2])-int(match[1])<MAX_BYTES:
            raise ValueError('Digital download range exceeds the MCP response limit')
    if row['market']=='ozon':
        headers.update({'Client-Id':credentials['client_id'],'Api-Key':credentials['api_key']})
    elif digital:
        if row.get('authentication')=='public_catalog':
            if (row['method'],path,credentials)!=('GET','/api/v1/catalog',{'public_catalog':True}):
                raise ValueError('Invalid public Digital request')
        elif row.get('authentication')=='wb_digital' and set(credentials)=={'digital_token'}:
            headers['Authorization']='Bearer '+credentials['digital_token']
        else:
            raise ValueError('Dedicated WB Digital credential required')
    else:
        headers['Authorization'] = credentials['api_token']
    spec = row['contract']
    query = []
    for key,value in inputs.get('query',{}).items():
        style,explode = spec['query_styles'][key]
        if isinstance(value,list):
            values = [_scalar(v) for v in value]
            if style=='form' and explode:
                query.extend((key,v) for v in values)
            else:
                query.append((key,{'spaceDelimited':' ','pipeDelimited':'|'}.get(style,',').join(values)))
        else:
            query.append((key,_scalar(value)))
    url = 'https://'+row['host']+path+('?' + urlencode(query) if query else '')
    data = None
    if 'body' in inputs:
        body,media = inputs['body'],_media(spec,inputs)
        if media=='application/json' or media.endswith('+json'):
            data = json.dumps(body,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
        elif media=='multipart/form-data':
            data,media = _multipart(body,spec['media'][media],spec['definitions'],inputs.get('files',{}))
        elif media=='application/x-www-form-urlencoded':
            data = urlencode(body,doseq=True).encode()
        else:
            data = base64.b64decode(body,validate=True)
        headers['Content-Type'] = media
    return urllib.request.Request(url,data=data,method=row['method'],headers=headers)


def transfer(tool, arguments, creds, directory, deadline, *, csv_download=False):
    validate(tool,arguments)
    row = find_route(arguments['operation_id'])
    inputs = arguments['input']
    if csv_download and (tool != 'market_api_read' or row['operation_id'] != 'wb_get_api_nm_report_downloads_file_downloadid'):
        raise ValueError('Unexpected report download')
    if tool == 'market_api_propose' and ozon_review_reply.is_reply_route(row):
        wire = market_transport.Wire('ozon',creds,directory,deadline)
        body = dict(inputs['body'],mark_review_as_processed=False)

        def comments(method,url,**params):
            data = wire(method,url,**params)
            if creds['api_key'] in json.dumps(data,ensure_ascii=False,default=str):
                raise finance_gate.ProviderResponseInvalid('Unsafe marketplace response')
            return data

        def submit():
            # Leave time for a fresh comment read even when the create socket times out.
            create_deadline = min(deadline - 5,time.monotonic() + 4)
            return _transfer(tool,row,dict(inputs,body=body),creds,directory,create_deadline)

        return ozon_review_reply.publish(comments,submit,body['review_id'],body['text'])
    return _transfer(tool,row,inputs,creds,directory,deadline,csv_download=csv_download)


def _transfer(tool, row, inputs, creds, directory, deadline, *, csv_download=False):
    restricted_read = tool == 'market_api_read' and row['operation_id'] in WB_READ_REQUIREMENTS

    def access_denied(issue):
        return dict(operation_id=row['operation_id'], status=issue['status'], http_status=issue['http_status'],
                    access_issue=issue, data=None, complete=False, read_only=True)

    if restricted_read and (issue := wb_read_access_issue(row, creds)):
        return access_denied(issue)
    request = build_request(row,inputs,creds)
    wire = market_transport.Wire(row['market'],creds,directory,deadline)
    if csv_download:
        cache_key = wire._key('GET', request.full_url, None, None)
        cached = wire._http_cache(cache_key, binary=True)
        if cached is not None:
            observed = wire.fetch_times[-1]
            return cached, datetime.fromtimestamp(observed,timezone.utc).isoformat() if observed else None
    token=creds.get('digital_token') or creds.get('api_token') or creds.get('api_key')
    interval=row.get('interval_seconds',{})
    saved_jobs=('/api/v1/warehouse_remains','/api/v1/paid_storage','/api/v1/acceptance_report')
    ozon_act = (tool == 'market_api_read' and row['host'] == market_transport.SELLER
                and row['path'] in market_transport.OZON_ACT_METHODS)
    wb_report = row['host'] == market_transport.WB_FEED_HOST and row['path'] in (*saved_jobs, market_transport.WB_FEED_PATH)
    if ozon_act or wb_report:
        job=row['path'] in saved_jobs
        query={k:_scalar(v) for k,v in inputs.get('query',{}).items()}
        value=wire(row['method'],'https://'+row['host']+row['path'],body=inputs.get('body'),
                   query=query or None,interval=interval.get('personal',0.1 if ozon_act else 60),
                   basic_interval=interval.get('basic',10800),
                   ttl=1800 if job else 0,job=job)
        clean=json.loads(json.dumps(_project(value),ensure_ascii=False,default=str))
        secrets = (token,) if ozon_act else creds.values()
        encoded = json.dumps(clean,ensure_ascii=False)
        if any(json.dumps(v,ensure_ascii=False)[1:-1] in encoded for v in secrets if isinstance(v,str) and v):
            raise finance_gate.ProviderResponseInvalid('Unsafe marketplace data')
        result={'operation_id':row['operation_id'],'data':clean,'source':'marketplace_or_confirmed_task',
                'observed_at':datetime.now(timezone.utc).isoformat()}
        if ozon_act:
            result.update(http_status=200, fetched_at=datetime.fromtimestamp(wire.fetch_times[-1], timezone.utc).isoformat())
        if job:
            result.update(operation_status='accepted',asynchronous=True)
        return result
    value=interval.get('personal' if market_transport._wb_personal(creds.get('api_token','')) else 'basic',0)
    def prepare():
        if value:
            wire.admit_catalog(row['host'],row.get('official_path',row['path']),row['quota_group'],value,read_recovery=tool=='market_api_read')
    def open_request():
        remaining = deadline-time.monotonic()
        if remaining<0.2:
            raise finance_gate.LocalPreparationTimeout('Deadline expired')
        return wire.opener.open(request,timeout=min(10,remaining))
    def paced():
        # The shared quota guard accepts the vendor token under api_token. Only
        # the HTTP builder uses the dedicated Digital credential and audience.
        guard_credentials={'api_token':token} if row['host']==DIGITAL_HOST and token else creds
        return market_cooldown.request(directory,row['market'],guard_credentials,request,open_request,
                                       before_send=prepare,read_recovery=tool=='market_api_read')
    # The original Ozon finance reader shares an account-wide gate across
    # endpoints. A new route must observe its outstanding Retry-After too.
    use_finance = row['market']=='ozon' and ('/finance/' in row['path'] or '/report/realization/' in row['path'])
    def send():
        if use_finance:
            return finance_gate.run(directory,creds['client_id'],paced,(creds['api_key'],),read_recovery=tool=='market_api_read')
        if row['market']=='wb' and ('{' in row['path'] or row.get('quota_group','').startswith('wb-group-')):
            group=row['host']+':'+row.get('quota_group',row['path'])
            return finance_gate.run(str(Path(directory).parent/'.catalog-method-gates'),group,paced,(token,),read_recovery=tool=='market_api_read')
        return paced()
    try:
        response = send()
    except urllib.error.HTTPError as exc:
        if not restricted_read or exc.code not in (402, 403):
            raise
        exc.close()
        return access_denied(wb_read_access_issue(row, creds, http_status=exc.code))
    with response:
        status = response.status
        media = response.headers.get_content_type()
        content_range=response.headers.get('Content-Range','')
        budget = market_transport.MAX_REPORT if csv_download else MAX_BYTES
        raw = response.read(budget+1)
    result = {'operation_id':row['operation_id'],'http_status':status,'fetched_at':datetime.now(timezone.utc).isoformat()}
    if (row['host']==DIGITAL_HOST and row['path']=='/api/v1/content/download/{uri}'
            and re.fullmatch(r'bytes [0-9]{1,19}-[0-9]{1,19}/(?:[0-9]{1,19}|\*)',content_range)):
        result['content_range']=content_range
    # A provider acknowledgement must survive a large, binary or malformed
    # write response. It can be asynchronous; acceptance is not fulfilment.
    if tool=='market_api_propose':
        result.update(provider_acknowledged=True,operation_status='unknown')
    if len(raw)>budget:
        if tool=='market_api_propose':
            return dict(result,response_truncated=True)
        raise finance_gate.ProviderResponseTooLarge('Use smaller page or period')
    if raw and token and token.encode() in raw:
        raise finance_gate.ProviderResponseInvalid('Unsafe marketplace response')
    if csv_download:
        if status != 200 or media not in ('application/zip','application/octet-stream','text/csv'):
            raise finance_gate.ProviderResponseInvalid('Unexpected CSV response')
        wire.store(cache_key, raw, 900, fetched=time.time())
        return raw,result['fetched_at']
    if not raw:
        result['data'] = None
    elif media=='application/json' or media.endswith('+json') or raw.lstrip().startswith((b'{',b'[')):
        try:
            value = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),object_pairs_hook=market_transport._no_duplicates)
        except (ValueError,UnicodeError) as exc:
            if tool=='market_api_propose':
                return dict(result,response_available=False)
            raise finance_gate.ProviderResponseInvalid('Invalid JSON') from exc
        data=_project(value)
        if row.get('restricted_data')=='activation_keys' and isinstance(data,dict):
            for item in data.get('items') or []:
                if isinstance(item,dict):
                    item.pop('value',None)
        encoded=json.dumps(data,ensure_ascii=False,allow_nan=False)
        if token and json.dumps(token,ensure_ascii=False)[1:-1] in encoded:
            raise finance_gate.ProviderResponseInvalid('Unsafe marketplace data')
        result['data'] = data
    else:
        result.update(content_type=media,size_bytes=len(raw),data_base64=base64.b64encode(raw).decode())
    if tool=='market_api_propose':
        data=result.get('data')
        rejected=isinstance(data,dict) and (data.get('error') is True or data.get('success') is False)
        result['operation_status']=('denied' if rejected else 'unknown' if status==207 or _has_errors(data) else 'accepted')
        result['asynchronous']=(status==202 or row['effect'] in ('report_job','report_generation','document_generation','service_generation')
                                or isinstance(data,dict) and any(k in data for k in ('task_id','taskId')))
    return result


def _has_errors(value):
    if isinstance(value,list):
        return any(_has_errors(x) for x in value)
    if isinstance(value,dict):
        return any((k.casefold() in ('errors','error','failed','errorlist') and v) or _has_errors(v) for k,v in value.items())
    return False


def _project(value):
    if isinstance(value,list):
        return [_project(x) for x in value]
    if isinstance(value,dict):
        return {k:_project(v) for k,v in value.items()
                if re.sub(r'[^a-z0-9]','',k.casefold()) not in ('apikey','clientsecret','authorization','refreshtoken','accesstoken','password')}
    return value
