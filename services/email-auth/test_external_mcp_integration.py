"""Exercise browser key lifecycle and real WSGI MCP routing with synthetic data."""
from datetime import datetime, timezone
import gc
import io
import json
import re
import secrets
import unittest
import urllib.error
from unittest.mock import patch
import finance_gate
import portal
import test_ozon_article_summary
import test_ozon_finance_reports
import test_ozon_return_events

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

    def test_authenticated_seller_read_queries_are_not_rate_limited_at_thirty(self):
        _,token=self.app.external_keys.create("Read reports",secrets.token_urlsafe(16),self.app.login.clock(),"marketplaces")
        query={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
            "name":"ozon_clusters","arguments":{}}}
        for _ in range(35):
            response=json.loads(self.mcp(query, token)['body'])['result']
            self.assertEqual(json.loads(response['content'][0]['text'])['status'],'not_configured')

    def test_generic_report_groups_are_available_only_through_the_internal_bridge(self):
        _,token=self.app.external_keys.create("Read reports",secrets.token_urlsafe(16),self.app.login.clock(),"all")
        name='ozon_fbo_reports'
        args={'report':'macroclusters'}
        data={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}
        self.assertEqual(json.loads(self.mcp(data,token)['body'])['error']['code'],-32602)
        internal=self.request('/api/read','POST',{'operation':name,'arguments':args},auth=self.token,cookie=False)
        self.assertEqual(json.loads(internal['body'])['status'],'schema')

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
        names={tool['name'] for tool in json.loads(result['body'])['result']['tools']}
        self.assertEqual(names, {'ozon_products','ozon_product_info','ozon_accrual_types','ozon_accrual_day','ozon_article_summary','ozon_realization_article_month','ozon_return_events',
                                 'ozon_unallocated_accruals','ozon_fbo_supply_orders','ozon_fbo_supply_details','ozon_fbo_supply_items','ozon_fbo_volume_allocation',
                                 'ozon_posting_accruals','ozon_prices','ozon_orders_analytics',
                                 'ozon_posting_status','ozon_posting_candidates','wb_products','wb_prices','wb_commissions',
                                 'wb_box_tariffs','wb_sales_summary'}
                         | (set(portal.market_specs.TOOLS) - portal.market_specs.seller_reports.GROUPS.keys()))

    def test_finance_429_propagates_delay_and_prevents_early_mcp_or_api_retry(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        calls=[]
        def gateway(*args):
            calls.append(1)
            raise urllib.error.HTTPError('',429,'synthetic-private',{'Retry-After':'45'},io.BytesIO(b'private'))
        self.app.gateway=gateway
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_accrual_types','arguments':{}}}
        first=json.loads(self.mcp(message,token)['body'])['result']
        info=json.loads(first['content'][0]['text'])
        self.assertTrue(first['isError'])
        self.assertEqual(info['retry_after_seconds'],45)
        second=json.loads(self.mcp(message,token)['body'])['result']
        info=json.loads(second['content'][0]['text'])
        self.assertEqual(info['reason'],'local_limiter')
        self.assertIsNone(info['http_status'])
        self.assertTrue(1 <= info['retry_after_seconds'] <= 45)
        bridge=self.request('/api/read','POST',{'operation':'ozon_accrual_types','arguments':{}},auth=self.token)
        self.assertTrue(bridge['status'].startswith('429'))
        self.assertTrue(1 <= int(bridge['headers']['Retry-After']) <= 45)
        self.assertEqual(len(calls),1)
        self.assertNotIn('synthetic-private',json.dumps([first,second,bridge]))

    def test_finance_429_without_delay_does_not_repeat(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        calls=[]
        def gateway(*args):
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{},io.BytesIO())
        self.app.gateway=gateway
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_accrual_types','arguments':{}}}
        for attempt in range(2):
            result=json.loads(self.mcp(message,token)['body'])['result']
            info=json.loads(result['content'][0]['text'])
            self.assertEqual(info['http_status'],None if attempt else 429)
            self.assertEqual(info.get('reason'),'local_limiter' if attempt else None)
            self.assertTrue(1 <= info['retry_after_seconds'] <= 60)
        self.assertEqual(len(calls),1)

    def test_retry_interval_matching_secret_is_not_exposed_by_mcp_or_api(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'45'})
        def gateway(*args): raise urllib.error.HTTPError('',429,'',{'Retry-After':'45'},io.BytesIO())
        self.app.gateway=gateway
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_accrual_types','arguments':{}}}
        # The synthetic key is two digits. Keep an unrelated timestamp from
        # containing the same digits and making the leak assertion unreliable.
        with (patch('finance_gate.time.time',return_value=1000.0),
              patch('finance_gate.time.monotonic',return_value=100.0),
              patch('external_mcp.datetime',wraps=datetime) as clock):
            clock.now.return_value=datetime(2026,1,1,tzinfo=timezone.utc)
            response=self.mcp(message,token)
            info=json.loads(json.loads(response['body'])['result']['content'][0]['text'])
            self.assertIsNone(info['retry_after_seconds'])
            self.assertEqual(info['reason'],'retry_interval_unavailable')
            self.assertNotIn('45',response['body'])
            bridge=self.request('/api/read','POST',{'operation':'ozon_accrual_types','arguments':{}},auth=self.token)
        self.assertEqual(bridge['headers'].get('Retry-After'),'60')
        self.assertEqual(json.loads(bridge['body'])['retry_after_seconds'],60)
        self.assertNotIn('45',bridge['body'])

    def test_known_oversized_provider_response_is_reported_without_locking_retries(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        calls=[]
        def gateway(*args):
            calls.append(1)
            if len(calls)==1: raise finance_gate.ProviderResponseTooLarge('private')
            return {'types':[]}
        self.app.gateway=gateway
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_accrual_types','arguments':{}}}
        first=json.loads(self.mcp(message,token)['body'])['result']
        info=json.loads(first['content'][0]['text'])
        self.assertEqual(info['error'],'provider_response_too_large')
        self.assertIn('Фильтр SKU или артикула не уменьшает',info['message'])
        second=json.loads(self.mcp(message,token)['body'])['result']
        self.assertNotIn('isError',second)
        self.assertEqual(len(calls),2)

    def test_partial_article_summary_reaches_authenticated_mcp(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        self.app.gateway=portal._provider_read
        opener=test_ozon_article_summary.FakeOpener(postings={'posting_accruals':[
            {'posting_number':'A-1','accruals':[test_ozon_article_summary.accrual(10,'5.00')]}]})
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_article_summary','arguments':test_ozon_article_summary.ARGS}}
        with patch('portal.urllib.request.build_opener',return_value=opener):
            output=json.loads(self.mcp(message,token)['body'])['result']
        self.assertNotIn('isError',output)
        result=json.loads(output['content'][0]['text'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['identity']['sku_candidates'],['10','11'])
        self.assertFalse(result['selection']['complete_period'])
        self.assertIsNone(result['unknown_metrics']['buyout_units'])
        self.assertEqual(len(opener.calls),4)

    def test_daily_and_monthly_reports_reach_authenticated_mcp(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        self.app.gateway=portal._provider_read
        class Opener:
            def open(self,request,timeout):
                payload=(test_ozon_finance_reports.daily_payload() if request.full_url.endswith('/by-day')
                         else test_ozon_finance_reports.monthly_payload())
                return test_ozon_article_summary.Response(json.dumps(payload,default=str,ensure_ascii=False).encode())
        calls=[('ozon_accrual_day',test_ozon_finance_reports.DAY_ARGS),
               ('ozon_realization_article_month',test_ozon_finance_reports.MONTH_ARGS)]
        with patch('portal.urllib.request.build_opener',return_value=Opener()):
            for name,args in calls:
                with self.subTest(name=name):
                    message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                             'params':{'name':name,'arguments':args}}
                    output=json.loads(self.mcp(message,token)['body'])['result']
                    self.assertNotIn('isError',output)
                    result=json.loads(output['content'][0]['text'])
                    self.assertTrue(result['read_only'])
                    self.assertNotIn('synthetic-private',json.dumps(result))

    def test_monthly_sku_conflict_reaches_mcp_as_blocked_without_quantities(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        self.app.gateway=portal._provider_read
        payload=test_ozon_finance_reports.monthly_payload()
        payload['rows'][2]['item']['sku']=10
        class Opener:
            def open(self,request,timeout):
                return test_ozon_article_summary.Response(json.dumps(payload,default=str,ensure_ascii=False).encode())
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_realization_article_month','arguments':test_ozon_finance_reports.MONTH_ARGS}}
        with patch('portal.urllib.request.build_opener',return_value=Opener()):
            output=json.loads(self.mcp(message,token)['body'])['result']
        self.assertNotIn('isError',output)
        result=json.loads(output['content'][0]['text'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'sku_offer_id_conflict')
        self.assertIsNone(result['observed_delivery_units'])
        self.assertNotIn('by_sku',result)

    def test_return_events_reach_authenticated_mcp(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        self.vault.save('ozon',{'client_id':'777','api_key':'synthetic-private'})
        self.app.gateway=portal._provider_read
        class Opener:
            def open(self,request,timeout):
                self_request=json.loads(request.data)
                if self_request['filter']['return_schema']!='FBO':raise AssertionError('wrong scheme')
                payload={'returns':[test_ozon_return_events.row()],'has_next':False}
                return test_ozon_article_summary.Response(json.dumps(payload,ensure_ascii=False).encode())
        message={'jsonrpc':'2.0','id':7,'method':'tools/call',
                 'params':{'name':'ozon_return_events','arguments':test_ozon_return_events.ARGS}}
        with patch('portal.urllib.request.build_opener',return_value=Opener()):
            output=json.loads(self.mcp(message,token)['body'])['result']
        self.assertNotIn('isError',output)
        result=json.loads(output['content'][0]['text'])
        self.assertEqual(result['matched_rows'],1)
        self.assertEqual(result['period_basis'],'logistic.return_date')
        self.assertIsNone(result['financial_return_total'])
        self.assertNotIn('private-address',json.dumps(result))

    def test_new_finance_tools_share_429_cooldown_with_existing_tools(self):
        token=re.search(r'value="(mcp_[A-Za-z0-9_-]{43})"',self.create()['body']).group(1)
        calls=[]
        def gateway(*args):
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{'Retry-After':'45'},io.BytesIO())
        self.app.gateway=gateway
        for index,(name,args) in enumerate([('ozon_accrual_day',test_ozon_finance_reports.DAY_ARGS),
                                           ('ozon_unallocated_accruals',{'date':'2026-11-15'}),
                                           ('ozon_realization_article_month',test_ozon_finance_reports.MONTH_ARGS)]):
            self.vault.save('ozon',{'client_id':str(700+index),'api_key':'synthetic-private'})
            message={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':name,'arguments':args}}
            first=json.loads(self.mcp(message,token)['body'])['result']
            self.assertTrue(first['isError'])
            second=json.loads(self.mcp({'jsonrpc':'2.0','id':8,'method':'tools/call',
                'params':{'name':'ozon_accrual_types','arguments':{}}},token)['body'])['result']
            info=json.loads(second['content'][0]['text'])
            self.assertEqual(info['reason'],'local_limiter')
            self.assertIsNone(info['http_status'])
            self.assertEqual(len(calls),index+1)
            self.assertNotIn('synthetic-private',json.dumps([first,second]))

    def test_new_finance_methods_reject_duplicate_json_and_oversized_response(self):
        creds={'client_id':'777','api_key':'synthetic-private'}
        for name,args in [('ozon_accrual_day',test_ozon_finance_reports.DAY_ARGS),
                          ('ozon_unallocated_accruals',{'date':'2026-11-15'}),
                          ('ozon_fbo_supply_items',{'bundle_id':'abc'}),
                          ('ozon_realization_article_month',test_ozon_finance_reports.MONTH_ARGS)]:
            for raw,exception in [(b'{"x":1,"x":2}',finance_gate.ProviderResponseInvalid),
                                  (b' ' * 512001,finance_gate.ProviderResponseTooLarge)]:
                class Opener:
                    def open(self,request,timeout): return test_ozon_article_summary.Response(raw)
                with self.subTest(name=name,exception=exception.__name__), \
                     patch('portal.urllib.request.build_opener',return_value=Opener()), self.assertRaises(exception):
                    portal._provider_read(name,args,creds)


if __name__ == '__main__': unittest.main()
