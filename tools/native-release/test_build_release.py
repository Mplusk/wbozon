import unittest
import tempfile
from pathlib import Path
import build_release
from release_host import ReleaseError, compatible, database_access
from cd import Transaction
from unittest.mock import Mock


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.files = {p: b'x' for p in ('service/portal.py', 'service/gunicorn.conf.py',
                                        'requirements-offline.lock', 'test_native.py')}

    def test_only_supported_paths_are_accepted(self):
        build_release._validate_files(self.files)
        for name in ('service/../../etc/config', 'service//file', '/service/file', 'arbitrary',
                     'wheelhouse/../../wheel.whl', 'service/./file', 'bootstrap/not-a-wheel'):
            with self.subTest(name=name), self.assertRaises(build_release.BuildError):
                build_release._validate_files({**self.files, name: b'bad'})

    def test_missing_service_or_nonbinary_content_refused_before_writes(self):
        with self.assertRaises(build_release.BuildError):
            build_release._validate_files({'service/portal.py': b'x'})
        with self.assertRaises(build_release.BuildError):
            build_release._validate_files({**self.files, 'test_native.py': 'invalid'})

    def test_sql_in_an_existing_handler_or_new_module_requires_migration_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'service').mkdir()
            module = root / 'service' / 'portal.py'
            module.write_text('def handle(db): return 1')
            original = database_access(root)
            module.write_text('def handle(db): db.execute("ALTER TABLE sessions ADD COLUMN flag TEXT")')
            self.assertNotEqual(database_access(root), original)
            module.rename(root / 'service' / 'other.py')
            self.assertNotEqual(database_access(root), original)

    def test_changed_handler_cannot_hide_schema_change_behind_sql_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ('old', 'new')]
            for p in paths:
                svc = p / 'service'
                svc.mkdir(parents=True)
                for name in ('app.py', 'finance_gate.py', 'external_keys.py', 'oauth_mcp.py', 'gunicorn.conf.py'):
                    (svc / name).write_text('')
                (svc / 'portal.py').write_text('class Vault: pass\nclass Portal:\n def __init__(self): pass\n'
                    ' def read_gateway(self): pass\ndef create_portal(): pass\ndef provider_read(): pass\n'
                    'def sql(db,q): db.execute(q)\n')
            with (paths[1] / 'service/portal.py').open('a') as f:
                f.write('def handle(db): sql(db, "ALTER " + "TABLE sessions ADD flag INT")\n')
            with self.assertRaises(ReleaseError):
                compatible(*paths)
            for p in paths:
                module = p / 'service/portal.py'
                module.write_text((paths[0] / 'service/portal.py').read_text().split('# helper')[0] +
                                  '# helper\nimport helper\ndef migrate(db): helper.update(db)\n')
                (p / 'service/helper.py').write_text('def update(db): pass')
            (paths[1] / 'service/helper.py').write_text('def update(db): getattr(db, "execute")("ALTER " + "TABLE sessions ADD flag INT")')
            with self.assertRaises(ReleaseError):
                compatible(*paths)

    def test_schema_mismatch_keeps_ingress_closed_and_receipt_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Mock(state=Path(tmp))
            host.check_schema.side_effect = ReleaseError('schema mismatch')
            transaction = Transaction(host)
            with self.assertRaises(ReleaseError):
                transaction.complete({'schemas': ['old'], 'txn': 'fixture', 'mark': 1}, 'old', 'checksum')
            host.barrier.assert_called_once_with('fixture', 1, True)
            self.assertFalse(transaction.active.exists())
