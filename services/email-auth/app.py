"""Isolated email-login prototype. No marketplace data or credentials included."""
from ui import document, CSP
import hashlib, hmac, html, os, re, secrets, smtplib, sqlite3, ssl, time
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit
from http.cookies import SimpleCookie

class LoginApp:
    def __init__(self, db, secret, base_url, recipient='maksim@kuchermg.ru', mailer=None, clock=time.time):
        if len(secret) < 32: raise ValueError('SESSION_SECRET must be at least 32 characters')
        u=urlsplit(base_url)
        if u.scheme != 'https' or not u.netloc or u.path not in ('','/') or u.query or u.fragment or u.username: raise ValueError('HTTPS origin required')
        self.db,self.secret,self.origin,self.recipient,self.mailer,self.clock=db,secret,base_url.rstrip('/'),recipient.lower(),mailer,clock
        with self.connect() as c:
            c.executescript('CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, expires REAL); CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL, csrf TEXT); CREATE TABLE IF NOT EXISTS attempts(bucket TEXT, at REAL); CREATE INDEX IF NOT EXISTS attempts_bucket ON attempts(bucket,at);')
    def connect(self):
        c=sqlite3.connect(self.db,timeout=5); c.execute('PRAGMA busy_timeout=5000'); return c
    def digest(self,value): return hashlib.sha256(value.encode()).hexdigest()
    def sign(self,value): return hmac.new(self.secret.encode(),value.encode(),hashlib.sha256).hexdigest()
    def cookies(self,e):
        c=SimpleCookie()
        try:c.load(e.get('HTTP_COOKIE',''))
        except Exception:return {}
        return {k:v.value for k,v in c.items()}
    def permit(self,bucket,limit,window,busy_timeout_ms=None):
        now=self.clock()
        with self.connect() as c:
            if busy_timeout_ms is not None: c.execute('PRAGMA busy_timeout='+str(int(busy_timeout_ms)))
            c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM attempts WHERE at < ?', (now-86400,))
            n=c.execute('SELECT count(*) FROM attempts WHERE bucket=? AND at>?',(bucket,now-window)).fetchone()[0]
            if n>=limit:return False
            c.execute('INSERT INTO attempts VALUES(?,?)',(bucket,now)); return True
    def __call__(self,e,start):
        status='200 OK'; headers=[]
        def reply(body,code='200 OK',extra=()):
            if not body.lstrip().lower().startswith('<!doctype'): body=document('Кабинет', '<h1>Кабинет</h1><p>'+body+'</p>', auth=True)
            data=body.encode(); start(code,[('Content-Type','text/html; charset=utf-8'),('Content-Length',str(len(data))),('Cache-Control','no-store'),('Referrer-Policy','strict-origin'),('X-Content-Type-Options','nosniff'),('Content-Security-Policy',CSP),*extra]); return [data]
        def cookie(name,value,age):return ('Set-Cookie',f'{name}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}')
        def form(title,action,fields):return document(title,f'<p class="eyebrow">Личный кабинет</p><h1>{title}</h1><p>Войдите по одноразовой ссылке из письма. Пароль не нужен.</p><form method="post" action="{action}">{fields}<button>Продолжить</button></form><p class="fineprint">Ссылка действует 15 минут и работает один раз. Используйте тот же браузер для подтверждения входа.</p>',auth=True)
        def recovery(title, message, code):
            # Only fixed application text. Never reflect the link, token, email or cookies.
            page=document(title,'<p class="eyebrow">Вход в кабинет</p><h1>'+title+'</h1><p>'+message+'</p><p>Ссылка действует 15 минут и подходит для одного входа. Новую ссылку нужно запросить вручную: письмо не отправляется автоматически.</p><p><a class="button" href="/">Запросить новую ссылку или открыть кабинет</a></p>',auth=True)
            return reply(page,code)
        if e.get('HTTP_HOST')!=urlsplit(self.origin).netloc:return reply('Invalid host','400 Bad Request')
        path=e.get('PATH_INFO','/'); method=e.get('REQUEST_METHOD','GET'); values={}
        if method not in ('GET','POST'):return reply('Method not allowed','405 Method Not Allowed')
        if method=='POST':
            if e.get('HTTP_ORIGIN')!=self.origin:return reply('Forbidden','403 Forbidden')
            if e.get('CONTENT_TYPE','').split(';')[0]!='application/x-www-form-urlencoded':return reply('Unsupported content type','415 Unsupported Media Type')
            try: length=int(e.get('CONTENT_LENGTH','0'))
            except ValueError:return reply('Invalid request','400 Bad Request')
            if not 0<length<=4096:return reply('Request too large','413 Payload Too Large')
            try: values=parse_qs(e['wsgi.input'].read(length).decode('utf-8','replace'),max_num_fields=8)
            except ValueError: return reply('Invalid request','400 Bad Request')
        get=lambda k:values.get(k,[''])[0]
        cs=self.cookies(e); now=self.clock()
        if path=='/' and method=='GET':
            sid=cs.get('__Host-sid','')
            with self.connect() as c: row=c.execute('SELECT csrf FROM sessions WHERE hash=? AND expires>?',(self.digest(sid),now)).fetchone() if sid else None
            if row:return reply(form('Вход выполнен','/logout',f'<input type="hidden" name="csrf" value="{row[0]}">')+'<p>Это прототип авторизации, не кабинет маркетплейса.</p>')
            return reply(form('Вход','/auth/request','<label for="login-email">Электронная почта</label><input id="login-email" name="email" type="email" required autocomplete="email" placeholder="you@example.ru">'))
        if path=='/auth/request' and method=='POST':
            generic=document('Проверьте почту','<p class="eyebrow">Следующий шаг</p><h1>Проверьте почту</h1><p>Если адрес разрешён и отправка доступна, вы получите письмо со ссылкой. Ссылка действует 15 минут.</p><p class="fineprint">Нет письма? Проверьте папку «Спам» или повторите запрос позже.</p><a href="/">Вернуться ко входу</a>',auth=True)
            allowed=self.permit('ip:'+self.digest(e.get('REMOTE_ADDR','unknown')),5,900)
            if not allowed:return reply(generic,'200 OK')
            if get('email').strip().lower()!=self.recipient:return reply(generic)
            if not self.permit('recipient',5,3600):return reply(generic)
            token=secrets.token_urlsafe(32); digest=self.digest(token)
            with self.connect() as c:
                c.execute('DELETE FROM tokens WHERE expires<=?',(now,)); c.execute('DELETE FROM sessions WHERE expires<=?',(now,)); c.execute('INSERT INTO tokens VALUES(?,?)',(digest,now+900))
            try:
                if self.mailer is None:raise RuntimeError('Mailer unavailable')
                self.mailer(self.recipient,self.origin+'/auth/link?token='+token)
            except Exception:
                with self.connect() as c:c.execute('DELETE FROM tokens WHERE hash=?',(digest,))
                # Never log exceptions: SMTP errors can contain authentication details.
            return reply(generic)
        if path=='/auth/link' and method=='GET':
            if len(e.get('QUERY_STRING',''))>512:return recovery('Не удалось открыть ссылку','Ссылка повреждена или неполная. Откройте её целиком из письма либо запросите новую.','400 Bad Request')
            try: token=parse_qs(e.get('QUERY_STRING',''),max_num_fields=4).get('token',[''])[0]
            except ValueError:return recovery('Не удалось открыть ссылку','Ссылка повреждена или неполная. Откройте её целиком из письма либо запросите новую.','400 Bad Request')
            if not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',token):return recovery('Недействительная ссылка','Ссылка повреждена или неполная. Откройте её целиком из письма либо запросите новую.','400 Bad Request')
            nonce=secrets.token_urlsafe(24); signed=nonce+'.'+self.sign(nonce+':'+self.digest(token))
            fields=f'<input type="hidden" name="token" value="{html.escape(token,quote=True)}"><input type="hidden" name="csrf" value="{nonce}">'
            # GET does not consume or authenticate. Scanners must not log users in.
            return reply(form('Подтвердить вход','/auth/confirm',fields),extra=[cookie('__Host-login',signed,900)])
        if path=='/auth/confirm' and method=='POST':
            token=get('token'); nonce=get('csrf'); login_cookie=cs.get('__Host-login','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',nonce) or not re.fullmatch(r'[A-Za-z0-9_-]{32}\.[0-9a-f]{64}',login_cookie):return recovery('Не удалось подтвердить вход','Откройте ссылку из письма заново в том же браузере и подтвердите вход. Если это не помогает, запросите новую ссылку.','403 Forbidden')
            expected=nonce+'.'+self.sign(nonce+':'+self.digest(token))
            if not hmac.compare_digest(login_cookie,expected):return recovery('Не удалось подтвердить вход','Откройте ссылку из письма заново в том же браузере и подтвердите вход. Если это не помогает, запросите новую ссылку.','403 Forbidden')
            sid=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                cur=c.execute('DELETE FROM tokens WHERE hash=? AND expires>?',(self.digest(token),now))
                if cur.rowcount!=1:return recovery('Ссылка истекла или уже использована','По этой ссылке больше нельзя войти. Запросите новую ссылку; если вход уже выполнен, откройте кабинет.','400 Bad Request')
                c.execute('INSERT INTO sessions VALUES(?,?,?)',(self.digest(sid),now+43200,csrf))
            return reply(document('Вход выполнен','<p class="eyebrow">Готово</p><h1>Вход выполнен</h1><p>Теперь можно перейти к подключению кабинетов.</p><a class="button" href="/">Открыть кабинет</a>',auth=True),extra=[cookie('__Host-sid',sid,43200),cookie('__Host-login','',0)])
        if path=='/logout' and method=='POST':
            sid=cs.get('__Host-sid',''); csrf=get('csrf')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}',sid) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',csrf):return reply('Forbidden','403 Forbidden')
            with self.connect() as c:
                row=c.execute('SELECT csrf FROM sessions WHERE hash=? AND expires>?',(self.digest(sid),now)).fetchone()
                if not row or not hmac.compare_digest(row[0],csrf):return reply('Forbidden','403 Forbidden')
                c.execute('DELETE FROM sessions WHERE hash=?',(self.digest(sid),))
            return reply(document('Вы вышли','<h1>Вы вышли.</h1><p>Сеанс завершён. Сохранённые ключи остались на сервере.</p><a class="button" href="/">Войти снова</a>',auth=True),extra=[cookie('__Host-sid','',0)])
        return reply('Not found','404 Not Found')

def smtp_mailer(recipient,link):
    sender=os.environ['SMTP_USER']; password=os.environ['SMTP_PASSWORD']
    if sender!='no-reply@3dprinta.shop':raise ValueError('Unexpected sender')
    msg=EmailMessage(); msg['From']=sender; msg['To']=recipient; msg['Subject']='Вход в SIDZEN'
    msg.set_content('Для входа откройте ссылку и нажмите «Продолжить». Ссылка действует 15 минут и работает один раз.\n\n'+link+'\n\nЕсли вы не запрашивали вход, проигнорируйте письмо.')
    with smtplib.SMTP_SSL('smtp.spaceweb.ru',465,timeout=15,context=ssl.create_default_context()) as smtp:
        smtp.login(sender,password); smtp.send_message(msg)

def create_app():
    return LoginApp(os.environ['DATABASE_PATH'],os.environ['SESSION_SECRET'],os.environ.get('BASE_URL','https://3dprinta.shop'),mailer=smtp_mailer)
