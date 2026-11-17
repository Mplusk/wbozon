import unittest
import tempfile
from pathlib import Path
import build_release
from release_host import database_access


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
