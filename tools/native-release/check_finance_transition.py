"""Offline acceptance for the reviewed old/new service trees using separate Python processes."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from finance_guard import FinanceGuard, GuardError
from release_host import ReleaseError, compatible

PROBE = HERE / 'fixtures/transition_probe.py'


def verify_sources(old, new):
    compatible(old,new)
    compatible(new,old)
    def must_refuse(first, second, reason):
        for a,b in ((first,second),(second,first)):
            try:
                compatible(a,b)
            except ReleaseError:
                continue
            raise AssertionError('unreviewed release admitted: ' + reason)

    # The unreviewed combination must fail even when every blob came from an approved release.
    for name in ('finance_gate.py','portal.py','external_mcp.py','app.py','gunicorn.conf.py','ozon_fbo.py','new-helper.py'):
        path = new / 'service' / name
        original = path.read_bytes() if path.exists() else None
        try:
            if name == 'portal.py':
                path.write_bytes((old / 'service' / name).read_bytes())
            else:
                path.write_bytes((original or b'') + b'\n# unreviewed\n')
            must_refuse(old, new, name)
        finally:
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
    # The previous portal and gate cannot dispatch newly registered finance operations together.
    originals = {}
    try:
        for name in ('finance_gate.py','portal.py','external_mcp.py'):
            path = new / 'service' / name
            originals[path] = path.read_bytes()
            path.write_bytes((old / 'service' / name).read_bytes())
        must_refuse(old, new, 'previous gate and dispatch with new provider registry')
    finally:
        for path, original in originals.items():
            path.write_bytes(original)
    clone = old.parent / 'changed-portal'
    shutil.copytree(old, clone)
    with (clone / 'service/ui.py').open('ab') as module:
        module.write(b'\n# altered imported module\n')
    must_refuse(old, clone, 'imported module changed while finance gate stayed the same')


def exercise(python, old, new, temp):
    def start(release, data, scenario='ok', tag='read', tick=0):
        return subprocess.Popen([str(python),'-B','-I',str(PROBE),'worker',str(data),scenario,tag,str(tick)],
                                cwd=release/'service',stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def result(proc):
        out, err = proc.communicate(timeout=16)
        assert proc.returncode == 0, err
        return json.loads(out)
    def snapshot(data):
        return {p.relative_to(data):p.read_bytes() for p in data.rglob('*')
                if p.is_file() and p.relative_to(data).parts[0].startswith('.ozon-finance-gates')}
    def until_file(path, proc):
        end = time.monotonic()+8
        while not path.exists():
            if proc.poll() is not None or time.monotonic()>end:
                raise AssertionError('provider never entered: '+path.name)
            time.sleep(.025)
    for source, target in [(old,new),(new,old)]:
        for scenario in ('hold','killed','known429','unknown429'):
            data = temp / (source.name+'-'+scenario)
            data.mkdir(mode=0o700)
            gate_dir = str(data/'.ozon-finance-gates')
            if scenario in ('hold','killed'):
                proc = start(source,data,'hold','active')
                try:
                    until_file(data/'entered-active',proc)
                    assert result(start(target,data))['reason'] == 'busy'
                    if scenario == 'hold':
                        (data/'release-active').touch()
                        assert result(proc)['kind'] == 'ok'
                        assert result(start(target,data,tag='resumed'))['kind'] == 'ok'
                    else:
                        proc.kill()
                        proc.communicate(timeout=5)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=5)
            else:
                assert result(start(source,data,scenario,tag='active'))['status'] == 429
            if scenario in ('killed','unknown429'):
                before = snapshot(data)
                assert result(start(target,data))['reason'] == 'retry_time_unknown'
                try:
                    with FinanceGuard(gate_dir,os.getuid(),0):
                        raise AssertionError('normal deployment admitted an uncertain operation')
                except GuardError:
                    pass
                with FinanceGuard(gate_dir,os.getuid(),0,allow_uncertain=True) as guard:
                    guard.verify()
                assert result(start(source,data))['reason'] == 'retry_time_unknown'
                assert snapshot(data) == before
            elif scenario == 'known429':
                before = snapshot(data)
                with FinanceGuard(gate_dir,os.getuid(),0) as guard:
                    guard.verify()
                blocked = result(start(target,data,tick=17))
                assert (blocked['reason'],blocked['retry_after_seconds']) == ('cooldown',43), blocked
                assert before == snapshot(data)
                assert result(start(target,data,tag='resumed',tick=61))['kind'] == 'ok'
            attempts = (data/'provider-attempts').read_text().splitlines()
            assert len(attempts) == (2 if scenario in ('hold','known429') else 1), attempts
            assert sum(v.startswith('active ') for v in attempts) == 1
    data = temp/'composite'
    data.mkdir(mode=0o700)
    rejected = result(start(new,data,'dictionary429','composite'))
    assert (rejected['status'],rejected['retry_after_seconds']) == (429,1), rejected
    assert result(start(old,data))['reason'] == 'cooldown'
    assert result(start(old,data,tag='after-expiry',tick=2))['kind'] == 'ok'
    assert (data/'provider-attempts').read_text().splitlines() == [
        'composite /v1/finance/accrual/by-day','composite /v1/finance/accrual/types','after-expiry /v1/finance/accrual/types']
    # A mismatched epoch must remain blocked in both releases. Simulate an interrupted state write.
    epoch, = data.glob('.ozon-finance-gates.epoch-*.json')
    record = json.loads(epoch.read_bytes())
    record['generation'] += 1
    epoch.write_text(json.dumps(record))
    before = snapshot(data)
    for version in (old,new):
        assert result(start(version,data))['reason'] == 'storage_unavailable'
    assert snapshot(data) == before and len((data/'provider-attempts').read_text().splitlines()) == 3
    print('PASS both worker versions preserve locks, interrupted outcomes, retry deadlines, epochs and attempt counts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-service',type=Path,required=True)
    parser.add_argument('--adoption-service',type=Path)
    parser.add_argument('--candidate-service',type=Path,required=True)
    parser.add_argument('--python',type=Path,required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        old, new = tmp/'baseline', tmp/'candidate'
        for src, dst in ((args.baseline_service,old),(args.candidate_service,new)):
            shutil.copytree(src,dst/'service')
        if args.adoption_service:
            original = tmp / 'adopted'
            shutil.copytree(args.adoption_service, original / 'service')
            compatible(original, old)
            compatible(old, original)
            print('PASS original adoption pair remains compatible in both directions',flush=True)
        verify_sources(old,new)
        print('PASS exact source trees in both directions; partial upgrades and altered sources rejected',flush=True)
        python = args.python.absolute()
        exercise(python, old, new, tmp)
        data = tmp / 'portal-data'
        data.mkdir(mode=0o700)
        for release, stage in ((old,'seed'),(new,'candidate'),(old,'restored')):
            subprocess.run([str(python),'-B','-I',str(PROBE),'activity',str(data),stage],
                           cwd=release/'service',check=True,timeout=20)
        print('PASS both portal versions share the same sessions, key, activity status and vault')


if __name__ == '__main__':
    main()
