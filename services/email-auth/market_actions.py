"""Owner-confirmed seller operations. An interrupted transmission stays uncertain."""
from contextlib import closing, contextmanager, nullcontext
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import urllib.error
from urllib.parse import parse_qs

import finance_gate
import market_catalog
import market_health
from ui import document


SCHEMA = (
    'CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY, key_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
        fingerprint TEXT NOT NULL, contract TEXT NOT NULL, slot TEXT NOT NULL, version TEXT NOT NULL,
        created INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL, body BLOB, result BLOB,
        UNIQUE(key_id,request_id))''',
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, proposal TEXT, at INTEGER NOT NULL, event TEXT NOT NULL)''',
)
STATUS = {'pending':'Ожидает подтверждения','expired':'Срок подтверждения истёк','rejected':'Отклонено владельцем',
          'unknown':'Требуется сверка результата на площадке','accepted':'Площадка подтвердила приём',
          'denied':'Площадка отклонила запрос','held':'Отправка остановлена до обращения к площадке',
          'reconciled_applied':'Владелец подтвердил выполнение на площадке',
          'reconciled_absent':'Владелец проверил отсутствие изменения на площадке'}


class ActionError(ValueError):
    pass


def _json(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _version(row):
    return hashlib.sha256(_json(row).encode()).hexdigest()


def fingerprint(operation, inputs):
    return _version(market_catalog.normalized(market_catalog.routes()[operation],inputs))


@contextmanager
def exclusive(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'r+b') as handle:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX)
        yield


@contextmanager
def transmission(dispatch, arguments):
    """The sending process must own this fence even if its WSGI parent dies."""
    if not isinstance(dispatch,dict):
        raise finance_gate.LocalPreparationError('Missing approval')
    path,pid=dispatch['journal'],dispatch['proposal']
    with exclusive(path+'.wire.lock'):
        uri=Path(path).resolve().as_uri()+'?mode=rw'
        with closing(sqlite3.connect(uri,uri=True,timeout=1)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT status,operation,fingerprint FROM proposals WHERE id=?",(pid,)).fetchone()
            current=db.execute('SELECT paused FROM control WHERE id=1').fetchone()
            approval=db.execute("SELECT max(id) FROM events WHERE proposal=? AND event='owner_approved'",(pid,)).fetchone()[0]
            stop=db.execute("SELECT coalesce(max(id),0) FROM events WHERE event='paused'").fetchone()[0]
            sent=db.execute("SELECT 1 FROM events WHERE proposal=? AND event='wire_started'",(pid,)).fetchone()
            if (not row or row!=('unknown',arguments['operation_id'],fingerprint(arguments['operation_id'],arguments['input']))
                    or current!=(0,) or not approval or approval<=stop or sent):
                raise finance_gate.LocalPreparationError('Approval is no longer available')
            db.execute("INSERT INTO events(proposal,at,event) VALUES(?,strftime('%s','now'),'wire_started')",(pid,))
        yield


class Store:
    def __init__(self, portal):
        self.portal = portal
        self.path = str(portal.vault.path)+'.market-actions'
        fd = os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        os.close(fd)
        with self.db() as db:
            for ddl in SCHEMA:
                db.execute(ddl)
            db.execute('INSERT OR IGNORE INTO control VALUES(1,0)')

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=2)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def now(self):
        return int(self.portal.login.clock())

    def event(self,db,pid,event):
        db.execute('INSERT INTO events(proposal,at,event) VALUES(?,?,?)',(pid,self.now(),event))

    def encrypt(self,pid,kind,obj):
        nonce=secrets.token_bytes(12)
        return nonce+self.portal.vault.aead.encrypt(nonce,_json(obj).encode(),(pid+':'+kind).encode())

    def decrypt(self,pid,kind,blob):
        return json.loads(self.portal.vault.aead.decrypt(blob[:12],blob[12:],(pid+':'+kind).encode()))

    def account(self,row):
        provider=row['market']
        slots=[provider+'_write']
        preferred=market_catalog.category_key(row['host']) if provider=='wb' else None
        for slot in [*slots,preferred,provider]:
            if slot and (snapshot:=self.portal.vault.snapshot(slot)):
                return slot,snapshot[0],snapshot[1]
        raise ActionError('Ключ площадки для этого действия ещё не настроен.')

    def cleanup(self,db):
        db.execute("UPDATE proposals SET status='expired',body=NULL WHERE status='pending' AND expires<=?",(self.now(),))
        db.execute("UPDATE proposals SET body=NULL,result=NULL WHERE created<? AND status NOT IN ('unknown','pending')",
                   (self.now()-30*86400,))

    def propose(self,key,args):
        market_catalog.validate('market_api_propose',args)
        if not key.startswith('cp_') or not self.portal.external_keys.active(key):
            raise ActionError('Для регистрации запроса нужен ключ с правом предложений.')
        row=market_catalog.routes()[args['operation_id']]
        identity=fingerprint(row['operation_id'],args['input'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.cleanup(db)
            old=db.execute('SELECT * FROM proposals WHERE key_id=? AND request_id=?',(key,args['request_id'])).fetchone()
            if old:
                if old['fingerprint']!=identity:
                    raise ActionError('Этот request_id уже использован для других параметров.')
                return self.receipt(old)
            if db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0]:
                raise ActionError('Владелец остановил запись.')
            if db.execute("SELECT 1 FROM proposals WHERE fingerprint=? AND status IN ('pending','unknown')",(identity,)).fetchone():
                raise ActionError('Такое действие уже ожидает подтверждения или сверки.')
            if db.execute('SELECT COUNT(*) FROM proposals WHERE created>?',(self.now()-3600,)).fetchone()[0]>=30:
                raise ActionError('Достигнут часовой лимит предложений.')
            slot,account,version=self.account(row)
            market_catalog.build_request(row,args['input'],account)
            retained=db.execute('SELECT coalesce(sum(coalesce(length(body),0)+coalesce(length(result),0)),0) FROM proposals').fetchone()[0]
            if retained+len(_json(args['input']).encode())>32*1024*1024:
                raise ActionError('Хранилище ожидающих операций заполнено.')
            pid=secrets.token_hex(16)
            db.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                       (pid,key,args['request_id'],row['operation_id'],identity,_version(row),slot,version,
                        self.now(),self.now()+900,'pending',self.encrypt(pid,'input',args['input'])))
            self.event(db,pid,'proposed')
            return self.receipt(db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone())

    def receipt(self,row):
        result=dict(proposal_id=row['id'],operation_id=row['operation'],status=row['status'],
                    created_at=row['created'],expires_at=row['expires'])
        if row['result']:
            result['outcome']=self.decrypt(row['id'],'result',row['result'])
        return result

    def status(self,key,pid):
        with self.db() as db:
            self.cleanup(db)
            row=db.execute('SELECT * FROM proposals WHERE id=? AND key_id=?',(pid,key)).fetchone()
            if row is None:
                raise ActionError('Запрос с таким идентификатором у этого ключа отсутствует.')
            return self.receipt(row)

    @contextmanager
    def dispatch_lock(self):
        # Process death releases the lock. The committed unknown receipt remains.
        with exclusive(self.path+'.lock'):
            yield

    def pause(self,paused):
        with self.db() as db:
            db.execute('UPDATE control SET paused=? WHERE id=1',(int(paused),))
            self.event(db,None,'paused' if paused else 'resumed')
        if paused:
            with self.dispatch_lock(), exclusive(self.path+'.wire.lock'):
                pass

    def decision(self,pid,action,fields):
        if not re.fullmatch(r'[0-9a-f]{32}',pid):
            raise ActionError('Некорректный номер операции.')
        reconciles=action in ('reconciled_applied','reconciled_absent')
        with self.dispatch_lock(), (exclusive(self.path+'.wire.lock') if reconciles else nullcontext()):
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                self.cleanup(db)
                p=db.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone()
                if not p:
                    raise ActionError('Операция отсутствует.')
                if action in ('reconciled_applied','reconciled_absent'):
                    evidence=fields.get('evidence','').strip()
                    if p['status']!='unknown' or not 4<=len(evidence)<=200:
                        raise ActionError('Для сверки укажите проверенный документ или номер операции в кабинете площадки.')
                    db.execute('UPDATE proposals SET status=?,result=? WHERE id=?',(action,self.encrypt(pid,'result',{'owner_evidence':evidence}),pid))
                    self.event(db,pid,action)
                    return
                if p['status']!='pending':
                    raise ActionError('Этот запрос уже обработан или просрочен.')
                if action=='reject':
                    db.execute("UPDATE proposals SET status='rejected',body=NULL WHERE id=?",(pid,))
                    self.event(db,pid,'rejected')
                    return
                row=market_catalog.routes().get(p['operation'])
                if (action!='approve' or row is None or row['category']!='write' or not hmac.compare_digest(_version(row),p['contract'])
                        or not hmac.compare_digest(fields.get('fingerprint',''),p['fingerprint']) or fields.get('confirm')!='yes'):
                    raise ActionError('Проверьте актуальные параметры и подтвердите отправку.')
                if publication(row) and fields.get('rights')!='yes':
                    raise ActionError('Для публикации подтвердите права на модель.')
                if db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0] or not self.portal.external_keys.active(p['key_id']):
                    raise ActionError('Запись остановлена или ключ приложения отозван.')
                if db.execute("SELECT COUNT(*) FROM events WHERE event='owner_approved' AND at>?",(self.now()-3600,)).fetchone()[0]>=10:
                    raise ActionError('Достигнут лимит десяти отправок в час.')
                slot,creds,version=self.account(row)
                if slot!=p['slot'] or version!=p['version']:
                    raise ActionError('Ключ площадки заменён. Создайте новый запрос.')
                inputs=self.decrypt(pid,'input',p['body'])
                if fingerprint(row['operation_id'],inputs)!=p['fingerprint']:
                    raise ActionError('Параметры запроса изменились.')
                market_catalog.build_request(row,inputs,creds)
                db.execute("UPDATE proposals SET status='unknown' WHERE id=?",(pid,))
                self.event(db,pid,'owner_approved')
            args={'operation_id':row['operation_id'],'input':inputs,'request_id':p['request_id']}
            try:
                if self.portal.gateway is self.portal.native_provider:
                    result=self.portal.native_provider('market_api_propose',args,creds,self.portal.finance_gate_dir,
                                                       dispatch={'journal':self.path,'proposal':pid})
                else:
                    result=self.portal.gateway('market_api_propose',args,creds)
                status=result.get('operation_status','unknown')
                if status not in ('accepted','denied','unknown'):
                    status='unknown'
            except urllib.error.HTTPError as error:
                status='denied' if error.code in (400,401,403,404,405,413,415,422,429) else 'unknown'
                result={'http_status':error.code}
                if error.code==429:
                    result['retry_after_seconds']=getattr(error,'retry_after_seconds',None)
                error.close()
            except finance_gate.GateError as error:
                status='held' if error.reason in ('busy','cooldown','retry_time_unknown') else 'unknown'
                result={'reason':error.reason,'retry_after_seconds':error.retry_after_seconds}
            except finance_gate.LocalPreparationError:
                status,result='held',{'reason':'local_preparation'}
            except Exception:
                status,result='unknown',{'reason':'check_marketplace_before_retry'}
            with self.db() as db:
                db.execute('UPDATE proposals SET status=?,result=? WHERE id=?',(status,self.encrypt(pid,'result',result),pid))
                self.event(db,pid,status)


def publication(row):
    return row['path'] in ('/v3/product/import','/v1/product/import-by-sku','/v1/product/unarchive',
        '/v2/product/pictures/import','/v1/product/attributes/update','/content/v2/cards/upload','/content/v2/cards/update',
        '/content/v2/cards/recover','/content/v2/cards/upload/add','/content/v3/media/file','/content/v3/media/save')


def display_inputs(value):
    if isinstance(value,dict):
        return {k:display_inputs(v) for k,v in value.items()}
    if isinstance(value,list):
        return [display_inputs(v) for v in value]
    if isinstance(value,str) and len(value)>8192:
        return {'length':len(value),'sha256_utf8':hashlib.sha256(value.encode()).hexdigest(),'preview':value[:120]}
    return value


def owner_page(store,csrf,fresh):
    esc=lambda v:html.escape(str(v),quote=True)
    hidden='<input type="hidden" name="csrf" value="'+esc(csrf)+'">'
    def form(action,extra='',label=None):
        return ('<form method="post" action="/settings/catalog">'+hidden+'<input type="hidden" name="action" value="'+action+'">'+extra
                +'<button>'+esc(label or action)+'</button></form>')
    with store.db() as db:
        store.cleanup(db)
        paused=db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0]
        rows=db.execute("SELECT * FROM proposals ORDER BY (status IN ('pending','unknown')) DESC,created DESC,id DESC LIMIT 60").fetchall()
    content='<main id="content"><p><a href="/settings">Настройки</a></p><h1>Операции маркетплейсов</h1>'
    content+='<p>'+('Отправка остановлена.' if paused else 'Каждую отправку подтверждает владелец. До десяти отправок в час.')+'</p>'
    content+=form('resume' if paused else 'pause',label='Возобновить отправку' if paused else 'Остановить отправку')
    if not fresh:
        content+='<p>Для подтверждения операции и изменения ключей войдите заново по ссылке из письма.</p>'
    if fresh:
        content+='<details><summary>Ключи площадок для записи</summary><p>Можно указать отдельные ключи. По умолчанию используется сохранённый ключ кабинета с выданными площадкой правами.</p>'
        for provider,fields in [('ozon_write',('client_id','api_key')),('wb_write',('api_token',))]:
            content+='<form method="post" action="/settings">'+hidden+'<input type="hidden" name="provider" value="'+provider+'">'
            for field in fields:
                label={'client_id':'Ozon Client ID','api_key':'Ozon API Key','api_token':'Токен WB для записи'}[field]
                content+='<label>'+label+'<input type="password" name="'+field+'" maxlength="2048" required autocomplete="off"></label>'
            content+='<button>Сохранить</button></form>'
        content+='</details>'
        content+='<details><summary>Категорийные ключи WB для каталога</summary>'
        for slot,label in market_catalog.WB_EXTRA.values():
            content+='<form method="post" action="/settings">'+hidden+'<input type="hidden" name="provider" value="'+slot+'"><label>'+label+'<input type="password" name="api_token" maxlength="2048" required autocomplete="off"></label><button>Сохранить</button></form>'
        content+='</details>'
    if not rows:
        content+='<p>Запросов от приложений пока нет.</p>'
    clients={identifier:name for identifier,name,_,_ in store.portal.external_keys.list_active()}
    for p in rows:
        row=market_catalog.routes().get(p['operation'])
        title=row['title'] if row else p['operation']
        content+='<section class="market-card"><h2>'+esc(title)+'</h2><p>'+esc(STATUS[p['status']])+'</p><p><code>'+esc(p['operation'])+'</code> · '+esc(p['id'])+'</p>'
        content+='<p>Приложение '+esc(clients.get(p['key_id'],'(ключ отозван)'))+' · Ключ кабинета '+esc(p['slot'])+' · версия '+esc(p['version'][:12])+'</p>'
        if p['status']=='pending' and row:
            try:
                slot,account,version=store.account(row)
                if slot==p['slot'] and version==p['version'] and row['market']=='ozon':
                    content+='<p>Ozon Client ID '+esc(account['client_id'])+'</p>'
            except ActionError:
                pass
        pid='<input type="hidden" name="pid" value="'+esc(p['id'])+'">'
        if row:
            content+='<p><code>'+esc(row['method']+' '+row['host']+row['path'])+'</code></p>'
        if p['body']:
            content+='<details'+(' open' if p['status']=='pending' else '')+'><summary>Параметры запроса</summary><pre>'+esc(json.dumps(display_inputs(store.decrypt(p['id'],'input',p['body'])),ensure_ascii=False,indent=2))+'</pre></details>'
        if p['result']:
            content+='<details><summary>Результат</summary><pre>'+esc(_json(display_inputs(store.decrypt(p['id'],'result',p['result']))))+'</pre></details>'
        if fresh and p['status']=='pending' and row:
            fields=pid+'<input type="hidden" name="fingerprint" value="'+esc(p['fingerprint'])+'"><label><input type="checkbox" name="confirm" value="yes" required>Подтверждаю отправку этих параметров в кабинет площадки</label>'
            if publication(row):
                fields+='<label><input type="checkbox" name="rights" value="yes" required>Права на модель улажены</label>'
            if not paused:
                content+=form('approve',fields,'Подтвердить и отправить')
            content+=form('reject',pid,'Отклонить')
        if fresh and p['status']=='unknown':
            evidence=pid+'<label>Проверенный номер операции или документ на площадке<input name="evidence" required minlength="4" maxlength="200"></label>'
            content+=form('reconciled_applied',evidence,'Подтвердить выполнение после сверки')+form('reconciled_absent',evidence,'Зафиксировать отсутствие изменения после сверки')
        content+='</section>'
    return document('Операции маркетплейсов',content+'</main>')


def handle(portal,e,method,session,now,body,reply,start):
    store=Store(portal)
    fresh=session[0]-43200>=now-300
    if method=='GET':
        return reply(owner_page(store,session[1],fresh))
    if e.get('HTTP_ORIGIN')!=portal.login.origin:
        return reply('Forbidden','403 Forbidden')
    fields=parse_qs(body('application/x-www-form-urlencoded'),max_num_fields=7,keep_blank_values=True,strict_parsing=True)
    if any(len(v)!=1 for v in fields.values()):
        raise ValueError('Duplicate fields')
    fields={k:v[0] for k,v in fields.items()}
    if not hmac.compare_digest(fields.get('csrf',''),session[1]):
        return reply('Forbidden','403 Forbidden')
    action=fields.get('action')
    if action!='pause' and not fresh:
        return reply('Для этого действия войдите заново по ссылке из письма.','403 Forbidden')
    if action in ('pause','resume'):
        store.pause(action=='pause')
    else:
        store.decision(fields.get('pid',''),action,fields)
    start('303 See Other',[('Location','/settings/catalog'),('Cache-Control','no-store'),('Content-Length','0')])
    return [b'']
