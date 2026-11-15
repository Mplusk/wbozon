"""Synthetic end-to-end OAuth and MCP requests through the portal WSGI entrypoint."""
import base64
import gc
import hashlib
import io
import json
import re
import secrets
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import oauth_mcp
import test_portal


class OAuthPortalIntegrationTests(unittest.TestCase):
    setUp = test_portal.Tests.setUp
    request = test_portal.Tests.request

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def call(self, path, method='GET', fields=None, *, json_body=False, token=None,
             cookie=None, origin=None, query=None):
        raw = b'' if fields is None else (
            json.dumps(fields).encode() if json_body else urlencode(fields).encode())
        env = {'PATH_INFO':path, 'REQUEST_METHOD':method, 'HTTP_HOST':'3dprinta.shop',
               'QUERY_STRING':urlencode(query or {}),
               'CONTENT_TYPE':'application/json' if json_body else 'application/x-www-form-urlencoded',
               'CONTENT_LENGTH':str(len(raw)), 'wsgi.input':io.BytesIO(raw)}
        if token: env['HTTP_AUTHORIZATION']='Bearer '+token
        if cookie: env['HTTP_COOKIE']=cookie
        if origin: env['HTTP_ORIGIN']=origin
        result={}
        def start(status, headers): result.update(status=status, headers=dict(headers))
        result['body']=b''.join(self.app(env,start)).decode()
        return result

    def create_owner_key(self):
        page=self.request()['body']
        nonce=re.search(r'name="request_id" value="([A-Za-z0-9_-]{22})"',page).group(1)
        result=self.request('/settings/mcp/create','POST',
                            {'csrf':self.csrf,'name':'Synthetic OAuth client','request_id':nonce})
        self.assertTrue(result['status'].startswith('200'))
        self.assertEqual(result['headers'].get('Cache-Control'),'no-store')
        return re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',result['body']).group(1)

    def mcp(self,token):
        return self.call('/mcp','POST',{'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'wb_prices','arguments':{'limit':1}}},json_body=True,token=token)

    def test_expired_authorization_ticket_is_rejected(self):
        raw_key=self.create_owner_key()
        client_id=next(iter(oauth_mcp.CIMD));redirect=oauth_mcp.CIMD[client_id][0][0]
        verifier=secrets.token_urlsafe(32)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        start=self.call('/oauth/authorize',query={'response_type':'code','client_id':client_id,
            'redirect_uri':redirect,'code_challenge_method':'S256','code_challenge':challenge,
            'scope':'mcp:read','resource':'https://3dprinta.shop/mcp','state':'expired_state'})
        self.assertTrue(start['status'].startswith('200'))
        nonce=re.search(r'name="csrf" value="([A-Za-z0-9_-]{43})"',start['body']).group(1)
        ticket=re.search(r'name="ticket" value="([A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43})"',start['body']).group(1)
        cookie=start['headers']['Set-Cookie'].split(';',1)[0]
        self.now+=oauth_mcp.PENDING_TTL+1
        result=self.call('/oauth/authorize','POST',{'csrf':nonce,'ticket':ticket,'key':raw_key},
                         cookie=cookie,origin='https://3dprinta.shop')
        self.assertTrue(result['status'].startswith('400'))
        self.assertNotIn('Location',result['headers'])
        self.assertNotIn(raw_key,result['body'])

    def test_fixed_metadata_client_identifiers_through_http(self):
        raw_key=self.create_owner_key()
        self.vault.save('ozon',{'client_id':'123','api_key':'synthetic-ozon-secret'})
        resource='https://3dprinta.shop/mcp'
        self.assertEqual({client[1] for client in oauth_mcp.CIMD.values()},{'ChatGPT','Claude','Grok'})
        for client_id,(redirects,display_name) in oauth_mcp.CIMD.items():
            with self.subTest(client=display_name):
                redirect=redirects[0]; verifier=secrets.token_urlsafe(32)
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                start=self.call('/oauth/authorize',query={'response_type':'code','client_id':client_id,
                    'redirect_uri':redirect,'code_challenge_method':'S256','code_challenge':challenge,
                    'scope':'mcp:read','resource':resource,'state':'synthetic_state'})
                self.assertTrue(start['status'].startswith('200'),start['body'])
                nonce=re.search(r'name="csrf" value="([A-Za-z0-9_-]{43})"',start['body']).group(1)
                ticket=re.search(r'name="ticket" value="([A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43})"',start['body']).group(1)
                cookie=start['headers']['Set-Cookie'].split(';',1)[0]
                approved=self.call('/oauth/authorize','POST',{'csrf':nonce,'ticket':ticket,'key':raw_key},
                    cookie=cookie,origin='https://3dprinta.shop')
                self.assertTrue(approved['status'].startswith('303'))
                code=parse_qs(urlsplit(approved['headers']['Location']).query)['code'][0]
                exchange=self.call('/oauth/token','POST',{'grant_type':'authorization_code','client_id':client_id,
                    'redirect_uri':redirect,'code':code,'code_verifier':verifier,'resource':resource})
                self.assertTrue(exchange['status'].startswith('200'),exchange['body'])
                access=json.loads(exchange['body'])['access_token']
                response=self.call('/mcp','POST',{'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'ozon_products','arguments':{'limit':1}}},json_body=True,token=access)
                self.assertTrue(response['status'].startswith('200'))
                self.assertEqual(self.calls[-1][0],'ozon_products')
                self.assertNotIn(raw_key,response['body']);self.assertNotIn('synthetic-ozon-secret',response['body'])

    def test_three_clients_code_pkce_rotation_and_revocation(self):
        raw_key=self.create_owner_key()
        key_id=self.app.external_keys.list_active()[0][0]
        self.vault.save('wb',{'api_token':'synthetic-wb-secret'})
        resource='https://3dprinta.shop/mcp'
        for path in ('/.well-known/oauth-protected-resource/mcp','/.well-known/oauth-authorization-server'):
            result=self.call(path)
            self.assertTrue(result['status'].startswith('200'))
            self.assertEqual(result['headers'].get('Cache-Control'),'no-store')
            self.assertNotIn(raw_key,result['body'])

        credentials=[]
        callbacks=[
            ('ChatGPT','https://chatgpt.com/connector_platform_oauth_redirect'),
            ('Claude','https://claude.ai/api/mcp/auth_callback'),
            ('Grok','https://grok.com/connectors-oauth-exchange-code/'),
        ]
        for name,redirect in callbacks:
            with self.subTest(client=name):
                registration=self.call('/oauth/register','POST',{'redirect_uris':[redirect],
                    'client_name':name,'token_endpoint_auth_method':'none',
                    'grant_types':['authorization_code','refresh_token'],'response_types':['code']},json_body=True)
                self.assertTrue(registration['status'].startswith('201'),registration['body'])
                client_id=json.loads(registration['body'])['client_id']
                verifier=secrets.token_urlsafe(32)
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                state=secrets.token_urlsafe(24)
                query={'response_type':'code','client_id':client_id,'redirect_uri':redirect,
                       'code_challenge':challenge,'code_challenge_method':'S256',
                       'scope':'mcp:read offline_access','state':state,'resource':resource}
                authorization=self.call('/oauth/authorize',query=query)
                self.assertTrue(authorization['status'].startswith('200'),authorization['body'])
                self.assertNotIn(raw_key,authorization['body'])
                nonce=re.search(r'name="csrf" value="([A-Za-z0-9_-]{43})"',authorization['body']).group(1)
                ticket=re.search(r'name="ticket" value="([A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43})"',authorization['body']).group(1)
                cookie=authorization['headers']['Set-Cookie'].split(';',1)[0]
                rejected=self.call('/oauth/authorize','POST',{'csrf':nonce,'ticket':ticket,'key':raw_key},cookie=cookie,
                                   origin='https://attacker.invalid')
                self.assertTrue(rejected['status'].startswith('400'))
                self.assertNotIn(raw_key,rejected['body'])
                approval=self.call('/oauth/authorize','POST',{'csrf':nonce,'ticket':ticket,'key':raw_key},
                                   cookie=cookie,origin='https://3dprinta.shop')
                self.assertTrue(approval['status'].startswith('303'),approval['body'])
                location=approval['headers']['Location']; parsed=urlsplit(location)
                self.assertEqual(parsed.scheme+'://'+parsed.netloc+parsed.path,redirect)
                values=parse_qs(parsed.query)
                self.assertEqual(values['state'],[state]); self.assertNotIn(raw_key,location)
                code=values['code'][0]
                form={'grant_type':'authorization_code','client_id':client_id,'code':code,
                      'redirect_uri':redirect,'code_verifier':verifier,'resource':resource}
                bad=self.call('/oauth/token','POST',{**form,'code_verifier':secrets.token_urlsafe(32)})
                self.assertTrue(bad['status'].startswith('400'))
                exchange=self.call('/oauth/token','POST',form)
                self.assertTrue(exchange['status'].startswith('200'),exchange['body'])
                self.assertEqual(exchange['headers'].get('Cache-Control'),'no-store')
                minted=json.loads(exchange['body']); access=minted['access_token']; refresh=minted['refresh_token']
                self.assertNotEqual(access,raw_key);self.assertNotEqual(refresh,raw_key)
                self.assertTrue(self.call('/oauth/token','POST',form)['status'].startswith('400'))
                success=self.mcp(access)
                self.assertTrue(success['status'].startswith('200'),success['body'])
                self.assertNotIn('synthetic-wb-secret',success['body'])
                self.assertEqual(self.calls[-1][0],'wb_prices')
                rotation={'grant_type':'refresh_token','client_id':client_id,'refresh_token':refresh,'resource':resource}
                rotated=self.call('/oauth/token','POST',rotation)
                self.assertTrue(rotated['status'].startswith('200'),rotated['body'])
                rotated_tokens=json.loads(rotated['body'])
                self.assertTrue(self.call('/oauth/token','POST',rotation)['status'].startswith('400'))
                self.assertTrue(self.mcp(access)['status'].startswith('401'))
                self.assertTrue(self.mcp(rotated_tokens['access_token'])['status'].startswith('200'))
                credentials.append((client_id,rotated_tokens))

        # Plain bearer keys continue working, and revoking one key invalidates
        # both the raw credential and every OAuth token derived from it.
        self.assertTrue(self.mcp(raw_key)['status'].startswith('200'))
        self.assertTrue(self.request('/settings/mcp/revoke','POST',{'csrf':self.csrf,'key_id':key_id})['status'].startswith('303'))
        self.assertTrue(self.mcp(raw_key)['status'].startswith('401'))
        for client_id,tokens in credentials:
            self.assertTrue(self.mcp(tokens['access_token'])['status'].startswith('401'))
            refresh=self.call('/oauth/token','POST',{'grant_type':'refresh_token','client_id':client_id,
                'refresh_token':tokens['refresh_token'],'resource':resource})
            self.assertTrue(refresh['status'].startswith('400'))
        with open(self.vault.path,'rb') as vault:
            data=vault.read()
            self.assertNotIn(raw_key.encode(),data)
            for _,tokens in credentials:
                self.assertNotIn(tokens['access_token'].encode(),data)
                self.assertNotIn(tokens['refresh_token'].encode(),data)


if __name__=='__main__': unittest.main()
