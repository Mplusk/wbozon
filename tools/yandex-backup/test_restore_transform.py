from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import restore_transform as transform


OLD = b"o" * 32
CURRENT = b"c" * 32


def encrypted(key, aad, data):
    nonce = b"n" * 12
    return nonce + AESGCM(key).encrypt(nonce, data, aad.encode())


def decrypted(key, aad, data):
    return AESGCM(key).decrypt(data[:12], data[12:], aad.encode())


class RestoreTransformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def vault(self, name, key, providers):
        path = self.root / name
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)")
            db.execute("CREATE TABLE external_mcp_keys(public_id TEXT, revoked_at REAL)")
            db.execute("INSERT INTO external_mcp_keys VALUES('key',NULL)")
            db.execute("CREATE TABLE oauth_tokens(value TEXT)")
            db.execute("INSERT INTO oauth_tokens VALUES('token')")
            db.execute("CREATE TABLE oauth_codes(value TEXT)")
            db.execute("INSERT INTO oauth_codes VALUES('code')")
            db.executemany("INSERT INTO vault VALUES(?,?)", [(p, encrypted(key, p, value)) for p, value in providers.items()])
        return path

    def test_rewraps_vault_preserves_current_backup_settings_and_revokes_grants(self):
        archived = self.vault("archive.db", OLD, {"ozon": b"archive-credential", "yandex_backup": b"old-settings"})
        current = self.vault("current.db", CURRENT, {"yandex_backup": b"current-settings", "yandex_backup_metadata": b"current-metadata"})
        transform.prepare_vault(archived, current, OLD, CURRENT)
        with closing(sqlite3.connect(archived)) as db:
            rows = dict(db.execute("SELECT provider,blob FROM vault"))
            self.assertEqual(decrypted(CURRENT, "ozon", rows["ozon"]), b"archive-credential")
            self.assertEqual(decrypted(CURRENT, "yandex_backup", rows["yandex_backup"]), b"current-settings")
            self.assertEqual(decrypted(CURRENT, "yandex_backup_metadata", rows["yandex_backup_metadata"]), b"current-metadata")
            self.assertIsNotNone(db.execute("SELECT revoked_at FROM external_mcp_keys").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0], 0)

    def test_wrong_key_does_not_commit_changes(self):
        archived = self.vault("archive.db", OLD, {"ozon": b"archive-credential"})
        current = self.vault("current.db", CURRENT, {"yandex_backup": b"current", "yandex_backup_metadata": b"meta"})
        with self.assertRaisesRegex(transform.InvalidBackup, "vault_key_mismatch"):
            transform.prepare_vault(archived, current, b"x" * 32, CURRENT)
        with closing(sqlite3.connect(archived)) as db:
            self.assertEqual(decrypted(OLD, "ozon", db.execute("SELECT blob FROM vault").fetchone()[0]), b"archive-credential")

    def test_factory_settings_never_revert_or_reactivate_from_archive(self):
        archived = self.vault("archive.db", OLD, {
            "ozon": b"archive-credential", "n8n": b"old-n8n",
            "content_factory_budget_gateway_setup": b"old-gateway",
            "n8n_content_factory_write": b"old-revoked-write-key",
        })
        current = self.vault("current.db", CURRENT, {
            "yandex_backup": b"current", "yandex_backup_metadata": b"meta",
            "n8n": b"current-n8n", "content_factory_budget_gateway_setup": b"current-gateway",
        })
        transform.prepare_vault(archived, current, OLD, CURRENT)
        with closing(sqlite3.connect(archived)) as db:
            rows = dict(db.execute("SELECT provider,blob FROM vault"))
        self.assertEqual(decrypted(CURRENT, "ozon", rows["ozon"]), b"archive-credential")
        self.assertEqual(decrypted(CURRENT, "n8n", rows["n8n"]), b"current-n8n")
        self.assertEqual(decrypted(CURRENT, "content_factory_budget_gateway_setup", rows["content_factory_budget_gateway_setup"]), b"current-gateway")
        self.assertNotIn("n8n_content_factory_write", rows)

    def test_auth_clears_sessions_but_preserves_rate_limit_history(self):
        current, prepared = self.root / "auth.db", self.root / "new-auth.db"
        with closing(sqlite3.connect(current)) as db, db:
            for table in ("sessions", "tokens", "attempts"):
                db.execute("CREATE TABLE " + table + " (value TEXT)")
                db.execute("INSERT INTO " + table + " VALUES ('fixture')")
        transform.prepare_auth(current, prepared)
        with closing(sqlite3.connect(prepared)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        with closing(sqlite3.connect(current)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def action_db(self, name, key):
        path = self.root / name
        with closing(sqlite3.connect(path)) as db, db:
            for ddl in transform.ACTION_SCHEMA:
                db.execute(ddl)
            db.execute("INSERT INTO control VALUES(1,0)")
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("id", "key", "request", "op", "finger", "contract", "slot", "version", 1, 2,
                        "unknown", encrypted(key, "id:input", b"input"), encrypted(key, "id:result", b"result")))
        return path

    def test_current_action_ledger_is_preserved_and_paused(self):
        live = self.action_db("live-actions.db", CURRENT)
        archived = self.action_db("old-actions.db", OLD)
        prepared = self.root / "prepared-actions.db"
        transform.prepare_actions(live, archived, prepared, OLD, CURRENT)
        with closing(sqlite3.connect(prepared)) as db:
            self.assertEqual(db.execute("SELECT paused FROM control").fetchone()[0], 1)
            status, body = db.execute("SELECT status,body FROM proposals").fetchone()
            self.assertEqual(status, "unknown")
            self.assertEqual(decrypted(CURRENT, "id:input", body), b"input")
            self.assertEqual(db.execute("SELECT event FROM events").fetchone()[0], "paused")

    def test_old_action_ledger_is_rewrapped_if_no_current_ledger(self):
        archived = self.action_db("old-actions.db", OLD)
        prepared = self.root / "prepared-actions.db"
        transform.prepare_actions(None, archived, prepared, OLD, CURRENT)
        with closing(sqlite3.connect(prepared)) as db:
            body, result = db.execute("SELECT body,result FROM proposals").fetchone()
            self.assertEqual(decrypted(CURRENT, "id:input", body), b"input")
            self.assertEqual(decrypted(CURRENT, "id:result", result), b"result")
            self.assertEqual(db.execute("SELECT paused FROM control").fetchone()[0], 1)

    def test_missing_action_ledger_still_starts_paused(self):
        prepared = self.root / "prepared-actions.db"
        transform.prepare_actions(None, None, prepared, OLD, CURRENT)
        with closing(sqlite3.connect(prepared)) as db:
            self.assertEqual(db.execute("SELECT paused FROM control").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
