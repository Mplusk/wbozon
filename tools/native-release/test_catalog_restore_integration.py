"""Synthetic recovery tests on extracted 24b restore-controller methods."""
import ast
import contextlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock


class ReleaseError(Exception): pass


def extract(name, namespace):
    path = str(Path(__file__).resolve().parent.parent / 'yandex-backup' / 'restore_controller.py')
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, path, 'exec'), namespace)
    return namespace[name]


class RestoreIntegrationTests(unittest.TestCase):
    def test_current_catalogue_slots_are_preserved_by_restore_policy(self):
        tree = ast.parse((Path(__file__).resolve().parent.parent / 'yandex-backup' / 'restore_transform.py').read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_preserve_current')
        ns = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                     'restore_transform.py', 'exec'), ns)
        for slot in ('content_factory_catalog_setup', 'content_factory_catalog_openai', 'content_factory_catalog_gemini'):
            self.assertTrue(ns['_preserve_current'](slot), slot)
        self.assertFalse(ns['_preserve_current']('cf_catalog_setup'))

    def test_recovery_refuses_before_rollback_for_inactive_active_and_failed(self):
        restore_files = SimpleNamespace(rollback=MagicMock(), _validate_records=MagicMock())
        namespace = {'ReleaseError':ReleaseError, '_ensure_identity':MagicMock(),
                     '_snapshot_dir':MagicMock(), '_release_space':MagicMock(),
                     '_release_restore_hold':MagicMock(), '_start_old_or_restored':MagicMock(),
                     'restore_files':restore_files, 'Path':Path}
        recover = extract('recover', namespace)
        journal = {'restore':1, 'restore_state':'modifying', 'old':'old', 'new':'old',
                   'old_sha':'sha', 'new_sha':'sha', 'phase':'restoring', 'txn':'x', 'mark':1}
        for initial in ('inactive','active','failed'):
            with self.subTest(initial=initial):
                host = MagicMock()
                state = {'status':initial}
                host.props.side_effect = lambda: {'ActiveState':state['status'],
                                                  'MainPID':'42' if state['status']=='active' else '0'}
                host.stop.side_effect = lambda: state.update(status='inactive')
                host.process.return_value = (42,'generation','cgroup')
                host.catalogue_quiescent.side_effect = ReleaseError('worker busy')
                txn = MagicMock()
                txn.host = host
                txn.gate_lock.return_value = contextlib.nullcontext(MagicMock())
                txn.drain.return_value = (42,'generation','cgroup')
                with self.assertRaises(ReleaseError): recover(txn, dict(journal))
                host.catalogue_quiescent.assert_called_once()
                self.assertEqual(host.stop.call_count, 0 if initial == 'inactive' else 1)
                restore_files.rollback.assert_not_called()

    def test_activation_and_recovery_guard_before_database_mutations(self):
        tree = ast.parse((Path(__file__).resolve().parent.parent / 'yandex-backup' / 'restore_controller.py').read_text(encoding='utf-8'))
        for name, required in (('activate', ('snapshot','apply')), ('recover', ('rollback',))):
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
            guard = [n.lineno for n in calls if n.func.attr == 'catalogue_quiescent']
            self.assertEqual(len(guard), 1)
            for operation in required:
                targets = [n.lineno for n in calls if n.func.attr == operation and
                           isinstance(n.func.value, ast.Name) and n.func.value.id == 'restore_files']
                self.assertEqual(len(targets), 1)
                self.assertLess(guard[0], targets[0])


if __name__ == '__main__': unittest.main()
