import gc
import multiprocessing
import os
import secrets
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from portal import Vault
import content_factory_budget_vault as setup


def _enable_process(path,key,token,gate,queue):
    vault=Vault(path,key)
    gate.wait(5)
    try:
        setup.enable(vault,token,confirm_scope=True,confirm_estimate=True,expected_vault_path=path)
        queue.put('enabled')
    except (ValueError,setup.GatewayBusyError):
        queue.put('rejected')
    except BaseException:
        queue.put('error')


def _hold_gate_process(path,key,entered,release):
    vault=Vault(path,key)
    with setup._gate(setup._base(vault,path)):
        entered.set(); release.wait(5)


def _startup_process(path,key,start,queue):
    vault=Vault(path,key)
    queue.put('ready')
    start.wait(5)
    proxy=setup.Proxy(vault,expected_vault_path=path,transport=lambda *_:None)
    queue.put(('result',proxy._startup_failure,proxy.ready()))


class BudgetVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=os.path.join(self.tmp.name,'vault.sqlite')
        self.vault_key=secrets.token_bytes(32)
        self.vault=Vault(self.path,self.vault_key)
        self.token='synthetic-budget-key-1234567890'
    def tearDown(self):
        gc.collect(); self.tmp.cleanup()
    def enable(self,token=None):
        return setup.enable(self.vault,token or self.token,confirm_scope=True,confirm_estimate=True,expected_vault_path=self.path)
    def load(self): return setup.load(self.vault,expected_vault_path=self.path,transport=lambda *_:None)

    def test_opt_in_reload_disable_reenable_preserves_identity_and_halt(self):
        self.assertIsNone(self.load())
        with self.assertRaises(PermissionError): setup.enable(self.vault,self.token,expected_vault_path=self.path)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name,setup.DIRECTORY)))
        self.enable(); a=self.load(); self.assertIsNotNone(a)
        self.assertFalse(a.ready()); a.fence.arm(lambda:True); self.assertTrue(a.ready())
        original=a.fence.key
        b=self.load(); self.assertEqual(b.fence.key,original); self.assertTrue(b.ready())
        setup.disable(self.vault,expected_vault_path=self.path)
        self.assertFalse(a.ready()); self.assertIsNone(self.load())
        self.enable(); c=self.load(); self.assertEqual(c.fence.key,original); self.assertFalse(c.ready())

    def test_two_concurrent_enables_never_replace_master(self):
        def attempt(_):
            try: self.enable(); return 'enabled'
            except (ValueError,setup.GatewayBusyError): return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['enabled','rejected'])
        self.assertEqual(self.load().fence.key,self.load().fence.key)
        self.assertEqual(self.vault.read(setup.SLOT)['generation'],1)

    def test_two_processes_cannot_create_two_masters(self):
        ctx=multiprocessing.get_context('spawn'); gate=ctx.Event(); queue=ctx.Queue()
        workers=[ctx.Process(target=_enable_process,args=(self.path,self.vault_key,self.token,gate,queue)) for _ in range(2)]
        for worker in workers: worker.start()
        gate.set()
        results=[queue.get(timeout=10) for _ in workers]
        for worker in workers: worker.join(5); self.assertEqual(worker.exitcode,0)
        queue.close(); queue.join_thread()
        self.assertEqual(sorted(results),['enabled','rejected'])
        self.assertEqual(self.vault.read(setup.SLOT)['generation'],1)

    def test_interprocess_gate_blocks_disable_until_release(self):
        self.enable()
        ctx=multiprocessing.get_context('spawn'); entered=ctx.Event(); release=ctx.Event()
        worker=ctx.Process(target=_hold_gate_process,args=(self.path,self.vault_key,entered,release)); worker.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(setup.GatewayBusyError): setup.disable(self.vault,expected_vault_path=self.path)
            self.assertIsNotNone(self.load())
        finally:
            release.set(); worker.join(5)
        self.assertEqual(worker.exitcode,0)
        setup.disable(self.vault,expected_vault_path=self.path)
        self.assertIsNone(self.load())

    def test_orphan_and_wrong_vault_fail_closed(self):
        os.mkdir(os.path.join(self.tmp.name,setup.DIRECTORY))
        with self.assertRaises(ValueError): self.enable()
        self.assertNotIn(setup.SLOT,self.vault.present())
        with self.assertRaises(ValueError): setup.load(self.vault,expected_vault_path=os.path.join(self.tmp.name,'other.sqlite'))

    def test_rejects_reused_n8n_key_without_creating_files(self):
        self.vault.save('n8n',{'api_key':self.token})
        with self.assertRaises(ValueError): self.enable()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name,setup.DIRECTORY)))

    def test_stored_values_are_encrypted(self):
        self.enable()
        with open(self.path,'rb') as f: self.assertNotIn(self.token.encode(),f.read())

    def test_ai_write_key_is_never_overwritten(self):
        self.vault.save('n8n_content_factory_write',{'api_key':'different-ai-write-key-123456'})
        self.enable()
        self.assertEqual(self.vault.read('n8n_content_factory_write'),{'api_key':'different-ai-write-key-123456'})
        self.assertEqual(self.vault.read(setup.TOKEN_SLOT),{'api_key':self.token})

    def test_gateway_path_hardlink_rejected(self):
        self.enable(); target=os.path.join(self.tmp.name,setup.DIRECTORY,setup.DATABASE)
        alias=os.path.join(self.tmp.name,'hardlink.db')
        try: os.link(target,alias)
        except (OSError,NotImplementedError): self.skipTest('Hardlinks unavailable')
        with self.assertRaises(ValueError): self.load()

    def test_gateway_directory_wrong_owner_is_rejected(self):
        self.enable()
        directory=os.path.join(self.tmp.name,setup.DIRECTORY)
        wrong=SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,st_uid=999)
        with patch.object(setup.os,'name','posix'), patch.object(setup.os,'geteuid',return_value=111,create=True), patch.object(setup.os,'lstat',return_value=wrong):
            with self.assertRaises(ValueError): setup._existing_db(self.tmp.name)

    def test_hardlinked_sqlite_sidecar_is_rejected(self):
        self.enable()
        directory=os.path.join(self.tmp.name,setup.DIRECTORY)
        outside=os.path.join(self.tmp.name,'outside-file')
        with open(outside,'wb') as f: f.write(b'unchanged')
        try: os.link(outside,os.path.join(directory,setup.DATABASE+'-wal'))
        except (OSError,NotImplementedError): self.skipTest('Hardlinks unavailable')
        with self.assertRaises(ValueError): self.load()
        with open(outside,'rb') as f: self.assertEqual(f.read(),b'unchanged')

    def test_lazy_workers_reload_disable_and_never_call_network_on_ready(self):
        calls=[]
        workers=[setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *args:calls.append(args)) for _ in range(2)]
        self.assertFalse(workers[0].ready())
        self.enable()
        for worker in workers:
            self.assertTrue(worker.enabled())
            self.assertFalse(worker.ready())
        workers[0].fence.arm(lambda:True)
        self.assertTrue(all(w.ready() for w in workers))
        setup.disable(self.vault,expected_vault_path=self.path)
        self.assertTrue(all(not w.ready() for w in workers))
        self.enable()
        self.assertTrue(all(not w.ready() for w in workers))
        self.assertEqual(calls,[])

    def test_new_worker_halts_gateway_until_owner_rechecks(self):
        self.enable()
        first=setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *_:None)
        first.owner_arm(); self.assertTrue(first.ready())
        second=setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *_:None)
        self.assertFalse(first.ready()); self.assertFalse(second.ready())

    def test_simultaneous_worker_startup_waits_then_both_halt(self):
        self.enable()
        original=setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *_:None)
        original.owner_arm(); self.assertTrue(original.ready())
        ctx=multiprocessing.get_context('spawn'); start=ctx.Event(); queue=ctx.Queue()
        workers=[ctx.Process(target=_startup_process,args=(self.path,self.vault_key,start,queue)) for _ in range(2)]
        try:
            with setup._gate(setup._base(self.vault,self.path)):
                for worker in workers: worker.start()
                self.assertEqual([queue.get(timeout=8) for _ in workers],['ready','ready'])
                start.set()
                time.sleep(.25)
                self.assertTrue(all(worker.is_alive() for worker in workers))
            self.assertEqual([queue.get(timeout=8) for _ in workers],[('result',False,False)]*2)
            for worker in workers:
                worker.join(5); self.assertEqual(worker.exitcode,0)
            self.assertFalse(original.ready())
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(5)
            queue.close(); queue.join_thread()

    def test_disable_serializes_against_inflight_dispatch(self):
        self.enable()
        proxy=setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *_:None)
        started=threading.Event(); release=threading.Event(); stopped=threading.Event(); errors=[]
        original=proxy._require
        class Stub:
            def confirm(self,*args): started.set(); release.wait(4); return True
        proxy._require=lambda:Stub()
        worker=threading.Thread(target=lambda:proxy.confirm('owner','proposal'))
        def stop():
            try: setup.disable(self.vault,expected_vault_path=self.path)
            except BaseException as exc: errors.append(type(exc).__name__)
            finally: stopped.set()
        worker.start(); self.assertTrue(started.wait(2))
        stopper=threading.Thread(target=stop); stopper.start(); stopper.join(2)
        self.assertTrue(stopped.is_set()); self.assertEqual(errors,['GatewayBusyError'])
        release.set(); worker.join(4); stopper.join(4)
        proxy._require=original
        setup.disable(self.vault,expected_vault_path=self.path)
        with self.assertRaises(PermissionError): proxy.owner_arm()

    def test_arm_after_disable_begins_cannot_reenable_stale_state(self):
        self.enable(); proxy=setup.Proxy(self.vault,expected_vault_path=self.path,transport=lambda *_:None)
        entered=threading.Event(); release=threading.Event(); outcome=[]
        original=setup._disable_unlocked
        def slow_disable(*args,**kwargs):
            entered.set(); release.wait(4); return original(*args,**kwargs)
        def arm():
            try: proxy.owner_arm(); outcome.append('armed')
            except (PermissionError,setup.GatewayBusyError): outcome.append('rejected')
        with patch.object(setup,'_disable_unlocked',side_effect=slow_disable):
            stop=threading.Thread(target=lambda:setup.disable(self.vault,expected_vault_path=self.path))
            stop.start(); self.assertTrue(entered.wait(2))
            contender=threading.Thread(target=arm); contender.start(); contender.join(2)
            self.assertEqual(outcome,['rejected'])
            release.set(); stop.join(4); contender.join(4)
        self.assertEqual(outcome,['rejected'])

    def test_ui_explains_scope_and_never_prefills_secret(self):
        import content_factory_budget_vault_ui
        page=content_factory_budget_vault_ui.render('c'*32,False)
        self.assertIn('dataTableRow:update',page)
        self.assertIn('не гарантирует',page)
        self.assertNotIn(self.token,page)


if __name__=='__main__': unittest.main()
