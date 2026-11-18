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


def suite(python, gate_source, baseline_service=None, candidate_service=None, market_bootstrap=False):
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
                      recovery_unit=recover, qa_release_pair=bool(baseline_service) and not market_bootstrap,
                      worker_config_sha256=digest((root / 'current/service/gunicorn.conf.py').read_bytes()))
        unit_data = ('[Service]\nType=exec\nDynamicUser=yes\nUser=' + tag + '\nSupplementaryGroups=wbozon-release\n'
            'WorkingDirectory=' + str(root / 'current/service') + '\nStateDirectory=' + tag + '\nStateDirectoryMode=0700\n'
            'Environment=PYTHONPATH=' + str(fixtures) + ' WBOZON_QA_RELEASE_PAIR=' + str(int(config['qa_release_pair'])) +
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
        assert launch(new, 'none').wait(timeout=150) != 0
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
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--python', type=Path, required=True)
        parser.add_argument('--gate-source', type=Path)
        parser.add_argument('--baseline-service', type=Path)
        parser.add_argument('--candidate-service', type=Path)
        parser.add_argument('--market-bootstrap', action='store_true')
        args = parser.parse_args()
        if bool(args.baseline_service) != bool(args.candidate_service) or bool(args.gate_source) == bool(args.baseline_service):
            parser.error('provide both service trees or a single synthetic gate source')
        suite(args.python, args.gate_source, args.baseline_service, args.candidate_service, args.market_bootstrap)
