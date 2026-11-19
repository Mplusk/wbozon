import io
import json
import secrets
import ssl
import unittest
import urllib.request
from email.message import Message
from unittest.mock import patch
import time

import finance_gate
import market_actions as actions
import market_catalog as catalog
import market_https
import test_external_mcp_integration
import test_portal


class CatalogTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    mcp=test_external_mcp_integration.ExternalMCPIntegrationTests.mcp

    def key(self,access):
        return self.app.external_keys.create(access,secrets.token_urlsafe(16),self.now,access)

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
