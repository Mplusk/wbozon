"""One-shot staging entry point; invoked by an isolated, bounded scheduler.

Secrets come only from the existing vault and its key file, never CLI args.
Set a service manager deadline; this process does not install a timer.
"""
import os
import signal
import sqlite3
import sys
import json
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class _ReadOnlyVault:
    """Read only the two catalogue slots; do not initialize or modify vault schema."""
    _slots = frozenset(('content_factory_catalog_openai', 'content_factory_catalog_gemini', 'content_factory_catalog_setup'))

    def __init__(self, path, key):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError('Invalid vault key')
        self.path, self._aead = path, AESGCM(key)

    def read(self, provider):
        if provider not in self._slots:
            raise ValueError('Invalid catalogue slot')
        with closing(sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            row = db.execute('SELECT CASE WHEN length(blob) BETWEEN 29 AND 8192 THEN blob ELSE NULL END FROM vault WHERE provider=?', (provider,)).fetchone()
        if row is None:
            return None
        blob = row[0]
        if not isinstance(blob, bytes) or not 29 <= len(blob) <= 8192:
            raise ValueError('Invalid stored value')
        value = json.loads(self._aead.decrypt(blob[:12], blob[12:], provider.encode()))
        if provider == 'content_factory_catalog_setup':
            if (not isinstance(value, dict) or set(value) != {'version', 'enabled'}
                    or type(value['version']) is not int or value['version'] != 1 or value['enabled'] is not True):
                raise ValueError('Invalid catalogue setup')
            return value
        if not isinstance(value, dict) or set(value) != {'api_key'} or not isinstance(value['api_key'], str) or not 8 <= len(value['api_key']) <= 2048:
            raise ValueError('Invalid stored value')
        return value


def main():
    if len(sys.argv) != 1: return 2
    if os.environ.get('CF_MODEL_CATALOG_ENABLED', '0') != '1': return 0
    # systemd must also set TimeoutStartSec. This caps a stalled runner on POSIX;
    # a forced exit releases SQLite's transaction without logging secrets.
    if hasattr(signal, 'alarm'): signal.alarm(450)
    try:
        from portal import secret_file
        import content_factory_model_catalog_config as config
        path = os.environ['VAULT_PATH']
        if not os.path.isabs(path) or not os.path.isfile(path) or os.path.islink(path): return 1
        # Reject an absent or wrong vault before opening its two catalogue slots.
        with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'vault', 'connection_checks', 'bridge_activity'}.issubset(tables): return 1
        vault = _ReadOnlyVault(path, secret_file('VAULT_KEY_FILE', True))
        # The catalogue can only run after the owner has completed opt-in.
        if vault.read('content_factory_catalog_setup') is None:
            return 0
        outcome = config.run(os.environ, vault, secret_file)
        return 0 if outcome['status'] in ('attempted', 'already_attempted', 'busy', 'not_due') else 1
    except Exception:
        # Do not log exception text: transport errors can contain credentials.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
