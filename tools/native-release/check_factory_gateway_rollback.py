"""Offline rollback rehearsal: separate gateway DB keeps uncertainty.

Both service trees must be git-archived into isolated directories by CI. This
does not exercise systemd, a live n8n, provider APIs, or production credentials.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def run(python, service, env, body):
    code = "import sys; sys.path.insert(0,sys.argv[1]); " + body
    subprocess.run([python, '-B', '-I', '-c', code, str(service)], env=env,
                   check=True, timeout=30)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--previous', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--python', required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-rollback-') as temp:
        base=Path(temp); data=base/'gateway'; data.mkdir()
        for name,contents in (('session',b's'*64),('vault-key',b'v'*32),('gateway-key',b'k'*32),
                              ('read-hash',b'f'*64),('smtp',b'synthetic')):
            (base/name).write_bytes(contents)
        env={key:value for key,value in os.environ.items() if key in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP')}
        env.update(DATABASE_PATH=str(base/'login.db'),VAULT_PATH=str(base/'vault.db'),
                   SESSION_SECRET_FILE=str(base/'session'),VAULT_KEY_FILE=str(base/'vault-key'),
                   READ_TOKEN_HASH_FILE=str(base/'read-hash'),SMTP_PASSWORD_FILE=str(base/'smtp'),
                   CF_BUDGET_GATEWAY_ENABLED='1', CF_BUDGET_STAGING_VERIFIED='1',
                   CF_BUDGET_SCOPE_CONFIRMED='1',CF_GATEWAY_DATA_DIR=str(data),
                   CF_GATEWAY_HMAC_KEY_FILE=str(base/'gateway-key'))
        run(args.python,args.candidate,env,
            "import portal; p=portal.create_portal(); g=p.budget_gateway; "
            "assert g is not None and not g.ready(); f=g.fence; f.arm(lambda:True); "
            "o='a'*64; b={'synthetic':'binding'}; q=f.prepare(o,'budget','factory-budget',b); "
            "assert f.dispatch(q,o,b,lambda:'unknown')=='unknown'; assert not f.is_ready()")
        run(args.python,args.previous,env,
            "import portal; p=portal.create_portal(); assert not hasattr(p,'budget_gateway')")
        run(args.python,args.candidate,env,
            "import portal,sqlite3; p=portal.create_portal(); f=p.budget_gateway.fence; "
            "assert not f.is_ready(); db=sqlite3.connect(f.path); "
            "assert db.execute(\"SELECT count(*) FROM fence_proposal WHERE state='unknown'\").fetchone()[0]==1; db.close()")
    print('PASS: gateway unknown state survives previous-service rollback and restart')


if __name__ == '__main__': main()
