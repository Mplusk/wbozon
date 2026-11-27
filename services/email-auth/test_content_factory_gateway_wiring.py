import unittest
import os
import tempfile
from content_factory_gateway_wiring import verifier, inspection_reader, token_reader, build, FACTORY


class GatewayWiringTests(unittest.TestCase):
    def setUp(self):
        self.ident = 'factoryid1234'
        self.credential = {'id':self.ident, 'type':'openAiApi', 'name':'SIDZEN CF OpenAI', 'updatedAt':'v1',
                           'isGlobal':False, 'isManaged':False, 'isResolvable':False, 'resolverId':None}
        self.flow = {'id': FACTORY, 'active': True, 'activeVersionId':'v1', 'versionId':'v1',
                     'nodes':[{'credentials': {'openAiApi': {'id':self.ident}}}]}
        self.pages = {'workflows?limit=100': {'data':[{'id': FACTORY}], 'nextCursor':None},
                      'workflows/'+FACTORY:self.flow, 'credentials/'+self.ident:self.credential,
                      'credentials/'+self.ident+'/test':{'status':'OK', 'message':'private details'}}
        self.calls=[]
        def read(method, path, timeout):
            self.calls.append((method, path))
            return self.pages[path]
        self.read=read

    def test_factory_only_passes_and_another_workflow_fails(self):
        self.assertFalse(verifier(self.read)('openai',self.ident))
        check=verifier(self.read, inventory_complete=True)
        self.assertTrue(check('openai',self.ident))
        self.pages['workflows?limit=100']['data'].append({'id':'other'})
        self.pages['workflows/other']={'id':'other','active':False,'nodes':self.flow['nodes']}
        self.assertFalse(check('openai',self.ident))

    def test_hidden_active_version_fails_closed(self):
        self.flow['versionId']='draft'
        self.assertFalse(verifier(self.read, inventory_complete=True)('openai',self.ident))

    def test_incomplete_pagination_and_wrong_metadata_fail(self):
        self.pages['workflows?limit=100'].pop('nextCursor')
        self.assertFalse(verifier(self.read, inventory_complete=True)('openai',self.ident))
        self.credential['type']='another'
        self.assertFalse(verifier(self.read, inventory_complete=True)('openai',self.ident))

    def test_inspection_does_not_return_upstream_message_or_secret(self):
        with self.assertRaises(RuntimeError): inspection_reader(self.read, {'openai':self.ident})('credential','factory-openai')
        observed=inspection_reader(self.read, {'openai':self.ident}, bounded_supervisor=True)('credential','factory-openai')
        self.assertEqual(observed['observation'],'credential_exists_unverified')
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))
        self.assertEqual(len(observed['evidence']),64)
        self.assertNotIn('private',str(observed))

    def test_write_token_uses_distinct_vault_slot_and_never_echoes(self):
        class Vault:
            def read(self, name):
                if name == 'n8n': return None
                if name != 'n8n_content_factory_write': raise AssertionError()
                return {'api_key':'x'*32}
        self.assertEqual(token_reader(Vault())(),'x'*32)
        class Bad:
            def read(self, _): raise ValueError('secret-must-not-escape')
        with self.assertRaisesRegex(RuntimeError,'Gateway token unavailable') as err:
            token_reader(Bad())()
        self.assertNotIn('secret-must-not-escape',str(err.exception))

    def test_composition_requires_owner_attestation_and_readiness_is_local(self):
        class Vault:
            def read(self, name): return None if name == 'n8n' else {'api_key':'t'*32}
        seen=[]
        def supervisor(token, mode, provider, ident):
            seen.append((mode,provider,ident))
            return True
        with tempfile.TemporaryDirectory() as tmp:
            gateway, _ = build(Vault(), db_path=os.path.join(tmp,'one.db'), hmac_key=b'x'*32,
                credential_ids={'openai':'OpenID123', 'gemini':'GemID1234'}, supervisor=supervisor)
            gateway.fence.arm(lambda: True)
            self.assertFalse(gateway.ready())
            self.assertEqual(seen, [])
            with self.assertRaises(PermissionError): gateway.prepare_credential('owner-identifier-example','openai')
            self.assertEqual(seen, [])
            allowed, _ = build(Vault(), db_path=os.path.join(tmp,'two.db'), hmac_key=b'x'*32,
                credential_ids={'openai':'OpenID123', 'gemini':'GemID1234'}, supervisor=supervisor, owner_scope_confirmed=True)
            allowed.fence.arm(lambda: True)
            allowed.prepare_credential('owner-identifier-example','openai')
            self.assertEqual(seen, [('scan','openai','OpenID123')])


if __name__ == '__main__': unittest.main()
