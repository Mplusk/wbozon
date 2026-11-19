"""Verified native deployment, durable start interlock and crash recovery."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import time

# Invoked with python -I. Only the administrator-installed module directory is trusted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release
import github_artifact
from finance_guard import FinanceGuard
from native_release import secure, syncdir
from release_host import Host, ReleaseError, atomic_json, boot_id, command, compatible, discard, process_identity, process_uid

CONFIG = '/etc/wbozon-cd.json'
SCOPED_BACKUP_SECONDS = 5
# Table columns, constraints and unique indexes created by the reviewed KeyStore.
SCOPED_KEYS_SCHEMA = '01adb0bd15e847f9d1fadccb4b45169c0271bb00a15f7667d69d796787278723'
ACTION_SCHEMA = '990bf1ca3f3cd956f47988c1b16ad9bfe6d60ca16c2fc8fb5eed8f6933bf7ac1'

MARKET_BASE = 'b543e1568394095a20f14213feb3a194a456716c70d43b95c06b33c98cd32aed'
MARKET_BRIDGE = '4fbd8858010f37f39b7676c4b34fede5526aa56623569bbc231e77524edce0cf'
MARKET_REPORTS = '88b7d5dde100b635d7ac881e1f6999fc9d0626fff5c1ea9e722e31ec58aa9dbc'
MARKET_V2 = 'efce8b3fceb171b0ce4025e08708389623993046cb2962d26fbee43063af4187'
# Exact next service inventory from d5ae72734b1827894c1a26d132389005eb166e4c.
# Change only together with the complete transition review.
MARKET_V2_NEXT = '0f009122b40353df9f0a65c8cd8b01b3efaf39703eaa5281349bd8c8183aae81'
# Verified regional reports, cancellation coverage and seller rating, service tree 95a19af.
MARKET_V2_REPORT_FIX = '85d5b8a840f480678fe83fbf3bbd301a8db75d0910b8dfb39dfdce5fbe63d41f'
# SHA-256 inventories of subsequent reviewed services, in deployment order.
MARKET_REPORT_SUCCESSORS = (
    'ea75c8c64e6a3295f2c3e4c8e483b15e7194e89bb300587ff9b936bf04e18dba',
    '013c84e6a33beb009e7498468adbbe43950835cba416fd6ee690bf5b89f50a0f',
    '9cbdcecd8e7361ac58560f50ce9c97281e36d9bdeb9cddd101c7a4fcfd78b1a2',
    '9b5fb7bdca64eaa954643f59314b0bd6c0486ca90bb3fd48bdc078599a1f1945',
    '872fa677179f0d545a10425930829940f99b16a8d8ab542d40556ff32018401c',
    '514293153b21ffc85b1094b8bd5b278ffefc7374260848c77cd67901f0e26744',
    'a6d6fea9cc73afa9739c8a93f36b6ccb4cfae1cf98a022a370b062a3c73b2624',
    'a63868975d03c62fa5a51248a9c71ce05f0e9baf297944103dbc0b12531d73b9',
)


def present(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def backup_scoped_keys(host, txn):
    """Snapshot optional grants and the action journal while both workers are drained."""
    for suffix,schema in (('.avito-keys',SCOPED_KEYS_SCHEMA),('.catalog-keys',SCOPED_KEYS_SCHEMA),
                          ('.market-actions',ACTION_SCHEMA)):
        backup_sidecar(host,txn,suffix,schema)


def backup_sidecar(host, txn, suffix, expected_schema):
    source = Path(host.cfg['databases'][1] + suffix)
    if not present(source):
        return
    info = source.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != host.gate.parent.stat().st_uid or info.st_mode & 0o077):
        raise ReleaseError('unexpected scoped key database')
    directory = secure(host.state / 'backups' / txn, True)
    snapshot = directory / source.name
    partial = directory / (source.name + '.partial')
    deadline = time.monotonic() + SCOPED_BACKUP_SECONDS

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise ReleaseError('scoped key database snapshot timed out')

    with partial.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
    try:
        with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as src:
            src.execute('PRAGMA trusted_schema=OFF')
            with contextlib.closing(sqlite3.connect(partial)) as dest:
                src.backup(dest, pages=128, progress=progress, sleep=0.05)
                dest.execute('PRAGMA trusted_schema=OFF')
                dest.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                if Host.schema(dest) != expected_schema or dest.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ReleaseError('scoped key snapshot verification failed')
        with partial.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(partial, snapshot)
        syncdir(directory)
    finally:
        partial.unlink(missing_ok=True)


def market_inventory(release):
    # Called only on complete releases already authenticated by Host.verify.
    # The original installed release_host predates service inventory support.
    service = release / 'service'
    files = {p.relative_to(service).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in service.rglob('*') if p.is_file()}
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def market_transition(old, new):
    first, second = market_inventory(old), market_inventory(new)
    protected = {MARKET_BRIDGE, MARKET_REPORTS, MARKET_V2}
    successors = (MARKET_V2_NEXT, MARKET_V2_REPORT_FIX, *MARKET_REPORT_SUCCESSORS)
    for inventory in successors:
        if (not isinstance(inventory, str) or not re.fullmatch('[0-9a-f]{64}', inventory) or
                inventory in protected | {MARKET_BASE, '0' * 64}):
            raise ReleaseError('invalid marketplace release inventory')
        protected.add(inventory)
    if (first, second) == (MARKET_BASE, MARKET_BRIDGE):
        return 'bootstrap'
    previous = MARKET_REPORTS
    for reviewed in (MARKET_V2, *successors):
        if first == second == reviewed or {first, second} == {previous, reviewed}:
            return 'preserve_blocked_finance'
        previous = reviewed
    if {first, second} == {MARKET_BRIDGE, MARKET_REPORTS}:
        return 'shared'
    if first != second and {first, second} & protected:
        raise ReleaseError('unreviewed marketplace cooldown transition')
    compatible(old, new)
    return None


class Transaction:
    def __init__(self, host):
        self.host = host
        self.pending = host.state / 'pending.json'
        self.permit = host.state / 'permit.json'
        self.active = host.state / 'active.json'

    def phase(self, j, phase):
        j['phase'] = phase
        atomic_json(self.pending, j)
        print('Deployment phase:', phase, flush=True)

    def receipt(self):
        name = self.host.current()
        if self.active.exists():
            receipt = json.loads(secure(self.active).read_bytes())
            if receipt['release'] != name:
                raise ReleaseError('active receipt differs from current release')
            return name, receipt['manifest']
        if name != self.host.cfg['baseline']:
            raise ReleaseError('missing release receipt')
        return name, self.host.cfg['baseline_sha256']

    @property
    def market_hold(self):
        return self.host.root / '.market-cutover-pending'

    def check_market_hold(self, j):
        record = json.loads(secure(self.market_hold).read_bytes())
        if record != {field: j[field] for field in ('txn', 'old', 'new')}:
            raise ReleaseError('marketplace bootstrap hold differs from the transaction')

    def seal_market_decision(self, j, name):
        # Recovery has a ConditionPathExists=pending.json. Keep that journal until
        # the hold is gone; persist the rollback floor before any seller can call.
        if 'market_final' not in j:
            if name == j['new']:
                self.check_market_hold(j)
            j['market_final'] = name
            self.phase(j, 'market-final')
        if j['market_final'] != name:
            raise ReleaseError('marketplace bootstrap decision differs from serving release')
        if present(self.market_hold):
            self.check_market_hold(j)
            discard(self.market_hold)

    def start_allowed(self):
        if not self.pending.exists():
            return True
        j = json.loads(secure(self.pending).read_bytes())
        if not self.permit.exists():
            return False
        permit = json.loads(secure(self.permit).read_bytes())
        pid, fd = permit['pid'], permit['fd']
        if (permit['boot'] != boot_id() or permit['txn'] != j['txn'] or
                permit['release'] != self.host.current() or permit['release'] not in (j['old'], j['new']) or
                j['phase'] not in ('starting', 'restoring', 'committed', 'market-final') or
                type(pid) is not int or pid <= 1 or type(fd) is not int or fd < 0):
            return False
        try:
            ticks, group = process_identity(pid)
            proc = Path('/proc', str(pid))
            lock = self.host.state / 'deploy.lock'
            match = os.path.samestat(lock.stat(), (proc / 'fd' / str(fd)).stat())
            info = (proc / 'fdinfo' / str(fd)).read_text()
            return (match and proc.stat().st_uid == 0 and ticks == permit['ticks'] and
                    (group.endswith('/' + self.host.cfg['recovery_unit']) or
                     re.fullmatch(r'/system\.slice/wbozon-deploy-[0-9]+-[0-9]+\.service', group) is not None) and
                    re.search(r'^lock:\s+\d+: FLOCK\s+ADVISORY\s+WRITE\s+' + str(pid) + r'\s', info, re.M) is not None)
        except (FileNotFoundError, ProcessLookupError):
            return False

    def start(self, j, name, guard):
        if j.get('market_bootstrap') and name == j['new'] and 'market_final' not in j:
            self.check_market_hold(j)
        pid = os.getpid()
        ticks, _ = process_identity(pid)
        atomic_json(self.permit, {'txn': j['txn'], 'release': name, 'pid': pid, 'ticks': ticks,
                                 'boot': boot_id(), 'fd': self.host.lock_fd})
        try:
            if not self.start_allowed():
                raise ReleaseError('controller does not hold the systemd startup permit')
            self.host.check_unit()
            command(['/usr/bin/systemctl', 'start', self.host.unit], timeout=100)
            self.ready(j, name, guard)
            if 'schemas' in j:
                self.host.check_schema(j['schemas'])
        finally:
            discard(self.permit)

    def ready(self, j, name, guard):
        # systemd reports the Gunicorn master started before its sync workers
        # have imported the application. Keep ingress fenced during cold start.
        # A recovery may try the committed candidate before restoring the previous
        # release. Reserve the longer wait for forward startup and the fallback.
        deadline = time.monotonic() + (18 if j['phase'] == 'committed' else 180)
        while True:
            try:
                checking = 'running process'
                pid, generation, cgroup = self.host.process(name)
                checking = 'worker startup'
                if not self.host.workers_idle(pid, cgroup):
                    raise ReleaseError('workers not ready')
                checking = 'marked health response'
                self.host.health(j['mark'])
                checking = 'listeners'
                self.host.sockets()
                # systemd may reassign its DynamicUser after a reboot. Revalidate the
                # same pinned directories, lock inodes, epochs and bytes with the new UID.
                checking = 'finance lock and cooldown state'
                guard.uid = process_uid(pid)
                guard.verify('/proc/' + str(pid) + '/root', '/proc/' + str(pid) + '/ns/time')
                checking = 'service invocation'
                if self.host.process(name) == (pid, generation, cgroup):
                    return
            except (OSError, ValueError, ReleaseError):
                pass
            if time.monotonic() >= deadline:
                raise ReleaseError('running release failed readiness at ' + checking)
            time.sleep(.3)

    def drain(self, name, mark, *, require_healthy=False):
        # A timed-out marked loopback probe is safe to repeat. Keep the same
        # Gunicorn master invocation and require Host.drain to prove both workers idle.
        identity = self.host.process(name)
        deadline = time.monotonic() + (60 if require_healthy else 35)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseError('marked drain probe timed out')
            try:
                drained = self.host.drain(name, mark, seconds=remaining, require_healthy=require_healthy)
            except TimeoutError:
                if self.host.process(name) != identity:
                    raise ReleaseError('portal changed during drain')
                time.sleep(.25)
                continue
            if drained != identity:
                raise ReleaseError('portal changed during drain')
            return drained

    def complete(self, j, name, sha):
        if 'market_final' in j and j['market_final'] != name:
            raise ReleaseError('cannot reopen an older marketplace bootstrap target')
        if 'schemas' in j:
            try:
                self.host.check_schema(j['schemas'])
            except Exception:
                self.host.barrier(j['txn'], j['mark'], True)
                raise
        atomic_json(self.active, {'release': name, 'manifest': sha, 'txn': j['txn'],
                                 'previous': j['old'] if name == j['new'] else j['previous']})
        self.host.barrier(j['txn'], j['mark'], False)
        self.host.health()
        discard(self.permit)
        if j.get('market_bootstrap'):
            self.seal_market_decision(j, name)
        discard(self.pending)
        print('Serving verified release', name, flush=True)

    def gate_lock(self, *, allow_uncertain=False):
        return FinanceGuard(self.host.gate, self.host.gate.parent.stat().st_uid, 4, allow_uncertain=allow_uncertain)

    def activate(self, name, sha, check_main):
        h = self.host
        if self.pending.exists():
            raise ReleaseError('previous recovery is pending')
        if present(self.market_hold):
            raise ReleaseError('orphaned marketplace bootstrap hold')
        old, old_sha = self.receipt()
        old_dir = h.verify(old, old_sha)
        candidate = h.verify(name, sha)
        transition = market_transition(old_dir, candidate)
        if transition == 'bootstrap':
            parent = h.gate.parent
            if present(parent / '.market-reports') or any(p.name.startswith('.market-method-gates') for p in parent.iterdir()):
                raise ReleaseError('legacy service already has marketplace report or cooldown state')
        h.check_unit()
        h.settled()
        old_pid, _, _ = h.process(old)
        if h.holder() != process_uid(old_pid):
            raise ReleaseError('DynamicUser holder identity mismatch')
        h.sockets()
        check_main()
        h.reserve(True)
        j = {'txn': secrets.token_hex(8), 'mark': secrets.randbelow(2**30) + 1,
             'old': old, 'old_sha': old_sha, 'new': name, 'new_sha': sha,
             'previous': json.loads(secure(self.active).read_bytes()).get('previous', old) if self.active.exists() else old}
        if transition == 'bootstrap':
            j['market_bootstrap'] = True
        self.phase(j, 'draining')
        if j.get('market_bootstrap'):
            atomic_json(self.market_hold, {field: j[field] for field in ('txn', 'old', 'new')})
        h.barrier(j['txn'], j['mark'], True)
        pid, generation, cg = self.drain(old, j['mark'], require_healthy=True)
        with self.gate_lock(allow_uncertain=transition == 'preserve_blocked_finance') as guard:
            guard.verify('/proc/' + str(pid) + '/root', '/proc/' + str(pid) + '/ns/time')
            if h.sockets() or not h.workers_idle(pid, cg) or h.process(old) != (pid, generation, cg):
                raise ReleaseError('portal changed during drain')
            # Both sync workers have finished their handlers, the fence is closed
            # and finance is locked. A full backup disk must leave this master alive.
            j['schemas'] = h.backup(j['txn'])
            backup_scoped_keys(h, j['txn'])
            self.phase(j, 'stopping')
            h.stop()
            h.release_group(old_dir)
            h.verify(old, old_sha)
            guard.verify()
            check_main()
            self.phase(j, 'switching')
            h.switch(name, j['txn'])
            self.phase(j, 'starting')
            self.start(j, name, guard)
            self.phase(j, 'committed')
        self.complete(j, name, sha)
        h.reserve(False)

    def recover(self):
        if not self.pending.exists():
            return
        j = json.loads(secure(self.pending).read_bytes())
        self.host.reserve(False)
        if 'market_final' in j:
            name = j['market_final']
            if not j.get('market_bootstrap') or name not in (j['old'], j['new']):
                raise ReleaseError('invalid final marketplace bootstrap release')
            sha = j['new_sha'] if name == j['new'] else j['old_sha']
            receipt = json.loads(secure(self.active).read_bytes())
            if (self.host.current() != name or receipt['txn'] != j['txn'] or
                    receipt['release'] != name or receipt['manifest'] != sha):
                raise ReleaseError('final marketplace receipt differs from journal')
            self.reconcile(j, name == j['new'])
            return
        committed = j['phase'] == 'committed'
        if committed:
            try:
                self.reconcile(j, True)
                return
            except Exception:
                # A new version can fail *after* private readiness, even after
                # admission reopened. Fence and drain it again before switching back.
                if not self.pending.exists():
                    raise
                self.host.verify(j['old'], j['old_sha'])
                self.phase(j, 'rolling-back')
        self.reconcile(j, False)

    def reconcile(self, j, committed):
        h = self.host
        name, sha = (j['new'], j['new_sha']) if committed else (j['old'], j['old_sha'])
        if j.get('market_bootstrap') and 'market_final' not in j and h.current() == j['new']:
            self.check_market_hold(j)
        if h.current() not in (j['old'], j['new']):
            raise ReleaseError('unexpected release during recovery')
        release = h.verify(name, sha)
        h.check_unit()
        h.settled()
        props = h.props()
        if h.current() == name and props['ActiveState'] == 'active':
            # In particular, a drain timeout or an interrupted preflight must leave
            # the serving master and any outstanding provider operation alone.
            h.process(name)
            if j['phase'] in ('draining', 'stopping'):
                self.complete(j, name, sha)
                return
            try:
                if j['phase'] == 'restoring' and not committed:
                    # Recovery itself may have been interrupted while the old
                    # workers were booting. Verify the existing process before
                    # considering another stop/start of the very same release.
                    with self.gate_lock(allow_uncertain=True) as guard:
                        self.ready(j, name, guard)
                else:
                    h.health(j['mark'])
            except (OSError, ValueError, ReleaseError):
                pass
            else:
                self.complete(j, name, sha)
                return
        h.barrier(j['txn'], j['mark'], True)
        h.holder()
        if props['ActiveState'] == 'active':
            self.drain(h.current(), j['mark'])
        with self.gate_lock(allow_uncertain=True) as guard:
            if props['ActiveState'] == 'active':
                p, _, _ = self.drain(h.current(), j['mark'])
                guard.verify('/proc/' + str(p) + '/root', '/proc/' + str(p) + '/ns/time')
            if h.props()['ActiveState'] not in ('inactive', 'failed'):
                h.stop()
            if h.props()['MainPID'] != '0':
                raise ReleaseError('unexpected live process during recovery')
            h.release_group(release)
            self.phase(j, 'committed' if committed else 'restoring')
            h.switch(name, j['txn'])
            self.start(j, name, guard)
        self.complete(j, name, sha)


def prune(host, txn):
    # Only delete previously registered CI builds and controller-created snapshots.
    # Always keep the running release, its predecessor and the five most recent entries.
    from native_release import syncdir
    current, _ = txn.receipt()
    keep = {current, host.cfg['baseline']}
    if txn.active.exists():
        keep.add(json.loads(secure(txn.active).read_bytes()).get('previous'))
    registry = host.state / 'managed'
    registry.mkdir(mode=0o700, exist_ok=True)
    receipts = sorted(registry.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for receipt in receipts[5:]:
        name = receipt.stem
        if name in keep:
            continue
        if (not re.fullmatch(r'[0-9a-f]{40}-[1-9][0-9]{0,19}-[1-9][0-9]{0,4}', name) or
                json.loads(secure(receipt).read_bytes()) != {'release': name}):
            raise ReleaseError('invalid managed build receipt')
        for parent in (host.releases, build_release.CANDIDATES):
            path = parent / name
            if path.exists():
                shutil.rmtree(secure(path, True))
                syncdir(parent)
        discard(receipt)
    snapshots = []
    directory = host.state / 'backups'
    for entry in directory.iterdir():
        if re.fullmatch('[0-9a-f]{16}', entry.name) and (entry / 'cd-snapshot.json').is_file():
            if json.loads(secure(entry / 'cd-snapshot.json').read_bytes()) == {'txn': entry.name}:
                snapshots.append(entry)
    for path in sorted(snapshots, key=lambda p: p.stat().st_mtime_ns, reverse=True)[5:]:
        shutil.rmtree(secure(path, True))
        syncdir(directory)


def deploy(host, txn, request_file):
    path = secure(request_file)
    if path.parent != Path('/run/wbozon-cd') or path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
        raise ReleaseError('invalid private deployment envelope')
    try:
        request = github_artifact._json(path.read_bytes())
    finally:
        path.unlink()
    client = github_artifact.GitHub(request.pop('token'))
    attempt = request.pop('attempt')
    if not isinstance(attempt, str) or not re.fullmatch('[1-9][0-9]{0,4}', attempt):
        raise ReleaseError('invalid deployment attempt')
    if txn.pending.exists():
        txn.recover()
    files = github_artifact.verify_request(client, request)
    commit, run = request['commit'], request['run_id']
    def check_main():
        if client.json('/git/ref/heads/main').get('object', {}).get('sha') != commit:
            raise ReleaseError('a newer commit is on main')
    name = commit + '-' + run + '-' + attempt
    active, active_sha = txn.receipt()
    if active == name:
        host.verify(active, active_sha)
        host.process(active)
        host.health()
        return
    prune(host, txn)
    for directory in (host.state, host.releases):
        if shutil.disk_usage(directory).free < 512 * 1024**2:
            raise ReleaseError('insufficient free space for another build')
    print('Verified GitHub artifact', request['artifact_id'], flush=True)
    release = host.releases / name
    receipt = host.state / 'managed' / (name + '.json')
    if release.exists() or (build_release.CANDIDATES / name).exists() or receipt.exists():
        raise ReleaseError('build attempt already exists; use a fresh workflow attempt')
    atomic_json(receipt, {'release': name})
    sha = build_release.prepare(files, release, build_release.CANDIDATES / name,
                                run_id=run, attempt=attempt, release_gid=host.group)
    shutil.rmtree(secure(build_release.CANDIDATES / name, True))
    txn.activate(name, sha, check_main)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('action', choices=('recover', 'start-check', 'deploy'))
    parser.add_argument('request_file', nargs='?')
    args = parser.parse_args()
    host = Host(args.config)
    txn = Transaction(host)
    if args.action == 'start-check':
        return 0 if txn.start_allowed() else 1
    os.umask(0o077)
    try:
        with host.locked():
            if args.action == 'recover':
                txn.recover()
            else:
                try:
                    deploy(host, txn, args.request_file)
                except Exception:
                    txn.recover()
                    raise
    except BlockingIOError:
        if args.action != 'recover':
            raise
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        safe_types = (ReleaseError, build_release.BuildError, github_artifact.VerificationError)
        detail = str(exc) if isinstance(exc, safe_types) else type(exc).__name__
        print('Deployment did not complete:', detail, file=sys.stderr, flush=True)
        sys.exit(1)
