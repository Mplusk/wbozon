#!/usr/bin/env python3
"""Opt-in, single-service deployment. No daemon, timer or remote execution.

This control file must be installed and reviewed independently of image releases.
Production secrets stay in a protected runtime env file, never in CI.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

NAME = '3dprinta-auth'
LABEL = 'io.sidzen.service'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
BACKUP = re.compile(r'3dprinta-auth-(?:previous|failed)-[0-9a-f]{16}\Z')
PROBE = "import urllib.request; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/',headers={'Host':'3dprinta.shop'}),timeout=2); assert r.status==200"


class DeployError(Exception):
    pass


class UncertainDockerOutcome(DeployError):
    """Daemon may still finish the command. No automatic compensating mutations."""


def checked_path(value, kind, owner=None):
    p = Path(value)
    if any(c in value for c in ('\x00', '\n', '\r', ',')) or not p.is_absolute() or p == Path('/') or p.is_symlink():
        raise DeployError('Runtime paths must be absolute, dedicated and not symlinks')
    if not p.exists() or (kind == 'file' and not p.is_file()) or (kind == 'dir' and not p.is_dir()):
        raise DeployError('A configured runtime path does not exist or has the wrong type')
    # No symlink component may redirect the deployer into another project.
    if any(x.is_symlink() for x in (p, *p.parents)):
        raise DeployError('Symlink runtime paths are not allowed')
    for parent in p.parents:
        mode = parent.stat()
        if mode.st_uid not in (0, os.geteuid()) or stat.S_IMODE(mode.st_mode) & 0o022:
            raise DeployError('Parent directories must be controlled by the trusted operator and not writable by others')
    mode = p.stat()
    if mode.st_uid != (os.geteuid() if owner is None else owner) or stat.S_IMODE(mode.st_mode) & 0o077:
        raise DeployError('Config, state and secrets must be owned by the deploy identity and private')
    return p


def load_config(path):
    path = checked_path(path, 'file')
    try:
        c = json.loads(path.read_text())
    except (ValueError, OSError):
        raise DeployError('Invalid configuration') from None
    if set(c) != {'image_repository', 'state_dir', 'env_file', 'data_dir', 'port', 'network'}:
        raise DeployError('Unexpected or missing configuration keys')
    if not isinstance(c['image_repository'], str) or not re.fullmatch(r'ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_./-]*', c['image_repository']):
        raise DeployError('Only an explicit GHCR image repository is supported')
    if '..' in c['image_repository'] or c['image_repository'].endswith('/'):
        raise DeployError('Invalid image repository')
    if type(c['port']) is not int or not 1024 <= c['port'] <= 65535:
        raise DeployError('Invalid loopback port')
    if c['network'] != '3dprinta-auth-backend':
        raise DeployError('Only the separately provisioned dedicated network is allowed')
    for k, kind in [('state_dir', 'dir'), ('env_file', 'file'), ('data_dir', 'dir')]:
        if not isinstance(c[k], str):
            raise DeployError('Invalid runtime path')
        checked_path(c[k], kind, owner=10001 if k == 'data_dir' else None)
    paths = [Path(c[k]).resolve() for k in ('state_dir', 'env_file', 'data_dir')]
    if len(set(paths)) != 3 or paths[0] in paths[2].parents or paths[2] in paths[0].parents:
        raise DeployError('State and application data must be separate')
    data = paths[2]
    for protected in (path.resolve(), paths[0], paths[1]):
        if protected == data or data in protected.parents:
            raise DeployError('Config, state and secrets cannot be inside application data')
    return c


class Docker:
    def call(self, args, timeout=60):
        try:
            r = subprocess.run(['docker', *args], capture_output=True, text=True,
                               timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            # The command may have taken effect. The caller must inspect/recover,
            # never blindly repeat a timed-out mutation.
            raise UncertainDockerOutcome('Docker command outcome is uncertain; inspect before recovery') from None
        if r.returncode:
            raise DeployError('Docker command failed; provider output suppressed')
        return r.stdout

    def inspect(self, name):
        names = self.call(['ps', '-a', '--format', '{{.Names}}']).splitlines()
        if name not in names:
            return None
        raw = self.call(['inspect', '--format', '{"owner":{{json (index .Config.Labels "io.sidzen.service")}},"image":{{json .Config.Image}},"running":{{json .State.Running}}}', name])
        obj = json.loads(raw)
        return {'owned': obj.get('owner') == NAME, 'image': obj.get('image'),
                'running': obj.get('running', False)}

    def health(self, name):
        for _ in range(20):
            try:
                self.call(['exec', name, 'python', '-c', PROBE], timeout=5)
                return True
            except DeployError:
                time.sleep(1)
        return False


class Release:
    def __init__(self, config, docker):
        self.c, self.d = config, docker
        self.path = Path(config['state_dir']) / 'state.json'
        self.uncertain_path = Path(config['state_dir']) / 'uncertain.json'

    def mark_uncertain(self, stage, candidate=None):
        # O_EXCL: never overwrite unresolved evidence from an earlier failure.
        fd = os.open(self.uncertain_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'stage': stage, 'candidate': candidate}, f)
            f.flush(); os.fsync(f.fileno())

    def state(self):
        if not self.path.exists():
            return {'active': None, 'previous': None, 'pending': None}
        if self.path.is_symlink():
            raise DeployError('State file must not be a symlink')
        value = json.loads(self.path.read_text())
        if set(value) != {'active', 'previous', 'pending'}:
            raise DeployError('Invalid deployment state')
        if value['previous'] is not None and not BACKUP.fullmatch(value['previous']):
            raise DeployError('Invalid previous-container identity')
        return value

    def save(self, value):
        temp = self.path.with_name('.state-' + secrets.token_hex(8))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(value, f); f.flush(); os.fsync(f.fileno())
            os.replace(temp, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally:
            if temp.exists(): temp.unlink()

    def own(self, name, required=False):
        if name != NAME and not BACKUP.fullmatch(name):
            raise DeployError('Refusing an unrelated container name')
        obj = self.d.inspect(name)
        if obj is not None and not obj['owned']:
            raise DeployError('Refusing an existing container not owned by this service')
        if required and obj is None:
            raise DeployError('Expected service container is missing')
        return obj

    def candidate(self, image):
        candidate = NAME + '-candidate-' + secrets.token_hex(8)
        uncertain = False
        try:
            self.d.call(['run', '-d', '--name', candidate, '--label', LABEL + '=' + NAME,
                         '--network', 'none', '--read-only', '--cap-drop=ALL',
                         '--security-opt=no-new-privileges', '--memory=128m', '--cpus=.5',
                         '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                         '--tmpfs', '/data:rw,noexec,nosuid,size=16m,uid=10001,gid=10001',
                         '-e', 'BASE_URL=https://3dprinta.shop', '-e', 'DATABASE_PATH=/data/test.sqlite',
                         '-e', 'SESSION_SECRET=' + secrets.token_hex(32), image])
            if not self.d.health(candidate):
                raise DeployError('Candidate failed readiness; current service is unchanged')
        except UncertainDockerOutcome:
            uncertain = True
            self.mark_uncertain('candidate', candidate)
            raise
        finally:
            if not uncertain:
                try:
                    obj = self.d.inspect(candidate)
                    if obj is not None:
                        if not obj['owned']: raise DeployError('Candidate identity mismatch')
                        self.d.call(['rm', '-f', candidate])
                except UncertainDockerOutcome:
                    self.mark_uncertain('candidate-cleanup', candidate)
                    raise

    def deploy(self, digest):
        if not DIGEST.fullmatch(digest):
            raise DeployError('A sha256 image digest is required, not a tag')
        if self.uncertain_path.exists():
            raise DeployError('Uncertain Docker outcome requires independent inspection before any mutation')
        s = self.state()
        if s['pending'] is not None:
            raise DeployError('An interrupted deployment exists; run recover first')
        image = self.c['image_repository'] + '@' + digest
        current = self.own(NAME)
        if current and current['image'] == image:
            if not self.d.health(NAME): raise DeployError('Current image is unhealthy')
            return 'already_active'
        # Refuse a managed service not represented by this control state.
        if current and s['active'] != current['image']:
            raise DeployError('Current container and deployment state disagree')
        if not current and s['active'] is not None:
            raise DeployError('Active state has no container; recover explicitly')
        self.d.call(['network', 'inspect', self.c['network']])
        try:
            self.d.call(['pull', image], timeout=300)
        except UncertainDockerOutcome:
            self.mark_uncertain('image-pull')
            raise
        self.candidate(image)
        backup = NAME + '-previous-' + secrets.token_hex(8) if current else None
        s['pending'] = {'target': image, 'backup': backup, 'before': s['active']}
        self.save(s)
        try:
            if current:
                self.d.call(['stop', '--time', '20', NAME])
                self.d.call(['rename', NAME, backup])
            self.d.call(['run', '-d', '--name', NAME, '--label', LABEL + '=' + NAME,
                         '--restart', 'unless-stopped', '--network', self.c['network'],
                         '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                         '--memory=192m', '--cpus=.5', '--pids-limit=100',
                         '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                         '--mount', 'type=bind,src=' + self.c['data_dir'] + ',dst=/data',
                         '--env-file', self.c['env_file'], '-e', 'BASE_URL=https://3dprinta.shop',
                         '-e', 'DATABASE_PATH=/data/auth.sqlite',
                         '-p', '127.0.0.1:' + str(self.c['port']) + ':8000', image])
            if not self.d.health(NAME):
                raise DeployError('New runtime failed readiness')
            self.save({'active': image, 'previous': backup, 'pending': None})
            return 'deployed'
        except UncertainDockerOutcome:
            self.mark_uncertain('activation')
            raise
        except DeployError:
            self.recover()
            raise DeployError('Deployment failed; prior state was restored. Inspect before retrying') from None

    def recover(self):
        if self.uncertain_path.exists():
            raise DeployError('Verify Docker command has settled and reconcile uncertain evidence before recovery')
        s = self.state(); p = s['pending']
        if p is None: return 'nothing_pending'
        if set(p) != {'target', 'backup', 'before'} or (p['backup'] is not None and not BACKUP.fullmatch(p['backup'])):
            raise DeployError('Invalid pending state')
        current = self.own(NAME)
        backup = self.own(p['backup'], required=False) if p['backup'] else None
        # A crash before rename can leave the prior service at its original name.
        if current and current['image'] == p['before'] and backup is None:
            self.d.call(['start', NAME])
        else:
            if current:
                if current['image'] != p['target']:
                    raise DeployError('Unexpected active image; refusing recovery')
                self.d.call(['stop', '--time', '20', NAME])
                self.d.call(['rename', NAME, NAME + '-failed-' + secrets.token_hex(8)])
            if p['before'] is not None:
                if not backup or backup['image'] != p['before']:
                    raise DeployError('Prior container unavailable; manual recovery required')
                self.d.call(['rename', p['backup'], NAME])
                self.d.call(['start', NAME])
        if p['before'] is not None and not self.d.health(NAME):
            raise DeployError('Prior runtime restored but not healthy; inspect manually')
        self.save({'active': p['before'], 'previous': s['previous'], 'pending': None})
        return 'recovered'

    def rollback(self):
        s = self.state()
        if s['pending'] is not None: raise DeployError('Recover pending deployment first')
        if s['previous'] is None: raise DeployError('No previous release is recorded')
        previous = self.own(s['previous'], required=True)
        current = self.own(NAME, required=True)
        if current['image'] != s['active']:
            raise DeployError('Current state mismatch')
        # Reuse verified activation/recovery logic with the previous digest.
        image = previous['image']; prefix = self.c['image_repository'] + '@'
        if not image.startswith(prefix): raise DeployError('Previous image repository mismatch')
        return self.deploy(image[len(prefix):])


@contextlib.contextmanager
def locked(config):
    path = Path(config['state_dir']) / 'deploy.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise DeployError('Another deployment is running') from None
        yield
    finally: os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--execute', action='store_true', help='Explicitly allow Docker mutations')
    p.add_argument('action', choices=['check', 'deploy', 'rollback', 'recover'])
    p.add_argument('--digest')
    a = p.parse_args()
    engine = None
    try:
        c = load_config(a.config)
        if a.action == 'check':
            print('Configuration valid; no Docker or network operations performed'); return
        if not a.execute: raise DeployError('Mutations require --execute; nothing performed')
        with locked(c):
            engine = Release(c, Docker())
            if a.action == 'deploy':
                if not a.digest: raise DeployError('--digest is required')
                result = engine.deploy(a.digest)
            else: result = getattr(engine, a.action)()
        print(result)
    except UncertainDockerOutcome:
        if engine is not None and not engine.uncertain_path.exists():
            engine.mark_uncertain('command')
        print('Docker outcome uncertain. No automatic recovery. Verify the command has settled and reconcile protected state before continuing.')
        raise SystemExit(2)
    except (DeployError, OSError, ValueError, TypeError, KeyError):
        # Never print Docker inspect, environment contents or raw provider errors.
        print('Operation failed. Inspect protected deployment state and service health; no secret details printed.')
        raise SystemExit(1)


if __name__ == '__main__': main()
