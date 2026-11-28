"""Transaction/recovery tests on synthetic databases and a simulated portal."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import restore_files

if os.name == "posix":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-release"))
    import restore_controller as controller


class Interrupted(Exception):
    pass


class Guard:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def verify(self, *args): pass


class Host:
    def __init__(self, root):
        self.data, self.state = root / "data", root / "state"
        self.data.mkdir(mode=0o700); self.state.mkdir(mode=0o700)
        (self.state / "backups").mkdir(mode=0o700)
        self.cfg = {"databases": [str(self.data / "auth.sqlite"), str(self.data / "vault.sqlite")]}
        self.running, self.fenced = True, False
        self.catalogue_busy = False
    def current(self): return "a" * 40
    def verify(self, name, sha): pass
    def check_unit(self): pass
    def catalogue_quiescent(self):
        if self.running or self.catalogue_busy:
            raise controller.ReleaseError("catalogue still active")
    def settled(self): pass
    def holder(self): return os.getuid()
    def sockets(self): return 0
    def workers_idle(self, *args): return True
    def process(self, *args): return os.getpid(), "invocation", "/test"
    def reserve(self, value): pass
    def barrier(self, txn, mark, value): self.fenced = value
    def stop(self): self.running = False
    def props(self): return {"ActiveState": "active" if self.running else "inactive", "MainPID": str(os.getpid()) if self.running else "0"}


class Transaction:
    def __init__(self, host):
        self.host = host
        self.pending = host.state / "pending.json"
        self.active = host.state / "active.json"
        self.market_hold = host.state / "hold"
    def receipt(self): return self.host.current(), "b" * 64
    def phase(self, journal, phase):
        journal["phase"] = phase
        self.pending.write_text(json.dumps(journal))
    def drain(self, name, mark, **kwargs): return self.host.process(name)
    def gate_lock(self, **kwargs): return Guard()
    def start(self, journal, name, guard): self.host.running = True
    def ready(self, journal, name, guard): pass
    def complete(self, journal, name, sha):
        self.host.fenced = False
        self.pending.unlink()


def make_db(path, statements):
    with closing(sqlite3.connect(path)) as db, db:
        for sql, args in statements: db.execute(sql, args)
    os.chmod(path, 0o600)


def encrypt(key, name, clear):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, clear, name.encode())


class Fixture:
    def __init__(self, root):
        self.host = Host(root)
        self.txn = Transaction(self.host)
        self.work, archive = root / "work", root / "archive"
        self.work.mkdir(mode=0o700); archive.mkdir(mode=0o700)
        self.old, self.current = os.urandom(32), os.urandom(32)
        vault = [
            ("CREATE TABLE vault(provider TEXT PRIMARY KEY,blob BLOB NOT NULL)", ()),
            ("CREATE TABLE external_mcp_keys(public_id TEXT,revoked_at REAL)", ()),
            ("CREATE TABLE oauth_codes(hash TEXT)", ()), ("CREATE TABLE oauth_tokens(hash TEXT)", ()),
        ]
        make_db(self.host.data / "vault.sqlite", vault + [
            ("INSERT INTO vault VALUES(?,?)", ("yandex_backup", encrypt(self.current, "yandex_backup", b"disk"))),
            ("INSERT INTO vault VALUES(?,?)", ("yandex_backup_metadata", encrypt(self.current, "yandex_backup_metadata", b"disk metadata"))),
            ("INSERT INTO vault VALUES(?,?)", ("ozon", encrypt(self.current, "ozon", b"current"))),
        ])
        make_db(archive / "vault.sqlite", vault + [
            ("INSERT INTO vault VALUES(?,?)", ("ozon", encrypt(self.old, "ozon", b"archived"))),
            ("INSERT INTO external_mcp_keys VALUES('revoked-on-restore', NULL)", ()),
        ])
        make_db(self.host.data / "auth.sqlite", [
            ("CREATE TABLE sessions(hash TEXT)", ()), ("CREATE TABLE tokens(hash TEXT)", ()),
            ("CREATE TABLE attempts(bucket TEXT)", ()), ("INSERT INTO sessions VALUES('session')", ()),
        ])
        (archive / "key").write_bytes(self.old)
        self.files = {"databases/vault.sqlite": archive / "vault.sqlite", "secrets/vault-key": archive / "key"}
    def value(self):
        with closing(sqlite3.connect(self.host.data / "vault.sqlite")) as db:
            blob = db.execute("SELECT blob FROM vault WHERE provider='ozon'").fetchone()[0]
            return AESGCM(self.current).decrypt(blob[:12], blob[12:], b"ozon")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "getuid") and os.getuid() == 0,
                     "isolated Linux root fixture required")
class ControllerTests(unittest.TestCase):
    def test_full_restore_uses_only_synthetic_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, "secure", side_effect=lambda path, *a: Path(path)):
            fixture = Fixture(Path(directory))
            controller.activate(fixture.txn, fixture.files, fixture.work, fixture.current)
            self.assertEqual(fixture.value(), b"archived")
            self.assertTrue(fixture.host.running)
            self.assertFalse(fixture.host.fenced)
            self.assertFalse(fixture.txn.pending.exists())
            self.assertEqual(json.loads(fixture.txn.market_hold.read_text())["purpose"], "restore")
            with closing(sqlite3.connect(fixture.host.data / "vault.sqlite.market-actions")) as db:
                self.assertEqual(db.execute("SELECT paused FROM control").fetchone()[0], 1)
            with closing(sqlite3.connect(fixture.host.data / "auth.sqlite")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_recovery_after_every_file_replacement(self):
        original = restore_files.apply
        for limit in range(1, len(restore_files.FILES) + 1):
            with self.subTest(after=limit), tempfile.TemporaryDirectory() as directory, \
                    patch.object(controller, "secure", side_effect=lambda path, *a: Path(path)):
                fixture = Fixture(Path(directory))
                count = [0]
                def fail_after(_name):
                    count[0] += 1
                    if count[0] == limit: raise Interrupted()
                def interrupt_apply(root, prepared, **kwargs):
                    return original(root, prepared, **kwargs, after_each=fail_after)
                with patch.object(restore_files, "apply", side_effect=interrupt_apply), \
                        patch.object(controller, "recover", side_effect=Interrupted):
                    with self.assertRaises(Interrupted):
                        controller.activate(fixture.txn, fixture.files, fixture.work, fixture.current)
                self.assertTrue(fixture.txn.pending.exists())
                self.assertTrue(fixture.host.fenced)
                controller.recover(fixture.txn, json.loads(fixture.txn.pending.read_text()))
                self.assertEqual(fixture.value(), b"current")
                self.assertTrue(fixture.host.running)
                self.assertFalse(fixture.host.fenced)
                self.assertFalse(fixture.txn.pending.exists())
                self.assertFalse(fixture.txn.market_hold.exists())

    def test_busy_catalogue_refuses_restore_and_can_recover_without_data_change(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, "secure", side_effect=lambda path, *a: Path(path)):
            fixture = Fixture(Path(directory))
            fixture.host.catalogue_busy = True
            with self.assertRaises(controller.ReleaseError):
                controller.activate(fixture.txn, fixture.files, fixture.work, fixture.current)
            self.assertEqual(fixture.value(), b"current")
            self.assertTrue(fixture.txn.pending.exists())
            self.assertTrue(fixture.host.fenced)
            fixture.host.catalogue_busy = False
            controller.recover(fixture.txn, json.loads(fixture.txn.pending.read_text()))
            self.assertEqual(fixture.value(), b"current")
            self.assertTrue(fixture.host.running)
            self.assertFalse(fixture.txn.pending.exists())
            self.assertFalse(fixture.host.fenced)

    def test_bad_archive_schema_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, "secure", side_effect=lambda path, *a: Path(path)):
            fixture = Fixture(Path(directory))
            with closing(sqlite3.connect(fixture.files["databases/vault.sqlite"])) as db:
                db.execute("CREATE TABLE unexpected(data TEXT)")
            with self.assertRaises(ValueError):
                controller.activate(fixture.txn, fixture.files, fixture.work, fixture.current)
            self.assertEqual(fixture.value(), b"current")
            self.assertTrue(fixture.host.running)
            self.assertFalse(fixture.host.fenced)


if __name__ == "__main__":
    unittest.main()
