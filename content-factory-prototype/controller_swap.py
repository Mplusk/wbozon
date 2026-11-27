"""Review-only controller updater. Do not run before CI and operational review.

The entry point accepts no arguments and targets one precise production state.
Tests inject temporary paths into the internal function; these are not exposed
as command-line options. A successful swap does not deploy the portal.
"""
import sys
if __name__ == '__main__' and not sys.flags.isolated:
    raise SystemExit('Run with /usr/bin/python3 -I; environment isolation required')
import fcntl
import hashlib
import json
import os
import pathlib
import stat
import tempfile
import subprocess
import urllib.request

STATE = pathlib.Path('/var/lib/3dprinta-native-deployer')
LIB = pathlib.Path('/usr/local/libexec/wbozon-cd')
ROOT = pathlib.Path('/opt/3dprinta-portal')
CANDIDATE = STATE / 'reviewed-cd-a3e3362.py'
EXPECTED_RELEASE = '7549b40f1f34a1dfdff79bcdf8a6b35ec75de13f-45763537568-1'
EXPECTED_MAIN = '7549b40f1f34a1dfdff79bcdf8a6b35ec75de13f'
OLD_SHA = '7b1410b73820b5d22c19d0c904dc2b76d36dac000c3df3614cc099e3179e8f39'
NEW_SHA = 'aa83c320050169c690043a2dca88f992140e83f1fd8645314960553f3f69e7e0'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path, directory=False, owner=0, *, boundary=pathlib.Path('/')):
    """No symlinks, untrusted owners, or group/world writable ancestors."""
    path = pathlib.Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute normalized path required')
    boundary = pathlib.Path(boundary)
    if not boundary.is_absolute() or '..' in boundary.parts:
        raise ValueError('invalid trust boundary')
    try:
        relative = path.relative_to(boundary)
    except ValueError:
        raise ValueError('path outside trust boundary') from None
    paths = [boundary]
    for part in relative.parts:
        paths.append(paths[-1] / part)
    for p in paths:
        info = p.lstat()
        if info.st_uid != owner or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('untrusted path')
    if directory and not path.is_dir():
        raise ValueError('directory required')
    if not directory and not path.is_file():
        raise ValueError('file required')
    return path


def syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _perform(state, lib, root, candidate, old_sha, new_sha, expected_release, verify_release, preflight, owner=0, *, boundary=pathlib.Path('/')):
    def check(path, directory=False):
        return trusted(path, directory, owner, boundary=boundary)
    state = check(state, True)
    lib = check(lib, True)
    root = check(root, True)
    fd = os.open(state / 'deploy.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('untrusted deploy lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(state / 'pending.json'):
            raise ValueError('pending deployment')
        receipt = json.loads(check(state / 'active.json').read_bytes())
        if not isinstance(receipt, dict) or receipt.get('release') != expected_release:
            raise ValueError('unexpected active receipt')
        link = root / 'current'
        if not link.is_symlink() or link.lstat().st_uid != owner:
            raise ValueError('untrusted current link')
        release = check(root / 'releases' / expected_release, True)
        if link.resolve(strict=True) != release:
            raise ValueError('unexpected current release')
        verify_release(expected_release, receipt.get('manifest'))
        target = check(lib / 'cd.py')
        if stat.S_IMODE(target.stat().st_mode) != 0o644 or target.stat().st_nlink != 1:
            raise ValueError('unexpected controller permissions')
        old = target.read_bytes()
        new = check(candidate).read_bytes()
        if digest(old) != old_sha or digest(new) != new_sha:
            raise ValueError('controller digest mismatch')
        # Preserve original bytes once; never overwrite an unknown backup.
        backup = state / ('cd-before-' + old_sha + '.py')
        try:
            bfd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            b = check(backup)
            if stat.S_IMODE(b.stat().st_mode) != 0o600 or b.stat().st_nlink != 1 or digest(b.read_bytes()) != old_sha:
                raise ValueError('backup mismatch')
        else:
            with os.fdopen(bfd, 'wb') as out:
                out.write(old); out.flush(); os.fsync(out.fileno())
            syncdir(state)
        # Check current health and remote main under the same lock immediately
        # before replacement; failure never changes the installed controller.
        preflight()
        tmpfd, tmp = tempfile.mkstemp(prefix='.cd-reviewed-', dir=lib)
        try:
            with os.fdopen(tmpfd, 'wb') as out:
                os.fchmod(out.fileno(), 0o644)
                out.write(new); out.flush(); os.fsync(out.fileno())
            os.replace(tmp, target)
            syncdir(lib)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        result = check(target)
        if stat.S_IMODE(result.stat().st_mode) != 0o644 or result.stat().st_nlink != 1 or digest(result.read_bytes()) != new_sha:
            raise RuntimeError('installed controller failed readback; inspect under lock before action')
        return {'old_sha256': old_sha, 'new_sha256': new_sha, 'release': expected_release}
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def preflight():
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run(['/usr/bin/git', '-c', 'credential.helper=', 'ls-remote',
                             'https://github.com/Mplusk/wbozon.git', 'refs/heads/main'],
                            env=env, capture_output=True, timeout=10, check=True)
    if result.stdout != (EXPECTED_MAIN + '\trefs/heads/main\n').encode('ascii'):
        raise ValueError('remote main changed or unavailable')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open('https://3dprinta.shop/healthz', timeout=5) as response:
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096 or json.loads(raw).get('status') != 'ok':
            raise ValueError('portal health not confirmed')


def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    # Import only from the trusted installed controller directory.
    trusted(pathlib.Path(__file__).absolute())
    trusted(LIB, True)
    trusted(LIB / 'release_host.py')
    trusted(LIB / 'native_release.py')
    sys.path.insert(0, str(LIB))
    from release_host import Host
    host = Host(pathlib.Path('/etc/wbozon-cd.json'))
    if host.state != STATE or host.root != ROOT:
        raise ValueError('unexpected host configuration')
    print(json.dumps(_perform(STATE, LIB, ROOT, CANDIDATE, OLD_SHA, NEW_SHA,
                              EXPECTED_RELEASE, host.verify, preflight), sort_keys=True))


if __name__ == '__main__':
    main()
