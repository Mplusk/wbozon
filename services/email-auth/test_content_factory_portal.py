"""Owner-only access and secret handling for the content-factory tab."""
import unittest
from unittest.mock import patch

import test_portal


class ContentFactoryPortalTests(test_portal.Tests):
    def request_tab(self, **kwargs):
        headers = kwargs.pop('headers', {})
        return self.request('/settings', headers={**headers, 'QUERY_STRING': 'tab=content-factory'}, **kwargs)

    def test_budget_routes_fail_closed_and_require_owner_confirmation(self):
        prepare = '/settings/content-factory/budget/prepare'
        confirm = '/settings/content-factory/budget/confirm'
        fields = {'csrf': self.csrf, 'revision': '0', 'monthly_limit_usd': '12,34'}
        self.assertTrue(self.request(prepare, 'POST', fields)['status'].startswith('503'))
        class Gateway:
            def __init__(self): self.pending = {}; self.revision = 0
            def ready(self): return True
            def settings(self): return {'revision': self.revision, 'monthly_limit_cents': 1000, 'estimated_reserved_cents': None}
            def prepare(self, owner, revision, cents):
                if revision != self.revision: raise ValueError('stale')
                self.pending['p'*32] = (owner, cents)
                return {'proposal_id':'p'*32, 'before_cents':1000, 'after_cents':cents}
            def confirm(self, owner, proposal):
                item = self.pending.pop(proposal)
                if item[0] != owner: return False
                self.revision += 1
                return True
        gateway = Gateway(); self.app.budget_gateway = gateway
        self.assertTrue(self.request(prepare, 'POST', fields, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(prepare, 'POST', fields, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(prepare, 'POST', {**fields, 'csrf': 'bad'})['status'].startswith('403'))
        self.assertTrue(self.request(prepare, 'POST', {**fields, 'monthly_limit_usd': '1e9'})['status'].startswith('400'))
        result = self.request(prepare, 'POST', fields)
        self.assertIn('10.00 → 12.34 USD', result['body'])
        self.assertEqual(gateway.revision, 0)
        accepted = self.request(confirm, 'POST', {'csrf': self.csrf, 'proposal_id': 'p'*32})
        self.assertIn('не включается автоматически', accepted['body'])
        self.assertEqual(gateway.revision, 1)
        self.assertTrue(self.request(confirm, 'POST', {'csrf': self.csrf, 'proposal_id': 'p'*32})['status'].startswith('503'))
        self.assertTrue(self.request(prepare, 'POST', fields)['status'].startswith('400'))
        self.now += 301
        self.assertTrue(self.request(prepare, 'POST', fields)['status'].startswith('403'))

    def test_real_budget_draft_gateway_changes_local_draft_only(self):
        import content_factory_budget
        store = content_factory_budget.BudgetStore(self.vault.path)
        self.app.budget_gateway = content_factory_budget.BudgetDraftGateway(store, enabled=True)
        with patch('content_factory.snapshot', return_value={'status': 'unavailable'}):
            page = self.request_tab()
        self.assertIn('name="monthly_limit_usd"', page['body'])
        result = self.request('/settings/content-factory/budget/prepare', 'POST',
                              {'csrf': self.csrf, 'revision': '0', 'monthly_limit_usd': '10.00'})
        self.assertIn('0.00 → 10.00 USD', result['body'])
        import re
        match = re.search(r'name="proposal_id" value="([a-f0-9]{32})"', result['body'])
        self.assertIsNotNone(match)
        result = self.request('/settings/content-factory/budget/confirm', 'POST',
                              {'csrf': self.csrf, 'proposal_id': match.group(1)})
        self.assertIn('Бюджет кабинета сохранён', result['body'])
        settings = store.settings()
        self.assertEqual(settings['monthly_limit_cents'], 1000)
        self.assertIs(settings['enabled'], False)
        self.assertIs(settings['can_run'], False)

    def test_write_token_setup_is_opt_in_owner_only_and_never_echoes(self):
        path = '/settings/content-factory/ai/write-token'
        secret = 'synthetic-control-token-12345'
        fields = {'csrf':self.csrf, 'api_key':secret}
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('503'))
        self.assertNotIn('n8n_content_factory_write', self.vault.present())
        self.app.write_token_setup = True
        self.assertTrue(self.request(path, 'POST', fields, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path, 'POST', fields, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path, 'POST', {**fields, 'csrf':'bad'})['status'].startswith('403'))
        response = self.request(path, 'POST', fields)
        self.assertTrue(response['status'].startswith('303'))
        self.assertNotIn(secret, response['body'])
        self.assertEqual(self.vault.read('n8n_content_factory_write'), {'api_key':secret})
        with patch('content_factory.snapshot', return_value={'status':'unavailable'}):
            page = self.request_tab()
        self.assertNotIn(secret, page['body'])
        self.assertIn('могут распространяться на другие', page['body'])
        self.now += 301
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('403'))

    def test_gateway_composition_is_disabled_without_explicit_complete_config(self):
        import os
        import portal
        base = {'DATABASE_PATH':self.login.db, 'VAULT_PATH':self.vault.path}
        def fake_secret(name, binary=False):
            return (b'k'*32 if binary else 'f'*64)
        with patch.dict(os.environ, base, clear=True), patch.object(portal, 'LoginApp', return_value=self.login), \
                patch.object(portal, 'Vault', return_value=self.vault), patch.object(portal, 'secret_file', side_effect=fake_secret):
            app = portal.create_portal()
            self.assertIsNone(app.ai_gateway)
            self.assertFalse(app.write_token_setup)
            os.environ['CF_GATEWAY_ENABLED'] = '1'
            with self.assertRaises(ValueError): portal.create_portal()

    def test_gateway_arm_and_halt_require_authenticated_owner(self):
        class Fence:
            def __init__(self): self.actions=[]
            def arm(self, authorised):
                if not authorised(): raise PermissionError()
                self.actions.append('arm')
            def halt(self): self.actions.append('halt')
        class Gateway:
            def __init__(self): self.fence=Fence()
            def token_reader(self): return 't'*32
        self.app.ai_gateway = Gateway()
        self.app.write_token_setup = True
        path='/settings/content-factory/ai/gateway'
        fields={'csrf':self.csrf, 'action':'arm'}
        self.assertTrue(self.request(path,'POST',fields,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path,'POST',fields,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',{**fields,'csrf':'bad'})['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',fields)['status'].startswith('303'))
        self.now += 301
        self.assertTrue(self.request(path,'POST',fields)['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',{**fields,'action':'halt'})['status'].startswith('303'))
        self.assertEqual(self.app.ai_gateway.fence.actions,['arm','halt'])

    def test_gateway_arm_supports_budget_only_configuration(self):
        class Fence:
            def __init__(self): self.actions=[]
            def arm(self, authorised):
                if not authorised(): raise PermissionError()
                self.actions.append('arm')
            def halt(self): self.actions.append('halt')
        class Gateway:
            def __init__(self): self.fence=Fence()
            def token_reader(self): return 't'*32
        self.app.ai_gateway = None
        self.app.budget_gateway = Gateway()
        self.app.write_token_setup = True
        path='/settings/content-factory/ai/gateway'
        for action in ('arm','halt'):
            self.assertTrue(self.request(path,'POST',{'csrf':self.csrf,'action':action})['status'].startswith('303'))
        self.assertEqual(self.app.budget_gateway.fence.actions,['arm','halt'])

    def test_create_portal_opt_in_composes_stopped_gateway(self):
        import os
        import portal
        directory=os.path.join(self.tmp.name,'gateway')
        os.mkdir(directory)
        env={'DATABASE_PATH':self.login.db, 'VAULT_PATH':self.vault.path, 'CF_GATEWAY_ENABLED':'1',
             'CF_GATEWAY_STAGING_VERIFIED':'1', 'CF_GATEWAY_OWNER_SCOPE_CONFIRMED':'1',
             'CF_GATEWAY_DATA_DIR':directory, 'CF_GATEWAY_HMAC_KEY_FILE':os.path.join(self.tmp.name,'hmac'),
             'VAULT_KEY_FILE':os.path.join(self.tmp.name,'vault-key'),
             'CF_N8N_OPENAI_ID':'OpenID123', 'CF_N8N_GEMINI_ID':'GemID1234'}
        def fake_secret(name, binary=False): return (b'v'*32 if name == 'VAULT_KEY_FILE' else b'k'*32) if binary else 'f'*64
        with patch.dict(os.environ,env,clear=True), patch.object(portal,'LoginApp',return_value=self.login), \
                patch.object(portal,'Vault',return_value=self.vault), patch.object(portal,'secret_file',side_effect=fake_secret):
            app=portal.create_portal()
            self.assertTrue(app.write_token_setup)
            self.assertIsNotNone(app.ai_gateway)
            self.assertFalse(app.ai_gateway_available())
            self.assertTrue(callable(app.ai_gateway_inspector))
            os.environ['CF_MODEL_CONFIG_ENABLED']='1'
            with self.assertRaises(ValueError): portal.create_portal()
            os.environ['CF_MODEL_CONFIG_STAGING_VERIFIED']='1'
            os.environ['CF_N8N_MODEL_READER_ID']='ReaderID123'
            app=portal.create_portal()
            self.assertTrue(app.model_config_enabled)
            self.assertEqual(app.ai_gateway.model_reader_id,'ReaderID123')

    def test_provision_and_reconciliation_require_owner_two_steps(self):
        class Fence:
            def __init__(self): self.calls=[]
            def halt(self): self.calls.append('halt')
            def inspect_unknown(self, proposal, owner, authorised, reader):
                if not authorised(): raise PermissionError()
                self.calls.append(('inspect',proposal,owner))
                return 'credential_exists_unverified'
            def confirm_unknown(self, proposal, owner, authorised, reader, *, risk_confirmed):
                if not authorised() or not risk_confirmed: raise PermissionError()
                self.calls.append(('confirm',proposal,owner))
        class Gateway:
            def __init__(self): self.fence=Fence(); self.calls=[]
            def ready(self): return True
            def prepare_provision(self, owner, provider):
                self.calls.append(('prepare',owner,provider)); return 'p'*32
            def commit_provision(self, owner, proposal, provider, key):
                self.calls.append(('commit',owner,provider,proposal)); return 'unknown'
        self.app.ai_gateway=Gateway()
        self.app.ai_gateway_inspector=lambda *_: None
        self.app.write_token_setup=True
        prep='/settings/content-factory/ai/credentials/provision/prepare'
        commit='/settings/content-factory/ai/credentials/provision/commit'
        fields={'csrf':self.csrf,'provider':'openai'}
        self.assertTrue(self.request(prep,'POST',fields,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(prep,'POST',fields,origin=False)['status'].startswith('403'))
        result=self.request(prep,'POST',fields)
        self.assertTrue(result['status'].startswith('200'))
        self.assertIn('provision/commit',result['body'])
        self.assertEqual(self.app.ai_gateway.calls[0][0],'prepare')
        secret='synthetic-ai-key-123456789'
        result=self.request(commit,'POST',{**fields,'proposal_id':'p'*32,'api_key':secret})
        self.assertTrue(result['status'].startswith('200'))
        self.assertNotIn(secret,result['body'])
        self.assertIn('reconcile/inspect',result['body'])
        inspect='/settings/content-factory/ai/reconcile/inspect'
        confirm='/settings/content-factory/ai/reconcile/confirm'
        data={'csrf':self.csrf,'proposal_id':'p'*32}
        result=self.request(inspect,'POST',data)
        self.assertTrue(result['status'].startswith('200'))
        self.assertIn('значение ключа',result['body'])
        self.assertTrue(self.request(confirm,'POST',data)['status'].startswith('400'))
        self.assertTrue(self.request(confirm,'POST',{**data,'risk':'understood'})['status'].startswith('200'))
        self.assertEqual([x if isinstance(x,str) else x[0] for x in self.app.ai_gateway.fence.calls],['halt','inspect','confirm'])

    def test_create_portal_composes_budget_without_ai_gateway(self):
        import os
        import portal
        directory=os.path.join(self.tmp.name,'budget-gateway')
        os.mkdir(directory)
        env={'DATABASE_PATH':self.login.db, 'VAULT_PATH':self.vault.path, 'CF_BUDGET_GATEWAY_ENABLED':'1',
             'CF_BUDGET_STAGING_VERIFIED':'1', 'CF_BUDGET_SCOPE_CONFIRMED':'1',
             'CF_GATEWAY_DATA_DIR':directory, 'CF_GATEWAY_HMAC_KEY_FILE':os.path.join(self.tmp.name,'hmac'),
             'VAULT_KEY_FILE':os.path.join(self.tmp.name,'vault-key')}
        def fake_secret(name, binary=False): return (b'v'*32 if name == 'VAULT_KEY_FILE' else b'k'*32) if binary else 'f'*64
        with patch.dict(os.environ,env,clear=True), patch.object(portal,'LoginApp',return_value=self.login), \
                patch.object(portal,'Vault',return_value=self.vault), patch.object(portal,'secret_file',side_effect=fake_secret):
            app=portal.create_portal()
            self.assertIsNone(app.ai_gateway)
            self.assertIsNotNone(app.budget_gateway)
            self.assertTrue(app.write_token_setup)
            self.assertFalse(app.budget_gateway_available())

    def test_create_portal_composes_ai_and_budget_with_shared_fence(self):
        import os
        import portal
        directory=os.path.join(self.tmp.name,'shared-gateway')
        os.mkdir(directory)
        env={'DATABASE_PATH':self.login.db, 'VAULT_PATH':self.vault.path, 'CF_GATEWAY_ENABLED':'1',
             'CF_GATEWAY_STAGING_VERIFIED':'1', 'CF_GATEWAY_OWNER_SCOPE_CONFIRMED':'1',
             'CF_BUDGET_GATEWAY_ENABLED':'1', 'CF_BUDGET_STAGING_VERIFIED':'1', 'CF_BUDGET_SCOPE_CONFIRMED':'1',
             'CF_GATEWAY_DATA_DIR':directory, 'CF_GATEWAY_HMAC_KEY_FILE':os.path.join(self.tmp.name,'hmac'),
             'VAULT_KEY_FILE':os.path.join(self.tmp.name,'vault-key'),
             'CF_N8N_OPENAI_ID':'OpenID123', 'CF_N8N_GEMINI_ID':'GemID1234'}
        def fake_secret(name, binary=False): return (b'v'*32 if name == 'VAULT_KEY_FILE' else b'k'*32) if binary else 'f'*64
        with patch.dict(os.environ,env,clear=True), patch.object(portal,'LoginApp',return_value=self.login), \
                patch.object(portal,'Vault',return_value=self.vault), patch.object(portal,'secret_file',side_effect=fake_secret):
            first=portal.create_portal()
            self.assertEqual(first.ai_gateway.fence.path,first.budget_gateway.fence.path)
            self.assertEqual(first.ai_gateway.fence.key,first.budget_gateway.fence.key)
            restarted=portal.create_portal()
            self.assertFalse(restarted.ai_gateway_available())
            self.assertFalse(restarted.budget_gateway_available())

    def test_model_config_endpoint_requires_separate_bearer_and_is_opt_in(self):
        import content_factory_model_config
        self.vault.save('n8n',{'api_key':'different-token-123456789'})
        content_factory_model_config.create_reader_token(self.vault)
        token=self.vault.read(content_factory_model_config.READER_SLOT)['token']
        content_factory_model_config.compare_and_save(self.vault,0,{'text':'gpt-5.4-mini','image':'models/gemini-3.1-flash-image'})
        path='/api/content-factory/model-config'
        self.assertTrue(self.request(path,'GET',cookie=False,headers={'HTTP_AUTHORIZATION':'Bearer '+token})['status'].startswith('404'))
        self.app.model_config_enabled=True
        self.assertTrue(self.request(path,'GET',cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path,'GET',cookie=False,headers={'HTTP_AUTHORIZATION':'Bearer wrong'})['status'].startswith('401'))
        result=self.request(path,'GET',cookie=False,headers={'HTTP_AUTHORIZATION':'Bearer '+token})
        self.assertTrue(result['status'].startswith('200'))
        self.assertIn('gpt-5.4-mini',result['body'])
        self.assertNotIn(token,result['body'])
        self.assertTrue(self.request(path,'GET',cookie=False,headers={'HTTP_AUTHORIZATION':'Bearer '+token, 'QUERY_STRING':'x=1'})['status'].startswith('400'))

    def test_reader_token_setup_requires_owner_fresh_session_and_never_renders_token(self):
        self.app.model_config_enabled=True
        path='/settings/content-factory/ai/reader-token'
        data={'csrf':self.csrf}
        self.assertTrue(self.request(path,'POST',data,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path,'POST',data,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',{**data,'csrf':'bad'})['status'].startswith('403'))
        result=self.request(path,'POST',data)
        self.assertTrue(result['status'].startswith('303'))
        token=self.vault.read('content_factory_model_reader')['token']
        self.assertNotIn(token,result['body'])
        self.now+=301
        self.assertTrue(self.request(path,'POST',data)['status'].startswith('403'))

    def test_model_settings_two_phase_and_revision_bound(self):
        import os
        import re
        from content_factory_fence import Fence
        fence=Fence(os.path.join(self.tmp.name,'model-fence.db'),b'x'*32)
        fence.arm(lambda: True)
        class TestGateway:
            def __init__(self): self.fence=fence
            def ready(self): return self.fence.is_ready()
        self.app.model_config_enabled=True
        self.app.ai_gateway=TestGateway()
        prepare='/settings/content-factory/ai/models/prepare'
        confirm='/settings/content-factory/ai/models/confirm'
        fields={'csrf':self.csrf,'text_model':'gpt-5.4-mini','image_model':'models/gemini-3.1-flash-image','ack_estimate':'understood'}
        self.assertTrue(self.request(prepare,'POST',fields,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(prepare,'POST',fields,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(prepare,'POST',{k:v for k,v in fields.items() if k != 'ack_estimate'})['status'].startswith('400'))
        result=self.request(prepare,'POST',fields)
        self.assertTrue(result['status'].startswith('200'))
        proposal=re.search(r'name="proposal_id" value="([A-Za-z0-9_-]+)"',result['body']).group(1)
        body={**fields,'proposal_id':proposal,'revision':'0'}
        self.assertTrue(self.request(confirm,'POST',{**body,'image_model':'models/other'})['status'].startswith('400'))
        result=self.request(confirm,'POST',body)
        self.assertTrue(result['status'].startswith('200'))
        self.assertIn('отдельного подключения',result['body'])
        import content_factory_model_config
        self.assertEqual(content_factory_model_config.read(self.vault)['revision'],1)

    def test_reader_credential_requires_explicit_two_phase_owner_action(self):
        import content_factory_model_config
        class Gateway:
            def ready(self): return True
        class FakeProvision:
            def __init__(self, vault, gateway): self.calls=invocations
            def prepare(self, owner): self.calls.append('prepare'); return 'p'*32
            def confirm(self, owner, proposal): self.calls.append('confirm'); return 'accepted_unconfirmed'
        invocations=[]
        self.app.model_config_enabled=True
        self.app.ai_gateway=Gateway()
        prepare='/settings/content-factory/ai/reader-credential/prepare'
        confirm='/settings/content-factory/ai/reader-credential/confirm'
        data={'csrf':self.csrf}
        with patch.object(content_factory_model_config,'ReaderProvision',FakeProvision):
            self.assertTrue(self.request(prepare,'POST',data,cookie=False)['status'].startswith('401'))
            self.assertTrue(self.request(prepare,'POST',data,origin=False)['status'].startswith('403'))
            result=self.request(prepare,'POST',data)
            self.assertTrue(result['status'].startswith('200'))
            self.assertEqual(invocations,['prepare'])
            self.assertIn('Подтвердите передачу',result['body'])
            result=self.request(confirm,'POST',{**data,'proposal_id':'p'*32})
            self.assertTrue(result['status'].startswith('200'))
            self.assertIn('не проверены',result['body'])
            self.assertEqual(invocations,['prepare','confirm'])

    def test_cutover_requires_separate_draft_and_publish_confirmations(self):
        import html
        import re
        import content_factory_model_config as models
        models.create_reader_token(self.vault)
        models.compare_and_save(self.vault,0,{'text':'gpt-5.4-mini','image':'models/gemini-3.1-flash-image'})
        class Gateway:
            def ready(self): return True
        class Cutover:
            def __init__(self): self.calls=[]
            def prepare_draft(self,owner):
                self.calls.append('preview-draft'); return {'proposal_id':'p'*32,'version':'v1','changes':[]}
            def confirm_draft(self,owner,proposal):
                self.calls.append('save-draft'); return {'status':'verified','receipt':{'draft_version':'v2'}}
            def prepare_publish(self,owner,receipt): self.calls.append('preview-publish'); return 'q'*32
            def confirm_publish(self,owner,proposal,receipt): self.calls.append('publish'); return 'verified'
        class Jobs:
            def status(self,owner,ident): return {'state':'completed','result':self.result}
        self.app.ai_gateway=Gateway(); self.app.ai_cutover=Cutover(); self.app.ai_cutover_jobs=Jobs(); self.app.model_config_enabled=True
        base='/settings/content-factory/ai/cutover/'
        def fake_start(store,owner,operation,payload):
            cut=self.app.ai_cutover
            if operation=='pause_prepare': store.result={'kind':'pause_preview','proposal_id':'s'*32,'version':'v1'}
            elif operation=='pause_confirm': store.result={'kind':'pause_result','status':'verified','receipt':{'version':'v1'}}
            elif operation=='draft_prepare': store.result={'kind':'draft_preview','pause_receipt':payload['pause_receipt'],**cut.prepare_draft(owner)}
            elif operation=='draft_confirm': store.result={'kind':'draft_result',**cut.confirm_draft(owner,payload['proposal_id'])}
            elif operation=='publish_prepare': store.result={'kind':'publish_preview','proposal_id':cut.prepare_publish(owner,payload['receipt']),'receipt':payload['receipt']}
            elif operation=='publish_confirm': store.result={'kind':'publish_result','status':cut.confirm_publish(owner,payload['proposal_id'],payload['receipt'])}
            return 'j'*32
        def poll(): return self.request(base+'job',headers={'QUERY_STRING':'job='+'j'*32})
        with patch('content_factory_cutover_jobs.start',side_effect=fake_start):
            self.assertTrue(self.request(base+'pause/prepare','POST',{'csrf':self.csrf},cookie=False)['status'].startswith('401'))
            self.assertTrue(self.request(base+'pause/prepare','POST',{'csrf':self.csrf},origin=False)['status'].startswith('403'))
            first=self.request(base+'pause/prepare','POST',{'csrf':self.csrf})
            self.assertTrue(first['status'].startswith('200'))
            self.assertIn('Проверка запущена',first['body'])
            self.assertIn('остановку',poll()['body'])
            self.request(base+'pause/confirm','POST',{'csrf':self.csrf,'proposal_id':'s'*32,'ack_pause':'understood'})
            paused=html.unescape(re.search(r'name="pause_receipt" value="([^"]+)"',poll()['body']).group(1))
            self.request(base+'draft/prepare','POST',{'csrf':self.csrf,'pause_receipt':paused})
            self.assertIn('Проверка изменений',poll()['body'])
            self.assertEqual(self.app.ai_cutover.calls,['preview-draft'])
            self.request(base+'draft/confirm','POST',{'csrf':self.csrf,'proposal_id':'p'*32,'pause_receipt':paused})
            receipt=html.unescape(re.search(r'name="receipt" value="([^"]+)"',poll()['body']).group(1))
            self.request(base+'publish/prepare','POST',{'csrf':self.csrf,'receipt':receipt})
            self.assertIn('Подтвердите публикацию',poll()['body'])
            self.assertEqual(self.app.ai_cutover.calls,['preview-draft','save-draft','preview-publish'])
            body={'csrf':self.csrf,'proposal_id':'q'*32,'receipt':receipt}
            self.assertTrue(self.request(base+'publish/confirm','POST',body)['status'].startswith('400'))
            self.assertTrue(self.request(base+'publish/confirm','POST',{**body,'ack_cost':'understood'})['status'].startswith('200'))
            self.assertEqual(self.app.ai_cutover.calls[-1],'publish')

    def test_tab_requires_login_and_is_read_only(self):
        self.assertTrue(self.request_tab(cookie=False)['status'].startswith('401'))
        page = self.request_tab()
        self.assertTrue(page['status'].startswith('200'))
        self.assertIn('Контент-фабрика', page['body'])
        self.assertIn('action="/settings/content-factory/connect"', page['body'])
        self.assertTrue(self.request_tab(method='POST')['status'].startswith('405'))

    def test_bridge_key_is_encrypted_and_never_echoed(self):
        secret = 'synthetic-n8n-key-for-tests'
        fields = {'csrf': self.csrf, 'api_key': secret}
        bad_origin = self.request('/settings/content-factory/connect', 'POST', fields, origin=False)
        self.assertTrue(bad_origin['status'].startswith('403'))
        bad_csrf = self.request('/settings/content-factory/connect', 'POST', {**fields, 'csrf': 'bad'})
        self.assertTrue(bad_csrf['status'].startswith('403'))
        result = self.request('/settings/content-factory/connect', 'POST', fields)
        self.assertTrue(result['status'].startswith('303'))
        self.assertEqual(self.vault.read('n8n')['api_key'], secret)
        with open(self.vault.path, 'rb') as db:
            self.assertNotIn(secret.encode(), db.read())
        with patch('content_factory.snapshot', return_value={'status': 'unavailable'}):
            self.assertNotIn(secret, self.request_tab()['body'])

    def test_saving_key_requires_recent_login_and_valid_input(self):
        self.now += 301
        result = self.request('/settings/content-factory/connect', 'POST', {'csrf': self.csrf, 'api_key': 'x' * 30})
        self.assertTrue(result['status'].startswith('403'))
        self.assertNotIn('n8n', self.vault.present())

    def test_ai_models_are_saved_only_as_local_draft(self):
        path = '/settings/content-factory/models'
        fields = {'csrf': self.csrf, 'text_model': 'gpt-5.4-mini',
                  'image_model': 'models/gemini-3.1-flash-image', 'video_model': ''}
        self.assertTrue(self.request(path, 'POST', fields, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path, 'POST', fields, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path, 'POST', {**fields, 'csrf': 'bad'})['status'].startswith('403'))
        self.assertTrue(self.request(path, 'POST', {**fields, 'text_model': '<script>'})['status'].startswith('400'))
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('303'))
        self.assertEqual(self.vault.read('content_factory_ai_models')['text_model'], 'gpt-5.4-mini')
        self.assertNotIn('n8n', self.vault.present())
        self.assertTrue(self.request(path, 'POST', {**fields, 'text_model': 'o4-mini'})['status'].startswith('303'))
        self.now += 301
        self.assertTrue(self.request(path, 'POST', fields)['status'].startswith('403'))

    def test_model_identifier_rejects_key_like_values_and_legacy_values_do_not_render(self):
        path = '/settings/content-factory/models'
        fields = {'csrf': self.csrf, 'text_model': 'gpt-model',
                  'image_model': 'models/gemini-image', 'video_model': ''}
        for value in ('sk-testfake123456789', 'AIzaTestFake123456789',
                      'models/aIZaTestFake123456789', 'models/SK-testfake123456789'):
            for field in ('text_model', 'image_model', 'video_model'):
                response = self.request(path, 'POST', {**fields, field: value})
                self.assertTrue(response['status'].startswith('400'))
                self.assertNotIn(value, response['body'])
            self.assertNotIn('content_factory_ai_models', self.vault.present())
        self.vault.save('content_factory_ai_models', {'text_model': 'sk-testfake123456789',
                         'image_model': 'models/gemini-image', 'video_model': ''})
        with patch('content_factory.snapshot', return_value={'status': 'unavailable'}):
            result = self.request_tab()
        self.assertFalse(result['status'].startswith('200'))
        self.assertNotIn('sk-testfake123456789', result['body'])

    def test_ai_credentials_fail_closed_before_reading_body_without_gateway(self):
        class NoBodyRead:
            def read(self, *_): raise AssertionError('Secret body should not be read')
        path = '/settings/content-factory/ai/credentials/commit'
        result = self.request(path, 'POST', {'csrf': self.csrf}, headers={'wsgi.input': NoBodyRead()})
        self.assertTrue(result['status'].startswith('503'))

    def test_ai_credentials_require_owner_csrf_and_show_verified_only_on_real_result(self):
        class Gateway:
            def __init__(self): self.calls = []
            def ready(self): return True
            def prepare_credential(self, owner, provider):
                self.calls.append(('prepare', owner, provider))
                return 'p' * 32
            def commit_credential(self, owner, proposal, provider, key):
                self.calls.append(('commit', owner, proposal, provider))
                if key.endswith('unknown'): raise OSError('private error')
                return 'verified'
        gateway = Gateway(); self.app.ai_gateway = gateway
        prepare = '/settings/content-factory/ai/credentials/prepare'
        commit = '/settings/content-factory/ai/credentials/commit'
        fields = {'csrf': self.csrf, 'provider': 'openai'}
        self.assertTrue(self.request(prepare, 'POST', fields, cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(prepare, 'POST', fields, origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(prepare, 'POST', {**fields, 'csrf': 'bad'})['status'].startswith('403'))
        prepared = self.request(prepare, 'POST', fields)
        self.assertIn('name="api_key"', prepared['body'])
        self.assertIn('p' * 32, prepared['body'])
        secret = 'synthetic-key-for-test-only'
        data = {**fields, 'proposal_id': 'p' * 32, 'api_key': secret}
        result = self.request(commit, 'POST', data)
        self.assertIn('состояние подтверждено проверкой n8n', result['body'])
        self.assertNotIn(secret, result['body'])
        result = self.request(commit, 'POST', {**data, 'api_key': secret + '-unknown'})
        self.assertIn('Результат изменения неизвестен', result['body'])
        self.assertNotIn('private error', result['body'])
        self.assertNotIn(secret, result['body'])
        self.now += 301
        self.assertTrue(self.request(prepare, 'POST', fields)['status'].startswith('403'))


if __name__ == '__main__':
    unittest.main()
