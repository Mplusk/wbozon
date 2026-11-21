import base64
import io
import json
import secrets
import ssl
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch
import time

import finance_gate
import market_actions as actions
import market_catalog as catalog
import market_https
import market_transport
import seller_reports
import test_external_mcp_integration
import test_portal


class CatalogTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    mcp=test_external_mcp_integration.ExternalMCPIntegrationTests.mcp

    def key(self,access):
        return self.app.external_keys.create(access,secrets.token_urlsafe(16),self.now,access)

    @staticmethod
    def wb_token(kind, category, seller='fixture-seller', expires=None):
        claims = dict(acc=kind, sid=seller, s=1 << category, t=False,
                      exp=int(time.time())+900 if expires is None else expires)
        segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        return {'api_token':'fixture.'+segment+'.signature'}

    def test_restricted_wb_reads_select_a_compatible_key_only_for_the_same_seller(self):
        rating = catalog.find_route('wb_get_api_common_rating')
        recs = catalog.find_route('wb_post_api_content_recommendations_list')
        personal = self.wb_token(3,7)
        service = self.wb_token(4,7)
        other_seller = self.wb_token(4,7,seller='other-seller')
        self.vault.save('wb_feedback',personal)
        self.vault.save('wb',service)
        self.assertEqual(catalog.read_credentials(self.vault,rating),service)
        self.vault.save('wb',other_seller)
        self.assertEqual(catalog.read_credentials(self.vault,rating),personal)
        self.assertEqual(catalog.wb_read_access_issue(recs,service)['reason'],'wb_token_category_required')
        self.assertEqual(catalog.wb_read_access_issue(rating,self.wb_token(4,7,expires=1))['reason'],'wb_token_expired')

    def test_personal_wb_rating_token_is_reported_locally_without_an_http_request(self):
        op = 'wb_get_api_common_rating'
        creds = self.wb_token(3,7)
        with patch('urllib.request.OpenerDirector.open') as send:
            result = catalog.transfer('market_api_read',{'operation_id':op,'input':{}},creds,
                                      self.app.finance_gate_dir,time.monotonic()+5)
            debug = seller_reports.execute('wb_seller_information',{'report':'seller_rating','input':{}},creds,
                                           self.app.finance_gate_dir,time.monotonic()+5)
        send.assert_not_called()
        self.assertEqual(result['access_issue']['reason'],'wb_token_type_required')
        self.assertEqual(result['access_issue']['credential_type'],'Персональный')
        self.assertEqual(result['access_issue']['supported_token_types'],['Сервисный'])
        self.assertIsNone(result['http_status'])
        self.assertFalse(result['complete'])
        self.assertEqual(debug['status'],'provider_access_denied')

    def test_recommendations_report_actual_wb_denial_without_guessing_subscription_state(self):
        op = 'wb_post_api_content_recommendations_list'
        creds = self.wb_token(3,1)
        reply = io.BytesIO(json.dumps({'detail':creds['api_token']}).encode())
        rejection = urllib.error.HTTPError('',403,'Forbidden',Message(),reply)
        with patch('urllib.request.OpenerDirector.open',side_effect=rejection) as send:
            result = catalog.transfer('market_api_read',{'operation_id':op,'input':{'body':{'limit':1}}},
                                      creds,self.app.finance_gate_dir,time.monotonic()+5)
        send.assert_called_once()
        issue = result['access_issue']
        self.assertEqual(issue['reason'],'content_recommendations_access_required')
        self.assertEqual(issue['http_status'],403)
        self.assertTrue(issue['category_allowed_by_token'])
        self.assertEqual(issue['subscription_status'],'unverified')
        self.assertIn('Джем',issue['required_subscription'])
        self.assertNotIn(creds['api_token'],json.dumps(result))
        self.assertTrue(reply.closed)

    def test_compatible_wb_rating_and_recommendations_can_return_real_fields(self):
        fixtures = (
            ('wb_get_api_common_rating',self.wb_token(4,7),{}, {'valuation':4.7,'feedbackCount':10}),
            ('wb_post_api_content_recommendations_list',self.wb_token(3,1),{'body':{'limit':1}},
             {'data':[{'nmId':123,'recomNms':[456]}],'next':0}),
        )
        for operation, creds, inputs, data in fixtures:
            with self.subTest(operation=operation):
                response = io.BytesIO(json.dumps(data).encode())
                response.status = 200
                response.headers = Message()
                response.headers['Content-Type'] = 'application/json'
                with patch('urllib.request.OpenerDirector.open',return_value=response) as send:
                    result = catalog.transfer('market_api_read',{'operation_id':operation,'input':inputs},creds,
                                              self.app.finance_gate_dir,time.monotonic()+5)
                self.assertEqual(send.call_args.args[0].get_header('Authorization'),creds['api_token'])
                self.assertEqual(result['data'],data)

    def call(self,token,name,args):
        body=json.loads(self.mcp({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},token)['body'])
        if 'error' in body:
            return body['error']
        return json.loads(body['result']['content'][0]['text'])

    def setup_action(self):
        self.vault.save('wb',{'api_token':'synthetic-wb-permission'})
        key,token=self.key('catalog_operations')
        args={'operation_id':'wb_get_adv_start','input':{'query':{'id':1}},'request_id':secrets.token_urlsafe(16)}
        result=self.call(token,'market_api_propose',args)
        pid=result['proposal_id']
        form={'action':'approve','pid':pid,'csrf':self.csrf,'confirm':'yes',
              'fingerprint':actions.fingerprint(args['operation_id'],args['input'])}
        return key,token,args,pid,form

    def test_catalogue_and_strict_authorization(self):
        self.assertEqual(len(catalog.routes()),809)
        identifiers=list(catalog.routes())
        for operation in (identifiers[0],identifiers[len(identifiers)//2],identifiers[-1]):
            self.assertEqual(catalog.find_route(operation),catalog.routes()[operation])
        self.assertIsNone(catalog.find_route('absent_operation'))
        self.assertEqual(catalog.routes()['wb_get_adv_start']['category'],'write')
        self.assertEqual(catalog.routes()['ozon_post_v1_fbp_order_direct_timeslot_edit']['category'],'write')
        for access,expected in [('marketplaces',set()),('avito',set()),('all',set()),
                                ('catalog_read',set(catalog.READ_TOOLS)),('catalog_operations',set(catalog.PUBLIC_TOOLS))]:
            _,token=self.key(access)
            listed=json.loads(self.mcp({'jsonrpc':'2.0','id':1,'method':'tools/list'},token)['body'])
            names={x['name'] for x in listed['result']['tools']}
            self.assertEqual(names & catalog.PUBLIC_TOOLS.keys(),expected)
            if access!='catalog_operations':
                self.assertEqual(self.call(token,'market_api_propose',{})['code'],-32602)
        _,token=self.key('catalog_read')
        data=self.call(token,'market_api_search',{'market':'ozon','limit':1})
        self.assertEqual((len(data['operations']),data['next_offset']),(1,1))
        meta=self.call(token,'market_api_describe',{'operation_id':'wb_get_adv_start'})
        self.assertEqual(meta['category'],'write')
        denied=self.call(token,'market_api_read',{'operation_id':'wb_get_adv_start','input':{'query':{'id':1}}})
        self.assertEqual(denied.get('error'),'invalid_arguments')
        self.assertFalse(self.calls)

    def test_owner_confirmation_only_revocation_and_idempotency(self):
        key,token,args,pid,form=self.setup_action()
        self.assertFalse(self.calls)
        self.assertEqual(self.call(token,'market_api_propose',args)['proposal_id'],pid)
        self.assertIn('403',self.request('/settings/catalog','POST',form,origin=False)['status'])
        self.assertIn('403',self.request('/settings/catalog','POST',dict(form,csrf='wrong'))['status'])
        self.assertIn('401',self.request('/settings/catalog','POST',form,cookie=False)['status'])
        self.assertIn('409',self.request('/settings/catalog','POST',dict(form,confirm='no'))['status'])
        self.assertIn('wb_get_adv_start',self.request('/settings/catalog')['body'])
        self.assertFalse(self.calls)
        self.app.gateway=lambda op,a,c:(self.calls.append(op) or {'operation_status':'accepted','http_status':202})
        self.assertIn('303',self.request('/settings/catalog','POST',form)['status'])
        self.assertIn('409',self.request('/settings/catalog','POST',form)['status'])
        self.assertEqual(self.calls,['market_api_propose'])
        self.assertEqual(self.call(token,'market_api_status',{'proposal_id':pid})['status'],'accepted')
        self.app.external_keys.revoke(key,self.now)
        self.assertIn('401',self.mcp({'jsonrpc':'2.0','id':1,'method':'ping'},token)['status'])

    def test_reviewed_aliases_use_the_current_wire_contract_and_safety_policy(self):
        for row in catalog.routes().values():
            if 'alias_of' not in row:
                continue
            current=catalog.routes()[row['alias_of']]
            for field in ('method','host','path','category','contract','quota_group','interval_seconds'):
                self.assertEqual(row.get(field),current.get(field),row['operation_id'])
            self.assertEqual(catalog.find_route(row['operation_id']),row)
        for name in ('ozon_verify_courier_code','ozon_create_or_get_product_exemplar','ozon_post_v1_notification_check'):
            with self.assertRaises(ValueError):
                catalog.validate('market_api_read',{'operation_id':name,'input':{}})
        self.assertEqual(catalog.describe('ozon_get_supply_timeslots')['path'],'/v2/supply-order/timeslot/list')
        self.assertEqual(catalog.describe('ozon_ship')['category'],'retired')
        self.assertEqual(catalog.describe('ozon_refund_r_f_b_s')['category'],'superseded')
        self.assertIn('ozon_refund_r_f_b_s',
                      {r['operation_id'] for r in catalog.search({'search':'/v2/returns/rfbs/return-money'})['operations']})
        alias=catalog.routes()['ozon_update_stocks']
        params={'body':{'stocks':[{'offer_id':'example','stock':1,'warehouse_id':1}]}}
        self.assertEqual(actions.fingerprint(alias['operation_id'],params),actions.fingerprint(alias['alias_of'],params))
        rating=catalog.routes()['wb_get_api_analytics_banned_products_shadowed']
        with self.assertRaisesRegex(ValueError,'documented filter'):
            catalog.validate_inputs(rating,{'body':{'onlyShadowedNms':False}})

    def test_digital_credentials_are_isolated_for_reads_proposals_and_public_catalogue(self):
        self.vault.save('wb',{'api_token':'ordinary-seller'})
        self.vault.save('wb_write',{'api_token':'ordinary-writer'})
        public=catalog.routes()['wb_get_api_catalog']
        row=catalog.routes()['wb_get_api_offers_author']
        change=catalog.routes()['wb_post_api_offers']
        creds=catalog.read_credentials(self.vault,public)
        self.assertIsNone(catalog.build_request(public,{},creds).get_header('Authorization'))
        self.assertIsNone(catalog.read_credentials(self.vault,row))
        with self.assertRaises(actions.ActionError):
            actions.Store(self.app).account(change)
        with self.assertRaises(ValueError):
            catalog.build_request(row,{},self.vault.read('wb'))
        self.vault.save('wb_digital',{'digital_token':'synthetic-digital'})
        self.assertEqual(actions.Store(self.app).account(change)[:2],('wb_digital',{'digital_token':'synthetic-digital'}))
        self.assertEqual(catalog.build_request(row,{},catalog.read_credentials(self.vault,row)).get_header('Authorization'),
                         'Bearer synthetic-digital')
        self.assertTrue(actions.publication(change))
        self.assertIsNone(catalog.build_request(public,{},catalog.read_credentials(self.vault,public)).get_header('Authorization'))
        self.assertEqual(self.request('/settings','POST',{'csrf':self.csrf,'provider':'wb_digital',
                         'digital_token':'Bearer synthetic-new'})['status'],'200 OK')
        self.assertEqual(self.vault.read('wb_digital'),{'digital_token':'synthetic-new'})

    def test_digital_files_and_activation_keys(self):
        creds={'digital_token':'synthetic-digital'}
        row=catalog.routes()['wb_get_api_content_download_uri']
        args={'path':{'uri':'vol19/924/document/content'}}
        req=catalog.build_request(row,args,creds)
        self.assertEqual(req.full_url,'https://devapi-digital.wildberries.ru/api/v1/content/download/vol19/924/document/content')
        self.assertEqual(req.get_header('Range'),'bytes=0-368639')
        for bad in ('../foo','//outside.test/file','vol/%2e%2e/foo','https://elsewhere.test/file'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                catalog.build_request(row,{'path':{'uri':bad}},creds)
        with self.assertRaises(ValueError):
            catalog.build_request(row,dict(args,header={'Range':'bytes=0-9999999'}),creds)
        response=io.BytesIO(b'{"items":[{"id":2,"value":"one-use-key"}],"total":1}')
        response.status=200
        response.headers=Message()
        response.headers['Content-Type']='application/json'
        args={'operation_id':'wb_get_api_keys_api_keys_redeemed','input':{}}
        with patch('urllib.request.OpenerDirector.open',return_value=response),patch('market_transport.Wire.admit_catalog'), \
             patch.object(catalog.market_cooldown,'request',wraps=catalog.market_cooldown.request) as guard:
            result=catalog.transfer('market_api_read',args,creds,self.app.finance_gate_dir,time.monotonic()+5)
        self.assertEqual(guard.call_args.args[2],{'api_token':'synthetic-digital'})
        self.assertEqual(result['data'],{'items':[{'id':2}],'total':1})

    def test_ambiguous_outcome_blocks_equivalent_replay_and_pause(self):
        _,token,args,pid,form=self.setup_action()
        def failed(op,a,c):
            self.calls.append(op)
            raise finance_gate.GateError('storage_unavailable')
        self.app.gateway=failed
        self.assertIn('303',self.request('/settings/catalog','POST',form)['status'])
        self.assertEqual(self.call(token,'market_api_status',{'proposal_id':pid})['status'],'unknown')
        with self.assertRaises(actions.ActionError):
            actions.Store(self.app).propose(self.app.external_keys.check_token(token),dict(args,request_id=secrets.token_urlsafe(16)))
        self.assertIn('303',self.request('/settings/catalog','POST',{'action':'pause','csrf':self.csrf})['status'])
        with self.assertRaises(finance_gate.LocalPreparationError):
            with actions.transmission({'journal':actions.Store(self.app).path,'proposal':pid},args):
                self.fail('paused orphan should never reach transport')
        self.assertEqual(len(self.calls),1)

    def test_step_up_rotation_and_worker_one_shot(self):
        key,token,args,pid,form=self.setup_action()
        self.now+=301
        self.assertIn('403',self.request('/settings/catalog','POST',form)['status'])
        self.now-=301
        self.vault.save('wb',{'api_token':'replacement-wb'})
        self.assertIn('409',self.request('/settings/catalog','POST',form)['status'])
        self.assertFalse(self.calls)
        store=actions.Store(self.app)
        with store.db() as db:
            db.execute("UPDATE proposals SET status='unknown' WHERE id=?",(pid,))
            store.event(db,pid,'owner_approved')
        ticket={'journal':store.path,'proposal':pid}
        with actions.transmission(ticket,args):
            pass
        with self.assertRaises(finance_gate.LocalPreparationError):
            with actions.transmission(ticket,args): self.fail('duplicate transport')

    def test_destination_input_and_multipart(self):
        r=catalog.routes()['wb_fbs_stock_update']
        inputs={'path':{'warehouseId':2},'body':{'stocks':[{'chrtId':1,'amount':5}]}}
        with self.assertRaises(ValueError):
            catalog.build_request(dict(r,host='other.invalid'),inputs,{'api_token':'fixture'})
        with self.assertRaises(ValueError):
            catalog.build_request(r,dict(inputs,path={'warehouseId':'../..'}),{'api_token':'fixture'})
        self.assertEqual(actions.fingerprint(r['operation_id'],inputs),
                         actions.fingerprint(r['operation_id'],dict(inputs,media_type='application/json')))
        row=catalog.routes()['wb_post_content_media_file']
        upload={'body':{'uploadfile':'YWJj'},'files':{'uploadfile':{'name':'picture.png','type':'image/png'}},
                'header':{'X-Nm-Id':'42','X-Photo-Number':1}}
        req=catalog.build_request(row,upload,{'api_token':'fixture'})
        self.assertIn(b'filename="picture.png"',req.data)
        self.assertIn(b'image/png',req.data)
        spec=catalog.routes()['wb_post_api_search_report_product_search_texts']['contract']
        for value in (10,31):
            catalog._check(value,spec['definitions']['TextLimit'],spec['definitions'])
        with self.assertRaises(ValueError):
            catalog._check(101,spec['definitions']['TextLimit'],spec['definitions'])

    def test_response_redaction_and_provider_outcomes(self):
        credentials={'api_token':'secret-token'}
        row='wb_get_adv_start'
        args={'operation_id':row,'request_id':'Z'*20,'input':{'query':{'id':1}}}
        for raw,status,expected in [(b'{"error":true}',200,'denied'),(b'{"items":[{"errors":["value"]}]}',207,'unknown'),
                                    (b'{"queued":true}',202,'accepted')]:
            response=io.BytesIO(raw)
            response.status=status
            response.headers=Message()
            response.headers['Content-Type']='application/json'
            with patch('urllib.request.OpenerDirector.open',return_value=response),patch('market_transport.Wire.admit_catalog'):
                out=catalog.transfer('market_api_propose',args,credentials,self.app.finance_gate_dir,time.monotonic()+5)
            self.assertEqual(out['operation_status'],expected)
        response=io.BytesIO(b'{"extra":"secret\\u002dtoken"}')
        response.status=200
        response.headers=Message()
        response.headers['Content-Type']='application/json'
        with patch('urllib.request.OpenerDirector.open',return_value=response),patch('market_transport.Wire.admit_catalog'):
            with self.assertRaises(finance_gate.ProviderResponseInvalid):
                catalog.transfer('market_api_propose',args,credentials,self.app.finance_gate_dir,time.monotonic()+5)

    def test_ozon_acts_share_cache_and_respect_pacing_and_retry_after(self):
        directory = self.app.finance_gate_dir
        creds = {"client_id":"91","api_key":"synthetic-acceptance-key"}
        summary = "ozon_post_v1_supply_order_act_summary_get"
        products = "ozon_post_v1_supply_order_act_product_get"
        url = "https://api-seller.ozon.ru/v1/supply-order/act/summary/get"
        now = [1770000000.0]
        attempts = []

        def send(request, **_):
            attempts.append(request.full_url)
            if len(attempts) == 2:
                error = urllib.error.HTTPError(request.full_url,429,"",{"Retry-After":"9"},io.BytesIO())
                raise error
            document = {"supplies_acts":[{"is_agreement_completed":True}]} if request.full_url == url else {"supply_acts":[]}
            response = io.BytesIO(json.dumps(document).encode())
            response.status = 200
            return response

        def get(operation, body):
            args = {"operation_id":operation,"input":{"body":body}}
            return catalog.transfer("market_api_read",args,creds,directory,time.monotonic()+20)

        with patch("urllib.request.OpenerDirector.open",side_effect=send), patch.object(
                time,"time",side_effect=lambda:now[0]), patch.object(time,"monotonic",side_effect=lambda:now[0]), patch.object(
                finance_gate,"_boot_identity",return_value="a"*64):
            first = get(summary,{"order_id":1})
            legacy = market_transport.Wire("ozon",creds,directory,time.monotonic()+20)
            self.assertEqual(legacy("POST",url,body={"order_id":1}),first["data"])
            self.assertEqual(get(summary,{"order_id":1})["fetched_at"],first["fetched_at"])
            with self.assertRaises(finance_gate.GateError) as paced:
                get(products,{"supply_id":7})
            self.assertEqual(paced.exception.retry_after_seconds,market_transport.OZON_ACT_INTERVAL)
            self.assertEqual(len(attempts),1)
            now[0] += market_transport.OZON_ACT_INTERVAL
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                get(products,{"supply_id":7})
            self.assertEqual(rejected.exception.retry_after_seconds,9)
            rejected.exception.close()
            now[0] += 8
            with self.assertRaises(finance_gate.GateError) as blocked:
                get(summary,{"order_id":2})
            self.assertEqual(blocked.exception.retry_after_seconds,1)
            self.assertEqual(len(attempts),2)
            now[0] += 1
            actual = get(products,{"supply_id":7})
            self.assertEqual(actual["data"],{"supply_acts":[]})
            self.assertEqual(len(attempts),3)

    def test_tls_is_prepared_by_the_real_opener_and_reused_across_requests(self):
        market_https._context.cache_clear()
        with patch('market_https.ssl.create_default_context',wraps=ssl.create_default_context) as create:
            create.assert_not_called()
            openers=[urllib.request.build_opener(market_https.VerifiedHTTPS) for _ in range(2)]
            first,second=(next(h for h in opener.handlers if isinstance(h,market_https.VerifiedHTTPS)) for opener in openers)
            self.assertEqual(create.call_count,1)
            self.assertIs(first._context,second._context)
            self.assertTrue(first._context.check_hostname)
            self.assertEqual(first._context.verify_mode,ssl.CERT_REQUIRED)
        market_https._context.cache_clear()
