"""Durable, one-at-a-time cutover jobs with a whole-operation deadline.

The supervisor and worker are separate spawned processes; request handlers only
enqueue and poll. Secrets are passed through local process IPC, never argv or
database plaintext. A crash/timeout is ambiguous and blocks new jobs.
"""
import hashlib
import hmac
import json
import multiprocessing
import secrets
import sqlite3
import time
from contextlib import closing
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OPERATIONS = {'pause_prepare','pause_confirm','restore_prepare','restore_confirm',
              'draft_prepare','draft_confirm','publish_prepare','publish_confirm'}
READ_ONLY = {'pause_prepare','restore_prepare','draft_prepare','publish_prepare'}


class Store:
    def __init__(self,path,key,clock=time.time):
        if not isinstance(key,bytes) or len(key)!=32: raise ValueError('Invalid job key')
        self.path,self.key,self.clock=str(path),key,clock
        self.aead=AESGCM(key)
        with closing(self.db()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS cf_cutover_jobs (
              id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, operation TEXT NOT NULL,
              state TEXT NOT NULL, payload BLOB NOT NULL, result BLOB, created REAL NOT NULL, updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS cf_cutover_job_audit (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
                owner_hash TEXT NOT NULL, operation TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL);''')

    def db(self): return sqlite3.connect(self.path, timeout=3, isolation_level=None)
    def owner_hash(self,owner):
        if not isinstance(owner,str) or len(owner)<16: raise ValueError('Invalid owner')
        return hmac.new(self.key,b'job-owner\0'+owner.encode(),hashlib.sha256).hexdigest()
    def _seal(self,ident,value):
        raw=json.dumps(value,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>16384: raise ValueError('Job data too large')
        nonce=secrets.token_bytes(12)
        return nonce+self.aead.encrypt(nonce,raw,('cutover-job:'+ident).encode())
    def _open(self,ident,blob):
        raw=bytes(blob)
        return json.loads(self.aead.decrypt(raw[:12],raw[12:],('cutover-job:'+ident).encode()))

    def create(self,owner,operation,payload):
        if operation not in OPERATIONS or not isinstance(payload,dict): raise ValueError('Invalid job')
        ident=secrets.token_urlsafe(24); oh=self.owner_hash(owner); now=self.clock()
        encrypted=self._seal(ident,{'owner':owner,'payload':payload})
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM cf_cutover_jobs WHERE state IN ('queued','running','unknown')").fetchone()[0]:
                db.rollback(); raise PermissionError('An existing job needs attention')
            db.execute('INSERT INTO cf_cutover_jobs VALUES (?,?,?,?,?,?,?,?)',(ident,oh,operation,'queued',encrypted,None,now,now))
            db.execute('INSERT INTO cf_cutover_job_audit(job_id,owner_hash,operation,event,at) VALUES (?,?,?,?,?)',(ident,oh,operation,'queued',now))
            db.commit()
        return ident

    def claim(self,ident):
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT owner_hash,operation,state,payload FROM cf_cutover_jobs WHERE id=?',(ident,)).fetchone()
            if not row or row[2]!='queued': db.rollback(); raise PermissionError('Job unavailable')
            now=self.clock()
            db.execute("UPDATE cf_cutover_jobs SET state='running',updated=? WHERE id=?",(now,ident))
            db.execute('INSERT INTO cf_cutover_job_audit(job_id,owner_hash,operation,event,at) VALUES (?,?,?,?,?)',(ident,row[0],row[1],'running',now))
            db.commit()
        data=self._open(ident,row[3])
        return row[1],data['owner'],data['payload']

    def finish(self,ident,result):
        if not isinstance(result,dict): raise ValueError('Invalid job result')
        encrypted=self._seal(ident,result)
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT owner_hash,operation,state FROM cf_cutover_jobs WHERE id=?',(ident,)).fetchone()
            if not row or row[2]!='running': db.rollback(); raise PermissionError('Job unavailable')
            now=self.clock()
            db.execute("UPDATE cf_cutover_jobs SET state='completed',result=?,updated=? WHERE id=?",(encrypted,now,ident))
            db.execute('INSERT INTO cf_cutover_job_audit(job_id,owner_hash,operation,event,at) VALUES (?,?,?,?,?)',(ident,row[0],row[1],'completed',now))
            db.commit()

    def unknown(self,ident):
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT owner_hash,operation,state FROM cf_cutover_jobs WHERE id=?',(ident,)).fetchone()
            if row and row[2] in ('queued','running'):
                now=self.clock()
                db.execute("UPDATE cf_cutover_jobs SET state='unknown',updated=? WHERE id=?",(now,ident))
                db.execute('INSERT INTO cf_cutover_job_audit(job_id,owner_hash,operation,event,at) VALUES (?,?,?,?,?)',(ident,row[0],row[1],'unknown',now))
            db.commit()

    def status(self,owner,ident):
        with closing(self.db()) as db:
            row=db.execute('SELECT owner_hash,state,result,updated FROM cf_cutover_jobs WHERE id=?',(ident,)).fetchone()
        if not row or not hmac.compare_digest(row[0],self.owner_hash(owner)): raise PermissionError('Job unavailable')
        if row[1] in ('queued','running') and self.clock()-row[3] > 130:
            self.unknown(ident)
            return self.status(owner,ident)
        return {'state':row[1],'result':self._open(ident,row[2]) if row[2] is not None else None}


def execute_real(operation,owner,payload):
    """Called only in isolated worker; never logs exceptions or credentials."""
    import portal
    app=portal.create_portal()
    cutover=app.ai_cutover
    if cutover is None: raise PermissionError('Cutover unavailable')
    from content_factory_cutover import Pause
    pause=Pause(cutover.fence,cutover.wire)
    if operation=='pause_prepare' and not payload:
        return {'kind':'pause_preview', **pause.prepare(owner)}
    if operation=='pause_confirm' and set(payload)=={'proposal_id'}:
        return {'kind':'pause_result', **pause.confirm(owner,payload['proposal_id'])}
    if operation=='restore_prepare' and set(payload)=={'receipt'}:
        return {'kind':'restore_preview','proposal_id':pause.prepare_restore(owner,payload['receipt']),'receipt':payload['receipt']}
    if operation=='restore_confirm' and set(payload)=={'receipt','proposal_id'}:
        return {'kind':'restore_result','status':pause.confirm_restore(owner,payload['proposal_id'],payload['receipt'])}
    if operation=='draft_prepare' and set(payload)=={'pause_receipt'}:
        return {'kind':'draft_preview', 'pause_receipt':payload['pause_receipt'], **cutover.prepare_draft(owner,payload['pause_receipt'])}
    if operation=='draft_confirm' and set(payload)=={'proposal_id','pause_receipt'}:
        return {'kind':'draft_result', **cutover.confirm_draft(owner,payload['proposal_id'],payload['pause_receipt'])}
    if operation=='publish_prepare' and set(payload)=={'receipt'}:
        return {'kind':'publish_preview', 'proposal_id':cutover.prepare_publish(owner,payload['receipt']), 'receipt':payload['receipt']}
    if operation=='publish_confirm' and set(payload)=={'receipt','proposal_id'}:
        return {'kind':'publish_result', 'status':cutover.confirm_publish(owner,payload['proposal_id'],payload['receipt'])}
    raise ValueError('Invalid operation')


def _worker(path,key,ident,function):
    store=Store(path,key)
    operation=None
    try:
        operation,owner,payload=store.claim(ident)
        result=function(operation,owner,payload)
        store.finish(ident,result)
    except BaseException:
        try:
            if operation in READ_ONLY:
                store.finish(ident,{'kind':'rejected_before_send'})
            else:
                store.unknown(ident)
        except Exception: pass


def _supervisor(path,key,ident,function,deadline):
    store=Store(path,key)
    context=multiprocessing.get_context('spawn')
    process=context.Process(target=_worker,args=(path,key,ident,function))
    try:
        process.start(); process.join(deadline)
        if process.is_alive():
            process.terminate(); process.join(2)
            if process.is_alive(): process.kill(); process.join(1)
        if process.exitcode != 0: store.unknown(ident)
    except BaseException:
        try: store.unknown(ident)
        except Exception: pass
    finally:
        if process.pid is not None and not process.is_alive(): process.close()


def start(store,owner,operation,payload,*,deadline=55,function=execute_real):
    if not 1<=deadline<=120 or not callable(function): raise ValueError('Invalid job supervisor')
    ident=store.create(owner,operation,payload)
    context=multiprocessing.get_context('spawn')
    supervisor=context.Process(target=_supervisor,args=(store.path,store.key,ident,function,deadline))
    try:
        supervisor.start()
    except Exception:
        store.unknown(ident)
        raise RuntimeError('Job unavailable') from None
    # The process deliberately outlives this request; no secrets in its argv.
    return ident
