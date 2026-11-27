"""Owner opt-in for the budget gateway using the existing encrypted vault.

The state directory and vault path are fixed by the trusted composition root,
never by HTTP input. Creating a DB and committing the vault span two files; a
crash between them leaves an orphan which is refused, never silently reset.
No provider requests are made during setup, load or page rendering.
"""
import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import closing
from contextlib import contextmanager

from content_factory_n8n_budget import LiveBudgetGateway

VAULT_PATH='/var/lib/3dprinta-portal/vault.sqlite'
DIRECTORY='.content-factory-budget-gateway'
DATABASE='content-factory-gateway.sqlite3'
SLOT='content_factory_budget_gateway_setup'
TOKEN_SLOT='n8n_content_factory_budget_write'
# Enables the owner setup form only. External writes remain stopped until the
# owner supplies a separate token, confirms its scope and explicitly arms it.
FEATURE_AVAILABLE=True


class GatewayBusyError(RuntimeError):
    """Another worker owns the gateway; no operation was started here."""


@contextmanager
def _gate(parent, timeout=0):
    """One OS-level lock shared by every worker and state transition."""
    path=os.path.join(parent,'.content-factory-budget-gateway.lock')
    fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0),0o600)
    locked=False
    try:
        st=os.fstat(fd); at=os.lstat(path)
        if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(at.st_mode) or st.st_nlink!=1
                or st.st_ino!=at.st_ino or st.st_dev!=at.st_dev): raise ValueError('Invalid gateway lock')
        if os.name=='posix' and (st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)&0o077): raise ValueError('Invalid gateway lock')
        deadline=time.monotonic()+timeout
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True; break
            except (OSError,BlockingIOError):
                if time.monotonic()>=deadline: raise GatewayBusyError('Gateway lock unavailable') from None
                time.sleep(.05)
        yield
    finally:
        if locked:
            if os.name=='nt':
                import msvcrt
                os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def _budget_token_reader(vault):
    def read():
        try:
            data=vault.read(TOKEN_SLOT)
            token=data.get('api_key') if isinstance(data,dict) else None
            if not isinstance(token,str) or not re.fullmatch(r'[!-~]{16,2048}',token): raise ValueError()
            return token
        except Exception:
            raise RuntimeError('Budget gateway token unavailable') from None
    return read


def _base(vault, expected_vault_path):
    actual=vault.path
    if not all(isinstance(p,str) and os.path.isabs(p) for p in (actual,expected_vault_path)): raise ValueError('Invalid vault path')
    if os.path.normcase(os.path.realpath(actual)) != os.path.normcase(os.path.realpath(expected_vault_path)): raise ValueError('Unexpected vault path')
    st=os.lstat(actual)
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1: raise ValueError('Invalid vault file')
    parent=os.path.dirname(os.path.abspath(actual))
    pst=os.lstat(parent)
    if stat.S_ISLNK(pst.st_mode):
        # systemd DynamicUser StateDirectory uses this root-owned link. Do not
        # accept a configurable symlink or another target, even if it exists.
        if (parent != '/var/lib/3dprinta-portal' or pst.st_uid != 0
                or os.readlink(parent) != 'private/3dprinta-portal'
                or os.path.realpath(parent) != '/var/lib/private/3dprinta-portal'):
            raise ValueError('Invalid state directory')
        parent=os.path.realpath(parent); pst=os.lstat(parent)
    if not stat.S_ISDIR(pst.st_mode) or stat.S_ISLNK(pst.st_mode) or os.path.realpath(parent) != parent: raise ValueError('Invalid state directory')
    if os.name=='posix' and (stat.S_IMODE(pst.st_mode)&0o077 or pst.st_uid != os.geteuid() or st.st_uid != os.geteuid()):
        raise ValueError('State directory is not private')
    return parent


def _existing_db(parent):
    directory=os.path.join(parent,DIRECTORY); db_path=os.path.join(directory,DATABASE)
    st=os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or os.path.realpath(directory) != directory: raise ValueError('Invalid gateway directory')
    if os.name=='posix' and (stat.S_IMODE(st.st_mode)&0o077 or st.st_uid != os.geteuid()): raise ValueError('Gateway directory is not private or owned by the service')
    allowed={DATABASE,DATABASE+'-journal',DATABASE+'-wal',DATABASE+'-shm'}
    if set(os.listdir(directory))-allowed: raise ValueError('Gateway directory is not dedicated')
    for name in os.listdir(directory):
        entry=os.lstat(os.path.join(directory,name))
        if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode) or entry.st_nlink != 1:
            raise ValueError('Invalid gateway database file')
        if os.name=='posix' and (entry.st_uid != os.geteuid() or stat.S_IMODE(entry.st_mode)&0o077):
            raise ValueError('Gateway database file is not private')
    dst=os.lstat(db_path)
    if not stat.S_ISREG(dst.st_mode) or stat.S_ISLNK(dst.st_mode) or dst.st_nlink != 1: raise ValueError('Invalid gateway database')
    uri='file:'+db_path.replace('\\','/').replace('?','%3F').replace('#','%23')+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=1)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required={'fence_control','fence_proposal','fence_audit','cf_live_budget_snapshot','cf_live_budget_proposals','cf_live_budget_refresh'}
    if not required.issubset(tables) or tables-required-{'fence_reconciliation','sqlite_sequence'}: raise ValueError('Invalid gateway database')
    return db_path


def _decode(vault,slot,blob):
    raw=bytes(blob)
    return json.loads(vault.aead.decrypt(raw[:12],raw[12:],slot.encode()))


def _seal(vault,slot,value):
    nonce=secrets.token_bytes(12)
    return nonce+vault.aead.encrypt(nonce,json.dumps(value,separators=(',',':')).encode(),slot.encode())


def _config(value):
    if (not isinstance(value,dict) or set(value)!={'version','enabled','generation','master'}
            or value['version']!=1 or type(value['enabled']) is not bool
            or type(value['generation']) is not int or value['generation']<1
            or not isinstance(value['master'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['master'])):
        raise ValueError('Invalid gateway configuration')
    key=base64.urlsafe_b64decode(value['master']+'=')
    if len(key)!=32: raise ValueError('Invalid gateway key')
    return key


def enable(vault, token, *, confirm_scope=False, confirm_estimate=False, expected_vault_path=VAULT_PATH):
    parent=_base(vault,expected_vault_path)
    with _gate(parent):
        return _enable_unlocked(vault,token,confirm_scope=confirm_scope,confirm_estimate=confirm_estimate,expected_vault_path=expected_vault_path)


def _enable_unlocked(vault, token, *, confirm_scope=False, confirm_estimate=False, expected_vault_path=VAULT_PATH):
    """Owner-only: caller must separately verify fresh session, Origin, CSRF."""
    if confirm_scope is not True or confirm_estimate is not True: raise PermissionError('Owner confirmation required')
    if not isinstance(token,str) or not re.fullmatch(r'[!-~]{16,2048}',token): raise ValueError('Invalid token')
    parent=_base(vault,expected_vault_path)
    with closing(sqlite3.connect(vault.path,timeout=5,isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT blob FROM vault WHERE provider=?',(SLOT,)).fetchone()
        for slot in ('n8n','n8n_content_factory_write'):
            prior=db.execute('SELECT blob FROM vault WHERE provider=?',(slot,)).fetchone()
            old=_decode(vault,slot,prior[0]) if prior else None
            if isinstance(old,dict) and isinstance(old.get('api_key'),str) and hmac.compare_digest(token,old['api_key']):
                raise ValueError('Use a separate key')
        if row:
            config=_decode(vault,SLOT,row[0]); key=_config(config)
            if config['enabled']: raise ValueError('Already enabled')
            db_path=_existing_db(parent)
            # Disabled state must have durably halted dispatch before re-enable.
            with closing(sqlite3.connect(db_path,timeout=1)) as fence_db:
                if fence_db.execute('SELECT halted FROM fence_control WHERE id=1').fetchone() != (1,): raise ValueError('Gateway is not halted')
            config=dict(config,enabled=True,generation=config['generation']+1)
        else:
            directory=os.path.join(parent,DIRECTORY)
            if os.path.lexists(directory): raise ValueError('Unreconciled gateway state')
            os.mkdir(directory,0o700)
            st=os.lstat(directory)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or os.path.realpath(directory) != directory: raise ValueError('Invalid gateway directory')
            db_path=os.path.join(directory,DATABASE)
            flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
            fd=os.open(db_path,flags,0o600)
            os.close(fd)
            key=secrets.token_bytes(32)
            # Schema is initialized while the vault transaction serializes all
            # setup attempts; no network call is made by the constructor.
            LiveBudgetGateway(db_path,key,token_reader=lambda:None,configuration_ready=lambda:False)
            config={'version':1,'enabled':True,'generation':1,'master':base64.urlsafe_b64encode(key).decode().rstrip('=')}
        db.execute('INSERT OR REPLACE INTO vault VALUES (?,?)',(TOKEN_SLOT,_seal(vault,TOKEN_SLOT,{'api_key':token})))
        db.execute('INSERT OR REPLACE INTO vault VALUES (?,?)',(SLOT,_seal(vault,SLOT,config)))
        db.commit()


def load(vault, *, expected_vault_path=VAULT_PATH, transport=None):
    parent=_base(vault,expected_vault_path)
    value=vault.read(SLOT)
    if value is None: return None
    key=_config(value)
    if value['enabled'] is not True: return None
    db_path=_existing_db(parent)
    instance=value['master']
    def configured():
        try:
            current=vault.read(SLOT)
            return (_config(current)==key and current['enabled'] is True and current['master']==instance
                    and bool(_budget_token_reader(vault)()))
        except Exception: return False
    args={'token_reader':_budget_token_reader(vault),'configuration_ready':configured}
    if transport is not None: args['transport']=transport
    return LiveBudgetGateway(db_path,key,**args)


def disable(vault, *, expected_vault_path=VAULT_PATH):
    """Halt first, then mark disabled; crash between them stays halted."""
    parent=_base(vault,expected_vault_path)
    with _gate(parent):
        return _disable_unlocked(vault,expected_vault_path=expected_vault_path)


def _disable_unlocked(vault, *, expected_vault_path=VAULT_PATH):
    gateway=load(vault,expected_vault_path=expected_vault_path)
    if gateway is None: raise ValueError('Gateway not enabled')
    gateway.fence.halt()
    with closing(sqlite3.connect(vault.path,timeout=5,isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT blob FROM vault WHERE provider=?',(SLOT,)).fetchone()
        if not row: raise ValueError('Missing gateway state')
        config=_decode(vault,SLOT,row[0]); key=_config(config)
        if not config['enabled'] or not hmac.compare_digest(key,gateway.fence.key): raise ValueError('Gateway changed')
        config=dict(config,enabled=False,generation=config['generation']+1)
        db.execute('UPDATE vault SET blob=? WHERE provider=?',(_seal(vault,SLOT,config),SLOT))
        db.commit()


class Proxy:
    """Per-worker lazy cache; every call checks the durable vault revision."""
    source='n8n_live'
    def __init__(self,vault,*,expected_vault_path=VAULT_PATH,transport=None):
        self.vault,self.expected,self.transport=vault,expected_vault_path,transport
        self._lock=threading.Lock(); self._version=None; self._gateway=None
        self._startup_failure=False
        try:
            parent=_base(vault,expected_vault_path)
            # Every fresh worker halts the shared fence. After a restart or a
            # restored snapshot the owner must explicitly inspect n8n and arm.
            # Simultaneous gunicorn worker startups can briefly contend while
            # each one halts the shared fence. Wait only here, never on web
            # actions; a longer-running operation still fails this worker shut.
            with _gate(parent, timeout=2):
                gateway=self._get()
                if gateway is not None: gateway.fence.halt()
        except Exception:
            self._startup_failure=True

    def enabled(self):
        try:
            item=self.vault.snapshot(SLOT)
            if not item: return False
            value,_=item; _config(value)
            return value['enabled'] is True
        except Exception: return False

    def _get(self):
        item=self.vault.snapshot(SLOT)
        if not item: return None
        value,version=item; _config(value)
        if value['enabled'] is not True: return None
        with self._lock:
            if self._version != version:
                gateway=load(self.vault,expected_vault_path=self.expected,transport=self.transport)
                self._gateway,self._version=gateway,version
            return self._gateway

    def _require(self):
        gateway=self._get()
        if gateway is None: raise PermissionError('Gateway unavailable')
        return gateway

    def ready(self):
        try:
            if self._startup_failure: return False
            gateway=self._get()
            return gateway is not None and gateway.ready() is True
        except Exception: return False
    @property
    def fence(self): return self._require().fence
    def token_reader(self): return self._require().token_reader()
    def settings(self): return self._require().settings()
    def owner_arm(self):
        if self._startup_failure: raise PermissionError('Gateway unavailable')
        with _gate(_base(self.vault,self.expected)):
            gateway=self._require()
            gateway.token_reader()
            gateway.fence.arm(lambda: gateway.configuration_ready())
    def owner_halt(self):
        if self._startup_failure: raise PermissionError('Gateway unavailable')
        with _gate(_base(self.vault,self.expected)):
            self._require().fence.halt()
    def refresh(self):
        with _gate(_base(self.vault,self.expected)): return self._require().refresh()
    def prepare(self,*args):
        with _gate(_base(self.vault,self.expected)): return self._require().prepare(*args)
    def confirm(self,*args):
        with _gate(_base(self.vault,self.expected)): return self._require().confirm(*args)
