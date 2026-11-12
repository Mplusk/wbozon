import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from gateway.store import GatewayStore
from gates import GateError, GateStore
from scout.advance import transition
from storage import Store

SOURCE = 'https://example.com/model'
OBSERVED = '2026-11-12T12:00:00+00:00'
USER, CHAT = 42, 42
ACTION = {'sku': 'product1', 'quantity': 10}


class GateStoreTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'project.sqlite3'
        self.gateway = GatewayStore(Path(temp.name) / 'gateway.sqlite3')

    def save(self, stage, **extra):
        value = {'stage': stage,
                 'idea': {'model_url': SOURCE, 'name': 'Кандидат', 'mass_g': 100, 'print_hours': 2},
                 'screening': {'packaging_liters': 1, 'significant_postprocessing': False,
                               'restricted_category': False, 'third_party_brand': False, 'evidence': 'source'},
                 'preliminary_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                           'review': {'passed': True, 'evidence': 'review'}},
                 'final_economics': {'calculation': {'passed': True, 'evidence': 'calc'},
                                     'review': {'passed': True, 'evidence': 'review'},
                                     'price': 1000, 'first_batch_quantity': 10},
                 'compliance': {'required_document': 'нет'},
                 'launch_plan': {'first_batch_budget': 1000, 'advertising_budget': 100},
                 'listing': {'marketplace': 'ozon', 'draft': {'title': 'Кандидат', 'description': 'Описание',
                      'characteristics': {'материал': 'PLA'}, 'images': ['https://example.com/image']},
                      'api_validation': {'passed': True, 'evidence': 'api'},
                      'quality_review': {'passed': True, 'evidence': 'review'}}}
        value.update(extra)
        with Store(self.path) as store:
            return store.save_candidate('product1', value, source=SOURCE, observed_at=OBSERVED)

    def store(self):
        return GateStore(self.path, USER, CHAT, gateway=self.gateway)

    def request(self, store, revision, gate):
        payload = store.proposal('product1', revision, gate)
        return store.request_gate('product1', revision, gate, payload)

    def decide(self, store, nonce, update=100, approve=True, user=USER, chat=CHAT):
        return store.decide(nonce, user_id=user, chat_id=chat, update_id=update, approve=approve)

    def test_sample_owner_approval_transition_once_and_payload_binding(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            gate = self.request(store, revision, 'sample')
            self.assertEqual(store.describe(gate['nonce'])['payload'], gate['payload'])
            self.assertEqual(self.decide(store, gate['nonce'])['status'], 'approved')
            self.assertEqual(self.decide(store, gate['nonce'])['status'], 'duplicate')
            with self.assertRaises(GateError):
                store.transition_approved(gate['nonce'], candidate_id='product1', revision_id=revision,
                                          gate='sample', payload={'sku': 'product1', 'quantity': 11})
            result = store.transition_approved(gate['nonce'], candidate_id='product1', revision_id=revision,
                                               gate='sample', payload=gate['payload'])
            self.assertEqual(result['stage'], 'sample')
            self.assertEqual(store.transition_decided(gate['nonce'])['status'], 'already_advanced')
        with Store(self.path) as store:
            latest = store.candidate_history('product1')[-1]['candidate']
        self.assertEqual(latest['approvals']['sample']['channel'], 'telegram')
        self.assertTrue(latest['approvals']['sample']['approved'])

    def test_launch_requires_separate_rights_and_current_gates(self):
        revision = self.save('launch_approval', compliance={'required_document': 'нет'},
                             launch_plan={'first_batch_budget': 1000, 'advertising_budget': 100})
        with self.store() as store:
            with self.assertRaises(GateError):
                self.request(store, revision, 'launch')
            rights = store.request_gate('product1', revision, 'rights',
                                        {'model_url': SOURCE, 'confirmation': 'rights_settled'})
            self.decide(store, rights['nonce'], update=101)
            self.assertEqual(store.transition_decided(rights['nonce'])['status'], 'rights_recorded')
            gate = self.request(store, revision, 'launch')
            self.decide(store, gate['nonce'])
            result = store.transition_decided(gate['nonce'])
            self.assertEqual(result['stage'], 'listing')
        with Store(self.path) as store:
            self.assertEqual(store.candidate_history('product1')[-1]['candidate']['rights']['status'], 'settled')

    def test_publication_requires_separate_rights_even_with_forged_record(self):
        revision = self.save('publication_approval', rights={'status': 'settled', 'owner_evidence': 'fake'},
                             listing={'marketplace':'ozon', 'draft': {'title':'A', 'description':'B', 'characteristics':{'x':'y'}, 'images':['url']},
                                      'api_validation': {'passed': True, 'evidence': 'api'},
                                      'quality_review': {'passed': True, 'evidence': 'review'}})
        with self.store() as store:
            with self.assertRaises(GateError):
                self.request(store, revision, 'publication')
            rights = store.request_gate('product1', revision, 'rights',
                                        {'model_url': SOURCE, 'confirmation': 'rights_settled'})
            self.decide(store, rights['nonce'], update=101)
            gate = self.request(store, revision, 'publication')
            self.decide(store, gate['nonce'])
            result = store.transition_decided(gate['nonce'])
            self.assertEqual(result['stage'], 'first_batch')

    def test_invalid_owner_chat_replay_and_wrong_stage_rejected(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            with self.assertRaises(GateError):
                self.request(store, revision, 'launch')
            gate = self.request(store, revision, 'sample')
            with self.assertRaises(GateError):
                self.decide(store, gate['nonce'], user=43)
            with self.assertRaises(GateError):
                self.decide(store, gate['nonce'], chat=43)
            self.decide(store, gate['nonce'])
            with self.assertRaises(GateError):
                self.decide(store, gate['nonce'], approve=False)
            with self.assertRaises(GateError):
                self.decide(store, gate['nonce'], update=101)

    def test_stale_version_changed_contents_expiry_and_denial(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            with mock.patch('gates.store.time.time', return_value=100):
                gate = store.request_gate('product1', revision, 'sample', store.proposal('product1', revision, 'sample'), ttl_seconds=10)
            with mock.patch('gates.store.time.time', return_value=110):
                with self.assertRaises(GateError):
                    self.decide(store, gate['nonce'])
            with mock.patch('gates.store.time.time', return_value=111):
                gate = self.request(store, revision, 'sample')
                self.decide(store, gate['nonce'], update=101, approve=False)
                with self.assertRaises(GateError):
                    store.transition_approved(gate['nonce'], candidate_id='product1', revision_id=revision,
                                              gate='sample', payload=gate['payload'])
                gate = self.request(store, revision, 'sample')
                store.connection.execute("UPDATE candidate_revisions SET payload_json='{}' WHERE id=?", (revision,))
                store.connection.commit()
                with self.assertRaises(GateError):
                    store.describe(gate['nonce'])

    def test_failed_pipeline_check_rolls_back_consumption(self):
        revision = self.save('launch_approval')
        with self.store() as store:
            rights = store.request_gate('product1', revision, 'rights',
                                        {'model_url': SOURCE, 'confirmation': 'rights_settled'})
            self.decide(store, rights['nonce'], update=101)
            gate = self.request(store, revision, 'launch')
            self.decide(store, gate['nonce'])
            with mock.patch('pipeline.candidate.advance', side_effect=ValueError('failed')):
                with self.assertRaises(GateError):
                    store.transition_approved(gate['nonce'], candidate_id='product1', revision_id=revision,
                                              gate='launch', payload=gate['payload'])
            status = store.connection.execute('SELECT status FROM owner_gate_requests WHERE nonce=?', (gate['nonce'],)).fetchone()[0]
            self.assertEqual(status, 'approved')

    def test_gate_decision_bound_to_latest_revision(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            gate = self.request(store, revision, 'sample')
            self.save('sample_approval')
            with self.assertRaises(GateError):
                self.decide(store, gate['nonce'])

    def test_gate_payload_cannot_hide_or_change_critical_fields(self):
        revision = self.save('launch_approval')
        with self.store() as store:
            expected = store.proposal('product1', revision, 'launch')
            altered = copy.deepcopy(expected)
            altered['candidate']['launch_plan']['advertising_budget'] = 1
            for payload in ({'message': 'approve'}, altered):
                with self.subTest(payload=payload), self.assertRaises(GateError):
                    store.request_gate('product1', revision, 'launch', payload)
            self.assertEqual(store.list_pending(), [])
            self.assertEqual(expected['candidate']['final_economics']['first_batch_quantity'], 10)
            self.assertEqual(expected['candidate']['final_economics']['price'], 1000)
            self.assertEqual(expected['candidate']['launch_plan']['advertising_budget'], 100)

    def test_missing_card_or_budgets_cannot_request_gate(self):
        revision = self.save('publication_approval', listing={'api_validation': {'passed': True, 'evidence': 'x'},
                             'quality_review': {'passed': True, 'evidence': 'x'}})
        with self.store() as store:
            with self.assertRaises(GateError):
                store.proposal('product1', revision, 'publication')
        revision = self.save('launch_approval', launch_plan={'first_batch_budget': 5})
        with self.store() as store:
            with self.assertRaises(GateError):
                store.proposal('product1', revision, 'launch')

    def test_owner_change_invalidates_unconsumed_approval(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            gate = self.request(store, revision, 'sample')
            self.decide(store, gate['nonce'])
        with GateStore(self.path, 43, 43, gateway=self.gateway) as store:
            with self.assertRaises(GateError):
                store.transition_decided(gate['nonce'])
        with self.store() as store:
            self.assertEqual(store.transition_decided(gate['nonce'])['status'], 'advanced')

    def test_concurrent_transition_is_idempotent(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            gate = self.request(store, revision, 'sample')
            self.decide(store, gate['nonce'])
        def run(_):
            with self.store() as store:
                return store.transition_decided(gate['nonce'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertCountEqual([r['status'] for r in results], ['advanced', 'already_advanced'])
        self.assertEqual(results[0]['candidate_revision_id'], results[1]['candidate_revision_id'])
        with Store(self.path) as store:
            self.assertEqual(len(store.candidate_history('product1')), 2)

    def test_rights_are_not_carried_to_new_revision(self):
        revision = self.save('launch_approval')
        with self.store() as store:
            rights = store.request_gate('product1', revision, 'rights',
                                        {'model_url': SOURCE, 'confirmation': 'rights_settled'})
            self.decide(store, rights['nonce'], update=101)
            gate = self.request(store, revision, 'launch')
            self.decide(store, gate['nonce'])
            store.transition_decided(gate['nonce'])
        publication = self.save('publication_approval', rights={'status':'settled', 'owner_evidence':'old'})
        with self.store() as store:
            with self.assertRaises(GateError):
                self.request(store, publication, 'publication')

    def test_consume_requires_a_transaction(self):
        revision = self.save('sample_approval')
        with self.store() as store:
            gate = self.request(store, revision, 'sample')
            self.decide(store, gate['nonce'])
            with self.assertRaises(GateError):
                store.consume_in_transaction(gate['nonce'], candidate_id='product1', revision_id=revision,
                                              gate='sample', payload=gate['payload'])
