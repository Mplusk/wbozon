"""Synthetic Streamable HTTP, authorization and tool contract checks."""
import io
import json
import unittest

import external_mcp as mcp
import ozon_mcp


class ExternalMCPTests(unittest.TestCase):
    def setUp(self):
        self.token='mcp_'+'A'*43
        self.calls=[]
    def request(self, obj=None, *, method='POST', token=None, origin=None, headers=None, raw=None, auth=None, execute=None):
        if raw is None: raw=json.dumps(obj).encode('utf-8')
        e={'REQUEST_METHOD':method,'CONTENT_TYPE':'application/json', 'CONTENT_LENGTH':str(len(raw)),
           'wsgi.input':io.BytesIO(raw)}
        if token is not False: e['HTTP_AUTHORIZATION']='Bearer '+(token or self.token)
        if origin is not None: e['HTTP_ORIGIN']=origin
        e.update(headers or {})
        def verify(t): return 'key-id' if t==self.token else None
        def run(key,name,args):
            self.calls.append((key,name,args))
            return {'items':[], 'read_only':True, 'untrusted_data':True}
        out={}
        def start(status,header_list): out.update(status=status,headers=dict(header_list))
        body=b''.join(mcp.handle(e,start,auth or verify,execute or run,'https://3dprinta.shop'))
        out['body']=json.loads(body) if body else None
        return out
    def rpc(self,method,params=None,ident=1):
        return {'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}}
    def test_initialize_negotiates_supported_version_without_session(self):
        for version in mcp.SUPPORTED+('unknown',):
            out=self.request(self.rpc('initialize',{'protocolVersion':version,'capabilities':{},'clientInfo':{'name':'x','version':'1'}}))
            self.assertEqual(out['status'],'200 OK')
            self.assertEqual(out['body']['result']['protocolVersion'],version if version in mcp.SUPPORTED else mcp.LATEST)
            self.assertIn('tools',out['body']['result']['capabilities'])
            self.assertNotIn('Mcp-Session-Id',out['headers'])
    def test_read_only_tool_list_and_matching_validation(self):
        out=self.request(self.rpc('tools/list'),headers={'HTTP_MCP_PROTOCOL_VERSION':'2025-11-25'})
        tools=out['body']['result']['tools']
        self.assertEqual({t['name'] for t in tools}, {'ozon_products','ozon_product_info','ozon_accrual_types','ozon_accrual_day','ozon_posting_accruals','ozon_article_summary','ozon_realization_article_month','ozon_return_events','ozon_prices','ozon_orders_analytics','ozon_posting_status','ozon_posting_candidates','wb_products','wb_prices','wb_commissions','wb_box_tariffs','wb_sales_summary'})
        self.assertTrue(all(t['annotations']['readOnlyHint'] and not t['annotations']['destructiveHint'] for t in tools))
        self.assertTrue(all(t['_meta']['securitySchemes']==[{'type':'oauth2','scopes':['mcp:read']}] for t in tools))
        self.assertTrue(all(t['securitySchemes']==t['_meta']['securitySchemes'] for t in tools))
    def test_notifications_are_accepted_without_response_body_or_execution(self):
        out=self.request({'jsonrpc':'2.0','method':'notifications/initialized'})
        self.assertEqual(out['status'],'202 Accepted');self.assertIsNone(out['body']);self.assertFalse(self.calls)
    def test_tool_call_returns_real_callback_result(self):
        params={'name':'wb_prices','arguments':{'limit':1,'offset':10}}
        out=self.request(self.rpc('tools/call',params,ident='client-id'))
        self.assertEqual(out['body']['id'],'client-id')
        self.assertEqual(self.calls,[('key-id','wb_prices',params['arguments'])])
        self.assertTrue(json.loads(out['body']['result']['content'][0]['text'])['read_only'])

    def test_oversized_tool_result_is_explicit_and_not_silently_truncated(self):
        params={'name':'ozon_posting_accruals','arguments':{'posting_numbers':['A'],'view':'details'}}
        out=self.request(self.rpc('tools/call',params),execute=lambda *_:{'data':'x'*(mcp.MAX_OUTPUT+1)})
        result=out['body']['result']
        self.assertTrue(result['isError'])
        info=json.loads(result['content'][0]['text'])
        self.assertEqual(info['error'],'response_too_large')
        self.assertIn('не был обрезан',info['message'])
        self.assertNotIn('x'*100, json.dumps(result))

    def test_ozon_product_sku_reaches_mcp_client_with_limitations(self):
        listed = self.request(self.rpc('tools/list'))['body']['result']['tools']
        description = next(tool['description'] for tool in listed if tool['name'] == 'ozon_products')
        self.assertIn('SKU', description)
        self.assertIn('название', description)
        self.assertIn('identity_enrichment', description)
        self.assertIn('не подтверждены', description)
        def run(_, name, args):
            return ozon_mcp.project_response(name, args, {'result': {'items': [
                {'product_id': 7, 'offer_id': '0007', 'sku': 9007199254740993}], 'last_id': ''}},
                {'client_id': '123', 'api_key': 'synthetic-api-key'})
        response = self.request(self.rpc('tools/call', {'name': 'ozon_products', 'arguments': {'limit': 1}}), execute=run)
        result = json.loads(response['body']['result']['content'][0]['text'])
        self.assertEqual(result['items'][0]['offer_id'], '0007')
        self.assertEqual(result['items'][0]['sku'], '9007199254740993')
        self.assertEqual(result['identity']['sku_coverage'], 'not_verified')
        self.assertEqual(result['pagination_state'], 'empty_cursor')
        self.assertNotIn('synthetic-api-key', json.dumps(response))
    def test_order_reports_accept_period_and_reject_invalid_arguments(self):
        period={'date_from':'2026-11-02', 'date_to':'2026-11-08'}
        for name in ('ozon_orders_analytics','wb_sales_summary'):
            with self.subTest(name=name):
                out=self.request(self.rpc('tools/call',{'name':name,'arguments':period}))
                self.assertNotIn('isError',out['body']['result'])
                self.assertEqual(self.calls[-1],('key-id',name,period))
                before=len(self.calls)
                invalid=self.request(self.rpc('tools/call',{'name':name,'arguments':{'date_from':'invalid','date_to':'2026-11-08'}}))
                self.assertTrue(invalid['body']['result']['isError'])
                self.assertEqual(len(self.calls),before)
    def test_ozon_posting_status_requires_a_valid_scheme(self):
        args={'date_from':'2026-11-02','date_to':'2026-11-08','scheme':'FBO'}
        out=self.request(self.rpc('tools/call',{'name':'ozon_posting_status','arguments':args}))
        self.assertNotIn('isError',out['body']['result'])
        self.assertEqual(self.calls[-1],('key-id','ozon_posting_status',args))
        before=len(self.calls)
        out=self.request(self.rpc('tools/call',{'name':'ozon_posting_status','arguments':{**args,'scheme':'bad'}}))
        self.assertTrue(out['body']['result']['isError'])
        self.assertEqual(len(self.calls),before)
    def test_seller_errors_are_safe_and_not_jsonrpc_errors(self):
        def fail(*_): raise mcp.MCPToolError('provider_access_denied')
        out=self.request(self.rpc('tools/call',{'name':'wb_prices'}),execute=fail)
        self.assertTrue(out['body']['result']['isError'])
        self.assertEqual(json.loads(out['body']['result']['content'][0]['text'])['error'],'provider_access_denied')
        def fail_sensitive(*_): raise Exception('SENSITIVE-TOKEN')
        out=self.request(self.rpc('tools/call',{'name':'wb_prices'}),execute=fail_sensitive)
        self.assertNotIn('SENSITIVE-TOKEN',json.dumps(out))
        self.assertTrue(out['body']['result']['isError'])
    def test_authentication_required_and_revocation_is_immediate(self):
        for token in (False,'mcp_'+'B'*43,'A'*43,'mcp_x'):
            out=self.request(self.rpc('tools/list'),token=token)
            self.assertEqual(out['status'],'401 Unauthorized')
            self.assertEqual(out['headers']['WWW-Authenticate'],'Bearer resource_metadata="https://3dprinta.shop/.well-known/oauth-protected-resource/mcp"')
        self.assertEqual(self.request(self.rpc('tools/list'),auth=lambda t:None)['status'],'401 Unauthorized')
        self.assertEqual(self.request(self.rpc('tools/list'),headers={'HTTP_AUTHORIZATION':'bearer '+self.token})['status'],'200 OK')
        def broken(_): raise RuntimeError('secret')
        out=self.request(self.rpc('tools/list'),auth=broken)
        self.assertEqual(out['status'],'503 Service Unavailable');self.assertNotIn('secret',json.dumps(out))
    def test_origin_and_transport_validation(self):
        self.assertEqual(self.request(self.rpc('tools/list'),origin='https://evil.example')['status'],'403 Forbidden')
        self.assertEqual(self.request(self.rpc('tools/list'),origin='https://3dprinta.shop')['status'],'200 OK')
        out=self.request(method='GET');self.assertEqual(out['status'],'405 Method Not Allowed')
        self.assertEqual(self.request(self.rpc('ping'),headers={'CONTENT_TYPE':'text/plain'})['status'],'415 Unsupported Media Type')
        self.assertEqual(self.request(self.rpc('ping'),headers={'HTTP_MCP_PROTOCOL_VERSION':'2026-07-28'})['status'],'400 Bad Request')
    def test_bad_requests_and_tools_never_reach_marketplace(self):
        for value in ([],None,{'jsonrpc':'2.0','id':True,'method':'ping'},{'jsonrpc':'2.0','method':'tools/call','params':{}}):
            self.assertTrue(self.request(value)['status'].startswith('400'))
        self.assertTrue(self.request(raw=b'not JSON')['status'].startswith('400'))
        self.assertTrue(self.request(raw=b'x'*(mcp.MAX_BODY+1))['status'].startswith('400'))
        out=self.request(self.rpc('tools/call',{'name':'wb_prices','arguments':{'url':'http://internal/'}}))
        self.assertTrue(out['body']['result']['isError'])
        out=self.request(self.rpc('tools/call',{'name':'wb_delete','arguments':{}}))
        self.assertEqual(out['body']['error']['code'],-32602)
        self.assertFalse(self.calls)
    def test_tool_arguments_whitelist_does_not_allow_browser_lookup(self):
        out=self.request(self.rpc('tools/call',{'name':'wb_prices','arguments':{'limit':1,'nm_id':123}}))
        self.assertTrue(out['body']['result']['isError']);self.assertFalse(self.calls)
    def modern(self,method,params=None,headers=None):
        data=dict(params or {})
        data['_meta']={'io.modelcontextprotocol/protocolVersion':mcp.MODERN,
                       'io.modelcontextprotocol/clientCapabilities':{}}
        extra={'HTTP_MCP_PROTOCOL_VERSION':mcp.MODERN,'HTTP_MCP_METHOD':method}
        if method=='tools/call': extra['HTTP_MCP_NAME']=data.get('name','')
        extra.update(headers or {})
        return self.request(self.rpc(method,data),headers=extra)
    def test_modern_discovery_and_read_only_call(self):
        discover=self.modern('server/discover')
        self.assertEqual(discover['status'],'200 OK')
        result=discover['body']['result']
        self.assertEqual(result['resultType'],'complete')
        self.assertIn(mcp.MODERN,result['supportedVersions'])
        self.assertIn('2025-11-25',result['supportedVersions'])
        self.assertEqual(result['_meta']['io.modelcontextprotocol/serverInfo']['name'],'sidzen-marketplace-readonly')
        listed=self.modern('tools/list')['body']['result']
        self.assertEqual(listed['resultType'],'complete');self.assertEqual(len(listed['tools']),len(mcp.TOOLS))
        self.assertEqual((listed['ttlMs'],listed['cacheScope']),(0,'private'))
        call=self.modern('tools/call',{'name':'ozon_products','arguments':{'limit':1}})
        self.assertEqual(call['body']['result']['resultType'],'complete')
        self.assertEqual(self.calls,[('key-id','ozon_products',{'limit':1})])
    def test_modern_headers_must_match_the_message(self):
        for mismatch in ({'HTTP_MCP_METHOD':'tools/list'}, {'HTTP_MCP_NAME':'wb_products'},
                         {'HTTP_MCP_PROTOCOL_VERSION':'2025-11-25'}):
            with self.subTest(mismatch=mismatch):
                result=self.modern('tools/call',{'name':'wb_prices','arguments':{}},mismatch)
                self.assertEqual(result['status'],'400 Bad Request')
                self.assertEqual(result['body']['error']['code'],-32020)
        self.assertFalse(self.calls)
    def test_modern_encoded_name_and_missing_client_capabilities(self):
        encoded='=?base64?d2JfcHJpY2Vz?='
        out=self.modern('tools/call',{'name':'wb_prices','arguments':{}},{'HTTP_MCP_NAME':encoded})
        self.assertEqual(out['status'],'200 OK')
        data=self.rpc('server/discover',{'_meta':{'io.modelcontextprotocol/protocolVersion':mcp.MODERN}})
        out=self.request(data,headers={'HTTP_MCP_PROTOCOL_VERSION':mcp.MODERN,'HTTP_MCP_METHOD':'server/discover'})
        self.assertEqual(out['status'],'400 Bad Request')
    def test_modern_unknown_method_is_http_404(self):
        out=self.modern('not-supported')
        self.assertEqual(out['status'],'404 Not Found');self.assertEqual(out['body']['error']['code'],-32601)
    def test_future_client_can_negotiate_down_from_unsupported_version(self):
        future='2027-12-31'
        data=self.rpc('server/discover',{'_meta':{'io.modelcontextprotocol/protocolVersion':future,
                                                 'io.modelcontextprotocol/clientCapabilities':{}}})
        out=self.request(data,headers={'HTTP_MCP_PROTOCOL_VERSION':future,'HTTP_MCP_METHOD':'server/discover'})
        self.assertEqual(out['status'],'400 Bad Request')
        self.assertEqual(out['body']['error']['code'],-32022)
        self.assertIn(mcp.MODERN,out['body']['error']['data']['supported'])
        self.assertEqual(out['body']['error']['data']['requested'],future)

if __name__=='__main__': unittest.main()
