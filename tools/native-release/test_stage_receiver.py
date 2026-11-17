"""Synthetic tests for an unprivileged, staging-only SSH receiver."""
import fcntl
import hashlib
import io
import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import stage_receiver as receiver


class StageReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)
        self.commit = 'a' * 40
        self.data = b'synthetic artifact bytes'
        self.digest = hashlib.sha256(self.data).hexdigest()

    def receive(self, data=None, commit=None, digest=None, size=None, now=None):
        body = self.data if data is None else data
        return receiver.receive(self.directory, io.BytesIO(body), commit or self.commit,
                                digest or self.digest, len(self.data) if size is None else size, now=now)

    def child(self, code):
        env = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp.name, 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': str(pathlib.Path(receiver.__file__).parent)}
        process = subprocess.Popen([sys.executable, '-c', code, str(self.directory), self.commit, self.digest],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def line(self, process):
        readable, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(readable, 'child did not become ready')
        return process.stdout.readline().strip()

    def test_valid_artifact_can_be_received_twice(self):
        self.receive()
        path = self.directory / (self.commit + '.tar.gz')
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.receive()
        self.assertEqual(path.read_bytes(), self.data)

    def test_bounded_versioned_cache_keeps_legacy_and_new_upload(self):
        self.receive()
        for i in range(1, 15):
            receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                             len(self.data), run_id=str(i), run_attempt='1')
        self.assertEqual(len(list(self.directory.glob('*-*-*.tar.gz'))), 12)
        self.assertTrue((self.directory / (self.commit + '.tar.gz')).exists())
        self.assertTrue((self.directory / (self.commit + '-14-1.tar.gz')).exists())

    def test_reserves_byte_capacity_for_next_versioned_upload(self):
        with patch.object(receiver, 'MAX_BYTES', len(self.data)), patch.object(receiver, 'MAX_TOTAL_BYTES', len(self.data) * 3):
            for i in range(1, 7):
                receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                                 len(self.data), run_id=str(i), run_attempt='1')
            self.assertEqual(len(list(self.directory.glob('*-*-*.tar.gz'))), 2)

    def test_quota_after_interrupted_cleanup_allows_verified_new_upload(self):
        prior = self.directory / (self.commit + '-1-1.tar.gz')
        prior.write_bytes(self.data)
        prior.chmod(0o600)
        with patch.object(receiver, 'MAX_ARTIFACTS', 1):
            with self.assertRaises(receiver.Rejected):
                receiver.receive(self.directory, io.BytesIO(b'bad'), self.commit, self.digest,
                                 len(self.data), run_id='2', run_attempt='1')
            self.assertEqual(prior.read_bytes(), self.data)
            receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                             len(self.data), run_id='2', run_attempt='1')
            self.assertFalse(prior.exists())

    def test_same_commit_from_different_workflow_attempts_does_not_conflict(self):
        receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                         len(self.data), run_id='123456', run_attempt='1')
        another = b'a different workflow artifact'
        receiver.receive(self.directory, io.BytesIO(another), self.commit, hashlib.sha256(another).hexdigest(),
                         len(another), run_id='123456', run_attempt='2')
        self.assertEqual((self.directory / (self.commit + '-123456-1.tar.gz')).read_bytes(), self.data)
        self.assertEqual((self.directory / (self.commit + '-123456-2.tar.gz')).read_bytes(), another)

    def test_old_and_new_names_share_the_storage_limit(self):
        self.receive()
        with patch.object(receiver, 'MAX_ARTIFACTS', 1), self.assertRaises(receiver.Rejected):
            receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                             len(self.data), run_id='123456', run_attempt='1')
        self.assertEqual((self.directory / (self.commit + '.tar.gz')).read_bytes(), self.data)

    def test_same_run_and_attempt_cannot_overwrite_different_payload(self):
        receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                         len(self.data), run_id='123456', run_attempt='1')
        other = b'an altered artifact'
        with self.assertRaises(receiver.Rejected):
            receiver.receive(self.directory, io.BytesIO(other), self.commit, hashlib.sha256(other).hexdigest(),
                             len(other), run_id='123456', run_attempt='1')
        self.assertEqual((self.directory / (self.commit + '-123456-1.tar.gz')).read_bytes(), self.data)

    def test_invalid_workflow_identity_cannot_escape(self):
        with self.assertRaises(receiver.Rejected):
            receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                             len(self.data), run_id='../bad', run_attempt='1')

    def test_digest_mismatch_never_publishes_or_leaves_partial(self):
        with self.assertRaises(receiver.Rejected):
            self.receive(data=b'x' * len(self.data))
        self.assertFalse((self.directory / (self.commit + '.tar.gz')).exists())
        self.assertFalse((self.directory / ('.' + self.commit + '.partial')).exists())

    def test_truncated_or_long_stream_is_refused(self):
        for data in (self.data[:-1], self.data + b'x'):
            with self.subTest(size=len(data)), self.assertRaises(receiver.Rejected):
                self.receive(data=data)
        self.assertFalse((self.directory / (self.commit + '.tar.gz')).exists())

    def test_existing_conflicting_artifact_is_not_overwritten(self):
        path = self.directory / (self.commit + '.tar.gz')
        path.write_bytes(b'existing')
        path.chmod(0o600)
        with self.assertRaises(receiver.Rejected):
            self.receive()
        self.assertEqual(path.read_bytes(), b'existing')

    def test_invalid_metadata_cannot_escape_directory(self):
        with self.assertRaises(receiver.Rejected):
            self.receive(commit='../outside')
        with self.assertRaises(receiver.Rejected):
            self.receive(size=receiver.MAX_BYTES + 1)

    def test_symlink_and_unexpected_entry_are_refused(self):
        path = self.directory / (self.commit + '.tar.gz')
        path.symlink_to('/etc/passwd')
        with self.assertRaises((receiver.Rejected, OSError)):
            self.receive()
        path.unlink()
        (self.directory / 'unrelated').write_text('preserve')
        with self.assertRaises(receiver.Rejected):
            self.receive()
        self.assertEqual((self.directory / 'unrelated').read_text(), 'preserve')

    def test_competing_receiver_is_refused(self):
        path = self.directory / '.receiver.lock'
        path.write_bytes(b'')
        path.chmod(0o600)
        with path.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(receiver.Rejected):
                self.receive()

    def test_successful_upload_does_not_remove_old_artifacts(self):
        self.receive()
        old = self.directory / (self.commit + '.tar.gz')
        os.utime(old, (1, 1))
        self.receive(commit='b' * 40)
        self.assertTrue(old.exists())
        self.assertTrue((self.directory / ('b' * 40 + '.tar.gz')).is_file())

    def test_unexpired_retention_limit_refuses_new_artifact(self):
        self.receive()
        with patch.object(receiver, 'MAX_ARTIFACTS', 1):
            with self.assertRaises(receiver.Rejected):
                self.receive(commit='b' * 40)
        self.assertTrue((self.directory / (self.commit + '.tar.gz')).is_file())

    def test_failed_upload_does_not_remove_expired_artifacts(self):
        self.receive()
        old = self.directory / (self.commit + '.tar.gz')
        os.utime(old, (1, 1))
        with patch.object(receiver, 'MAX_ARTIFACTS', 2):
            with self.assertRaises(receiver.Rejected):
                self.receive(data=self.data[:-1], commit='b' * 40)
        self.assertEqual(old.read_bytes(), self.data)

    def test_total_size_and_upload_rate_are_bounded(self):
        with patch.object(receiver, 'MAX_TOTAL_BYTES', len(self.data) * 2 - 1):
            self.receive()
            with self.assertRaises(receiver.Rejected):
                self.receive(commit='b' * 40)
        self.assertTrue((self.directory / (self.commit + '.tar.gz')).exists())
        with patch.object(receiver, 'MAX_HOURLY_ATTEMPTS', 2):
            with self.assertRaises(receiver.Rejected):
                self.receive(commit='c' * 40)

    def test_free_space_reserve_refuses_upload(self):
        with patch.object(receiver, 'MIN_FREE_BYTES', 10**30):
            with self.assertRaises(receiver.Rejected):
                self.receive()
        self.assertFalse((self.directory / (self.commit + '.tar.gz')).exists())

    def test_shell_metacharacters_in_command_are_rejected(self):
        command = 'stage ' + self.commit + ' ' + self.digest + ' 24; id'
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), patch.object(receiver, 'receive') as call:
            self.assertEqual(receiver.main(), 1)
            call.assert_not_called()

    def test_recovery_after_process_is_killed_during_upload(self):
        code = ('import sys,stage_receiver as r\n'
                'class Slow:\n'
                ' def __init__(self): self.first=True\n'
                ' def read(self,n):\n'
                '  if self.first: self.first=False; return b"a"\n'
                '  print("partial",flush=True); sys.stdin.readline(); return b""\n'
                'r.receive(sys.argv[1],Slow(),sys.argv[2],sys.argv[3],24)')
        process = self.child(code)
        self.assertEqual(self.line(process), 'partial')
        process.kill()
        process.wait(timeout=5)
        self.assertTrue((self.directory / ('.' + self.commit + '.partial')).exists())
        self.receive()
        self.assertFalse((self.directory / ('.' + self.commit + '.partial')).exists())
        self.assertEqual((self.directory / (self.commit + '.tar.gz')).read_bytes(), self.data)

    def test_recovery_after_kill_between_link_and_unlink(self):
        code = ('import io,os,sys,stage_receiver as r\n'
                'link=os.link\n'
                'def paused(*a,**kw):\n'
                ' link(*a,**kw); print("linked",flush=True); sys.stdin.readline()\n'
                'os.link=paused\n'
                'data=b"synthetic artifact bytes"\n'
                'r.receive(sys.argv[1],io.BytesIO(data),sys.argv[2],sys.argv[3],len(data))')
        process = self.child(code)
        self.assertEqual(self.line(process), 'linked')
        process.kill()
        process.wait(timeout=5)
        self.assertTrue((self.directory / ('.' + self.commit + '.partial')).exists())
        self.receive()
        self.assertFalse((self.directory / ('.' + self.commit + '.partial')).exists())
        self.assertEqual((self.directory / (self.commit + '.tar.gz')).read_bytes(), self.data)

    def test_new_run_name_recovers_kill_between_link_and_unlink(self):
        code = ('import io,os,sys,stage_receiver as r\n'
                'link=os.link\n'
                'def paused(*a,**kw):\n'
                ' link(*a,**kw); print("linked",flush=True); sys.stdin.readline()\n'
                'os.link=paused\n'
                'data=b"synthetic artifact bytes"\n'
                'r.receive(sys.argv[1],io.BytesIO(data),sys.argv[2],sys.argv[3],len(data),run_id="55",run_attempt="2")')
        process = self.child(code)
        self.assertEqual(self.line(process), 'linked')
        process.kill()
        process.wait(timeout=5)
        partial = self.directory / ('.' + self.commit + '-55-2.partial')
        self.assertTrue(partial.exists())
        receiver.receive(self.directory, io.BytesIO(self.data), self.commit, self.digest,
                         len(self.data), run_id='55', run_attempt='2')
        self.assertFalse(partial.exists())
        self.assertEqual((self.directory / (self.commit + '-55-2.tar.gz')).read_bytes(), self.data)

    def test_alarm_cleans_own_partial(self):
        code = ('import signal,sys,stage_receiver as r\n'
                'signal.signal(signal.SIGALRM,r._deadline)\n'
                'class Slow:\n'
                ' def read(self,n):\n'
                '  print("waiting",flush=True); sys.stdin.readline(); return b""\n'
                'try: r.receive(sys.argv[1],Slow(),sys.argv[2],sys.argv[3],24)\n'
                'except TimeoutError: print("timeout",flush=True)')
        process = self.child(code)
        self.assertEqual(self.line(process), 'waiting')
        process.send_signal(signal.SIGALRM)
        self.assertEqual(self.line(process), 'timeout')
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertFalse((self.directory / ('.' + self.commit + '.partial')).exists())

    def test_untrusted_partial_symlink_is_preserved_and_refused(self):
        partial = self.directory / ('.' + self.commit + '.partial')
        partial.symlink_to('/etc/passwd')
        with self.assertRaises(receiver.Rejected):
            self.receive()
        self.assertTrue(partial.is_symlink())


if __name__ == '__main__':
    unittest.main()
