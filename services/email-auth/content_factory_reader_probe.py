"""Verify a recent, side-effect-free n8n reader execution before cutover.

The owner runs a separate two-node manual test workflow. Verification reads
only its exact execution, configuration, and credential metadata; it cannot
launch or mutate workflows. No provider requests or credential tests occur.
"""
import datetime
import re

URL='https://3dprinta.shop/api/content-factory/model-config'
READER_NAME='SIDZEN CF Model Config Reader'


def _time(value):
    if not isinstance(value,str): raise ValueError()
    dt=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError()
    return dt.timestamp()


def _graph(flow, workflow_id, reader_id):
    if not isinstance(flow,dict) or flow.get('id') != workflow_id or flow.get('active') is not False: raise ValueError()
    if flow.get('pinData') not in ({},None): raise ValueError()
    nodes=flow.get('nodes')
    if not isinstance(nodes,list) or len(nodes)!=2 or any(not isinstance(n,dict) for n in nodes): raise ValueError()
    triggers=[n for n in nodes if n.get('type')=='n8n-nodes-base.manualTrigger']
    readers=[n for n in nodes if n.get('type')=='n8n-nodes-base.httpRequest']
    if len(triggers)!=1 or len(readers)!=1: raise ValueError()
    trigger,reader=triggers[0],readers[0]
    if any(n.get('disabled') is True or n.get('alwaysOutputData') is True or n.get('onError') not in (None,'stopWorkflow') for n in nodes): raise ValueError()
    if not all(isinstance(n.get('name'),str) and n['name'] for n in nodes): raise ValueError()
    if trigger.get('parameters',{}) != {}: raise ValueError()
    if reader.get('credentials') != {'httpTemplatedCustomAuth':{'id':reader_id,'name':READER_NAME}}: raise ValueError()
    params=reader.get('parameters')
    if not isinstance(params,dict) or set(params)-{'method','url','authentication','genericAuthType','options'}: raise ValueError()
    if (params.get('method','GET')!='GET' or params.get('url')!=URL or params.get('authentication')!='genericCredentialType'
            or params.get('genericAuthType')!='httpTemplatedCustomAuth'): raise ValueError()
    options=params.get('options',{})
    if not isinstance(options,dict) or set(options)-{'redirect','timeout'}: raise ValueError()
    if options.get('redirect') != {'redirect':{'followRedirects':False}}: raise ValueError()
    if type(options.get('timeout')) is not int or not 1 <= options['timeout'] <= 10000: raise ValueError()
    expected={trigger['name']:{'main':[[{'node':reader['name'],'type':'main','index':0}]]}}
    if flow.get('connections') != expected: raise ValueError()
    return trigger['name'],reader['name']


def verify(workflow, execution, metadata, *, workflow_id, execution_id, reader_id, expected, now):
    """Return bool only. Fail closed for missing, stale, redacted or pinned data."""
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',workflow_id) or not re.fullmatch(r'[1-9][0-9]{0,19}',execution_id): return False
        trigger_name,reader_name=_graph(workflow,workflow_id,reader_id)
        if (not isinstance(execution,dict) or str(execution.get('id')) != execution_id or execution.get('workflowId') != workflow_id
                or execution.get('status') != 'success' or execution.get('mode') != 'manual'): return False
        started=_time(execution.get('startedAt')); stopped=_time(execution.get('stoppedAt'))
        updated=_time(metadata.get('updatedAt'))
        if not (now-300 <= started <= stopped <= now+5 and updated <= started): return False
        if (metadata.get('id') != reader_id or metadata.get('name') != READER_NAME
                or metadata.get('type') != 'httpTemplatedCustomAuth' or metadata.get('isGlobal') is not False
                or metadata.get('isManaged') is not False or metadata.get('isResolvable') is not False
                or 'resolverId' not in metadata or metadata['resolverId'] is not None): return False
        if execution.get('workflowVersionId') is not None and execution['workflowVersionId'] != workflow.get('versionId'): return False
        data=execution.get('data')
        if not isinstance(data,dict): return False
        run=data.get('resultData',{}).get('runData')
        if not isinstance(run,dict) or set(run) != {trigger_name,reader_name}: return False
        reader_runs=run[reader_name]
        if not isinstance(reader_runs,list) or len(reader_runs)!=1: return False
        main=reader_runs[0].get('data',{}).get('main')
        if not isinstance(main,list) or len(main)!=1 or not isinstance(main[0],list) or len(main[0])!=1: return False
        if not isinstance(main[0][0],dict) or main[0][0].get('json') != expected: return False
        # The exact graph used at execution must be present, not inferred from
        # the currently saved workflow or a node's pinned/manual output.
        snapshot=execution.get('workflowData',data.get('workflowData'))
        if _graph(snapshot,workflow_id,reader_id) != (trigger_name,reader_name): return False
        if snapshot.get('nodes') != workflow.get('nodes') or snapshot.get('connections') != workflow.get('connections'): return False
        if execution.get('data',{}).get('startData',{}).get('destinationNode') is not None: return False
        return True
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
        return False
