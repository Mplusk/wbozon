"""Isolated POSIX tests; the production entry point is never invoked."""
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

if os.name == 'posix':
    spec = importlib.util.spec_from_file_location('controller_swap', pathlib.Path(__file__).with_name('controller_swap.py'))
    swap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(swap)


@unittest.skipUnless(os.name == 'posix', 'requires POSIX flock and filesystem')
class ControllerSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.state, self.lib, self.root = (self.base / p for p in ('state', 'lib', 'root'))
        for p in (self.state, self.lib, self.root, self.root / 'releases', self.root / 'releases' / 'release1'):
            p.mkdir(mode=0o700)
        (self.root / 'current').symlink_to('releases/release1')
        (self.state / 'deploy.lock').write_bytes(b'')
        (self.state / 'deploy.lock').chmod(0o600)
        (self.state / 'active.json').write_text('{"release":"release1","manifest":"test"}')
        (self.lib / 'cd.py').write_bytes(b'old')
        (self.lib / 'cd.py').chmod(0o644)
        self.candidate = self.state / 'candidate.py'
        self.candidate.write_bytes(b'new')
    def tearDown(self):
        self.tmp.cleanup()

    def run_swap(self, old=b'old', new=b'new', verify=lambda *args: None, preflight=lambda: None):
        return swap._perform(self.state, self.lib, self.root, self.candidate,
                             swap.digest(old), swap.digest(new), 'release1', verify, preflight,
                             os.geteuid(), boundary=self.base)

    def test_install_saves_exact_backup_and_readback(self):
        seen = []
        self.run_swap(verify=lambda release, manifest: seen.append((release, manifest)))
        self.assertEqual(seen, [('release1', 'test')])
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'new')
        self.assertEqual((self.state / ('cd-before-' + swap.digest(b'old') + '.py')).read_bytes(), b'old')

    def test_pending_blocks_swap(self):
        (self.state / 'pending.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'pending'):
            self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_unexpected_candidate_and_release_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.run_swap(new=b'different')
        (self.state / 'active.json').write_text('{"release":"other"}')
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_release_verification_failure_blocks_swap(self):
        def fail(*args):
            raise ValueError('manifest mismatch')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.run_swap(verify=fail)
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_existing_unexpected_backup_fails_closed(self):
        backup = self.state / ('cd-before-' + swap.digest(b'old') + '.py')
        backup.write_bytes(b'wrong')
        backup.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'backup mismatch'):
            self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_busy_lock_refuses_concurrent_change(self):
        with open(self.state / 'deploy.lock', 'rb') as held:
            swap.fcntl.flock(held.fileno(), swap.fcntl.LOCK_EX | swap.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_candidate_symlink_refused(self):
        self.candidate.unlink()
        self.candidate.symlink_to(self.lib / 'cd.py')
        with self.assertRaisesRegex(ValueError, 'untrusted'):
            self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_world_writable_ancestor_is_rejected(self):
        self.state.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'untrusted'):
            self.run_swap()
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_wrong_owner_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'untrusted'):
            swap.trusted(self.state, True, os.geteuid() + 1, boundary=self.base)

    def test_path_outside_fixture_boundary_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            swap.trusted(self.base.parent, True, os.geteuid(), boundary=self.base)

    def test_failed_health_or_main_preflight_preserves_controller(self):
        def fail():
            raise ValueError('preflight failed')
        with self.assertRaisesRegex(ValueError, 'preflight'):
            self.run_swap(preflight=fail)
        self.assertEqual((self.lib / 'cd.py').read_bytes(), b'old')

    def test_preflight_rejects_wrong_main_and_bad_health(self):
        import io
        from types import SimpleNamespace
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def __init__(self, payload):
                self.payload = payload
            def open(self, *args, **kwargs):
                return Response(self.payload)
        right_head = (swap.EXPECTED_MAIN + '\trefs/heads/main\n').encode()
        with patch.object(swap.subprocess, 'run', return_value=SimpleNamespace(stdout=b'wrong\n')):
            with self.assertRaisesRegex(ValueError, 'main'):
                swap.preflight()
        with patch.object(swap.subprocess, 'run', return_value=SimpleNamespace(stdout=right_head)), \
             patch.object(swap.urllib.request, 'build_opener', return_value=Opener(b'{"status":"bad"}')):
            with self.assertRaisesRegex(ValueError, 'health'):
                swap.preflight()


if __name__ == '__main__':
    unittest.main()
