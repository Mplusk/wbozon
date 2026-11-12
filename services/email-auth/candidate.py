"""Explicit isolated candidate entrypoint; not the production entrypoint.
Generates only synthetic tmpfs secrets, then starts the real portal factory.
"""
import hashlib
import os
from pathlib import Path
import secrets

if __name__ == '__main__':
    os.umask(0o077)
    base=Path('/data/candidate-secrets'); base.mkdir(mode=0o700)
    for name, value in {
        'SESSION_SECRET_FILE':secrets.token_hex(32).encode(),
        'VAULT_KEY_FILE':secrets.token_bytes(32),
        'READ_TOKEN_HASH_FILE':hashlib.sha256(secrets.token_bytes(32)).hexdigest().encode(),
        'SMTP_PASSWORD_FILE':b'synthetic-not-a-credential',
    }.items():
        target=base/name; target.write_bytes(value); os.environ[name]=str(target)
    os.environ.update(DATABASE_PATH='/data/test.sqlite',VAULT_PATH='/data/vault.sqlite',BASE_URL='https://3dprinta.shop')
    os.execvp('gunicorn',['gunicorn','--config','gunicorn.conf.py','--bind','0.0.0.0:8000','portal:create_portal()'])
