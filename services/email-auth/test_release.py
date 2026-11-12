import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release', Path(__file__).parent / 'deploy/release.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
A, B = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
REPO = 'ghcr.io/cossomoj/email-auth'


class FakeDocker:
    def __init__(self):
        self.items = {}; self.calls = []; self.bad = set(); self.fail_run = False
    def inspect(self, name): return self.items.get(name)
    def health(self, name): return name not in self.bad and name in self.items
    def call(self, args, timeout=60):
        self.calls.append(args)
        command = args[0]
        if command == 'run':
            name = args[args.index('--name') + 1]
            if self.fail_run and name == r.NAME: raise r.DeployError('run failed')
            if name in self.items: raise r.DeployError('exists')
            self.items[name] = {'owned': True, 'image': args[-1], 'running': True}
        elif command == 'stop': self.items[args[-1]]['running'] = False
        elif command == 'start': self.items[args[-1]]['running'] = True
        elif command == 'rename': self.items[args[2]] = self.items.pop(args[1])
        elif command == 'rm': self.items.pop(args[-1])
        return ''


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        (self.base / 'state').mkdir(mode=0o700)
        self.c = {'state_dir': str(self.base / 'state'), 'image_repository': REPO,
                  'env_file': str(self.base / 'runtime.env'), 'data_dir': str(self.base / 'data'),
                  'port': 18731, 'network': '3dprinta-auth-backend'}
        self.d = FakeDocker(); self.e = r.Release(self.c, self.d)
    def tearDown(self): self.tmp.cleanup()
    def test_deploy_and_noop(self):
        self.assertEqual(self.e.deploy(A), 'deployed')
        before = len(self.d.calls)
        self.assertEqual(self.e.deploy(A), 'already_active')
        self.assertEqual(len(self.d.calls), before)
    def test_reject_tag_and_injection(self):
        for digest in ('latest', B + ';echo BAD', 'sha256:' + 'g' * 64):
            with self.assertRaises(r.DeployError): self.e.deploy(digest)
        self.assertEqual(self.d.calls, [])
    def test_refuse_neighbor(self):
        self.d.items[r.NAME] = {'owned': False, 'image': 'other', 'running': True}
        with self.assertRaises(r.DeployError): self.e.deploy(A)
        self.assertEqual(self.d.calls, [])
    def test_candidate_failure_preserves_current(self):
        self.e.deploy(A)
        original = self.d.health
        self.d.health = lambda n: False if '-candidate-' in n else original(n)
        with self.assertRaises(r.DeployError): self.e.deploy(B)
        self.assertEqual(self.d.items[r.NAME]['image'], REPO + '@' + A)
        self.assertIsNone(self.e.state()['pending'])
    def test_activation_failure_rolls_back(self):
        self.e.deploy(A); self.d.fail_run = True
        with self.assertRaises(r.DeployError): self.e.deploy(B)
        self.assertEqual(self.d.items[r.NAME]['image'], REPO + '@' + A)
        self.assertTrue(self.d.items[r.NAME]['running'])
        self.assertIsNone(self.e.state()['pending'])
    def test_health_failure_preserves_failed_container_and_restores(self):
        self.e.deploy(A)
        self.d.health = lambda n: n in self.d.items and self.d.items[n]['image'] != REPO + '@' + B
        # Allow synthetic candidate while failing production only.
        self.d.health = lambda n: '-candidate-' in n or self.d.items.get(n, {}).get('image') != REPO + '@' + B
        with self.assertRaises(r.DeployError): self.e.deploy(B)
        self.assertEqual(self.d.items[r.NAME]['image'], REPO + '@' + A)
        self.assertTrue(any('-failed-' in n for n in self.d.items))
    def test_explicit_rollback(self):
        self.e.deploy(A); self.e.deploy(B); self.e.rollback()
        self.assertEqual(self.d.items[r.NAME]['image'], REPO + '@' + A)
    def test_interrupted_before_rename_recover(self):
        self.e.deploy(A); s = self.e.state()
        s['pending'] = {'target': REPO + '@' + B, 'before': REPO + '@' + A,
                        'backup': r.NAME + '-previous-' + '1' * 16}
        self.e.save(s)
        with self.assertRaises(r.DeployError): self.e.deploy(B)
        self.e.recover()
        self.assertIsNone(self.e.state()['pending'])
    def test_no_secrets_or_public_port_in_candidate(self):
        self.e.deploy(A)
        runs = [a for a in self.d.calls if a[0] == 'run']
        candidate = runs[0]; production = runs[1]
        self.assertNotIn('--env-file', candidate)
        self.assertEqual(candidate[candidate.index('--network') + 1], 'none')
        self.assertNotIn('-p', candidate)
        self.assertEqual(production[production.index('-p') + 1], '127.0.0.1:18731:8000')
        self.assertNotIn('--privileged', production)
        self.assertNotIn('docker.sock', ' '.join(production))
    def test_lock_prevents_concurrent_activation(self):
        with r.locked(self.c):
            with self.assertRaises(r.DeployError):
                with r.locked(self.c): pass
    def test_state_rejects_unrelated_previous(self):
        self.e.save({'active': None, 'previous': 'list-sign-proxy-1', 'pending': None})
        with self.assertRaises(r.DeployError): self.e.rollback()
        self.assertEqual(self.d.calls, [])
    def test_path_rejects_symlink_and_world_readable(self):
        f = self.base / 'secret'; f.write_text('synthetic'); f.chmod(0o600)
        link = self.base / 'link'; link.symlink_to(f)
        with self.assertRaises(r.DeployError): r.checked_path(str(link), 'file')
        f.chmod(0o644)
        with self.assertRaises(r.DeployError): r.checked_path(str(f), 'file')
    def test_config_schema_and_network(self):
        f = self.base / 'config.json'; f.write_text(json.dumps(self.c)); f.chmod(0o600)
        with patch.object(r, 'checked_path', side_effect=lambda p, *a, **k: Path(p)):
            self.assertEqual(r.load_config(str(f)), self.c)
            f.write_text(json.dumps(dict(self.c, network='host')))
            with self.assertRaises(r.DeployError): r.load_config(str(f))
    def test_state_write_mode(self):
        self.e.deploy(A)
        self.assertEqual(self.e.path.stat().st_mode & 0o777, 0o600)
    def test_activation_timeout_does_not_recover(self):
        self.e.deploy(A)
        original = self.d.call
        def call(args, timeout=60):
            if args[0] == 'run' and args[args.index('--name') + 1] == r.NAME:
                self.d.calls.append(args)
                raise r.UncertainDockerOutcome('synthetic timeout')
            return original(args, timeout)
        self.d.call = call
        with self.assertRaises(r.UncertainDockerOutcome): self.e.deploy(B)
        self.assertIsNotNone(self.e.state()['pending'])
        self.assertTrue(self.e.uncertain_path.exists())
        before = len(self.d.calls)
        with self.assertRaises(r.DeployError): self.e.recover()
        with self.assertRaises(r.DeployError): self.e.deploy(B)
        self.assertEqual(len(self.d.calls), before)
    def test_candidate_timeout_no_cleanup_or_activation(self):
        original = self.d.call
        def call(args, timeout=60):
            if args[0] == 'run':
                self.d.calls.append(args)
                raise r.UncertainDockerOutcome('synthetic timeout')
            return original(args, timeout)
        self.d.call = call
        with self.assertRaises(r.UncertainDockerOutcome): self.e.deploy(A)
        self.assertTrue(self.e.uncertain_path.exists())
        self.assertFalse(any(a[0] in ('rm', 'stop', 'rename') for a in self.d.calls))
    def test_timeout_classification(self):
        with patch.object(r.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 1)):
            with self.assertRaises(r.UncertainDockerOutcome): r.Docker().call(['run'])
    def test_untrusted_parent_rejected(self):
        parent = self.base / 'unsafe'; parent.mkdir(mode=0o777); parent.chmod(0o777)
        f = parent / 'secret'; f.write_text('synthetic'); f.chmod(0o600)
        with self.assertRaisesRegex(r.DeployError, 'Parent directories'):
            r.checked_path(str(f), 'file')
    def test_runtime_secret_inside_data_rejected(self):
        c = dict(self.c, env_file=str(self.base / 'data/runtime.env'))
        f = self.base / 'config.json'; f.write_text(json.dumps(c)); f.chmod(0o600)
        with patch.object(r, 'checked_path', side_effect=lambda p, *a, **k: Path(p)):
            with self.assertRaisesRegex(r.DeployError, 'inside application data'): r.load_config(str(f))


if __name__ == '__main__': unittest.main()
