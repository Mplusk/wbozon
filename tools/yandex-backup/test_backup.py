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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


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
            (root / '.market-reports').mkdir()
            connections = []
            try:
                for name in ("auth.sqlite", "vault.sqlite", "vault.sqlite.cost-calculator", ".market-reports/reports.db"):
                    db = sqlite3.connect(root / name)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE important(value TEXT)")
                    db.execute("INSERT INTO important VALUES (?)", (name,))
                    db.commit()
                    connections.append(db)
                result = dict(backup.snapshots(root, work))
                self.assertEqual(set(result), {"auth.sqlite", "vault.sqlite", "vault.sqlite.cost-calculator", ".market-reports/reports.db"})
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

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "root vault fixture required")
    def test_unreadable_settings_records_a_failed_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = root / 'data'; data.mkdir()
            (data / '.yandex-backup.lock').touch(mode=0o600)
            config = {'recipient_file': str(root/'recipients'), 'data_root': str(data),
                      'credentials': str(root/'credentials'), 'state_dir': str(root/'state'),
                      'runtime_dir': str(root/'runtime'), 'deploy_lock': str(root/'deploy.lock')}
            config_file = root/'config.json'; config_file.write_text(json.dumps(config)); config_file.chmod(0o600)
            with patch.object(backup, 'load_settings', side_effect=backup.BackupError('backup_settings_unavailable')):
                with self.assertRaisesRegex(backup.BackupError, 'backup_settings_unavailable'):
                    backup.main(str(config_file))
            status = json.loads((root/'state'/'status.json').read_text())
            self.assertEqual(status['state'], 'failed')
            self.assertEqual(status['reason'], 'backup_settings_unavailable')

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "root vault fixture required")
    def test_disabled_vault_setting_never_calls_yandex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = root / 'data'; data.mkdir()
            credentials = root / 'credentials'; credentials.mkdir()
            key = os.urandom(32); (credentials / 'vault-key').write_bytes(key)
            (credentials / 'vault-key').chmod(0o600)
            settings = {'token': 'synthetic-token-123456789', 'public_url': 'https://disk.yandex.ru/d/example',
                        'disk_folder': 'disk:/Backups', 'enabled': False}
            nonce = os.urandom(12)
            blob = nonce + AESGCM(key).encrypt(nonce, json.dumps(settings).encode(), b'yandex_backup')
            with sqlite3.connect(data / 'vault.sqlite') as db:
                db.execute('CREATE TABLE vault(provider TEXT PRIMARY KEY,blob BLOB NOT NULL)')
                db.execute('INSERT INTO vault VALUES (?,?)', ('yandex_backup', blob))
            lock = data / '.yandex-backup.lock'; lock.touch(mode=0o600)
            config = {'recipient_file': str(root/'recipients'), 'data_root': str(data), 'credentials': str(credentials),
                      'state_dir': str(root/'state'), 'runtime_dir': str(root/'runtime'), 'deploy_lock': str(root/'deploy.lock')}
            config_file = root / 'config.json'; config_file.write_text(json.dumps(config)); config_file.chmod(0o600)
            with patch.object(backup, 'json_request', side_effect=AssertionError('network must not be used')):
                backup.main(str(config_file))
            self.assertFalse((root/'deploy.lock').exists())

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "root vault fixture required")
    def test_encrypted_settings_use_vault_key_and_associated_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = root/'data'; data.mkdir(); credentials = root/'credentials'; credentials.mkdir()
            key = os.urandom(32); (credentials/'vault-key').write_bytes(key); (credentials/'vault-key').chmod(0o600)
            settings = {'token': 'synthetic-token-123456789', 'public_url': 'https://disk.yandex.ru/d/example',
                        'disk_folder': 'disk:/Backups', 'enabled': True}
            nonce = os.urandom(12); blob = nonce + AESGCM(key).encrypt(nonce, json.dumps(settings).encode(), b'yandex_backup')
            with sqlite3.connect(data/'vault.sqlite') as db:
                db.execute('CREATE TABLE vault(provider TEXT PRIMARY KEY,blob BLOB NOT NULL)')
                db.execute('INSERT INTO vault VALUES (?,?)', ('yandex_backup', blob))
            self.assertEqual(backup.load_settings(data, credentials), settings)
            (credentials/'vault-key').write_bytes(os.urandom(32))
            with self.assertRaisesRegex(backup.BackupError, 'backup_settings_unavailable'):
                backup.load_settings(data, credentials)

    @unittest.skipUnless(Path("/usr/bin/age").is_file() and os.geteuid() == 0, "age and root test fixtures required")
    def test_enabled_job_encrypts_and_confirms_then_disabled_job_does_not_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = root/'data'; data.mkdir(); credentials = root/'credentials'; credentials.mkdir()
            key = os.urandom(32)
            for name, content in [('vault-key', key), ('session', b'synthetic-session')]:
                (credentials/name).write_bytes(content); (credentials/name).chmod(0o600)
            settings = {'token': 'synthetic-token-123456789', 'public_url': 'https://disk.yandex.ru/d/example',
                        'disk_folder': 'disk:/Backups', 'enabled': True}
            with sqlite3.connect(data/'auth.sqlite') as db:
                db.execute('CREATE TABLE sessions(value TEXT)')
                db.execute('INSERT INTO sessions VALUES (?)', ('test-record',))
            def store_settings():
                nonce = os.urandom(12)
                blob = nonce + AESGCM(key).encrypt(nonce, json.dumps(settings).encode(), b'yandex_backup')
                with sqlite3.connect(data/'vault.sqlite') as db:
                    db.execute('CREATE TABLE IF NOT EXISTS vault(provider TEXT PRIMARY KEY,blob BLOB NOT NULL)')
                    db.execute('INSERT OR REPLACE INTO vault VALUES (?,?)', ('yandex_backup', blob))
            store_settings()
            (data/'.yandex-backup.lock').touch(mode=0o600)
            identity = root/'identity'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True)
            recipient = root/'recipients'; recipient.write_bytes(identity.with_suffix('.pub').read_bytes())
            config = {'recipient_file': str(recipient), 'data_root': str(data), 'credentials': str(credentials),
                      'state_dir': str(root/'state'), 'runtime_dir': str(root/'runtime'), 'deploy_lock': str(root/'deploy.lock')}
            config_file = root/'config.json'; config_file.write_text(json.dumps(config)); config_file.chmod(0o600)
            uploads = []
            def confirm_upload(token, folder, encrypted):
                self.assertEqual(folder, 'disk:/Backups')
                self.assertTrue(encrypted.read_bytes().startswith(b'age-encryption.org/v1'))
                plaintext = subprocess.check_output(['/usr/bin/age', '-d', '-i', str(identity), str(encrypted)])
                import io
                with tarfile.open(fileobj=io.BytesIO(plaintext), mode='r:gz') as archive:
                    self.assertIn('databases/auth.sqlite', archive.getnames())
                    self.assertIn('databases/vault.sqlite', archive.getnames())
                    self.assertEqual(archive.extractfile('secrets/vault-key').read(), key)
                uploads.append('confirmed')
            with patch.object(backup, 'resolve_folder_identity', return_value='owner:resource'), \
                    patch.object(backup, 'verify_folder'), patch.object(backup, 'upload', side_effect=confirm_upload):
                backup.main(str(config_file))
                status = json.loads((root/'state'/'status.json').read_text())
                self.assertEqual(status['state'], 'uploaded_and_verified')
                self.assertIn('last_success', status)
                self.assertEqual(list((root/'state').glob('*.age')), [])
                settings['enabled'] = False; store_settings()
                backup.main(str(config_file))
            self.assertEqual(uploads, ['confirmed'])

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
