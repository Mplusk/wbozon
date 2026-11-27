"""Narrow, owner-confirmed factory credential cutover; not wired by default.

Only the exact hard-coded workflow and node IDs may change. Saving a draft and
publishing it are separately approved operations. Public n8n API has no CAS:
read-check-write is not atomic against a concurrent editor. The owner must
avoid editing this workflow and credentials during cutover. Real n8n staging,
scope provisioning and independent review remain mandatory before use.
"""
import copy
import hashlib
import hmac
import json
import multiprocessing
import re
import urllib.request
import urllib.parse
import content_factory_workflow_plan as migration

WORKFLOW = 'zWAbNcKhgzOX2PZq'
TARGETS = {
    'cf-t-openai': ('@n8n/n8n-nodes-langchain.openAi','openAiApi','openai'),
    'cf-i-gemini': ('@n8n/n8n-nodes-langchain.googleGemini','googlePalmApi','gemini'),
    'ce2011fe-2af6-467b-b0ec-4820e0a42893': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    '07ea2038-bef4-4503-a2d0-ea1e81f56144': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    '6957e8ea-d133-465f-982d-04e359a62719': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    '0708700d-78a1-4f23-95bb-5e26cc41a5a3': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    'c1d60114-4b59-45fa-b9a0-4399f105efb3': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    'b6a67b4b-0e06-40c8-ace1-9c65b3e282e6': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
    '3e055872-37d8-475a-818d-8c692f31e93b': ('n8n-nodes-base.httpRequest','googlePalmApi','gemini'),
}
NAMES = {'openai':'SIDZEN CF OpenAI', 'gemini':'SIDZEN CF Gemini', 'reader':'SIDZEN CF Model Config Reader'}
# Even omitted runtime fields can be lost during concurrent update in n8n
# 2.49.6. Never send them from an older snapshot, and require a real barrier.
WRITE_FIELDS = ('name','nodes','connections','settings','nodeGroups')
BASE = 'https://n8n.gamelife.shop/api/v1/workflows/' + WORKFLOW
OPEN_STATUSES = ('new','running','waiting','unknown')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def _request_once(token, action, payload):
    if not isinstance(token,str) or not re.fullmatch(r'[!-~]{16,2048}',token): raise ValueError()
    if action == 'get' and payload is None:
        method,url,data='GET',BASE,None
    elif (action == 'get_version' and isinstance(payload,dict) and set(payload) == {'versionId'}
          and isinstance(payload['versionId'],str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}',payload['versionId'])):
        method,url,data='GET',BASE+'/versions/'+payload['versionId'],None
    elif action == 'save_draft' and isinstance(payload,dict) and set(payload) == set(WRITE_FIELDS):
        method,url,data='PUT',BASE+'?publishIfActive=false',payload
    elif (action == 'publish' and isinstance(payload,dict) and set(payload) == {'versionId'}
          and isinstance(payload['versionId'],str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}',payload['versionId'])):
        method,url,data='POST',BASE+'/publish',payload
    elif action == 'unpublish' and payload is None:
        method,url,data='POST',BASE+'/unpublish',{}
    elif action == 'list_open' and isinstance(payload,dict) and set(payload)=={'status'} and payload['status'] in OPEN_STATUSES:
        query=urllib.parse.urlencode({'workflowId':WORKFLOW,'status':payload['status'],'limit':'1'})
        method,url,data='GET','https://n8n.gamelife.shop/api/v1/executions?'+query,None
    else: raise ValueError('Unsupported operation')
    raw=json.dumps(data,separators=(',',':')).encode() if data is not None else None
    req=urllib.request.Request(url,data=raw,method=method,headers={'X-N8N-API-KEY':token,'Accept':'application/json','Content-Type':'application/json'})
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        with opener.open(req,timeout=5) as response:
            body=response.read(2*1024*1024+1)
            if len(body)>2*1024*1024: raise ValueError()
            result=json.loads(body.decode('utf-8'))
            if not isinstance(result,dict): raise ValueError()
            return result
    except Exception:
        raise RuntimeError('n8n request unavailable') from None


def _child(pipe, token, action, payload):
    try: pipe.send(_request_once(token,action,payload))
    except BaseException:
        try: pipe.send(None)
        except Exception: pass
    finally: pipe.close()


class BoundedWire:
    """Fixed origin and workflow, one request per subprocess with deadline."""
    def __init__(self, token_reader, timeout=15):
        if not callable(token_reader) or not 1 <= timeout <= 20: raise ValueError('Invalid adapter')
        self.token_reader,self.timeout=token_reader,timeout

    def __call__(self, action, payload=None):
        context=multiprocessing.get_context('spawn')
        recv,send=context.Pipe(duplex=False)
        process=context.Process(target=_child,args=(send,self.token_reader(),action,payload),daemon=True)
        result=None
        try:
            process.start(); send.close()
            if recv.poll(self.timeout):
                try: result=recv.recv()
                except (EOFError,OSError): pass
        except Exception: pass
        finally:
            recv.close(); send.close()
            if process.pid is not None:
                process.join(.1)
                if process.is_alive(): process.terminate(); process.join(1)
                if process.is_alive(): process.kill(); process.join(1)
                if not process.is_alive(): process.close()
        if not isinstance(result,dict): raise RuntimeError('n8n request unavailable')
        return result


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def _graph(flow):
    if not isinstance(flow,dict) or flow.get('id') != WORKFLOW or flow.get('active') is not True:
        raise ValueError('Unexpected workflow')
    if any(key not in flow for key in WRITE_FIELDS): raise ValueError('Incomplete workflow')
    if not isinstance(flow['nodes'],list) or not isinstance(flow['connections'],dict) or not isinstance(flow['settings'],dict):
        raise ValueError('Invalid workflow')
    return {key:copy.deepcopy(flow[key]) for key in WRITE_FIELDS}


def _history_hash(value):
    if not isinstance(value,dict) or any(k not in value for k in ('nodes','connections','nodeGroups')):
        raise ValueError('Incomplete version history')
    return _digest({k:value[k] for k in ('nodes','connections','nodeGroups')})


def plan(flow, ids):
    if not isinstance(ids,dict) or set(ids) != set(NAMES) or len(set(ids.values())) != 3:
        raise ValueError('Invalid credentials')
    if any(not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,16}',value) for value in ids.values()):
        raise ValueError('Invalid credentials')
    graph=_graph(flow)
    version=flow.get('versionId')
    if not isinstance(version,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',version) or flow.get('activeVersionId') != version:
        raise ValueError('Draft and published versions differ')
    seen, changes = set(), []
    for node in graph['nodes']:
        if not isinstance(node,dict) or not isinstance(node.get('id'),str): raise ValueError('Invalid node')
        ident=node['id']
        if ident in seen: raise ValueError('Duplicate node')
        seen.add(ident)
        credentials=node.get('credentials',{})
        if not isinstance(credentials,dict): raise ValueError('Invalid credentials')
        managed=set(credentials).intersection({'openAiApi','googlePalmApi'})
        if ident not in TARGETS:
            if managed: raise ValueError('Unreviewed credential dependency')
            continue
        kind, ctype, provider = TARGETS[ident]
        if node.get('type') != kind or managed != {ctype} or not isinstance(credentials.get(ctype),dict):
            raise ValueError('Unexpected target node')
        existing=credentials[ctype]
        if set(existing) != {'id','name'} or not isinstance(existing.get('id'),str) or not isinstance(existing.get('name'),str): raise ValueError('Invalid existing credential')
        changes.append({'node_id':ident,'provider':provider,'old_id':existing['id'],'new_id':ids[provider]})
    if set(TARGETS) - seen: raise ValueError('Missing target nodes')
    before=_graph(flow)
    staged=migration.plan(flow,migration.graph_hash(flow),
                          openai={'id':ids['openai'],'name':NAMES['openai']},
                          google={'id':ids['gemini'],'name':NAMES['gemini']},
                          service={'id':ids['reader'],'name':NAMES['reader']})
    # Only this fixed, locally generated migration may modify the graph; the
    # caller cannot supply operations, paths, endpoints, or arbitrary nodes.
    nodes=graph['nodes']; connections=graph['connections']
    by_name={node['name']:node for node in nodes}
    for op in staged['operations']:
        kind=op.get('type')
        if kind == 'addNode':
            node=copy.deepcopy(op['node'])
            name=node['name']
            if name not in (migration.FETCH,migration.CHECK) or name in by_name: raise ValueError('Unexpected new node')
            node_id='cf-ai-reader-v1' if name == migration.FETCH else 'cf-ai-guard-v1'
            if node_id in seen: raise ValueError('Node ID collision')
            node['id']=node_id
            nodes.append(node); by_name[name]=node; seen.add(node_id)
        elif kind == 'removeConnection':
            source=op['source']
            if source not in migration.TRIGGERS or connections.get(source) != {'main':[[{'node':'Товар','type':'main','index':0}]]}:
                raise ValueError('Unexpected trigger edge')
            del connections[source]
        elif kind == 'addConnection':
            source,target=op['source'],op['target']
            allowed={(trigger,migration.FETCH) for trigger in migration.TRIGGERS} | {(migration.FETCH,migration.CHECK),(migration.CHECK,'Товар')}
            if (source,target) not in allowed or source in connections: raise ValueError('Unexpected connection')
            connections[source]={'main':[[{'node':target,'type':'main','index':0}]]}
        elif kind == 'setNodeParameter':
            name=op['nodeName']
            if name not in (migration.TEXT,migration.IMAGE) or op['path'] != 'modelId.value': raise ValueError('Unexpected parameter')
            by_name[name]['parameters']['modelId']['value']=op['value']
        elif kind == 'setNodeCredential':
            name,key=op['nodeName'],op['credentialKey']
            if name not in (*migration.OPENAI,*migration.GOOGLE) or key not in ('openAiApi','googlePalmApi'): raise ValueError('Unexpected credential')
            by_name[name]['credentials'][key]={'id':op['credentialId'],'name':op['credentialName']}
        else: raise ValueError('Unexpected migration action')
    changes += [{'node_id':'cf-ai-reader-v1','provider':'reader','new_id':ids['reader']},
                {'node_id':'cf-ai-guard-v1','provider':'config_guard'},
                {'node_id':'cf-t-openai','provider':'text_model_from_config'},
                {'node_id':'cf-i-gemini','provider':'image_model_from_config'}]
    return {'version':version,'before_hash':_digest(before),'before_history_hash':_history_hash(before),'after_hash':_digest(graph),
            'changes':changes,'body':graph}


class Cutover:
    """Requires an already authenticated owner and bounded, fixed-origin wire.

    preflight(stage, ids) must independently verify metadata and non-use by other
    workflows, including archived accessible workflows. No default adapter or
    route is exposed; injecting an unbounded or permissive adapter is unsafe.
    """
    def __init__(self, fence, wire, ids, preflight, readiness_probe=None, quiescence_probe=None):
        if not callable(wire) or not callable(preflight): raise ValueError('Missing safety adapter')
        self.fence,self.wire,self.ids,self.preflight=fence,wire,dict(ids),preflight
        self.readiness_probe=readiness_probe
        self.quiescence_probe=quiescence_probe

    def _quiescent(self):
        # A separately installed barrier can strengthen the mandatory paused
        # workflow and drained-execution checks. n8n still permits manual runs:
        # the owner must not start or edit the workflow during migration.
        return self.quiescence_probe is None or (callable(self.quiescence_probe) and self.quiescence_probe() is True)

    def _history_available(self, version, expected_hash):
        history=self.wire('get_version',{'versionId':version})
        return (isinstance(history,dict) and history.get('workflowId') == WORKFLOW
                and history.get('versionId') == version and _history_hash(history) == expected_hash)

    def _paused_plan(self, pause_receipt):
        if not self._quiescent(): raise PermissionError('Workflow is not quiescent')
        flow=Pause(self.fence,self.wire).check(pause_receipt)
        view=copy.deepcopy(flow)
        view['active']=True
        view['activeVersionId']=pause_receipt['version']
        return plan(view,self.ids)

    def prepare_draft(self, owner, pause_receipt):
        if self.preflight('before_draft',self.ids) is not True: raise PermissionError('Isolation unavailable')
        p=self._paused_plan(pause_receipt)
        if not self._history_available(p['version'],p['before_history_hash']): raise PermissionError('Previous version unavailable')
        binding={**{k:p[k] for k in ('version','before_hash','before_history_hash','after_hash','changes')},'pause':pause_receipt}
        proposal=self.fence.prepare(owner,'cutover','factory-cutover',binding)
        return {'proposal_id':proposal,'version':p['version'],'changes':p['changes']}

    def confirm_draft(self, owner, proposal, pause_receipt):
        p=self._paused_plan(pause_receipt)
        binding={**{k:p[k] for k in ('version','before_hash','before_history_hash','after_hash','changes')},'pause':pause_receipt}
        result=[]
        def write_once():
            if not self._quiescent(): return 'unknown'
            if self.preflight('before_draft',self.ids) is not True: return 'unknown'
            again=self._paused_plan(pause_receipt)
            if again['version'] != p['version'] or again['before_hash'] != p['before_hash']: return 'unknown'
            if not self._history_available(p['version'],p['before_history_hash']): return 'unknown'
            saved=self.wire('save_draft',p['body'])
            if not isinstance(saved,dict) or not isinstance(saved.get('versionId'),str) or saved['versionId'] == p['version']:
                return 'unknown'
            after=self.wire('get')
            if (after.get('active') is not False or after.get('activeVersionId') is not None
                    or after.get('versionId') != saved['versionId']): return 'unknown'
            view=copy.deepcopy(after); view['active']=True
            if _digest(_graph(view)) != p['after_hash'] or not Pause(self.fence,self.wire)._empty(): return 'unknown'
            receipt={'old_version':p['version'],'draft_version':saved['versionId'],'before_hash':p['before_hash'],
                     'before_history_hash':p['before_history_hash'],'after_hash':p['after_hash'],'ids':self.ids}
            raw=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()
            receipt['signature']=hmac.new(self.fence.key,b'cutover-receipt\0'+raw,hashlib.sha256).hexdigest()
            result.append(receipt)
            return 'verified'
        status=self.fence.dispatch(proposal,owner,binding,write_once)
        return {'status':status,'receipt':result[0] if status == 'verified' and result else None}

    def _validate_receipt(self, receipt):
        if not isinstance(receipt,dict) or set(receipt) != {'old_version','draft_version','before_hash','before_history_hash','after_hash','ids','signature'}:
            raise ValueError('Invalid receipt')
        payload={k:v for k,v in receipt.items() if k != 'signature'}
        if payload['ids'] != self.ids or any(not isinstance(payload[k],str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',payload[k]) for k in ('old_version','draft_version')):
            raise ValueError('Invalid receipt')
        if any(not isinstance(payload[k],str) or not re.fullmatch(r'[a-f0-9]{64}',payload[k]) for k in ('before_hash','before_history_hash','after_hash')): raise ValueError('Invalid receipt')
        if payload['old_version'] == payload['draft_version']: raise ValueError('Invalid receipt')
        expected=hmac.new(self.fence.key,b'cutover-receipt\0'+json.dumps(payload,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
        if not isinstance(receipt['signature'],str) or not hmac.compare_digest(expected,receipt['signature']): raise ValueError('Invalid receipt')
        return payload

    def _publish_binding(self, receipt):
        payload=self._validate_receipt(receipt)
        current=self.wire('get')
        if (current.get('active') is not False or current.get('activeVersionId') is not None
                or current.get('versionId') != payload['draft_version']): raise ValueError('Draft or active version changed')
        view=copy.deepcopy(current); view['active']=True
        if _digest(_graph(view)) != payload['after_hash'] or not Pause(self.fence,self.wire)._empty(): raise ValueError('Draft or execution state changed')
        if not self._history_available(payload['old_version'],payload['before_history_hash']): raise ValueError('Previous version unavailable')
        return payload

    def prepare_publish(self, owner, receipt):
        if not self._quiescent(): raise PermissionError('Workflow is not quiescent')
        binding=self._publish_binding(receipt)
        if not callable(self.readiness_probe) or self.readiness_probe() is not True:
            raise PermissionError('Reader path has not been tested')
        if self.preflight('before_publish',self.ids) is not True: raise PermissionError('Isolation unavailable')
        return self.fence.prepare(owner,'cutover','factory-cutover',binding)

    def confirm_publish(self, owner, proposal, receipt):
        binding=self._publish_binding(receipt)
        def publish_once():
            if (not self._quiescent() or not callable(self.readiness_probe) or self.readiness_probe() is not True
                    or self.preflight('before_publish',self.ids) is not True or self._publish_binding(receipt) != binding): return 'unknown'
            self.wire('publish', {'versionId':binding['draft_version']})
            current=self.wire('get')
            return ('verified' if current.get('activeVersionId') == binding['draft_version']
                    and current.get('versionId') == binding['draft_version']
                    and _digest(_graph(current)) == binding['after_hash'] else 'unknown')
        return self.fence.dispatch(proposal,owner,binding,publish_once)


class Pause:
    """Explicit owner-confirmed pause of the fixed factory workflow only.

    While paused, the owner must not manually run, edit or re-publish this
    workflow. An unknown outcome must be reconciled; never auto-retry. A pause
    receipt is evidence of observed state, not an exclusive remote lock.
    """
    def __init__(self,fence,wire):
        if not callable(wire): raise ValueError('Missing wire')
        self.fence,self.wire=fence,wire

    def _empty(self):
        for status in OPEN_STATUSES:
            value=self.wire('list_open',{'status':status})
            if (not isinstance(value,dict) or not isinstance(value.get('data'),list)
                    or 'nextCursor' not in value or value['data'] or value['nextCursor'] not in (None,'')):
                return False
        return True

    def _snapshot(self):
        flow=self.wire('get'); graph=_graph(flow)
        version=flow.get('versionId')
        if not isinstance(version,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',version) or flow.get('activeVersionId') != version:
            raise ValueError('Published and draft versions differ')
        history=self.wire('get_version',{'versionId':version})
        history_hash=_history_hash(graph)
        if (history.get('workflowId') != WORKFLOW or history.get('versionId') != version
                or _history_hash(history) != history_hash): raise ValueError('Previous version unavailable')
        if not self._empty(): raise PermissionError('Executions have not drained')
        return {'version':version,'before_hash':_digest(graph),'before_history_hash':history_hash}

    def _receipt(self,receipt):
        if not isinstance(receipt,dict) or set(receipt) != {'version','before_hash','before_history_hash','drained','signature'}:
            raise ValueError('Invalid pause receipt')
        payload={k:v for k,v in receipt.items() if k!='signature'}
        if (not isinstance(payload['version'],str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',payload['version'])
                or type(payload['drained']) is not bool
                or any(not isinstance(payload[k],str) or not re.fullmatch(r'[a-f0-9]{64}',payload[k]) for k in ('before_hash','before_history_hash'))):
            raise ValueError('Invalid pause receipt')
        raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
        signature=hmac.new(self.fence.key,b'cutover-pause\0'+raw,hashlib.sha256).hexdigest()
        if not isinstance(receipt['signature'],str) or not hmac.compare_digest(signature,receipt['signature']): raise ValueError('Invalid pause receipt')
        return payload

    def check(self,receipt):
        """Verify paused version and recheck all open execution statuses."""
        payload=self._receipt(receipt)
        current=self.wire('get')
        if (not isinstance(current,dict) or current.get('id') != WORKFLOW or current.get('active') is not False
                or current.get('activeVersionId') is not None or current.get('versionId') != payload['version']):
            raise ValueError('Workflow is no longer paused')
        view=copy.deepcopy(current); view['active']=True
        if _digest(_graph(view)) != payload['before_hash']: raise ValueError('Paused workflow changed')
        history=self.wire('get_version',{'versionId':payload['version']})
        if (history.get('workflowId') != WORKFLOW or history.get('versionId') != payload['version']
                or _history_hash(history) != payload['before_history_hash']): raise ValueError('Previous version unavailable')
        if not self._empty(): raise PermissionError('Executions have not drained')
        return current

    def prepare(self,owner):
        binding=self._snapshot()
        proposal=self.fence.prepare(owner,'cutover','factory-cutover',{'action':'pause',**binding})
        return {'proposal_id':proposal,**binding}

    def confirm(self,owner,proposal):
        binding=self._snapshot()
        result=[]
        def stop_once():
            if self._snapshot()!=binding: return 'unknown'
            self.wire('unpublish')
            current=self.wire('get')
            if (not isinstance(current,dict) or current.get('id') != WORKFLOW or current.get('active') is not False
                    or current.get('activeVersionId') is not None or current.get('versionId') != binding['version']): return 'unknown'
            # Reuse the known unchanged graph without falsely calling an
            # inactive workflow active in n8n.
            view=copy.deepcopy(current); view['active']=True
            if _digest(_graph(view)) != binding['before_hash']: return 'unknown'
            drained=self._empty()
            receipt={'version':binding['version'],'before_hash':binding['before_hash'],
                     'before_history_hash':binding['before_history_hash'],'drained':drained}
            raw=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()
            receipt['signature']=hmac.new(self.fence.key,b'cutover-pause\0'+raw,hashlib.sha256).hexdigest()
            result.append(receipt)
            return 'verified'
        status=self.fence.dispatch(proposal,owner,{'action':'pause',**binding},stop_once)
        return {'status':status,'receipt':result[0] if status == 'verified' and result else None}

    def prepare_restore(self,owner,receipt):
        self.check(receipt)
        return self.fence.prepare(owner,'cutover','factory-cutover',{'action':'restore','pause':receipt})

    def confirm_restore(self,owner,proposal,receipt):
        payload=self._receipt(receipt)
        self.check(receipt)
        def restore_once():
            self.check(receipt)
            self.wire('publish',{'versionId':payload['version']})
            current=self.wire('get')
            return ('verified' if current.get('active') is True and current.get('activeVersionId') == payload['version']
                    and current.get('versionId') == payload['version'] and _digest(_graph(current)) == payload['before_hash']
                    else 'unknown')
        return self.fence.dispatch(proposal,owner,{'action':'restore','pause':receipt},restore_once)
