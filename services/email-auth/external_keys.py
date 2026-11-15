"""Owner-managed read-only MCP credentials, stored only as hashes."""
import hashlib
import html
import math
import re
import secrets
import sqlite3
from datetime import datetime, timezone

MAX_ACTIVE_KEYS = 10
TOKEN_PATTERN = re.compile(r'mcp_[A-Za-z0-9_-]{43}\Z')
ID_PATTERN = re.compile(r'[0-9a-f]{24}\Z')
REQUEST_PATTERN = re.compile(r'[A-Za-z0-9_-]{22}\Z')


class KeyLimitError(ValueError):
    pass


class DuplicateRequestError(ValueError):
    pass


def _valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799


def _valid_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 60 and name == name.strip()
            and not re.search(r'[\x00-\x1f\x7f]', name))


class KeyStore:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path, timeout=0.5) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS external_mcp_keys (
                public_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
                last_used_at REAL, request_hash TEXT NOT NULL UNIQUE)''')

    def create(self, name, request_id, at):
        if not _valid_name(name) or not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id) or not _valid_time(at):
            raise ValueError('Invalid key request')
        token = 'mcp_' + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(12)
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        request_hash = hashlib.sha256(request_id.encode('ascii')).hexdigest()
        with sqlite3.connect(self.path, timeout=0.5) as c:
            # Serialize the count and insert across application workers.
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM external_mcp_keys WHERE request_hash=?', (request_hash,)).fetchone():
                raise DuplicateRequestError('This creation request was already used')
            if c.execute('SELECT COUNT(*) FROM external_mcp_keys WHERE revoked_at IS NULL').fetchone()[0] >= MAX_ACTIVE_KEYS:
                raise KeyLimitError('Too many active keys')
            c.execute('INSERT INTO external_mcp_keys VALUES (?,?,?,?,NULL,NULL,?)',
                      (key_id, token_hash, name, at, request_hash))
        return key_id, token

    def revoke(self, key_id, at):
        if not isinstance(key_id, str) or not ID_PATTERN.fullmatch(key_id) or not _valid_time(at):
            raise ValueError('Invalid key')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            result = c.execute('UPDATE external_mcp_keys SET revoked_at=? WHERE public_id=? AND revoked_at IS NULL', (at, key_id))
            return result.rowcount == 1

    def check_token(self, token):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token): return None
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with sqlite3.connect(self.path, timeout=0.5) as c:
            row = c.execute('SELECT public_id FROM external_mcp_keys WHERE token_hash=? AND revoked_at IS NULL', (digest,)).fetchone()
        return row[0] if row else None

    def mark_used(self, key_id, at):
        if not isinstance(key_id, str) or not ID_PATTERN.fullmatch(key_id) or not _valid_time(at):
            raise ValueError('Invalid key use')
        with sqlite3.connect(self.path, timeout=0.5) as c:
            # A request completing late must not move the timestamp backwards
            # or be rejected solely because another request was recorded first.
            result = c.execute('''UPDATE external_mcp_keys
                SET last_used_at=MAX(COALESCE(last_used_at,0),?)
                WHERE public_id=? AND revoked_at IS NULL''', (at, key_id))
            return result.rowcount == 1

    def list_active(self):
        with sqlite3.connect(self.path, timeout=0.5) as c:
            return c.execute('SELECT public_id,name,created_at,last_used_at FROM external_mcp_keys WHERE revoked_at IS NULL ORDER BY created_at DESC, public_id').fetchall()


def _escape(value):
    return html.escape(str(value), quote=True)


def _stamp(value):
    if not _valid_time(value): raise ValueError('Invalid timestamp')
    return datetime.fromtimestamp(value, timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')


def render_section(store, csrf, editable, origin):
    """Return the owner settings fragment; no credential is recovered here."""
    request_id = secrets.token_urlsafe(16)
    disabled = '' if editable else ' disabled'
    endpoint = _escape(origin.rstrip('/') + '/mcp')
    content = '<section class="mcp-card" aria-labelledby="external-mcp-title"><div><h2 id="external-mcp-title">Подключение других ИИ</h2>'
    content += '<p>Адрес MCP: <strong>' + endpoint + '</strong>. Тип подключения: Streamable HTTP. Доступ только для чтения.</p>'
    content += '<p>Создайте отдельный ключ для каждого приложения. Передайте его приложению как заголовок <code>Authorization: Bearer КЛЮЧ</code>. Поддержка заголовка зависит от приложения; ссылка без ключа не даёт доступ.</p>'
    content += '<p>Claude: в настройках собственного коннектора выберите отсутствие входа и добавьте этот заголовок в Request headers. Grok: используйте клиент или API, поддерживающий заголовки MCP. Для ChatGPT уже подключено приложение SIDZEN · WB и Ozon MCP.</p>'
    if not editable:
        content += '<p>Чтобы создать или отозвать ключ, войдите заново по ссылке из письма.</p>'
    content += '<form method="post" action="/settings/mcp/create"><input type="hidden" name="csrf" value="'+_escape(csrf)+'"><input type="hidden" name="request_id" value="'+_escape(request_id)+'"><label for="external-mcp-name">Название приложения</label><input id="external-mcp-name" type="text" name="name" maxlength="60" required'+disabled+'><button'+disabled+'>Создать ключ</button></form>'
    rows = store.list_active()
    if not rows:
        content += '<p>Ключей для других ИИ пока нет.</p>'
    else:
        content += '<h3>Активные ключи</h3>'
        for key_id, name, created_at, last_used_at in rows:
            content += '<div><p><strong>'+_escape(name)+'</strong> — создан '+_escape(_stamp(created_at))
            content += '; последнее обращение: '+(_escape(_stamp(last_used_at)) if last_used_at is not None else 'пока не было')+'.</p>'
            content += '<form class="secondary" method="post" action="/settings/mcp/revoke"><input type="hidden" name="csrf" value="'+_escape(csrf)+'"><input type="hidden" name="key_id" value="'+_escape(key_id)+'"><button'+disabled+'>Отозвать ключ</button></form></div>'
    return content + '</div></section>'


def render_created(name, token, origin):
    """Show a newly issued secret in this POST response only; caller must use no-store."""
    if not _valid_name(name) or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('Invalid created key')
    return ('<main id="content"><h1>Ключ создан</h1><p>Для приложения: '+_escape(name)+'. Скопируйте ключ сейчас: после закрытия этой страницы он больше не показывается.</p>'
        '<label for="external-mcp-url">Адрес MCP</label><input id="external-mcp-url" type="text" readonly value="'+_escape(origin.rstrip('/')+'/mcp')+'">'
        '<label for="external-mcp-token">Ключ доступа</label><input id="external-mcp-token" type="text" readonly value="'+_escape(token)+'" autocomplete="off" spellcheck="false">'
        '<p>Если приложение запрашивает заголовок, укажите Authorization: Bearer, пробел и этот ключ. Храните ключ как пароль; вы сможете отозвать его в кабинете.</p><p><a href="/settings">Вернуться к настройкам</a></p></main>')
