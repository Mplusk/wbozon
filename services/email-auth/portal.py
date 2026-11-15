"""Local integration prototype. Browser sessions never authenticate the read API."""
from ui import document, CSP
import hashlib, hmac, html, json, math, os, re, secrets, sqlite3, ssl, subprocess, sys, urllib.request, urllib.error, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app import LoginApp, smtp_mailer

FIELDS = {'ozon': ('client_id', 'api_key'), 'wb': ('api_token',)}
CHECK_OPERATIONS = {'ozon':'ozon_products', 'wb':'wb_prices'}
CHECK_PROVIDER_LIMIT, CHECK_PROVIDER_WINDOW = 1, 60
CHECK_GLOBAL_LIMIT, CHECK_GLOBAL_WINDOW = 10, 3600
CHECK_MESSAGES = {
    'ok':'Проверка чтения данных выполнена успешно.',
    'denied':'Маркетплейс отклонил доступ. Проверьте ключ, срок действия и права чтения.',
    'rate_limited':'Маркетплейс временно ограничил запросы. Повторите проверку позже.',
    'unavailable':'Маркетплейс временно недоступен. Повторите проверку позже.',
    'failed':'Не удалось подтвердить соединение. Проверьте настройки и повторите позже.',
}
BRIDGE_OPERATIONS = {'ozon_products':'товары Ozon', 'wb_prices':'цены Wildberries', 'unknown':'операция не определена'}
BRIDGE_OUTCOMES = {'ok':'данные получены', 'not_configured':'ключ не добавлен', 'failed':'не удалось получить данные', 'rate_limited':'превышен лимит запросов'}

class Vault:
    def __init__(self, path, key):
        if len(key) != 32: raise ValueError('32-byte vault key required')
        self.path, self.aead = path, AESGCM(key)
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS connection_checks(provider TEXT PRIMARY KEY, version TEXT NOT NULL, outcome TEXT NOT NULL, checked_at REAL NOT NULL)')
            c.execute("CREATE TABLE IF NOT EXISTS bridge_activity(source TEXT PRIMARY KEY CHECK(source IN ('bridge','mcp')), operation TEXT NOT NULL, outcome TEXT NOT NULL, completed_at REAL NOT NULL)")
    def save(self, provider, values):
        nonce = secrets.token_bytes(12)
        blob = nonce + self.aead.encrypt(nonce, json.dumps(values).encode(), provider.encode())
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', (provider, blob))
            c.execute('DELETE FROM connection_checks WHERE provider=?',(provider,))
    def snapshot(self, provider):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row = c.execute('SELECT blob FROM vault WHERE provider=?', (provider,)).fetchone()
        if not row: return None
        blob = bytes(row[0])
        values=json.loads(self.aead.decrypt(blob[:12], blob[12:], provider.encode()))
        return values, hashlib.sha256(blob).hexdigest()
    def read(self, provider):
        snapshot=self.snapshot(provider)
        return snapshot[0] if snapshot else None
    def record_check(self, provider, version, outcome, checked_at):
        if outcome not in CHECK_MESSAGES: raise ValueError('Invalid outcome')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
            if not row or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version): return False
            c.execute('INSERT OR REPLACE INTO connection_checks VALUES (?,?,?,?)',(provider,version,outcome,checked_at))
        return True
    def version_matches(self, provider, version):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
        return bool(row) and hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version)
    def check_status(self, provider):
        # Read storage metadata only; rendering never decrypts credentials.
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT v.blob,c.version,c.outcome,c.checked_at FROM vault v JOIN connection_checks c ON v.provider=c.provider WHERE v.provider=?',(provider,)).fetchone()
        if not row or row[2] not in CHECK_MESSAGES or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),row[1]): return None
        return row[2],row[3]
    def present(self):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            return {r[0] for r in c.execute('SELECT provider FROM vault')}
    def record_bridge_activity(self, source, operation, outcome, completed_at):
        if source not in ('bridge','mcp') or operation not in BRIDGE_OPERATIONS or outcome not in BRIDGE_OUTCOMES: raise ValueError('Invalid bridge activity')
        # A delayed writer must not overwrite a more recently completed request.
        with sqlite3.connect(self.path, timeout=0.5) as c:
            c.execute('INSERT INTO bridge_activity VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET operation=excluded.operation, outcome=excluded.outcome, completed_at=excluded.completed_at WHERE excluded.completed_at >= bridge_activity.completed_at', (source,operation,outcome,completed_at))
    def bridge_activity(self, source):
        if source not in ('bridge','mcp'): raise ValueError('Invalid bridge source')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row=c.execute('SELECT operation,outcome,completed_at FROM bridge_activity WHERE source=?',(source,)).fetchone()
        if row and (row[0] not in BRIDGE_OPERATIONS or row[1] not in BRIDGE_OUTCOMES): raise ValueError('Invalid bridge activity')
        return row

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def decode_provider_payload(raw):
    """Reject non-JSON constants and numeric overflow before normalizing data."""
    def reject_constant(value): raise ValueError('Invalid provider JSON number')
    def finite_float(value):
        number=float(value)
        if not math.isfinite(number): raise ValueError('Invalid provider JSON number')
        return number
    payload=json.loads(raw,parse_constant=reject_constant,parse_float=finite_float)
    if not isinstance(payload,dict): raise ValueError('Invalid provider response')
    return payload

def _provider_read(operation, args, creds):
    limit = args.get('limit', 20)
    allowed={'limit','nm_id'} if operation=='wb_prices' else {'limit'}
    nm_id=args.get('nm_id')
    if type(limit) is not int or not 1 <= limit <= 100 or set(args)-allowed:
        raise ValueError('Invalid arguments')
    if 'nm_id' in args and (type(nm_id) is not int or not 1<=nm_id<=9223372036854775807 or limit!=1):
        raise ValueError('Invalid WB article')
    if operation == 'ozon_products':
        req = urllib.request.Request('https://api-seller.ozon.ru/v3/product/list',
            data=json.dumps({'filter': {'visibility':'ALL'}, 'limit':limit, 'last_id':''}).encode(),
            headers={'Client-Id':creds['client_id'], 'Api-Key':creds['api_key'], 'Content-Type':'application/json'}, method='POST')
    elif operation == 'wb_prices':
        req = urllib.request.Request('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit='+str(limit)+'&offset=0'+('&filterNmID='+str(nm_id) if nm_id is not None else ''),
            headers={'Authorization':creds['api_token']}, method='GET')
    else: raise ValueError('Unknown operation')
    opener=urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=15) as resp:
        raw=resp.read(512001)
        if len(raw)>512000: raise ValueError('Response too large')
        data=decode_provider_payload(raw)
    if operation=='ozon_products':
        rows=data.get('result',{}).get('items')
        if not isinstance(rows,list): raise ValueError('Unexpected schema')
        keys=('product_id','offer_id','archived','is_fbo_visible','is_fbs_visible')
    else:
        # WB reports an error flag in its envelope; data must not override it.
        if ('error' in data and data['error'] is not False) or data.get('errorText') not in (None,''):
            raise ValueError('Provider reported an error')
        rows=data.get('data',{}).get('listGoods')
        if not isinstance(rows,list): raise ValueError('Unexpected schema')
        keys=('nmID','vendorCode','discount','clubDiscount','currencyIsoCode4217')
    values=list(creds.values())
    def clean(v):
        if isinstance(v,str):
            for secret in values: v=v.replace(secret,'[REDACTED]')
            return v[:256]
        return v if type(v) in (int,float,bool) or v is None else None
    out=[]
    for row in rows[:limit]:
        if not isinstance(row,dict): raise ValueError('Unexpected row')
        item={k:clean(row[k]) for k in keys if k in row}
        if operation=='wb_prices':
            sizes=row.get('sizes',[])
            if not isinstance(sizes,list): raise ValueError('Unexpected sizes')
            item['sizes']=[{k:clean(s[k]) for k in ('sizeID','techSizeName','price','discountedPrice','clubDiscountedPrice') if k in s} for s in sizes[:100] if isinstance(s,dict)]
        out.append(item)
    return {'items':out, 'first_page_only':True, 'untrusted_data':True, 'read_only':True}

PROVIDER_DEADLINE_SECONDS = 20
PROVIDER_WORKER = os.path.realpath(__file__)

def provider_read(operation, args, creds):
    """Bound the entire provider operation, including DNS and response reads.

    Credentials go over a private stdin pipe, never argv or the environment.
    Killing the subprocess on timeout also stops a stuck resolver/socket call.
    """
    env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
    # Preserve standard outbound network settings; never pass unrelated app secrets.
    for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HTTPS_PROXY', 'HTTP_PROXY',
                 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy',
                 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
        if name in os.environ: env[name] = os.environ[name]
    try:
        process = subprocess.run([sys.executable, PROVIDER_WORKER, '--provider-worker'],
            input=json.dumps({'operation':operation, 'args':args, 'creds':creds}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PROVIDER_DEADLINE_SECONDS,
            env=env, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError('Marketplace request timed out') from None
    if process.returncode:
        raise urllib.error.URLError('Marketplace worker failed')
    try:
        result=json.loads(process.stdout)
        if result.get('kind') == 'http' and type(result.get('status')) is int:
            raise urllib.error.HTTPError('', result['status'], 'Marketplace request failed', {}, None)
        if result.get('kind') == 'unavailable': raise urllib.error.URLError('Marketplace unavailable')
        if result.get('kind') != 'ok' or not isinstance(result.get('result'),dict):
            raise ValueError('Marketplace response failed')
        return result['result']
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        raise ValueError('Invalid marketplace worker response') from None

def _provider_worker():
    """Private child entry point; exceptions and upstream bodies are not logged."""
    try:
        request=json.load(sys.stdin)
        result={'kind':'ok', 'result':_provider_read(request['operation'],request['args'],request['creds'])}
    except urllib.error.HTTPError as exc:
        result={'kind':'http', 'status':exc.code}
        exc.close()
    except (TimeoutError,urllib.error.URLError):
        result={'kind':'unavailable'}
    except Exception:
        result={'kind':'failed'}
    sys.stdout.write(json.dumps(result,ensure_ascii=True,allow_nan=False))

def render_preview(provider, result, fetched_at, sensitive_values=(), lookup_nm_id=None):
    """Render allowlisted normalized data only; upstream strings are untrusted text."""
    rows=result.get('items')
    if not isinstance(rows,list): raise ValueError('Invalid items')
    if lookup_nm_id is not None and (provider!='wb' or type(lookup_nm_id) is not int or not 1<=lookup_nm_id<=9223372036854775807): raise ValueError('Invalid WB article')
    title='Товар Wildberries по артикулу' if lookup_nm_id is not None else 'Первые товары Ozon' if provider=='ozon' else 'Первые цены Wildberries'
    stamp=datetime.fromtimestamp(fetched_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    def text(value):
        if value is None: return 'не указано'
        if type(value) is bool: return 'да' if value else 'нет'
        if not isinstance(value,(str,int,float)): return 'не указано'
        rendered=str(value)
        for secret in sensitive_values:
            if isinstance(secret,str) and secret: rendered=rendered.replace(secret,'[REDACTED]')
        return html.escape(rendered[:256],quote=True)
    def field_text(key,value):
        if key=='currencyIsoCode4217':
            # Display the provider's code verbatim, never assume RUB or convert prices.
            if not isinstance(value,str) or not re.fullmatch(r'[A-Z]{3}',value): return 'не указано'
        if key in ('price','discountedPrice','clubDiscountedPrice','discount','clubDiscount'):
            # WB prices are JSON numbers, not truth values or numeric strings.
            # Reject nonfinite values accepted by Python's permissive JSON parser.
            if type(value) not in (int,float): return 'не указано'
            if type(value) is float and not math.isfinite(value): return 'не указано'
            if value<0 or (key in ('discount','clubDiscount') and value>100): return 'не указано'
        return text(value)
    def fields(row,labels):
        return '<dl>'+''.join('<dt>'+label+'</dt><dd>'+field_text(key,row.get(key))+'</dd>' for key,label in labels)+'</dl>'
    content='<main id="content"><p class="eyebrow">Данные маркетплейса</p><h1>'+title+'</h1><p>Получено: '+stamp+'.</p>'
    if lookup_nm_id is not None:
        content+='<p>Запрошен артикул Wildberries: '+str(lookup_nm_id)+'. Показан ответ только по этому артикулу, до одного товара. Это не отчёт об остатках или продажах. Данные только для чтения и могут измениться после запроса.</p>'
    else:
        content+='<p>Только первая страница, до 20 товаров. Это не полный ассортимент и не отчёт об остатках или продажах. Данные отображаются только для чтения; значения могут измениться после запроса.</p>'
    content+='<p><a href="/settings">Вернуться к настройкам</a></p>'
    if not rows:
        content+='<p>Ответ API не содержит товар с этим артикулом. Проверьте артикул и выбранный кабинет; этот ответ не доказывает удаление товара.</p>' if lookup_nm_id is not None else '<p>В первой странице ответа нет товаров. Это не доказывает отсутствие всех товаров в кабинете.</p>'
    content+='<div class="data-grid">'
    for index,row in enumerate(rows[:20],1):
        if not isinstance(row,dict): raise ValueError('Invalid row')
        content+='<section><h2>Товар '+str(index)+'</h2>'
        if provider=='ozon':
            content+=fields(row,(('product_id','ID товара'),('offer_id','Артикул продавца'),('archived','В архиве'),('is_fbo_visible','Видимость FBO по API'),('is_fbs_visible','Видимость FBS по API')))
        else:
            content+=fields(row,(('nmID','Артикул Wildberries'),('vendorCode','Артикул продавца'),('discount','Скидка по API, %'),('clubDiscount','Скидка WB Клуба по API, %'),('currencyIsoCode4217','Код валюты из API')))
            sizes=row.get('sizes',[])
            if not isinstance(sizes,list): raise ValueError('Invalid sizes')
            content+='<p>Цены приведены без пересчёта, в валюте указанного кода из API. Если код не указан, валюту нужно уточнить в кабинете; рубли по умолчанию не подставляются. Показано не более 10 размеров на товар.</p>'
            if not sizes: content+='<p>Информация о ценах и размерах не получена.</p>'
            for size in sizes[:10]:
                if not isinstance(size,dict): raise ValueError('Invalid size')
                content+=fields(size,(('sizeID','ID размера'),('techSizeName','Размер'),('price','Цена'),('discountedPrice','Цена со скидкой'),('clubDiscountedPrice','Цена для WB Клуба')))
        content+='</section>'
    content+='</div></main>'
    return document(title,content)

class Portal:
    def __init__(self, login, vault, read_token_hash, gateway=provider_read, mcp_read_token_hash=None):
        if not re.fullmatch(r'[0-9a-f]{64}',read_token_hash): raise ValueError('Expected SHA256 token hash')
        if mcp_read_token_hash is not None and (not re.fullmatch(r'[0-9a-f]{64}',mcp_read_token_hash) or hmac.compare_digest(read_token_hash,mcp_read_token_hash)):
            raise ValueError('Expected distinct SHA256 MCP token hash')
        self.login,self.vault,self.token_hash,self.gateway=login,vault,read_token_hash,gateway
        self.mcp_token_hash=mcp_read_token_hash
    def request_budget(self, provider, now):
        """Read-only local limiter snapshot. It neither reserves nor grants a request."""
        if provider not in CHECK_OPERATIONS: raise ValueError('Invalid provider')
        with self.login.connect() as c:
            c.execute('BEGIN')
            local=[row[0] for row in c.execute(
                'SELECT at FROM attempts WHERE bucket=? AND at>? ORDER BY at DESC LIMIT ?',
                ('connection-check:'+provider,now-CHECK_PROVIDER_WINDOW,CHECK_PROVIDER_LIMIT))]
            shared=[row[0] for row in c.execute(
                'SELECT at FROM attempts WHERE bucket=? AND at>? ORDER BY at DESC LIMIT ?',
                ('connection-check:all',now-CHECK_GLOBAL_WINDOW,CHECK_GLOBAL_LIMIT))]
        ready_at=now
        if len(local)==CHECK_PROVIDER_LIMIT: ready_at=max(ready_at,local[-1]+CHECK_PROVIDER_WINDOW)
        if len(shared)==CHECK_GLOBAL_LIMIT: ready_at=max(ready_at,shared[-1]+CHECK_GLOBAL_WINDOW)
        return {'retry_seconds':math.ceil(ready_at-now),
                'hourly_remaining':CHECK_GLOBAL_LIMIT-len(shared)}

    def __call__(self,e,start):
        path=e.get('PATH_INFO','/'); method=e.get('REQUEST_METHOD','GET')
        if path == '/' and method == 'GET' and e.get('HTTP_HOST') == urlsplit(self.login.origin).netloc:
            sid=self.login.cookies(e).get('__Host-sid','')
            if re.fullmatch(r'[A-Za-z0-9_-]{43}',sid):
                with self.login.connect() as c:
                    present=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                if present:
                    start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path not in ('/settings','/settings/check','/settings/preview','/settings/wb-lookup','/api/read','/healthz'):
            return self.login(e,start)
        def reply(text,code='200 OK',json_response=False):
            if not json_response and not text.lstrip().lower().startswith('<!doctype'): text=document('Кабинет', '<main id="content" class="error-card"><h1>Кабинет</h1><p>'+text+'</p></main>')
            body=(json.dumps(text) if json_response else text).encode()
            start(code,[('Content-Type','application/json' if json_response else 'text/html; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('Referrer-Policy','no-referrer' if json_response else 'strict-origin'),('X-Content-Type-Options','nosniff'),('Content-Security-Policy',CSP)]); return [body]
        if e.get('HTTP_HOST')!=urlsplit(self.login.origin).netloc: return reply('Invalid host','400 Bad Request')
        if path=='/healthz':
            # Liveness only: no session, credential, database, SMTP or seller access.
            # This does not assert that downstream integrations are configured/healthy.
            status='200 OK' if method in ('GET','HEAD') else '405 Method Not Allowed'
            payload=b'{"status":"ok"}' if status=='200 OK' else b'{"error":"method"}'
            headers=[('Content-Type','application/json'),('Content-Length',str(len(payload))),
                     ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]
            if status!='200 OK': headers.append(('Allow','GET, HEAD'))
            start(status,headers)
            return [b''] if method=='HEAD' else [payload]
        def body(kind):
            if e.get('CONTENT_TYPE','').split(';')[0]!=kind: raise ValueError()
            n=int(e.get('CONTENT_LENGTH','0'))
            if not 0<n<=4096: raise ValueError()
            raw=e['wsgi.input'].read(n)
            if len(raw)!=n: raise ValueError()
            return raw.decode('utf-8')
        if path=='/api/read':
            if method!='POST': return reply({'error':'method'},'405 Method Not Allowed',True)
            # Browser cookies, Origin and Sites identity header confer no API authority.
            auth=e.get('HTTP_AUTHORIZATION','')
            if not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43,128}',auth):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            supplied_hash=hashlib.sha256(auth[7:].encode()).hexdigest()
            bridge_authorized=hmac.compare_digest(supplied_hash,self.token_hash)
            mcp_authorized=hmac.compare_digest(supplied_hash,self.mcp_token_hash or '0'*64)
            if not (bridge_authorized or (self.mcp_token_hash is not None and mcp_authorized)):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            source='mcp' if self.mcp_token_hash is not None and mcp_authorized else 'bridge'
            def record_bridge(operation,outcome):
                # Observability must not change the response of an authorised read.
                try: self.vault.record_bridge_activity(source,operation,outcome,self.login.clock())
                except Exception: pass
            if not self.login.permit('api',30,60):
                # The body has not been validated, so do not trust or record its operation.
                record_bridge('unknown','rate_limited')
                return reply({'error':'rate_limited'},'429 Too Many Requests',True)
            try:
                data=json.loads(body('application/json'))
                if not isinstance(data,dict) or set(data)!={'operation','arguments'}: raise ValueError()
                op=data['operation']; args=data['arguments']
                if op not in ('ozon_products','wb_prices') or not isinstance(args,dict) or set(args)-{'limit'} or type(args.get('limit',20)) is not int or not 1<=args.get('limit',20)<=100: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply({'error':'invalid_request'},'400 Bad Request',True)
            try:
                creds=self.vault.read('ozon' if op=='ozon_products' else 'wb')
                if not creds:
                    record_bridge(op,'not_configured')
                    return reply({'error':'not_configured'},'409 Conflict',True)
                result=self.gateway(op,args,creds)
                record_bridge(op,'ok')
                return reply(result,json_response=True)
            except Exception:
                record_bridge(op,'failed')
                return reply({'error':'read_failed'},'502 Bad Gateway',True)
        if path in ('/settings/check','/settings/preview','/settings/wb-lookup') and method!='POST': return reply('Method not allowed','405 Method Not Allowed')
        if method not in ('GET','POST'): return reply('Method not allowed','405 Method Not Allowed')
        sid=self.login.cookies(e).get('__Host-sid','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',sid): return reply('Войдите заново на главной странице.','401 Unauthorized')
        now=self.login.clock()
        try:
            with self.login.connect() as c:
                if path in ('/settings/check','/settings/preview','/settings/wb-lookup'):
                    c.execute('PRAGMA busy_timeout=500')
                session=c.execute('SELECT expires,csrf FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),now)).fetchone()
        except sqlite3.Error:
            return reply('Сервис временно недоступен. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
        if not session: return reply('Войдите заново на главной странице.','401 Unauthorized')
        if path in ('/settings/check','/settings/preview','/settings/wb-lookup'):
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                lookup=path=='/settings/wb-lookup'
                if set(form)!=({'nm_id','csrf'} if lookup else {'provider','csrf'}) or any(len(v)!=1 for v in form.values()): raise ValueError()
                provider='wb' if lookup else form['provider'][0]; csrf=form['csrf'][0]
                nm_id=None
                if lookup:
                    article=form['nm_id'][0]
                    if not re.fullmatch(r'[1-9][0-9]{0,18}',article) or int(article)>9223372036854775807: raise ValueError()
                    nm_id=int(article)
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if provider not in CHECK_OPERATIONS: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply('Некорректный запрос.','400 Bad Request')
            # Global owner-level buckets persist across workers/sessions and prevent click storms.
            try:
                allowed=self.login.permit('connection-check:'+provider,CHECK_PROVIDER_LIMIT,CHECK_PROVIDER_WINDOW,busy_timeout_ms=500) and self.login.permit('connection-check:all',CHECK_GLOBAL_LIMIT,CHECK_GLOBAL_WINDOW,busy_timeout_ms=500)
            except sqlite3.Error:
                return reply('Проверка временно недоступна. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
            if not allowed:
                return reply('Слишком много запросов. Для проверки или просмотра одного кабинета доступен один запрос в минуту, всего — не более 10 в час. <a href="/settings">Настройки</a>','429 Too Many Requests')
            try:
                snapshot=self.vault.snapshot(provider)
                if not snapshot: return reply('Сначала сохраните ключ. <a href="/settings">Настройки</a>','409 Conflict')
                creds,version=snapshot
            except Exception: return reply('Не удалось прочитать настройки. <a href="/settings">Настройки</a>','503 Service Unavailable')
            try:
                arguments={'limit':20 if path=='/settings/preview' else 1}
                if lookup: arguments['nm_id']=nm_id
                result=self.gateway(CHECK_OPERATIONS[provider],arguments,creds)
                if not isinstance(result,dict) or not isinstance(result.get('items'),list): raise ValueError('Invalid response')
                if lookup and (len(result['items'])>1 or any(not isinstance(row,dict) or type(row.get('nmID')) is not int or row['nmID']!=nm_id for row in result['items'])):
                    raise ValueError('Unexpected article response')
                outcome='ok'
            except urllib.error.HTTPError as exc:
                outcome='denied' if exc.code in (401,403) else 'rate_limited' if exc.code==429 else 'unavailable' if 500<=exc.code<=599 else 'failed'
                exc.close()
            except (TimeoutError,urllib.error.URLError): outcome='unavailable'
            except Exception: outcome='failed'
            if path in ('/settings/preview','/settings/wb-lookup'):
                # Revalidate after the bounded upstream call: logout/expiry or key replacement
                # must not return a snapshot from an old session/account.
                try:
                    with self.login.connect() as c:
                        c.execute('PRAGMA busy_timeout=500')
                        live=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                    unchanged=self.vault.version_matches(provider,version)
                except Exception:
                    return reply('Не удалось проверить доступ к данным. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
                if not live: return reply('Войдите заново на главной странице.','401 Unauthorized')
                if not unchanged:
                    return reply('Ключ изменился во время запроса. Повторите просмотр. <a href="/settings">Настройки</a>','409 Conflict')
                if outcome!='ok': return reply(CHECK_MESSAGES[outcome]+' <a href="/settings">Настройки</a>','502 Bad Gateway')
                try:
                    return reply(render_preview(provider,result,self.login.clock(),creds.values(),lookup_nm_id=nm_id if lookup else None))
                except (ValueError,TypeError,KeyError):
                    return reply('Ответ маркетплейса не удалось отобразить. <a href="/settings">Настройки</a>','502 Bad Gateway')
            try:
                if not self.vault.record_check(provider,version,outcome,self.login.clock()):
                    return reply('Ключ был заменён во время проверки. Проверьте новый ключ. <a href="/settings">Настройки</a>','409 Conflict')
            except Exception: return reply('Не удалось сохранить результат проверки. <a href="/settings">Настройки</a>','503 Service Unavailable')
            # Never render/log upstream payloads or exception strings. PRG prevents refresh resubmission.
            start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]); return [b'']
        if method=='POST':
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            if session[0]-43200 < now-300: return reply('Для изменения ключей войдите заново по письму.','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=6,keep_blank_values=True,strict_parsing=True)
                if any(len(v)!=1 for v in form.values()): raise ValueError()
                form={k:v[0] for k,v in form.items()}
                csrf=form.get('csrf',''); provider=form.get('provider','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if provider not in FIELDS or set(form)!=set(FIELDS[provider])|{'csrf','provider'}: raise ValueError()
                values={k:form[k] for k in FIELDS[provider]}
                if any(not re.fullmatch(r'[!-~]{1,2048}',v) for v in values.values()): raise ValueError()
                if provider=='ozon' and not re.fullmatch(r'[0-9]{1,32}',values['client_id']): raise ValueError()
                self.vault.save(provider,values)
            except (ValueError,TypeError,UnicodeError): return reply('Проверьте введённые значения.','400 Bad Request')
            except Exception: return reply('Не удалось сохранить настройки.','503 Service Unavailable')
            return reply('Ключ сохранён зашифрованно. Соединение ещё не проверено. <a href="/settings">Настройки</a>')
        try:
            configured=self.vault.present()
        except sqlite3.Error:
            return reply('Настройки временно недоступны. Повторите позже.','503 Service Unavailable')
        content='<main id="content"><p class="eyebrow">Интеграции</p><h1>Подключение кабинетов</h1>'
        content+='<p class="intro">Здесь можно сохранить ключи Wildberries и Ozon. Используйте отдельные ключи только с необходимыми правами чтения. Права на изменение товаров, цен и заказов не нужны.</p>'
        content+='<p>Ключи хранятся зашифрованно и не отображаются после сохранения. Сохранение ключа не подтверждает его действительность и не подключает MCP автоматически.</p>'
        content+='<div class="steps" aria-label="Порядок подключения"><div class="step"><b><span>01</span>Добавьте ключ</b>Доступ только на чтение</div><div class="step"><b><span>02</span>Проверьте связь</b>Один безопасный запрос</div><div class="step"><b><span>03</span>Откройте данные</b>Первые товары и цены</div></div>'
        content+='<div class="session-note"><p>Сохранить или заменить ключи можно в течение первых 5 минут после входа по письму. Затем выйдите и запросите новую ссылку. Уже сохранённые ключи останутся на сервере.</p>'
        content+='<form method="post" action="/logout"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Выйти для повторного входа</button></form></div>'
        if session[0]-43200 < now-300:
            content+='<p role="status">Время для изменения ключей истекло. Для сохранения ключей сначала выйдите и получите новую ссылку входа.</p>'
        names={'ozon':'Ozon','wb':'Wildberries'}
        labels={'client_id':'Идентификатор продавца (Client ID)', 'api_key':'API-ключ Ozon', 'api_token':'API-токен Wildberries'}
        content+='<div class="market-grid">'
        for provider,fields in FIELDS.items():
            saved=provider in configured
            content+=f'<section class="market-card {provider}" aria-labelledby="{provider}-title"><div class="market-head"><span class="market-icon" aria-hidden="true">{"O" if provider=="ozon" else "WB"}</span><div><h2 id="{provider}-title">{names[provider]}</h2><small>КАБИНЕТ ПРОДАВЦА</small></div></div>'
            content+='<p class="status '+('status-saved' if saved else '')+'">Ключ: '+('сохранён зашифрованно' if saved else 'не добавлен')+'.</p>'
            try:
                check=self.vault.check_status(provider) if saved else None
            except sqlite3.Error:
                return reply('Настройки временно недоступны. Повторите позже.','503 Service Unavailable')
            if check:
                outcome,checked_at=check
                stamp=datetime.fromtimestamp(checked_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                scope='список товаров Ozon' if provider=='ozon' else 'цены и скидки Wildberries'
                content+='<p>Последняя проверка: '+stamp+'. '+CHECK_MESSAGES[outcome]+'</p>'
                content+='<p>Область проверки — только операция чтения: '+scope+'. Это результат на указанное время, не подтверждение всех прав или подключения MCP.</p>'
            else:
                content+='<p class="status">Соединение с маркетплейсом: не проверено. '+('Срок действия и права ключа пока не подтверждены.' if saved else 'Сначала добавьте ключ.')+'</p>'
            if saved:
                content+='<details class="details"><summary>Лимиты запросов и условия проверки</summary>'
                try:
                    budget=self.request_budget(provider,self.login.clock())
                    content+='<p>Локальный лимит портала на момент загрузки страницы: осталось до '+str(budget['hourly_remaining'])+' из '+str(CHECK_GLOBAL_LIMIT)+' запросов за скользящий час для обоих кабинетов вместе.</p>'
                    if budget['retry_seconds']:
                        content+='<p>Следующая попытка для этого кабинета по локальному лимиту — не раньше чем через '+str(budget['retry_seconds'])+' с после загрузки этой страницы.</p>'
                    else:
                        content+='<p>Локальный лимит сейчас допускает попытку запроса.</p>'
                    content+='<p>Это не резервирование запроса и не гарантия доступности API. Другая вкладка может израсходовать лимит. <a href="/settings">Обновить сведения о лимите</a>. Обновление страницы не обращается к маркетплейсам.</p>'
                except (sqlite3.Error,ValueError,TypeError,OverflowError):
                    content+='<p>Не удалось оценить локальный лимит. Он будет проверен при отправке запроса. <a href="/settings">Обновить страницу</a>.</p>'
                content+='</details>'
                content+='<form class="secondary" method="post" action="/settings/check"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Проверить подключение</button></form>'
                content+='<p>Проверка прочитает до одной записи через API маркетплейса, не меняя товары, цены или заказы.</p>'
                label='Показать первые товары Ozon' if provider=='ozon' else 'Показать первые цены Wildberries'
                content+='<form class="secondary" method="post" action="/settings/preview"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>'+label+'</button></form>'
                content+='<p>Просмотр загрузит только первую страницу, до 20 товаров. Данные не сохраняются в кабинете. Проверка и просмотр используют общий лимит: один запрос кабинета в минуту, всего не более 10 запросов в час.</p>' 
                if provider=='wb':
                    content+='<form class="save-form" method="post" action="/settings/wb-lookup"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><label for="wb-lookup-id">Артикул Wildberries (nmID), не артикул продавца</label><input id="wb-lookup-id" type="text" name="nm_id" inputmode="numeric" pattern="[1-9][0-9]{0,18}" maxlength="19" required><button>Показать цены этого товара</button></form><p>Поиск читает до одного товара по точному артикулу и использует тот же общий лимит проверки и просмотра. Товары, цены и заказы не изменяются.</p>'
            if provider=='ozon': content+='<p>Введите Client ID и API-ключ из кабинета продавца Ozon. Оба значения нужно сохранить вместе.</p>'
            else: content+='<p>Введите API-токен из кабинета продавца Wildberries с правами чтения цен и скидок.</p>'
            content+='<form class="save-form" method="post" action="/settings"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'">'
            for field in fields:
                ident=provider+'-'+field
                content+=f'<p class="field"><label for="{ident}">{labels[field]}</label><input id="{ident}" type="password" name="{field}" autocomplete="off" required maxlength="2048"></p>'
            content+='<button>'+('Заменить сохранённый ключ' if saved else 'Сохранить ключ')+'</button></form></section>'
        content+='</div><section class="mcp-card" aria-labelledby="mcp-title"><h2 id="mcp-title">Связь с MCP</h2><div>'
        try:
            for source,label in (('mcp','MCP'),('bridge','другого подключённого клиента')):
                activity=self.vault.bridge_activity(source)
                if activity:
                    operation,outcome,completed_at=activity
                    stamp=datetime.fromtimestamp(completed_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                    content+='<p><strong>Последнее обращение '+label+':</strong> '+stamp+' — '+BRIDGE_OPERATIONS[operation]+', '+BRIDGE_OUTCOMES[outcome]+'.</p>'
                else:
                    content+='<p><strong>Обращений '+label+' пока не зафиксировано.</strong></p>'
        except (sqlite3.Error,ValueError,TypeError,OverflowError,OSError):
            content+='<p>Не удалось загрузить сведения об обращениях к API кабинета.</p>'
        content+='<p>Показаны последние завершённые обращения к API кабинета с момента включения учёта. MCP определяется по отдельному ключу доступа и появится здесь, только если интеграция использует этот API; проверки и просмотр в кабинете не учитываются. Это не проверка текущего подключения MCP. <a href="/settings">Обновить сведения</a>.</p></div></section></main>'
        return reply(document('Подключение кабинетов',content,progress=True))

def secret_file(name, binary=False):
    path=os.environ[name]
    with open(path,'rb') as f: raw=f.read(8193)
    if len(raw)>8192: raise ValueError('Invalid secret file')
    return raw if binary else raw.decode().strip()

def file_smtp_mailer(recipient, link):
    sender='no-reply@3dprinta.shop'
    msg=EmailMessage(); msg['From']=sender;msg['To']=recipient;msg['Subject']='Вход в SIDZEN'
    msg.set_content('Ссылка для входа действует 15 минут. Откройте её и подтвердите вход.\n\n'+link)
    with smtplib.SMTP_SSL('smtp.spaceweb.ru',465,timeout=15,context=ssl.create_default_context()) as smtp:
        smtp.login(sender,secret_file('SMTP_PASSWORD_FILE'));smtp.send_message(msg)

def create_portal():
    # Production file mounts are configured by the operator, never by request input.
    login=LoginApp(os.environ['DATABASE_PATH'],secret_file('SESSION_SECRET_FILE'),os.environ.get('BASE_URL','https://3dprinta.shop'),mailer=file_smtp_mailer)
    vault=Vault(os.environ['VAULT_PATH'],secret_file('VAULT_KEY_FILE',True))
    mcp_hash=secret_file('MCP_READ_TOKEN_HASH_FILE') if 'MCP_READ_TOKEN_HASH_FILE' in os.environ else None
    return Portal(login,vault,secret_file('READ_TOKEN_HASH_FILE'),mcp_read_token_hash=mcp_hash)

if __name__ == '__main__' and sys.argv[1:] == ['--provider-worker']:
    _provider_worker()
