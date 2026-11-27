import copy
import os
import tempfile
import unittest
from content_factory_cutover import Cutover, Pause, TARGETS, WORKFLOW, plan
from content_factory_fence import Fence
import content_factory_workflow_plan as migration


IDS={'openai':'OpenAIid123','gemini':'Geminiid123','reader':'Readerid123'}
TARGET_NAMES={'cf-t-openai':migration.TEXT,'cf-i-gemini':migration.IMAGE,
 'ce2011fe-2af6-467b-b0ec-4820e0a42893':'Gemini Omni — видео',
 '07ea2038-bef4-4503-a2d0-ea1e81f56144':'Google — статус видео',
 '6957e8ea-d133-465f-982d-04e359a62719':'Google — скачать видео',
 '0708700d-78a1-4f23-95bb-5e26cc41a5a3':'Gemini — проверить изображение',
 'c1d60114-4b59-45fa-b9a0-4399f105efb3':'Gemini — проверить видео',
 'b6a67b4b-0e06-40c8-ace1-9c65b3e282e6':'Gemini — перепроверить видео',
 '3e055872-37d8-475a-818d-8c692f31e93b':'Gemini — финальная проверка видео'}


def workflow():
    nodes=[]
    for ident,(kind,ctype,provider) in TARGETS.items():
        old='cH5bc3kc2uN9fzd8' if provider=='openai' else 'm9Mcn55vNwWxgQx6'
        node={'id':ident,'name':TARGET_NAMES[ident],'type':kind,'parameters':{},'credentials':{ctype:{'id':old,'name':'Old'}}}
        if ident in ('cf-t-openai','cf-i-gemini'): node['parameters']={'modelId':{'mode':'id','value':'old-model'}}
        nodes.append(node)
    nodes += [{'id':'trigger1','name':migration.TRIGGERS[0],'type':'n8n-nodes-base.manualTrigger'},
              {'id':'trigger2','name':migration.TRIGGERS[1],'type':'n8n-nodes-base.scheduleTrigger'},
              {'id':'product1','name':'Товар','type':'n8n-nodes-base.dataTable'}]
    return {'id':WORKFLOW,'name':'Factory','active':True,'versionId':'v1','activeVersionId':'v1','nodes':nodes,
            'connections':{n:{'main':[[{'node':'Товар','type':'main','index':0}]]} for n in migration.TRIGGERS},
            'settings':{},'staticData':None,'pinData':{},'meta':None,'nodeGroups':[]}


class Wire:
    def __init__(self): self.flow=workflow(); self.old=copy.deepcopy(self.flow); self.calls=[]; self.fail_save=False; self.open_runs=False
    def __call__(self,action,payload=None):
        self.calls.append(action)
        if action=='get': return copy.deepcopy(self.flow)
        if action=='get_version':
            if payload['versionId'] != 'v1': raise AssertionError('Unexpected version')
            return {'workflowId':WORKFLOW,'versionId':'v1',**{k:copy.deepcopy(self.old[k]) for k in ('nodes','connections','nodeGroups')}}
        if action=='list_open': return {'data':[{'id':'1'}] if self.open_runs else [],'nextCursor':None}
        if action=='unpublish':
            self.flow['active']=False; self.flow['activeVersionId']=None
            return {'active':False}
        if action=='save_draft':
            if self.fail_save: raise OSError('simulated error')
            self.flow.update(copy.deepcopy(payload)); self.flow['versionId']='v2'
            return {'versionId':'v2'}
        if action=='publish':
            self.flow['active']=True; self.flow['activeVersionId']=payload['versionId']
            return {'versionId':payload['versionId']}
        raise AssertionError('Unexpected operation')


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.fence=Fence(os.path.join(self.tmp.name,'fence.db'),b'x'*32)
        self.fence.arm(lambda:True); self.wire=Wire(); self.owner='owner-identity-example'
        self.cut=Cutover(self.fence,self.wire,IDS,lambda stage,ids: True,readiness_probe=lambda:True,quiescence_probe=lambda:True)
    def tearDown(self): self.tmp.cleanup()

    def pause(self):
        pause=Pause(self.fence,self.wire)
        p=pause.prepare(self.owner)
        result=pause.confirm(self.owner,p['proposal_id'])
        self.assertEqual(result['status'],'verified')
        return result['receipt']

    def test_model_reader_and_credential_migration_is_narrow(self):
        before=workflow(); p=plan(before,IDS); graph=p['body']
        self.assertEqual(graph['settings'],before['settings'])
        self.assertEqual(graph['nodeGroups'],before['nodeGroups'])
        nodes={n['name']:n for n in graph['nodes']}
        self.assertEqual(nodes[migration.FETCH]['parameters']['url'],'https://3dprinta.shop/api/content-factory/model-config')
        self.assertEqual(nodes[migration.FETCH]['credentials']['httpTemplatedCustomAuth']['id'],IDS['reader'])
        self.assertIn('models.text',nodes[migration.TEXT]['parameters']['modelId']['value'])
        self.assertIn('models.image',nodes[migration.IMAGE]['parameters']['modelId']['value'])
        self.assertEqual(len(graph['nodes']),len(before['nodes'])+2)
        self.assertEqual(before['versionId'],'v1')

    def test_draft_then_separate_publish_confirmation(self):
        receipt=self.pause()
        proposal=self.cut.prepare_draft(self.owner,receipt)
        self.assertNotIn('save_draft',self.wire.calls)
        result=self.cut.confirm_draft(self.owner,proposal['proposal_id'],receipt)
        self.assertEqual(result['status'],'verified')
        self.assertIsNone(self.wire.flow['activeVersionId'])
        p2=self.cut.prepare_publish(self.owner,result['receipt'])
        self.assertNotIn('publish',self.wire.calls)
        self.assertEqual(self.cut.confirm_publish(self.owner,p2,result['receipt']),'verified')
        self.assertEqual(self.wire.flow['activeVersionId'],'v2')

    def test_changed_snapshot_rejects_before_write(self):
        receipt=self.pause(); proposal=self.cut.prepare_draft(self.owner,receipt)
        self.wire.flow['settings']['timezone']='Europe/Moscow'
        with self.assertRaises(ValueError): self.cut.confirm_draft(self.owner,proposal['proposal_id'],receipt)
        self.assertNotIn('save_draft',self.wire.calls)

    def test_ambiguous_save_blocks_retries(self):
        receipt=self.pause(); p=self.cut.prepare_draft(self.owner,receipt)
        self.wire.fail_save=True
        self.assertEqual(self.cut.confirm_draft(self.owner,p['proposal_id'],receipt)['status'],'unknown')
        self.assertFalse(self.fence.is_ready())
        self.assertEqual(self.wire.calls.count('save_draft'),1)

    def test_unexpected_managed_node_and_tampered_receipt(self):
        flow=workflow()
        flow['nodes'].append({'id':'unreviewed','name':'extra','type':'n8n-nodes-base.httpRequest',
                              'credentials':{'googlePalmApi':{'id':'old','name':'Old'}}})
        with self.assertRaises(ValueError): plan(flow,IDS)
        paused=self.pause(); p=self.cut.prepare_draft(self.owner,paused); result=self.cut.confirm_draft(self.owner,p['proposal_id'],paused)
        receipt=dict(result['receipt'],draft_version='v3')
        with self.assertRaises(ValueError): self.cut.prepare_publish(self.owner,receipt)
        self.assertNotIn('publish',self.wire.calls)

    def test_cutover_requires_quiescence_and_verified_reader(self):
        disabled=Cutover(self.fence,self.wire,IDS,lambda stage,ids:True,quiescence_probe=lambda:False)
        paused=self.pause()
        with self.assertRaises(PermissionError): disabled.prepare_draft(self.owner,paused)
        p=self.cut.prepare_draft(self.owner,paused)
        result=self.cut.confirm_draft(self.owner,p['proposal_id'],paused)
        no_reader=Cutover(self.fence,self.wire,IDS,lambda stage,ids:True,quiescence_probe=lambda:True)
        with self.assertRaises(PermissionError): no_reader.prepare_publish(self.owner,result['receipt'])
        self.assertNotIn('publish',self.wire.calls)

    def test_runtime_fields_are_not_sent_or_overwritten(self):
        self.wire.flow['staticData']={'value':'before'}
        self.wire.flow['pinData']={'node':[{'json':{'sample':True}}]}
        self.wire.flow['meta']={'custom':'preserved'}
        self.wire.old=copy.deepcopy(self.wire.flow)
        paused=self.pause(); proposal=self.cut.prepare_draft(self.owner,paused)
        self.wire.flow['staticData']={'value':'concurrent-update'}
        result=self.cut.confirm_draft(self.owner,proposal['proposal_id'],paused)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(self.wire.flow['staticData'],{'value':'concurrent-update'})
        self.assertEqual(self.wire.flow['pinData'],{'node':[{'json':{'sample':True}}]})
        self.assertEqual(self.wire.flow['meta'],{'custom':'preserved'})

    def test_pause_is_separate_confirmed_action_and_records_previous_version(self):
        pause=Pause(self.fence,self.wire)
        proposal=pause.prepare(self.owner)
        self.assertNotIn('unpublish',self.wire.calls)
        result=pause.confirm(self.owner,proposal['proposal_id'])
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['receipt']['version'],'v1')
        self.assertTrue(result['receipt']['drained'])
        self.assertFalse(self.wire.flow['active'])

    def test_pause_refuses_ongoing_or_changed_workflow(self):
        pause=Pause(self.fence,self.wire)
        self.wire.open_runs=True
        with self.assertRaises(PermissionError): pause.prepare(self.owner)
        self.assertNotIn('unpublish',self.wire.calls)
        self.wire.open_runs=False
        p=pause.prepare(self.owner)
        self.wire.flow['settings']['timezone']='UTC'
        with self.assertRaises(PermissionError): pause.confirm(self.owner,p['proposal_id'])
        self.assertNotIn('unpublish',self.wire.calls)

    def test_paused_cutover_rechecks_drain_and_blocks_on_new_execution(self):
        receipt=self.pause()
        self.wire.open_runs=True
        with self.assertRaises(PermissionError): self.cut.prepare_draft(self.owner,receipt)
        self.assertNotIn('save_draft',self.wire.calls)
        self.wire.open_runs=False
        proposal=self.cut.prepare_draft(self.owner,receipt)
        original=self.wire.__call__
        def racing_wire(action,payload=None):
            result=original(action,payload)
            if action=='save_draft': self.wire.open_runs=True
            return result
        self.cut.wire=racing_wire
        result=self.cut.confirm_draft(self.owner,proposal['proposal_id'],receipt)
        self.assertEqual(result['status'],'unknown')
        self.assertFalse(self.fence.is_ready())


if __name__ == '__main__': unittest.main()
