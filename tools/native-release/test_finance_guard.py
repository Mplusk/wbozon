"""Synthetic concurrency tests; never access a production finance gate."""
import json
import os
import pathlib
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'services' / 'email-auth'))
import finance_gate
from finance_guard import FinanceGuard, GuardError, _boot_id


class FinanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'gate')
        finance_gate.run(self.root, '101', lambda: None)

    def guard(self, wait=.2):
        return FinanceGuard(self.root, os.getuid(), wait)

    def child(self, code):
        env = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp.name, 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': os.pathsep.join(sys.path[:2])}
        proc = subprocess.Popen([sys.executable, '-c', code, self.root], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.addCleanup(cleanup)
        return proc

    def line(self, proc):
        readable, _, _ = select.select([proc.stdout], [], [], 5)
        self.assertTrue(readable, 'child process did not become ready')
        return proc.stdout.readline().strip()

    def test_existing_calls_are_blocked_then_resume_without_state_reset(self):
        with self.guard() as guard:
            guard.verify()
            with self.assertRaises(finance_gate.GateError) as error:
                finance_gate.run(self.root, '101', lambda: self.fail('must not run'))
            self.assertEqual(error.exception.reason, 'busy')
        self.assertEqual(finance_gate.run(self.root, '101', lambda: 'ok'), 'ok')

    def test_guard_is_single_use_even_if_nested(self):
        guard = self.guard()
        with guard:
            with self.assertRaises(GuardError):
                with guard:
                    self.fail('nested acquisition')
            guard.verify()
        with self.assertRaises(GuardError):
            with guard:
                self.fail('guard cannot be reused')
        self.assertEqual(finance_gate.run(self.root, '101', lambda: 'ok'), 'ok')

    def test_unknown_in_flight_is_refused(self):
        with self.assertRaises(TimeoutError):
            finance_gate.run(self.root, '101', lambda: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('must not acquire')

    def test_recovery_can_hold_unknown_state_without_clearing_it(self):
        with self.assertRaises(TimeoutError):
            finance_gate.run(self.root, '101', lambda: (_ for _ in ()).throw(TimeoutError()))
        key = finance_gate._key('101')
        state = pathlib.Path(self.root, key + '.json')
        before = state.read_bytes()
        with FinanceGuard(self.root, os.getuid(), .2, allow_uncertain=True) as guard:
            guard.verify()
        self.assertEqual(state.read_bytes(), before)
        with self.assertRaises(finance_gate.GateError) as error:
            finance_gate.run(self.root, '101', lambda: self.fail('unknown state must block retry'))
        self.assertEqual(error.exception.reason, 'retry_time_unknown')

    def test_existing_request_is_allowed_to_finish_before_guard(self):
        started, finish = threading.Event(), threading.Event()
        def request():
            finance_gate.run(self.root, '101', lambda: (started.set(), finish.wait(3)))
        thread = threading.Thread(target=request)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(2))
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('in-flight request must hold the lock')
        finish.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        with self.guard() as guard:
            guard.verify()

    def test_guard_waits_for_a_request_which_finishes_in_time(self):
        started, finish = threading.Event(), threading.Event()
        worker = threading.Thread(target=lambda: finance_gate.run(self.root, '101',
                                   lambda: (started.set(), finish.wait(3))))
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(2))
        timer = threading.Timer(.1, finish.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.guard(2) as guard:
            guard.verify()
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_expired_deadline_is_not_accepted_after_an_oversleep(self):
        started, finish = threading.Event(), threading.Event()
        worker = threading.Thread(target=lambda: finance_gate.run(self.root, '101',
                                   lambda: (started.set(), finish.wait(3))))
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(2))
        timer = threading.Timer(.08, finish.set)
        timer.start()
        self.addCleanup(timer.cancel)
        real_sleep = time.sleep
        with patch('finance_guard.time.sleep', side_effect=lambda _: real_sleep(.2)):
            with self.assertRaises(GuardError):
                with self.guard(.05):
                    self.fail('deadline already expired')

    def test_finance_request_in_another_process_blocks_guard(self):
        proc = self.child('import sys,finance_gate\n'
                          'def call():\n print("ready",flush=True); sys.stdin.readline()\n'
                          'finance_gate.run(sys.argv[1],"101",call)')
        self.assertEqual(self.line(proc), 'ready')
        with self.assertRaises(GuardError):
            with self.guard(.15):
                self.fail('another process owns the client lock')
        with self.assertRaises(GuardError):
            with FinanceGuard(self.root, os.getuid(), .15, allow_uncertain=True):
                self.fail('recovery must also wait for the provider')
        proc.stdin.write('\n')
        proc.stdin.flush()
        self.assertEqual(proc.wait(timeout=5), 0)
        with self.guard() as guard:
            guard.verify()

    def test_worker_past_bootstrap_cannot_bypass_guard(self):
        proc = self.child('import sys,finance_gate\n'
                          'original=finance_gate._ensure_client\n'
                          'def pause(*a,**kw):\n original(*a,**kw); print("ready",flush=True); sys.stdin.readline()\n'
                          'finance_gate._ensure_client=pause\n'
                          'try: finance_gate.run(sys.argv[1],"101",lambda: print("called",flush=True))\n'
                          'except finance_gate.GateError as e: print(e.reason,flush=True)')
        self.assertEqual(self.line(proc), 'ready')
        with self.guard():
            proc.stdin.write('\n')
            proc.stdin.flush()
            self.assertEqual(self.line(proc), 'busy')
            self.assertEqual(proc.wait(timeout=5), 0)

    def test_killed_guard_releases_process_locks(self):
        proc = self.child('import os,sys,finance_guard\n'
                          'with finance_guard.FinanceGuard(sys.argv[1],os.getuid(),1):\n'
                          ' print("ready",flush=True); sys.stdin.readline()')
        self.assertEqual(self.line(proc), 'ready')
        with self.assertRaises(finance_gate.GateError):
            finance_gate.run(self.root, '101', lambda: None)
        proc.kill()
        proc.wait(timeout=5)
        self.assertEqual(finance_gate.run(self.root, '101', lambda: 'ok'), 'ok')

    def test_killed_worker_leaves_uncertain_state(self):
        proc = self.child('import sys,finance_gate\n'
                          'def call():\n print("ready",flush=True); sys.stdin.readline()\n'
                          'finance_gate.run(sys.argv[1],"101",call)')
        self.assertEqual(self.line(proc), 'ready')
        proc.kill()
        proc.wait(timeout=5)
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('an interrupted provider outcome is unknown')

    def test_guard_fds_are_not_inherited_through_exec(self):
        proc = self.child('import os,sys,subprocess,finance_guard\n'
                          'with finance_guard.FinanceGuard(sys.argv[1],os.getuid(),1):\n'
                          ' subprocess.Popen([sys.executable,"-c","import time; time.sleep(2)"],close_fds=False,'
                          'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                          ' print("ready",flush=True); os._exit(0)')
        self.assertEqual(self.line(proc), 'ready')
        self.assertEqual(proc.wait(timeout=5), 0)
        with self.guard() as guard:
            guard.verify()

    def test_replaced_lock_is_detected(self):
        with self.guard() as guard:
            key = finance_gate._key('101')
            old = pathlib.Path(self.root, key + '.lock')
            old.unlink()
            old.write_bytes(b'')
            old.chmod(0o600)
            with self.assertRaises(GuardError):
                guard.verify()

    def test_replaced_parent_is_detected(self):
        with self.guard() as guard:
            parent = pathlib.Path(self.tmp.name)
            moved = parent.with_name(parent.name + '-moved')
            parent.rename(moved)
            parent.mkdir()
            try:
                with self.assertRaises(GuardError):
                    guard.verify()
            finally:
                parent.rmdir()
                moved.rename(parent)

    def test_permissions_changed_while_locked_are_detected(self):
        with self.guard() as guard:
            parent = pathlib.Path(self.tmp.name)
            parent.chmod(0o777)
            try:
                with self.assertRaises(GuardError):
                    guard.verify()
            finally:
                parent.chmod(0o700)
            store = pathlib.Path(self.root)
            store.chmod(0o000)
            try:
                with self.assertRaises((OSError, GuardError)):
                    guard.verify()
            finally:
                store.chmod(0o700)

    def test_boot_identity_rejects_fifo_and_oversized_file(self):
        path = pathlib.Path(self.tmp.name, 'fake-boot')
        os.mkfifo(path, 0o600)
        with self.assertRaises(GuardError):
            _boot_id(path)
        path.unlink()
        path.write_bytes(b'x' * 100)
        with self.assertRaises(GuardError):
            _boot_id(path)

    def test_namespace_parent_must_match(self):
        with self.guard() as guard:
            guard.verify('/proc/self/root', '/proc/self/ns/time')
            with self.assertRaises((OSError, GuardError)):
                guard.verify(self.root)
            with self.assertRaises(GuardError):
                guard.verify(time_namespace=self.root + '.bootstrap.lock')

    def test_fifo_cannot_block_before_lock_timeout(self):
        lock = self.root + '.bootstrap.lock'
        os.unlink(lock)
        os.mkfifo(lock, 0o600)
        with self.assertRaises(GuardError):
            with self.guard(0):
                self.fail('FIFO is not a regular lock file')

    def test_lock_must_be_readable_and_writable_by_service_user(self):
        pathlib.Path(self.root + '.bootstrap.lock').chmod(0o400)
        with self.assertRaises(GuardError):
            with self.guard(0):
                self.fail('read-only lock')

    def test_namespace_mismatch_of_individual_lock_is_detected(self):
        key = finance_gate._key('101')
        target = pathlib.Path('/proc/self/root') / self.root.lstrip('/') / (key + '.lock')
        real_stat = os.stat
        def altered(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if pathlib.Path(path) == target:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
            return result
        with self.guard() as guard, patch('finance_guard.os.stat', side_effect=altered):
            with self.assertRaises(GuardError):
                guard.verify('/proc/self/root', '/proc/self/ns/time')

    def test_unregistered_state_is_refused(self):
        path = pathlib.Path(self.root, 'a' * 64 + '.json')
        path.write_text('{}')
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('unexpected state')

    def test_epoch_mismatch_is_refused(self):
        key = finance_gate._key('101')
        pathlib.Path(self.root + '.epoch-' + key + '.json').write_text('{"version":1,"generation":999}')
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('bad generation')

    def test_known_retry_is_preserved(self):
        def limited():
            raise urllib.error.HTTPError('https://invalid.example', 429, 'synthetic', {'Retry-After': '20'}, None)
        with self.assertRaises(urllib.error.HTTPError):
            finance_gate.run(self.root, '101', limited)
        with self.guard() as guard:
            guard.verify()
        with self.assertRaises(finance_gate.GateError) as error:
            finance_gate.run(self.root, '101', lambda: self.fail('cooldown must remain'))
        self.assertEqual(error.exception.reason, 'cooldown')

    def test_recovery_preserves_retry_from_another_boot(self):
        def limited():
            raise urllib.error.HTTPError('https://invalid.example', 429, 'synthetic', {'Retry-After': '20'}, None)
        with self.assertRaises(urllib.error.HTTPError):
            finance_gate.run(self.root, '101', limited)
        state = pathlib.Path(self.root, finance_gate._key('101') + '.json')
        value = json.loads(state.read_text())
        value['boot'] = '0' * 64
        state.write_text(json.dumps(value))
        before = state.read_bytes()
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('unknown retry must block a deployment')
        with FinanceGuard(self.root, os.getuid(), .2, allow_uncertain=True) as guard:
            guard.verify()
        self.assertEqual(state.read_bytes(), before)

        with self.assertRaises(finance_gate.GateError) as error:
            finance_gate.run(self.root, '101', lambda: self.fail('unknown retry must remain blocked'))
        self.assertEqual(error.exception.reason, 'retry_time_unknown')

    def test_cooldown_replaced_while_locked_is_detected(self):
        def limited():
            raise urllib.error.HTTPError('https://invalid.example', 429, 'synthetic', {'Retry-After': '20'}, None)
        with self.assertRaises(urllib.error.HTTPError):
            finance_gate.run(self.root, '101', limited)
        key = finance_gate._key('101')
        state = pathlib.Path(self.root, key + '.json')
        with self.guard() as guard:
            saved = json.loads(state.read_text())
            state.write_text(json.dumps({'version': 1, 'status': 'idle', 'generation': saved['generation']}))
            with self.assertRaises(GuardError):
                guard.verify()

    def test_extreme_retry_clock_is_refused_without_overflow(self):
        def limited():
            raise urllib.error.HTTPError('https://invalid.example', 429, 'synthetic', {'Retry-After': '20'}, None)
        with self.assertRaises(urllib.error.HTTPError):
            finance_gate.run(self.root, '101', limited)
        state = pathlib.Path(self.root, finance_gate._key('101') + '.json')
        value = json.loads(state.read_text())
        value['at_wall'] = 10**500
        state.write_text(json.dumps(value))
        with self.assertRaises(GuardError):
            with self.guard():
                self.fail('invalid retry clock')

    def test_absent_gate_is_never_created(self):
        missing = os.path.join(self.tmp.name, 'absent')
        with self.assertRaises(GuardError):
            with FinanceGuard(missing, os.getuid(), 0):
                self.fail('absent gate')
        self.assertFalse(os.path.exists(missing + '.bootstrap.lock'))


if __name__ == '__main__':
    unittest.main()
