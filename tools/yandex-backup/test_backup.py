import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import backup


class BackupTests(unittest.TestCase):
    def test_upload_url_is_limited_to_yandex_https(self):
        self.assertEqual(backup.validate_upload_url("https://uploader1d.dst.yandex.net:443/upload-target/test").hostname,
                         "uploader1d.dst.yandex.net")
        for url in ("http://uploader1d.dst.yandex.net/upload", "https://attacker.test/upload",
                    "https://yandex.net.attacker.test/upload", "https://user@uploader.yandex.net/upload",
                    "https://uploader.yandex.net:444/upload", "https://uploader.yandex.net/upload#x"):
            with self.subTest(url=url), self.assertRaises(backup.BackupError):
                backup.validate_upload_url(url)

    def test_destination_must_match_public_folder_identity(self):
        shared = {"type": "dir", "resource_id": "owner:folder"}
        with patch.object(backup, "json_request", return_value=dict(shared)):
            backup.verify_folder("test-token", "disk:/requested", "owner:folder")
        for mismatch in (dict(shared, resource_id="other:folder"), dict(shared, type="file")):
            with self.subTest(mismatch=mismatch), patch.object(backup, "json_request", return_value=mismatch):
                with self.assertRaisesRegex(backup.BackupError, "folder_identity_mismatch"):
                    backup.verify_folder("test-token", "disk:/requested", "owner:folder")

    @unittest.skipUnless(hasattr(os, "statvfs"), "Linux snapshot test")
    def test_live_wal_databases_are_snapshotted_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            work = Path(temp) / "work"
            root.mkdir(); work.mkdir()
            connections = []
            try:
                for name in ("auth.sqlite", "vault.sqlite", "vault.sqlite.cost-calculator"):
                    db = sqlite3.connect(root / name)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE important(value TEXT)")
                    db.execute("INSERT INTO important VALUES (?)", (name,))
                    db.commit()
                    connections.append(db)
                result = dict(backup.snapshots(root, work))
                self.assertEqual(set(result), {"auth.sqlite", "vault.sqlite", "vault.sqlite.cost-calculator"})
                for name, path in result.items():
                    with contextlib.closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute("SELECT value FROM important").fetchone(), (name,))
                        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            finally:
                for db in connections:
                    db.close()

    @unittest.skipUnless(hasattr(os, "statvfs"), "Linux snapshot test")
    def test_snapshot_rejects_symlinked_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"; root.mkdir()
            work = Path(temp) / "work"; work.mkdir()
            for name in ("auth.sqlite", "vault.sqlite"):
                with sqlite3.connect(root / name) as db:
                    db.execute("CREATE TABLE important(value TEXT)")
            (root / "vault.sqlite.cost-calculator").symlink_to(root / "vault.sqlite")
            with self.assertRaisesRegex(backup.BackupError, "unsafe_file_permissions"):
                backup.snapshots(root, work)

    @unittest.skipUnless(hasattr(os, "statvfs"), "Linux snapshot test")
    def test_busy_database_fails_closed_and_releases_other_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"; root.mkdir()
            work = Path(temp) / "work"; work.mkdir()
            for name in ("auth.sqlite", "vault.sqlite"):
                with sqlite3.connect(root / name) as db:
                    db.execute("CREATE TABLE important(value TEXT)")
            blocker = sqlite3.connect(root / "vault.sqlite", isolation_level=None)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(backup.BackupError, "sqlite_snapshot_failed"):
                    backup.snapshots(root, work)
                with sqlite3.connect(root / "auth.sqlite", timeout=0) as other:
                    other.execute("BEGIN IMMEDIATE")
            finally:
                blocker.rollback(); blocker.close()

    def test_uncertain_put_is_reconciled_without_retry(self):
        calls = []
        class Connection:
            def __init__(self, host, port, **kwargs):
                calls.append((host, port))
            def request(self, method, path, body, headers):
                calls.append((method, path, set(headers)))
                self.data = body.read()
                raise TimeoutError()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as temp:
            encrypted = Path(temp) / "backup.age"
            encrypted.write_bytes(b"already encrypted")
            response = {"href": "https://uploader1d.dst.yandex.net/upload-target/opaque", "method": "PUT", "templated": False}
            meta = {"type": "file", "size": encrypted.stat().st_size, "md5": backup.file_digest(encrypted, "md5")}
            with patch.object(backup, "json_request", side_effect=[response, meta]), \
                    patch.object(backup.http.client, "HTTPSConnection", Connection):
                backup.upload("oauth-test-token", "disk:/requested", encrypted)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][0], "PUT")
            self.assertNotIn("Authorization", calls[1][2])

    def test_unconfirmed_upload_preserves_encrypted_file_without_retry(self):
        calls = []
        class Connection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, *args, **kwargs):
                calls.append("PUT")
                raise TimeoutError()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as temp:
            encrypted = Path(temp) / "backup.age"
            encrypted.write_bytes(b"encrypted")
            link = {"href": "https://uploader1d.dst.yandex.net/upload-target/opaque", "method": "PUT", "templated": False}
            with patch.object(backup, "json_request", side_effect=[link] + [backup.BackupError("yandex_http_404")] * 6), \
                    patch.object(backup.http.client, "HTTPSConnection", Connection), patch.object(backup.time, "sleep"):
                with self.assertRaisesRegex(backup.BackupError, "upload_not_confirmed"):
                    backup.upload("oauth-test-token", "disk:/requested", encrypted)
            self.assertEqual(calls, ["PUT"])
            self.assertEqual(encrypted.read_bytes(), b"encrypted")

    @unittest.skipUnless(Path("/usr/bin/age").is_file() and os.geteuid() == 0, "age and root test fixtures required")
    def test_encrypted_archive_decrypts_with_independent_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "identity"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            recipient = root / "recipients.txt"
            recipient.write_bytes(key.with_suffix(".pub").read_bytes())
            creds = root / "credentials"; creds.mkdir()
            for name in ("vault-key", "session"):
                (creds / name).write_bytes(("fixture-" + name).encode())
                (creds / name).chmod(0o600)
            snap = root / "snapshot.sqlite"; snap.write_bytes(b"synthetic snapshot")
            archive = root / "backup.age"
            backup.encrypt_archive(archive, recipient, [("auth.sqlite", snap)], creds, root / "missing-key")
            plaintext = subprocess.check_output(["/usr/bin/age", "-d", "-i", str(key), str(archive)])
            import io
            with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
                self.assertEqual(tar.extractfile("databases/auth.sqlite").read(), b"synthetic snapshot")
                self.assertEqual(tar.extractfile("secrets/vault-key").read(), b"fixture-vault-key")
                manifest = json.load(tar.extractfile("manifest.json"))
                self.assertEqual(manifest["databases"]["databases/auth.sqlite"], backup.file_digest(snap))

    @unittest.skipUnless(Path("/usr/bin/age").is_file() and os.geteuid() == 0, "age and root test fixtures required")
    def test_existing_encrypted_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recipient = root / "recipients.txt"; recipient.write_text("unused")
            existing = root / "backup.age"; existing.write_bytes(b"previous backup")
            with self.assertRaises(FileExistsError):
                backup.encrypt_archive(existing, recipient, [], root, root / "missing")
            self.assertEqual(existing.read_bytes(), b"previous backup")


if __name__ == "__main__":
    unittest.main()
