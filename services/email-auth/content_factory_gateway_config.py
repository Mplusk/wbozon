"""Explicitly opt in to the factory gateway with isolated operator config.

The caller must provision a dedicated empty data directory and a separate
32-byte HMAC key, verify an isolated end-to-end staging run, and obtain owner
confirmation that the API token can read every project and its archives.
Configuration never enables writes: Fence starts halted until owner action.
"""
import os
import hmac
import sqlite3
from contextlib import closing
from content_factory_gateway_wiring import build


def _dedicated_directory(path, protected=()):
    if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path) or os.path.islink(path):
        raise ValueError('Dedicated gateway directory required')
    resolved = os.path.realpath(path)
    allowed = {'content-factory-gateway.sqlite3', 'content-factory-gateway.sqlite3-journal',
               'content-factory-gateway.sqlite3-shm', 'content-factory-gateway.sqlite3-wal'}
    if set(os.listdir(resolved)) - allowed: raise ValueError('Gateway directory is not dedicated')
    db_path = os.path.join(resolved, 'content-factory-gateway.sqlite3')
    if os.path.lexists(db_path) and (os.path.islink(db_path) or not os.path.isfile(db_path)):
        raise ValueError('Invalid gateway database')
    if os.path.exists(db_path):
        for other in protected:
            if isinstance(other, str) and os.path.exists(other) and os.path.samefile(db_path, other):
                raise ValueError('Gateway database must be separate')
        # Do not initialize or mutate an existing unrelated SQLite database.
        uri = 'file:' + db_path.replace('\\', '/').replace('?', '%3F').replace('#', '%23') + '?mode=ro'
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'fence_control', 'fence_proposal', 'fence_audit'}
            allowed_tables = {'fence_reconciliation', 'sqlite_sequence', 'cf_live_budget_snapshot',
                              'cf_live_budget_proposals', 'cf_live_budget_refresh',
                              'cf_cutover_jobs', 'cf_cutover_job_audit'}
            if not required.issubset(tables) or tables - required - allowed_tables:
                raise ValueError('Invalid gateway database')
        except (sqlite3.Error, OSError):
            raise ValueError('Invalid gateway database') from None
    return db_path


def configure(environ, vault, secret_reader):
    enabled = environ.get('CF_GATEWAY_ENABLED', '0')
    if enabled not in ('0', '1'): raise ValueError('Invalid gateway flag')
    if enabled == '0': return None, None, False
    if environ.get('CF_GATEWAY_STAGING_VERIFIED') != '1' or environ.get('CF_GATEWAY_OWNER_SCOPE_CONFIRMED') != '1':
        raise ValueError('Staging verification and owner scope confirmation required')
    db_path = _dedicated_directory(environ.get('CF_GATEWAY_DATA_DIR'),
                                   (environ.get('DATABASE_PATH'), environ.get('VAULT_PATH')))
    ids = {'openai':environ.get('CF_N8N_OPENAI_ID'), 'gemini':environ.get('CF_N8N_GEMINI_ID')}
    if not environ.get('CF_GATEWAY_HMAC_KEY_FILE') or not environ.get('VAULT_KEY_FILE'):
        raise ValueError('Separate gateway HMAC key required')
    if os.path.realpath(environ['CF_GATEWAY_HMAC_KEY_FILE']) == os.path.realpath(environ['VAULT_KEY_FILE']):
        raise ValueError('Separate gateway HMAC key required')
    if (os.path.exists(environ['CF_GATEWAY_HMAC_KEY_FILE']) and os.path.exists(environ['VAULT_KEY_FILE'])
            and os.path.samefile(environ['CF_GATEWAY_HMAC_KEY_FILE'], environ['VAULT_KEY_FILE'])):
        raise ValueError('Separate gateway HMAC key required')
    key = secret_reader('CF_GATEWAY_HMAC_KEY_FILE', True)
    if not isinstance(key, bytes) or len(key) != 32: raise ValueError('Invalid gateway HMAC key')
    vault_key = secret_reader('VAULT_KEY_FILE', True)
    if not isinstance(vault_key, bytes) or hmac.compare_digest(key, vault_key):
        raise ValueError('Separate gateway HMAC key required')
    control, reader = build(vault, db_path=db_path, hmac_key=key, credential_ids=ids, owner_scope_confirmed=True,
                            reader_id=environ.get('CF_N8N_MODEL_READER_ID'))
    return control, reader, True
