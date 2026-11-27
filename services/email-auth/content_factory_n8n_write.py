"""Strict n8n credential adapter; not enabled without verified factory IDs.

This module does not authorise callers or provide a gateway. A composition
root must verify factory-only ownership and dependency isolation before use.
"""
import json
import multiprocessing
import re
import urllib.error
import urllib.request

_BASE = 'https://n8n.gamelife.shop/api/v1/'
_TYPES = {'openai': 'openAiApi', 'gemini': 'googlePalmApi'}
_NAMES = {'openai': 'SIDZEN CF OpenAI', 'gemini': 'SIDZEN CF Gemini'}
_READER_TYPE = 'httpTemplatedCustomAuth'
_READER_NAME = 'SIDZEN CF Model Config Reader'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json_response(response):
    if response.status != 200: raise ValueError('unexpected response')
    raw = response.read(65537)
    if len(raw) > 65536: raise ValueError('response too large')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict): raise ValueError('unexpected response')
    return value


def update_and_verify(provider, credential_id, token, new_key, *, opener=None):
    """Make exactly one update attempt. Never retry on an ambiguous result.

    A 2xx response does not prove the key is valid or that other workflows do
    not reference this credential. Isolation is the caller's
    independently verified responsibility. Failure is always unknown because
    n8n may have applied the PATCH before returning an error.
    """
    if provider not in _TYPES or not isinstance(credential_id, str) or not re.fullmatch(r'[A-Za-z0-9]{8,64}', credential_id):
        raise ValueError('Invalid destination')
    if not isinstance(token, str) or not re.fullmatch(r'[!-~]{16,2048}', token): raise ValueError('Invalid access')
    if not isinstance(new_key, str) or not re.fullmatch(r'[!-~]{16,2048}', new_key) or new_key.startswith('='):
        raise ValueError('Invalid input')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    headers = {'X-N8N-API-KEY': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}
    base = _BASE + 'credentials/' + credential_id
    # Complete replacement prevents previously configured endpoints, headers,
    # or expressions from receiving the new key. Only use factory-only IDs.
    data = {'apiKey': new_key}
    if provider == 'openai': data['url'] = 'https://api.openai.com/v1'
    else: data['host'] = 'https://generativelanguage.googleapis.com'
    payload = json.dumps({'type': _TYPES[provider], 'data': data, 'isPartialData': False}, separators=(',', ':')).encode('utf-8')
    try:
        with opener.open(urllib.request.Request(base, data=payload, headers=headers, method='PATCH'), timeout=5) as response:
            saved = _json_response(response)
        version = saved.get('updatedAt')
        if (saved.get('id') != credential_id or saved.get('type') != _TYPES[provider]
                or not isinstance(version, str) or not version): return 'unknown'
        with opener.open(urllib.request.Request(base, headers=headers, method='GET'), timeout=5) as response:
            latest = _json_response(response)
        # n8n doesn't return the stored secret. Metadata alone cannot prove
        # which key was saved if another writer races us.
        return ('accepted_unconfirmed' if latest.get('id') == credential_id and latest.get('type') == _TYPES[provider]
                and latest.get('updatedAt') == version else 'unknown')
    except Exception:
        # Never log or return response/error text: it may echo credentials.
        return 'unknown'


def _child(send, function, args):
    try:
        result = function(*args)
        send.send(result if result in ('verified', 'unknown', 'accepted_unconfirmed') else 'unknown')
    except BaseException:
        try: send.send('unknown')
        except Exception: pass
    finally:
        send.close()


def _run_bounded(function, args, timeout):
    """Run once in a disposable process; timeouts never imply remote failure.

    An n8n request may complete after the child is terminated. The caller
    must persist and retain the unknown state, blocking any repeat attempt.
    Secret arguments are passed through multiprocessing's local IPC, not argv.
    """
    if not 1 <= timeout <= 60: raise ValueError('Invalid deadline')
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(send, function, args), daemon=True)
    try:
        process.start()
        send.close()
        if receive.poll(timeout):
            try: outcome = receive.recv()
            except (EOFError, OSError): outcome = 'unknown'
        else:
            outcome = 'unknown'
    except Exception:
        outcome = 'unknown'
    finally:
        receive.close()
        send.close()
        if process.pid is not None:
            process.join(0.1)
            if process.is_alive():
                process.terminate(); process.join(1)
            if process.is_alive():
                process.kill(); process.join(1)
            if not process.is_alive(): process.close()
    return outcome if outcome in ('verified', 'unknown', 'accepted_unconfirmed') else 'unknown'


def bounded_update(provider, credential_id, token, new_key):
    return _run_bounded(update_and_verify, (provider, credential_id, token, new_key), 15)


def provision_and_verify(provider, credential_id, token, new_key, idempotency_key, *, opener=None):
    """Create one uniquely identified factory credential, once, without retry.

    Caller must first obtain a unique random ID, obtain owner approval and hold
    the dispatch fence. Unknown includes any failure, including the preflight
    lookup: never infer absence from a timeout or permission error.
    """
    if provider not in (*_TYPES, 'reader') or not isinstance(credential_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,16}', credential_id):
        raise ValueError('Invalid destination')
    if not isinstance(token, str) or not re.fullmatch(r'[!-~]{16,2048}', token): raise ValueError('Invalid access')
    if not isinstance(new_key, str) or not re.fullmatch(r'[!-~]{16,2048}', new_key) or new_key.startswith('='):
        raise ValueError('Invalid input')
    if provider == 'reader' and (not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',new_key) or len(set(new_key)) < 16):
        raise ValueError('Invalid reader token')
    if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', idempotency_key):
        raise ValueError('Invalid idempotency key')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    headers = {'X-N8N-API-KEY': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}
    detail = _BASE + 'credentials/' + credential_id
    if provider == 'reader':
        kind, name = _READER_TYPE, _READER_NAME
        data = {'template': json.dumps({'headers':{'Authorization':'Bearer {{api_key}}'}},separators=(',',':')),
                'placeholderDefs':json.dumps([{'name':'api_key','title':'Reader token','type':'password'}],separators=(',',':')),
                'placeholderValues':json.dumps({'api_key':new_key},separators=(',',':')),
                'serviceHost':'3dprinta.shop','serviceOrigin':'https://3dprinta.shop'}
    else:
        kind, name = _TYPES[provider], _NAMES[provider]
        data = {'apiKey': new_key}
        if provider == 'openai': data['url'] = 'https://api.openai.com/v1'
        else: data['host'] = 'https://generativelanguage.googleapis.com'
    payload = json.dumps({'id': credential_id, 'name': name, 'type': kind, 'data': data}, separators=(',', ':')).encode('utf-8')
    try:
        try:
            with opener.open(urllib.request.Request(detail, headers=headers, method='GET'), timeout=5):
                return 'unknown'  # ID already exists; do not touch it.
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code != 404: return 'unknown'
        create_headers = dict(headers, **{'Idempotency-Key': idempotency_key})
        with opener.open(urllib.request.Request(_BASE + 'credentials', data=payload, headers=create_headers, method='POST'), timeout=5) as response:
            if response.status != 200 and response.status != 201: return 'unknown'
            raw = response.read(65537)
            if len(raw) > 65536: return 'unknown'
            saved = json.loads(raw.decode('utf-8'))
        if not isinstance(saved, dict) or (saved.get('id'),saved.get('name'),saved.get('type')) != (credential_id,name,kind): return 'unknown'
        with opener.open(urllib.request.Request(detail, headers=headers, method='GET'), timeout=5) as response:
            latest = _json_response(response)
        if ((latest.get('id'),latest.get('name'),latest.get('type')) !=
                (credential_id,name,kind) or latest.get('updatedAt') != saved.get('updatedAt')
                or not isinstance(saved.get('updatedAt'), str) or not saved['updatedAt']): return 'unknown'
        return 'accepted_unconfirmed'
    except Exception:
        return 'unknown'


def bounded_provision(provider, credential_id, token, new_key, idempotency_key):
    return _run_bounded(provision_and_verify, (provider, credential_id, token, new_key, idempotency_key), 15)
