"""The CD fence must reopen promptly if the optional key database is locked."""
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cd import backup_scoped_keys, ReleaseError


class ScopedSnapshot(unittest.TestCase):
    def test_locked_or_invalid_database_publishes_no_snapshot(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            destination = root / 'backups' / 'txn'
            destination.mkdir(parents=True)
            host = SimpleNamespace(cfg={'databases':['',str(root/'vault.db')]}, state=root, gate=root/'gate')
            source = root / 'vault.db.avito-keys'
            with contextlib.closing(sqlite3.connect(source)) as writer:
                writer.execute('CREATE TABLE invalid (id INTEGER)')
                source.chmod(0o600)
                writer.execute('BEGIN EXCLUSIVE')
                # The systemd acceptance suite separately validates the real root-owned snapshot directory.
                with patch('cd.secure',return_value=destination), patch('cd.SCOPED_BACKUP_SECONDS',0.2):
                    started = time.monotonic()
                    with self.assertRaisesRegex(ReleaseError,'timed out'):
                        backup_scoped_keys(host,'txn')
                    self.assertLess(time.monotonic()-started,3)
                    writer.rollback()
                    with self.assertRaisesRegex(ReleaseError,'verification failed'):
                        backup_scoped_keys(host,'txn')
            self.assertEqual(list(destination.iterdir()),[])
