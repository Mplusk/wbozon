import hashlib
import io
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

import restore_archive as restore
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def db_bytes(path, name):
    db_path = Path(path) / name
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO sample(value) VALUES ('fixture')")
    return db_path.read_bytes()


def sample(path):
    databases = {"databases/auth.sqlite": db_bytes(path, "auth.db"),
                 "databases/vault.sqlite": db_bytes(path, "vault.db")}
    manifest = {"version": 1, "created_utc": "2026-11-27T01:30:00+00:00",
                "databases": {name: hashlib.sha256(data).hexdigest() for name, data in databases.items()}}
    return {**databases, "secrets/vault-key": b"v" * 32, "secrets/session": b"s" * 48,
            "manifest.json": json.dumps(manifest).encode()}


def archive(files, *, extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra:
            info, data = extra
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    output.seek(0)
    return output


class RestoreArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = sample(self.root)
        self.stage = self.root / "stage"
        self.stage.mkdir(mode=0o700)

    def check_invalid(self, expected, content):
        with self.assertRaisesRegex(restore.InvalidBackup, expected):
            restore.unpack_verified(content, self.stage)

    def test_valid_archive_checks_hashes_and_database_integrity(self):
        result = restore.unpack_verified(archive(self.files), self.stage)
        self.assertEqual(result["created_utc"], "2026-11-27T01:30:00+00:00")
        self.assertEqual(result["files"]["databases/auth.sqlite"].read_bytes(), self.files["databases/auth.sqlite"])
        self.assertEqual(set(result["digests"]), {"databases/auth.sqlite", "databases/vault.sqlite"})

    def test_rejects_changed_database_and_missing_key(self):
        modified = dict(self.files, **{"databases/auth.sqlite": b"not-the-snapshot"})
        self.check_invalid("database_digest_mismatch", archive(modified))

    def test_rejects_archive_path_traversal(self):
        info = tarfile.TarInfo("../../outside")
        info.size = 4
        self.check_invalid("invalid_member", archive(self.files, extra=(info, b"evil")))
        self.assertFalse((self.root.parent / "outside").exists())

    def test_rejects_duplicate_or_link_member(self):
        info = tarfile.TarInfo("databases/auth.sqlite")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        self.check_invalid("invalid_member", archive(self.files, extra=(info, None)))

    def test_rejects_oversized_tar_extension_before_parsing(self):
        info = tarfile.TarInfo("pax-header")
        info.type = tarfile.XHDTYPE
        info.size = 65537
        self.check_invalid("invalid_archive_metadata", archive(self.files, extra=(info, b"x" * info.size)))

    def test_rejects_valid_hash_for_non_sqlite_content(self):
        changed = dict(self.files, **{"databases/auth.sqlite": b"not sqlite"})
        manifest = json.loads(changed["manifest.json"])
        manifest["databases"]["databases/auth.sqlite"] = hashlib.sha256(changed["databases/auth.sqlite"]).hexdigest()
        changed["manifest.json"] = json.dumps(manifest).encode()
        self.check_invalid("invalid_database", archive(changed))

    def test_requires_supported_schema_when_policy_provided(self):
        with self.assertRaisesRegex(restore.InvalidBackup, "unsupported_database_schema"):
            restore.unpack_verified(archive(self.files), self.stage,
                                    schema_policy={"databases/auth.sqlite": {"0" * 64}})

    def test_rejects_missing_required_secret_and_bad_session(self):
        changed = dict(self.files)
        changed.pop("secrets/vault-key")
        self.check_invalid("missing_required_member", archive(changed))

    def test_rejects_nonempty_staging_directory_before_writing(self):
        (self.stage / "important").write_text("unchanged")
        self.check_invalid("unsafe_staging_directory", archive(self.files))
        self.assertEqual((self.stage / "important").read_text(), "unchanged")

    def test_vault_key_pair_is_checked_without_exposing_credentials(self):
        vault = self.root / "encrypted.db"
        nonce = b"n" * 12
        with closing(sqlite3.connect(vault)) as db, db:
            db.execute("CREATE TABLE vault(provider TEXT PRIMARY KEY, blob BLOB NOT NULL)")
            db.execute("INSERT INTO vault VALUES(?, ?)", ("example", nonce + AESGCM(b"v" * 32).encrypt(nonce, b"secret", b"example")))
        files = dict(self.files, **{"databases/vault.sqlite": vault.read_bytes()})
        manifest = json.loads(files["manifest.json"])
        manifest["databases"]["databases/vault.sqlite"] = hashlib.sha256(files["databases/vault.sqlite"]).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode()
        result = restore.unpack_verified(archive(files), self.stage)
        restore.verify_vault_key(result["files"])
        result["files"]["secrets/vault-key"].write_bytes(b"x" * 32)
        with self.assertRaisesRegex(restore.InvalidBackup, "vault_key_mismatch"):
            restore.verify_vault_key(result["files"])

    def test_history_key_authenticates_archived_rows(self):
        history, key = self.root / "history.db", self.root / "history.key"
        key.write_bytes(b"h" * 32)
        fields = ("scope", "orders", "2026-11-01", "2026-11-02T00:00:00+00:00")
        nonce = b"n" * 12
        blob = nonce + AESGCM(key.read_bytes()).encrypt(nonce, b"synthetic", json.dumps(list(fields)).encode())
        with closing(sqlite3.connect(history)) as db, db:
            db.execute("CREATE TABLE days(scope TEXT,dataset TEXT,day TEXT,observed TEXT,blob BLOB)")
            db.execute("INSERT INTO days VALUES(?,?,?,?,?)", (*fields, blob))
        files = {"databases/market-reports/history-v2.db": history, "secrets/history-v2.key": key}
        restore.verify_history_key(files)
        key.write_bytes(b"x" * 32)
        with self.assertRaisesRegex(restore.InvalidBackup, "history_key_mismatch"):
            restore.verify_history_key(files)


if __name__ == "__main__":
    unittest.main()
