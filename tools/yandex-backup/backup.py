#!/usr/bin/env python3
"""Create encrypted SQLite snapshots and upload them to a specified Yandex Disk folder.

All credentials and deployment-specific paths live outside the repository.  The
archive is encrypted to an age recipient whose private key is not on the VPS.
"""

import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import fcntl
except ImportError:  # Pure helpers can still be tested on Windows.
    fcntl = None


API = "https://cloud-api.yandex.net/v1/disk"
DATABASES = (
    ("auth.sqlite", True),
    ("vault.sqlite", True),
    ("vault.sqlite.avito-keys", False),
    ("vault.sqlite.catalog-keys", False),
    ("vault.sqlite.market-actions", False),
    ("vault.sqlite.cost-calculator", False),
    (".market-reports/history-v2.db", False),
)
SECRET_FILES = (
    ("vault-key", True),
    ("session", True),
)


class BackupError(Exception):
    """Expected error with a fixed, non-secret message."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def regular_file(path, *, secret=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BackupError("required_file_missing") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (secret and (info.st_uid != 0 or info.st_mode & 0o077))):
        raise BackupError("unsafe_file_permissions")
    return info


def load_settings(data_root, credentials):
    """Read the same AES-GCM-protected settings saved by the portal."""
    database = Path(data_root) / "vault.sqlite"
    keyfile = Path(credentials) / "vault-key"
    regular_file(database)
    regular_file(keyfile, secret=True)
    key = keyfile.read_bytes()
    if len(key) != 32:
        raise BackupError("invalid_vault_key")
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT blob FROM vault WHERE provider=?", ("yandex_backup",)).fetchone()
        if row is None:
            return None
        blob = bytes(row[0])
        settings = json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], b"yandex_backup"))
    except Exception:
        raise BackupError("backup_settings_unavailable") from None
    if (not isinstance(settings, dict) or set(settings) != {"token", "public_url", "disk_folder", "enabled"} or
            type(settings["enabled"]) is not bool or
            not isinstance(settings["token"], str) or
            not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,2048}", settings["token"]) or
            not isinstance(settings["public_url"], str) or not isinstance(settings["disk_folder"], str)):
        raise BackupError("invalid_backup_settings")
    return settings


def json_request(path, params, token=None):
    url = API + path + "?" + urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "wbozon-backup/1"}
    if token:
        headers["Authorization"] = "OAuth " + token
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                raise BackupError("invalid_yandex_response")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise BackupError("yandex_http_" + str(code)) from None
    except (OSError, URLError, TimeoutError):
        raise BackupError("yandex_unavailable") from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BackupError("invalid_yandex_response") from None
    if not isinstance(result, dict):
        raise BackupError("invalid_yandex_response")
    return result


def validate_upload_url(value):
    """Accept only HTTPS uploader hosts controlled by Yandex, without credentials."""
    if not isinstance(value, str) or len(value) > 8192:
        raise BackupError("invalid_upload_url")
    parsed = urlsplit(value)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise BackupError("invalid_upload_url") from None
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or
            port not in (None, 443) or parsed.fragment or
            not re.fullmatch(r"[a-z0-9.-]+", host) or
            not (host.endswith(".yandex.net") or host.endswith(".yandex.ru"))):
        raise BackupError("invalid_upload_url")
    return parsed


def verify_folder(token, folder, expected_resource_id):
    if not isinstance(folder, str) or not folder.startswith("disk:/") or ".." in folder.split("/"):
        raise BackupError("invalid_disk_folder")
    if not isinstance(expected_resource_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,200}", expected_resource_id):
        raise BackupError("invalid_folder_identity")
    target = json_request("/resources", {"path": folder, "limit": 0}, token)
    if target.get("type") != "dir" or target.get("resource_id") != expected_resource_id:
        raise BackupError("folder_identity_mismatch")


def resolve_folder_identity(public_url):
    if not isinstance(public_url, str) or len(public_url) > 512:
        raise BackupError("invalid_public_folder_link")
    public = urlsplit(public_url)
    try:
        valid_port = public.port is None
    except ValueError:
        valid_port = False
    if (public.scheme != "https" or public.hostname not in ("disk.yandex.ru", "yadi.sk") or
            public.username or public.password or not valid_port or public.query or public.fragment or
            not re.fullmatch(r"/d/[A-Za-z0-9_-]{1,128}/?", public.path)):
        raise BackupError("invalid_public_folder_link")
    shared = json_request("/public/resources", {"public_key": public_url, "limit": 0})
    identity = shared.get("resource_id")
    if shared.get("type") != "dir" or not isinstance(identity, str):
        raise BackupError("invalid_public_folder_link")
    return identity


def file_digest(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def snapshots(data_root, work_dir):
    """Lock writers across all present databases, then make verified SQLite snapshots."""
    data_root, work_dir = Path(data_root), Path(work_dir)
    selected = []
    for rel, required in DATABASES:
        source = data_root / rel
        if not source.exists() and not source.is_symlink():
            if required:
                raise BackupError("required_database_missing")
            continue
        for component in list(source.parents)[: len(Path(rel).parts) - 1]:
            if component.is_symlink():
                raise BackupError("unsafe_database_path")
        info = regular_file(source)
        selected.append((rel, source, info))
    available = os.statvfs(work_dir).f_bavail * os.statvfs(work_dir).f_frsize
    if sum(info.st_size for _, _, info in selected) * 2 + 32 * 1024 * 1024 > available:
        raise BackupError("insufficient_temporary_space")
    locks = []
    results = []
    try:
        for _, source, _ in selected:
            lock = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None)
            locks.append(lock)
            lock.execute("BEGIN IMMEDIATE")
        for index, (rel, source, original) in enumerate(selected):
            target = work_dir / (str(index) + ".sqlite")
            deadline = time.monotonic() + 30
            def progress(_status, _remaining, _total):
                if time.monotonic() > deadline:
                    raise BackupError("sqlite_snapshot_timed_out")
            with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as src:
                with contextlib.closing(sqlite3.connect(target)) as dst:
                    src.backup(dst, pages=128, progress=progress, sleep=0.05)
                    if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise BackupError("sqlite_integrity_failed")
            current = regular_file(source)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise BackupError("database_changed_during_snapshot")
            os.chmod(target, 0o600)
            results.append((rel, target))
    except sqlite3.Error:
        raise BackupError("sqlite_snapshot_failed") from None
    finally:
        for lock in reversed(locks):
            with contextlib.suppress(sqlite3.Error):
                lock.rollback()
            lock.close()
    return results


def add_bytes(tar, name, data):
    import io
    member = tarfile.TarInfo(name)
    member.size, member.mode, member.mtime = len(data), 0o600, int(time.time())
    tar.addfile(member, io.BytesIO(data))


def encrypt_archive(out_path, recipient_path, snapshot_files, credentials, history_key):
    regular_file(recipient_path)
    created = False
    try:
        with open(out_path, "xb") as encrypted:
            created = True
            os.fchmod(encrypted.fileno(), 0o600)
            process = subprocess.Popen(["/usr/bin/age", "-R", str(recipient_path)],
                                       stdin=subprocess.PIPE, stdout=encrypted, stderr=subprocess.DEVNULL)
            try:
                manifest = {"version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "databases": {}}
                with tarfile.open(fileobj=process.stdin, mode="w|gz") as archive:
                    for rel, snapshot in snapshot_files:
                        name = "databases/" + rel.lstrip(".")
                        archive.add(snapshot, arcname=name, recursive=False)
                        manifest["databases"][name] = file_digest(snapshot)
                    for name, required in SECRET_FILES:
                        path = Path(credentials) / name
                        if not path.exists() and not required:
                            continue
                        regular_file(path, secret=True)
                        add_bytes(archive, "secrets/" + name, path.read_bytes())
                    if history_key.exists():
                        regular_file(history_key)
                        add_bytes(archive, "secrets/history-v2.key", history_key.read_bytes())
                    add_bytes(archive, "manifest.json", json.dumps(manifest, sort_keys=True).encode())
                process.stdin.close()
                if process.wait(timeout=60) != 0:
                    raise BackupError("encryption_failed")
                encrypted.flush()
                os.fsync(encrypted.fileno())
            except BaseException:
                process.kill()
                process.wait()
                raise
    except BaseException:
        if created:
            Path(out_path).unlink(missing_ok=True)
        raise


def upload(token, folder, encrypted):
    name = encrypted.name
    remote_path = folder.rstrip("/") + "/" + name
    info = json_request("/resources/upload", {"path": remote_path, "overwrite": "false"}, token)
    if info.get("method") != "PUT" or info.get("templated") is not False:
        raise BackupError("invalid_upload_response")
    url = validate_upload_url(info.get("href"))
    connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=45,
                                             context=ssl.create_default_context())
    request_path = url.path + ("?" + url.query if url.query else "")
    try:
        with open(encrypted, "rb") as data:
            connection.request("PUT", request_path, body=data,
                               headers={"Content-Type": "application/octet-stream", "Content-Length": str(encrypted.stat().st_size)})
            response = connection.getresponse()
            response.read(4096)
    except (OSError, TimeoutError, http.client.HTTPException):
        # Never repeat a PUT after an uncertain response. Keep the encrypted file.
        pass
    finally:
        connection.close()
    expected_md5 = file_digest(encrypted, "md5")
    for attempt in range(6):
        try:
            meta = json_request("/resources", {"path": remote_path}, token)
            if (meta.get("type") == "file" and meta.get("size") == encrypted.stat().st_size and
                    meta.get("md5") == expected_md5):
                return
        except BackupError:
            pass
        if attempt < 5:
            time.sleep(3)
    raise BackupError("upload_not_confirmed")


def write_status(state_dir, state, reason=None):
    directory = Path(state_dir)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = directory / "status.json"
    try:
        previous = json.loads(target.read_text())
        if not isinstance(previous, dict): previous = {}
    except (OSError, ValueError):
        previous = {}
    stamp = datetime.now(timezone.utc).isoformat()
    status = {"state": state, "last_attempt": stamp}
    if state == "uploaded_and_verified":
        status["last_success"] = stamp
    elif isinstance(previous.get("last_success"), str):
        status["last_success"] = previous["last_success"]
    if reason and re.fullmatch(r"[a-z0-9_]{1,80}", reason):
        status["reason"] = reason
    fd, name = tempfile.mkstemp(prefix=".status-", dir=directory)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as stream:
            json.dump(status, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(name)


def main(config_path="/etc/wbozon-yandex-backup.json"):
    if fcntl is None:
        raise BackupError("linux_required")
    config_path = Path(config_path)
    regular_file(config_path, secret=True)
    config = json.loads(config_path.read_text())
    required = {"recipient_file", "data_root", "credentials", "state_dir", "runtime_dir", "deploy_lock"}
    if not isinstance(config, dict) or set(config) != required or not all(isinstance(v, str) for v in config.values()):
        raise BackupError("invalid_config")
    state, runtime = Path(config["state_dir"]), Path(config["runtime_dir"])
    state.mkdir(mode=0o755, parents=True, exist_ok=True)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_lock = Path(config["data_root"]) / ".yandex-backup.lock"
    if not settings_lock.exists() and not settings_lock.is_symlink():
        print('backup_disabled_or_not_configured')
        return
    fd = os.open(settings_lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != Path(config["data_root"]).stat().st_uid or info.st_mode & 0o077):
            raise BackupError("unsafe_settings_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            settings = load_settings(config["data_root"], config["credentials"])
            if settings is None or not settings["enabled"]:
                print('backup_disabled_or_not_configured')
                return
            token = settings["token"]
            folder = settings["disk_folder"]
            verify_folder(token, folder, resolve_folder_identity(settings["public_url"]))
            name = "wbozon-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(6).hex() + ".tar.gz.age"
            encrypted = state / name
            deploy_fd = os.open(config["deploy_lock"], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(deploy_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with tempfile.TemporaryDirectory(prefix="snapshot-", dir=runtime) as temp:
                    snapshot_files = snapshots(config["data_root"], temp)
                    history_key = Path(config["data_root"]) / ".market-reports" / "history-v2.key"
                    encrypt_archive(encrypted, config["recipient_file"], snapshot_files, config["credentials"], history_key)
            finally:
                os.close(deploy_fd)
            upload(token, folder, encrypted)
            digest = file_digest(encrypted)
            encrypted.unlink()
            write_status(state, "uploaded_and_verified")
            print(json.dumps({"result": "uploaded_and_verified", "file": name, "sha256": digest}, sort_keys=True))
        except Exception as error:
            with contextlib.suppress(OSError, ValueError):
                write_status(state, "failed", str(error) if isinstance(error, BackupError) else type(error).__name__.lower())
            raise
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except (BackupError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print("backup_failed: " + (str(error) if isinstance(error, BackupError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
