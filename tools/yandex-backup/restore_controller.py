"""Apply a verified backup through the portal's durable deployment interlock.

The caller holds the deploy lock. A pending journal prevents normal service
starts while any data file can be in an intermediate state. Recovery never
replays a restored file set after admission has reopened.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from native_release import secure, syncdir
from release_host import ReleaseError, atomic_json
import restore_files
import restore_prepare


def _snapshot_dir(host, journal):
    txn = journal.get("txn", "")
    if not isinstance(txn, str) or len(txn) != 16 or any(c not in "0123456789abcdef" for c in txn):
        raise ReleaseError("invalid restore transaction")
    parent = secure(host.state / "backups" / txn, True)
    record = json.loads(secure(parent / "cd-snapshot.json").read_bytes())
    if record != {"txn": txn}:
        raise ReleaseError("invalid restore snapshot")
    return secure(parent / "before", True)


def _ensure_identity(host, name, journal):
    if host.current() != name or name != journal["old"] or name != journal["new"]:
        raise ReleaseError("release changed during restore")
    if journal["old_sha"] != journal["new_sha"]:
        raise ReleaseError("invalid restore release manifest")
    host.verify(name, journal["old_sha"])
    host.check_unit()
    host.settled()


def _start_old_or_restored(txn, journal, guard):
    txn.phase(journal, "restoring")
    txn.start(journal, journal["old"], guard)


def _space_path(host, journal):
    txn = journal.get("txn", "")
    if not isinstance(txn, str) or len(txn) != 16 or any(c not in "0123456789abcdef" for c in txn):
        raise ReleaseError("invalid restore transaction")
    return host.state / (".restore-space-" + txn)


def _release_space(host, journal):
    path = _space_path(host, journal)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o077:
        raise ReleaseError("invalid restore reserve")
    path.unlink()
    syncdir(path.parent)


def _reserve_space(host, journal, data, before, records, prepared):
    if host.state.stat().st_dev != data.stat().st_dev or not hasattr(os, "posix_fallocate"):
        raise ReleaseError("restore requires an available rollback reserve")
    required = sum((before / r["file"]).stat().st_size for r in records.values() if r)
    replacement = sum(Path(p).stat().st_size for p in prepared.values() if p is not None)
    path = _space_path(host, journal)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.posix_fallocate(fd, 0, required + 8 * 1024 * 1024)
        os.fsync(fd)
    finally:
        os.close(fd)
    syncdir(path.parent)
    if shutil.disk_usage(data).free < replacement + 32 * 1024 * 1024:
        raise ReleaseError("insufficient space for a reversible restore")


def _release_restore_hold(txn, journal):
    path = txn.market_hold
    if not path.exists() and not path.is_symlink():
        return
    observed = json.loads(secure(path).read_bytes())
    if observed != {"purpose": "restore", "txn": journal["txn"]}:
        raise ReleaseError("restore provider hold differs from journal")
    path.unlink()
    syncdir(path.parent)


def activate(txn, files, work, current_key):
    """Restore an already verified archive; keep the same authenticated release."""
    host = txn.host
    if txn.pending.exists() or txn.market_hold.exists():
        raise ReleaseError("another recovery is pending")
    name, sha = txn.receipt()
    host.verify(name, sha)
    host.check_unit()
    host.settled()
    pid, _, _ = host.process(name)
    if host.holder() != os.stat("/proc/" + str(pid)).st_uid:
        raise ReleaseError("portal identity changed")
    host.sockets()
    host.reserve(True)
    previous = json.loads(secure(txn.active).read_bytes()).get("previous", name) if txn.active.exists() else name
    journal = {"txn": secrets.token_hex(8), "mark": secrets.randbelow(2**30) + 1,
               "old": name, "new": name, "old_sha": sha, "new_sha": sha, "previous": previous,
               "restore": 1, "restore_state": "untouched"}
    try:
        txn.phase(journal, "draining")
        host.barrier(journal["txn"], journal["mark"], True)
        pid, generation, group = txn.drain(name, journal["mark"], require_healthy=True)
        with txn.gate_lock(allow_uncertain=True) as guard:
            guard.verify("/proc/" + str(pid) + "/root", "/proc/" + str(pid) + "/ns/time")
            if host.sockets() or not host.workers_idle(pid, group) or host.process(name) != (pid, generation, group):
                raise ReleaseError("portal changed during restore drain")
            txn.phase(journal, "stopping")
            host.stop()
            if host.props()["MainPID"] != "0":
                raise ReleaseError("portal did not stop")
            data = Path(host.cfg["databases"][0]).parent.resolve(strict=True)
            if Path(host.cfg["databases"][1]).parent.resolve(strict=True) != data:
                raise ReleaseError("unexpected data directory")
            info = data.stat()
            parent = host.state / "backups" / journal["txn"]
            parent.mkdir(mode=0o700)
            syncdir(parent.parent)
            before = parent / "before"
            before.mkdir(mode=0o700)
            records = restore_files.snapshot(data, before, uid=info.st_uid)
            atomic_json(parent / "cd-snapshot.json", {"txn": journal["txn"]})
            prepared = restore_prepare.build(files, before, records, current_key, work)
            if host.holder() != info.st_uid:
                raise ReleaseError("portal identity changed before restore")
            _reserve_space(host, journal, data, before, records, prepared)
            atomic_json(txn.market_hold, {"purpose": "restore", "txn": journal["txn"]})
            journal["records"] = records
            journal["restore_state"] = "modifying"
            txn.phase(journal, "restoring")
            restore_files.apply(data, prepared, uid=info.st_uid, gid=info.st_gid)
            guard.verify()
            if host.holder() != info.st_uid:
                raise ReleaseError("portal identity changed during restore")
            txn.start(journal, name, guard)
            txn.phase(journal, "committed")
            _release_space(host, journal)
        txn.complete(journal, name, sha)
        host.reserve(False)
    except BaseException:
        if txn.pending.exists():
            recover(txn, json.loads(secure(txn.pending).read_bytes()))
        else:
            host.reserve(False)
        raise


def recover(txn, journal):
    """Finish committed work or repeatedly roll back any incomplete replacement."""
    host = txn.host
    if journal.get("restore") != 1 or journal.get("restore_state") not in ("untouched", "modifying", "rolled_back"):
        raise ReleaseError("invalid restore journal")
    name = journal["old"]
    _ensure_identity(host, name, journal)
    host.reserve(False)
    host.barrier(journal["txn"], journal["mark"], True)
    props = host.props()
    state = journal["restore_state"]
    committed = journal["phase"] == "committed"
    if props["ActiveState"] == "active":
        pid, _, _ = host.process(name)
        if not committed and state == "modifying":
            txn.drain(name, journal["mark"])
        elif state in ("untouched", "rolled_back") or committed:
            _release_space(host, journal)
            if not committed:
                _release_restore_hold(txn, journal)
            with txn.gate_lock(allow_uncertain=True) as guard:
                guard.verify("/proc/" + str(pid) + "/root", "/proc/" + str(pid) + "/ns/time")
                txn.ready(journal, name, guard)
            txn.complete(journal, name, journal["old_sha"])
            return
    with txn.gate_lock(allow_uncertain=True) as guard:
        if host.props()["ActiveState"] not in ("inactive", "failed"):
            host.stop()
        if host.props()["MainPID"] != "0":
            raise ReleaseError("portal still running during restore recovery")
        if not committed and state == "modifying":
            before = _snapshot_dir(host, journal)
            restore_files._validate_records(before, journal.get("records"))
            _release_space(host, journal)
            data = Path(host.cfg["databases"][0]).parent.resolve(strict=True)
            info = data.stat()
            restore_files.rollback(data, before, journal.get("records"), uid=info.st_uid, gid=info.st_gid)
            journal["restore_state"] = "rolled_back"
        else:
            _release_space(host, journal)
        if committed:
            txn.phase(journal, "committed")
            txn.start(journal, name, guard)
        else:
            _release_restore_hold(txn, journal)
            _start_old_or_restored(txn, journal, guard)
    txn.complete(journal, name, journal["old_sha"])
