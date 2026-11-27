"""Explicit opt-in budget composition; does not arm or grant n8n scopes."""
import os
import hmac
from content_factory_gateway_config import _dedicated_directory
from content_factory_gateway_wiring import token_reader
from content_factory_n8n_budget import LiveBudgetGateway


def configure_budget(environ, vault, secret_reader, credential_control=None):
    enabled = environ.get('CF_BUDGET_GATEWAY_ENABLED','0')
    if enabled not in ('0','1'): raise ValueError('Invalid budget gateway flag')
    if enabled == '0': return None
    if (environ.get('CF_BUDGET_STAGING_VERIFIED') != '1' or
        environ.get('CF_BUDGET_SCOPE_CONFIRMED') != '1'):
        raise ValueError('Budget staging and owner scope confirmation required')
    db_path = _dedicated_directory(environ.get('CF_GATEWAY_DATA_DIR'),
                                   (environ.get('DATABASE_PATH'), environ.get('VAULT_PATH')))
    key_file = environ.get('CF_GATEWAY_HMAC_KEY_FILE')
    vault_file = environ.get('VAULT_KEY_FILE')
    if not key_file or not vault_file or os.path.realpath(key_file) == os.path.realpath(vault_file):
        raise ValueError('Separate gateway HMAC key required')
    if os.path.exists(key_file) and os.path.exists(vault_file) and os.path.samefile(key_file,vault_file):
        raise ValueError('Separate gateway HMAC key required')
    key = secret_reader('CF_GATEWAY_HMAC_KEY_FILE',True)
    vault_key = secret_reader('VAULT_KEY_FILE',True)
    if (not isinstance(key,bytes) or len(key) != 32 or not isinstance(vault_key,bytes) or len(vault_key) != 32
            or hmac.compare_digest(key,vault_key)):
        raise ValueError('Separate gateway HMAC key required')
    if credential_control is not None:
        other = credential_control.fence
        if os.path.realpath(other.path) != os.path.realpath(db_path) or other.key != key:
            raise ValueError('Gateway fence mismatch')
    def configured():
        # No network calls: owner must provision a separate token. Real scope
        # is still checked by n8n on each API request, not inferred here.
        return 'n8n_content_factory_write' in vault.present()
    return LiveBudgetGateway(db_path, key, token_reader=token_reader(vault),
                             configuration_ready=configured)
