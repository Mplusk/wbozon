"""Recovery tests use only synthetic SQLite files in temporary directories."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import restore_files as restore


def database(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt": os.chmod(path.parent, 0o700)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS sample(value TEXT)")
        db.execute("DELETE FROM sample")
        db.execute("INSERT INTO sample VALUES(?)", (value,))
    if os.name != "nt":
        os.chmod(path, 0o600)


def value(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT value FROM sample").fetchone()[0]


class Interrupted(Exception):
    pass


class FileRestorationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.before = Path(self.tmp.name) / "before"
        self.incoming = Path(self.tmp.name) / "incoming"
        for folder in (self.root, self.before, self.incoming):
            folder.mkdir(mode=0o700)
        for index, name in enumerate(restore.FILES):
            if name.endswith(".key"):
                key = self.root / name
                key.parent.mkdir(parents=True, exist_ok=True)
                key.write_bytes(b"old-key")
                if os.name != "nt": os.chmod(key, 0o600)
            else:
                database(self.root / name, "before-" + str(index))
                database(self.incoming / str(index), "after-" + str(index))
        self.records = restore.snapshot(self.root, self.before)
        self.prepared = {name: self.incoming / str(index) for index, name in enumerate(restore.FILES)
                         if name in restore.DATABASES}

    def assert_original(self):
        for index, name in enumerate(restore.FILES):
            path = self.root / name
            if name.endswith(".key"):
                self.assertEqual(path.read_bytes(), b"old-key")
            else:
                self.assertEqual(value(path), "before-" + str(index))

    def test_rollback_at_every_interrupted_replacement(self):
        for limit in range(1, len(self.prepared) + 1):
            count = [0]
            def interrupt(_name):
                count[0] += 1
                if count[0] == limit: raise Interrupted()
            with self.assertRaises(Interrupted):
                restore.apply(self.root, self.prepared, after_each=interrupt)
            restore.rollback(self.root, self.before, self.records)
            self.assert_original()

    def test_rollback_is_itself_restartable(self):
        restore.apply(self.root, self.prepared)
        for limit in range(1, len(restore.FILES) + 1):
            count = [0]
            def interrupt(_name):
                count[0] += 1
                if count[0] == limit: raise Interrupted()
            with self.assertRaises(Interrupted):
                restore.rollback(self.root, self.before, self.records, after_each=interrupt)
        restore.rollback(self.root, self.before, self.records)
        self.assert_original()

    def test_tampered_snapshot_is_rejected_before_any_replacement(self):
        (self.before / "3.snapshot").write_bytes(b"broken")
        with self.assertRaisesRegex(restore.RestoreFilesError, "snapshot_digest_mismatch"):
            restore.rollback(self.root, self.before, self.records)
        self.assert_original()

    def test_absent_file_is_removed_by_rollback(self):
        root = Path(self.tmp.name) / "empty"
        before = Path(self.tmp.name) / "empty-before"
        root.mkdir(mode=0o700)
        before.mkdir(mode=0o700)
        records = restore.snapshot(root, before)
        restore.apply(root, {"vault.sqlite": self.incoming / "1"})
        self.assertTrue((root / "vault.sqlite").exists())
        restore.rollback(root, before, records)
        self.assertFalse((root / "vault.sqlite").exists())

    def test_unknown_path_cannot_be_restored(self):
        with self.assertRaises(restore.RestoreFilesError):
            restore.apply(self.root, {"../outside": self.incoming / "0"})


if __name__ == "__main__":
    unittest.main()
