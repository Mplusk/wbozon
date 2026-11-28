"""One-time adoption of the existing 3dprinta portal on the reviewed host."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))
from native_release import DROP_IN, DROP_IN_SHA256, HELPER, HELPER_SHA256, secure, syncdir
from release_host import Host, atomic_json, command, digest, unit_properties

BASE = 'ebd651e963c95f7c796ff1439bb7498160adfd43'
BASE_HASH = '63681c25f5140e509594b800f1bd3dd8cde0a17f764f350cfc0a5e1b57c918f9'
UNIT_HASH = '70e9403e0ea587ca418bf0fee3fbec0f6ef85b7ad64f0d67747acd109e87d931'
UNIT = '3dprinta-portal.service'
STATE = Path('/var/lib/3dprinta-native-deployer')
ROOT = Path('/opt/3dprinta-portal')
CONFIG = Path('/etc/wbozon-cd.json')
LIB = Path('/usr/local/libexec/wbozon-cd')
SYSTEM = Path('/etc/systemd/system')


def install(source, destination):
    data = secure(source).read_bytes()
    fd, tmp = tempfile.mkstemp(dir=secure(destination.parent, True))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            os.fchmod(out.fileno(), 0o644)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, destination)
        syncdir(destination.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    if os.geteuid() or CONFIG.exists() or (STATE / 'pending.json').exists():
        raise RuntimeError('adoption requires a new installation and no pending transaction')
    if (ROOT / 'current').resolve() != ROOT / 'releases' / BASE:
        raise RuntimeError('unexpected running release')
    before = unit_properties(UNIT, ['MainPID', 'InvocationID', 'User'])
    if digest(command(['/usr/bin/systemctl', 'cat', '--no-pager', UNIT]).encode()) != UNIT_HASH:
        raise RuntimeError('original unit hash mismatch')
    for path, sha in ((HELPER, HELPER_SHA256), (DROP_IN, DROP_IN_SHA256),
                      (STATE / 'baselines' / (BASE + '.json'), BASE_HASH)):
        if digest(secure(path).read_bytes()) != sha:
            raise RuntimeError('adoption input changed')
    try:
        grp.getgrnam('wbozon-release')
    except KeyError:
        command(['/usr/sbin/groupadd', '--system', 'wbozon-release'])
    try:
        test_account = pwd.getpwnam('wbozon-test')
    except KeyError:
        command(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home', '--home-dir',
                 '/nonexistent', '--shell', '/usr/sbin/nologin', 'wbozon-test'])
        test_account = pwd.getpwnam('wbozon-test')
    if (test_account.pw_uid == 0 or test_account.pw_shell != '/usr/sbin/nologin' or
            test_account.pw_gid != grp.getgrnam('wbozon-test').gr_gid):
        raise RuntimeError('unexpected sandbox identity')
    LIB.mkdir(mode=0o755, exist_ok=True)
    for name in ('cd.py', 'release_host.py', 'catalog_cd_guard.py', 'build_release.py', 'github_artifact.py',
                 'finance_guard.py', 'native_release.py', 'dispatch.py'):
        install(SOURCE / name, LIB / name)
    for p in (STATE / 'candidates', STATE / 'backups'):
        p.mkdir(mode=0o700, exist_ok=True)
    config = dict(unit=UNIT, root=str(ROOT), state=str(STATE), baseline=BASE, baseline_sha256=BASE_HASH,
                  user=before['User'], holder='wbozon-cd-uid-holder.service', release_group='wbozon-release',
                  databases=['/var/lib/3dprinta-portal/auth.sqlite', '/var/lib/3dprinta-portal/vault.sqlite'],
                  gate='/var/lib/3dprinta-portal/.ozon-finance-gates', addresses=['127.0.0.1', '172.30.31.1'],
                  port=18731, host='3dprinta.shop', unit_sha256=UNIT_HASH, recovery_unit='wbozon-cd-recover.service',
                  dependencies=['docker.service', 'marketplace-egress-tunnel.service'],
                  hooks={str(HELPER): HELPER_SHA256, str(DROP_IN): DROP_IN_SHA256})
    atomic_json(CONFIG, config)
    h = Host(CONFIG)
    h.verify(BASE, BASE_HASH)
    h.process(BASE)
    h.health()
    h.sockets()
    feed = SOURCE / 'systemd'
    install(feed / 'wbozon-cd-recover.service', SYSTEM / 'wbozon-cd-recover.service')
    install(feed / 'wbozon-cd-recover.timer', SYSTEM / 'wbozon-cd-recover.timer')
    install(feed / '40-wbozon-cd.conf', SYSTEM / (UNIT + '.d/40-wbozon-cd.conf'))
    command(['/usr/bin/systemctl', 'daemon-reload'])
    config['unit_sha256'] = digest(command(['/usr/bin/systemctl', 'cat', '--no-pager', UNIT]).encode())
    atomic_json(CONFIG, config)
    Host(CONFIG).check_unit()
    command(['/usr/bin/systemctl', 'enable', '--now', 'wbozon-cd-recover.timer'])
    if unit_properties(UNIT, ['MainPID', 'InvocationID', 'User']) != before:
        raise RuntimeError('portal process changed during provisioning')
    h.health()
    print('Installed recovery timer and startup interlock. Running portal identity verified.')


if __name__ == '__main__':
    main()
