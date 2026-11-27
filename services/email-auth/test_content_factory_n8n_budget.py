import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from content_factory_n8n_budget import LiveBudgetGateway, Unavailable, _snapshot, _worker
from content_factory_budget_config import configure_budget

ROW = {'id':1,'setting_key':'pilot','enabled':True,'monthly_budget_usd':10,
       'updatedAt':'2026-11-26T19:54:41.209Z'}
SNAP = {'id':1,'enabled':True,'before_cents':1000,'updatedAt':ROW['updatedAt']}


class Response:
    status = 200
    def __init__(self, value): self.value = json.dumps(value).encode()
    def read(self, n): return self.value[:n]
    def __enter__(self): return self
    def __exit__(self,*args): pass


class API:
    def __init__(self): self.row = dict(ROW); self.requests = []; self.before_patch = None
    def open(self, req, timeout):
        self.requests.append(req)
        if req.get_method() == 'GET': return Response({'data':[self.row], 'nextCursor':None})
        data = json.loads(req.data)
        if self.before_patch: self.before_patch(self.row)
        if not all(self.row.get(f['columnName']) == f['value'] for f in data['filter']['filters']):
            return Response([])
        self.row = dict(self.row, monthly_budget_usd=data['data']['monthly_budget_usd'], updatedAt='2026-11-27T19:54:41.209Z')
        return Response([self.row])


class Tests(unittest.TestCase):
    def test_exact_conditional_patch_and_readback(self):
        api = API()
        with patch('urllib.request.build_opener', return_value=api):
            self.assertEqual(_worker('write','x'*32,SNAP,1500),'verified')
        req = api.requests[1]; payload = json.loads(req.data)
        self.assertTrue(req.full_url.endswith('/kUd3G9ZAaqVWWcdI/rows/update'))
        self.assertEqual(payload['data'], {'monthly_budget_usd':15})
        self.assertEqual({x['columnName'] for x in payload['filter']['filters']}, {'id','setting_key','updatedAt','monthly_budget_usd','enabled'})
        self.assertTrue(payload['returnData'])
        self.assertEqual(len(api.requests),3)

    def test_server_side_cas_rejects_concurrent_changes(self):
        for column,value in (('enabled',False),('monthly_budget_usd',11),('updatedAt','2026-11-27T00:00:00Z'),('setting_key','other')):
            api = API(); api.before_patch = lambda row: row.update({column:value})
            with patch('urllib.request.build_opener',return_value=api):
                self.assertEqual(_worker('write','x'*32,SNAP,1500),'unknown')
            self.assertEqual(api.row['monthly_budget_usd'], 11 if column == 'monthly_budget_usd' else 10)
            self.assertEqual(len(api.requests),2)

    def test_duplicate_or_pagination_fails(self):
        for page in ({'data':[ROW,ROW],'nextCursor':None}, {'data':[ROW],'nextCursor':'more'}):
            with self.assertRaises(ValueError): _snapshot(page)

    def test_owner_confirmation_and_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = dict(SNAP)
            def transport(action,token,expected=None,after=None):
                if action == 'read': return dict(live)
                if live != expected: return 'unknown'
                live['before_cents'] = after; live['updatedAt'] = '2026-11-27T19:54:41.209Z'
                return 'verified'
            gate = LiveBudgetGateway(os.path.join(tmp,'db.sqlite'),b'x'*32,token_reader=lambda:'t'*32,transport=transport)
            self.assertFalse(gate.ready())
            gate.fence.arm(lambda:True)
            self.assertIsNone(gate.settings()['monthly_limit_cents'])
            state=gate.refresh(); self.assertEqual(state['source'],'n8n_live')
            proposal=gate.prepare('a'*64,state['revision'],1500)
            with self.assertRaises(PermissionError): gate.confirm('b'*64,proposal['proposal_id'])
            self.assertTrue(gate.confirm('a'*64,proposal['proposal_id']))
            self.assertEqual(gate.refresh()['monthly_limit_cents'],1500)
            with self.assertRaises(PermissionError): gate.confirm('a'*64,proposal['proposal_id'])
            proposal=gate.prepare('a'*64,gate.settings()['revision'],2000)
            live['updatedAt']='2026-11-28T19:54:41.209Z'
            self.assertFalse(gate.confirm('a'*64,proposal['proposal_id']))
            self.assertFalse(gate.ready())

    def test_render_settings_never_calls_network_and_refresh_is_single_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, release = threading.Event(), threading.Event()
            calls = []
            def transport(action, token):
                calls.append(action); entered.set(); release.wait(2); return dict(SNAP)
            gate = LiveBudgetGateway(os.path.join(tmp,'db.sqlite'),b'x'*32,token_reader=lambda:'t'*32,transport=transport)
            self.assertIsNone(gate.settings()['monthly_limit_cents']); self.assertEqual(calls,[])
            result=[]
            def run(): result.append(gate.refresh())
            thread=threading.Thread(target=run); thread.start(); self.assertTrue(entered.wait(1))
            with self.assertRaises(Unavailable): gate.refresh()
            release.set(); thread.join(2)
            self.assertEqual(calls,['read']); self.assertEqual(result[0]['monthly_limit_cents'],1000)

    def test_refresh_lease_clock_is_read_after_database_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,'db.sqlite'); seen=[]
            gate=LiveBudgetGateway(path,b'x'*32,token_reader=lambda:'t'*32,
                                   transport=lambda action,token:dict(SNAP))
            blocker=sqlite3.connect(path,isolation_level=None)
            blocker.execute('BEGIN IMMEDIATE')
            entered=threading.Event(); now=[100.0]; result=[]
            original=sqlite3.connect
            class Wrapped:
                def __init__(self,conn): self.conn=conn
                def execute(self,sql,*args):
                    if sql == 'BEGIN IMMEDIATE': entered.set()
                    return self.conn.execute(sql,*args)
                def __getattr__(self,name): return getattr(self.conn,name)
            def connect(*args,**kwargs): return Wrapped(original(*args,**kwargs))
            def refresh():
                try: result.append(gate.refresh())
                except Exception as exc: result.append(exc)
            with patch('content_factory_n8n_budget.sqlite3.connect',side_effect=connect), \
                 patch('content_factory_n8n_budget.time.time',side_effect=lambda:(seen.append(now[0]) or now[0])):
                thread=threading.Thread(target=refresh); thread.start()
                self.assertTrue(entered.wait(1))
                now[0]=150.0
                blocker.commit(); blocker.close(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result and isinstance(result[0],dict))
            self.assertEqual(seen[0],150.0)

    def test_parallel_confirmation_sends_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def transport(action,token,expected=None,after=None):
                if action == 'read': return dict(SNAP)
                calls.append(after); return 'verified'
            gate = LiveBudgetGateway(os.path.join(tmp,'db.sqlite'),b'x'*32,token_reader=lambda:'t'*32,transport=transport)
            gate.fence.arm(lambda:True); gate.refresh()
            proposal=gate.prepare('a'*64,1,1500)['proposal_id']
            barrier=threading.Barrier(2); results=[]
            def confirm():
                barrier.wait()
                try: results.append(gate.confirm('a'*64,proposal))
                except PermissionError: results.append(False)
            threads=[threading.Thread(target=confirm) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertCountEqual(results,[True,False]); self.assertEqual(calls,[1500])

    def test_pending_proposals_bounded_and_expired_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=LiveBudgetGateway(os.path.join(tmp,'db.sqlite'),b'x'*32,token_reader=lambda:'t'*32,
                                   transport=lambda action,token,*args:dict(SNAP))
            instant=[1000]
            gate.fence.clock=lambda:instant[0]
            gate.fence.arm(lambda:True); gate.refresh()
            proposals=[gate.prepare('a'*64,1,1500) for _ in range(5)]
            with self.assertRaises(PermissionError): gate.prepare('a'*64,1,1500)
            instant[0]+=301
            gate.prepare('a'*64,1,1600)
            with closing(__import__('sqlite3').connect(gate.path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM cf_live_budget_proposals').fetchone()[0],1)
            with self.assertRaises(Unavailable): gate.confirm('a'*64,proposals[0]['proposal_id'])

    def test_opt_in_composition_shares_fence_and_requires_token(self):
        class Vault:
            present_names=set()
            def present(self): return self.present_names
        with tempfile.TemporaryDirectory() as tmp:
            directory=os.path.join(tmp,'gateway'); os.mkdir(directory); vault=Vault()
            reader=lambda name,binary: b'k'*32 if name == 'CF_GATEWAY_HMAC_KEY_FILE' else b'v'*32
            self.assertIsNone(configure_budget({},vault,reader))
            with self.assertRaises(ValueError): configure_budget({'CF_BUDGET_GATEWAY_ENABLED':'1'},vault,reader)
            flags={'CF_BUDGET_GATEWAY_ENABLED':'1','CF_BUDGET_STAGING_VERIFIED':'1','CF_BUDGET_SCOPE_CONFIRMED':'1',
                   'CF_GATEWAY_DATA_DIR':directory,'CF_GATEWAY_HMAC_KEY_FILE':os.path.join(tmp,'hmac'),
                   'VAULT_KEY_FILE':os.path.join(tmp,'vaultkey')}
            gate=configure_budget(flags,vault,reader)
            self.assertFalse(gate.ready())
            gate.fence.arm(lambda:True)
            self.assertFalse(gate.ready())
            vault.present_names={'n8n_content_factory_write'}
            self.assertTrue(gate.ready())
            second=configure_budget(flags,vault,reader)
            second.fence.halt()
            self.assertFalse(gate.ready())
            with self.assertRaises(ValueError): configure_budget(flags,vault,lambda name,binary:b'k'*32)


if __name__ == '__main__': unittest.main()
