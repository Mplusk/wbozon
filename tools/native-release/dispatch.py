"""Forced SSH command. Accept one bounded deployment envelope via stdin."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

RUNTIME = Path('/run/wbozon-cd')
CONTROLLER = '/usr/local/libexec/wbozon-cd/cd.py'


def dispatch():
    if os.geteuid() != 0 or os.environ.get('SSH_ORIGINAL_COMMAND') != 'deploy':
        raise ValueError()
    signal.alarm(15)
    raw = sys.stdin.buffer.read(16385)
    signal.alarm(0)
    if len(raw) > 16384:
        raise ValueError()
    request = json.loads(raw)
    if set(request) != {'commit', 'run_id', 'attempt', 'artifact_id', 'build_attempt', 'tar_sha256', 'token'}:
        raise ValueError()
    run, attempt = request['run_id'], request['attempt']
    if not re.fullmatch('[1-9][0-9]{0,19}', run) or not re.fullmatch('[1-9][0-9]{0,4}', attempt):
        raise ValueError()
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid or RUNTIME.stat().st_mode & 0o077:
        raise ValueError()
    fd, name = tempfile.mkstemp(dir=RUNTIME, prefix='request-')
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
    unit = 'wbozon-deploy-' + run + '-' + attempt + '.service'
    try:
        result = subprocess.run(['/usr/bin/systemd-run', '--quiet', '--wait', '--collect', '--unit=' + unit,
            '--property=OnFailure=wbozon-cd-recover.service', '--property=RuntimeMaxSec=25min',
            '--property=UMask=0077', '--property=LimitCORE=0', '--property=TasksMax=128',
            '/usr/bin/python3', '-I', CONTROLLER, 'deploy', name], check=False)
        print('Deployment succeeded.' if result.returncode == 0 else 'Deployment failed. Check the deployment unit journal.')
        return int(result.returncode != 0)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        sys.exit(dispatch())
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit('Invalid or unavailable deployment request.')
