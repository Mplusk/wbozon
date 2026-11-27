"""Prepare isolated database snapshots for a conservative restoration."""

from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from restore_archive import InvalidBackup


ACTION_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY, key_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
        fingerprint TEXT NOT NULL, contract TEXT NOT NULL, slot TEXT NOT NULL, version TEXT NOT NULL,
        created INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL, body BLOB, result BLOB,
        UNIQUE(key_id,request_id))''',
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, proposal TEXT, at INTEGER NOT NULL, event TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ozon_review_replies (
        review_digest TEXT PRIMARY KEY, proposal TEXT NOT NULL)''',
)


def _connect(path):
    db = sqlite3.connect(path, timeout=3)
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rewrap(cipher_old, cipher_new, blob, aad):
    if not isinstance(blob, bytes) or not 28 <= len(blob) <= 1024 * 1024:
        raise InvalidBackup("invalid_encrypted_record")
    clear = cipher_old.decrypt(blob[:12], blob[12:], aad)
    nonce = os.urandom(12)
    return nonce + cipher_new.encrypt(nonce, clear, aad)


def _preserve_current(provider):
    return (provider in {"yandex_backup", "yandex_backup_metadata", "n8n"} or
            provider.startswith("content_factory_") or provider.startswith("n8n_content_factory_"))


def prepare_vault(archive_path, current_path, old_key, current_key):
    """Rewrap the backup, retaining current Disk and content-factory settings."""
    if len(old_key) != 32 or len(current_key) != 32:
        raise InvalidBackup("invalid_vault_key")
    try:
        with closing(_connect(current_path)) as live:
            rows = live.execute("SELECT provider,blob FROM vault LIMIT 1001").fetchall()
            if len(rows) > 1000:
                raise InvalidBackup("invalid_current_vault")
            current = {provider: blob for provider, blob in rows
                       if isinstance(provider, str) and _preserve_current(provider)}
        if not {"yandex_backup", "yandex_backup_metadata"} <= set(current):
            raise InvalidBackup("current_backup_settings_missing")
        with closing(_connect(archive_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT provider,blob FROM vault LIMIT 1001").fetchall()
            if len(rows) > 1000:
                raise InvalidBackup("invalid_vault")
            old, new = AESGCM(old_key), AESGCM(current_key)
            for provider, blob in rows:
                if not isinstance(provider, str) or not 1 <= len(provider) <= 256:
                    raise InvalidBackup("invalid_vault")
                if _preserve_current(provider):
                    db.execute("DELETE FROM vault WHERE provider=?", (provider,))
                    continue
                encrypted = _rewrap(old, new, blob, provider.encode())
                db.execute("UPDATE vault SET blob=? WHERE provider=?", (encrypted, provider))
            db.executemany("INSERT OR REPLACE INTO vault(provider,blob) VALUES(?,?)", current.items())
            tables = _tables(db)
            if "external_mcp_keys" in tables:
                db.execute("UPDATE external_mcp_keys SET revoked_at=? WHERE revoked_at IS NULL", (time.time(),))
            for name in ("oauth_codes", "oauth_tokens"):
                if name in tables:
                    db.execute("DELETE FROM " + name)
    except (sqlite3.Error, InvalidTag, UnicodeError):
        raise InvalidBackup("vault_key_mismatch") from None


def prepare_auth(current_path, destination):
    """Preserve current rate-limit history but require a fresh owner login."""
    shutil.copyfile(current_path, destination)
    try:
        with closing(_connect(destination)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not {"sessions", "tokens"} <= _tables(db):
                raise InvalidBackup("invalid_auth_database")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM tokens")
    except sqlite3.Error:
        raise InvalidBackup("invalid_auth_database") from None


def revoke_sidecar(path):
    try:
        with closing(_connect(path)) as db, db:
            db.execute("UPDATE external_mcp_keys SET revoked_at=? WHERE revoked_at IS NULL", (time.time(),))
    except sqlite3.Error:
        raise InvalidBackup("invalid_key_database") from None


def prepare_actions(current_path, archived_path, destination, old_key, current_key):
    """Retain the newest action ledger, or rewrap the archived one, then pause it."""
    current = Path(current_path) if current_path else None
    archived = Path(archived_path) if archived_path else None
    source = current if current and current.is_file() else archived
    if source:
        shutil.copyfile(source, destination)
    try:
        with closing(_connect(destination)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for ddl in ACTION_SCHEMA:
                db.execute(ddl)
            db.execute("INSERT OR IGNORE INTO control VALUES(1,1)")
            if source is not None and source == archived and source != current:
                old, new = AESGCM(old_key), AESGCM(current_key)
                for ident, body, result in db.execute("SELECT id,body,result FROM proposals").fetchall():
                    if not isinstance(ident, str) or len(ident) > 128:
                        raise InvalidBackup("invalid_action_database")
                    values = []
                    for kind, blob in (("input", body), ("result", result)):
                        values.append(None if blob is None else _rewrap(old, new, blob, (ident + ":" + kind).encode()))
                    db.execute("UPDATE proposals SET body=?,result=? WHERE id=?", (*values, ident))
            db.execute("UPDATE control SET paused=1 WHERE id=1")
            db.execute("UPDATE proposals SET status='expired',body=NULL WHERE status='pending'")
            db.execute("INSERT INTO events(proposal,at,event) VALUES(NULL,?,?)", (int(time.time()), "paused"))
    except (sqlite3.Error, InvalidTag, ValueError, UnicodeError):
        raise InvalidBackup("invalid_action_database") from None
