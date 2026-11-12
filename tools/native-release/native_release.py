"""Manual single-unit release switch. Never provisions, installs or migrates data."""
import argparse, contextlib, fcntl, hashlib, json, os, pathlib, re, stat, subprocess, sys, time, urllib.request

UNIT = '3dprinta-portal.service'
ROOT = pathlib.Path('/opt/3dprinta-portal')
STATE = pathlib.Path('/var/lib/3dprinta-native-deployer')
class Refused(Exception): pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused('health redirect refused')

def secure(path, directory=False):
    path = pathlib.Path(path)
    if not path.is_absolute() or '..' in path.parts: raise Refused('absolute normalized path required')
    for p in [*reversed(path.parents), path]:
        s = p.lstat()
        if stat.S_ISLNK(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise Refused('untrusted path')
    if directory and not path.is_dir(): raise Refused('directory required')
    return path

def syncdir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def write_json(path, data):
    tmp=path.with_suffix('.new')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path);syncdir(path.parent)

def verify_release(root, name, digest):
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}',name): raise Refused('invalid release name')
    if not re.fullmatch('[0-9a-f]{64}',digest): raise Refused('expected manifest SHA256 required')
    release=secure(root/'releases'/name,True)
    manifest=secure(release/'MANIFEST.json')
    raw=manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest: raise Refused('manifest digest mismatch')
    files=json.loads(raw)
    if not isinstance(files,dict) or not files: raise Refused('invalid manifest')
    actual=set()
    for p in release.rglob('*'):
        secure(p)
        if p.is_file() and p!=manifest: actual.add(str(p.relative_to(release)))
    if actual!=set(files): raise Refused('release file inventory mismatch')
    for name,expected in files.items():
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected): raise Refused('invalid file hash')
        p=release/name
        if hashlib.sha256(p.read_bytes()).hexdigest()!=expected: raise Refused('release file hash mismatch')
    if not {'service/portal.py','service/gunicorn.conf.py','venv/bin/gunicorn','venv/bin/python'}<=actual:
        raise Refused('incomplete prepared release')
    return release

class Controller:
    def __init__(self,root=ROOT,state=STATE,runner=None,health=None):
        self.root,self.state=root,state
        self.runner=runner or self.command
        self.health=health or self.http_health
    @staticmethod
    def command(*args):
        try:
            r=subprocess.run(['/usr/bin/systemctl',*args,UNIT],capture_output=True,text=True,timeout=60,check=False)
        except (subprocess.TimeoutExpired,OSError): raise Refused('systemctl outcome unknown; journal retained; inspect host before recover') from None
        if r.returncode: raise Refused('systemctl failed; journal retained; inspect host before recover')
        return r.stdout
    @staticmethod
    def http_health():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        req=urllib.request.Request('http://127.0.0.1:18731/',headers={'Host':'3dprinta.shop'})
        with opener.open(req,timeout=5) as r:
            if r.status!=200: raise Refused('health check failed')
    def current(self):
        p=self.root/'current'
        if not p.is_symlink(): raise Refused('current must be a release symlink')
        target=p.resolve(strict=True)
        if target.parent!=self.root/'releases': raise Refused('current outside release root')
        return target.name
    def switch(self,name):
        temp=self.root/'current.pending'
        if temp.exists() or temp.is_symlink(): raise Refused('stale symlink intent; inspect first')
        temp.symlink_to('releases/'+name)
        os.replace(temp,self.root/'current');syncdir(self.root)
    def check_unit(self,digest):
        if not re.fullmatch('[0-9a-f]{64}',digest): raise Refused('unit SHA256 required')
        unit=self.runner('cat','--no-pager')
        if hashlib.sha256(unit.encode()).hexdigest()!=digest: raise Refused('unit content mismatch')
        # Exact inspected unit hash covers drop-ins. Require release-based start, no writable app cwd.
        shown=self.runner('show','--property=WorkingDirectory','--property=ExecStart','--property=NeedDaemonReload')
        if 'NeedDaemonReload=no' not in shown or f'WorkingDirectory={self.root}/current/service' not in shown or f'{self.root}/current/venv/bin/gunicorn' not in shown:
            raise Refused('unsupported loaded unit; no daemon-reload performed')
    def ready(self):
        for attempt in range(20):
            self.runner('is-active','--quiet')
            try:
                self.health();return
            except OSError:
                if attempt==19: raise Refused('readiness deadline exceeded; journal retained') from None
                time.sleep(.5)
    def no_jobs(self):
        if self.runner('list-jobs','--no-legend','--no-pager').strip():
            raise Refused('unsettled unit job; inspect first')
    def deploy(self,new,new_hash,old_hash,unit_hash):
        pending=self.state/'pending.json'
        if pending.exists(): raise Refused('unfinished operation; inspect and explicitly recover')
        old=self.current()
        if old==new: raise Refused('already current')
        verify_release(self.root,new,new_hash);verify_release(self.root,old,old_hash)
        self.check_unit(unit_hash)
        self.no_jobs()
        self.runner('is-active','--quiet')
        write_json(pending,{'old':old,'new':new,'old_hash':old_hash,'new_hash':new_hash,'unit_hash':unit_hash})
        self.runner('stop')
        # Confirm shutdown before touching current; timeout/nonzero leaves intent intact.
        if self.runner('show','--property=ActiveState','--value').strip()!='inactive': raise Refused('unit not confirmed inactive')
        self.switch(new)
        self.runner('start');self.ready()
        write_json(self.state/'last.json',json.loads(pending.read_text()))
        pending.unlink();syncdir(self.state)
    def recover(self):
        pending=self.state/'pending.json'
        data=json.loads(secure(pending).read_text())
        verify_release(self.root,data['old'],data['old_hash'])
        verify_release(self.root,data['new'],data['new_hash'])
        self.check_unit(data['unit_hash'])
        self.no_jobs()
        if self.current() not in {data['old'],data['new']}: raise Refused('unexpected current release')
        self.runner('stop')
        if self.runner('show','--property=ActiveState','--value').strip()!='inactive': raise Refused('unit not confirmed inactive')
        temp=self.root/'current.pending'
        if temp.is_symlink() and temp.lstat().st_uid==0 and os.readlink(temp) in {'releases/'+data['old'],'releases/'+data['new']}:
            temp.unlink();syncdir(self.root)
        self.switch(data['old']);self.runner('start');self.ready()
        pending.unlink();syncdir(self.state)

@contextlib.contextmanager
def lock(state):
    fd=os.open(state/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally: os.close(fd)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['check','deploy','rollback','recover'])
    p.add_argument('--release');p.add_argument('--manifest-sha256');p.add_argument('--current-manifest-sha256');p.add_argument('--unit-sha256')
    p.add_argument('--execute',action='store_true');p.add_argument('--data-compatible',action='store_true')
    p.add_argument('--confirmed-no-systemd-jobs',action='store_true')
    a=p.parse_args()
    try:
        if os.geteuid()!=0: raise Refused('root required for trusted release paths')
        secure(ROOT,True);secure(ROOT/'releases',True);secure(STATE,True)
        if STATE.stat().st_mode&0o077: raise Refused('state must be private')
        c=Controller()
        with lock(STATE):
            if a.action=='check':
                verify_release(ROOT,a.release or '',a.manifest_sha256 or '');c.check_unit(a.unit_sha256 or '')
            else:
                if not a.execute or not a.data_compatible: raise Refused('explicit execute and data-compatible acknowledgement required')
                if a.action=='recover':
                    if not a.confirmed_no_systemd_jobs: raise Refused('inspect systemd jobs and reconcile uncertain outcome first')
                    c.recover()
                elif a.action=='rollback':
                    last=json.loads(secure(STATE/'last.json').read_text())
                    if c.current()!=last['new']: raise Refused('last release no longer current')
                    c.deploy(last['old'],last['old_hash'],last['new_hash'],a.unit_sha256 or '')
                else: c.deploy(a.release or '',a.manifest_sha256 or '',a.current_manifest_sha256 or '',a.unit_sha256 or '')
        print('Completed; no credentials, data or other units modified.')
    except (Refused,OSError,ValueError,TypeError,KeyError):
        print('Refused or incomplete. Preserve journal and inspect trusted configuration/systemd state; no automatic retry.',file=sys.stderr);return 1
    return 0

if __name__=='__main__': sys.exit(main())
