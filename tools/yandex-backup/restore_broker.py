#!/usr/bin/env python3
"""Restricted socket-activated controller for owner-requested Disk restores."""
import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup
import cd
from native_release import secure, syncdir
from release_host import Host, ReleaseError, process_identity, process_uid
import restore_archive
import restore_controller
import restore_decrypt
import restore_disk
import restore_files


MAX_REQUEST = 32768
MAX_REPLY = 262144
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{22}\Z")
CONFIG = Path("/etc/wbozon-yandex-backup.json")


class BrokerError(ValueError):
    pass


class RestoreOutcomeUnknown(ValueError):
    pass


def _exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            raise BrokerError("invalid_request")
        data.extend(chunk)
    return bytes(data)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("invalid_request")
        result[key] = value
    return result


def receive(stream):
    stream.settimeout(10)
    length = struct.unpack("!I", _exact(stream, 4))[0]
    if not 0 < length <= MAX_REQUEST:
        raise BrokerError("invalid_request")
    try:
        request = json.loads(_exact(stream, length), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError):
        raise BrokerError("invalid_request") from None
    if not isinstance(request, dict):
        raise BrokerError("invalid_request")
    if request.get("action") in ("list", "resume") and set(request) == {"action"}:
        return request
    if (request.get("action") != "restore" or set(request) != {"action", "request_id", "archive", "identity"} or
            not isinstance(request["request_id"], str) or not REQUEST_ID.fullmatch(request["request_id"]) or
            not isinstance(request["archive"], str) or not restore_disk.NAME.fullmatch(request["archive"]) or
            not isinstance(request["identity"], str) or len(request["identity"]) > 11000):
        raise BrokerError("invalid_request")
    try:
        identity = base64.b64decode(request.pop("identity"), validate=True)
    except (ValueError, TypeError):
        raise BrokerError("invalid_request") from None
    if not 32 <= len(identity) <= 8192:
        raise BrokerError("invalid_request")
    request["identity"] = identity
    return request


def reply(stream, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    if not 0 < len(raw) <= MAX_REPLY:
        raise BrokerError("invalid_response")
    stream.sendall(struct.pack("!I", len(raw)) + raw)


def check_peer(stream, host):
    pid, uid, _gid = struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    props = host.props()
    master = int(props["MainPID"])
    if props["ActiveState"] != "active" or master <= 1 or uid != process_uid(master) or pid <= 1:
        raise BrokerError("invalid_peer")
    _, master_group = process_identity(master)
    _, group = process_identity(pid)
    if not props["ControlGroup"] or master_group != props["ControlGroup"] or group != master_group:
        raise BrokerError("invalid_peer")
    host.process(host.current())


def config_for(host):
    backup.regular_file(CONFIG, secret=True)
    raw = CONFIG.read_bytes()
    if len(raw) > 16384:
        raise BrokerError("invalid_config")
    config = json.loads(raw)
    fields = {"recipient_file", "data_root", "credentials", "state_dir", "runtime_dir", "deploy_lock"}
    if (not isinstance(config, dict) or set(config) != fields or any(not isinstance(v, str) for v in config.values()) or
            Path(config["deploy_lock"]) != host.state / "deploy.lock" or
            Path(config["data_root"]).resolve(strict=True) != Path(host.cfg["databases"][0]).parent.resolve(strict=True) or
            Path(host.cfg["databases"][1]).parent.resolve(strict=True) != Path(config["data_root"]).resolve(strict=True) or
            [Path(p).name for p in host.cfg["databases"]] != ["auth.sqlite", "vault.sqlite"]):
        raise BrokerError("invalid_config")
    secure(Path(config["credentials"]), True)
    secure(Path(config["state_dir"]), True)
    return config


@contextlib.contextmanager
def settings_lock(config):
    root = Path(config["data_root"])
    descriptor = os.open(root / ".yandex-backup.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != root.stat().st_uid or
                info.st_mode & 0o077):
            raise BrokerError("invalid_settings_lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _claim(config, request_id):
    directory = Path(config["state_dir"]) / "restore-requests"
    directory.mkdir(mode=0o700, exist_ok=True)
    secure(directory, True)
    if directory.stat().st_mode & 0o077:
        raise BrokerError("invalid_request_directory")
    path = directory / request_id
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as stream:
        json.dump({"accepted": int(time.time())}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    syncdir(directory)
    return True


def _status(config, state, reason=None):
    if state not in {"started", "completed", "failed", "unknown"}:
        raise BrokerError("invalid_status")
    value = {"state": state, "at": int(time.time())}
    if reason and re.fullmatch(r"[a-z0-9_]{1,80}", reason):
        value["reason"] = reason
    path = Path(config["state_dir"]) / "restore-status.json"
    fd, temp = tempfile.mkstemp(prefix=".restore-status-", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        syncdir(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def run_restore(host, config, request):
    runtime = Path(os.environ.get("RUNTIME_DIRECTORY", ""))
    if runtime.parent != Path("/run") or not re.fullmatch(r"wbozon-restore-[A-Za-z0-9_.:-]+", runtime.name):
        raise BrokerError("unsafe_runtime_directory")
    runtime = secure(runtime, True)
    if runtime.stat().st_mode & 0o077:
        raise BrokerError("unsafe_runtime_directory")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=runtime) as temp:
        root = Path(temp)
        encrypted, archive = root / "backup.age", root / "backup.tar.gz"
        with encrypted.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            restore_disk.download(config, request["archive"], output)
            output.flush()
            os.fsync(output.fileno())
        identity = request.pop("identity")
        try:
            restore_decrypt.decrypt(encrypted, identity, archive)
        finally:
            del identity
        unpacked = root / "unpacked"
        unpacked.mkdir(mode=0o700)
        with archive.open("rb") as source:
            result = restore_archive.unpack_verified(source, unpacked)
        restore_archive.verify_vault_key(result["files"])
        restore_archive.verify_history_key(result["files"])
        work = root / "prepared"
        work.mkdir(mode=0o700)
        key = Path(config["credentials"]) / "vault-key"
        backup.regular_file(key, secret=True)
        current_key = key.read_bytes()
        try:
            restore_controller.activate(cd.Transaction(host), result["files"], work, current_key)
        except Exception:
            # Once the controller begins, only its durable journal/recovery can
            # establish the result. Do not invite a potentially destructive retry.
            raise RestoreOutcomeUnknown("restore_outcome_unknown") from None


def serve(stream):
    host = Host(cd.CONFIG)
    check_peer(stream, host)
    request = receive(stream)
    config = config_for(host)
    if request["action"] == "resume":
        with host.locked():
            txn = cd.Transaction(host)
            if txn.pending.exists():
                raise BrokerError("recovery_pending")
            path = txn.market_hold
            if path.exists() or path.is_symlink():
                try:
                    value = json.loads(secure(path).read_bytes())
                except (OSError, ValueError):
                    raise BrokerError("invalid_restore_hold") from None
                if (not isinstance(value, dict) or set(value) != {"purpose", "txn"} or
                        value["purpose"] != "restore" or not isinstance(value["txn"], str) or
                        not re.fullmatch(r"[a-f0-9]{16}", value["txn"])):
                    raise BrokerError("invalid_restore_hold")
                path.unlink()
                syncdir(path.parent)
        reply(stream, {"status": "ok"})
        return
    if request["action"] == "list":
        with settings_lock(config):
            items = restore_disk.list_backups(config)
        reply(stream, {"status": "ok", "backups": [{"name": item["name"], "size": item["size"]} for item in items]})
        return
    with settings_lock(config), host.locked():
        if cd.Transaction(host).pending.exists():
            raise BrokerError("recovery_pending")
        if not _claim(config, request["request_id"]):
            reply(stream, {"status": "accepted"})
            return
        _status(config, "started")
        reply(stream, {"status": "accepted"})
        try:
            run_restore(host, config, request)
        except RestoreOutcomeUnknown:
            _status(config, "unknown")
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, (BrokerError, restore_archive.InvalidBackup,
                           restore_disk.RestoreDiskError, restore_decrypt.DecryptionError,
                           restore_files.RestoreFilesError)) else "restore_failed"
            _status(config, "failed", reason)
            raise
        _status(config, "completed")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        with socket.socket(fileno=0) as connection:
            serve(connection)
    except Exception:
        # Deliberately never log a request, identity, URL, or exception payload.
        print("restore request failed", file=sys.stderr, flush=True)
        sys.exit(1)
