import os
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import restore_decrypt
import restore_archive
import backup


@unittest.skipUnless(hasattr(os, "memfd_create") and shutil.which("age") and shutil.which("age-keygen"),
                     "Linux and age are required")
class AgeRestoreTests(unittest.TestCase):
    def test_age_identity_roundtrip_and_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identities = [root / "identity", root / "other"]
            for identity in identities:
                subprocess.run(["age-keygen", "-o", str(identity)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            recipient = subprocess.check_output(["age-keygen", "-y", str(identities[0])], text=True).strip()
            encrypted, output = root / "data.age", root / "decrypted"
            with encrypted.open("wb") as target:
                subprocess.run(["age", "-r", recipient], input=b"synthetic test only", stdout=target, check=True)
            restore_decrypt.decrypt(encrypted, identities[0].read_bytes(), output)
            self.assertEqual(output.read_bytes(), b"synthetic test only")
            output.unlink()
            with self.assertRaisesRegex(restore_decrypt.DecryptionError, "decryption_failed"):
                restore_decrypt.decrypt(encrypted, identities[1].read_bytes(), output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("ssh-keygen"), "SSH keygen is required")
    def test_ssh_identity_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity, encrypted, output = root / "ssh-key", root / "data.age", root / "decrypted"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with encrypted.open("wb") as target:
                subprocess.run(["age", "-R", str(identity) + ".pub"], input=b"synthetic SSH test",
                               stdout=target, check=True)
            restore_decrypt.decrypt(encrypted, identity.read_bytes(), output)
            self.assertEqual(output.read_bytes(), b"synthetic SSH test")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "backup secret permission contract requires root")
    def test_actual_backup_format_roundtrip(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity, recipient = root / "identity", root / "recipient"
            subprocess.run(["age-keygen", "-o", str(identity)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            recipient.write_text(subprocess.check_output(["age-keygen", "-y", str(identity)], text=True))
            credentials = root / "credentials"
            credentials.mkdir(mode=0o700)
            (credentials / "vault-key").write_bytes(b"v" * 32)
            (credentials / "session").write_text("s" * 48)
            for path in credentials.iterdir(): os.chmod(path, 0o600)
            auth, vault, history = root / "auth.db", root / "vault.db", root / "history.db"
            with closing(sqlite3.connect(auth)) as db, db:
                db.execute("CREATE TABLE sessions(hash TEXT)")
            with closing(sqlite3.connect(vault)) as db, db:
                db.execute("CREATE TABLE vault(provider TEXT PRIMARY KEY,blob BLOB)")
                nonce = b"n" * 12
                db.execute("INSERT INTO vault VALUES(?,?)", ("ozon", nonce + AESGCM(b"v" * 32).encrypt(nonce, b"fixture", b"ozon")))
            history_key = root / "history.key"
            history_key.write_bytes(b"h" * 32)
            fields = ("scope", "orders", "2026-11-01", "2026-11-02T00:00:00+00:00")
            nonce = b"q" * 12
            encrypted_row = nonce + AESGCM(b"h" * 32).encrypt(nonce, b"fixture", json.dumps(list(fields)).encode())
            with closing(sqlite3.connect(history)) as db, db:
                db.execute("CREATE TABLE days(scope TEXT,dataset TEXT,day TEXT,observed TEXT,blob BLOB)")
                db.execute("INSERT INTO days VALUES(?,?,?,?,?)", (*fields, encrypted_row))
            encrypted, output, staged = root / "backup.age", root / "backup.tar.gz", root / "staged"
            backup.encrypt_archive(encrypted, recipient, [("auth.sqlite", auth), ("vault.sqlite", vault),
                (".market-reports/history-v2.db", history)], credentials, history_key)
            restore_decrypt.decrypt(encrypted, identity.read_bytes(), output)
            staged.mkdir(mode=0o700)
            with output.open("rb") as source:
                result = restore_archive.unpack_verified(source, staged)
            restore_archive.verify_vault_key(result["files"])
            restore_archive.verify_history_key(result["files"])


if __name__ == "__main__":
    unittest.main()
