"""Local synthetic Gunicorn test. No email or marketplace network requests."""
import hashlib,json,os,pathlib,secrets,socket,sqlite3,subprocess,sys,tempfile,time,urllib.request,urllib.error,urllib.parse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

base=pathlib.Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as tmp:
    root=pathlib.Path(tmp);token=secrets.token_urlsafe(32);key=secrets.token_bytes(32)
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    env.update(DATABASE_PATH=str(root/'auth.sqlite'),VAULT_PATH=str(root/'vault.sqlite'),BASE_URL='https://3dprinta.shop')
    values={'SESSION_SECRET_FILE':secrets.token_hex(32).encode(),'VAULT_KEY_FILE':key,'READ_TOKEN_HASH_FILE':hashlib.sha256(token.encode()).hexdigest().encode(),'SMTP_PASSWORD_FILE':b'synthetic'}
    for name,value in values.items():
        path=root/name;path.write_bytes(value);path.chmod(0o600);env[name]=str(path)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    process=subprocess.Popen([str(pathlib.Path(sys.executable).parent/'gunicorn'),'--config','gunicorn.conf.py','--bind',f'127.0.0.1:{port}','portal:create_portal()'],cwd=base/'service',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def request(path,method='GET',data=None,headers=None):
        h={'Host':'3dprinta.shop',**(headers or {})}
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data,headers=h,method=method)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    try:
        for _ in range(50):
            if process.poll() is not None:raise RuntimeError('runtime exited')
            try:
                if request('/')[0]==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise RuntimeError('runtime readiness timeout')
        assert request('/healthz')[0]==200
        assert request('/healthz','HEAD')[0]==200 and request('/healthz','HEAD')[1]==b''
        assert request('/healthz','POST',b'{}',{'Content-Type':'application/json'})[0]==405
        invalid_status,invalid_page=request('/auth/link?token=bad')
        assert invalid_status==400 and 'Запросить новую ссылку'.encode() in invalid_page
        missing_status,missing_page=request('/auth/confirm','POST',b'token=bad&csrf=bad',{'Origin':'https://3dprinta.shop','Content-Type':'application/x-www-form-urlencoded'})
        assert missing_status==403 and 'Не удалось подтвердить вход'.encode() in missing_page
        assert request('/settings')[0]==401
        assert request('/api/read','POST',b'{}',{'Content-Type':'application/json','oai-authenticated-user-id':'owner'})[0]==401
        # Synthetic session fixture, never an authentication bypass in the app.
        sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
        with sqlite3.connect(root/'auth.sqlite') as c:c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(sid.encode()).hexdigest(),time.time()+43200,csrf))
        headers={'Cookie':'__Host-sid='+sid,'Origin':'https://3dprinta.shop','Content-Type':'application/x-www-form-urlencoded'}
        settings=request('/settings',headers=headers)
        assert settings[0]==200
        assert 'Статус подключения MCP здесь не проверяется'.encode() in settings[1]
        assert 'Идентификатор продавца (Client ID)'.encode() in settings[1]
        assert 'Соединение с маркетплейсом: не проверено'.encode() in settings[1]
        form=urllib.parse.urlencode({'provider':'wb','csrf':csrf,'api_token':'synthetic-test-token'}).encode()
        assert request('/settings','POST',form,{**headers,'Origin':'https://evil.example'})[0]==403
        assert request('/settings','POST',form,headers)[0]==200
        assert 'осталось до 10 из 10'.encode() in request('/settings',headers=headers)[1]
        with sqlite3.connect(root/'auth.sqlite') as c: assert c.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
        assert b'synthetic-test-token' not in request('/settings',headers=headers)[1]
        assert b'synthetic-test-token' not in (root/'vault.sqlite').read_bytes()
        check_form=urllib.parse.urlencode({'provider':'ozon','csrf':csrf}).encode()
        assert request('/settings/check','GET',headers=headers)[0]==405
        assert request('/settings/check','POST',check_form,{'Content-Type':'application/x-www-form-urlencoded'})[0]==401
        assert request('/settings/check','POST',check_form,{**headers,'Origin':'https://evil.example'})[0]==403
        assert request('/settings/check','POST',check_form,headers)[0]==409
        assert request('/settings/check','POST',check_form,headers)[0]==429
        assert 'осталось до 9 из 10'.encode() in request('/settings',headers=headers)[1]
        assert request('/settings/preview','GET',headers=headers)[0]==405
        assert request('/settings/preview','POST',check_form,{'Content-Type':'application/x-www-form-urlencoded'})[0]==401
        assert request('/settings/preview','POST',check_form,{**headers,'Origin':'https://evil.example'})[0]==403
        assert request('/settings/preview','POST',check_form,headers)[0]==429
        # Ozon has no saved key, so authenticated API must stop before network.
        payload=json.dumps({'operation':'ozon_products','arguments':{}}).encode()
        assert request('/api/read','POST',payload,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})[0]==409
        lookup_form=urllib.parse.urlencode({'nm_id':'123','csrf':csrf}).encode()
        assert request('/settings/wb-lookup','GET',headers=headers)[0]==405
        assert request('/settings/wb-lookup','POST',lookup_form,{'Content-Type':'application/x-www-form-urlencoded'})[0]==401
        assert request('/settings/wb-lookup','POST',lookup_form,{**headers,'Origin':'null'})[0]==403
        bad_lookup=urllib.parse.urlencode({'nm_id':'1&url=evil','csrf':csrf}).encode()
        assert request('/settings/wb-lookup','POST',bad_lookup,headers)[0]==400
        # Remove synthetic WB fixture so the positive lookup stops before network.
        with sqlite3.connect(root/'vault.sqlite') as c:c.execute('DELETE FROM vault WHERE provider=?',('wb',))
        assert request('/settings/wb-lookup','POST',lookup_form,headers)[0]==409
        assert request('/settings/wb-lookup','POST',lookup_form,headers)[0]==429
        print('PASS: WB lookup guards plus existing real Gunicorn synthetic smoke; no live API requests')
    finally:
        process.terminate();process.wait(timeout=10)
