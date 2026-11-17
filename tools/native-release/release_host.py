"""Linux operations used by the single-portal deployment transaction."""
import ast
import contextlib
import fcntl
import grp
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time

import build_release
from native_release import secure, syncdir


class ReleaseError(Exception):
    pass


def command(args, *, data=None, timeout=15):
    result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'})
    if result.returncode:
        raise ReleaseError(Path(args[0]).name + ' failed (' + str(result.returncode) + ')')
    return result.stdout


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.cd-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        syncdir(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def discard(path):
    path.unlink(missing_ok=True)
    syncdir(path.parent)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def process_identity(pid):
    proc = Path('/proc', str(pid))
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[19], (proc / 'cgroup').read_text().strip().split('::')[-1]


def unit_properties(unit, names):
    raw = command(['/usr/bin/systemctl', 'show', unit, *['--property=' + n for n in names]])
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def compatible(old, new):
    """An automatic rollback must be able to read the new DBs and finance state."""
    def source(directory, file):
        return (directory / 'service' / file).read_text(encoding='utf-8')

    for file in ('finance_gate.py', 'app.py', 'external_keys.py', 'oauth_mcp.py', 'gunicorn.conf.py'):
        if source(old, file) != source(new, file):
            raise ReleaseError('storage, gate or worker contract changed: ' + file)

    def contract(directory):
        nodes = {}
        for node in ast.parse(source(directory, 'portal.py')).body:
            name = getattr(node, 'name', '')
            if name in ('Vault', 'create_portal', 'provider_read'):
                nodes[name] = ast.dump(node)
            if name == 'Portal':
                for sub in node.body:
                    if getattr(sub, 'name', '') in ('__init__', 'read_gateway'):
                        nodes['Portal.' + sub.name] = ast.dump(sub)
        return nodes
    first = contract(old)
    if len(first) != 5 or first != contract(new):
        raise ReleaseError('startup, storage or finance gate contract changed')


class Host:
    def __init__(self, config):
        self.cfg = json.loads(secure(config).read_bytes())
        self.root = secure(self.cfg['root'], True)
        self.releases = secure(self.root / 'releases', True)
        self.state = secure(self.cfg['state'], True)
        if self.state.stat().st_mode & 0o077:
            raise ReleaseError('deployer state is not private')
        self.unit = self.cfg['unit']
        self.port = self.cfg['port']
        self.addresses = [str(ipaddress.IPv4Address(a)) for a in self.cfg['addresses']]
        if (not 1 <= self.port <= 65535 or not self.addresses or self.addresses[0] != '127.0.0.1' or
                not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', self.unit)):
            raise ReleaseError('invalid portal configuration')
        self.gate = Path(self.cfg['gate'])
        self.group = grp.getgrnam(self.cfg['release_group']).gr_gid

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.state / 'deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = fd
            yield
        finally:
            self.lock_fd = None
            os.close(fd)

    def current(self):
        link = self.root / 'current'
        if not link.is_symlink() or link.lstat().st_uid:
            raise ReleaseError('untrusted current link')
        target = link.resolve(strict=True)
        if target.parent != self.releases:
            raise ReleaseError('current outside releases')
        return target.name

    def verify(self, name, sha):
        if not re.fullmatch(r'[0-9a-f]{40}(?:-[1-9][0-9]{0,19}-[1-9][0-9]{0,4})?', name):
            raise ReleaseError('invalid release name')
        release = secure(self.releases / name, True)
        external = name == self.cfg['baseline']
        manifest = (self.state / 'baselines' / (name + '.json')) if external else release / 'MANIFEST.json'
        raw = secure(manifest).read_bytes()
        if digest(raw) != sha:
            raise ReleaseError('unexpected release manifest')
        inventory = build_release.inventory(release)
        if not external:
            inventory.pop('MANIFEST.json')
        if inventory != json.loads(raw):
            raise ReleaseError('release content differs from manifest')
        return release

    def release_group(self, release):
        # Rollback must still be able to execute the legacy release after DynamicUser recycling.
        for p in [release, *release.rglob('*')]:
            if p.lstat().st_gid != self.group:
                os.chown(p, 0, self.group, follow_symlinks=False)

    def switch(self, name, txn):
        temp = self.root / ('.cd-link-' + txn)
        if temp.is_symlink() and temp.lstat().st_uid == 0:
            temp.unlink()
        temp.symlink_to(self.releases / name)
        os.replace(temp, self.root / 'current')
        syncdir(self.root)

    def check_unit(self):
        unit_text = command(['/usr/bin/systemctl', 'cat', '--no-pager', self.unit])
        if digest(unit_text.encode()) != self.cfg['unit_sha256']:
            raise ReleaseError('systemd unit changed')
        props = unit_properties(self.unit, ['NeedDaemonReload', 'KillMode', 'DynamicUser', 'PrivateNetwork',
                               'PartOf', 'BoundBy', 'RequiredBy', 'ConsistsOf', 'PropagatesStopTo', 'TriggeredBy'])
        if (props.pop('NeedDaemonReload') != 'no' or props.pop('KillMode') != 'mixed' or
                props.pop('DynamicUser') != 'yes' or props.pop('PrivateNetwork') != 'no' or any(props.values())):
            raise ReleaseError('unexpected loaded unit or stop propagation')
        for name, sha in self.cfg.get('hooks', {}).items():
            if digest(secure(name).read_bytes()) != sha:
                raise ReleaseError('unit start hook changed')
        dependencies = command(['/usr/bin/systemctl', 'list-dependencies', '--plain', '--all', '--no-pager',
                                '--no-legend', self.unit])
        if any(line.strip() in ('docker.service', 'containerd.service', 'docker.socket')
               for line in dependencies.splitlines()):
            raise ReleaseError('unexpected container activation dependency')
        for unit in self.cfg['dependencies']:
            if unit_properties(unit, ['ActiveState'])['ActiveState'] != 'active':
                raise ReleaseError('required network service unavailable')

    def props(self):
        return unit_properties(self.unit, ['ActiveState', 'MainPID', 'ControlGroup', 'InvocationID', 'SubState'])

    def settled(self, seconds=95):
        end = time.monotonic() + seconds
        while command(['/usr/bin/systemctl', 'list-jobs', '--no-legend', '--no-pager', self.unit]).strip():
            if time.monotonic() >= end:
                raise ReleaseError('systemd job has not settled')
            time.sleep(.25)

    def process(self, release, *, environment=True):
        props = self.props()
        pid = int(props['MainPID'])
        if props['ActiveState'] != 'active' or pid <= 1 or not props['ControlGroup']:
            raise ReleaseError('portal is not running')
        proc = Path('/proc', str(pid))
        if (proc / 'cwd').resolve(strict=True) != self.releases / release / 'service':
            raise ReleaseError('running process and current release differ')
        if environment:
            env = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
            auth, vault = (Path(os.fsdecode(env[k])) for k in (b'DATABASE_PATH', b'VAULT_PATH'))
            gate = Path(os.fsdecode(env[b'OZON_FINANCE_GATE_DIR'])) if b'OZON_FINANCE_GATE_DIR' in env else vault.parent / '.ozon-finance-gates'
            if [str(auth), str(vault)] != self.cfg['databases'] or gate != self.gate:
                raise ReleaseError('running storage paths differ from the adopted paths')
        if os.stat(proc / 'ns/net').st_ino != os.stat('/proc/self/ns/net').st_ino:
            raise ReleaseError('unexpected service network namespace')
        return pid, props['InvocationID'], props['ControlGroup']

    def workers_idle(self, pid, cgroup):
        # An outbound worker may outlive its HTTP peer. Require just the Gunicorn master
        # and its two direct workers, with no provider subprocess anywhere in the cgroup.
        tree = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
        members = set()
        for procfile in tree.rglob('cgroup.procs'):
            members.update(int(v) for v in procfile.read_text().split())
        if len(members) != 3 or pid not in members:
            return False
        try:
            for worker in members - {pid}:
                proc = Path('/proc', str(worker))
                fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[1]) != pid or b'--provider-worker' in (proc / 'cmdline').read_bytes().split(b'\0'):
                    return False
        except FileNotFoundError:
            return False
        return True

    def sockets(self):
        rows = command(['/usr/bin/ss', '-Htan', 'sport = :' + str(self.port)]).splitlines()
        listeners, connections = set(), 0
        for row in rows:
            state, recvq, _, local, *_ = row.split()
            if state == 'LISTEN':
                listeners.add(local)
                connections += int(recvq)
            elif state != 'TIME-WAIT':
                connections += 1
        if listeners != {a + ':' + str(self.port) for a in self.addresses}:
            raise ReleaseError('unexpected portal listeners')
        return connections

    def drain(self, name, seconds=35):
        pid, generation, cgroup = self.process(name)
        end, stable = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            if not self.sockets() and self.workers_idle(pid, cgroup):
                stable += 1
                if stable >= 2 and self.process(name) == (pid, generation, cgroup):
                    return pid, generation, cgroup
            else:
                stable = 0
            time.sleep(.25)
        raise ReleaseError('requests still active at drain deadline')

    def health(self, mark=0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            if mark:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark.to_bytes(4, os.sys.byteorder))
            sock.connect((self.addresses[0], self.port))
            sock.sendall(('GET /healthz HTTP/1.1\r\nHost: ' + self.cfg['host'] +
                          '\r\nConnection: close\r\n\r\n').encode('ascii'))
            response = http.client.HTTPResponse(sock)
            response.begin()
            if response.status != 200 or json.loads(response.read(256)) != {'status': 'ok'}:
                raise ReleaseError('portal health response failed')
        finally:
            sock.close()

    @staticmethod
    def barrier_exists(table):
        if not re.fullmatch('wbozon_cd_[0-9a-f]{16}', table):
            raise ReleaseError('invalid ingress table')
        return ('table inet ' + table) in command(['/usr/sbin/nft', 'list', 'tables']).splitlines()

    def barrier(self, txn, mark, enable):
        table = 'wbozon_cd_' + txn
        if self.barrier_exists(table) == enable:
            return
        if enable:
            # Match initial SYN even when local or bridge traffic is untracked. A forwarded
            # marked health SYN retains its socket mark on output, including the loopback input.
            addresses = '{ ' + ', '.join(self.addresses) + ' }'
            rules = ['add table inet ' + table]
            for hook in ('input', 'output'):
                chain = 'inet ' + table + ' ' + hook
                rules.append('add chain ' + chain + ' { type filter hook ' + hook + ' priority -5; policy accept; }')
                rules.append('add rule ' + chain + ' ip daddr ' + addresses + ' tcp dport ' + str(self.port) +
                             ' tcp flags & (syn | ack) == syn meta mark != ' + str(mark) + ' reject with tcp reset')
        else:
            rules = ['delete table inet ' + table]
        command(['/usr/sbin/nft', '-f', '-'], data='\n'.join(rules) + '\n')
        if self.barrier_exists(table) != enable:
            raise ReleaseError('unexpected ingress barrier state')

    def stop(self):
        command(['/usr/bin/systemctl', 'stop', self.unit], timeout=75)
        self.settled()
        props = self.props()
        if props['ActiveState'] != 'inactive' or int(props['MainPID']):
            raise ReleaseError('portal has not stopped')

    def holder(self):
        unit = self.cfg['holder']
        if unit_properties(unit, ['ActiveState'])['ActiveState'] != 'active':
            command(['/usr/bin/systemd-run', '--quiet', '--collect', '--unit=' + unit,
                     '--property=DynamicUser=yes', '--property=User=' + self.cfg['user'],
                     '--property=RuntimeMaxSec=600', '/usr/bin/sleep', '590'])
        hp = int(unit_properties(unit, ['MainPID'])['MainPID'])
        if hp <= 1:
            raise ReleaseError('service identity holder unavailable')
        return Path('/proc', str(hp)).stat().st_uid

    def backup(self, txn):
        target = self.state / 'backups' / txn
        target.mkdir(mode=0o700)
        for file, required in zip(self.cfg['databases'], (('tokens', 'sessions'), ('vault', 'connection_checks'))):
            p = Path(file)
            info = p.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.gate.parent.stat().st_uid:
                raise ReleaseError('unexpected application database')
            with contextlib.closing(sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=3)) as src:
                src.execute('PRAGMA trusted_schema=OFF')
                db = target / p.name
                with contextlib.closing(sqlite3.connect(db)) as dst:
                    src.backup(dst)
                    dst.execute('PRAGMA trusted_schema=OFF')
                    tables = {v[0] for v in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not set(required) <= tables or dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ReleaseError('database snapshot verification failed')
                with db.open('rb') as stream:
                    os.fsync(stream.fileno())
        syncdir(target)
        syncdir(target.parent)
