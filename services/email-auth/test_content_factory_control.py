import os
import tempfile
import unittest
from content_factory_control import CredentialControl


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.ok = True
        self.gateway = CredentialControl(os.path.join(self.tmp.name, 'fence.db'), b'x'*32,
            credential_ids={'openai':'A123456789', 'gemini':'B123456789'},
            token_reader=lambda: 't'*32,
            isolation_verifier=lambda provider, ident: self.ok,
            transport=lambda *args: self.calls.append(args[:2]) or 'verified')
        self.owner = 'authenticated-owner-digest'

    def tearDown(self): self.tmp.cleanup()

    def test_closed_until_owner_arms(self):
        self.assertFalse(self.gateway.ready())
        with self.assertRaises(PermissionError): self.gateway.prepare_credential(self.owner, 'openai')
        with self.assertRaises(PermissionError): self.gateway.fence.arm(lambda: False)
        self.gateway.fence.arm(lambda: True)
        self.assertTrue(self.gateway.ready())

    def test_proposal_bound_to_provider_and_owner(self):
        self.gateway.fence.arm(lambda: True)
        proposal = self.gateway.prepare_credential(self.owner, 'openai')
        with self.assertRaises(PermissionError): self.gateway.commit_credential(self.owner, proposal, 'gemini', 'k'*32)
        with self.assertRaises(PermissionError): self.gateway.commit_credential('other-owner-id-123', proposal, 'openai', 'k'*32)
        self.assertEqual(self.gateway.commit_credential(self.owner, proposal, 'openai', 'k'*32), 'verified')
        self.assertEqual(self.calls, [('openai', 'A123456789')])
        with self.assertRaises(PermissionError): self.gateway.commit_credential(self.owner, proposal, 'openai', 'k'*32)

    def test_isolation_rechecked_and_unknown_blocks(self):
        self.gateway.fence.arm(lambda: True)
        proposal = self.gateway.prepare_credential(self.owner, 'openai')
        self.ok = False
        self.assertEqual(self.gateway.commit_credential(self.owner, proposal, 'openai', 'k'*32), 'unknown')
        self.assertFalse(self.gateway.ready())
        self.assertEqual(self.calls, [])

    def test_expiry_rechecked_after_claim(self):
        times = iter([100, 100, 500, 500])
        self.gateway.fence.clock = lambda: next(times)
        self.gateway.fence.arm(lambda: True)
        proposal = self.gateway.prepare_credential(self.owner, 'openai')
        self.assertEqual(self.gateway.commit_credential(self.owner, proposal, 'openai', 'k'*32), 'cancelled')
        self.assertEqual(self.calls, [])

    def test_acceptance_is_not_reported_as_verified(self):
        self.gateway.transport = lambda *args: 'accepted_unconfirmed'
        self.gateway.fence.arm(lambda: True)
        proposal = self.gateway.prepare_credential(self.owner, 'openai')
        self.assertEqual(self.gateway.commit_credential(self.owner, proposal, 'openai', 'k'*32), 'accepted_unconfirmed')
        self.assertTrue(self.gateway.ready())

    def test_provisioning_is_explicit_and_fenced(self):
        self.gateway.fence.arm(lambda: True)
        with self.assertRaises(PermissionError): self.gateway.prepare_provision(self.owner, 'openai')
        calls = []
        self.gateway.provision_verifier = lambda provider, ident: self.ok
        self.gateway.provision_transport = lambda *args: calls.append((args[0], args[1], args[4])) or 'accepted_unconfirmed'
        proposal = self.gateway.prepare_provision(self.owner, 'openai')
        self.assertEqual(self.gateway.commit_provision(self.owner, proposal, 'openai', 'k'*32), 'accepted_unconfirmed')
        self.assertEqual(calls, [('openai', 'A123456789', proposal)])
        with self.assertRaises(PermissionError): self.gateway.commit_provision(self.owner, proposal, 'openai', 'k'*32)

    def test_provisioning_preflight_change_blocks_send(self):
        self.gateway.fence.arm(lambda: True)
        self.gateway.provision_verifier = lambda provider, ident: self.ok
        self.gateway.provision_transport = lambda *args: self.calls.append('sent') or 'accepted_unconfirmed'
        proposal = self.gateway.prepare_provision(self.owner, 'openai')
        self.ok = False
        self.assertEqual(self.gateway.commit_provision(self.owner, proposal, 'openai', 'k'*32), 'unknown')
        self.assertEqual(self.calls, [])


if __name__ == '__main__': unittest.main()
