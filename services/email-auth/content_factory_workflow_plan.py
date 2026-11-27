"""Offline migration plan. It does not connect to n8n or apply operations.

The caller must compare the entire live graph immediately before cutover and
perform the transition through an authorised, audited gateway. n8n's public
workflow API has no CAS; this plan cannot prevent concurrent editor changes.
"""
import hashlib
import json
import re

WORKFLOW = 'zWAbNcKhgzOX2PZq'
TEXT = 'OpenAI — идея и текст'
IMAGE = 'Gemini — изображение'
FETCH = 'Получить настройки моделей фабрики'
CHECK = 'Проверить настройки моделей фабрики'
URL = 'https://3dprinta.shop/api/content-factory/model-config'
OPENAI = ('OpenAI — идея и текст',)
GOOGLE = ('Gemini — изображение', 'Gemini Omni — видео', 'Google — статус видео',
          'Google — скачать видео', 'Gemini — проверить изображение',
          'Gemini — проверить видео', 'Gemini — перепроверить видео',
          'Gemini — финальная проверка видео')
TRIGGERS = ('Ручной запуск', 'Генерация — 10:00 МСК')
VALIDATE_JS = r'''const a = $input.all();
if (a.length !== 1) throw new Error('Invalid factory configuration');
const v = a[0].json;
const keys = x => x && typeof x === 'object' && !Array.isArray(x) ? Object.keys(x).sort().join(',') : '';
const id = x => typeof x === 'string' && /^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$/.test(x) && !/(^|\/)(sk-|AIza)/i.test(x);
if (keys(v) !== 'models,revision' || !Number.isSafeInteger(v.revision) || v.revision < 1 ||
    keys(v.models) !== 'image,text' || !id(v.models.text) || !id(v.models.image))
  throw new Error('Invalid factory configuration');
return [{json: {revision: v.revision, models: {text: v.models.text, image: v.models.image}}}];'''


def graph_hash(workflow):
    raw = json.dumps(workflow, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _reference(value):
    if not isinstance(value, dict) or set(value) != {'id', 'name'}:
        raise ValueError('Invalid credential reference')
    if not all(isinstance(value[k], str) and re.fullmatch(r'[A-Za-z0-9 _.-]{1,100}', value[k]) for k in value):
        raise ValueError('Invalid credential reference')
    if value['id'].startswith(('sk-', 'AIza')) or value['name'].startswith(('sk-', 'AIza')):
        raise ValueError('Credential reference must not contain a key')
    return value


def plan(workflow, expected_hash, *, openai, google, service):
    """Create narrow n8n update operations from a fresh, verified snapshot.

    Credential refs contain only IDs and display names, never credentials.
    The result is an unexecuted plan, not an approval or a CAS guarantee.
    """
    if not isinstance(workflow, dict) or workflow.get('id') != WORKFLOW or workflow.get('active') is not True:
        raise ValueError('Wrong workflow')
    version = workflow.get('versionId')
    if not isinstance(version, str) or not version or workflow.get('activeVersionId') != version:
        raise ValueError('Published and draft versions differ')
    if not isinstance(expected_hash, str) or expected_hash != graph_hash(workflow):
        raise ValueError('Snapshot changed')
    openai, google, service = map(_reference, (openai, google, service))
    ids = {openai['id'], google['id'], service['id']}
    if len(ids) != 3 or ids.intersection({'cH5bc3kc2uN9fzd8', 'm9Mcn55vNwWxgQx6'}):
        raise ValueError('Credentials must be separate')
    nodes = workflow.get('nodes')
    if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
        raise ValueError('Invalid nodes')
    by_name = {}
    for node in nodes:
        if node.get('name') in by_name:
            raise ValueError('Duplicate node')
        by_name[node.get('name')] = node
    if FETCH in by_name or CHECK in by_name:
        raise ValueError('Migration already present')
    if any(name not in by_name for name in (*TRIGGERS, *OPENAI, *GOOGLE, 'Товар')):
        raise ValueError('Missing expected node')
    if by_name[TEXT].get('type') != '@n8n/n8n-nodes-langchain.openAi' or by_name[IMAGE].get('type') != '@n8n/n8n-nodes-langchain.googleGemini':
        raise ValueError('Unexpected model nodes')
    if any(by_name[n].get('type') != 'n8n-nodes-base.httpRequest' for n in GOOGLE if n != IMAGE):
        raise ValueError('Unexpected Google node type')
    for node in nodes:
        credentials = node.get('credentials', {})
        if not isinstance(credentials, dict):
            raise ValueError('Invalid credential mapping')
        for key, expected, allowed in (('openAiApi', 'cH5bc3kc2uN9fzd8', OPENAI),
                                       ('googlePalmApi', 'm9Mcn55vNwWxgQx6', GOOGLE)):
            credential = credentials.get(key)
            if isinstance(credential, dict) and credential.get('id') == expected and node.get('name') not in allowed:
                raise ValueError('Unexpected credential use')
    for name, key, old_id in [(TEXT, 'openAiApi', 'cH5bc3kc2uN9fzd8')] + [(n, 'googlePalmApi', 'm9Mcn55vNwWxgQx6') for n in GOOGLE]:
        if by_name[name].get('credentials', {}).get(key, {}).get('id') != old_id:
            raise ValueError('Credential assignments changed')
        if (openai if key == 'openAiApi' else google)['id'] == old_id:
            raise ValueError('Shared credential must not be reused')
    connections = workflow.get('connections')
    if not isinstance(connections, dict):
        raise ValueError('Invalid connections')
    for name in TRIGGERS:
        if connections.get(name) != {'main': [[{'node': 'Товар', 'type': 'main', 'index': 0}]]}:
            raise ValueError('Unexpected trigger connection')
    for name in (TEXT, IMAGE):
        model = by_name[name].get('parameters', {}).get('modelId')
        if not isinstance(model, dict) or model.get('mode') != 'id' or not isinstance(model.get('value'), str):
            raise ValueError('Unexpected model field')
    operations = [
        {'type': 'addNode', 'node': {'name': FETCH, 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.5,
         'position': [-600, -200], 'parameters': {'method': 'GET', 'url': URL, 'authentication': 'genericCredentialType',
         'genericAuthType': 'httpTemplatedCustomAuth', 'options': {'redirect': {'redirect': {'followRedirects': False}}, 'timeout': 5000}},
         'credentials': {'httpTemplatedCustomAuth': service}}},
        {'type': 'addNode', 'node': {'name': CHECK, 'type': 'n8n-nodes-base.code', 'typeVersion': 2,
         'position': [-400, -200], 'parameters': {'jsCode': VALIDATE_JS}}},
    ]
    for trigger in TRIGGERS:
        operations += [{'type': 'removeConnection', 'source': trigger, 'target': 'Товар', 'sourceIndex': 0, 'targetIndex': 0},
                       {'type': 'addConnection', 'source': trigger, 'target': FETCH, 'sourceIndex': 0, 'targetIndex': 0}]
    operations += [{'type': 'addConnection', 'source': FETCH, 'target': CHECK, 'sourceIndex': 0, 'targetIndex': 0},
                   {'type': 'addConnection', 'source': CHECK, 'target': 'Товар', 'sourceIndex': 0, 'targetIndex': 0}]
    for name, slot in ((TEXT, 'text'), (IMAGE, 'image')):
        operations.append({'type': 'setNodeParameter', 'nodeName': name, 'path': 'modelId.value',
                           'value': "={{ $('" + CHECK + "').first().json.models." + slot + ' }}'})
    for name, kind, ref in [(n, 'openAiApi', openai) for n in OPENAI] + [(n, 'googlePalmApi', google) for n in GOOGLE]:
        operations.append({'type': 'setNodeCredential', 'nodeName': name, 'credentialKey': kind,
                           'credentialId': ref['id'], 'credentialName': ref['name']})
    return {'workflow_id': WORKFLOW, 'version': version, 'graph_sha256': expected_hash, 'operations': operations}
