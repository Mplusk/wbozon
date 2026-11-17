"""Synthetic, offline verification of deployment artifact provenance."""
import hashlib
import io
import json
import tarfile
import unittest
import zipfile
from unittest.mock import patch

import github_artifact as verifier


class FakeGitHub:
    def __init__(self, data, archive):
        self.data, self.archive = data, archive

    def json(self, path):
        return self.data[path]

    def artifact_zip(self, artifact_id):
        return self.archive


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.sha, self.run, self.attempt, self.artifact = 'a' * 40, '12345', '1', '67890'
        wheel, bootstrap = b'synthetic-wheel', b'synthetic-bootstrap'
        self.wheel_hash = hashlib.sha256(wheel).hexdigest()
        self.files = {
            'service/portal.py': b'print("synthetic")\n',
            'service/finance_gate.py': b'# gate\n',
            'wheelhouse/example-1-py3-none-any.whl': wheel,
            'bootstrap/pip-26.3-py3-none-any.whl': bootstrap,
            'requirements-offline.lock': ('example==1 --hash=sha256:' + hashlib.sha256(wheel).hexdigest() + '\n').encode(),
            'requirements-bootstrap.lock': b'synthetic-bootstrap-lock\n',
            'test_native.py': b'# synthetic test\n',
            'PROVENANCE.json': json.dumps({'repository': verifier.REPO, 'source_commit': self.sha,
                                            'run_id': self.run, 'run_attempt': self.attempt}).encode(),
        }
        self.bootstrap_hash = hashlib.sha256(bootstrap).hexdigest()
        self.request = {'commit': self.sha, 'run_id': self.run, 'build_attempt': self.attempt,
                        'artifact_id': self.artifact, 'tar_sha256': ''}
        self.tree = []
        for path, data in self.files.items():
            if path.startswith('service/'):
                self.tree.append({'path': 'services/email-auth/' + path[8:], 'type': 'blob',
                                  'mode': '100644', 'sha': verifier._blob_hash(data)})
        for name in ('requirements-offline.lock', 'requirements-bootstrap.lock', 'test_native.py'):
            self.tree.append({'path': 'tools/native-test-bundle/' + name, 'type': 'blob',
                              'mode': '100644', 'sha': verifier._blob_hash(self.files[name])})
        self.client = self.build()

    def build(self, *, symlink=False, duplicate=False, special=None):
        files = dict(self.files)
        files['FILES.sha256'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                        for name, data in sorted(files.items())).encode()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            for name, data in sorted(files.items()):
                item = tarfile.TarInfo('./' + name)
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
            if symlink:
                item = tarfile.TarInfo('./service/escape')
                item.type, item.linkname = tarfile.SYMTYPE, '/etc/passwd'
                tar.addfile(item)
            if duplicate:
                item = tarfile.TarInfo('./service/portal.py')
                item.size = 1
                tar.addfile(item, io.BytesIO(b'x'))
            if special is not None:
                item = tarfile.TarInfo('./service/special')
                item.type = special
                tar.addfile(item)
        archive = raw.getvalue()
        digest = hashlib.sha256(archive).hexdigest()
        self.request['tar_sha256'] = digest
        archive_name = 'native-portal-' + self.sha + '.tar.gz'
        data = io.BytesIO()
        with zipfile.ZipFile(data, mode='w') as z:
            z.writestr(archive_name, archive)
            z.writestr('SHA256SUMS', digest + '  ' + archive_name + '\n')
        zipped = data.getvalue()
        base = '/actions/runs/' + self.run
        repo = {'id': 42, 'full_name': verifier.REPO}
        jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'}
                for name in verifier.REQUIRED_JOBS]
        response = {
            base: {'id': int(self.run), 'repository': repo, 'head_repository': repo,
                   'event': 'push', 'path': '.github/workflows/ci.yml', 'head_branch': 'main', 'head_sha': self.sha},
            base + '/attempts/' + self.attempt + '/jobs?per_page=100': {'jobs': jobs, 'total_count': len(jobs)},
            '/git/ref/heads/main': {'object': {'sha': self.sha}},
            '/actions/artifacts/' + self.artifact: {'id': int(self.artifact),
                'name': 'native-portal-tested-' + self.sha + '-' + self.run + '-' + self.attempt,
                'expired': False, 'digest': 'sha256:' + hashlib.sha256(zipped).hexdigest(),
                'workflow_run': {'id': int(self.run), 'head_sha': self.sha, 'head_branch': 'main',
                                 'repository_id': 42, 'head_repository_id': 42}},
            '/git/trees/' + self.sha + '?recursive=1': {'truncated': False, 'tree': self.tree},
        }
        return FakeGitHub(response, zipped)

    def verify(self):
        with patch.object(verifier, 'BOOTSTRAP_HASH', self.bootstrap_hash), patch.object(verifier, 'APPROVED_WHEELS', {self.wheel_hash}):
            return verifier.verify_request(self.client, self.request)

    def test_valid_artifact_is_bound_to_successful_run_and_git_tree(self):
        self.assertEqual(self.verify()['service/portal.py'], self.files['service/portal.py'])

    def test_stale_main_failed_job_and_wrong_branch_are_refused(self):
        ref = self.client.data['/git/ref/heads/main']['object']
        ref['sha'] = 'b' * 40
        with self.assertRaises(verifier.VerificationError):
            self.verify()
        ref['sha'] = self.sha
        jobs = self.client.data['/actions/runs/' + self.run + '/attempts/1/jobs?per_page=100']['jobs']
        jobs[0]['conclusion'] = 'failure'
        with self.assertRaises(verifier.VerificationError):
            self.verify()
        jobs[0]['conclusion'] = 'success'
        self.client.data['/actions/runs/' + self.run]['head_branch'] = 'feature'
        with self.assertRaises(verifier.VerificationError):
            self.verify()

    def test_altered_artifact_or_source_tree_is_refused(self):
        self.client.archive += b'x'
        with self.assertRaises(verifier.VerificationError):
            self.verify()
        self.client = self.build()
        self.tree[0]['sha'] = '0' * 40
        with self.assertRaises(verifier.VerificationError):
            self.verify()

    def test_symlink_duplicate_and_untrusted_source_type_are_refused(self):
        for kwargs in ({'symlink': True}, {'duplicate': True},
                       {'special': tarfile.GNUTYPE_SPARSE}, {'special': tarfile.CONTTYPE}):
            self.client = self.build(**kwargs)
            with self.assertRaises(verifier.VerificationError):
                self.verify()
        self.client = self.build()
        self.tree[0]['mode'] = '120000'
        with self.assertRaises(verifier.VerificationError):
            self.verify()

    def test_invalid_request_and_expired_artifact_are_refused(self):
        self.request['commit'] = '../bad'
        with self.assertRaises(verifier.VerificationError):
            self.verify()
        self.request['commit'] = self.sha
        self.client.data['/actions/artifacts/' + self.artifact]['expired'] = True
        with self.assertRaises(verifier.VerificationError):
            self.verify()

    def test_decompressed_tar_and_wheel_set_are_bounded(self):
        with patch.object(verifier, 'MAX_TAR', 1024), self.assertRaises(verifier.VerificationError):
            self.verify()
        with patch.object(verifier, 'APPROVED_WHEELS', set()), self.assertRaises(verifier.VerificationError):
            verifier.verify_wheels(self.files)


if __name__ == '__main__':
    unittest.main()
