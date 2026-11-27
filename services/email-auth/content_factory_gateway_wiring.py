"""Fail-closed building blocks for an n8n factory credential gateway.

No portal routes are enabled here. Provision an owner-scoped n8n API token in
the encrypted vault under n8n_content_factory_write; never reuse the read-only
dashboard key. Only enable after the owner sets the two dedicated IDs and
confirms migration of the factory workflow. Dependency scans cannot prevent a
concurrent n8n editor from changing references after a scan.
"""
import hashlib
import hmac
import json
import multiprocessing
import re
import time
import urllib.parse
import urllib.request

from content_factory_control import CredentialControl

BASE = 'https://n8n.gamelife.shop/api/v1/'
FACTORY = 'zWAbNcKhgzOX2PZq'
EXPECTED = {'openai': ('openAiApi', 'SIDZEN CF OpenAI'), 'gemini': ('googlePalmApi', 'SIDZEN CF Gemini'),
            'reader': ('httpTemplatedCustomAuth', 'SIDZEN CF Model Config Reader')}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def token_reader(vault):
    def read():
        try:
            data = vault.read('n8n_content_factory_write')
            value = data.get('api_key') if isinstance(data, dict) else None
            if not isinstance(value, str) or not re.fullmatch(r'[!-~]{16,2048}', value): raise ValueError()
            previous = vault.read('n8n')
            readonly = previous.get('api_key') if isinstance(previous, dict) else None
            if isinstance(readonly, str) and hmac.compare_digest(value, readonly): raise ValueError()
            return value
        except Exception:
            raise RuntimeError('Gateway token unavailable') from None
    return read


class ReadAPI:
    def __init__(self, get_token):
        self.get_token = get_token

    def __call__(self, method, path, timeout):
        # Only fixed resource shapes; no caller-controlled origin or fragments.
        if method != 'GET' or not isinstance(path, str): raise ValueError()
        if not (re.fullmatch(r'credentials/[A-Za-z0-9_-]{1,16}', path) and method == 'GET'
                or re.fullmatch(r'workflows/[A-Za-z0-9_-]{1,64}', path) and method == 'GET'
                or re.fullmatch(r'workflows\?limit=100(?:&cursor=[A-Za-z0-9%_.~-]{1,1200})?', path) and method == 'GET'):
            raise ValueError()
        token = self.get_token()
        req = urllib.request.Request(BASE + path, method=method,
            headers={'X-N8N-API-KEY': token, 'Accept':'application/json'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(req, timeout=min(4, max(.1, timeout))) as response:
                raw = response.read(2*1024*1024+1)
                if len(raw) > 2*1024*1024: raise ValueError()
                result = json.loads(raw.decode('utf-8'))
                if not isinstance(result, dict): raise ValueError()
                return result
        except Exception:
            raise RuntimeError('n8n read unavailable') from None


def _references(nodes, ident):
    if not isinstance(nodes, list) or len(nodes) > 10000: raise ValueError()
    for node in nodes:
        if not isinstance(node, dict): raise ValueError()
        creds = node.get('credentials', {})
        if not isinstance(creds, dict): raise ValueError()
        for item in creds.values():
            if not isinstance(item, dict) or not isinstance(item.get('id'), str): raise ValueError()
            if item['id'] == ident: return True
    return False


def verifier(read, *, clock=time.monotonic, deadline=12, inventory_complete=False, fresh_identifier=False):
    def verify(provider, ident):
        try:
            # Public listing coverage of archived workflows and inaccessible
            # projects is unproven for this installation. Production build never
            # sets this flag: remain closed until independently established.
            if inventory_complete is not True: return False
            if provider not in EXPECTED or not re.fullmatch(r'[A-Za-z0-9_-]{1,16}', ident): return False
            until = clock() + deadline
            def get(path):
                remaining = until - clock()
                if remaining <= 0: raise TimeoutError()
                return read('GET', path, remaining)
            if not fresh_identifier:
                metadata = get('credentials/' + ident)
                if ((metadata.get('id'), metadata.get('type'), metadata.get('name')) != (ident, *EXPECTED[provider])
                        or metadata.get('isGlobal') is not False
                        or metadata.get('isManaged') is not False
                        or metadata.get('isResolvable') is not False
                        or 'resolverId' not in metadata or metadata['resolverId'] is not None): return False
            cursor, seen, ids, factory_used, factory_seen = None, set(), set(), False, False
            for _ in range(10):
                query = 'workflows?limit=100' + ('&cursor=' + urllib.parse.quote(cursor, safe='') if cursor else '')
                page = get(query)
                if not isinstance(page.get('data'), list) or 'nextCursor' not in page: return False
                for item in page['data']:
                    if not isinstance(item, dict) or not isinstance(item.get('id'), str): return False
                    wid = item['id']
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', wid) or wid in ids: return False
                    ids.add(wid)
                    if wid == FACTORY: factory_seen = True
                    if len(ids) > 300: return False
                    flow = get('workflows/' + wid)
                    if flow.get('id') != wid or type(flow.get('active')) is not bool: return False
                    used = _references(flow.get('nodes'), ident)
                    if flow['active']:
                        active = flow.get('activeVersionId')
                        if not isinstance(active, str) or not active: return False
                        if active != flow.get('versionId'):
                            published = flow.get('activeVersion')
                            if not isinstance(published, dict) or published.get('versionId') != active: return False
                            used = _references(published.get('nodes'), ident) or used
                    if used and (wid != FACTORY or fresh_identifier): return False
                    if used: factory_used = True
                nxt = page['nextCursor']
                if nxt in (None, ''): return factory_seen if fresh_identifier else factory_used
                if not isinstance(nxt, str) or len(nxt) > 400 or nxt in seen: return False
                seen.add(nxt); cursor = nxt
            return False
        except Exception:
            return False
    return verify


def inspection_reader(read, ids, *, bounded_supervisor=False):
    """Read non-secret metadata only; no provider test or secret identity."""
    def inspect(operation, target):
        # The actual read worker does not yet have a whole-process deadline.
        # Production build deliberately leaves inspection unavailable.
        if bounded_supervisor is not True: raise RuntimeError('Inspection unavailable')
        if operation != 'credential' or target not in ('factory-openai', 'factory-gemini', 'factory-reader'): raise ValueError()
        provider = target.removeprefix('factory-')
        ident = ids[provider]
        metadata = read('GET', 'credentials/' + ident, 4)
        if ((metadata.get('id'), metadata.get('type'), metadata.get('name')) != (ident, *EXPECTED[provider])
                or not isinstance(metadata.get('updatedAt'), str)): raise ValueError()
        evidence = {'id':ident, 'type':metadata['type'], 'name':metadata['name'],
                    'updatedAt':metadata['updatedAt']}
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'observation': 'credential_exists_unverified',
                'evidence': digest}
    return inspect


def _read_worker(pipe, token, mode, provider, ident):
    try:
        reader = ReadAPI(lambda: token)
        if mode in ('scan', 'provision'):
            result = verifier(reader, inventory_complete=True, fresh_identifier=mode == 'provision')(provider, ident)
            pipe.send(result is True)
        elif mode == 'inspect':
            result = inspection_reader(reader, {provider: ident}, bounded_supervisor=True)('credential', 'factory-' + provider)
            pipe.send(result)
        else:
            pipe.send(False)
    except BaseException:
        try: pipe.send(False)
        except Exception: pass
    finally:
        pipe.close()


def _bounded_read(token, mode, provider, ident, *, timeout=15):
    """Run the whole read-only inventory or inspection with a wall deadline."""
    if mode not in ('scan', 'inspect', 'provision') or provider not in EXPECTED or not 1 <= timeout <= 20:
        return False
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_read_worker, args=(send, token, mode, provider, ident), daemon=True)
    result = False
    try:
        process.start(); send.close()
        if receive.poll(timeout):
            try: result = receive.recv()
            except (EOFError, OSError): result = False
    except Exception:
        result = False
    finally:
        receive.close(); send.close()
        if process.pid is not None:
            process.join(.1)
            if process.is_alive(): process.terminate(); process.join(1)
            if process.is_alive(): process.kill(); process.join(1)
            if not process.is_alive(): process.close()
    if mode in ('scan', 'provision'): return result is True
    if (isinstance(result, dict) and set(result) == {'observation', 'evidence'}
            and result['observation'] == 'credential_exists_unverified'
            and isinstance(result['evidence'], str) and re.fullmatch(r'[a-f0-9]{64}', result['evidence'])):
        return result
    return False


def _cutover_worker(pipe, token, stage, ids):
    try:
        reader=ReadAPI(lambda:token)
        for provider in ('openai','gemini','reader'):
            ident=ids[provider]
            if stage == 'before_draft':
                meta=reader('GET','credentials/'+ident,4)
                if ((meta.get('id'),meta.get('type'),meta.get('name')) != (ident,*EXPECTED[provider])
                        or meta.get('isGlobal') is not False or meta.get('isManaged') is not False
                        or meta.get('isResolvable') is not False or meta.get('resolverId') is not None
                        or 'resolverId' not in meta):
                    pipe.send(False); return
            if not verifier(reader,inventory_complete=True,fresh_identifier=stage == 'before_draft')(provider,ident):
                pipe.send(False); return
        pipe.send(True)
    except BaseException:
        try: pipe.send(False)
        except Exception: pass
    finally: pipe.close()


def bounded_cutover_preflight(token, stage, ids, *, timeout=15):
    if (stage not in ('before_draft','before_publish') or not isinstance(ids,dict)
            or set(ids) != {'openai','gemini','reader'} or not 1 <= timeout <= 20): return False
    ctx=multiprocessing.get_context('spawn'); recv,send=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_cutover_worker,args=(send,token,stage,ids),daemon=True)
    result=False
    try:
        process.start(); send.close()
        if recv.poll(timeout):
            try: result=recv.recv() is True
            except (EOFError,OSError): pass
    except Exception: pass
    finally:
        recv.close(); send.close()
        if process.pid is not None:
            process.join(.1)
            if process.is_alive(): process.terminate(); process.join(1)
            if process.is_alive(): process.kill(); process.join(1)
            if not process.is_alive(): process.close()
    return result


def build(vault, *, db_path, hmac_key, credential_ids, owner_scope_confirmed=False, supervisor=_bounded_read, reader_id=None):
    """Explicit opt-in; owner must confirm token can read all projects/archives."""
    read_token = token_reader(vault)
    if not callable(supervisor): raise ValueError('Supervisor required')
    ids = dict(credential_ids)
    if reader_id is not None:
        if (not isinstance(reader_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,16}',reader_id)
                or reader_id in ids.values()): raise ValueError('Invalid reader credential ID')
        ids['reader'] = reader_id
    def isolated(provider, ident):
        if owner_scope_confirmed is not True or ids.get(provider) != ident: return False
        return supervisor(read_token(), 'scan', provider, ident) is True
    def provisionable(provider, ident):
        if owner_scope_confirmed is not True or ids.get(provider) != ident: return False
        return supervisor(read_token(), 'provision', provider, ident) is True
    def inspect(operation, target):
        if owner_scope_confirmed is not True or operation != 'credential' or target not in ('factory-openai', 'factory-gemini', 'factory-reader'):
            raise PermissionError('Inspection unavailable')
        provider = target.removeprefix('factory-')
        result = supervisor(read_token(), 'inspect', provider, ids[provider])
        if not isinstance(result, dict): raise PermissionError('Inspection unavailable')
        return result
    control = CredentialControl(db_path, hmac_key, credential_ids=credential_ids,
                                token_reader=read_token, isolation_verifier=isolated,
                                provision_verifier=provisionable,
                                configuration_ready=lambda: owner_scope_confirmed is True)
    control.model_reader_id = reader_id
    control.model_reader_probe = provisionable if reader_id is not None else None
    return control, inspect
