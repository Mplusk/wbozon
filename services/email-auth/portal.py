"""Local integration prototype. Browser sessions never authenticate the read API."""
import hashlib, hmac, html, json, os, re, secrets, sqlite3, ssl, urllib.request, smtplib
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app import LoginApp, smtp_mailer

FIELDS = {'ozon': ('client_id', 'api_key'), 'wb': ('api_token',)}
class Vault:
    def __init__(self, path, key):
        if len(key) != 32: raise ValueError('32-byte vault key required')
        self.path, self.aead = path, AESGCM(key)
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)')
    def save(self, provider, values):
        nonce = secrets.token_bytes(12)
        blob = nonce + self.aead.encrypt(nonce, json.dumps(values).encode(), provider.encode())
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', (provider, blob))
    def read(self, provider):
        with sqlite3.connect(self.path) as c:
            row = c.execute('SELECT blob FROM vault WHERE provider=?', (provider,)).fetchone()
        if not row: return None
        blob = bytes(row[0])
        return json.loads(self.aead.decrypt(blob[:12], blob[12:], provider.encode()))
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
        keys=('nmID','vendorCode','discount','clubDiscount')
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
        if path not in ('/settings','/api/read','/healthz'):
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
        if method not in ('GET','POST'): return reply('Method not allowed','405 Method Not Allowed')
        sid=self.login.cookies(e).get('__Host-sid','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',sid): return reply('Войдите заново на главной странице.','401 Unauthorized')
        now=self.login.clock()
        with self.login.connect() as c:
            session=c.execute('SELECT expires,csrf FROM sessions WHERE hash=? AND expires>?',(self.login.digest(sid),now)).fetchone()
        if not session: return reply('Войдите заново на главной странице.','401 Unauthorized')
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
        content='<h1>Подключение кабинетов</h1><p>Только ключи с минимальными правами чтения. Значения после сохранения не отображаются.</p>'
        content+='<form method="post" action="/logout"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'"><button>Выйти для повторного входа</button></form>'
        if session[0]-43200 < now-300:
            content+='<p>Для сохранения ключей сначала выйдите и получите новую ссылку входа.</p>'
        for provider,fields in FIELDS.items():
            content+=f'<h2>{provider.upper()}: '+('настроено' if provider in configured else 'не настроено')+'</h2>'
            content+='<form method="post" action="/settings"><input type="hidden" name="provider" value="'+provider+'"><input type="hidden" name="csrf" value="'+html.escape(session[1],quote=True)+'">'
            for field in fields: content+=f'<label>{field}<input type="password" name="{field}" autocomplete="off" required maxlength="2048"></label>'
            content+='<button>Сохранить</button></form>'
        return reply('<!doctype html><meta charset="utf-8"><title>SIDZEN</title>'+content)

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
