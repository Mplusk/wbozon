"""Exercise the controller on a separate systemd portal with synthetic databases.

Run as root with --python /absolute/python-with-gunicorn and --gate-source /path/finance_gate.py.
Only creates uniquely named test units, a loopback listener and its own state directories.
"""
import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cd import Transaction
from release_host import Host, atomic_json, command, digest


def driver(cfg, name, sha, fault):
    host = Host(cfg)
    class Inject(Transaction):
        def seal_market_decision(self, j, name):
            super().seal_market_decision(j, name)
            if fault == 'bootstrap-after-unhold':
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with client.open('http://127.0.0.1:' + str(host.port) + '/market-cutover', timeout=8) as r:
                    if r.read() != b'sent':
                        raise RuntimeError('final bridge did not admit its first synthetic provider request')
                os.kill(os.getpid(), signal.SIGKILL)
        def phase(self, journal, phase):
            super().phase(journal, phase)
            if phase == fault:
                os.kill(os.getpid(), signal.SIGKILL)
        def complete(self, journal, release, manifest):
            if fault == 'reports-after-open':
                host.barrier(journal['txn'], journal['mark'], False)
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for provider in ('ozon', 'wb'):
                    with client.open('http://127.0.0.1:' + str(host.port) + '/market-limit-' + provider,
                                     timeout=8) as r:
                        if r.read() != b'recorded':
                            raise RuntimeError('expected a fresh provider limit in reports release')
                (host.gate.parent / ('fail-' + release)).touch()
                os.kill(os.getpid(), signal.SIGKILL)
            if fault == 'bootstrap-after-open':
                host.barrier(journal['txn'], journal['mark'], False)
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with client.open('http://127.0.0.1:' + str(host.port) + '/market-cutover', timeout=8) as r:
                    if r.read() != b'held':
                        raise RuntimeError('bootstrap allowed an outbound request before commit')
                (host.state / 'bootstrap-observed').touch()
                (host.gate.parent / ('fail-' + release)).touch()
                os.kill(os.getpid(), signal.SIGKILL)
            if fault == 'unhealthy-after-open':
                host.barrier(journal['txn'], journal['mark'], False)
                if host.cfg.get('qa_release_pair'):
                    # Discard the client only after the candidate's real finance worker reached the provider.
                    with socket.create_connection(('127.0.0.1', host.port), timeout=3) as client:
                        client.sendall(b'GET /rollback-hold HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                        end = time.monotonic()+8
                        while not (host.gate.parent / 'provider-active-rollback-hold').exists():
                            if time.monotonic()>end:
                                raise RuntimeError('candidate finance read did not start')
                            time.sleep(.025)
                (host.gate.parent / ('fail-' + release)).touch()
                os.kill(os.getpid(), signal.SIGKILL)
            super().complete(journal, release, manifest)
    with host.locked():
        Inject(host).activate(name, sha, lambda: None)


def restore_driver(cfg, fault, index):
    """Exercise a real fenced restore using only the temporary fixture databases."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import restore_controller
    import restore_files
    host = Host(cfg)
    name, _ = Transaction(host).receipt()
    root = host.state / ('restore-fixture-' + index)
    root.mkdir(mode=0o700)
    archive, keyfile, work = root / 'vault.sqlite', root / 'vault-key', root / 'work'
    work.mkdir(mode=0o700)
    with contextlib.closing(sqlite3.connect(Path(host.cfg['databases'][1]).as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(archive)) as dst:
            src.backup(dst)
            old_key = b'a' * 32
            for provider, value in (('yandex_backup', b'old-test-settings'),
                                    ('yandex_backup_metadata', b'old-test-metadata'), ('ozon', b'from-backup')):
                nonce = os.urandom(12)
                dst.execute('UPDATE vault SET blob=? WHERE provider=?',
                            (nonce + AESGCM(old_key).encrypt(nonce, value, provider.encode()), provider))
            dst.commit()
    archive.chmod(0o600)
    keyfile.write_bytes(b'a' * 32)
    files = {'databases/vault.sqlite': archive, 'secrets/vault-key': keyfile}
    original_apply = restore_files.apply
    class Inject(Transaction):
        def phase(self, journal, phase):
            super().phase(journal, phase)
            if (fault == 'before-apply' and phase == 'restoring' and journal.get('restore_state') == 'modifying' or
                    fault == 'after-commit' and phase == 'committed'):
                os.kill(os.getpid(), signal.SIGKILL)
        def start(self, journal, release, guard):
            super().start(journal, release, guard)
            if fault == 'after-start':
                os.kill(os.getpid(), signal.SIGKILL)
        def complete(self, journal, release, sha):
            if fault == 'after-open':
                host.barrier(journal['txn'], journal['mark'], False)
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with client.open('http://127.0.0.1:' + str(host.port) + '/restore-write', timeout=8) as response:
                    if response.read() != b'changed':
                        raise RuntimeError('synthetic post-admission write failed')
                os.kill(os.getpid(), signal.SIGKILL)
            super().complete(journal, release, sha)
    if fault == 'partial':
        def interrupted_apply(*args, **kwargs):
            def stop(_name):
                os.kill(os.getpid(), signal.SIGKILL)
            return original_apply(*args, **kwargs, after_each=stop)
        restore_files.apply = interrupted_apply
    with host.locked():
        restore_controller.activate(Inject(host), files, work, b'c' * 32)


def suite(python, gate_source, baseline_service=None, candidate_service=None, market_bootstrap=False,
          market_reports=False, interrupted_finance=False, restore_test=False):
    tag = 'wbozon-qa-' + secrets.token_hex(3)
    unit, recover, holder = (tag + suffix + '.service' for suffix in ('', '-recover', '-holder'))
    run = str(secrets.randbelow(10**10) + 10**12)
    root = Path('/var/lib') / (tag + '-releases')
    state = Path('/var/lib') / (tag + '-cd')
    data = Path('/var/lib') / tag
    system = Path('/run/systemd/system')
    cfg = state / 'config.json'
    old, new = 'a' * 40, 'b' * 40 + '-1-1'
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    root.mkdir(mode=0o755)
    state.mkdir(mode=0o700)
    files = [system / unit, system / recover]
    def request(path):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                'http://127.0.0.1:' + str(port) + path, timeout=15) as r:
            return r.read()
    def until(check, limit=20):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                if check():
                    return
            except OSError:
                pass
            time.sleep(.1)
        raise RuntimeError('test condition did not complete')
    try:
        (root / 'releases').mkdir(mode=0o755)
        fixtures = root / 'fixtures'
        shutil.copytree(HERE / 'fixtures', fixtures, ignore=shutil.ignore_patterns('__pycache__'))
        (state / 'backups').mkdir(mode=0o700)
        (state / 'baselines').mkdir(mode=0o700)
        shas = {}
        for name in (old, new):
            directory = root / 'releases' / name
            service = directory / 'service'
            if baseline_service:
                shutil.copytree(baseline_service if name == old else candidate_service, service)
            else:
                service.mkdir(parents=True)
                (service / 'finance_gate.py').write_bytes(gate_source.read_bytes())
                for f in ('app.py', 'oauth_mcp.py', 'external_keys.py', 'gunicorn.conf.py'):
                    (service / f).write_text('# synthetic\n')
                (service / 'portal.py').write_text('class Vault: pass\nclass Portal:\n def __init__(self): pass\n'
                    ' def read_gateway(self): pass\ndef create_portal(): pass\ndef provider_read(): pass\n')
            service.chmod(0o755)
            directory.chmod(0o755)
            filemap = {}
            for f in service.rglob('*'):
                if f.is_dir():
                    f.chmod(0o755)
                    continue
                f.chmod(0o644)
                filemap[f.relative_to(directory).as_posix()] = digest(f.read_bytes())
            raw = json.dumps(filemap).encode()
            shas[name] = digest(raw)
            mf = state / 'baselines' / (old + '.json') if name == old else directory / 'MANIFEST.json'
            mf.write_bytes(raw)
        (root / 'current').symlink_to(root / 'releases' / old)
        config = dict(root=str(root), state=str(state), unit=unit, user=tag, holder=holder,
                      release_group='wbozon-release', baseline=old, baseline_sha256=shas[old],
                      addresses=['127.0.0.1'], port=port, host='localhost', gate=str(data / '.ozon-finance-gates'),
                      databases=[str(data / n) for n in ('auth.sqlite', 'vault.sqlite')], dependencies=[],
                      recovery_unit=recover, qa_release_pair=bool(baseline_service) and not (market_bootstrap or market_reports),
                      worker_config_sha256=digest((root / 'current/service/gunicorn.conf.py').read_bytes()))
        unit_data = ('[Service]\nType=exec\nDynamicUser=yes\nUser=' + tag + '\nSupplementaryGroups=wbozon-release\n'
            'WorkingDirectory=' + str(root / 'current/service') + '\nStateDirectory=' + tag + '\nStateDirectoryMode=0700\n'
            'Environment=PYTHONPATH=' + str(fixtures) + ' WBOZON_QA_RELEASE_PAIR=' + str(int(config['qa_release_pair'])) +
            ' WBOZON_QA_RESTORE=' + str(int(restore_test)) +
            ' PYTHONDONTWRITEBYTECODE=1 DATABASE_PATH=' + config['databases'][0] +
            ' VAULT_PATH=' + config['databases'][1] + '\nKillMode=mixed\nExecCondition=+/usr/bin/python3 -I ' +
            str(HERE / 'cd.py') + ' --config ' + str(cfg) + ' start-check\nExecStart=' + str(python) +
            ' -m gunicorn -w 2 --bind 127.0.0.1:' + str(port) + ' portal_fixture:application\n')
        files[0].write_text(unit_data)
        files[1].write_text('[Unit]\nConditionPathExists=' + str(state / 'pending.json') +
                            '\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I ' + str(HERE / 'cd.py') +
                            ' --config ' + str(cfg) + ' recover\n')
        command(['/usr/bin/systemctl', 'daemon-reload'])
        config['unit_sha256'] = digest(command(['/usr/bin/systemctl', 'cat', '--no-pager', unit]).encode())
        atomic_json(cfg, config)
        host = Host(cfg)
        command(['/usr/bin/systemctl', 'start', unit])
        until(lambda: request('/healthz') == b'{"status":"ok"}')
        assert request('/seed') == b'finished'
        index = 0
        def launch(target, fault, auto=True):
            nonlocal index
            index += 1
            args = ['/usr/bin/systemd-run', '--quiet', '--wait', '--collect', '--unit=wbozon-deploy-' + run +
                    '-' + str(index), '--property=UMask=0077']
            if auto:
                args.append('--property=OnFailure=' + recover)
            return subprocess.Popen([*args, '/usr/bin/python3', '-I', str(Path(__file__).resolve()), 'driver',
                                    str(cfg), target, shas[target], fault])
        def checked(name):
            return (not (state / 'pending.json').exists() and host.current() == name and
                    host.process(name) and request('/healthz') == b'{"status":"ok"}')
        if restore_test:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            def actual():
                with contextlib.closing(sqlite3.connect(data / 'vault.sqlite')) as db:
                    blob = db.execute("SELECT blob FROM vault WHERE provider='ozon'").fetchone()[0]
                return AESGCM(b'c' * 32).decrypt(blob[:12], blob[12:], b'ozon')
            for index, (fault, expected) in enumerate((('before-apply', b'before-restore'),
                    ('partial', b'before-restore'), ('after-start', b'before-restore'),
                    ('after-commit', b'from-backup'), ('after-open', b'after-admission')), 1):
                proc = subprocess.Popen(['/usr/bin/systemd-run', '--quiet', '--wait', '--collect',
                    '--unit=wbozon-restore@' + run + '-' + str(index), '--property=UMask=0077',
                    '--property=Slice=system.slice', '--property=OnFailure=' + recover,
                    '/usr/bin/python3', '-I', str(Path(__file__).resolve()), 'restore-driver', str(cfg), fault, str(index)])
                assert proc.wait(timeout=180) != 0
                until(lambda: checked(old), 140)
                assert actual() == expected, (fault, actual())
                hold = host.root / '.market-cutover-pending'
                if fault in ('after-commit', 'after-open'):
                    assert json.loads(hold.read_bytes()).get('purpose') == 'restore'
                    hold.unlink()  # Test-only: model the owner's later explicit resume.
                else:
                    assert not hold.exists()
                print('PASS synthetic restore recovery at', fault, flush=True)
            return
        if market_bootstrap:
            assert launch(new, 'bootstrap-after-open').wait(timeout=150) != 0
            until(lambda: checked(old) and not (root / '.market-cutover-pending').exists(), 140)
            assert (state / 'bootstrap-observed').exists()
            assert not (root / '.market-cutover-pending').exists()
            assert not (data / '.market-method-gates').exists()
            (data / ('fail-' + new)).unlink()
            assert launch(new, 'bootstrap-after-unhold').wait(timeout=150) != 0
            until(lambda: checked(new), 140)
            assert (data / '.market-method-gates').is_dir()
            assert request('/market-cutover') == b'sent'
            assert launch(old, 'none').wait(timeout=150) != 0 and checked(new)
            print('PASS bootstrap fences vendor requests through post-open failure, recovers, then forbids downgrade',
                  flush=True)
            return
        if market_reports:
            scoped = data / 'vault.sqlite.avito-keys'
            seeded_keys = (baseline_service / 'avito_mcp.py').is_file()
            if seeded_keys:
                assert scoped.stat().st_mode & 0o777 == 0o600
                assert Path(str(scoped)+'-wal').stat().st_size > 0
                master = host.process(old)[0]
                scoped.chmod(0o644)
                try:
                    assert launch(new, 'none').wait(timeout=150) != 0
                    until(lambda: checked(old), 140)
                    assert host.process(old)[0] == master
                finally:
                    scoped.chmod(0o600)
            def finance_snapshot():
                key = hashlib.sha256(b'7654321').hexdigest()
                state = (host.gate / (key + '.json')).read_bytes()
                epoch = (data / (host.gate.name + '.epoch-' + key + '.json')).read_bytes()
                assert json.loads(state)['status'] == 'in_flight'
                assert json.loads(state)['generation'] == json.loads(epoch)['generation']
                return state, epoch
            if interrupted_finance:
                assert request('/interrupt-finance') == b'held'
                snapshot = finance_snapshot()
            assert launch(new, 'reports-after-open').wait(timeout=150) != 0
            assert (data / ('fail-' + new)).is_file(), (
                f'reports-after-open fault was not reached: {baseline_service} -> {candidate_service}'
            )
            until(lambda: checked(old), 140)
            if interrupted_finance:
                assert request('/check-interrupted-finance') == b'held' and finance_snapshot() == snapshot
            for provider in ('ozon', 'wb'):
                response = request('/market-limit-' + provider)
                assert response == b'held', (
                    f'cooldown after rollback: provider={provider}, '
                    f'response={response if response in (b"held", b"recorded") else b"unexpected"!r}, '
                    f'pair={baseline_service} -> {candidate_service}'
                )
                assert (data / ('attempts-' + provider)).read_text().splitlines() == ['attempt']
            (data / ('fail-' + new)).unlink()
            assert launch(new, 'none').wait(timeout=150) == 0 and checked(new)
            if interrupted_finance:
                assert request('/check-interrupted-finance') == b'held' and finance_snapshot() == snapshot
                print('PASS interrupted Ozon finance read stays blocked with identical state and epoch through '
                      'activation, rollback and redeployment', flush=True)
            for provider in ('ozon', 'wb'):
                response = request('/market-limit-' + provider)
                assert response == b'held', (
                    f'cooldown after redeployment: provider={provider}, '
                    f'response={response if response in (b"held", b"recorded") else b"unexpected"!r}, '
                    f'pair={baseline_service} -> {candidate_service}'
                )
                assert (data / ('attempts-' + provider)).read_text().splitlines() == ['attempt']
            print('PASS Ozon and WB deadlines survive a post-admission systemd rollback and redeployment', flush=True)
            if seeded_keys:
                snapshots = list((state/'backups').glob('*/vault.sqlite.avito-keys'))
                assert snapshots
                for path in snapshots:
                    assert path.stat().st_mode & 0o777 == 0o600
                    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                        assert db.execute('SELECT name FROM external_mcp_keys').fetchall() == [('Avito fixture',)]
                print('PASS private scoped keys and latest WAL commit included in rollout snapshots', flush=True)
            return
        (data / 'started').unlink()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            response = pool.submit(request, '/hold')
            until(lambda: (data / 'started').exists())
            with socket.create_connection(('127.0.0.1', port), timeout=3) as detached:
                detached.sendall(b'GET /detached HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                until(lambda: (data / 'detached-handler').exists())
                detached.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            proc = launch(new, 'none')
            until(lambda: (state / 'pending.json').exists() and
                  host.barrier_exists('wbozon_cd_' + json.loads((state / 'pending.json').read_text())['txn']))
            try:
                request('/healthz')
            except OSError:
                pass
            else:
                raise AssertionError('new traffic admitted while draining')
            assert response.result() == b'finished' and proc.wait(timeout=150) == 0
        assert checked(new) and (data / 'completed').read_text().splitlines() == ['finished'] * 3
        print('PASS live and disconnected handlers completed their finance reads once; new connections fenced', flush=True)
        if baseline_service:
            assert request('/activity-candidate') == b'checked'
            paths = (data / 'provider-attempts').read_text().splitlines()
            assert len(paths) == 3 and all(p.endswith('/v1/finance/accrual/types') for p in paths)
        for phase in ('draining', 'stopping', 'switching', 'starting', 'committed'):
            target = old
            auto = phase != 'starting'
            proc = launch(target, phase, auto=auto)
            assert proc.wait(timeout=150) != 0
            if not auto:
                # Model boot between pointer switch and launch: stale journal, no controller,
                # no nft state. A normal systemd start must be refused until reconciliation.
                j = json.loads((state / 'pending.json').read_bytes())
                host.barrier(j['txn'], j['mark'], False)
                command(['/usr/bin/systemctl', 'start', unit])
                assert int(host.props()['MainPID']) == 0
                command(['/usr/bin/systemctl', 'start', recover], timeout=140)
            until(lambda: checked(old if phase == 'committed' else new), 140)
            print('PASS automatic recovery at', phase, flush=True)
        (data / ('fail-' + new)).touch()
        # This fixture deliberately exhausts the controller's 180-second
        # startup deadline before systemd can report the deployment failure.
        assert launch(new, 'none').wait(timeout=240) != 0
        until(lambda: checked(old), 140)
        assert not (state / 'permit.json').exists()
        print('PASS failed candidate health restored prior release', flush=True)
        (data / ('fail-' + new)).unlink()
        assert launch(new, 'unhealthy-after-open').wait(timeout=150) != 0
        until(lambda: checked(old), 140)
        assert not (state / 'permit.json').exists()
        print('PASS failure after ingress reopened restored prior release', flush=True)
        if baseline_service:
            assert (data / 'provider-attempts').read_text().splitlines().count('rollback-hold /v1/finance/accrual/by-day') == 1
            assert (data / 'completed').read_text().splitlines() == ['finished'] * 4
            assert request('/activity-restored') == b'checked'
            print('PASS restored original portal reads candidate activity, session, vault and MCP key', flush=True)
    finally:
        for u in (unit, holder, recover):
            subprocess.run(['/usr/bin/systemctl', 'stop', u], capture_output=True, timeout=100)
        pending = state / 'pending.json'
        if pending.exists() and cfg.exists():
            j = json.loads(pending.read_bytes())
            Host(cfg).barrier(j['txn'], j['mark'], False)
        for f in files:
            f.unlink(missing_ok=True)
        command(['/usr/bin/systemctl', 'daemon-reload'])
        for p in (root, state, Path('/var/lib/private') / tag):
            if p.exists() and p.name.startswith('wbozon-qa-'):
                shutil.rmtree(p)
        if data.is_symlink():
            data.unlink()


if __name__ == '__main__':
    if sys.argv[1:2] == ['driver']:
        driver(*sys.argv[2:])
    elif sys.argv[1:2] == ['restore-driver']:
        restore_driver(*sys.argv[2:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--python', type=Path, required=True)
        parser.add_argument('--gate-source', type=Path)
        parser.add_argument('--baseline-service', type=Path)
        parser.add_argument('--candidate-service', type=Path)
        parser.add_argument('--market-bootstrap', action='store_true')
        parser.add_argument('--market-reports', action='store_true')
        parser.add_argument('--interrupted-finance', action='store_true')
        parser.add_argument('--restore', action='store_true')
        args = parser.parse_args()
        if bool(args.baseline_service) != bool(args.candidate_service) or bool(args.gate_source) == bool(args.baseline_service):
            parser.error('provide both service trees or a single synthetic gate source')
        if args.interrupted_finance and not args.market_reports:
            parser.error('--interrupted-finance requires --market-reports')
        suite(args.python, args.gate_source, args.baseline_service, args.candidate_service,
              args.market_bootstrap, args.market_reports, args.interrupted_finance, args.restore)
