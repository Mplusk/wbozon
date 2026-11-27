import copy
import datetime
import unittest
from content_factory_reader_probe import verify,URL,READER_NAME


class ReaderProbeTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.datetime(2026,1,1,tzinfo=datetime.timezone.utc).timestamp()
        self.workflow_id='probe123'; self.execution_id='42'; self.reader_id='ReaderID123'
        self.config={'revision':1,'models':{'text':'gpt-5.4-mini','image':'models/gemini-3.1-flash-image'}}
        trigger={'id':'t','name':'Manual','type':'n8n-nodes-base.manualTrigger','parameters':{}}
        node={'id':'h','name':'Reader','type':'n8n-nodes-base.httpRequest',
              'credentials':{'httpTemplatedCustomAuth':{'id':self.reader_id,'name':READER_NAME}},
              'parameters':{'method':'GET','url':URL,'authentication':'genericCredentialType',
                            'genericAuthType':'httpTemplatedCustomAuth',
                            'options':{'redirect':{'redirect':{'followRedirects':False}},'timeout':5000}}}
        self.flow={'id':self.workflow_id,'active':False,'versionId':'v1','nodes':[trigger,node],
                   'pinData':{},'connections':{'Manual':{'main':[[{'node':'Reader','type':'main','index':0}]]}}}
        self.meta={'id':self.reader_id,'name':READER_NAME,'type':'httpTemplatedCustomAuth',
                   'isGlobal':False,'isManaged':False,'isResolvable':False,'resolverId':None,'updatedAt':'2025-12-31T23:58:00Z'}
        self.execution={'id':self.execution_id,'workflowId':self.workflow_id,'workflowVersionId':'v1','status':'success',
                        'mode':'manual','startedAt':'2025-12-31T23:59:00Z','stoppedAt':'2025-12-31T23:59:01Z',
                        'workflowData':copy.deepcopy(self.flow),
                        'data':{'resultData':{'runData':{'Manual':[{'data':{'main':[[{'json':{}}]]}}],
                                 'Reader':[{'data':{'main':[[{'json':self.config}]]}}]}}}}

    def verify(self):
        return verify(self.flow,self.execution,self.meta,workflow_id=self.workflow_id,execution_id=self.execution_id,
                      reader_id=self.reader_id,expected=self.config,now=self.now)

    def test_success(self): self.assertTrue(self.verify())
    def test_refuses_foreign_url_and_pinned_data(self):
        self.flow['nodes'][1]['parameters']['url']='https://example.com/'
        self.assertFalse(self.verify())
        self.flow['nodes'][1]['parameters']['url']=URL
        self.execution['workflowData']['pinData']={'Reader':[{'json':self.config}]}
        self.assertFalse(self.verify())
    def test_refuses_stale_or_different_key(self):
        self.meta['updatedAt']='2025-12-31T23:59:30Z'
        self.assertFalse(self.verify())
        self.meta['updatedAt']='2025-12-31T23:58:00Z'; self.execution['startedAt']='2025-12-31T23:00:00Z'
        self.assertFalse(self.verify())
    def test_refuses_partial_execution_and_unexpected_graph(self):
        self.execution['data']['startData']={'destinationNode':'Reader'}
        self.assertFalse(self.verify())
        self.execution['data'].pop('startData'); self.execution['workflowData']['nodes'].append({'name':'Injected'})
        self.assertFalse(self.verify())
    def test_refuses_changed_configuration(self):
        self.config={'revision':2,'models':self.config['models']}
        self.assertFalse(self.verify())


if __name__ == '__main__': unittest.main()
