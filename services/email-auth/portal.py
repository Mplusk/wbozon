"""Local integration prototype. Browser sessions never authenticate the read API."""
import hashlib, hmac, html, json, math, os, re, secrets, sqlite3, ssl, urllib.request, urllib.error, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app import LoginApp, smtp_mailer

FIELDS = {'ozon': ('client_id', 'api_key'), 'wb': ('api_token',)}
CHECK_OPERATIONS = {'ozon':'ozon_products', 'wb':'wb_prices'}
CHECK_MESSAGES = {
    'ok':'Проверка чтения данных выполнена успешно.',
    'denied':'Маркетплейс отклонил доступ. Проверьте ключ, срок действия и права чтения.',
    'rate_limited':'Маркетплейс временно ограничил запросы. Повторите проверку позже.',
    'unavailable':'Маркетплейс временно недоступен. Повторите проверку позже.',
    'failed':'Не удалось подтвердить соединение. Проверьте настройки и повторите позже.',
}

class Vault:
    def __init__(self, path, key):
        if len(key) != 32: raise ValueError('32-byte vault key required')
        self.path, self.aead = path, AESGCM(key)
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS connection_checks(provider TEXT PRIMARY KEY, version TEXT NOT NULL, outcome TEXT NOT NULL, checked_at REAL NOT NULL)')
    def save(self, provider, values):
        nonce = secrets.token_bytes(12)
        blob = nonce + self.aead.encrypt(nonce, json.dumps(values).encode(), provider.encode())
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', (provider, blob))
            c.execute('DELETE FROM connection_checks WHERE provider=?',(provider,))
    def snapshot(self, provider):
        with sqlite3.connect(self.path) as c:
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
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
            if not row or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version): return False
            c.execute('INSERT OR REPLACE INTO connection_checks VALUES (?,?,?,?)',(provider,version,outcome,checked_at))
        return True
    def version_matches(self, provider, version):
        with sqlite3.connect(self.path) as c:
            row=c.execute('SELECT blob FROM vault WHERE provider=?',(provider,)).fetchone()
        return bool(row) and hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),version)
    def check_status(self, provider):
        # Read storage metadata only; rendering never decrypts credentials.
        with sqlite3.connect(self.path) as c:
            row=c.execute('SELECT v.blob,c.version,c.outcome,c.checked_at FROM vault v JOIN connection_checks c ON v.provider=c.provider WHERE v.provider=?',(provider,)).fetchone()
        if not row or row[2] not in CHECK_MESSAGES or not hmac.compare_digest(hashlib.sha256(bytes(row[0])).hexdigest(),row[1]): return None
        return row[2],row[3]
    def present(self):
        with sqlite3.connect(self.path) as c:
            return {r[0] for r in c.execute('SELECT provider FROM vault')}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def provider_read(operation, args, creds):
    limit = args.get('limit', 20)
    if type(limit) is not int or not 1 <= limit <= 100 or set(args) - {'limit'}:
        raise ValueError('Invalid arguments')
    if operation == 'ozon_products':
        req = urllib.request.Request('https://api-seller.ozon.ru/v3/product/list',
            data=json.dumps({'filter': {'visibility':'ALL'}, 'limit':limit, 'last_id':''}).encode(),
            headers={'Client-Id':creds['client_id'], 'Api-Key':creds['api_key'], 'Content-Type':'application/json'}, method='POST')
    elif operation == 'wb_prices':
        req = urllib.request.Request('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit='+str(limit)+'&offset=0',
            headers={'Authorization':creds['api_token']}, method='GET')
    else: raise ValueError('Unknown operation')
    opener=urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=15) as resp:
        raw=resp.read(512001)
        if len(raw)>512000: raise ValueError('Response too large')
        data=json.loads(raw)
    if operation=='ozon_products':
        rows=data.get('result',{}).get('items')
        if not isinstance(rows,list): raise ValueError('Unexpected schema')
        keys=('product_id','offer_id','archived','is_fbo_visible','is_fbs_visible')
    else:
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

def render_preview(provider, result, fetched_at, sensitive_values=()):
    """Render allowlisted normalized data only; upstream strings are untrusted text."""
    rows=result.get('items')
    if not isinstance(rows,list): raise ValueError('Invalid items')
    title='Первые товары Ozon' if provider=='ozon' else 'Первые цены Wildberries'
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
    content='<main><h1>'+title+'</h1><p>Получено: '+stamp+'.</p>'
    content+='<p>Только первая страница, до 20 товаров. Это не полный ассортимент и не отчёт об остатках или продажах. Данные отображаются только для чтения; значения могут измениться после запроса.</p>'
    content+='<p><a href="/settings">Вернуться к настройкам</a></p>'
    if not rows: content+='<p>В первой странице ответа нет товаров. Это не доказывает отсутствие всех товаров в кабинете.</p>'
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
    content+='</main>'
    return '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>'+title+' — SIDZEN</title></head><body>'+content+'</body></html>'

class Portal:
    def __init__(self, login, vault, read_token_hash, gateway=provider_read):
        if not re.fullmatch(r'[0-9a-f]{64}',read_token_hash): raise ValueError('Expected SHA256 token hash')
        self.login,self.vault,self.token_hash,self.gateway=login,vault,read_token_hash,gateway
    def __call__(self,e,start):
        path=e.get('PATH_INFO','/'); method=e.get('REQUEST_METHOD','GET')
        if path == '/' and method == 'GET' and e.get('HTTP_HOST') == urlsplit(self.login.origin).netloc:
            sid=self.login.cookies(e).get('__Host-sid','')
            if re.fullmatch(r'[A-Za-z0-9_-]{43}',sid):
                with self.login.connect() as c:
                    present=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                if present:
                    start('303 See Other',[('Location','/settings'),('Cache-Control','no-store'),('Content-Length','0')]);return [b'']
        if path not in ('/settings','/settings/check','/settings/preview','/api/read','/healthz'):
            return self.login(e,start)
        def reply(text,code='200 OK',json_response=False):
            body=(json.dumps(text) if json_response else text).encode()
            start(code,[('Content-Type','application/json' if json_response else 'text/html; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('Referrer-Policy','no-referrer'),('X-Content-Type-Options','nosniff'),('Content-Security-Policy',"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")]); return [body]
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
            if not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43,128}',auth) or not hmac.compare_digest(hashlib.sha256(auth[7:].encode()).hexdigest(),self.token_hash):
                return reply({'error':'unauthorized'},'401 Unauthorized',True)
            if not self.login.permit('api',30,60): return reply({'error':'rate_limited'},'429 Too Many Requests',True)
            try:
                data=json.loads(body('application/json'))
                if not isinstance(data,dict) or set(data)!={'operation','arguments'}: raise ValueError()
                op=data['operation']; args=data['arguments']
                if op not in ('ozon_products','wb_prices') or not isinstance(args,dict) or set(args)-{'limit'} or type(args.get('limit',20)) is not int or not 1<=args.get('limit',20)<=100: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply({'error':'invalid_request'},'400 Bad Request',True)
            try:
                creds=self.vault.read('ozon' if op=='ozon_products' else 'wb')
                if not creds: return reply({'error':'not_configured'},'409 Conflict',True)
                result=self.gateway(op,args,creds)
                return reply(result,json_response=True)
            except Exception: return reply({'error':'read_failed'},'502 Bad Gateway',True)
        if path in ('/settings/check','/settings/preview') and method!='POST': return reply('Method not allowed','405 Method Not Allowed')
        if method not in ('GET','POST'): return reply('Method not allowed','405 Method Not Allowed')
        sid=self.login.cookies(e).get('__Host-sid','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',sid): return reply('Войдите заново на главной странице.','401 Unauthorized')
        now=self.login.clock()
        with self.login.connect() as c:
            session=c.execute('SELECT expires,csrf FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),now)).fetchone()
        if not session: return reply('Войдите заново на главной странице.','401 Unauthorized')
        if path in ('/settings/check','/settings/preview'):
            if e.get('HTTP_ORIGIN')!=self.login.origin: return reply('Forbidden','403 Forbidden')
            try:
                form=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=2,keep_blank_values=True,strict_parsing=True)
                if set(form)!={'provider','csrf'} or any(len(v)!=1 for v in form.values()): raise ValueError()
                provider=form['provider'][0]; csrf=form['csrf'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf) or not hmac.compare_digest(csrf,session[1]): return reply('Forbidden','403 Forbidden')
                if provider not in CHECK_OPERATIONS: raise ValueError()
            except (ValueError,TypeError,UnicodeError): return reply('Некорректный запрос.','400 Bad Request')
            # Global owner-level buckets persist across workers/sessions and prevent click storms.
            if not self.login.permit('connection-check:'+provider,1,60) or not self.login.permit('connection-check:all',10,3600):
                return reply('Слишком много запросов. Для проверки или просмотра одного кабинета доступен один запрос в минуту, всего — не более 10 в час. <a href="/settings">Настройки</a>','429 Too Many Requests')
            try:
                snapshot=self.vault.snapshot(provider)
                if not snapshot: return reply('Сначала сохраните ключ. <a href="/settings">Настройки</a>','409 Conflict')
                creds,version=snapshot
            except Exception: return reply('Не удалось прочитать настройки. <a href="/settings">Настройки</a>','503 Service Unavailable')
            try:
                result=self.gateway(CHECK_OPERATIONS[provider],{'limit':20 if path=='/settings/preview' else 1},creds)
                if not isinstance(result,dict) or not isinstance(result.get('items'),list): raise ValueError('Invalid response')
                outcome='ok'
            except urllib.error.HTTPError as exc:
                outcome='denied' if exc.code in (401,403) else 'rate_limited' if exc.code==429 else 'unavailable' if 500<=exc.code<=599 else 'failed'
                exc.close()
            except (TimeoutError,urllib.error.URLError): outcome='unavailable'
            except Exception: outcome='failed'
            if path=='/settings/preview':
                # Revalidate after the bounded upstream call: logout/expiry or key replacement
                # must not return a snapshot from an old session/account.
                try:
                    with self.login.connect() as c:
                        live=c.execute('SELECT 1 FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),self.login.clock())).fetchone()
                    unchanged=self.vault.version_matches(provider,version)
                except Exception:
                    return reply('Не удалось проверить доступ к данным. Повторите позже. <a href="/settings">Настройки</a>','503 Service Unavailable')
                if not live: return reply('Войдите заново на главной странице.','401 Unauthorized')
                if not unchanged:
                    return reply('Ключ изменился во время запроса. Повторите просмотр. <a href="/settings">Настройки</a>','409 Conflict')
                if outcome!='ok': return reply(CHECK_MESSAGES[outcome]+' <a href="/settings">Настройки</a>','502 Bad Gateway')
                try:
                    return reply(render_preview(provider,result,self.login.clock(),creds.values()))
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
        configured=self.vault.present()
        content='<main><h1>Подключение кабинетов</h1>'
        content+='<p>Здесь можно сохранить ключи Wildberries и Ozon. Используйте отдельные ключи только с необходимыми правами чтения. Права на изменение товаров, цен и заказов не нужны.</p>'
        content+='<p>Ключи хранятся зашифрованно и не отображаются после сохранения. Сохранение ключа не подтверждает его действительность и не подключает MCP автоматически.</p>'
        content+='<section aria-labelledby="mcp-title"><h2 id="mcp-title">Связь с MCP</h2><p>Статус подключения MCP здесь не проверяется. Наличие ключей в кабинете не означает, что ассистент уже получает данные.</p></section>'
        content+='<p>Сохранить или заменить ключи можно в течение первых 5 минут после входа по письму. Затем выйдите и запросите новую ссылку. Уже сохранённые ключи останутся на сервере.</p>'
        content+='<form method="post" action="/logout"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Выйти для повторного входа</button></form>'
        if session[0]-43200 < now-300:
            content+='<p role="status">Время для изменения ключей истекло. Для сохранения ключей сначала выйдите и получите новую ссылку входа.</p>'
        names={'ozon':'Ozon','wb':'Wildberries'}
        labels={'client_id':'Идентификатор продавца (Client ID)', 'api_key':'API-ключ Ozon', 'api_token':'API-токен Wildberries'}
        for provider,fields in FIELDS.items():
            saved=provider in configured
            content+=f'<section aria-labelledby="{provider}-title"><h2 id="{provider}-title">{names[provider]}</h2>'
            content+='<p>Ключ: '+('сохранён зашифрованно' if saved else 'не добавлен')+'.</p>'
            check=self.vault.check_status(provider) if saved else None
            if check:
                outcome,checked_at=check
                stamp=datetime.fromtimestamp(checked_at,timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
                scope='список товаров Ozon' if provider=='ozon' else 'цены и скидки Wildberries'
                content+='<p>Последняя проверка: '+stamp+'. '+CHECK_MESSAGES[outcome]+'</p>'
                content+='<p>Область проверки — только операция чтения: '+scope+'. Это результат на указанное время, не подтверждение всех прав или подключения MCP.</p>'
            else:
                content+='<p>Соединение с маркетплейсом: не проверено. '+('Срок действия и права ключа пока не подтверждены.' if saved else 'Сначала добавьте ключ.')+'</p>'
            if saved:
                content+='<form method="post" action="/settings/check"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Проверить подключение</button></form>'
                content+='<p>Проверка прочитает до одной записи через API маркетплейса, не меняя товары, цены или заказы.</p>'
                label='Показать первые товары Ozon' if provider=='ozon' else 'Показать первые цены Wildberries'
                content+='<form method="post" action="/settings/preview"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>'+label+'</button></form>'
                content+='<p>Просмотр загрузит только первую страницу, до 20 товаров. Данные не сохраняются в кабинете. Проверка и просмотр используют общий лимит: один запрос кабинета в минуту, всего не более 10 запросов в час.</p>' 
            if provider=='ozon': content+='<p>Введите Client ID и API-ключ из кабинета продавца Ozon. Оба значения нужно сохранить вместе.</p>'
            else: content+='<p>Введите API-токен из кабинета продавца Wildberries с правами чтения цен и скидок.</p>'
            content+='<form method="post" action="/settings"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'">'
            for field in fields:
                ident=provider+'-'+field
                content+=f'<p><label for="{ident}">{labels[field]}</label><br><input id="{ident}" type="password" name="{field}" autocomplete="off" required maxlength="2048"></p>'
            content+='<button>'+('Заменить сохранённый ключ' if saved else 'Сохранить ключ')+'</button></form></section>'
        content+='</main>'
        return reply('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Подключение кабинетов — SIDZEN</title></head><body>'+content+'</body></html>')

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
    return Portal(login,vault,secret_file('READ_TOKEN_HASH_FILE'))
