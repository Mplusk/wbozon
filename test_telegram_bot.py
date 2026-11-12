import contextlib
from concurrent.futures import ThreadPoolExecutor
import threading
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import urllib.error

from bot.telegram import BotEngine, BotError, Cursor, TelegramClient
from bot.__main__ import run_iteration
from gates.store import GateError, GateStore
from gateway.store import GatewayStore
from storage import Store

NONCE = "a" * 24


def callback(uid=1, *, user=11, chat=22, chat_type="private", data=None):
    return {"update_id": uid, "callback_query": {"id": "query", "from": {"id": user, "is_bot": False},
            "message": {"chat": {"id": chat, "type": chat_type}}, "data": data or ("g:" + NONCE + ":a")}}


def stop(uid=2, *, user=11, chat=22, chat_type="private"):
    return {"update_id": uid, "message": {"from": {"id": user, "is_bot": False},
            "chat": {"id": chat, "type": chat_type}, "text": "/stop"}}


class FakeClient:
    def __init__(self, updates=None):
        self.queue = updates or []
        self.answers, self.messages, self.offsets = [], [], []

    def updates(self, offset):
        self.offsets.append(offset)
        return self.queue

    def answer(self, cb_id, text):
        self.answers.append((cb_id, text))

    def message(self, *args):
        self.messages.append(args)


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cursor = Cursor(self.root / "offset.db")
        self.client = FakeClient()
        self.gates = mock.Mock()
        self.gates.coordinated.return_value = contextlib.nullcontext()
        self.gateway = mock.Mock()
        self.gateway.coordination_lock.return_value = contextlib.nullcontext()
        self.gates.decide.return_value = {"status": "approved"}
        self.gates.transition_decided.return_value = {"status": "advanced"}
        self.gateway.stop_and_probe.return_value = {"stopped": True, "probe_denied": True}
        self.gateway.status.return_value = {"stopped": False, "read_only": True}
        self.bot = BotEngine(self.client, self.gates, self.gateway, self.cursor, 11, 22)

    def test_valid_callback_and_cursor_survive_restart(self):
        self.client.queue = [callback()]
        self.bot.poll_once()
        self.gates.decide.assert_called_once_with(NONCE, user_id=11, chat_id=22, update_id=1, approve=True)
        self.assertEqual(Cursor(self.root / 'offset.db').read(), 2)
        self.assertEqual(self.client.answers[0][1], "Решение записано")
        self.client.queue = []
        self.bot.poll_once()
        self.assertEqual(self.client.offsets, [None, 2])
        self.assertEqual((self.root / 'offset.db').stat().st_mode & 0o777, 0o600)

    def test_non_owner_group_bot_and_inline_callbacks_are_ignored(self):
        self.client.queue = [callback(1, user=99), callback(2, chat=77), callback(3, chat_type='group'),
                             {"update_id": 4, "callback_query": {"from": {"id": 11, "is_bot": True},
                              "message": {"chat": {"id": 22, "type": "private"}}, "id": "q", "data": "g:"+NONCE+":a"}},
                             {"update_id": 5, "callback_query": {"from": {"id": 11, "is_bot": False},
                              "inline_message_id": "abc", "id": "q", "data": "g:"+NONCE+":a"}}]
        self.bot.poll_once()
        self.gates.decide.assert_not_called()
        self.assertEqual(self.cursor.read(), 6)

    def test_expired_or_invalid_callback_does_not_block_stop(self):
        self.client.queue = [callback(1), stop(2)]
        self.gates.decide.side_effect = GateError('expired')
        self.bot.poll_once()
        self.gateway.stop_and_probe.assert_called_once_with('tg-update-2')
        self.assertEqual(self.cursor.read(), 3)
        self.assertIn("недействителен", self.client.answers[0][1])
        self.assertIn("остановлена", self.client.messages[0][1])

    def test_stop_not_starved_by_candidate_db_failure(self):
        self.client.queue = [callback(1), stop(2)]
        self.gates.decide.side_effect = sqlite3.OperationalError('sensitive')
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.gateway.stop_and_probe.assert_called_once_with('tg-update-2')
        self.assertIsNone(self.cursor.read())

    def test_stop_not_confirmed_without_actual_probe(self):
        self.client.queue = [stop(1)]
        self.gateway.stop_and_probe.return_value = {"stopped": True, "probe_denied": False}
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.assertFalse(self.client.messages)
        self.assertIsNone(self.cursor.read())

    def test_non_owner_stop_does_nothing(self):
        self.client.queue = [stop(1, user=13), stop(2, chat_type='group')]
        self.bot.poll_once()
        self.gateway.stop_and_probe.assert_not_called()
        self.assertEqual(self.cursor.read(), 3)

    def test_full_gate_payload_is_visible_and_overlong_rejected(self):
        details = {"nonce": NONCE, "candidate_id": "sku-1", "revision_id": 2,
                   "gate": "launch", "payload": {"budget": 3000, "quantity": 20}, "expires_at": 2000000000}
        self.gates.describe.return_value = details
        self.bot.send_gate_request(NONCE)
        message = self.client.messages[0]
        self.assertEqual(message[0], 22)
        self.assertIn('"budget": 3000', message[1])
        self.assertIn('"quantity": 20', message[1])
        self.assertIn('"revision_id": 2', message[1])
        self.assertEqual(message[2]['inline_keyboard'][0][0]['callback_data'], 'g:'+NONCE+':a')
        details["payload"] = {"long": "x" * 5000}
        with self.assertRaises(BotError):
            self.bot.send_gate_request(NONCE)
        self.assertEqual(len(self.client.messages), 1)

    def test_rejects_bidi_spoofed_payload_and_invalid_buttons(self):
        self.gates.describe.return_value = {"nonce": NONCE, "candidate_id": "sku-1", "revision_id": 1,
               "gate": "sample", "payload": {"name": "x\u202ey"}, "expires_at": 2000000000}
        with self.assertRaises(BotError):
            self.bot.send_gate_request(NONCE)
        self.client.queue = [callback(data='approve:true')]
        self.bot.poll_once()
        self.gates.decide.assert_not_called()

    def test_mismatched_gateway_database_is_rejected(self):
        path = self.root / 'gates.db'
        with Store(path):
            pass
        with GateStore(path, 11, 22, gateway=GatewayStore(self.root / 'gateway-a.db')) as gates:
            other = GatewayStore(self.root / 'gateway-b.db')
            with self.assertRaises(BotError):
                BotEngine(self.client, gates, other, self.cursor, 11, 22)

    def test_gate_integration_records_decision_once(self):
        gate_path = self.root / 'gates.db'
        with Store(gate_path) as store:
            revision = store.save_candidate('sku', {'stage': 'launch_approval', 'idea': {'name': 'Cable', 'model_url': 'https://example.org/model'}}, source='source', observed_at='2026-11-12T00:00:00Z')
        with GateStore(gate_path, 11, 22, gateway=GatewayStore(self.root / "test-gateway.db")) as gates:
            request = gates.request_gate('sku', revision, 'rights', {'model_url': 'https://example.org/model', 'confirmation': 'rights_settled'})
            engine = BotEngine(self.client, gates, gates.gateway, self.cursor, 11, 22)
            engine.send_gate_request(request['nonce'])
            engine.process(callback(data='g:'+request['nonce']+':a'))
            duplicate = gates.decide(request['nonce'], user_id=11, chat_id=22, update_id=1, approve=True)
            self.assertEqual(duplicate['status'], 'duplicate')

    def test_approved_gate_transitions_once_after_crash_and_retry(self):
        path = self.root / 'sample.db'
        candidate = {'stage': 'sample_approval',
            'idea': {'model_url': 'https://example.org/model', 'name': 'Item', 'mass_g': 100, 'print_hours': 2},
            'screening': {'packaging_liters': 1, 'significant_postprocessing': False, 'restricted_category': False,
                          'third_party_brand': False, 'evidence': 'source'},
            'preliminary_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                      'review': {'passed': True, 'evidence': 'review'}}}
        with Store(path) as store:
            revision = store.save_candidate('sku', candidate, source='source', observed_at='2026-11-12T00:00:00Z')
        with GateStore(path, 11, 22, gateway=GatewayStore(self.root / "test-gateway.db")) as gates:
            proposal = gates.proposal('sku', revision, 'sample')
            request = gates.request_gate('sku', revision, 'sample', proposal)
            self.client.queue = [callback(data='g:' + request['nonce'] + ':a')]
            engine = BotEngine(self.client, gates, gates.gateway, self.cursor, 11, 22)
            with mock.patch.object(gates, 'transition_decided', side_effect=sqlite3.OperationalError('interrupted')):
                with self.assertRaises(BotError):
                    engine.poll_once()
            self.assertIsNone(self.cursor.read())
            engine.poll_once()  # Same update: duplicate decision, idempotent transition.
            self.assertEqual(self.cursor.read(), 2)
            self.assertEqual(gates.transition_decided(request['nonce'])['status'], 'already_advanced')
        with Store(path) as store:
            history = store.candidate_history('sku')
            self.assertEqual(len(history), 2)
            self.assertEqual(history[-1]['candidate']['stage'], 'sample')

    def test_real_gateway_stops_even_when_callback_db_unavailable(self):
        self.client.queue = [callback(1), stop(2)]
        self.gates.decide.side_effect = sqlite3.OperationalError('db failure')
        gateway = GatewayStore(self.root / 'gateway.db')
        engine = BotEngine(self.client, self.gates, gateway, self.cursor, 11, 22)
        engine.poll_once()
        self.assertTrue(gateway.status()['stopped'])
        self.gates.decide.assert_not_called()
        self.assertEqual(self.cursor.read(), 3)

    def test_transport_rejects_redirect_and_hides_error(self):
        token = '123456:abcdefghijklmnopqrstuvwx'
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError('leaked ' + token)
        client = TelegramClient(token, opener)
        with self.assertRaises(BotError) as ex:
            client.updates(None)
        self.assertNotIn(token, str(ex.exception))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, 'api.telegram.org')
        self.assertEqual(request.method, 'POST')
        with self.assertRaises(BotError):
            client.call('deleteWebhook', {})

    def test_iteration_creates_and_delivers_pending_real_gate(self):
        path = self.root / 'auto.db'
        candidate = {'stage': 'sample_approval',
            'idea': {'model_url': 'https://example.org/model', 'name': 'Item', 'mass_g': 100, 'print_hours': 2},
            'screening': {'packaging_liters': 1, 'significant_postprocessing': False, 'restricted_category': False,
                          'third_party_brand': False, 'evidence': 'source'},
            'preliminary_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                      'review': {'passed': True, 'evidence': 'review'}}}
        with Store(path) as store:
            store.save_candidate('sku', candidate, source='source', observed_at='2026-11-12T00:00:00Z')
        with GateStore(path, 11, 22, gateway=GatewayStore(self.root / "test-gateway.db")) as gates:
            engine = BotEngine(self.client, gates, gates.gateway, self.cursor, 11, 22)
            run_iteration(engine)
            self.assertEqual(len(self.client.messages), 1)
            nonce = gates.list_pending()[0]
            self.assertEqual(self.cursor.delivery_status(nonce), 'sent')
            run_iteration(engine)
            self.assertEqual(len(self.client.messages), 1)

    def test_iteration_handles_stop_before_worker_error(self):
        self.client.queue = [stop(1)]
        with mock.patch('bot.__main__.scan_gate_requests', side_effect=sqlite3.OperationalError('db failure')):
            self.gates.list_pending.return_value = []
            with self.assertRaises(BotError):
                run_iteration(self.bot)
        self.gateway.stop_and_probe.assert_called_once_with('tg-update-1')
        self.gates.list_pending.assert_called_once()
        self.assertEqual(self.cursor.read(), 2)

    def test_stop_serializes_with_callback_and_no_network_under_lock(self):
        gateway = GatewayStore(self.root / 'coordinated.db')
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        events = []
        def decide(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('timeout waiting to release test')
            return {'status': 'approved'}
        def transition(*args):
            events.append('transition')
            return {'status': 'advanced'}
        def answer(*args):
            # Reacquiring on the same thread fails if Telegram is called in lock.
            with gateway.coordination_lock(timeout=1):
                events.append('answer')
        self.gates.coordinated.side_effect = gateway.coordination_lock
        self.gates.decide.side_effect = decide
        self.gates.transition_decided.side_effect = transition
        self.client.answer = answer
        engine = BotEngine(self.client, self.gates, gateway, self.cursor, 11, 22)
        def stop_gateway():
            result = gateway.stop_and_probe('test-stop-concurrent')
            events.append('stop')
            finished.set()
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            callback_future = pool.submit(engine.process, callback())
            self.assertTrue(entered.wait(3))
            stop_future = pool.submit(stop_gateway)
            self.assertFalse(finished.wait(0.1))
            release.set()
            callback_future.result(timeout=5)
            self.assertTrue(stop_future.result(timeout=5)['probe_denied'])
        self.assertLess(events.index('transition'), events.index('stop'))
        self.assertTrue(gateway.status()['stopped'])

    def test_lock_unavailable_does_not_ack_decision(self):
        self.gates.coordinated.side_effect = TimeoutError('no lock')
        self.client.queue = [callback(1)]
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.gates.decide.assert_not_called()
        self.assertIsNone(self.cursor.read())

    def test_already_stopped_rejects_decision_without_advancing(self):
        self.gateway.status.return_value = {"stopped": True}
        self.client.queue = [callback(1)]
        self.bot.poll_once()
        self.gates.decide.assert_not_called()
        self.gates.transition_decided.assert_not_called()
        self.assertEqual(self.cursor.read(), 2)
        self.assertIn('остановлена', self.client.answers[0][1])

    def test_stop_and_callback_batch_does_not_approve(self):
        gateway = GatewayStore(self.root / 'stop-batch.db')
        self.client.queue = [callback(1), stop(2)]
        engine = BotEngine(self.client, self.gates, gateway, self.cursor, 11, 22)
        engine.poll_once()
        self.assertTrue(gateway.status()['stopped'])
        self.gates.decide.assert_not_called()
        self.gates.transition_decided.assert_not_called()
        self.assertEqual(self.cursor.read(), 3)

    def test_unavailable_gateway_does_not_ack_decision(self):
        self.gateway.status.side_effect = sqlite3.OperationalError('unavailable')
        self.client.queue = [callback(1)]
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.gates.decide.assert_not_called()
        self.assertIsNone(self.cursor.read())

    def test_stop_between_decision_and_transition_keeps_decision(self):
        self.gateway.status.side_effect = [{"stopped": False}, {"stopped": False}, {"stopped": True}]
        self.client.queue = [callback(1)]
        self.bot.poll_once()
        self.gates.decide.assert_called_once()
        self.gates.transition_decided.assert_not_called()
        self.assertEqual(self.cursor.read(), 2)
        self.assertIn('переход остановлен', self.client.answers[0][1])

    def test_gateway_failure_after_decision_leaves_update_for_retry(self):
        self.gateway.status.side_effect = [{"stopped": False}, {"stopped": False}, sqlite3.OperationalError('unavailable')]
        self.client.queue = [callback(1)]
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.gates.decide.assert_called_once()
        self.gates.transition_decided.assert_not_called()
        self.assertIsNone(self.cursor.read())

    def test_scan_is_not_wrapped_by_one_outer_coordination_lock(self):
        self.gates.coordinated.side_effect = AssertionError('scan was wrapped in external guard')
        self.gates.list_pending.return_value = []
        with mock.patch('bot.__main__.scan_gate_requests') as scan:
            run_iteration(self.bot)
        scan.assert_called_once_with(self.gates)
        self.gates.list_pending.assert_called_once()

    def test_stop_arriving_during_scan_prevents_delivery(self):
        self.gateway.status.side_effect = [{"stopped": False}, {"stopped": True}]
        with mock.patch('bot.__main__.scan_gate_requests') as scan:
            run_iteration(self.bot)
        scan.assert_called_once()
        self.gates.list_pending.assert_not_called()

    def test_unavailable_status_after_scan_does_not_deliver(self):
        self.gateway.status.side_effect = [{"stopped": False}, sqlite3.OperationalError('down')]
        with mock.patch('bot.__main__.scan_gate_requests'):
            with self.assertRaises(BotError):
                run_iteration(self.bot)
        self.gates.list_pending.assert_not_called()

    def test_stopped_gateway_blocks_gate_scans_and_sends(self):
        self.gateway.status.return_value = {"stopped": True, "read_only": True}
        self.client.queue = [stop(1)]
        with mock.patch('bot.__main__.scan_gate_requests') as scan:
            run_iteration(self.bot)
        self.gateway.stop_and_probe.assert_called_once_with('tg-update-1')
        scan.assert_not_called()
        self.gates.list_pending.assert_not_called()

    def test_unknown_gateway_state_fails_closed(self):
        self.gateway.status.return_value = {}
        with mock.patch('bot.__main__.scan_gate_requests') as scan:
            with self.assertRaises(BotError):
                run_iteration(self.bot)
        scan.assert_not_called()
        self.gates.list_pending.assert_not_called()

    def test_pending_delivery_is_persisted_and_never_repeated(self):
        self.gates.list_pending.return_value = [NONCE]
        self.gates.describe.return_value = {"nonce": NONCE, "candidate_id": "sku", "revision_id": 1,
                         "gate": "sample", "payload": {"name": "item"}, "expires_at": 2000000000}
        self.bot.deliver_pending_once()
        self.assertEqual(self.cursor.delivery_status(NONCE), 'sent')
        self.bot.deliver_pending_once()
        self.assertEqual(len(self.client.messages), 1)

    def test_unknown_delivery_is_not_retried(self):
        self.gates.list_pending.return_value = [NONCE]
        self.gates.describe.return_value = {"nonce": NONCE, "candidate_id": "sku", "revision_id": 1,
                         "gate": "sample", "payload": {"name": "item"}, "expires_at": 2000000000}
        self.client.message = mock.Mock(side_effect=BotError('unknown result'))
        self.bot.deliver_pending_once()
        self.assertEqual(self.cursor.delivery_status(NONCE), 'unknown')
        self.bot.deliver_pending_once()
        self.assertEqual(self.client.message.call_count, 1)

    def test_unrepresentable_pending_request_is_not_sent(self):
        self.gates.list_pending.return_value = [NONCE]
        self.gates.describe.return_value = {"nonce": NONCE, "candidate_id": "sku", "revision_id": 1,
                         "gate": "sample", "payload": {"name": "x" * 5000}, "expires_at": 2000000000}
        self.bot.deliver_pending_once()
        self.assertFalse(self.client.messages)
        self.assertEqual(self.cursor.delivery_status(NONCE), 'blocked')

    def test_out_of_order_and_malformed_updates_are_not_acknowledged(self):
        self.client.queue = [stop(3), callback(2)]
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.assertIsNone(self.cursor.read())
        self.gateway.stop_and_probe.assert_not_called()
        self.client.queue = [{'update_id': True}]
        with self.assertRaises(BotError):
            self.bot.poll_once()
        self.assertIsNone(self.cursor.read())


if __name__ == '__main__':
    unittest.main()
