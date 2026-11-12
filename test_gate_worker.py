from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from gateway.store import GatewayStore
from gates import GateError, GateStore
from gates.worker import scan
from storage import Store

SOURCE = 'https://example.com/model'
DATE = '2026-11-12T12:00:00+00:00'


class GateWorkerTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'db.sqlite3'
        self.gateway = GatewayStore(Path(temp.name) / 'gateway.sqlite3')

    def seed(self, stage, **overrides):
        candidate = {'stage': stage,
          'idea': {'model_url': SOURCE, 'name': 'Товар', 'mass_g': 100, 'print_hours': 1},
          'screening': {'packaging_liters': 1, 'significant_postprocessing': False,
                        'restricted_category': False, 'third_party_brand': False, 'evidence': 'source'},
          'preliminary_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                    'review': {'passed': True, 'evidence': 'review'}},
          'final_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                              'review': {'passed': True, 'evidence': 'review'}, 'price': 1000, 'first_batch_quantity': 10},
          'compliance': {'required_document': 'нет'},
          'launch_plan': {'first_batch_budget': 1000, 'advertising_budget': 100},
          'listing': {'marketplace': 'ozon',
                      'draft': {'title': 'Товар', 'description': 'Описание', 'characteristics': {'цвет': 'черный'}, 'images': ['https://example.com/i']},
                      'api_validation': {'passed': True, 'evidence': 'source'},
                      'quality_review': {'passed': True, 'evidence': 'review'}}}
        candidate.update(overrides)
        with Store(self.path) as store:
            return store.save_candidate('product1', candidate, source=SOURCE, observed_at=DATE)

    def store(self):
        return GateStore(self.path, 42, 42, gateway=self.gateway)

    def choose(self, store, nonce, approve=True, update=1):
        return store.decide(nonce, user_id=42, chat_id=42, update_id=update, approve=approve)

    def test_auto_sample_and_recover_after_confirmation(self):
        self.seed('sample_approval')
        with self.store() as store:
            self.assertEqual(scan(store)['created'], 1)
            self.assertEqual(scan(store)['waiting'], 1)
            nonce = store.list_pending()[0]
            self.choose(store, nonce)
            self.assertEqual(scan(store)['advanced'], 1)
            self.assertEqual(store.store.candidate_history('product1')[-1]['candidate']['stage'], 'sample')
            self.assertEqual(scan(store)['created'], 0)

    def test_rights_precedes_launch_and_publication(self):
        for stage in ('launch_approval', 'publication_approval'):
            with self.subTest(stage=stage):
                self.seed(stage)
                with self.store() as store:
                    self.assertEqual(scan(store)['created'], 1)
                    rights = store.list_pending()[0]
                    self.assertEqual(store.describe(rights)['gate'], 'rights')
                    self.choose(store, rights, update=store.connection.execute('SELECT COALESCE(MAX(update_id),0)+1 FROM owner_gate_updates').fetchone()[0])
                    self.assertEqual(scan(store)['created'], 1)
                    gate = store.list_pending()[0]
                    self.assertEqual(store.describe(gate)['gate'], 'launch' if stage == 'launch_approval' else 'publication')
                    self.choose(store, gate, update=store.connection.execute('SELECT COALESCE(MAX(update_id),0)+1 FROM owner_gate_updates').fetchone()[0])
                    self.assertEqual(scan(store)['advanced'], 1)

    def test_rejected_or_expired_is_never_reissued(self):
        self.seed('sample_approval')
        with self.store() as store:
            scan(store)
            nonce = store.list_pending()[0]
            self.choose(store, nonce, approve=False)
            for _ in range(3):
                result = scan(store)
                self.assertEqual(result['created'], 0)
                self.assertEqual(result['blocked'][0]['reason'], 'decision_rejected_or_expired')
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0], 1)
        self.seed('sample_approval')
        with self.store() as store:
            with mock.patch('gates.worker.time.time', return_value=100), mock.patch('gates.store.time.time', return_value=100):
                self.assertEqual(scan(store)['created'], 1)
            with mock.patch('gates.worker.time.time', return_value=5000), mock.patch('gates.store.time.time', return_value=5000):
                self.assertEqual(scan(store)['blocked'][0]['reason'], 'decision_rejected_or_expired')
                self.assertEqual(scan(store)['created'], 0)

    def test_rejected_rights_are_not_reissued_or_followed_by_launch(self):
        for stage in ('launch_approval', 'publication_approval'):
            with self.subTest(stage=stage):
                self.seed(stage)
                with self.store() as store:
                    scan(store)
                    nonce = store.list_pending()[0]
                    self.assertEqual(store.describe(nonce)['gate'], 'rights')
                    update = store.connection.execute('SELECT COALESCE(MAX(update_id),0)+1 FROM owner_gate_updates').fetchone()[0]
                    self.choose(store, nonce, approve=False, update=update)
                    for _ in range(3):
                        result = scan(store)
                        self.assertEqual(result['created'], 0)
                        self.assertEqual(result['blocked'][0]['reason'], 'rights_rejected_or_expired')
                    self.assertEqual(store.list_pending(), [])

    def test_incomplete_gate_not_asked(self):
        self.seed('publication_approval', listing={'api_validation': {'passed': True, 'evidence': 'a'}})
        with self.store() as store:
            # Rights can be confirmed first, but incomplete publication details
            # may never be presented as a publication request.
            self.assertEqual(scan(store)['created'], 1)
            self.choose(store, store.list_pending()[0])
            self.assertEqual(scan(store)['blocked'][0]['reason'], 'not_ready_or_changed')
            self.assertEqual(store.list_pending(), [])

    def test_concurrent_scans_create_at_most_one(self):
        self.seed('sample_approval')
        def run(_):
            with self.store() as store:
                return scan(store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(2)))
        with self.store() as store:
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0], 1)

    def test_batches_resume_without_starving_later_candidates(self):
        self.seed('sample_approval')
        with Store(self.path) as raw:
            source = raw.connection.execute('SELECT payload_json,source,observed_at,recorded_at FROM candidate_revisions LIMIT 1').fetchone()
            raw.connection.executemany('INSERT INTO candidate_revisions(candidate_id,payload_json,source,observed_at,recorded_at) VALUES(?,?,?,?,?)',
                [(f'later-{i}', source['payload_json'], source['source'], source['observed_at'], source['recorded_at']) for i in range(45)])
            raw.connection.commit()
        for _ in range(3):
            # Reopening verifies that the cursor survives a process restart.
            with self.store() as store:
                result = scan(store)
                self.assertLessEqual(result['scanned'], 20)
        with self.store() as store:
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0], 46)


class StopCoordinationTests(GateWorkerTests):
    def test_missing_gateway_fails_closed_on_all_mutations(self):
        revision = self.seed('sample_approval')
        with GateStore(self.path, 42, 42) as store:
            payload = store.proposal('product1', revision, 'sample')
            with self.assertRaises(GateError):
                store.request_gate('product1', revision, 'sample', payload)
            with self.assertRaises(GateError):
                scan(store)
            self.assertEqual(store.list_pending(), [])

    def test_stopped_gateway_blocks_scan_decision_and_transition(self):
        revision = self.seed('sample_approval')
        with self.store() as store:
            scan(store)
            nonce = store.list_pending()[0]
            self.choose(store, nonce)
            self.gateway.stop_and_probe('owner-stop-001')
            with self.assertRaises(GateError):
                scan(store)
            with self.assertRaises(GateError):
                store.transition_decided(nonce)
            with self.assertRaises(GateError):
                store.decide(nonce, user_id=42, chat_id=42, update_id=1, approve=True)
            with self.assertRaises(GateError):
                store.request_gate('product1', revision, 'sample', store.proposal('product1', revision, 'sample'))
            self.assertEqual(store.store.candidate_history('product1')[-1]['candidate']['stage'], 'sample_approval')

    def test_infrastructure_error_is_retryable_not_gate_rejection(self):
        import sqlite3
        revision = self.seed('sample_approval')
        with self.store() as store:
            payload = store.proposal('product1', revision, 'sample')
            with mock.patch.object(store, '_latest', side_effect=sqlite3.OperationalError('locked')):
                with self.assertRaises(sqlite3.OperationalError):
                    store.request_gate('product1', revision, 'sample', payload)
            self.assertEqual(store.list_pending(), [])

    def test_standalone_cli_cannot_bypass_stop(self):
        import os
        import subprocess
        import sys
        self.seed('sample_approval')
        self.gateway.stop_and_probe('owner-stop-003')
        env = dict(os.environ, TELEGRAM_OWNER_USER_ID='42', TELEGRAM_OWNER_CHAT_ID='42')
        for module, extra in [('gates.worker', []), ('gates.request',
                ['--candidate-id', 'product1', '--revision', '1', '--gate', 'sample'])]:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-m', module, '--db', str(self.path),
                                         '--gateway-db', str(self.gateway.path), *extra],
                                        capture_output=True, text=True, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Traceback', result.stderr)
        with self.store() as store:
            self.assertEqual(store.list_pending(), [])

    def test_large_pass_stop_interrupts_after_current_candidate(self):
        import threading
        self.seed('sample_approval')
        # Simulate a large queue; /stop must not wait for the entire pass.
        with Store(self.path) as raw:
            source = raw.connection.execute('SELECT payload_json,source,observed_at,recorded_at FROM candidate_revisions LIMIT 1').fetchone()
            raw.connection.executemany('INSERT INTO candidate_revisions(candidate_id,payload_json,source,observed_at,recorded_at) VALUES(?,?,?,?,?)',
                [(f'product-{i}', source['payload_json'], source['source'], source['observed_at'], source['recorded_at']) for i in range(400)])
            raw.connection.commit()
        entered, release = threading.Event(), threading.Event()

        def run_scan():
            with self.store() as store:
                original = store.proposal
                def pause_first(*args, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(timeout=3))
                    return original(*args, **kwargs)
                store.proposal = pause_first
                return scan(store)

        with ThreadPoolExecutor(max_workers=2) as pool:
            work = pool.submit(run_scan)
            self.assertTrue(entered.wait(timeout=3))
            stop = pool.submit(self.gateway.stop_and_probe, 'owner-stop-large')
            release.set()
            self.assertTrue(stop.result(timeout=3)['stopped'])
            try:
                result = work.result(timeout=3)
                self.assertLessEqual(result['scanned'], 20)
            except GateError:
                pass  # Stop acquired the guard between candidates.
        with self.store() as store:
            self.assertLessEqual(store.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0], 20)
            with self.assertRaises(GateError):
                scan(store)
            self.assertEqual(store.store.candidate_history('product1')[-1]['candidate']['stage'], 'sample_approval')

    def test_stop_waits_until_current_transaction_releases_guard(self):
        import threading
        self.seed('sample_approval')
        entered, released = threading.Event(), threading.Event()
        def perform_stop():
            entered.set()
            try:
                self.gateway.stop_and_probe('owner-stop-002')
            finally:
                released.set()
        with self.store() as store:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with store.coordinated():
                    future = pool.submit(perform_stop)
                    self.assertTrue(entered.wait(timeout=2))
                    self.assertFalse(released.wait(timeout=0.1))
                    payload = store.proposal('product1', 1, 'sample')
                    store.request_gate('product1', 1, 'sample', payload)
                future.result(timeout=5)
                self.assertTrue(released.is_set())
                with self.assertRaises(GateError):
                    scan(store)
