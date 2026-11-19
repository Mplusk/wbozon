"""Owner-managed read-only MCP credentials, stored only as hashes."""
import contextlib
import hashlib
import html
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from datetime import datetime, timezone

MAX_ACTIVE_KEYS = 10
TOKEN_PATTERN = re.compile(r'mcp_[A-Za-z0-9_-]{43}\Z')
ID_PATTERN = re.compile(r'(?:(?:av|all|cr|cp)_)?[0-9a-f]{24}\Z')
ACCESS_LABELS = {'marketplaces': 'Ozon и Wildberries', 'avito': 'Авито', 'all': 'Ozon, Wildberries и Авито',
                 'catalog_read':'Полный каталог Ozon и WB · чтение',
                 'catalog_operations':'Полный каталог Ozon и WB · чтение и запросы на изменение'}
PREFIXES = {'marketplaces':'','avito':'av_','all':'all_','catalog_read':'cr_','catalog_operations':'cp_'}
REQUEST_PATTERN = re.compile(r'[A-Za-z0-9_-]{22}\Z')
KEY_COLUMNS = 'public_id,token_hash,name,created_at,revoked_at,last_used_at,request_hash'


class KeyLimitError(ValueError):
    pass


class DuplicateRequestError(ValueError):
    pass


def _valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799


def _valid_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 60 and name == name.strip()
            and not re.search(r'[\x00-\x1f\x7f]', name))

def access_for_key(key_id):
    return next((name for name,prefix in PREFIXES.items() if prefix and key_id.startswith(prefix)), 'marketplaces')


def key_connection(path):
    companion = Path(str(path) + '.avito-keys').resolve().as_uri() + '?mode=rw'
    database = sqlite3.connect(path, timeout=0.5, uri=True)
    try:
        database.execute('ATTACH DATABASE ? AS additional_keys', (companion,))
        catalogue = Path(str(path) + '.catalog-keys').resolve().as_uri() + '?mode=rw'
        database.execute('ATTACH DATABASE ? AS catalog_keys', (catalogue,))
        database.execute('CREATE TEMP VIEW mcp_keys_for_access AS '
                         f'SELECT {KEY_COLUMNS} FROM main.external_mcp_keys UNION ALL '
                         f'SELECT {KEY_COLUMNS} FROM additional_keys.external_mcp_keys UNION ALL '
                         f'SELECT {KEY_COLUMNS} FROM catalog_keys.external_mcp_keys')
        return database
    except Exception:
        database.close()
        raise


def _table(key_id):
    access = access_for_key(key_id)
    if access.startswith('catalog_'):
        return 'catalog_keys.external_mcp_keys'
    return 'main.external_mcp_keys' if access=='marketplaces' else 'additional_keys.external_mcp_keys'


class KeyStore:
    def __init__(self, path):
        self.path = path
        companion = str(path) + '.avito-keys'
        # The previous release can authorize every row in the legacy key table.
        # Keep narrower and combined grants outside that table, including through rollback.
        catalogue = str(path) + '.catalog-keys'
        for sidecar in (companion,catalogue):
            descriptor = os.open(sidecar, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(descriptor)
        for database in (path, companion, catalogue):
            with contextlib.closing(sqlite3.connect(database, timeout=0.5)) as c, c:
                c.execute('''CREATE TABLE IF NOT EXISTS external_mcp_keys (
                public_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
                last_used_at REAL, request_hash TEXT NOT NULL UNIQUE)''')
                c.execute(f'SELECT {KEY_COLUMNS} FROM external_mcp_keys LIMIT 0')

    def create(self, name, request_id, at, access="marketplaces"):
        if not _valid_name(name) or not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id) or not _valid_time(at):
            raise ValueError('Invalid key request')
        if access not in ACCESS_LABELS:
            raise ValueError('Invalid marketplace access')
        token = 'mcp_' + secrets.token_urlsafe(32)
        key_id = PREFIXES[access] + secrets.token_hex(12)
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        request_hash = hashlib.sha256(request_id.encode('ascii')).hexdigest()
        with key_connection(self.path) as c:
            # Serialize the count and insert across application workers.
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM mcp_keys_for_access WHERE request_hash=?', (request_hash,)).fetchone():
                raise DuplicateRequestError('This creation request was already used')
            table = _table(key_id)
            if c.execute('SELECT COUNT(*) FROM '+table+' WHERE revoked_at IS NULL').fetchone()[0] >= MAX_ACTIVE_KEYS:
                raise KeyLimitError('Too many active keys')
            c.execute('INSERT INTO '+table+' VALUES (?,?,?,?,NULL,NULL,?)',
                      (key_id, token_hash, name, at, request_hash))
        return key_id, token

    def revoke(self, key_id, at):
        if not isinstance(key_id, str) or not ID_PATTERN.fullmatch(key_id) or not _valid_time(at):
            raise ValueError('Invalid key')
        with key_connection(self.path) as c:
            result = c.execute('UPDATE '+_table(key_id)+' SET revoked_at=? WHERE public_id=? AND revoked_at IS NULL', (at, key_id))
            return result.rowcount == 1

    def check_token(self, token):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token): return None
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with key_connection(self.path) as c:
            row = c.execute('SELECT public_id FROM mcp_keys_for_access WHERE token_hash=? AND revoked_at IS NULL', (digest,)).fetchone()
        return row[0] if row else None

    def mark_used(self, key_id, at):
        if not isinstance(key_id, str) or not ID_PATTERN.fullmatch(key_id) or not _valid_time(at):
            raise ValueError('Invalid key use')
        with key_connection(self.path) as c:
            # A request completing late must not move the timestamp backwards
            # or be rejected solely because another request was recorded first.
            result = c.execute('UPDATE '+_table(key_id)+'''
                SET last_used_at=MAX(COALESCE(last_used_at,0),?)
                WHERE public_id=? AND revoked_at IS NULL''', (at, key_id))
            return result.rowcount == 1

    def list_active(self):
        with key_connection(self.path) as c:
            return c.execute('SELECT public_id,name,created_at,last_used_at FROM mcp_keys_for_access WHERE revoked_at IS NULL ORDER BY created_at DESC, public_id').fetchall()

    def active(self,key_id):
        with contextlib.closing(key_connection(self.path)) as c:
            return c.execute('SELECT 1 FROM mcp_keys_for_access WHERE public_id=? AND revoked_at IS NULL',(key_id,)).fetchone() is not None


def _escape(value):
    return html.escape(str(value), quote=True)


def _stamp(value):
    if not _valid_time(value): raise ValueError('Invalid timestamp')
    return datetime.fromtimestamp(value, timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')


def render_section(store, csrf, origin):
    """Return the owner settings fragment; no credential is recovered here."""
    request_id = secrets.token_urlsafe(16)
    endpoint = _escape(origin.rstrip('/') + '/mcp')
    content = '<section class="mcp-card" aria-labelledby="external-mcp-title"><div><h2 id="external-mcp-title">Подключение других ИИ</h2>'
    content += '<p>Адрес MCP: <strong>' + endpoint + '</strong>. Тип подключения: Streamable HTTP. Права задаются отдельно при создании ключа.</p>'
    content += '<p>Создайте отдельный ключ для каждого приложения. Передайте его приложению как заголовок <code>Authorization: Bearer КЛЮЧ</code>. Поддержка заголовка зависит от приложения; ссылка без ключа не даёт доступ.</p>'
    content += '<p>Для Claude, Grok и ChatGPT используйте адрес MCP при добавлении коннектора. Если откроется страница подключения SIDZEN, введите на ней созданный ключ и разрешите чтение. Клиенты с ручной настройкой заголовков могут передать ключ через Bearer.</p>'
    content += '<form method="post" action="/settings/mcp/create"><input type="hidden" name="csrf" value="'+_escape(csrf)+'"><input type="hidden" name="request_id" value="'+_escape(request_id)+'"><label for="external-mcp-name">Название приложения</label><input id="external-mcp-name" type="text" name="name" maxlength="60" required><label for="mcp-account-access">Площадки для этого ключа</label><select id="mcp-account-access" name="access"><option value="marketplaces">Ozon и Wildberries</option><option value="avito">Авито</option><option value="all">Ozon, Wildberries и Авито</option></select><button>Создать ключ</button></form>'
    options = ''.join('<option value="'+k+'">'+_escape(v)+'</option>' for k,v in ACCESS_LABELS.items() if k.startswith('catalog_'))
    content = content.replace('</select>',options+'</select>')
    content += '<p>Полный каталог может содержать данные заказов, документы и сведения о покупателях. Запросы на изменение подтверждаются в разделе <a href="/settings/catalog">Операции маркетплейсов</a>.</p>'
    rows = store.list_active()
    if not rows:
        content += '<p>Ключей для других ИИ пока нет.</p>'
    else:
        content += '<h3>Активные ключи</h3>'
        for key_id, name, created_at, last_used_at in rows:
            content += '<div><p><strong>'+_escape(name)+'</strong> · '+ACCESS_LABELS[access_for_key(key_id)]+' — создан '+_escape(_stamp(created_at))
            content += '; последнее обращение: '+(_escape(_stamp(last_used_at)) if last_used_at is not None else 'пока не было')+'.</p>'
            content += '<form class="secondary" method="post" action="/settings/mcp/revoke"><input type="hidden" name="csrf" value="'+_escape(csrf)+'"><input type="hidden" name="key_id" value="'+_escape(key_id)+'"><button>Отозвать ключ</button></form></div>'
    return content + '</div></section>'


def render_created(name, token, origin, access="marketplaces"):
    """Show a newly issued secret in this POST response only; caller must use no-store."""
    if access not in ACCESS_LABELS or not _valid_name(name) or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('Invalid created key')
    return ('<main id="content"><h1>Ключ создан</h1><p>Для приложения: '+_escape(name)+'. Площадки: '+ACCESS_LABELS[access]+'. Скопируйте ключ сейчас: после закрытия этой страницы он больше не показывается.</p>'
        '<label for="external-mcp-url">Адрес MCP</label><input id="external-mcp-url" type="text" readonly value="'+_escape(origin.rstrip('/')+'/mcp')+'">'
        '<label for="external-mcp-token">Ключ доступа</label><input id="external-mcp-token" type="text" readonly value="'+_escape(token)+'" autocomplete="off" spellcheck="false">'
        '<p>Добавьте адрес MCP в Claude, Grok или ChatGPT. На странице подключения SIDZEN введите этот ключ и разрешите доступ. Если клиент запрашивает заголовок, укажите Authorization: Bearer, пробел и ключ. Храните ключ как пароль; вы сможете отозвать его в кабинете.</p><p><a href="/settings">Вернуться к настройкам</a></p></main>')
