"""Credential change service; not enabled or instantiated by default.

The deployment must supply an isolation verifier backed by actual n8n metadata
and a separately scoped token. Never use a permissive verifier in production.
"""
import re
from content_factory_fence import Fence
from content_factory_n8n_write import bounded_update, bounded_provision


class CredentialControl:
    def __init__(self, db_path, hmac_key, *, credential_ids, token_reader, isolation_verifier, transport=bounded_update,
                 provision_verifier=None, provision_transport=bounded_provision, configuration_ready=lambda: True):
        if not isinstance(credential_ids, dict) or set(credential_ids) != {'openai', 'gemini'}:
            raise ValueError('Both isolated credentials required')
        if (any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9]{8,64}', v) for v in credential_ids.values())
                or len(set(credential_ids.values())) != 2):
            raise ValueError('Invalid isolated credentials')
        if not all(callable(x) for x in (token_reader, isolation_verifier, transport, configuration_ready)):
            raise ValueError('Invalid gateway configuration')
        self.ids = dict(credential_ids)
        self.configuration_ready = configuration_ready
        self.token_reader, self.isolation_verifier, self.transport = token_reader, isolation_verifier, transport
        if provision_verifier is not None and (not callable(provision_verifier) or not callable(provision_transport)):
            raise ValueError('Invalid provisioning policy')
        self.provision_verifier, self.provision_transport = provision_verifier, provision_transport
        self.fence = Fence(db_path, hmac_key)

    def _isolated(self, provider):
        if provider not in self.ids: raise ValueError('Unknown provider')
        # Check dynamically before proposal and again at dispatch time.
        return self.isolation_verifier(provider, self.ids[provider]) is True

    def ready(self):
        try:
            # No network access in the page-render path. The complete isolation
            # scan runs at prepare and again immediately before dispatch.
            if self.configuration_ready() is not True or not self.fence.is_ready(): return False
            token = self.token_reader()
            return (isinstance(token, str)
                    and bool(re.fullmatch(r'[!-~]{16,2048}', token)))
        except Exception:
            return False

    def prepare_credential(self, owner, provider):
        if not self.ready() or not self._isolated(provider): raise PermissionError('Gateway unavailable')
        binding = {'provider': provider, 'credential_id': self.ids[provider]}
        return self.fence.prepare(owner, 'credential', 'factory-' + provider, binding)

    def commit_credential(self, owner, proposal, provider, key):
        if not isinstance(key, str) or not re.fullmatch(r'[!-~]{16,2048}', key) or key.startswith('='):
            raise ValueError('Invalid key')
        if provider not in self.ids: raise ValueError('Unknown provider')
        binding = {'provider': provider, 'credential_id': self.ids[provider]}
        def send():
            if not self._isolated(provider): return 'unknown'
            token = self.token_reader()
            return self.transport(provider, self.ids[provider], token, key)
        return self.fence.dispatch(proposal, owner, binding, send)

    def prepare_provision(self, owner, provider):
        if provider not in self.ids: raise ValueError('Unknown provider')
        ident = self.ids[provider]
        if (not self.fence.is_ready() or self.provision_verifier is None
                or len(ident) > 16 or self.provision_verifier(provider, ident) is not True):
            raise PermissionError('Provisioning unavailable')
        binding = {'operation': 'provision', 'provider': provider, 'credential_id': ident}
        return self.fence.prepare(owner, 'credential', 'factory-' + provider, binding)

    def commit_provision(self, owner, proposal, provider, key):
        if provider not in self.ids or not isinstance(key, str) or not re.fullmatch(r'[!-~]{16,2048}', key) or key.startswith('='):
            raise ValueError('Invalid input')
        if self.provision_verifier is None: raise PermissionError('Provisioning unavailable')
        ident = self.ids[provider]
        if len(ident) > 16: raise PermissionError('Provisioning unavailable')
        binding = {'operation': 'provision', 'provider': provider, 'credential_id': ident}
        def send():
            if self.provision_verifier(provider, ident) is not True: return 'unknown'
            token = self.token_reader()
            return self.provision_transport(provider, ident, token, key, proposal)
        return self.fence.dispatch(proposal, owner, binding, send)
