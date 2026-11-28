"""Prototype-only isolated tests: extract methods without executing host code."""
import ast
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock


class ReleaseError(Exception): pass


def extract_method(path, cls, method):
    tree = ast.parse((Path(__file__).resolve().parent / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == method)
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    namespace = {'ReleaseError':ReleaseError, 'OSError':OSError, 'ValueError':ValueError}
    exec(compile(module, path, 'exec'), namespace)
    return namespace[method]


def extract_properties(command):
    path = 'release_host.py'
    tree = ast.parse((Path(__file__).resolve().parent / path).read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'unit_properties')
    namespace = {'command':command}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), path, 'exec'), namespace)
    return namespace['unit_properties']


def extract_stop_hook_check(command):
    path = 'release_host.py'
    tree = ast.parse((Path(__file__).resolve().parent / path).read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'catalogue_empty_stop_hooks')
    namespace = {'command':command, 're':re, 'ReleaseError':ReleaseError,
                 'catalog_cd_guard':SimpleNamespace(UNIT='synthetic.service')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), path, 'exec'), namespace)
    return namespace['catalogue_empty_stop_hooks']


reconcile = extract_method('cd.py', 'Transaction', 'reconcile')
switch = extract_method('release_host.py', 'Host', 'switch')


class IntegrationTests(unittest.TestCase):
    def test_empty_stop_hooks_need_exact_typed_dbus_arrays(self):
        unit = 'o "/org/freedesktop/systemd1/unit/synthetic_2eservice"\n'
        command = MagicMock(side_effect=[unit, 'a(sasbttttuii) 0\n', 'a(sasbttttuii) 0\n'])
        extract_stop_hook_check(command)()
        self.assertEqual(command.call_count, 3)
        for bad in ('', 'a(sasbttttuii) 1', 'as 0'):
            command = MagicMock(side_effect=[unit, bad])
            with self.assertRaises(ReleaseError): extract_stop_hook_check(command)()
        with self.assertRaises(ReleaseError): extract_stop_hook_check(MagicMock(return_value='not a unit'))()

    def test_catalogue_show_requests_explicit_empty_properties(self):
        command = MagicMock(return_value='ExecStop=\nExecStopPost=\nRestart=no\n')
        properties = extract_properties(command)
        values = properties('synthetic.service', ['ExecStop','ExecStopPost','Restart'], all_values=True)
        self.assertEqual(values, {'ExecStop':'', 'ExecStopPost':'', 'Restart':'no'})
        self.assertIn('--all', command.call_args.args[0])
        properties('synthetic.service', ['Restart'])
        self.assertNotIn('--all', command.call_args.args[0])

    def test_switch_calls_guard_before_touching_files(self):
        host = MagicMock()
        host.catalogue_quiescent.side_effect = ReleaseError('busy')
        with self.assertRaises(ReleaseError): switch(host, 'new', 'txn')
        host.catalogue_quiescent.assert_called_once()
        self.assertFalse(host.root.__truediv__.called)

    def test_recovery_both_branches_refuse_when_switch_guard_refuses(self):
        j = {'old':'old','new':'new','old_sha':'a','new_sha':'b',
             'txn':'tx','mark':1,'phase':'rolling-back'}
        for initial in ('inactive', 'active', 'failed'):
            with self.subTest(initial=initial):
                host = MagicMock()
                host.current.return_value = 'old'
                host.verify.return_value = 'candidate'
                state = {'status':initial}
                host.props.side_effect = lambda: {'ActiveState':state['status'],
                                                  'MainPID':'42' if state['status'] == 'active' else '0'}
                host.stop.side_effect = lambda: state.update(status='inactive')
                host.switch.side_effect = ReleaseError('catalogue busy or untrusted')
                transaction = MagicMock()
                transaction.host = host
                transaction.gate_lock.return_value = contextlib.nullcontext(MagicMock())
                transaction.drain.return_value = (42, 'generation', 'cgroup')
                with self.assertRaises(ReleaseError): reconcile(transaction, j, True)
                host.switch.assert_called_once()
                transaction.start.assert_not_called()
                self.assertEqual(host.stop.call_count, 0 if initial == 'inactive' else 1)

    def test_recovery_can_switch_when_guard_permits(self):
        j = {'old':'old','new':'new','old_sha':'a','new_sha':'b',
             'txn':'tx','mark':1,'phase':'rolling-back'}
        host = MagicMock()
        host.current.return_value = 'old'
        host.verify.return_value = 'candidate'
        host.props.return_value = {'ActiveState':'inactive','MainPID':'0'}
        transaction = MagicMock()
        transaction.host = host
        transaction.gate_lock.return_value = contextlib.nullcontext(MagicMock())
        reconcile(transaction, j, True)
        host.switch.assert_called_once_with('new', 'tx')
        transaction.start.assert_called_once()


if __name__ == '__main__': unittest.main()
