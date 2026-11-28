"""The bootstrap installer ships its guard without dropping legacy modules."""
import ast
from pathlib import Path
import unittest


class InstallerInventoryTests(unittest.TestCase):
    def test_guard_and_legacy_controller_files_are_installed_once(self):
        path = Path(__file__).resolve().parent / 'install_host.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        candidates = []
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                try: values = ast.literal_eval(node.iter)
                except (ValueError, TypeError): continue
                if isinstance(values, (tuple, list)) and 'release_host.py' in values:
                    candidates.append(values)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(tuple(candidates[0]), ('cd.py', 'release_host.py', 'catalog_cd_guard.py',
            'build_release.py', 'github_artifact.py', 'finance_guard.py', 'native_release.py', 'dispatch.py'))


if __name__ == '__main__': unittest.main()
