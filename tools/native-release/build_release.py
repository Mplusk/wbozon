"""Prepare and test a verified native release without production credentials."""
import hashlib
import grp
import json
import os
import pathlib
import re
import stat
import subprocess
import github_artifact

RELEASES = pathlib.Path('/opt/3dprinta-portal/releases')
CANDIDATES = pathlib.Path('/var/lib/3dprinta-native-deployer/candidates')


class BuildError(Exception):
    pass


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _secure_directory(path, *, private=False):
    path = pathlib.Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise BuildError('invalid trusted directory')
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise BuildError('untrusted candidate parent')
    if private and path.stat().st_mode & 0o077:
        raise BuildError('candidate work directory must be private')


def _validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 500:
        raise BuildError('invalid candidate inventory')
    allowed = {'PROVENANCE.json', 'requirements-offline.lock', 'requirements-bootstrap.lock', 'test_native.py'}
    total = 0
    for name, data in files.items():
        if (not isinstance(name, str) or not isinstance(data, bytes) or
                not re.fullmatch('[A-Za-z0-9_./-]+', name) or
                any(part in ('', '.', '..') for part in name.split('/')) or
                not (name in allowed or name.startswith('service/') or
                     re.fullmatch(r'wheelhouse/[A-Za-z0-9_.-]+\.whl', name) or
                     re.fullmatch(r'bootstrap/[A-Za-z0-9_.-]+\.whl', name))):
            raise BuildError('unsafe candidate path')
        total += len(data)
    if total > 50_000_000 or not {'service/portal.py', 'service/gunicorn.conf.py',
                                 'test_native.py', 'requirements-offline.lock'} <= set(files):
        raise BuildError('incomplete candidate inventory')


def inventory(release):
    """Refuse links, special files, hardlinks and writable release content."""
    release = pathlib.Path(release)
    result = {}
    for path in sorted(release.rglob('*')):
        info = path.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022 or
                not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise BuildError('untrusted candidate file')
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise BuildError('candidate hardlink')
            result[path.relative_to(release).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _sync_tree(release):
    paths = [release, *release.rglob('*')]
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        info = path.lstat()
        if info.st_uid != 0 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise BuildError('untrusted candidate while syncing')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if stat.S_ISDIR(info.st_mode):
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _command(argv, *, cwd=None, timeout=120, stage='candidate preparation'):
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise BuildError(f'{stage} exceeded its {timeout}-second deadline') from None
    except OSError:
        raise BuildError(f'{stage} could not start') from None
    if result.returncode:
        raise BuildError(f'{stage} failed with exit code {result.returncode}')


def _sandbox(release, unit, args, cwd, *, runtime_seconds):
    properties = ['--property=User=wbozon-test', '--property=Group=wbozon-test', '--property=ProtectProc=invisible',
                  '--property=PrivateNetwork=yes', '--property=PrivateIPC=yes', '--property=PrivateDevices=yes',
                  '--property=ProtectSystem=strict', '--property=ProtectHome=yes', '--property=NoNewPrivileges=yes',
                  '--property=ProtectKernelTunables=yes', '--property=ProtectControlGroups=yes',
                  '--property=RestrictSUIDSGID=yes', '--property=TemporaryFileSystem=/run:ro',
                  '--property=TemporaryFileSystem=/opt:ro', '--property=TemporaryFileSystem=/var:ro',
                  '--property=TemporaryFileSystem=/tmp:rw,nosuid,nodev,noexec,size=64M,mode=1777',
                  '--property=TemporaryFileSystem=/var/tmp:rw,nosuid,nodev,noexec,size=64M,mode=1777',
                  '--property=BindReadOnlyPaths=' + str(release) + ':' + str(release),
                  '--property=MemoryMax=512M', '--property=CPUQuota=150%', '--property=TasksMax=100',
                  '--property=CPUWeight=20', '--property=IOWeight=20',
                  '--property=RuntimeMaxSec=' + str(runtime_seconds)]
    clean = ['/usr/bin/env', '-i', 'HOME=/tmp', 'PATH=/usr/bin:/bin', 'LC_ALL=C.UTF-8',
             'PYTHONDONTWRITEBYTECODE=1']
    _command(['/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + unit,
              '--working-directory=' + str(cwd), *properties, *clean, *args], timeout=runtime_seconds + 30,
             stage='isolated test unit ' + unit)
    try:
        status = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--property=LoadState', '--value'],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError('isolated test unit outcome unknown') from None
    if status.returncode != 0 or status.stdout.strip() != 'not-found':
        raise BuildError('isolated test unit still exists')


def prepare(files, release, work, *, run_id, attempt, release_gid):
    release, work = pathlib.Path(release), pathlib.Path(work)
    test_gid = grp.getgrnam('wbozon-test').gr_gid
    if (not release.is_absolute() or not work.is_absolute() or release.exists() or work.exists() or
            release.parent != RELEASES or work.parent != CANDIDATES or work.name != release.name or
            not re.fullmatch('[1-9][0-9]{0,19}', run_id) or not re.fullmatch('[1-9][0-9]{0,4}', attempt) or
            not re.fullmatch('[0-9a-f]{40}-' + run_id + '-' + attempt, release.name) or
            type(release_gid) is not int or release_gid < 0 or type(test_gid) is not int or test_gid < 0):
        raise BuildError('invalid or existing candidate path')
    _secure_directory(RELEASES)
    _secure_directory(CANDIDATES, private=True)
    _validate_files(files)
    github_artifact.verify_wheels(files)
    release.mkdir(mode=0o750)
    work.mkdir(mode=0o700)
    for name, data in files.items():
        if name.startswith('service/'):
            _write(release / name, data)
        elif name == 'test_native.py':
            _write(release / name, data)
        elif name.startswith(('wheelhouse/', 'bootstrap/')) or name == 'requirements-offline.lock':
            _write(work / name, data)
    python = release / 'venv/bin/python'
    _command(['/usr/bin/python3', '-m', 'venv', '--without-pip', '--copies', str(release / 'venv')],
             stage='candidate virtual environment creation')
    bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('pip',run_name='__main__')"
    _command([str(python), '-c', bootstrap, str(work / 'bootstrap/pip-26.3-py3-none-any.whl'),
              'install', '--isolated', '--no-index', '--no-cache-dir', '--no-compile', '--no-deps',
              '--find-links', str(work / 'wheelhouse'), '--require-hashes', '-r', str(work / 'requirements-offline.lock')],
             timeout=360, stage='offline wheel installation')
    for path in [p for p in release.rglob('*') if p.is_symlink()]:
        if path == release / 'venv/lib64' and os.readlink(path) == 'lib':
            path.unlink()
        else:
            raise BuildError('unexpected candidate link')
    # Only the isolated test UID can read the candidate; no candidate process can write it.
    for path in [release, *release.rglob('*')]:
        info = path.lstat()
        if (info.st_uid != 0 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or
                stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise BuildError('invalid candidate file')
        os.chown(path, 0, test_gid)
        os.chmod(path, 0o750 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o640)
    before = inventory(release)
    prefix = 'wbozon-test-' + run_id + '-' + attempt
    _sandbox(release, prefix + '-unit', [str(python), '-m', 'unittest', '-q'], release / 'service',
             runtime_seconds=720)
    _sandbox(release, prefix + '-smoke', [str(python), str(release / 'test_native.py')], '/tmp',
             runtime_seconds=300)
    if inventory(release) != before:
        raise BuildError('candidate changed during isolated tests')
    for path in [release, *release.rglob('*')]:
        os.chown(path, 0, release_gid)
    files_sha = inventory(release)
    required = {'service/portal.py', 'service/gunicorn.conf.py', 'venv/bin/gunicorn', 'venv/bin/python'}
    if not required <= set(files_sha):
        raise BuildError('incomplete candidate')
    _sync_tree(release)
    raw = (json.dumps(files_sha, sort_keys=True, separators=(',', ':')) + '\n').encode()
    manifest = release / 'MANIFEST.json'
    _write(manifest, raw, 0o640)
    os.chown(manifest, 0, release_gid)
    fd = os.open(release, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(release.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(raw).hexdigest()
