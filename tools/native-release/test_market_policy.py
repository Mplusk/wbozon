"""Verify the published service inventories and crash recovery order on the host controller."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cd import Transaction, market_transition
from release_host import ReleaseError, atomic_json


def check_archives(base, bridge, reports):
    assert market_transition(base, bridge) == 'bootstrap'
    assert market_transition(bridge, reports) == market_transition(reports, bridge) == 'shared'
    for first, second in ((bridge, base), (base, reports), (reports, base)):
        try:
            market_transition(first, second)
        except ReleaseError:
            continue
        raise AssertionError('unsafe marketplace release pair admitted')
    print('Verified exact marketplace service inventories and allowed directions', flush=True)


class CutoverRecovery(unittest.TestCase):
    def test_cold_workers_still_require_marked_health_and_finance_verification(self):
        host, guard = Mock(state=Path('state')), Mock()
        txn = Transaction(host)
        host.process.return_value = (123, 'generation', '/unit')
        for phase in ('starting', 'restoring'):
            host.workers_idle.side_effect = [False, True, True]
            host.health.side_effect = [TimeoutError(), None]
            with (self.subTest(phase=phase), patch('cd.time.monotonic', side_effect=[0, 20, 47]),
                  patch('cd.time.sleep'), patch('cd.process_uid', return_value=400)):
                txn.ready({'phase':phase, 'mark':7}, 'release', guard)
        self.assertEqual(host.health.call_count, 4)
        self.assertEqual(host.sockets.call_count, 2)
        self.assertEqual(guard.verify.call_count, 2)
        guard.verify.assert_called_with('/proc/123/root', '/proc/123/ns/time')
        self.assertEqual(guard.uid, 400)

    def test_cold_start_stops_if_marked_health_never_passes(self):
        host, guard = Mock(state=Path('state')), Mock()
        txn = Transaction(host)
        host.process.return_value = (123, 'generation', '/unit')
        host.health.side_effect = TimeoutError()
        for phase, budget in (('starting', 90), ('committed', 18), ('restoring', 90)):
            with self.subTest(phase=phase), patch('cd.time.monotonic', side_effect=[0, budget]):
                with self.assertRaisesRegex(ReleaseError, 'marked health response'):
                    txn.ready({'phase':phase, 'mark':7}, 'new', guard)
        guard.verify.assert_not_called()

    def test_interrupted_cold_restore_verifies_the_same_process_before_restarting(self):
        host = Mock(state=Path('state'))
        host.current.return_value = 'old'
        host.props.return_value = {'ActiveState':'active'}
        txn = Transaction(host)
        journal = {'phase':'restoring', 'mark':7, 'old':'old', 'new':'new', 'old_sha':'old-sha'}
        guard = Mock()
        with (patch.object(txn, 'gate_lock', return_value=nullcontext(guard)) as lock,
              patch.object(txn, 'ready') as ready, patch.object(txn, 'complete') as complete):
            txn.reconcile(journal, False)
        lock.assert_called_once_with(recovery=True)
        ready.assert_called_once_with(journal, 'old', guard)
        complete.assert_called_once_with(journal, 'old', 'old-sha')
        host.stop.assert_not_called()

    def test_only_repeat_a_timed_out_drain_for_the_same_service_within_budget(self):
        host = Mock(state=Path('state'))
        txn = Transaction(host)
        identity = (123, 'generation', '/unit')
        host.process.return_value = identity
        host.drain.side_effect = [TimeoutError(), identity]
        with patch('cd.time.monotonic', side_effect=[0, 1, 24]), patch('cd.time.sleep'):
            self.assertEqual(txn.drain('old', 7, require_healthy=True), identity)
        self.assertEqual([c.kwargs['seconds'] for c in host.drain.call_args_list], [59, 36])
        self.assertTrue(all(c.kwargs['require_healthy'] for c in host.drain.call_args_list))

        host.drain.reset_mock()
        host.drain.side_effect = TimeoutError()
        host.process.side_effect = [identity, (124, 'restart', '/unit')]
        with self.assertRaisesRegex(ReleaseError, 'portal changed'):
            txn.drain('old', 7)
        host.drain.assert_called_once()

        host.process.side_effect = None
        with patch('cd.time.monotonic', side_effect=[0, 1, 35]), patch('cd.time.sleep'):
            with self.assertRaisesRegex(ReleaseError, 'drain probe timed out'):
                txn.drain('old', 7)

    def test_cutover_journal_outlives_hold_and_cannot_recover_an_older_release(self):
        with TemporaryDirectory() as folder, patch('cd.secure', side_effect=lambda path: path):
            root = Path(folder)
            host = Mock(state=root, root=root)
            host.current.return_value = 'new'
            txn = Transaction(host)
            j = {'txn':'123', 'old':'old', 'new':'new', 'market_bootstrap':True,
                 'old_sha':'old-sha', 'new_sha':'new-sha'}
            atomic_json(txn.market_hold, {key:j[key] for key in ('txn','old','new')})
            atomic_json(txn.active, {'release':'new','manifest':'new-sha','txn':'123'})
            with patch('cd.discard', side_effect=OSError('power loss')):
                with self.assertRaises(OSError):
                    txn.seal_market_decision(j, 'new')
            self.assertEqual(json.loads(txn.pending.read_bytes())['market_final'], 'new')
            self.assertTrue(txn.market_hold.exists())
            with patch('cd.present', side_effect=OSError('cannot stat hold')):
                with self.assertRaises(OSError):
                    txn.seal_market_decision(j, 'new')
            self.assertTrue(txn.pending.exists() and txn.market_hold.exists())
            txn.seal_market_decision(j, 'new')
            self.assertFalse(txn.market_hold.exists())
            self.assertTrue(txn.pending.exists())
            with patch.object(txn, 'reconcile', side_effect=ReleaseError('not ready')) as reconcile:
                with self.assertRaises(ReleaseError):
                    txn.recover()
                reconcile.assert_called_once_with(j, True)
            with self.assertRaises(ReleaseError):
                txn.complete(j, 'old', 'old-sha')
            self.assertEqual(json.loads(txn.active.read_bytes())['release'], 'new')


if __name__ == '__main__' and len(sys.argv) == 4:
    check_archives(*map(Path, sys.argv[1:]))
