import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from content_factory_fence import Fence


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'fence.db')
        self.now = 1000
        self.fence = Fence(self.path, b'x' * 32, clock=lambda: self.now)
        self.owner = 'owner-session-example'
        self.fence.arm(lambda: True)
        self.p = self.fence.prepare(self.owner, 'credential', 'factory-openai', {'key':'synthetic-secret-value'})
        self.assertEqual(self.fence.dispatch(self.p, self.owner, {'key':'synthetic-secret-value'}, lambda: 'unknown'), 'unknown')
        self.fence.halt()
        self.result = {'observation': 'credential_exists_unverified', 'evidence': 'a'*64}
        self.read = lambda operation, target: dict(self.result)

    def tearDown(self): self.tmp.cleanup()

    def test_inspection_and_confirmation_leave_halted(self):
        self.assertEqual(self.fence.inspect_unknown(self.p, self.owner, lambda: True, self.read), 'credential_exists_unverified')
        self.fence.confirm_unknown(self.p, self.owner, lambda: True, self.read, risk_confirmed=True)
        self.assertFalse(self.fence.is_ready())
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT state FROM fence_proposal WHERE id=?', (self.p,)).fetchone()
            audit = str(db.execute('SELECT * FROM fence_audit').fetchall())
        self.assertEqual(row[0], 'owner_acknowledged_unknown')
        self.assertNotIn('synthetic-secret-value', audit)
        with self.assertRaises(PermissionError): self.fence.arm(lambda: True)

    def test_requires_fresh_owner_confirmation_and_identical_observation(self):
        self.fence.inspect_unknown(self.p, self.owner, lambda: True, self.read)
        with self.assertRaises(PermissionError): self.fence.confirm_unknown(self.p, self.owner, lambda: True, self.read)
        with self.assertRaises(PermissionError): self.fence.confirm_unknown(self.p, 'another-owner-identity', lambda: True, self.read, risk_confirmed=True)
        self.result['evidence'] = 'b'*64
        with self.assertRaises(PermissionError): self.fence.confirm_unknown(self.p, self.owner, lambda: True, self.read, risk_confirmed=True)
        self.result['evidence'] = 'a'*64
        self.now += 301
        with self.assertRaises(PermissionError): self.fence.confirm_unknown(self.p, self.owner, lambda: True, self.read, risk_confirmed=True)

    def test_unavailable_or_malformed_reader_stays_blocked(self):
        with self.assertRaises(PermissionError): self.fence.inspect_unknown(self.p, self.owner, lambda: True, lambda *_: {'observation':'verified'})
        with self.assertRaises(PermissionError): self.fence.inspect_unknown(self.p, self.owner, lambda: False, self.read)
        with self.assertRaises(PermissionError): self.fence.arm(lambda: True)


if __name__ == '__main__': unittest.main()
