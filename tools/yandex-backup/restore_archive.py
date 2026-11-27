"""Validate and unpack a decrypted Yandex backup into an isolated directory.

This module never opens or replaces application databases. Only the exact
version-one backup members are accepted; tar extraction is deliberately not
delegated to tarfile.extract().
"""

import contextlib
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tarfile
import time


DATABASES = {
    "databases/auth.sqlite": "auth.sqlite",
    "databases/vault.sqlite": "vault.sqlite",
    "databases/vault.sqlite.avito-keys": "vault.sqlite.avito-keys",
    "databases/vault.sqlite.catalog-keys": "vault.sqlite.catalog-keys",
    "databases/vault.sqlite.market-actions": "vault.sqlite.market-actions",
    "databases/vault.sqlite.cost-calculator": "vault.sqlite.cost-calculator",
    "databases/market-reports/reports.db": ".market-reports/reports.db",
    "databases/market-reports/history-v2.db": ".market-reports/history-v2.db",
}
SECRETS = {"secrets/vault-key", "secrets/session", "secrets/history-v2.key"}
REQUIRED = {"databases/auth.sqlite", "databases/vault.sqlite", "secrets/vault-key", "secrets/session", "manifest.json"}
ALLOWED = set(DATABASES) | SECRETS | {"manifest.json"}
MAX_DATABASE = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_SECRET = 8192
MAX_TAR_OVERHEAD = 16 * 1024 * 1024


class InvalidBackup(ValueError):
    """A safe, non-secret diagnostic for an invalid archive."""


class _BoundedTarStream:
    def __init__(self, stream):
        self.stream, self.total = stream, 0

    def read(self, size):
        if size < 0 or size > MAX_TOTAL + MAX_TAR_OVERHEAD - self.total:
            raise InvalidBackup("archive_too_large")
        data = self.stream.read(size)
        self.total += len(data)
        return data


class _SafeTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size > 65536:
            raise InvalidBackup("invalid_archive_metadata")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        raise InvalidBackup("invalid_archive_metadata")

    def _proc_sparse(self, archive):
        raise InvalidBackup("invalid_archive_metadata")


def _destination(directory, name):
    path = directory.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _copy_member(archive, member, destination):
    source = archive.extractfile(member)
    if source is None:
        raise InvalidBackup("invalid_member")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    remaining = member.size
    with source, os.fdopen(os.open(destination, flags, 0o600), "wb") as target:
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise InvalidBackup("truncated_member")
            target.write(block)
            digest.update(block)
            remaining -= len(block)
        target.flush()
        os.fsync(target.fileno())
    return digest.hexdigest()


def _check_database(path, expected_schema=None):
    try:
        uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 30
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise InvalidBackup("invalid_database")
            rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            fingerprint = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
            if expected_schema is not None and fingerprint not in expected_schema:
                raise InvalidBackup("unsupported_database_schema")
            return fingerprint
    except sqlite3.Error:
        raise InvalidBackup("invalid_database") from None


def unpack_verified(fileobj, directory, *, schema_policy=None):
    """Read a gzip tar into an empty private directory, returning verified paths.

    schema_policy, when provided, maps every present database archive name to
    its allowed schema fingerprints. Unknown/missing policy entries fail closed.
    """
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink() or any(directory.iterdir()):
        raise InvalidBackup("unsafe_staging_directory")
    if os.name != "nt" and info.st_mode & 0o077:
        raise InvalidBackup("unsafe_staging_directory")
    seen, hashes, paths, total = set(), {}, {}, 0
    try:
        with gzip.GzipFile(fileobj=fileobj, mode="rb") as compressed, \
                tarfile.open(fileobj=_BoundedTarStream(compressed), mode="r|", tarinfo=_SafeTarInfo) as archive:
            for member in archive:
                if member.name not in ALLOWED or member.name in seen or not member.isfile() or member.issparse():
                    raise InvalidBackup("invalid_member")
                seen.add(member.name)
                limit = MAX_DATABASE if member.name in DATABASES else MAX_SECRET
                if member.size < 0 or member.size > limit:
                    raise InvalidBackup("member_too_large")
                total += member.size
                if total > MAX_TOTAL:
                    raise InvalidBackup("archive_too_large")
                if shutil.disk_usage(directory).free < member.size + 32 * 1024 * 1024:
                    raise InvalidBackup("insufficient_temporary_space")
                target = _destination(directory, member.name)
                hashes[member.name] = _copy_member(archive, member, target)
                paths[member.name] = target
    except (tarfile.TarError, EOFError, OSError, RecursionError):
        raise InvalidBackup("invalid_archive") from None
    if not REQUIRED <= seen:
        raise InvalidBackup("missing_required_member")
    if "databases/market-reports/history-v2.db" in seen and "secrets/history-v2.key" not in seen:
        raise InvalidBackup("missing_history_key")
    try:
        manifest = json.loads(paths["manifest.json"].read_bytes())
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "created_utc", "databases"} or
                type(manifest["version"]) is not int or manifest["version"] != 1 or
                not isinstance(manifest["created_utc"], str) or
                datetime.fromisoformat(manifest["created_utc"].replace("Z", "+00:00")).tzinfo is None or
                not isinstance(manifest["databases"], dict)):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, OSError):
        raise InvalidBackup("invalid_manifest") from None
    observed = {name: hashes[name] for name in seen if name in DATABASES}
    if manifest["databases"] != observed or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                                                 for value in manifest["databases"].values() if isinstance(value, str)):
        raise InvalidBackup("database_digest_mismatch")
    if paths["secrets/vault-key"].stat().st_size != 32:
        raise InvalidBackup("invalid_vault_key")
    if "secrets/history-v2.key" in paths and paths["secrets/history-v2.key"].stat().st_size != 32:
        raise InvalidBackup("invalid_history_key")
    try:
        session = paths["secrets/session"].read_text(encoding="utf-8").strip()
        if not 32 <= len(session) <= MAX_SECRET or "\x00" in session:
            raise ValueError()
    except (UnicodeError, OSError, ValueError):
        raise InvalidBackup("invalid_session_key") from None
    for name in observed:
        if schema_policy is not None and name not in schema_policy:
            raise InvalidBackup("unsupported_database_schema")
        _check_database(paths[name], None if schema_policy is None else schema_policy[name])
    return {"created_utc": manifest["created_utc"], "files": paths, "digests": observed}


def verify_vault_key(files):
    """Check the archived key against each stored credential without exposing it."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = files["secrets/vault-key"].read_bytes()
    try:
        uri = files["databases/vault.sqlite"].absolute().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            rows = db.execute("SELECT provider,blob FROM vault LIMIT 1001").fetchall()
            if len(rows) > 1000:
                raise InvalidBackup("invalid_vault")
            cipher = AESGCM(key)
            for provider, blob in rows:
                if (not isinstance(provider, str) or not 1 <= len(provider) <= 256 or
                        not isinstance(blob, bytes) or not 28 <= len(blob) <= 1024 * 1024):
                    raise InvalidBackup("invalid_vault")
                cipher.decrypt(blob[:12], blob[12:], provider.encode())
    except (sqlite3.Error, InvalidTag, ValueError, UnicodeError):
        raise InvalidBackup("vault_key_mismatch") from None


def verify_history_key(files):
    """Authenticate encrypted history rows with their archived key and metadata."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    name = "databases/market-reports/history-v2.db"
    if name not in files:
        return
    key = files["secrets/history-v2.key"].read_bytes()
    try:
        uri = files[name].absolute().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 45
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            rows = db.execute("SELECT scope,dataset,day,observed,blob FROM days")
            cipher = AESGCM(key)
            for index, (scope, dataset, day, observed, blob) in enumerate(rows):
                if (index >= 1000000 or time.monotonic() >= deadline or
                        any(not isinstance(v, str) or len(v) > 1024 for v in (scope, dataset, day, observed)) or
                        not isinstance(blob, bytes) or not 28 <= len(blob) <= 16 * 1024 * 1024):
                    raise InvalidBackup("invalid_history")
                aad = json.dumps([scope, dataset, day, observed]).encode()
                cipher.decrypt(blob[:12], blob[12:], aad)
    except (sqlite3.Error, InvalidTag, ValueError, UnicodeError, KeyError):
        raise InvalidBackup("history_key_mismatch") from None
