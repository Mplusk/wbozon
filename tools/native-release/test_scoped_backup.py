"""The CD fence must reopen promptly if the optional key database is locked."""
import contextlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cd import backup_scoped_keys, ReleaseError, CALCULATOR_SCHEMA, CALCULATOR_DRAFT_SCHEMA
from release_host import Host


class ScopedSnapshot(unittest.TestCase):
    def test_calculator_snapshots_accept_only_the_reviewed_draft_migration(self):
        service = Path(__file__).resolve().parents[2] / 'services' / 'email-auth'
        sys.path.insert(0, str(service))
        try:
            import cost_catalog
            for migrated in (False, True):
                with self.subTest(migrated=migrated), tempfile.TemporaryDirectory() as work:
                    root = Path(work)
                    destination = root / 'backups' / 'txn'
                    destination.mkdir(parents=True)
                    host = SimpleNamespace(cfg={'databases':['', str(root/'vault.db')]}, state=root, gate=root/'gate')
                    source = root / 'vault.db.cost-calculator'
                    store = cost_catalog.Store(source)
                    if migrated:
                        store.save_form_draft('a' * 24, 'product', 'example', {'name': ['Черновик']}, 'Ошибка')
                    else:
                        with store.connect() as conn:
                            conn.execute('DROP TABLE cost_form_drafts')
                    with contextlib.closing(sqlite3.connect(source)) as db:
                        self.assertEqual(Host.schema(db), CALCULATOR_DRAFT_SCHEMA if migrated else CALCULATOR_SCHEMA)
                    with patch('cd.secure', return_value=destination):
                        backup_scoped_keys(host, 'txn')
                    snapshot = destination / source.name
                    with contextlib.closing(sqlite3.connect(snapshot)) as db:
                        self.assertEqual(Host.schema(db), CALCULATOR_DRAFT_SCHEMA if migrated else CALCULATOR_SCHEMA)
                        if migrated:
                            self.assertEqual(db.execute('SELECT count(*) FROM cost_form_drafts').fetchone()[0], 1)
                    snapshot.unlink()
                    with store.connect() as conn:
                        conn.execute('CREATE TABLE unexpected_change(id INTEGER)')
                    with patch('cd.secure', return_value=destination):
                        with self.assertRaisesRegex(ReleaseError, 'verification failed'):
                            backup_scoped_keys(host, 'txn')
                    self.assertEqual(list(destination.iterdir()), [])
        finally:
            sys.path.remove(str(service))

    def test_locked_or_invalid_database_publishes_no_snapshot(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            destination = root / 'backups' / 'txn'
            destination.mkdir(parents=True)
            host = SimpleNamespace(cfg={'databases':['',str(root/'vault.db')]}, state=root, gate=root/'gate')
            source = root / 'vault.db.avito-keys'
            with contextlib.closing(sqlite3.connect(source)) as writer:
                writer.execute('CREATE TABLE external_mcp_keys (id INTEGER)')
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
