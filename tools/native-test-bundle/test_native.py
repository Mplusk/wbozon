"""Local synthetic Gunicorn test. No email or marketplace network requests."""
import hashlib,json,os,pathlib,secrets,signal,socket,sqlite3,subprocess,sys,tempfile,time,urllib.request,urllib.error,urllib.parse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

base=pathlib.Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as tmp:
    root=pathlib.Path(tmp);token=secrets.token_urlsafe(32);key=secrets.token_bytes(32)
    env={k:v for k,v in os.environ.items() if k not in ('SESSION_SECRET','SMTP_PASSWORD','SMTP_USER')}
    env.update(DATABASE_PATH=str(root/'auth.sqlite'),VAULT_PATH=str(root/'vault.sqlite'),BASE_URL='https://3dprinta.shop')
    values={'SESSION_SECRET_FILE':secrets.token_hex(32).encode(),'VAULT_KEY_FILE':key,'READ_TOKEN_HASH_FILE':hashlib.sha256(token.encode()).hexdigest().encode(),'SMTP_PASSWORD_FILE':b'synthetic'}
    for name,value in values.items():
        path=root/name;path.write_bytes(value);path.chmod(0o600);env[name]=str(path)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    process=subprocess.Popen([str(pathlib.Path(sys.executable).parent/'gunicorn'),'--config','gunicorn.conf.py','--bind',f'127.0.0.1:{port}','portal:create_portal()'],cwd=base/'service',env=env,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def request(path,method='GET',data=None,headers=None,timeout=10):
        h={'Host':'3dprinta.shop',**(headers or {})}
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data,headers=h,method=method)
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    try:
        ready_by=time.monotonic()+60
        while (remaining:=ready_by-time.monotonic())>0:
            if process.poll() is not None:raise RuntimeError('runtime exited')
            try:
                if request('/',timeout=min(3,remaining))[0]==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise RuntimeError('runtime readiness timeout')
        assert request('/settings')[0]==401
        assert request('/api/read','POST',b'{}',{'Content-Type':'application/json','oai-authenticated-user-id':'owner'})[0]==401
        # Synthetic session fixture, never an authentication bypass in the app.
        sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
        with sqlite3.connect(root/'auth.sqlite') as c:c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(sid.encode()).hexdigest(),time.time()+43200,csrf))
        headers={'Cookie':'__Host-sid='+sid,'Origin':'https://3dprinta.shop','Content-Type':'application/x-www-form-urlencoded'}
        assert request('/settings',headers=headers)[0]==200
        form=urllib.parse.urlencode({'provider':'wb','csrf':csrf,'api_token':'synthetic-test-token'}).encode()
        assert request('/settings','POST',form,{**headers,'Origin':'https://evil.example'})[0]==403
        assert request('/settings','POST',form,headers)[0]==200
        assert b'synthetic-test-token' not in request('/settings',headers=headers)[1]
        assert b'synthetic-test-token' not in (root/'vault.sqlite').read_bytes()
        # Ozon has no saved key, so authenticated API must stop before network.
        payload=json.dumps({'operation':'ozon_products','arguments':{}}).encode()
        assert request('/api/read','POST',payload,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})[0]==409
    finally:
        process.terminate()
        try:
            # Gunicorn gives its workers up to 30 seconds to shut down. Allow
            # time to reap both workers under the production sandbox CPU share.
            status = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            # Only this synthetic server and its children belong to this group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            raise RuntimeError('native smoke Gunicorn did not stop within 60 seconds') from None
        if status != 0:
            raise RuntimeError('native smoke Gunicorn exited with status ' + str(status))
    print('PASS: real native Gunicorn + file secrets + sessions + CSRF + encrypted storage + independent API auth + graceful shutdown')
