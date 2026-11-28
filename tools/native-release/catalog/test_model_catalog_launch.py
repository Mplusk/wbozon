import importlib.util
from pathlib import Path, PurePosixPath
import unittest
from unittest.mock import patch

path = Path(__file__).with_name('3dprinta-model-catalog-launch.py')
spec = importlib.util.spec_from_file_location('catalog_launcher', path)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_candidate_must_be_immediate_valid_release(self):
        root = PurePosixPath('/opt/3dprinta-portal')
        name = 'a' * 40 + '-123-1'
        good = root / 'releases' / name
        self.assertEqual(launcher.validate_candidate(root, good), good)
        for bad in (root / 'releases' / 'bad', root / 'other' / name,
                    root / 'releases' / name / 'nested'):
            with self.assertRaises(ValueError): launcher.validate_candidate(root, bad)

    def test_exec_uses_one_pinned_release_and_filters_environment(self):
        release = PurePosixPath('/opt/3dprinta-portal/releases/' + 'a' * 40 + '-123-1')
        python, script = release / 'venv/bin/python', release / 'service/content_factory_model_catalog_cli.py'
        environment = {'DATABASE_PATH':'/synthetic/auth', 'VAULT_KEY_FILE':'/synthetic/key',
                       'PYTHONPATH':'/untrusted', 'SECRET':'never-pass', 'HTTPS_PROXY':'http://invalid'}
        with patch.object(launcher, 'resolve_release', return_value=(release, python, script)), \
             patch.dict(launcher.os.environ, environment, clear=True), \
             patch.object(launcher.os, 'chdir') as chdir, \
             patch.object(launcher.os, 'execve', side_effect=OSError('synthetic')) as execute:
            self.assertEqual(launcher.main(), 1)
        chdir.assert_called_once_with(release / 'service')
        args = execute.call_args.args
        self.assertEqual(args[0], python)
        self.assertEqual(args[1], [str(python), '-B', str(script)])
        self.assertNotIn('SECRET', args[2])
        self.assertNotIn('PYTHONPATH', args[2])
        self.assertNotIn('HTTPS_PROXY', args[2])
        self.assertEqual(args[2]['PYTHONNOUSERSITE'], '1')


if __name__ == '__main__': unittest.main()
