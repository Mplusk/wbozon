from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import restore_files
import restore_prepare
from restore_archive import InvalidBackup


def db(path, statements):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        for sql, args in statements:
            conn.execute(sql, args)
    if os.name != "nt": os.chmod(path, 0o600)


def blob(key, provider, value):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, value, provider.encode())


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data, self.before, self.archive, self.work = (root / name for name in ("data", "before", "archive", "work"))
        for p in (self.data, self.before, self.archive, self.work): p.mkdir(mode=0o700)
        self.old, self.new = os.urandom(32), os.urandom(32)
        vault_schema = [
            ("CREATE TABLE vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)", ()),
            ("CREATE TABLE external_mcp_keys(public_id TEXT, revoked_at REAL)", ()),
            ("CREATE TABLE oauth_codes(hash TEXT)", ()),
            ("CREATE TABLE oauth_tokens(hash TEXT)", ()),
        ]
        db(self.data / "vault.sqlite", vault_schema + [
            ("INSERT INTO vault VALUES(?,?)", ("yandex_backup", blob(self.new, "yandex_backup", b"current"))),
            ("INSERT INTO vault VALUES(?,?)", ("yandex_backup_metadata", blob(self.new, "yandex_backup_metadata", b"current-meta"))),
        ])
        db(self.archive / "vault.sqlite", vault_schema + [
            ("INSERT INTO vault VALUES(?,?)", ("ozon", blob(self.old, "ozon", b"old-credentials"))),
            ("INSERT INTO external_mcp_keys VALUES('old-grant',NULL)", ()),
            ("INSERT INTO oauth_tokens VALUES('old-token')", ()),
        ])
        db(self.data / "auth.sqlite", [
            ("CREATE TABLE sessions(hash TEXT)", ()), ("CREATE TABLE tokens(hash TEXT)", ()),
            ("CREATE TABLE attempts(bucket TEXT)", ()), ("INSERT INTO attempts VALUES('keep')", ()),
            ("INSERT INTO sessions VALUES('remove')", ()),
        ])
        self.key = self.archive / "key"
        self.key.write_bytes(self.old)
        self.files = {"databases/vault.sqlite": self.archive / "vault.sqlite", "secrets/vault-key": self.key}
        self.records = restore_files.snapshot(self.data, self.before)

    def test_build_restores_vault_without_reactivating_old_tokens(self):
        prepared = restore_prepare.build(self.files, self.before, self.records, self.new, self.work)
        self.assertEqual(set(prepared), set(restore_files.FILES))
        with closing(sqlite3.connect(prepared["vault.sqlite"])) as conn:
            values = dict(conn.execute("SELECT provider,blob FROM vault"))
            self.assertEqual(AESGCM(self.new).decrypt(values["ozon"][:12], values["ozon"][12:], b"ozon"), b"old-credentials")
            self.assertIsNotNone(conn.execute("SELECT revoked_at FROM external_mcp_keys").fetchone()[0])
            self.assertEqual(conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0], 0)
        with closing(sqlite3.connect(prepared["auth.sqlite"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)
        with closing(sqlite3.connect(prepared["vault.sqlite.market-actions"])) as conn:
            self.assertEqual(conn.execute("SELECT paused FROM control").fetchone()[0], 1)

    def test_schema_change_is_rejected_before_any_live_write(self):
        with closing(sqlite3.connect(self.archive / "vault.sqlite")) as conn:
            conn.execute("CREATE TABLE unexpected(data TEXT)")
        with self.assertRaisesRegex(InvalidBackup, "unsupported_database_schema"):
            restore_prepare.build(self.files, self.before, self.records, self.new, self.work)
        with closing(sqlite3.connect(self.data / "auth.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
