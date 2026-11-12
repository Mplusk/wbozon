import os
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from gateway.app import create_app, create_from_env
from gateway.store import GatewayStore, _fingerprint

OWNER = 'owner-' + '1' * 40
AGENT = 'agent-' + '2' * 40


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'gateway.db')
        self.app = create_app(db_path=self.path, owner_token=OWNER, agent_token=AGENT)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.action = {'operation': 'change_price', 'item_id': 'sku-1', 'parameters': {'new_price': '100'}}

    def post(self, path, *, token=AGENT, key='action-123', json=None):
        return self.client.post(path, json=json, headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': key})

    def test_factory_requires_separate_strong_tokens_and_absolute_database(self):
        for a, b in [(OWNER, OWNER), ('short', AGENT), (None, AGENT), ('т' * 33, AGENT)]:
            with self.subTest(a=a), self.assertRaises(ValueError):
                create_app(db_path=self.path, owner_token=a, agent_token=b)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            create_from_env()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_authorization_and_owner_only_stop(self):
        self.assertEqual(self.client.get('/v1/status').status_code, 401)
        self.assertEqual(self.client.get('/v1/status', headers={'Authorization': 'Bearer ' + 'x' * 40}).status_code, 401)
        self.assertEqual(self.post('/v1/stop').status_code, 403)
        self.assertFalse(GatewayStore(self.path).status()['stopped'])
        response = self.post('/v1/stop', token=OWNER, key='owner-stop-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'stopped': True, 'probe_denied': True})
        self.assertEqual(self.post('/v1/stop', token=OWNER, key='owner-stop-1').json(),
                         {'stopped': True, 'probe_denied': True})

    def test_default_readonly_denies_and_persists_after_restart(self):
        result = self.post('/v1/actions', json=self.action)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.json()['reason'], 'READ_ONLY')
        self.assertTrue(create_app(db_path=self.path, owner_token=OWNER, agent_token=AGENT))
        self.assertTrue(GatewayStore(self.path).status()['read_only'])
        self.post('/v1/stop', token=OWNER, key='owner-stop-2')
        self.assertEqual(GatewayStore(self.path).attempt('another-1', 'change_price', {}, 'agent')['reason'], 'STOPPED')
        self.assertTrue(GatewayStore(self.path).status()['stopped'])

    def test_idempotency_same_payload_replays_but_different_conflicts(self):
        one = self.post('/v1/actions', json=self.action)
        two = self.post('/v1/actions', json=self.action)
        other = self.post('/v1/actions', json={**self.action, 'item_id': 'other'})
        self.assertFalse(one.json()['replayed'])
        self.assertTrue(two.json()['replayed'])
        self.assertEqual(other.status_code, 409)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gateway_actions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gateway_audit").fetchone()[0], 2)

    def test_unknown_or_started_request_is_never_reexecuted(self):
        request_id = 'unknown-result-1'
        fingerprint = _fingerprint('change_price', self.action)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO gateway_actions VALUES (?,?,?,?,?,?,?)',
                       (hashlib.sha256(request_id.encode()).hexdigest(), 'change_price', fingerprint,
                        'unknown', 'RECONCILIATION_REQUIRED', 'old', 'old'))
        response = self.post('/v1/actions', key=request_id, json=self.action)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'unknown')
        self.assertTrue(response.json()['replayed'])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM gateway_audit').fetchone()[0], 0)

    def test_stop_probe_and_action_journal_do_not_store_credentials_or_payload(self):
        secret_text = 'some-untrusted-body-value'
        req = {**self.action, 'parameters': {'value': secret_text}}
        self.post('/v1/actions', token=AGENT, key='private-request-id', json=req)
        self.post('/v1/stop', token=OWNER, key='owner-stop-3')
        blob = Path(self.path).read_bytes()
        for value in (OWNER, AGENT, secret_text, 'private-request-id', 'owner-stop-3'):
            self.assertNotIn(value.encode(), blob)
        with sqlite3.connect(self.path) as db:
            events = [row[0] for row in db.execute('SELECT event FROM gateway_audit')]
        self.assertEqual(events, ['attempt_started', 'attempt_denied', 'stop', 'attempt_started', 'attempt_denied'])

    def test_audit_owner_only_and_paginated(self):
        self.post('/v1/actions', json=self.action)
        headers = {'Authorization': 'Bearer ' + OWNER}
        self.assertEqual(self.client.get('/v1/audit', headers={'Authorization': 'Bearer ' + AGENT}).status_code, 403)
        events = self.client.get('/v1/audit?limit=1', headers=headers).json()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'attempt_started')
        self.assertEqual(self.client.get('/v1/audit?after_id=1', headers=headers).json()['events'][0]['event'], 'attempt_denied')
        self.assertEqual(self.client.get('/v1/audit?limit=101', headers=headers).status_code, 422)

    def test_bypassed_flag_still_has_no_adapter(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE gateway_state SET readonly=0')
        response = self.post('/v1/actions', json=self.action)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['reason'], 'NO_VERIFIED_ADAPTER')

    def test_invalid_payload_does_not_echo_untrusted_value(self):
        value = 'private-text-that-must-not-appear'
        response = self.post('/v1/actions', json={**self.action, 'operation': value})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(value, response.text)

    def test_nested_coordination_fails_closed_and_releases(self):
        store = GatewayStore(self.path)
        with store.coordination_lock():
            with self.assertRaises(RuntimeError):
                store.stop_and_probe('nested-stop-01')
            with self.assertRaises(RuntimeError):
                with GatewayStore(str(Path(self.directory.name) / 'another.db')).coordination_lock():
                    pass
            self.assertFalse(store.status()['stopped'])
        self.assertTrue(store.stop_and_probe('nested-stop-01')['probe_denied'])

    def test_stop_waits_for_another_process_holding_guard(self):
        store = GatewayStore(self.path)
        code = ('import sys; from gateway.store import GatewayStore; '
                's=GatewayStore(sys.argv[1]); print("ready", flush=True); '
                'r=s.stop_and_probe("cross-process-stop"); print(r["probe_denied"], flush=True)')
        with store.coordination_lock():
            process = subprocess.Popen([sys.executable, '-c', code, self.path], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.2)
                self.assertFalse(store.status()['stopped'])
            finally:
                if process.poll() is not None and process.returncode != 0:
                    process.communicate()
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn('True', out)
        self.assertTrue(store.status()['stopped'])

    def test_insecure_directory_and_linked_database_rejected(self):
        insecure = Path(self.directory.name) / 'insecure'
        insecure.mkdir(mode=0o777)
        insecure.chmod(0o777)
        with self.assertRaises(ValueError):
            GatewayStore(insecure / 'db')
        link = Path(self.directory.name) / 'hardlink'
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            GatewayStore(link)

    def test_concurrent_calls_single_action_and_stop_survives_restart(self):
        barrier = threading.Barrier(6)
        results = []
        def attempt(i):
            barrier.wait()
            results.append(GatewayStore(self.path).attempt('same-key-123', 'change_price', {'price': 1}, 'agent'))
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 6)
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        self.assertTrue(GatewayStore(self.path).stop_and_probe('stop-after-threads')['probe_denied'])
        self.assertTrue(GatewayStore(self.path).status()['stopped'])


if __name__ == '__main__':
    unittest.main()
