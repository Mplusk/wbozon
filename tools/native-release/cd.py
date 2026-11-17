"""Verified native deployment, durable start interlock and crash recovery."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

# Invoked with python -I. Only the administrator-installed module directory is trusted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release
import github_artifact
from finance_guard import FinanceGuard
from native_release import secure
from release_host import Host, ReleaseError, atomic_json, boot_id, command, compatible, discard, process_identity

CONFIG = '/etc/wbozon-cd.json'


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
                j['phase'] not in ('starting', 'restoring', 'committed') or
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
        finally:
            discard(self.permit)

    def ready(self, j, name, guard):
        deadline = time.monotonic() + 18
        while True:
            try:
                pid, generation, cgroup = self.host.process(name)
                if not self.host.workers_idle(pid, cgroup):
                    raise ReleaseError('workers not ready')
                self.host.health(j['mark'])
                self.host.sockets()
                # systemd may reassign its DynamicUser after a reboot. Revalidate the
                # same pinned directories, lock inodes, epochs and bytes with the new UID.
                guard.uid = Path('/proc', str(pid)).stat().st_uid
                guard.verify('/proc/' + str(pid) + '/root', '/proc/' + str(pid) + '/ns/time')
                if self.host.process(name) == (pid, generation, cgroup):
                    return
            except (OSError, ValueError, ReleaseError):
                pass
            if time.monotonic() >= deadline:
                raise ReleaseError('running release failed readiness')
            time.sleep(.3)

    def complete(self, j, name, sha):
        atomic_json(self.active, {'release': name, 'manifest': sha, 'txn': j['txn']})
        self.host.barrier(j['txn'], j['mark'], False)
        self.host.health()
        discard(self.permit)
        discard(self.pending)
        print('Serving verified release', name, flush=True)

    def gate_lock(self, recovery=False):
        return FinanceGuard(self.host.gate, self.host.gate.parent.stat().st_uid, 4, allow_uncertain=recovery)

    def activate(self, name, sha, check_main):
        h = self.host
        if self.pending.exists():
            raise ReleaseError('previous recovery is pending')
        old, old_sha = self.receipt()
        old_dir = h.verify(old, old_sha)
        candidate = h.verify(name, sha)
        compatible(old_dir, candidate)
        h.check_unit()
        h.settled()
        old_pid, _, _ = h.process(old)
        if h.holder() != Path('/proc', str(old_pid)).stat().st_uid:
            raise ReleaseError('DynamicUser holder identity mismatch')
        h.health()
        h.sockets()
        check_main()
        j = {'txn': secrets.token_hex(8), 'mark': secrets.randbelow(2**30) + 1,
             'old': old, 'old_sha': old_sha, 'new': name, 'new_sha': sha}
        self.phase(j, 'draining')
        h.barrier(j['txn'], j['mark'], True)
        pid, generation, cg = h.drain(old)
        with self.gate_lock() as guard:
            guard.verify('/proc/' + str(pid) + '/root', '/proc/' + str(pid) + '/ns/time')
            if h.sockets() or not h.workers_idle(pid, cg) or h.process(old) != (pid, generation, cg):
                raise ReleaseError('portal changed during drain')
            self.phase(j, 'stopping')
            h.stop()
            h.release_group(old_dir)
            h.verify(old, old_sha)
            h.backup(j['txn'])
            guard.verify()
            check_main()
            self.phase(j, 'switching')
            h.switch(name, j['txn'])
            self.phase(j, 'starting')
            self.start(j, name, guard)
            self.phase(j, 'committed')
        self.complete(j, name, sha)

    def recover(self):
        if not self.pending.exists():
            return
        h = self.host
        j = json.loads(secure(self.pending).read_bytes())
        committed = j['phase'] == 'committed'
        name, sha = (j['new'], j['new_sha']) if committed else (j['old'], j['old_sha'])
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
                h.health(j['mark'])
            except (OSError, ValueError, ReleaseError):
                pass
            else:
                self.complete(j, name, sha)
                return
        h.barrier(j['txn'], j['mark'], True)
        h.holder()
        if props['ActiveState'] == 'active':
            h.drain(h.current())
        with self.gate_lock(recovery=True) as guard:
            if props['ActiveState'] == 'active':
                p, _, _ = h.drain(h.current())
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
    print('Verified GitHub artifact', request['artifact_id'], flush=True)
    release = host.releases / name
    sha = build_release.prepare(files, release, build_release.CANDIDATES / name,
                                run_id=run, attempt=attempt, release_gid=host.group)
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
