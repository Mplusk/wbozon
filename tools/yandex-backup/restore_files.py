"""Durable, repeatable file replacement for an offline restore transaction.

The caller must stop and fence every writer first and keep that fence until the
journal is committed or rollback is complete. This module knows only a fixed
set of application files; archive names and browser input are never paths.
"""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile


FILES = (
    "auth.sqlite", "vault.sqlite", "vault.sqlite.avito-keys",
    "vault.sqlite.catalog-keys", "vault.sqlite.market-actions",
    "vault.sqlite.cost-calculator", ".market-reports/history-v2.db",
    ".market-reports/history-v2.key",
)
DATABASES = set(FILES) - {".market-reports/history-v2.key"}


class RestoreFilesError(ValueError):
    pass


def _syncdir(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _root(root, uid=None):
    root = Path(root)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or root.is_symlink() or
            os.name != "nt" and (info.st_mode & 0o077 or uid is not None and info.st_uid != uid)):
        raise RestoreFilesError("unsafe_data_directory")
    return root


def _target(root, name, *, make_parent=False, uid=None, gid=None):
    if name not in FILES:
        raise RestoreFilesError("invalid_restore_target")
    parent = _root(root, uid)
    if name.startswith(".market-reports/"):
        parent = parent / ".market-reports"
        if make_parent and not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o700)
            if os.name != "nt" and uid is not None:
                os.chown(parent, uid, gid)
            _syncdir(parent.parent)
        if parent.is_symlink():
            raise RestoreFilesError("unsafe_data_directory")
        if parent.exists() and not parent.is_dir():
            raise RestoreFilesError("unsafe_data_directory")
        if parent.exists() and os.name != "nt":
            info = parent.stat()
            if info.st_mode & 0o077 or uid is not None and info.st_uid != uid:
                raise RestoreFilesError("unsafe_data_directory")
    return parent / Path(name).name


def _regular(path, uid=None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (os.name != "nt" and (info.st_mode & 0o077 or uid is not None and info.st_uid != uid))):
        raise RestoreFilesError("unsafe_database_file")
    return info


def snapshot(root, directory, *, uid=None):
    """Save independent SQLite snapshots. Must complete before any replacement."""
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink() or any(directory.iterdir()):
        raise RestoreFilesError("unsafe_snapshot_directory")
    records = {}
    for index, name in enumerate(FILES):
        source = _target(root, name, uid=uid)
        info = _regular(source, uid)
        if info is None:
            records[name] = None
            continue
        dest = directory / (str(index) + ".snapshot")
        if name in DATABASES:
            try:
                with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
                    db.execute("PRAGMA trusted_schema=OFF")
                    with closing(sqlite3.connect(dest)) as out:
                        db.backup(out)
                        out.execute("PRAGMA trusted_schema=OFF")
                        if out.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise RestoreFilesError("invalid_current_database")
            except sqlite3.Error:
                raise RestoreFilesError("cannot_snapshot_database") from None
        else:
            shutil.copyfile(source, dest)
        if os.name != "nt":
            os.chmod(dest, 0o600)
        with dest.open("r+b") as stream:
            os.fsync(stream.fileno())
        after = _regular(source, uid)
        if (after is None or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            raise RestoreFilesError("database_changed_during_snapshot")
        records[name] = {"file": dest.name, "sha256": _digest(dest)}
    _syncdir(directory)
    return records


def _validate_records(directory, records):
    if not isinstance(records, dict) or set(records) != set(FILES):
        raise RestoreFilesError("invalid_snapshot_record")
    for index, name in enumerate(FILES):
        record = records[name]
        if record is None:
            continue
        if (not isinstance(record, dict) or set(record) != {"file", "sha256"} or
                record["file"] != str(index) + ".snapshot" or not isinstance(record["sha256"], str) or
                len(record["sha256"]) != 64):
            raise RestoreFilesError("invalid_snapshot_record")
        path = directory / record["file"]
        if _regular(path) is None or _digest(path) != record["sha256"]:
            raise RestoreFilesError("snapshot_digest_mismatch")


def _replace(root, name, source, *, uid=None, gid=None):
    target = _target(root, name, make_parent=source is not None, uid=uid, gid=gid)
    if not target.parent.exists():
        return
    _regular(target, uid)
    if source is None:
        target.unlink(missing_ok=True)
    else:
        _regular(source)
        fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
                if uid is not None:
                    os.fchown(fd, uid, gid)
            with os.fdopen(fd, "wb") as out, open(source, "rb") as incoming:
                shutil.copyfileobj(incoming, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Offline SQLite snapshots incorporate WAL contents; stale sidecars cannot
    # be replayed against a different main file after a crash or replacement.
    if name in DATABASES:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            _regular(sidecar, uid)
            sidecar.unlink(missing_ok=True)
    _syncdir(target.parent)


def apply(root, prepared, *, uid=None, gid=None, after_each=None):
    if not isinstance(prepared, dict) or not set(prepared) <= set(FILES):
        raise RestoreFilesError("invalid_prepared_files")
    for name in FILES:
        if name in prepared:
            _replace(root, name, prepared[name], uid=uid, gid=gid)
            if after_each:
                after_each(name)


def rollback(root, directory, records, *, uid=None, gid=None, after_each=None):
    directory = Path(directory)
    _validate_records(directory, records)
    for name in FILES:
        record = records[name]
        _replace(root, name, directory / record["file"] if record else None, uid=uid, gid=gid)
        if after_each:
            after_each(name)
