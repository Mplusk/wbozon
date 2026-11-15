"""Exercise browser key lifecycle and real WSGI MCP routing with synthetic data."""
import gc
import io
import json
import re
import secrets
import unittest

import test_portal


class ExternalMCPIntegrationTests(unittest.TestCase):
    setUp = test_portal.Tests.setUp
    request = test_portal.Tests.request

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def creation_form(self):
        page = self.request()['body']
        match = re.search(r'name="request_id" value="([A-Za-z0-9_-]{22})"', page)
        self.assertIsNotNone(match)
        return {'csrf':self.csrf, 'request_id':match.group(1), 'name':'Test client'}

    def create(self, form=None, **kwargs):
        form = form or self.creation_form()
        response = self.request('/settings/mcp/create','POST',form,**kwargs)
        return response

    def mcp(self, data, token=None, origin=None):
        raw=json.dumps(data).encode()
        env={'PATH_INFO':'/mcp','REQUEST_METHOD':'POST','HTTP_HOST':'3dprinta.shop',
             'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),
             'HTTP_ACCEPT':'application/json, text/event-stream','wsgi.input':io.BytesIO(raw)}
        if token: env['HTTP_AUTHORIZATION']='Bearer '+token
        if origin is not None: env['HTTP_ORIGIN']=origin
        result={}
        def start(status,headers): result.update(status=status,headers=dict(headers))
        result['body']=b''.join(self.app(env,start)).decode()
        return result

    def test_session_origin_and_csrf_are_required(self):
        form=self.creation_form()
        for kwargs in ({'cookie':False},{'origin':False},
                       {'headers':{'HTTP_ORIGIN':'https://attacker.invalid'}}):
            with self.subTest(kwargs=kwargs):
                r=self.create(form,**kwargs)
                self.assertIn(r['status'].split()[0],('401','403'))
                self.assertEqual(self.app.external_keys.list_active(),[])
        bad={**form,'csrf':secrets.token_urlsafe(24)}
        self.assertTrue(self.create(bad)['status'].startswith('403'))
        self.assertEqual(self.app.external_keys.list_active(),[])

    def test_mcp_keys_work_for_all_active_session_but_marketplace_keys_require_recent_login(self):
        self.now+=301
        page=self.request()['body']
        self.assertIn('Создание и отзыв ключей для ИИ по-прежнему доступны.',page)
        section=page.split('id="external-mcp-title"',1)[1].split('</section>',1)[0]
        self.assertNotIn('disabled',section)
        self.assertTrue(self.request('/settings','POST',{'provider':'wb','csrf':self.csrf,'api_token':'synthetic'})['status'].startswith('403'))
        created=self.create()
        self.assertTrue(created['status'].startswith('200'))
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',created['body']).group(1)
        key_id=self.app.external_keys.list_active()[0][0]
        self.assertTrue(self.mcp({'jsonrpc':'2.0','id':1,'method':'ping'},token)['status'].startswith('200'))
        invalid=self.request('/settings/mcp/revoke','POST',{'csrf':secrets.token_urlsafe(24),'key_id':key_id})
        self.assertTrue(invalid['status'].startswith('403'))
        revoked=self.request('/settings/mcp/revoke','POST',{'csrf':self.csrf,'key_id':key_id})
        self.assertTrue(revoked['status'].startswith('303'))
        self.assertTrue(self.mcp({'jsonrpc':'2.0','id':2,'method':'ping'},token)['status'].startswith('401'))
        self.now+=43200
        self.assertTrue(self.create({'csrf':self.csrf,'request_id':secrets.token_urlsafe(16),'name':'Expired session'})['status'].startswith('401'))

    def test_token_shown_once_replay_denied_and_subsequent_get_is_clean(self):
        form=self.creation_form(); form['name']='<script>client</script>'
        result=self.create(form)
        self.assertTrue(result['status'].startswith('200'))
        self.assertEqual(result['headers'].get('Cache-Control'),'no-store')
        self.assertNotIn('Location',result['headers'])
        self.assertNotIn('<script>client</script>',result['body'])
        self.assertIn('&lt;script&gt;client&lt;/script&gt;',result['body'])
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',result['body']).group(1)
        self.assertNotIn(token,self.request()['body'])
        with open(self.vault.path,'rb') as vault: self.assertNotIn(token.encode(),vault.read())
        replay=self.create(form)
        self.assertTrue(replay['status'].startswith('409'))
        self.assertNotIn(token,replay['body'])
        self.assertEqual(len(self.app.external_keys.list_active()),1)

    def test_generated_mcp_token_is_separate_from_existing_api_and_revoked(self):
        created=self.create()
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',created['body']).group(1)
        key_id=self.app.external_keys.list_active()[0][0]
        self.vault.save('wb',{'api_token':'synthetic-wb'})
        data={'jsonrpc':'2.0','id':1,'method':'tools/call',
              'params':{'name':'wb_prices','arguments':{'limit':1}}}
        self.assertTrue(self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=token)['status'].startswith('401'))
        self.assertTrue(self.mcp(data,token=self.token)['status'].startswith('401'))
        self.now+=1
        result=self.mcp(data,token)
        self.assertTrue(result['status'].startswith('200'))
        self.assertEqual(json.loads(result['body'])['result']['content'][0]['type'],'text')
        self.assertEqual(self.calls[-1][0],'wb_prices')
        self.assertEqual(self.app.external_keys.list_active()[0][3],self.now)
        self.assertIn('последнее обращение:',self.request()['body'])
        invalid={'csrf':secrets.token_urlsafe(24),'key_id':key_id}
        self.assertTrue(self.request('/settings/mcp/revoke','POST',invalid)['status'].startswith('403'))
        self.assertTrue(self.mcp({'jsonrpc':'2.0','id':2,'method':'ping'},token)['status'].startswith('200'))
        revoked=self.request('/settings/mcp/revoke','POST',{'csrf':self.csrf,'key_id':key_id})
        self.assertTrue(revoked['status'].startswith('303'))
        self.assertTrue(self.mcp(data,token)['status'].startswith('401'))
        self.assertTrue(self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=self.token)['status'].startswith('200'))

    def test_mcp_origin_and_tool_list(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        message={'jsonrpc':'2.0','id':7,'method':'tools/list'}
        self.assertTrue(self.mcp(message,token,'https://attacker.invalid')['status'].startswith('403'))
        result=self.mcp(message,token)
        self.assertEqual(result['headers'].get('Cache-Control'),'no-store')
        self.assertEqual(len(json.loads(result['body'])['result']['tools']),9)


if __name__ == '__main__': unittest.main()
