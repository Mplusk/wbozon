#!/usr/bin/python3
"""Prototype launcher: pin the active sealed release once before exec.

Installation is blocked until systemd and deployment/pruning integration have
been independently reviewed and tested on an isolated Linux host.
"""
import os
import re
import stat
from pathlib import Path

ROOT = Path('/opt/3dprinta-portal')
_RELEASE = re.compile(r'[0-9a-f]{40}(?:-[1-9][0-9]{0,19}-[1-9][0-9]{0,4})?\Z')
_ENV = ('DATABASE_PATH', 'VAULT_PATH', 'VAULT_KEY_FILE',
        'CF_MODEL_CATALOG_DATA_DIR', 'CF_MODEL_CATALOG_ENABLED',
        'CF_MODEL_CATALOG_STAGING_VERIFIED', 'CF_GATEWAY_DATA_DIR',
        'CF_IMMUTABLE_DATA_DIR', 'CREDENTIALS_DIRECTORY')


def _owned(path, *, directory):
    st = path.lstat()
    if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
        raise ValueError('Untrusted release file')
    if directory and not stat.S_ISDIR(st.st_mode):
        raise ValueError('Invalid release directory')
    if not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
        raise ValueError('Invalid release file')


def validate_candidate(root, candidate):
    """Pure path constraint; caller verifies root ownership and symlinks."""
    if candidate.parent != root / 'releases' or not _RELEASE.fullmatch(candidate.name):
        raise ValueError('Invalid active release')
    return candidate


def resolve_release(root=ROOT):
    _owned(root.parent, directory=True)
    _owned(root, directory=True)
    _owned(root / 'releases', directory=True)
    current = root / 'current'
    link = current.lstat()
    if not stat.S_ISLNK(link.st_mode) or link.st_uid != 0:
        raise ValueError('Invalid active link')
    candidate = validate_candidate(root, current.resolve(strict=True))
    for path in (candidate, candidate / 'service', candidate / 'venv', candidate / 'venv' / 'bin'):
        _owned(path, directory=True)
    python = candidate / 'venv' / 'bin' / 'python'
    script = candidate / 'service' / 'content_factory_model_catalog_cli.py'
    _owned(python, directory=False)
    _owned(script, directory=False)
    if not os.access(python, os.X_OK): raise ValueError('Runtime unavailable')
    return candidate, python, script


def main():
    try:
        release, python, script = resolve_release()
        # No untrusted PYTHONPATH, user site-packages, proxy, or arbitrary env.
        environment = {key: os.environ[key] for key in _ENV if key in os.environ}
        environment.update(PATH='/usr/bin:/bin', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        os.chdir(release / 'service')
        os.execve(python, [str(python), '-B', str(script)], environment)
    except Exception:
        # Avoid exposing secret-bearing environment or raw exceptions.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
