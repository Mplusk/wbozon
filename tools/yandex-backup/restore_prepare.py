"""Build a replacement set without touching the running application's files."""
from pathlib import Path
import os
import shutil

from restore_archive import InvalidBackup, _check_database
import restore_files
import restore_transform


SCOPED_SCHEMA = "01adb0bd15e847f9d1fadccb4b45169c0271bb00a15f7667d69d796787278723"
CALCULATOR_SCHEMAS = {
    "c4971f834854e57c45466f15ae4c32a69c7e01de08b2eb16906519d45ce9cdca",
    "7732a71be6816db4486213c8fb437d0b04dcfd77f9e17da2dea8cb7c00c5fbbd",
}
ACTION_SCHEMAS = {
    "990bf1ca3f3cd956f47988c1b16ad9bfe6d60ca16c2fc8fb5eed8f6933bf7ac1",
    "77095318a0bc7ac1136afb4be823e28a6c5b98c0bde07296708183e5cde1f0fd",
}
HISTORY_SCHEMAS = {"e5dddd662a311edbd0df5fa53a7a7998e99db4f8b78a536ece2f3b7488394b89"}


def _snapshot(directory, records, name):
    record = records[name]
    return Path(directory) / record["file"] if record else None


def _compatible(archived, current, allowed=None):
    if current is not None:
        expected = {_check_database(current)}
        if allowed:
            expected |= allowed
    elif allowed:
        expected = allowed
    else:
        raise InvalidBackup("current_database_schema_unavailable")
    _check_database(archived, expected)


def _private(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def build(files, before, records, current_key, work):
    """Return fixed target -> prepared path/None after all validation succeeds.

    `before` is a quiescent snapshot; its records must first be authenticated by
    restore_files. Limits and the latest operation ledger are never rolled back.
    """
    before, work = Path(before), Path(work)
    restore_files._validate_records(before, records)
    if not work.is_dir() or work.is_symlink() or any(work.iterdir()):
        raise InvalidBackup("unsafe_staging_directory")
    current_vault = _snapshot(before, records, "vault.sqlite")
    current_auth = _snapshot(before, records, "auth.sqlite")
    if current_vault is None or current_auth is None:
        raise InvalidBackup("current_database_missing")
    _compatible(files["databases/vault.sqlite"], current_vault)
    old_key = files["secrets/vault-key"].read_bytes()
    if not isinstance(current_key, bytes) or len(current_key) != 32:
        raise InvalidBackup("invalid_vault_key")
    vault = work / "vault.sqlite"
    shutil.copyfile(files["databases/vault.sqlite"], vault)
    _private(vault)
    restore_transform.prepare_vault(vault, current_vault, old_key, current_key)
    auth = work / "auth.sqlite"
    restore_transform.prepare_auth(current_auth, auth)
    _private(auth)
    prepared = {"auth.sqlite": auth, "vault.sqlite": vault}

    for suffix in (".avito-keys", ".catalog-keys"):
        name = "vault.sqlite" + suffix
        source = _snapshot(before, records, name)
        if source is None:
            source = files.get("databases/" + name)
            if source is not None:
                _compatible(source, None, {SCOPED_SCHEMA})
        if source is None:
            prepared[name] = None
        else:
            dest = work / name
            shutil.copyfile(source, dest)
            _private(dest)
            restore_transform.revoke_sidecar(dest)
            prepared[name] = dest

    name = "vault.sqlite.market-actions"
    current = _snapshot(before, records, name)
    archived = files.get("databases/" + name)
    if current is None and archived is not None:
        _compatible(archived, None, ACTION_SCHEMAS)
    dest = work / name
    restore_transform.prepare_actions(current, archived, dest, old_key, current_key)
    _private(dest)
    prepared[name] = dest

    name = "vault.sqlite.cost-calculator"
    source = files.get("databases/" + name)
    if source is not None:
        _compatible(source, _snapshot(before, records, name), CALCULATOR_SCHEMAS)
    prepared[name] = source

    history = files.get("databases/market-reports/history-v2.db")
    if history is not None:
        _compatible(history, _snapshot(before, records, ".market-reports/history-v2.db"), HISTORY_SCHEMAS)
    prepared[".market-reports/history-v2.db"] = history
    prepared[".market-reports/history-v2.key"] = files.get("secrets/history-v2.key") if history else None
    return prepared
