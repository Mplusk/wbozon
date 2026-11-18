"""Verify the published service inventories and crash recovery order on the host controller."""
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
