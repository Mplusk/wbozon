"""Owner opt-in for a catalogue cache scoped to the portal's vault directory.

No provider requests or n8n changes occur here. An interrupted setup is
preserved as an orphan; it is not recreated or silently reset.
"""
import os
import stat
from contextlib import contextmanager

import content_factory_budget_vault as budget_vault
import content_factory_model_catalog_config as config

FEATURE_AVAILABLE = True  # Owner setup is still required before any provider request.
DIRECTORY = '.content-factory-model-catalog'
LOCK = '.content-factory-model-catalog.lock'
SLOT = 'content_factory_catalog_setup'


@contextmanager
def _lock(parent):
    path = os.path.join(parent, LOCK)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    try:
        st, link = os.fstat(fd), os.lstat(path)
        if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(link.st_mode) or st.st_nlink != 1
                or (st.st_ino, st.st_dev) != (link.st_ino, link.st_dev)):
            raise ValueError('Invalid catalogue lock')
        if os.name == 'posix' and (st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077):
            raise ValueError('Invalid catalogue lock')
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _checked_marker(marker):
    if (not isinstance(marker, dict) or set(marker) != {'version', 'enabled'}
            or type(marker['version']) is not int or marker['version'] != 1
            or marker['enabled'] is not True):
        raise ValueError('Invalid catalogue setup')


def _arguments(environ, parent, vault):
    values = dict(environ)
    values.update(CF_MODEL_CATALOG_ENABLED='1', CF_MODEL_CATALOG_STAGING_VERIFIED='1',
                  CF_MODEL_CATALOG_DATA_DIR=os.path.join(parent, DIRECTORY), VAULT_PATH=vault.path)
    return values


def _require_catalogue(parent):
    path = os.path.join(parent, DIRECTORY, config.CATALOG)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1:
        raise ValueError('Existing catalogue missing or invalid')


def setup_pending(vault, *, expected_vault_path=budget_vault.VAULT_PATH):
    """Indicate any prior setup or orphan without initializing state.

    An unreadable vault or path is treated as pending so the UI never offers a
    repeated setup while state cannot be reconciled.
    """
    if FEATURE_AVAILABLE is not True:
        return False
    try:
        parent = budget_vault._base(vault, expected_vault_path)
        if vault.read(SLOT) is not None:
            return True
        try:
            os.lstat(os.path.join(parent, DIRECTORY))
        except FileNotFoundError:
            return False
        return True
    except Exception:
        return True


def enable(vault, environ, secret_reader, *, confirmed=False, expected_vault_path=budget_vault.VAULT_PATH):
    """The caller must independently enforce owner session, CSRF and Origin."""
    if FEATURE_AVAILABLE is not True or confirmed is not True:
        raise PermissionError('Catalogue setup unavailable')
    parent = budget_vault._base(vault, expected_vault_path)
    with _lock(parent):
        marker = vault.read(SLOT)
        directory = os.path.join(parent, DIRECTORY)
        if marker is not None:
            _checked_marker(marker)
            raise ValueError('Catalogue already configured')
        else:
            if os.path.lexists(directory):
                raise ValueError('Unreconciled catalogue state')
            os.mkdir(directory, 0o700)
        # Never remove a directory after partial initialization: it may contain
        # persistent state. Leave it for explicit reconciliation instead.
        configured = config.configure(_arguments(environ, parent, vault), vault, secret_reader)
        if configured is None:
            raise ValueError('Catalogue unavailable')
        vault.save(SLOT, {'version': 1, 'enabled': True})
        return configured


def load(vault, environ, secret_reader, *, expected_vault_path=budget_vault.VAULT_PATH):
    if FEATURE_AVAILABLE is not True:
        return None
    parent = budget_vault._base(vault, expected_vault_path)
    marker = vault.read(SLOT)
    if marker is None:
        return None
    _checked_marker(marker)
    _require_catalogue(parent)
    return config.configure(_arguments(environ, parent, vault), vault, secret_reader, read_only=True)
