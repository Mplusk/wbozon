"""Security and compatibility tests for OAuth MCP; synthetic credentials only."""
import base64, hashlib, io, json, secrets, sqlite3, tempfile, threading, unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit, parse_qs
from external_keys import KeyStore
import oauth_mcp
from oauth_mcp import OAuthStore,OAuthError,handle,SCOPE
from ui import CSP

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=self.tmp.name+'/state.db'; self.now=10000
        self.keys=KeyStore(self.path); self.store=OAuthStore(self.path,lambda:self.now,signing_secret='x'*32)
        self.key_id,self.secret=self.keys.create('Client',secrets.token_urlsafe(16),self.now)
        self.origin='https://3dprinta.shop';self.resource=self.origin+'/mcp'
        self.redirect='https://claude.ai/api/mcp/auth_callback'
        self.client=self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token']})['client_id']
        self.verifier=secrets.token_urlsafe(32)
        self.challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
    def tearDown(self):
        import gc;gc.collect();self.tmp.cleanup()
    def request(self,path,method='GET',query='',body=b'',content='',cookie='',origin=None,auth=''):
        e={'PATH_INFO':path,'REQUEST_METHOD':method,'QUERY_STRING':query,'CONTENT_TYPE':content,
           'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),'HTTP_COOKIE':cookie,'HTTP_AUTHORIZATION':auth}
        if origin is not None:e['HTTP_ORIGIN']=origin
        r={}
        def start(status,headers):r.update(status=status,headers=dict(headers))
        r['body']=b''.join(handle(e,start,self.store,self.keys,self.origin)).decode();return r
    def auth_params(self,**kw):
        return dict(response_type='code',client_id=self.client,redirect_uri=self.redirect,scope=SCOPE,
                    state='state123',code_challenge=self.challenge,code_challenge_method='S256',resource=self.resource,**kw)
    def approve(self):
        r=self.request('/oauth/authorize',query=urlencode(self.auth_params()))
        self.assertEqual(r['status'],'200 OK');self.assertEqual(r['headers']['Cache-Control'],'no-store')
        self.assertEqual(r['headers']['Referrer-Policy'],'same-origin')
        cookie=r['headers']['Set-Cookie'].split(';')[0];nonce=cookie.split('=',1)[1]
        import re
        ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
        result=self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)
        self.assertEqual(result['status'],'303 See Other')
        self.assertEqual(result['headers']['Referrer-Policy'],'no-referrer')
        parts=urlsplit(result['headers']['Location']);q=parse_qs(parts.query)
        self.assertEqual(parts.scheme+'://'+parts.netloc+parts.path,self.redirect)
        self.assertEqual(q['iss'],[self.origin]);self.assertEqual(q['state'],['state123'])
        return q['code'][0],nonce
    def token_request(self,code,verifier=None,resource=None):
        return {'grant_type':'authorization_code','client_id':self.client,'code':code,'redirect_uri':self.redirect,
                'code_verifier':verifier or self.verifier,'resource':resource or self.resource}
    def test_metadata_and_allowlisted_cimd(self):
        p=json.loads(self.request('/.well-known/oauth-protected-resource/mcp')['body'])
        self.assertEqual(p['resource'],self.resource);self.assertEqual(p['authorization_servers'],[self.origin])
        p=json.loads(self.request('/.well-known/oauth-authorization-server')['body'])
        self.assertEqual(p['code_challenge_methods_supported'],['S256']);self.assertTrue(p['authorization_response_iss_parameter_supported'])
        for client,redirect in [('https://chatgpt.com/oauth/client.json','https://chatgpt.com/connector_platform_oauth_redirect'),('https://grok.com/oauth/mcp-client.json','https://grok.com/connectors-oauth-exchange-code/'),('https://claude.ai/oauth/mcp-oauth-client-metadata',self.redirect)]:
            d=self.auth_params();d.update(client_id=client,redirect_uri=redirect);ticket,nonce,_,_=self.store.begin(d,self.resource)
            self.assertTrue(ticket);self.assertTrue(nonce)
    def test_code_pkce_is_single_use_and_parent_revocation_invalidates_every_token(self):
        code,_=self.approve();args=self.token_request(code)
        with self.assertRaises(OAuthError):self.store.exchange({**args,'code_verifier':'A'*43},self.resource)
        with self.assertRaises(OAuthError):self.store.exchange({**args,'resource':self.origin+'/wrong'},self.resource)
        tokens=self.store.exchange(args,self.resource)
        self.assertEqual(self.store.verify(tokens['access_token'],self.resource),self.key_id)
        with self.assertRaises(OAuthError):self.store.exchange(args,self.resource)
        with self.assertRaises(OAuthError):self.store.exchange({'grant_type':'refresh_token','client_id':self.client,'refresh_token':tokens['refresh_token'],'scope':'mcp:write'},self.resource)
        refreshed=self.store.exchange({'grant_type':'refresh_token','client_id':self.client,'refresh_token':tokens['refresh_token']},self.resource)
        self.assertIsNone(self.store.verify(tokens['access_token'],self.resource))
        with self.assertRaises(OAuthError):self.store.exchange({'grant_type':'refresh_token','client_id':self.client,'refresh_token':tokens['refresh_token']},self.resource)
        self.assertEqual(self.store.verify(refreshed['access_token'],self.resource),self.key_id)
        self.keys.revoke(self.key_id,self.now)
        self.assertIsNone(self.store.verify(refreshed['access_token'],self.resource))
        with self.assertRaises(OAuthError):self.store.exchange({'grant_type':'refresh_token','client_id':self.client,'refresh_token':refreshed['refresh_token']},self.resource)
    def test_pending_csrf_and_replay(self):
        r=self.request('/oauth/authorize',query=urlencode(self.auth_params()));cookie=r['headers']['Set-Cookie'].split(';')[0];nonce=cookie.split('=',1)[1]
        import re
        ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
        for origin,ck in [(None,cookie),('null',cookie),('https://evil.example',cookie),(self.origin,''),(self.origin,'__Host-mcp-oauth=bad')]:
            self.assertEqual(self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=ck,origin=origin)['status'],'400 Bad Request')
        self.assertEqual(self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)['status'],'303 See Other')
        self.assertEqual(self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)['status'],'400 Bad Request')
    def test_consent_csp_allows_only_registered_provider_origins_and_keeps_ticket(self):
        import re
        r=self.request('/oauth/authorize',query=urlencode(self.auth_params()))
        policy=r['headers']['Content-Security-Policy']
        directive=next(x.strip() for x in policy.split(';') if x.strip().startswith('form-action '))
        self.assertEqual(set(directive.split()[1:]),{"'self'",'https://chatgpt.com','https://claude.ai','https://grok.com','https://console.x.ai'})
        self.assertNotIn('https://evil.example',policy)
        ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
        cookie=r['headers']['Set-Cookie'].split(';')[0];nonce=cookie.split('=',1)[1]
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':'wrong'}).encode()
        retry=self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)
        self.assertEqual(retry['status'],'403 Forbidden')
        self.assertEqual(retry['headers']['Content-Security-Policy'],policy)
        self.assertEqual(retry['headers']['Referrer-Policy'],'same-origin')
        self.assertIn('name="ticket" value="'+ticket+'"',retry['body'])
        self.assertEqual(self.request('/.well-known/oauth-authorization-server')['headers']['Content-Security-Policy'],CSP)
        self.assertEqual(self.request('/.well-known/oauth-authorization-server')['headers']['Referrer-Policy'],'no-referrer')
    def test_stateless_pending_resists_flood_tamper_and_concurrent_replay(self):
        for _ in range(1100): self.store.begin(self.auth_params(),self.resource)
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('select count(*) from oauth_pending_used').fetchone()[0],0)
        ticket,nonce,_,_=self.store.begin(self.auth_params(),self.resource)
        damaged=('A' if ticket[0]!='A' else 'B')+ticket[1:]
        with self.assertRaises(OAuthError):self.store.approve(damaged,nonce,self.key_id)
        with self.assertRaises(OAuthError):self.store.approve(ticket,'A'*43,self.key_id)
        def approve(_):
            try:self.store.approve(ticket,nonce,self.key_id);return True
            except (OAuthError,sqlite3.OperationalError):return False
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(approve,range(4)))
        self.assertEqual(sum(results),1)
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('select count(*) from oauth_pending_used').fetchone()[0],1)
        expired,csrf,_,_=self.store.begin(self.auth_params(),self.resource);self.now+=901
        with self.assertRaises(OAuthError):self.store.approve(expired,csrf,self.key_id)
    def test_parallel_consent_cookies_and_legacy_cookie_are_independent(self):
        import re
        def form():
            r=self.request('/oauth/authorize',query=urlencode(self.auth_params()))
            self.assertIn('Max-Age=900',r['headers']['Set-Cookie'])
            self.assertIn('Secure; HttpOnly; SameSite=Lax',r['headers']['Set-Cookie'])
            cookie=r['headers']['Set-Cookie'].split(';')[0]
            nonce=cookie.split('=',1)[1]
            ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
            return cookie,nonce,ticket
        c1,n1,t1=form();c2,n2,t2=form()
        self.assertNotEqual(c1.split('=',1)[0],c2.split('=',1)[0])
        cookies=c1+'; '+c2
        for cookie,nonce,ticket in [(c1,n1,t1),(c2,n2,t2)]:
            body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
            result=self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookies,origin=self.origin)
            self.assertEqual(result['status'],'303 See Other')
            self.assertTrue(result['headers']['Set-Cookie'].startswith(cookie.split('=',1)[0]+'=;'))
        _,legacy_nonce,legacy_ticket=form()
        legacy='__Host-mcp-oauth='+legacy_nonce
        body=urlencode({'csrf':legacy_nonce,'ticket':legacy_ticket,'key':self.secret}).encode()
        result=self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=legacy,origin=self.origin)
        self.assertEqual(result['status'],'303 See Other')
        self.assertTrue(result['headers']['Set-Cookie'].startswith('__Host-mcp-oauth=;'))
    def test_pending_cookies_are_bounded_across_repeated_authorization_attempts(self):
        from http.cookies import SimpleCookie
        jar={'other_session':'keep-me','__Host-mcp-oauth':'legacy-value',
             '__Host-mcp-oauth-invalid':'unrelated-value'}
        query=urlencode(self.auth_params())
        for _ in range(40):
            raw='; '.join(name+'='+value for name,value in jar.items())
            e={'PATH_INFO':'/oauth/authorize','REQUEST_METHOD':'GET','QUERY_STRING':query,
               'HTTP_COOKIE':raw,'wsgi.input':io.BytesIO(b''),'CONTENT_LENGTH':'0'}
            r={}
            def start(status,headers):r.update(status=status,headers=headers)
            b''.join(handle(e,start,self.store,self.keys,self.origin))
            self.assertEqual(r['status'],'200 OK')
            for name,value in r['headers']:
                if name!='Set-Cookie':continue
                parsed=SimpleCookie();parsed.load(value)
                self.assertEqual(len(parsed),1)
                morsel=next(iter(parsed.values()))
                if morsel['max-age']=='0': jar.pop(morsel.key,None)
                else: jar[morsel.key]=morsel.value
            self.assertEqual(jar['other_session'],'keep-me')
            self.assertEqual(jar['__Host-mcp-oauth'],'legacy-value')
            self.assertEqual(jar['__Host-mcp-oauth-invalid'],'unrelated-value')
            pending=[name for name in jar if name.startswith('__Host-mcp-oauth-') and len(name)==len('__Host-mcp-oauth-')+43]
            self.assertLessEqual(len(pending),8)
            self.assertLess(len('; '.join(name+'='+value for name,value in jar.items())),4096)
    def test_fifteen_minute_ttl_and_recovery_page_do_not_expose_input(self):
        import re
        def form():
            r=self.request('/oauth/authorize',query=urlencode(self.auth_params()))
            cookie=r['headers']['Set-Cookie'].split(';')[0];nonce=cookie.split('=',1)[1]
            ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
            return cookie,nonce,ticket
        cookie,nonce,ticket=form();self.now+=301
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
        self.assertEqual(self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)['status'],'303 See Other')
        cookie,nonce,ticket=form();self.now+=901
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
        r=self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)
        self.assertEqual(r['status'],'400 Bad Request');self.assertIn('Подключение прервано',r['body'])
        self.assertIn('начните подключение заново',r['body']);self.assertNotIn(self.secret,r['body'])
        self.assertNotIn(ticket,r['body']);self.assertNotIn(nonce,r['body'])
        self.assertEqual(r['headers']['Cache-Control'],'no-store');self.assertIn('text/html',r['headers']['Content-Type'])
    def test_diagnostics_are_fixed_categories_without_request_values(self):
        import re
        r=self.request('/oauth/authorize',query=urlencode(self.auth_params()))
        cookie=r['headers']['Set-Cookie'].split(';')[0];nonce=cookie.split('=',1)[1]
        ticket=re.search(r'name="ticket" value="([A-Za-z0-9_.-]+)"',r['body']).group(1)
        body=urlencode({'csrf':nonce,'ticket':ticket,'key':self.secret}).encode()
        oauth_mcp._REJECTION_TIMES.clear()
        with patch.object(oauth_mcp._LOG,'warning') as warning:
            self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin='https://attacker.invalid')
            self.request('/oauth/authorize','POST',body=b'bad',content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)
            self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie='',origin=self.origin)
            self.now+=901
            self.request('/oauth/authorize','POST',body=body,content='application/x-www-form-urlencoded',cookie=cookie,origin=self.origin)
            categories=[call.args[1] for call in warning.call_args_list]
            self.assertEqual(categories,['origin','form','csrf','ticket'])
            self.assertNotIn(self.secret,str(warning.call_args_list));self.assertNotIn(ticket,str(warning.call_args_list));self.assertNotIn(nonce,str(warning.call_args_list))
    def test_rejects_redirect_and_dcr_abuse(self):
        for url in ['https://evil.example/callback','https://claude.ai.evil.example/api/mcp/auth_callback','https://claude.ai/api/mcp/auth_callback?evil=1']:
            with self.assertRaises(OAuthError):self.store.register({'redirect_uris':[url]})
        d=self.auth_params();d['redirect_uri']='https://evil.example/callback'
        with self.assertRaises(OAuthError):self.store.begin(d,self.resource)
    def test_confidential_registration_is_rejected_and_metadata_only_advertises_public_clients(self):
        metadata=json.loads(self.request('/.well-known/oauth-authorization-server')['body'])
        self.assertEqual(metadata['token_endpoint_auth_methods_supported'],['none'])
        self.assertEqual(self.store.register({'redirect_uris':[self.redirect]})['token_endpoint_auth_method'],'none')
        for method in ('client_secret_post','client_secret_basic'):
            with self.assertRaises(OAuthError):self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':method})
    def test_concurrent_code_exchange_is_single_use(self):
        code,_=self.approve(); args=self.token_request(code)
        def exchange(_):
            try:self.store.exchange(args,self.resource);return True
            except (OAuthError,sqlite3.OperationalError):return False
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(exchange,range(4)))
        self.assertEqual(sum(results),1)
    def test_expiration(self):
        code,_=self.approve();self.now+=61
        with self.assertRaises(OAuthError):self.store.exchange(self.token_request(code),self.resource)
    def test_http_registration_and_token_exchange_are_oauth_responses(self):
        registration={'redirect_uris':[self.redirect], 'token_endpoint_auth_method':'none',
                      'grant_types':['authorization_code','refresh_token']}
        r=self.request('/oauth/register','POST',body=json.dumps(registration).encode(),content='application/json')
        self.assertEqual(r['status'],'201 Created'); self.assertEqual(r['headers']['Cache-Control'],'no-store')
        self.client=json.loads(r['body'])['client_id'];code,_=self.approve()
        body=urlencode(self.token_request(code)).encode()
        r=self.request('/oauth/token','POST',body=body,content='application/x-www-form-urlencoded')
        self.assertEqual(r['status'],'200 OK');self.assertEqual(r['headers']['Cache-Control'],'no-store')
        self.assertEqual(self.store.verify(json.loads(r['body'])['access_token'],self.resource),self.key_id)
        replay=self.request('/oauth/token','POST',body=body,content='application/x-www-form-urlencoded')
        self.assertEqual(replay['status'],'400 Bad Request');self.assertEqual(json.loads(replay['body'])['error'],'invalid_grant')
    def test_dcr_without_refresh_does_not_issue_or_accept_refresh(self):
        self.client=self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':'none'})['client_id']
        code,_=self.approve();tokens=self.store.exchange(self.token_request(code),self.resource)
        self.assertNotIn('refresh_token',tokens)
        with self.assertRaises(OAuthError):self.store.exchange({'grant_type':'refresh_token','client_id':self.client,'refresh_token':'mcp_'+'A'*43},self.resource)
    def test_public_dcr_is_idempotent_and_persistent(self):
        ids={self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':'none'})['client_id'] for _ in range(150)}
        self.assertEqual(len(ids),1)
        with sqlite3.connect(self.path) as c:self.assertLessEqual(c.execute('select count(*) from oauth_clients').fetchone()[0],2)
        self.now+=60*86400
        self.assertEqual(self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':'none'})['client_id'],next(iter(ids)))
        with sqlite3.connect(self.path) as c:self.assertEqual(c.execute('select count(*) from oauth_clients').fetchone()[0],2)
    def test_invalid_grant_flood_and_repeat_dcr_never_take_write_lock(self):
        statements=[]
        original=self.store.connect
        def traced():
            connection=original();connection.set_trace_callback(statements.append);return connection
        self.store.connect=traced
        for _ in range(1000):
            with self.assertRaises(OAuthError):
                self.store.exchange(self.token_request(secrets.token_urlsafe(32)),self.resource)
        for _ in range(200):
            self.store.register({'redirect_uris':[self.redirect],'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token']})
        self.assertFalse(any(s.upper().startswith(('BEGIN','INSERT','UPDATE','DELETE')) for s in statements))
    def test_offline_scope_is_bounded_and_authorizes(self):
        params=self.auth_params();params['scope']='offline_access mcp:read'
        ticket,nonce,_,_=self.store.begin(params,self.resource); code,_,_=self.store.approve(ticket,nonce,self.key_id)
        tokens=self.store.exchange(self.token_request(code),self.resource)
        self.assertEqual(tokens['scope'],'mcp:read offline_access')
        self.assertEqual(self.store.verify(tokens['access_token'],self.resource),self.key_id)
        for bad in ('openid mcp:read','offline_access','mcp:read mcp:read'):
            params['scope']=bad
            with self.assertRaises(OAuthError):self.store.begin(params,self.resource)
        self.assertEqual(self.request('/.well-known/openid-configuration')['status'],'405 Method Not Allowed')

if __name__=='__main__':unittest.main()
