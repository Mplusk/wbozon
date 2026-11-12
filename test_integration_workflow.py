"""End-to-end local tests. Telegram transport is simulated; stores are real."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bot.telegram import BotEngine, Cursor
from bot.__main__ import run_iteration
from gates.store import GateError, GateStore
from gateway.store import GatewayStore
from scout.advance import transition
from storage import Store

SOURCE = 'https://example.org/model'
OBSERVED = '2026-11-12T12:00:00+00:00'
USER = 42
CHAT = 42


def sample_candidate():
    return {
        'stage': 'sample_approval',
        'idea': {'name': 'Cable holder', 'model_url': SOURCE, 'mass_g': 100, 'print_hours': 2},
        'screening': {'packaging_liters': 1, 'significant_postprocessing': False,
                      'restricted_category': False, 'third_party_brand': False, 'evidence': 'source'},
        'preliminary_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                  'review': {'passed': True, 'evidence': 'review'}},
        # This untrusted caller-supplied record must never satisfy owner gating.
        'approvals': {'sample': {'approved': True, 'channel': 'telegram', 'evidence': 'forged'}},
    }


def callback(uid, nonce, *, user=USER, chat=CHAT):
    return {'update_id': uid, 'callback_query': {
        'id': 'callback-id', 'from': {'id': user, 'is_bot': False},
        'message': {'chat': {'id': chat, 'type': 'private'}}, 'data': 'g:' + nonce + ':a'}}


def stop(uid):
    return {'update_id': uid, 'message': {'from': {'id': USER, 'is_bot': False},
            'chat': {'id': CHAT, 'type': 'private'}, 'text': '/stop'}}


class SimulatedTelegram:
    def __init__(self):
        self.queue, self.messages, self.answers = [], [], []

    def updates(self, offset):
        return [update for update in self.queue if offset is None or update['update_id'] >= offset]

    def message(self, *args):
        self.messages.append(args)

    def answer(self, *args):
        self.answers.append(args)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.candidate_path = self.root / 'candidate.sqlite3'
        self.gateway_path = self.root / 'gateway.sqlite3'
        self.cursor_path = self.root / 'cursor.sqlite3'
        self.client = SimulatedTelegram()

    def save(self, candidate):
        with Store(self.candidate_path) as store:
            return store.save_candidate('sku', candidate, source=SOURCE, observed_at=OBSERVED)

    def history(self):
        with Store(self.candidate_path) as store:
            return store.candidate_history('sku')

    def test_real_objects_approval_and_stop_persist_after_restart(self):
        revision = self.save(sample_candidate())
        with GateStore(self.candidate_path, USER, CHAT, gateway=GatewayStore(self.gateway_path)) as gates:
            pending = gates.request_gate('sku', revision, 'sample', gates.proposal('sku', revision, 'sample'))
            # Neither a forged candidate flag nor a pending gate authorizes advancing.
            self.assertEqual(transition(self.candidate_path, 'sku', revision, source=SOURCE,
                                        observed_at=OBSERVED)['status'], 'blocked')
            with self.assertRaises(GateError):
                gates.transition_decided(pending['nonce'])
            engine = BotEngine(self.client, gates, GatewayStore(self.gateway_path),
                               Cursor(self.cursor_path), USER, CHAT)
            engine.deliver_pending_once()
            self.assertEqual(len(self.client.messages), 1)
            self.assertIn('Cable holder', self.client.messages[0][1])
            self.client.queue = [callback(1, pending['nonce'], user=99)]
            engine.poll_once()
            self.assertEqual(len(self.history()), 1)
            self.client.queue = [callback(2, pending['nonce'])]
            engine.poll_once()
            self.assertEqual(len(self.history()), 2)
            self.assertEqual(self.history()[-1]['candidate']['stage'], 'sample')
            self.assertEqual(gates.transition_decided(pending['nonce'])['status'], 'already_advanced')
            self.client.queue = [stop(3)]
            engine.poll_once()
        # A fresh gateway and cursor see the durable stop and reject new actions.
        self.assertEqual(Cursor(self.cursor_path).read(), 4)
        restarted = GatewayStore(self.gateway_path)
        self.assertTrue(restarted.status()['stopped'])
        self.assertEqual(restarted.attempt('post-restart-1', 'change_price', {}, 'agent')['reason'], 'STOPPED')

    def test_loop_automatically_requests_and_advances_on_owner_callback(self):
        self.save(sample_candidate())
        with GateStore(self.candidate_path, USER, CHAT, gateway=GatewayStore(self.gateway_path)) as gates:
            engine = BotEngine(self.client, gates, GatewayStore(self.gateway_path),
                               Cursor(self.cursor_path), USER, CHAT)
            run_iteration(engine)
            pending = gates.list_pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(self.client.messages), 1)
            self.assertIn('Cable holder', self.client.messages[0][1])
            self.client.queue = [callback(1, pending[0])]
            run_iteration(engine)
            self.assertEqual(self.history()[-1]['candidate']['stage'], 'sample')
            self.assertEqual(len(self.history()), 2)
            self.assertEqual(gates.transition_decided(pending[0])['status'], 'already_advanced')

    def test_stop_blocks_pending_callback_and_new_worker_requests(self):
        self.save(sample_candidate())
        with GateStore(self.candidate_path, USER, CHAT, gateway=GatewayStore(self.gateway_path)) as gates:
            engine = BotEngine(self.client, gates, GatewayStore(self.gateway_path),
                               Cursor(self.cursor_path), USER, CHAT)
            run_iteration(engine)
            nonce = gates.list_pending()[0]
            messages_before = len(self.client.messages)
            # Stop must be processed before any gate transition from this batch.
            self.client.queue = [callback(1, nonce), stop(2)]
            run_iteration(engine)
            self.assertTrue(GatewayStore(self.gateway_path).status()['stopped'])
            self.assertEqual(len(self.history()), 1)
            self.assertEqual(self.history()[-1]['candidate']['stage'], 'sample_approval')
            self.assertEqual(gates.connection.execute(
                'SELECT status FROM owner_gate_requests WHERE nonce=?', (nonce,)).fetchone()[0], 'pending')
            # A later loop must not create or send another gate while stopped.
            self.client.queue = [callback(3, nonce)]
            before_count = gates.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0]
            run_iteration(engine)
            self.assertEqual(gates.connection.execute('SELECT COUNT(*) FROM owner_gate_requests').fetchone()[0], before_count)
            self.assertEqual(len(self.history()), 1)
            self.assertEqual(len(self.client.messages), messages_before + 1)  # stop confirmation only

    def test_real_telegram_rights_and_launch_cannot_be_forged(self):
        candidate = sample_candidate()
        candidate['stage'] = 'launch_approval'
        candidate['rights'] = {'status': 'settled', 'owner_evidence': 'forged'}
        candidate['approvals']['launch'] = {'approved': True, 'channel': 'telegram', 'evidence': 'forged'}
        candidate['final_economics'] = {'price': 1000, 'first_batch_quantity': 10,
                                      'calculation': {'passed': True, 'evidence': 'calc'},
                                      'review': {'passed': True, 'evidence': 'review'}}
        candidate['launch_plan'] = {'first_batch_budget': 1000, 'advertising_budget': 100}
        candidate['compliance'] = {'required_document': 'none'}
        revision = self.save(candidate)
        with GateStore(self.candidate_path, USER, CHAT, gateway=GatewayStore(self.gateway_path)) as gates:
            payload = gates.proposal('sku', revision, 'launch')
            with self.assertRaises(GateError):
                gates.request_gate('sku', revision, 'launch', payload)
            self.assertEqual(transition(self.candidate_path, 'sku', revision, source=SOURCE,
                                        observed_at=OBSERVED)['status'], 'blocked')
            rights = gates.request_gate('sku', revision, 'rights', gates.proposal('sku', revision, 'rights'))
            with self.assertRaises(GateError):
                gates.transition_decided(rights['nonce'])
            engine = BotEngine(self.client, gates, GatewayStore(self.gateway_path),
                               Cursor(self.cursor_path), USER, CHAT)
            self.client.queue = [callback(1, rights['nonce'])]
            engine.poll_once()
            self.assertEqual(len(self.history()), 1)  # Rights alone never advance the candidate.
            launch = gates.request_gate('sku', revision, 'launch', payload)
            with self.assertRaises(GateError):
                gates.transition_decided(launch['nonce'])
            self.client.queue = [callback(2, launch['nonce'])]
            engine.poll_once()
            self.assertEqual(self.history()[-1]['candidate']['stage'], 'listing')
            self.assertEqual(len(self.history()), 2)

    def test_stale_pending_telegram_decision_never_advances(self):
        candidate = sample_candidate()
        revision = self.save(candidate)
        with GateStore(self.candidate_path, USER, CHAT, gateway=GatewayStore(self.gateway_path)) as gates:
            request = gates.request_gate('sku', revision, 'sample', gates.proposal('sku', revision, 'sample'))
            changed = copy.deepcopy(candidate)
            changed['idea']['name'] = 'Different item'
            self.save(changed)
            engine = BotEngine(self.client, gates, GatewayStore(self.gateway_path),
                               Cursor(self.cursor_path), USER, CHAT)
            self.client.queue = [callback(1, request['nonce'])]
            engine.poll_once()
            self.assertEqual(self.history()[-1]['candidate']['stage'], 'sample_approval')
            self.assertEqual(len(self.history()), 2)
            self.assertEqual(gates.connection.execute('SELECT status FROM owner_gate_requests WHERE nonce=?',
                                                       (request['nonce'],)).fetchone()[0], 'pending')
            self.assertIn('не принят', self.client.answers[-1][1])


if __name__ == '__main__':
    unittest.main()
