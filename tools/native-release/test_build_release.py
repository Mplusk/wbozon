import unittest
import build_release


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
