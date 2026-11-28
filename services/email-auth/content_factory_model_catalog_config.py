"""Disabled-by-default composition for the standalone catalogue scheduler.

This is a staging-only integration boundary. It creates no network activity.
"""
import hashlib
import hmac
import os
import sqlite3
import stat
from contextlib import closing

import content_factory_model_catalog
import content_factory_model_schedule

CATALOG = 'catalog.sqlite3'
LOCK = 'schedule.sqlite3'


def _inside(a, b):
    try:
        a, b = os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b))
        return os.path.commonpath((a, b)) == b
    except (TypeError, ValueError):
        return False


def configure(environ, vault, secret_reader, *, read_only=False):
    flag = environ.get('CF_MODEL_CATALOG_ENABLED', '0')
    if flag not in ('0', '1'): raise ValueError('Invalid catalogue flag')
    if flag == '0': return None
    if environ.get('CF_MODEL_CATALOG_STAGING_VERIFIED') != '1':
        raise ValueError('Catalogue staging verification required')
    directory = environ.get('CF_MODEL_CATALOG_DATA_DIR')
    if not isinstance(directory, str) or not os.path.isabs(directory) or not os.path.isdir(directory) or os.path.islink(directory):
        raise ValueError('Dedicated catalogue directory required')
    directory = os.path.realpath(directory)
    paths = [environ.get('DATABASE_PATH'), environ.get('VAULT_PATH'), getattr(vault, 'path', None),
             environ.get('CF_GATEWAY_DATA_DIR'), environ.get('CF_IMMUTABLE_DATA_DIR')]
    vault_path = getattr(vault, 'path', None)
    if isinstance(vault_path, str):
        paths.append(os.path.join(os.path.dirname(vault_path), '.content-factory-budget-gateway'))
    # Directories may share a parent, but no catalogue data may contain or be
    # contained by an existing gateway directory, nor contain vault/auth files.
    for item in paths:
        if not isinstance(item, str) or not os.path.isabs(item): continue
        if _inside(item, directory) or (os.path.isdir(item) and _inside(directory, item)):
            raise ValueError('Catalogue storage must be separate')
    allowed = {name + suffix for name in (CATALOG, LOCK) for suffix in ('', '-journal', '-wal', '-shm')}
    if set(os.listdir(directory)) - allowed: raise ValueError('Catalogue directory must be dedicated')
    for name in allowed:
        p = os.path.join(directory, name)
        if os.path.lexists(p):
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError('Invalid catalogue file')
    cat_path, lock_path = (os.path.join(directory, name) for name in (CATALOG, LOCK))
    allowed_schemas = ((cat_path, ({'catalogue'},)),
                       (lock_path, ({'catalogue_runs'}, {'catalogue_runs', 'catalogue_attempts'})))
    for p, expected in allowed_schemas:
        if os.path.exists(p):
            try:
                from pathlib import Path
                with closing(sqlite3.connect(Path(p).as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables not in expected: raise ValueError('Invalid catalogue database')
            except sqlite3.Error:
                raise ValueError('Invalid catalogue database') from None
    vault_key = secret_reader('VAULT_KEY_FILE', True)
    if not isinstance(vault_key, bytes) or len(vault_key) != 32: raise ValueError('Invalid vault key')
    binding_key = hmac.new(vault_key, b'3dprinta:model-catalogue:v1', hashlib.sha256).digest()
    catalogue = content_factory_model_catalog.Catalog(cat_path, binding_key, read_only=read_only)
    return catalogue, lock_path


def run(environ, vault, secret_reader):
    configured = configure(environ, vault, secret_reader)
    if configured is None: return {'status': 'disabled'}
    catalogue, lock = configured
    return content_factory_model_schedule.run_once(vault, catalogue, lock)
