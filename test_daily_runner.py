import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from daily.__main__ import RunError, run


class DailyReportRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'work.sqlite3'
        self.db.write_bytes(b'placeholder')
        self.output = self.base / 'latest.json'
        self.output.write_text('previous', encoding='utf-8')

    def test_success_replaces_report_with_private_permissions(self):
        def generate(command, **kwargs):
            self.assertEqual(command[1:4], ['-m', 'reports', '--db'])
            kwargs['stdout'].write(b'{"ok":true}')
            return SimpleNamespace(returncode=0)
        with patch('daily.__main__.subprocess.run', side_effect=generate):
            run(self.db, self.output)
        self.assertEqual(self.output.read_text(), '{"ok":true}')
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_failure_preserves_old_report_and_cleans_partial(self):
        def failure(command, **kwargs):
            kwargs['stdout'].write(b'partial')
            return SimpleNamespace(returncode=2)
        with patch('daily.__main__.subprocess.run', side_effect=failure):
            with self.assertRaises(RunError):
                run(self.db, self.output)
        self.assertEqual(self.output.read_text(), 'previous')
        self.assertEqual(list(self.base.glob('.daily-*')), [])

    def test_missing_db_and_output_alias_rejected_without_write(self):
        with self.assertRaises(RunError):
            run(self.base / 'missing', self.output)
        with self.assertRaises(RunError):
            run(self.db, self.db)
        self.assertEqual(self.output.read_text(), 'previous')
        self.assertEqual(self.db.read_bytes(), b'placeholder')

    def test_concurrent_run_does_not_clobber_report(self):
        lock = self.base / '.latest.json.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(RunError):
                run(self.db, self.output)
        self.assertEqual(self.output.read_text(), 'previous')

    def test_end_to_end_report_uses_existing_database(self):
        from storage import Store
        self.db.unlink()
        with Store(self.db):
            pass
        run(self.db, self.output)
        payload = json.loads(self.output.read_text())
        self.assertEqual(payload['candidate_count'], 0)
        self.assertIn('generated_at', payload)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_symlink_output_rejected(self):
        self.output.unlink()
        self.output.symlink_to(self.db)
        with self.assertRaises(RunError):
            run(self.db, self.output)
        self.assertEqual(self.db.read_bytes(), b'placeholder')


if __name__ == '__main__':
    unittest.main()
