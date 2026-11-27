import os
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta

from content_factory_budget import BudgetStore, BudgetUnavailable, BudgetDraftGateway

NOW = datetime(2026, 11, 27, 8, tzinfo=timezone.utc)
GRAPH = 'a' * 64


class Quote:
    def __init__(self): self.cents = 6
    def quote(self, graph_hash, today):
        return {'graph_hash':graph_hash, 'estimated_cents':self.cents, 'rate_date':'2026-11-27'}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'vault.db')
        self.quote = Quote()
        self.store = BudgetStore(self.path, quote_builder=self.quote, clock=lambda:NOW)

    def tearDown(self): self.tmp.cleanup()

    def enable(self):
        self.store.seed_baseline('2026-11', 0, 'verified-fixture')
        self.store.configure(0, 10, True)

    def test_default_disabled_and_absent_quote(self):
        with self.assertRaises(BudgetUnavailable): self.store.reserve('1'*32, GRAPH)
        self.enable()
        with self.assertRaises(BudgetUnavailable): BudgetStore(self.path, clock=lambda:NOW).reserve('1'*32, GRAPH)

    def test_edit_user_limit_does_not_enable_or_fabricate_spending(self):
        self.assertEqual(self.store.change_limit(0, 1234), 1)
        snapshot = self.store.settings()
        self.assertEqual(snapshot['monthly_limit_cents'], 1234)
        self.assertFalse(snapshot['enabled'])
        self.assertIsNone(snapshot['estimated_reserved_cents'])
        self.assertIsNone(snapshot['actual_spending_cents'])
        self.assertFalse(snapshot['can_run'])
        with self.assertRaises(BudgetUnavailable): self.store.change_limit(0, 999)
        self.assertEqual(self.store.settings()['monthly_limit_cents'], 1234)

    def test_editing_lower_limit_cancels_outstanding_queue(self):
        self.enable(); self.store.reserve('1'*32, GRAPH)
        self.store.change_limit(1,5); self.store.change_limit(2,10)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)

    def test_parallel_reservations_cannot_exceed_limit(self):
        self.enable(); gate = threading.Barrier(2); results = []
        def worker(ident):
            gate.wait()
            try: self.store.reserve(ident*32, GRAPH); results.append('ok')
            except BudgetUnavailable: results.append('denied')
        threads = [threading.Thread(target=worker,args=(str(i),)) for i in (1,2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, ['ok','denied'])

    def test_unknown_survives_restart_and_prevents_new_reservations(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)
        restarted = BudgetStore(self.path, quote_builder=self.quote, clock=lambda:NOW)
        with self.assertRaises(BudgetUnavailable): restarted.reserve('2'*32,GRAPH)
        with self.assertRaises(ValueError): restarted._transition('1'*32,'unknown','cancelled')

    def test_dispatching_after_crash_is_also_blocked(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        self.store._transition('1'*32,'reserved','dispatching')
        with self.assertRaises(BudgetUnavailable): self.store.reserve('2'*32,GRAPH)
        self.assertEqual(self.store.status('1'*32)['state'], 'dispatching')
        self.assertIsNone(self.store.status('2'*32))

    def test_stop_and_budget_reduction_cancel_reserved(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        self.store.configure(1,10,False); self.store.configure(2,10,True)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)
        self.store.reserve('2'*32,GRAPH); self.store.configure(3,5,True); self.store.configure(4,10,True)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('2'*32, lambda: None)

    def test_expired_or_changed_quote_not_dispatched(self):
        self.enable(); self.store.reserve('1'*32,GRAPH); self.quote.cents = 7
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)

    def test_sent_reserve_never_released_or_replayed(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)
        with self.assertRaises(BudgetUnavailable): self.store.reserve('2'*32,GRAPH)
        with self.assertRaises(BudgetUnavailable): self.store.dispatch_once('1'*32, lambda: None)

    def test_stop_waits_for_in_flight_send(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def sender():
            try: self.store.dispatch_once('1'*32, lambda: (entered.set(), release.wait(2)))
            except BudgetUnavailable: pass
        def stop(): self.store.configure(1,10,False); stopped.set()
        sending = threading.Thread(target=sender); sending.start()
        self.assertTrue(entered.wait(1))
        stopping = threading.Thread(target=stop); stopping.start()
        self.assertFalse(stopped.wait(.15))
        release.set(); sending.join(2); stopping.join(2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.store.status('1'*32)['state'], 'unknown')

    def test_stop_between_marker_and_network_prevents_send(self):
        self.enable(); self.store.reserve('1'*32,GRAPH)
        marked, release, sent = threading.Event(), threading.Event(), threading.Event()
        original = self.store._transition
        def paused(*args):
            original(*args); marked.set(); release.wait(2)
        self.store._transition = paused
        def sender():
            try: self.store.dispatch_once('1'*32, sent.set)
            except BudgetUnavailable: pass
        sending = threading.Thread(target=sender); sending.start()
        self.assertTrue(marked.wait(1))
        self.store.configure(1,10,False)
        release.set(); sending.join(2)
        self.assertFalse(sent.is_set())
        self.assertEqual(self.store.status('1'*32)['state'], 'cancelled')

    def test_owner_bound_one_use_and_audited_draft(self):
        gate = BudgetDraftGateway(self.store, enabled=True); owner = 'a'*64
        proposal = gate.prepare(owner,0,1200)
        with self.assertRaises(BudgetUnavailable): gate.confirm('b'*64,proposal['proposal_id'])
        self.assertEqual(self.store.settings()['monthly_limit_cents'], 0)
        self.assertTrue(gate.confirm(owner,proposal['proposal_id']))
        with self.assertRaises(BudgetUnavailable): gate.confirm(owner,proposal['proposal_id'])
        self.assertEqual(self.store.settings()['monthly_limit_cents'], 1200)
        with closing(self.store._db()) as db:
            self.assertEqual(db.execute('SELECT event FROM cf_budget_audit ORDER BY seq').fetchall(), [('prepared',),('confirmed',)])

    def test_draft_gateway_expired_stale_and_disabled(self):
        owner = 'a'*64
        with self.assertRaises(BudgetUnavailable): BudgetDraftGateway(self.store).prepare(owner,0,500)
        gateway = BudgetDraftGateway(self.store,enabled=True)
        old = gateway.prepare(owner,0,500)
        self.store.change_limit(0,600)
        with self.assertRaises(BudgetUnavailable): gateway.confirm(owner,old['proposal_id'])
        new = gateway.prepare(owner,1,700)
        self.store.clock = lambda:NOW + timedelta(seconds=300)
        with self.assertRaises(BudgetUnavailable): gateway.confirm(owner,new['proposal_id'])
        self.assertEqual(self.store.settings()['monthly_limit_cents'], 600)

    def test_parallel_confirmation_only_once(self):
        owner = 'a'*64; gateway = BudgetDraftGateway(self.store,enabled=True)
        proposal = gateway.prepare(owner,0,1000); barrier = threading.Barrier(2); results = []
        def run():
            barrier.wait()
            try: gateway.confirm(owner,proposal['proposal_id']); results.append('ok')
            except BudgetUnavailable: results.append('denied')
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results,['ok','denied'])

    def test_budget_proposals_rate_limited(self):
        gateway = BudgetDraftGateway(self.store,enabled=True)
        for _ in range(10): gateway.prepare('a'*64,0,1000)
        with self.assertRaises(BudgetUnavailable): gateway.prepare('a'*64,0,1000)
        self.assertFalse(self.store.settings()['enabled'])


if __name__ == '__main__': unittest.main()
