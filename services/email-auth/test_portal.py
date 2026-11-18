import hashlib, io, json, secrets, sqlite3, tempfile, unittest
from urllib.parse import urlencode
from app import LoginApp
from portal import Portal, Vault

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.now=100000
        self.login=LoginApp(self.tmp.name+'/login.db','x'*32,'https://3dprinta.shop',clock=lambda:self.now)
        self.vault=Vault(self.tmp.name+'/vault.db',secrets.token_bytes(32))
        self.token=secrets.token_urlsafe(32);self.calls=[]
        def gateway(op,args,creds): self.calls.append((op,args,creds));return {'items':[]}
        self.app=Portal(self.login,self.vault,hashlib.sha256(self.token.encode()).hexdigest(),gateway)
        self.sid=secrets.token_urlsafe(32);self.csrf=secrets.token_urlsafe(24)
        with self.login.connect() as c:c.execute('INSERT INTO sessions VALUES (?,?,?)',(self.login.digest(self.sid),self.now+43200,self.csrf))
    def tearDown(self): self.tmp.cleanup()
    def request(self,path='/settings',method='GET',data=None,cookie=True,origin=True,auth=None,headers=None):
        kind='application/json' if path=='/api/read' else 'application/x-www-form-urlencoded'
        raw=(json.dumps(data) if kind=='application/json' else urlencode(data or {})).encode()
        e={'PATH_INFO':path,'REQUEST_METHOD':method,'HTTP_HOST':'3dprinta.shop','CONTENT_TYPE':kind,'CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        if cookie:e['HTTP_COOKIE']='__Host-sid='+self.sid
        if origin:e['HTTP_ORIGIN']='https://3dprinta.shop'
        if auth:e['HTTP_AUTHORIZATION']='Bearer '+auth
        e.update(headers or {});result={}
        def start(status,hs):result.update(status=status,headers=dict(hs))
        result['body']=b''.join(self.app(e,start)).decode();return result
    def save(self,**kw):return self.request(method='POST',data={'provider':'wb','csrf':self.csrf,'api_token':'test-secret-value'},**kw)
    def test_anonymous_settings(self): self.assertTrue(self.request(cookie=False)['status'].startswith('401'))
    def test_save_encrypted_and_never_echo(self):
        self.assertTrue(self.save()['status'].startswith('200'))
        self.assertEqual(self.vault.read('wb')['api_token'],'test-secret-value')
        with open(self.vault.path,'rb') as f:self.assertNotIn(b'test-secret-value',f.read())
        self.assertNotIn('test-secret-value',self.request()['body'])
    def test_csrf_and_origin(self):
        self.assertTrue(self.save(origin=False)['status'].startswith('403'))
        self.csrf='é'*32;self.assertTrue(self.save()['status'].startswith('403'))
        self.assertFalse(self.vault.present())
    def test_reauth_required(self):
        self.now+=301;self.assertTrue(self.save()['status'].startswith('403'))
    def test_stale_session_has_working_logout_path(self):
        self.now+=301
        page=self.request()['body'];self.assertIn('action="/logout"',page)
        result=self.request('/logout','POST',{'csrf':self.csrf})
        self.assertTrue(result['status'].startswith('200'));self.assertIn('Войти снова',result['body'])
        self.assertTrue(self.request()['status'].startswith('401'))
    def test_expiry(self):
        self.now+=43201;self.assertTrue(self.request()['status'].startswith('401'))
    def test_login_home_useful(self):self.assertEqual(self.request(path='/')['headers']['Location'],'/settings')
    def test_sites_header_not_authentication(self):
        r=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},headers={'HTTP_OAI_AUTHENTICATED_USER_ID':'owner'})
        self.assertTrue(r['status'].startswith('401'));self.assertFalse(self.calls)
    def test_api_success(self):
        self.save();r=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{'limit':1}},auth=self.token,cookie=False,origin=False)
        self.assertTrue(r['status'].startswith('200'));self.assertEqual(len(self.calls),1)
    def test_no_dynamic_url_or_write(self):
        for operation,args in [('wb_prices',{'url':'http://127.0.0.1'}),('wb_delete',{}),('wb_prices',{'limit':True})]:
            r=self.request('/api/read','POST',{'operation':operation,'arguments':args},auth=self.token)
            self.assertTrue(r['status'].startswith('400'))
        self.assertFalse(self.calls)
    def test_unknown_config_not_calls(self):
        r=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=self.token)
        self.assertTrue(r['status'].startswith('409'));self.assertFalse(self.calls)
    def test_tampered_ciphertext_closed(self):
        self.save()
        with sqlite3.connect(self.vault.path) as c:c.execute("UPDATE vault SET blob=?",(b'bad'*32,))
        r=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=self.token)
        self.assertTrue(r['status'].startswith('502'));self.assertFalse(self.calls)
    def test_api_rate_limit(self):
        data={'operation':'wb_prices','arguments':{}}
        for _ in range(30):self.request('/api/read','POST',data,auth=self.token)
        self.assertTrue(self.request('/api/read','POST',data,auth=self.token)['status'].startswith('429'))
    def test_wrong_key_no_decryption(self):
        self.save();other=Vault(self.vault.path,secrets.token_bytes(32))
        with self.assertRaises(Exception):other.read('wb')
    def test_body_limit(self):
        self.assertTrue(self.request(method='POST',data={'x':'x'*4096})['status'].startswith('400'))
    def test_header_injection_rejected(self):
        r=self.request(method='POST',data={'provider':'wb','csrf':self.csrf,'api_token':'abc\r\nInjected: x'})
        self.assertTrue(r['status'].startswith('400'));self.assertFalse(self.vault.present())
    def test_api_error_does_not_leak(self):
        self.save()
        def bad(*a):raise RuntimeError('test-secret-value')
        self.app.gateway=bad;r=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=self.token)
        self.assertNotIn('test-secret-value',r['body'])

if __name__=='__main__':unittest.main()

class FactoryTests(unittest.TestCase):
    def test_file_secrets_factory_and_missing_files_fail_closed(self):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from portal import create_portal
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); env={'DATABASE_PATH':str(base/'auth.sqlite'),'VAULT_PATH':str(base/'vault.sqlite')}
            values={'SESSION_SECRET_FILE':secrets.token_hex(32).encode(),
                    'VAULT_KEY_FILE':secrets.token_bytes(32),
                    'READ_TOKEN_HASH_FILE':hashlib.sha256(secrets.token_bytes(32)).hexdigest().encode(),
                    'SMTP_PASSWORD_FILE':b'synthetic'}
            for key,value in values.items():
                path=base/key;path.write_bytes(value);env[key]=str(path)
            with patch.dict(os.environ,env,clear=True):
                instance=create_portal();self.assertIsInstance(instance,Portal)
                mcp_path=base/'mcp-hash'
                mcp_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                mcp_path.write_text(mcp_hash,encoding='ascii')
                with patch.dict(os.environ,{'MCP_READ_TOKEN_HASH_FILE':str(mcp_path)}):
                    self.assertEqual(create_portal().mcp_token_hash,mcp_hash)
                    mcp_path.write_text(values['READ_TOKEN_HASH_FILE'].decode(),encoding='ascii')
                    with self.assertRaises(ValueError):create_portal()
                (base/'VAULT_KEY_FILE').unlink()
                with self.assertRaises(FileNotFoundError):create_portal()
            with patch.dict(os.environ,{'SESSION_SECRET':'not-used'},clear=True):
                with self.assertRaises(KeyError):create_portal()
