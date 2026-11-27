"""Read-only, narrowly scoped status view of the existing n8n content factory.

Only GET requests to this installation and fixed resources are supported.  n8n's
public API may not be available for every installation or API-key scope; such
failures are represented as unavailable, never as empty successful results.
"""
import json
import math
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request

BASE = 'https://n8n.gamelife.shop/api/v1/'
WORKFLOW_ID = 'zWAbNcKhgzOX2PZq'
QUEUE_ID = 'IeU0DQdy1Qq33yxz'
IDEAS_ID = 'fWb8j5VxtlQaEsjP'
BUDGET_ID = 'kUd3G9ZAaqVWWcdI'
MAX_BYTES = 262144  # projected worker output
MAX_HTTP_BYTES = 2 * 1024 * 1024  # unprojected API responses
TIMEOUT = 3
WALL_TIMEOUT = 12


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(path, token):
    # The caller cannot supply URLs, paths, methods, headers, or query strings.
    if path not in (
        'workflows/' + WORKFLOW_ID,
        'data-tables/' + QUEUE_ID + '/rows?limit=50&sortBy=createdAt%3Adesc',
        'data-tables/' + IDEAS_ID + '/rows?limit=50&sortBy=createdAt%3Adesc',
        'data-tables/' + BUDGET_ID + '/rows?limit=50',
    ):
        raise ValueError('Unsupported resource')
    req = urllib.request.Request(BASE + path, headers={
        'X-N8N-API-KEY': token, 'Accept': 'application/json'}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=TIMEOUT) as response:
        raw = response.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raise ValueError('Response too large')
        return json.loads(raw.decode('utf-8'))


def _text(value, limit=200, token=''):
    if not isinstance(value, str):
        return ''
    # Redact before truncation: a cut through a secret must not expose its prefix.
    return (value.replace(token, '[скрыто]') if token else value)[:limit]


def _token(vault):
    # Values are never rendered, returned to clients, or included in errors.
    token = None
    if vault is not None:
        try:
            entry = vault.read('n8n')
            if isinstance(entry, dict):
                token = entry.get('api_key')
        except Exception:
            raise RuntimeError('Vault unavailable') from None
    return token if isinstance(token, str) and re.fullmatch(r'[!-~]{16,2048}', token) else None


def _rows(value):
    if not isinstance(value, dict) or not isinstance(value.get('data'), list):
        raise ValueError('Unexpected response')
    if any(not isinstance(row, dict) for row in value['data']):
        raise ValueError('Unexpected row')
    return value['data'][:50]


def _redact(value, token):
    if isinstance(value, str):
        return value.replace(token, '[скрыто]')
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, token) for key, item in value.items()}
    return value


def _published_config(workflow):
    """Use only the version verified by the API as published."""
    config = {'status': 'unavailable', 'timezone': None, 'generation_time': None,
              'poll_cron': None, 'models': {'text': None, 'image': None, 'video': None},
              'instagram_account': None}
    if not isinstance(workflow, dict) or workflow.get('active') is not True:
        return config
    active = workflow.get('activeVersionId')
    draft = workflow.get('versionId')
    if not isinstance(active, str) or not active:
        return config
    published = workflow.get('activeVersion')
    if isinstance(published, dict) and published.get('versionId') == active:
        nodes = published.get('nodes')
    elif active == draft:
        nodes = workflow.get('nodes')
    else:
        return config
    settings = workflow.get('settings')
    if not isinstance(nodes, list):
        return config
    config['status'] = 'partial'
    if active == draft and isinstance(settings, dict) and settings.get('timezone') == 'Europe/Moscow':
        config['timezone'] = 'Europe/Moscow'

    def node(name, node_type):
        matches = [n for n in nodes if isinstance(n, dict) and n.get('name') == name and n.get('type') == node_type]
        return matches[0] if len(matches) == 1 and matches[0].get('disabled') is not True else {}

    schedule = node('Генерация — 10:00 МСК', 'n8n-nodes-base.scheduleTrigger')
    params = schedule.get('parameters', {})
    intervals = params.get('rule', {}).get('interval', []) if isinstance(params, dict) and isinstance(params.get('rule'), dict) else []
    if isinstance(intervals, list) and len(intervals) == 1 and isinstance(intervals[0], dict):
        rule = intervals[0]
        hour, minute = rule.get('triggerAtHour'), rule.get('triggerAtMinute', 0)
        if (rule.get('field', 'days') == 'days' and type(rule.get('daysInterval', 1)) is int and rule.get('daysInterval', 1) == 1
                and type(hour) is int and 0 <= hour <= 23 and type(minute) is int and 0 <= minute <= 59):
            config['generation_time'] = f'{hour:02d}:{minute:02d}'

    poll = node('Публикация — проверка каждые 5 минут', 'n8n-nodes-base.scheduleTrigger')
    params = poll.get('parameters', {})
    intervals = params.get('rule', {}).get('interval', []) if isinstance(params, dict) and isinstance(params.get('rule'), dict) else []
    if isinstance(intervals, list) and len(intervals) == 1 and intervals[0] == {'field': 'cronExpression', 'expression': '*/5 * * * *'}:
        config['poll_cron'] = '*/5 * * * *'

    for name, node_type, slot, pattern in (
        ('OpenAI — идея и текст', '@n8n/n8n-nodes-langchain.openAi', 'text', r'gpt-[a-zA-Z0-9][a-zA-Z0-9.\-]{0,60}'),
        ('Gemini — изображение', '@n8n/n8n-nodes-langchain.googleGemini', 'image', r'models/gemini-[a-zA-Z0-9][a-zA-Z0-9.\-]{0,60}'),
    ):
        params = node(name, node_type).get('parameters', {})
        model = params.get('modelId', {}) if isinstance(params, dict) else {}
        value = model.get('value') if isinstance(model, dict) and model.get('mode') == 'id' else None
        if isinstance(value, str) and re.fullmatch(pattern, value):
            config['models'][slot] = value
    return config


def _unavailable():
    return {
        'status': 'not_configured',
        'workflow': {'status': 'unknown', 'config': _published_config(None)},
        'queue': {'status': 'unavailable', 'items': [], 'has_more': False},
        'ideas': {'status': 'unavailable', 'items': [], 'has_more': False},
        'budget': {'status': 'unavailable', 'monthly_budget_usd': None, 'enabled': None},
        'metrics': {'status': 'unavailable'},
    }
def _collect(token):
    result = _unavailable()
    result['status'] = 'unavailable'
    successes = 0
    try:
        obj = _request('workflows/' + WORKFLOW_ID, token)
        if isinstance(obj, dict) and type(obj.get('active')) is bool:
            result['workflow']['status'] = 'active' if obj['active'] else 'inactive'
            result['workflow']['config'] = _published_config(obj)
            successes += 1
    except Exception:
        pass
    try:
        page = _request('data-tables/' + QUEUE_ID + '/rows?limit=50&sortBy=createdAt%3Adesc', token)
        rows = _rows(page)
        result['queue'] = {'status': 'available', 'items': [{
            'status': _text(r.get('status'), 40, token),
            'media_type': _text(r.get('media_type'), 40, token),
            'scheduled_at': _text(r.get('scheduled_at'), 40, token),
        } for r in rows], 'has_more': bool(page.get('nextCursor'))}
        successes += 1
    except Exception:
        pass
    try:
        page = _request('data-tables/' + IDEAS_ID + '/rows?limit=50&sortBy=createdAt%3Adesc', token)
        rows = _rows(page)
        result['ideas'] = {'status': 'available', 'items': [{
            'title': _text(r.get('title'), 200, token), 'format': _text(r.get('format'), 40, token),
            'status': _text(r.get('status'), 40, token), 'createdAt': _text(r.get('createdAt'), 40, token),
        } for r in rows], 'has_more': bool(page.get('nextCursor'))}
        successes += 1
    except Exception:
        pass
    try:
        rows = _rows(_request('data-tables/' + BUDGET_ID + '/rows?limit=50', token))
        pilot = next((r for r in rows if r.get('setting_key') == 'pilot'), None)
        amount = pilot.get('monthly_budget_usd') if pilot else None
        if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
            enabled = pilot.get('enabled')
            result['budget'] = {'status': 'available', 'monthly_budget_usd': amount,
                                'enabled': enabled if type(enabled) is bool else None}
            successes += 1
    except Exception:
        pass
    if successes:
        result['status'] = 'ok'
    return _redact(result, token)


def snapshot(vault=None):
    """Bound provider access to one child across portal workers, or fail closed.

    The persistent lock belongs beside the private vault DB; it is never
    removed while workers can be running. Unsupported platforms fail closed.
    """
    result = _unavailable()
    try:
        token = _token(vault)
    except Exception:
        result['status'] = 'unavailable'
        return result
    if not token:
        return result
    result['status'] = 'unavailable'
    try:
        import fcntl
        if vault is None or not isinstance(getattr(vault, 'path', None), str):
            return result
        directory = os.path.dirname(os.path.abspath(vault.path))
        lock_path = os.path.join(directory, '.content-factory-read.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return result
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return result
            try:
                proc = subprocess.run([sys.executable, '-I', os.path.abspath(__file__), '--worker'],
                    input=token.encode('ascii'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=WALL_TIMEOUT, check=False, env={'PATH': os.defpath}, pass_fds=(fd,))
                if proc.returncode != 0 or len(proc.stdout) > MAX_BYTES:
                    return result
                decoded = json.loads(proc.stdout.decode('utf-8'))
                if not isinstance(decoded, dict) or decoded.get('status') not in ('ok', 'unavailable'):
                    return result
                return _redact(decoded, token)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except Exception:
        return result


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    try:
        import signal
        # Keep the deadline even if the web worker dies and can no longer reap us.
        signal.alarm(WALL_TIMEOUT)
        key = sys.stdin.buffer.read(2049).decode('ascii')
        if not re.fullmatch(r'[!-~]{16,2048}', key):
            raise ValueError('Invalid token')
        sys.stdout.write(json.dumps(_collect(key), ensure_ascii=True))
    except Exception:
        raise SystemExit(1) from None
