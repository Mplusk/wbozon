"""OAuth authorization-code/PKCE for the owner-managed external MCP service.

All credentials are opaque and stored only as hashes. OAuth access and refresh
tokens are subordinate to an existing revocable external MCP key. This module
does not fetch client metadata, expose credentials, or trust caller URLs.
"""
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode

from ui import document, CSP

CALLBACKS = {
    'https://chatgpt.com/connector_platform_oauth_redirect': 'ChatGPT',
    'https://claude.ai/api/mcp/auth_callback': 'Claude',
    'https://grok.com/connectors-oauth-exchange-code/': 'Grok',
    'https://console.x.ai/connectors-oauth-exchange-code/': 'Grok',
}
CIMD = {
    'https://chatgpt.com/oauth/client.json': (['https://chatgpt.com/connector_platform_oauth_redirect'],'ChatGPT'),
    'https://claude.ai/oauth/mcp-oauth-client-metadata': (['https://claude.ai/api/mcp/auth_callback'],'Claude'),
    'https://grok.com/oauth/mcp-client.json': (['https://grok.com/connectors-oauth-exchange-code/','https://console.x.ai/connectors-oauth-exchange-code/'],'Grok'),
}
TOKEN_RE = re.compile(r'mcp_[A-Za-z0-9_-]{43}\Z')
VALUE_RE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
VERIFIER_RE = re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')
CLIENT_RE = re.compile(r'client_[A-Za-z0-9_-]{32}\Z')
SCOPE = 'mcp:read'

def _hash(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()

def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')

def _token():
    return 'mcp_' + secrets.token_urlsafe(32)

def _text(value, limit=512):
    return isinstance(value,str) and 0 < len(value) <= limit and not re.search(r'[\x00-\x1f\x7f]',value)

class OAuthError(Exception):
    def __init__(self, code):
        self.code=code
        super().__init__(code)

class OAuthStore:
    """Durable, atomic storage shared across all Gunicorn workers."""
    def __init__(self,path,clock=time.time,signing_secret=None):
        if not isinstance(signing_secret,str) or len(signing_secret)<32:
            raise ValueError('OAuth signing secret must contain at least 32 characters')
        self.path=path; self.clock=clock
        self.signing_key=hmac.new(signing_secret.encode('utf-8'),b'sidzen/oauth-pending/v1',hashlib.sha256).digest()
        with sqlite3.connect(path,timeout=0.5) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS oauth_clients(
                    id TEXT PRIMARY KEY, redirect TEXT NOT NULL, name TEXT NOT NULL,
                    method TEXT NOT NULL, secret_hash TEXT, created REAL NOT NULL, grants TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_pending_used(hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_codes(
                    hash TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL,
                    challenge TEXT NOT NULL, scope TEXT NOT NULL, resource TEXT NOT NULL,
                    key_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_tokens(
                    access_hash TEXT PRIMARY KEY, refresh_hash TEXT NOT NULL UNIQUE,
                    client TEXT NOT NULL, key_id TEXT NOT NULL, scope TEXT NOT NULL,
                    resource TEXT NOT NULL, access_expires REAL NOT NULL, refresh_expires REAL NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(self.path,timeout=0.5)

    def client(self,c,client_id):
        if client_id in CIMD:
            redirects,name=CIMD[client_id]
            return (client_id,redirects,name,'none',None,['authorization_code','refresh_token'])
        if not isinstance(client_id,str) or not CLIENT_RE.fullmatch(client_id): return None
        row=c.execute('SELECT id,redirect,name,method,secret_hash,grants FROM oauth_clients WHERE id=?',(client_id,)).fetchone()
        return (row[0],json.loads(row[1]),row[2],row[3],row[4],json.loads(row[5])) if row else None

    def register(self,data):
        if not isinstance(data,dict): raise OAuthError('invalid_client_metadata')
        redirects=data.get('redirect_uris')
        if not isinstance(redirects,list) or not 1<=len(redirects)<=2 or any(not isinstance(x,str) or x not in CALLBACKS for x in redirects) or len(set(redirects))!=len(redirects):
            raise OAuthError('invalid_redirect_uri')
        grants=data.get('grant_types',['authorization_code'])
        if not isinstance(grants,list) or 'authorization_code' not in grants or any(x not in ('authorization_code','refresh_token') for x in grants) or len(set(grants))!=len(grants):
            raise OAuthError('invalid_client_metadata')
        responses=data.get('response_types',['code'])
        if responses!=['code'] or data.get('application_type','web')!='web': raise OAuthError('invalid_client_metadata')
        # Public registration defaults to none and returns it explicitly.
        # This interoperates with clients that omit the optional field.
        method=data.get('token_endpoint_auth_method','none')
        if method!='none': raise OAuthError('invalid_client_metadata')
        name=data.get('client_name',CALLBACKS[redirects[0]])
        if not _text(name,100): raise OAuthError('invalid_client_metadata')
        name=CALLBACKS[redirects[0]]
        # A public client is identified by its immutable allowlisted redirects
        # and grants. Anonymous duplicate registrations cannot fill the table.
        stable=json.dumps([sorted(redirects),sorted(grants)],separators=(',',':')).encode()
        stable_id=base64.urlsafe_b64encode(hashlib.sha256(stable).digest()[:24]).rstrip(b'=').decode()
        client_id='client_'+stable_id
        now=self.clock()
        with self.connect() as c:
            # Public DCR is idempotent over a finite set of known redirects and
            # grants. Keep client IDs indefinitely so existing clients can reconnect.
            existing=c.execute('SELECT created FROM oauth_clients WHERE id=?',(client_id,)).fetchone()
            if not existing:
                c.execute('BEGIN IMMEDIATE')
                existing=c.execute('SELECT created FROM oauth_clients WHERE id=?',(client_id,)).fetchone()
                if not existing:
                    c.execute('INSERT INTO oauth_clients VALUES(?,?,?,?,?,?,?)',(client_id,json.dumps(redirects),name,method,None,now,json.dumps(grants)))
            if existing: now=existing[0]
        result={'client_id':client_id,'client_id_issued_at':int(now),'redirect_uris':redirects,
                'client_name':name,'token_endpoint_auth_method':method,
                'grant_types':grants,'response_types':['code']}
        return result

    def begin(self,data,resource):
        required=('response_type','client_id','redirect_uri','code_challenge','code_challenge_method')
        if any(not _text(data.get(k)) for k in required): raise OAuthError('invalid_request')
        state=data.get('state','')
        if state and not _text(state,1024): raise OAuthError('invalid_request')
        if data['response_type']!='code' or data['code_challenge_method']!='S256' or not VALUE_RE.fullmatch(data['code_challenge']):
            raise OAuthError('invalid_request')
        if data.get('resource',resource)!=resource: raise OAuthError('invalid_target')
        scope=data.get('scope',SCOPE)
        if not isinstance(scope,str) or len(scope)>128: raise OAuthError('invalid_scope')
        requested=scope.split()
        if not requested: requested=[SCOPE]
        if SCOPE not in requested or len(set(requested))!=len(requested) or any(x not in (SCOPE,'offline_access') for x in requested):
            raise OAuthError('invalid_scope')
        scope=' '.join(sorted(requested))
        with self.connect() as c:
            client=self.client(c,data['client_id'])
            allowed_redirect = client and data['redirect_uri'] in client[1]
            if not allowed_redirect or data['redirect_uri'] not in CALLBACKS:
                raise OAuthError('invalid_request')
            nonce=secrets.token_urlsafe(32)
            payload={'nonce':nonce,'client':client[0],'redirect':data['redirect_uri'],'state':state,
                     'challenge':data['code_challenge'],'scope':scope,'resource':resource,'expires':self.clock()+300}
            encoded=base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':'),ensure_ascii=True).encode()).rstrip(b'=').decode()
            signature=base64.urlsafe_b64encode(hmac.new(self.signing_key,encoded.encode('ascii'),hashlib.sha256).digest()).rstrip(b'=').decode()
            ticket=encoded+'.'+signature
            if len(ticket)>3000: raise OAuthError('invalid_request')
        return ticket,nonce,CALLBACKS[data['redirect_uri']],client[2]

    def approve(self,ticket,csrf,key_id):
        if not isinstance(ticket,str) or len(ticket)>3000 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}',ticket): raise OAuthError('invalid_request')
        encoded,signature=ticket.split('.')
        expected=base64.urlsafe_b64encode(hmac.new(self.signing_key,encoded.encode('ascii'),hashlib.sha256).digest()).rstrip(b'=').decode()
        if not hmac.compare_digest(signature,expected): raise OAuthError('invalid_request')
        try:
            payload=json.loads(base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)))
            nonce=payload['nonce'];expires=payload['expires']
            if not isinstance(nonce,str) or not VALUE_RE.fullmatch(nonce) or not isinstance(csrf,str) or not hmac.compare_digest(nonce,csrf) or type(expires) not in (int,float) or not self.clock()<expires<=self.clock()+300:
                raise ValueError()
            row=(payload['client'],payload['redirect'],payload['state'],payload['challenge'],payload['scope'],payload['resource'])
            if not all(isinstance(x,str) for x in row) or row[1] not in CALLBACKS: raise ValueError()
        except (KeyError,TypeError,ValueError,UnicodeError): raise OAuthError('invalid_request') from None
        code=secrets.token_urlsafe(32);now=self.clock()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active=c.execute('SELECT 1 FROM external_mcp_keys WHERE public_id=? AND revoked_at IS NULL',(key_id,)).fetchone()
            if not active or not self.client(c,row[0]): raise OAuthError('invalid_request')
            c.execute('DELETE FROM oauth_pending_used WHERE expires<=?',(now,))
            try: c.execute('INSERT INTO oauth_pending_used VALUES(?,?)',(_hash(nonce),expires))
            except sqlite3.IntegrityError: raise OAuthError('invalid_request') from None
            c.execute('DELETE FROM oauth_codes WHERE expires<=?',(now,))
            c.execute('INSERT INTO oauth_codes VALUES(?,?,?,?,?,?,?,?)',(_hash(code),row[0],row[1],row[3],row[4],row[5],key_id,now+60))
        return code,row[1],row[2]

    def _check_grant(self,c,params,resource,basic,now):
        """Read-only grant validation; caller repeats it inside the write lock."""
        client_id=params.get('client_id',''); secret=params.get('client_secret')
        if basic:
            if client_id and client_id!=basic[0]: raise OAuthError('invalid_client')
            if secret is not None: raise OAuthError('invalid_client')
            client_id,secret=basic
        client=self.client(c,client_id)
        if not client: raise OAuthError('invalid_client')
        if params['grant_type'] not in client[5]: raise OAuthError('unauthorized_client')
        if client[3]!='none' or secret is not None or basic: raise OAuthError('invalid_client')
        if params['grant_type']=='authorization_code':
            code=params.get('code','');verifier=params.get('code_verifier','');redirect=params.get('redirect_uri','')
            if not isinstance(code,str) or not VALUE_RE.fullmatch(code) or not isinstance(verifier,str) or not VERIFIER_RE.fullmatch(verifier): raise OAuthError('invalid_grant')
            digest=_hash(code)
            row=c.execute('SELECT client,redirect,challenge,scope,resource,key_id FROM oauth_codes WHERE hash=? AND expires>?',(digest,now)).fetchone()
            if not row or row[0]!=client_id or row[1]!=redirect or row[4]!=resource or not hmac.compare_digest(row[2],_challenge(verifier)):
                raise OAuthError('invalid_grant')
            key_id,scope=row[5],row[3]
        else:
            refresh=params.get('refresh_token','')
            if not isinstance(refresh,str) or not TOKEN_RE.fullmatch(refresh): raise OAuthError('invalid_grant')
            digest=_hash(refresh)
            row=c.execute('SELECT client,key_id,scope,resource FROM oauth_tokens WHERE refresh_hash=? AND refresh_expires>?',(digest,now)).fetchone()
            if not row or row[0]!=client_id or row[3]!=resource: raise OAuthError('invalid_grant')
            key_id,scope=row[1],row[2]
            if 'scope' in params:
                requested=params['scope'].split()
                if not requested or len(set(requested))!=len(requested) or SCOPE not in requested or not set(requested).issubset(set(scope.split())):
                    raise OAuthError('invalid_scope')
                scope=' '.join(sorted(requested))
        if not c.execute('SELECT 1 FROM external_mcp_keys WHERE public_id=? AND revoked_at IS NULL',(key_id,)).fetchone():
            raise OAuthError('invalid_grant')
        return client_id,client,key_id,scope,digest

    def exchange(self,params,resource,basic=None):
        if not isinstance(params,dict) or params.get('grant_type') not in ('authorization_code','refresh_token'):
            raise OAuthError('unsupported_grant_type')
        if params.get('resource',resource)!=resource: raise OAuthError('invalid_target')
        with self.connect() as c:
            # Garbage and forged grants do not contend for SQLite write locks.
            self._check_grant(c,params,resource,basic,self.clock())
            c.execute('BEGIN IMMEDIATE')
            now=self.clock()
            client_id,client,key_id,scope,digest=self._check_grant(c,params,resource,basic,now)
            if params['grant_type']=='authorization_code': c.execute('DELETE FROM oauth_codes WHERE hash=?',(digest,))
            else: c.execute('DELETE FROM oauth_tokens WHERE refresh_hash=?',(digest,))
            access,refresh=_token(),_token()
            c.execute('DELETE FROM oauth_tokens WHERE refresh_expires<=?',(now,))
            can_refresh='refresh_token' in client[5]
            c.execute('INSERT INTO oauth_tokens VALUES(?,?,?,?,?,?,?,?)',(_hash(access),_hash(refresh),client_id,key_id,scope,resource,now+3600,now+(30*86400 if can_refresh else 3600)))
        result={'access_token':access,'token_type':'Bearer','expires_in':3600,'scope':scope}
        if can_refresh: result['refresh_token']=refresh
        return result

    def verify(self,token,resource):
        if not isinstance(token,str) or not TOKEN_RE.fullmatch(token): return None
        with self.connect() as c:
            row=c.execute('''SELECT t.key_id FROM oauth_tokens t JOIN external_mcp_keys k ON k.public_id=t.key_id
                             WHERE t.access_hash=? AND t.access_expires>? AND t.resource=? AND t.scope IN (?,?) AND k.revoked_at IS NULL''',(_hash(token),self.clock(),resource,SCOPE,SCOPE+' offline_access')).fetchone()
        return row[0] if row else None


def handle(environ,start,store,keys,origin):
    """Handle only OAuth and metadata routes; caller validates the Host first."""
    path=environ.get('PATH_INFO',''); method=environ.get('REQUEST_METHOD','GET')
    resource=origin+'/mcp'
    def reply(status,body,kind='application/json; charset=utf-8',extra=()):
        raw=(json.dumps(body,ensure_ascii=False,separators=(',',':')) if kind.startswith('application/json') else body).encode('utf-8')
        start(status,[('Content-Type',kind),('Content-Length',str(len(raw))),('Cache-Control','no-store'),
            ('Pragma','no-cache'),('Referrer-Policy','no-referrer'),('X-Content-Type-Options','nosniff'),
            ('Content-Security-Policy',CSP),*extra])
        return [raw]
    def error(code,status='400 Bad Request'):
        return reply(status,{'error':code})
    def read_body(kind):
        if environ.get('CONTENT_TYPE','').split(';')[0].strip().lower()!=kind: raise OAuthError('invalid_request')
        try:
            n=int(environ.get('CONTENT_LENGTH','0'))
            if not 0<n<=4096: raise ValueError()
            raw=environ['wsgi.input'].read(n)
            if len(raw)!=n: raise ValueError()
            return raw.decode('utf-8')
        except (ValueError,UnicodeError,KeyError,TypeError): raise OAuthError('invalid_request') from None
    def params(raw):
        try:
            fields=parse_qs(raw,max_num_fields=20,keep_blank_values=True,strict_parsing=True)
            if any(len(values)!=1 for values in fields.values()): raise ValueError()
            return {k:v[0] for k,v in fields.items()}
        except ValueError: raise OAuthError('invalid_request') from None
    try:
        if path in ('/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp') and method=='GET':
            return reply('200 OK',{'resource':resource,'authorization_servers':[origin],'scopes_supported':[SCOPE],
                'bearer_methods_supported':['header']})
        if path=='/.well-known/oauth-authorization-server' and method=='GET':
            return reply('200 OK',{'issuer':origin,'authorization_endpoint':origin+'/oauth/authorize',
                'token_endpoint':origin+'/oauth/token','registration_endpoint':origin+'/oauth/register',
                'response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],
                'token_endpoint_auth_methods_supported':['none'],
                'code_challenge_methods_supported':['S256'],'scopes_supported':[SCOPE,'offline_access'],
                'client_id_metadata_document_supported':True,'authorization_response_iss_parameter_supported':True})
        if path=='/oauth/register' and method=='POST':
            try: data=json.loads(read_body('application/json'))
            except (ValueError,TypeError): raise OAuthError('invalid_client_metadata') from None
            return reply('201 Created',store.register(data))
        if path=='/oauth/authorize' and method=='GET':
            raw=environ.get('QUERY_STRING','')
            if not isinstance(raw,str) or len(raw)>4096: raise OAuthError('invalid_request')
            ticket,nonce,platform,name=store.begin(params(raw),resource)
            page=('<main id="content"><h1>Подключить '+html.escape(platform)+'</h1><p>Приложение '
                +html.escape(name)+' запрашивает доступ к чтению ваших данных Ozon и Wildberries. '
                'Оно не сможет изменять товары, цены или заказы.</p><form method="post" action="/oauth/authorize">'
                '<input type="hidden" name="csrf" value="'+nonce+'"><input type="hidden" name="ticket" value="'+ticket+'"><label for="mcp-key">Ключ MCP из кабинета</label>'
                '<input id="mcp-key" type="password" name="key" required autocomplete="off" maxlength="47">'
                '<button>Разрешить доступ</button></form><p><a href="/settings">Открыть настройки ключей</a></p></main>')
            return reply('200 OK',document('Подключение MCP',page),kind='text/html; charset=utf-8',extra=[('Set-Cookie','__Host-mcp-oauth='+nonce+'; Path=/; Max-Age=300; Secure; HttpOnly; SameSite=Lax')])
        if path=='/oauth/authorize' and method=='POST':
            if environ.get('HTTP_ORIGIN')!=origin: raise OAuthError('invalid_request')
            data=params(read_body('application/x-www-form-urlencoded'))
            if set(data)!={'csrf','ticket','key'}: raise OAuthError('invalid_request')
            cookie=SimpleCookie()
            try: cookie.load(environ.get('HTTP_COOKIE',''))
            except Exception: raise OAuthError('invalid_request') from None
            csrf=data['csrf']; stored=cookie.get('__Host-mcp-oauth')
            ticket=data['ticket']
            if not VALUE_RE.fullmatch(csrf) or len(ticket)>3000 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}',ticket) or not stored or not hmac.compare_digest(csrf,stored.value): raise OAuthError('invalid_request')
            key_id=keys.check_token(data['key'])
            if not key_id:
                page=('<main id="content"><h1>Ключ не подошёл</h1><p>Проверьте ключ MCP и попробуйте снова.</p>'
                    '<form method="post" action="/oauth/authorize"><input type="hidden" name="csrf" value="'+csrf+'"><input type="hidden" name="ticket" value="'+ticket+'">'
                    '<label for="mcp-key">Ключ MCP из кабинета</label><input id="mcp-key" type="password" name="key" required autocomplete="off" maxlength="47">'
                    '<button>Разрешить доступ</button></form><p><a href="/settings">Настройки ключей</a></p></main>')
                return reply('403 Forbidden',document('Ключ не подошёл',page),kind='text/html; charset=utf-8')
            code,redirect,state=store.approve(ticket,csrf,key_id)
            query={'code':code,'iss':origin}
            if state:query['state']=state
            return reply('303 See Other','',kind='text/html; charset=utf-8',extra=[('Location',redirect+'?'+urlencode(query)),('Set-Cookie','__Host-mcp-oauth=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax')])
        if path=='/oauth/token' and method=='POST':
            data=params(read_body('application/x-www-form-urlencoded'))
            basic=None; auth=environ.get('HTTP_AUTHORIZATION','')
            if auth:
                if not auth.lower().startswith('basic '): raise OAuthError('invalid_client')
                try:
                    decoded=base64.b64decode(auth[6:],validate=True).decode('ascii')
                    if ':' not in decoded: raise ValueError()
                    basic=decoded.split(':',1)
                except (ValueError,UnicodeError): raise OAuthError('invalid_client') from None
            return reply('200 OK',store.exchange(data,resource,basic))
        return error('invalid_request','405 Method Not Allowed')
    except OAuthError as exc:
        return error(exc.code,'401 Unauthorized' if exc.code=='invalid_client' else '400 Bad Request')
    except (sqlite3.Error,OverflowError):
        return error('temporarily_unavailable','503 Service Unavailable')
