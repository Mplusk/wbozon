"""Owner-only configuration for encrypted Yandex Disk backups."""

import contextlib
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag

try:
    import fcntl
except ImportError:
    fcntl = None


PROVIDER = "yandex_backup"
METADATA_PROVIDER = "yandex_backup_metadata"
STATUS_PATH = "/var/lib/wbozon-yandex-backup/status.json"


def validate_folder(public_url, disk_folder):
    if not isinstance(public_url, str) or len(public_url) > 512:
        raise ValueError("invalid public link")
    url = urlsplit(public_url)
    try:
        valid_port = url.port is None
    except ValueError:
        valid_port = False
    if (url.scheme != "https" or url.hostname not in ("disk.yandex.ru", "yadi.sk") or
            url.username or url.password or not valid_port or url.query or url.fragment or
            not re.fullmatch(r"/d/[A-Za-z0-9_-]{1,128}/?", url.path)):
        raise ValueError("invalid public link")
    if (not isinstance(disk_folder, str) or len(disk_folder) > 512 or not disk_folder.startswith("disk:/") or
            any(part in ("", ".", "..") for part in disk_folder[6:].split("/")) or
            any(ord(char) < 32 or ord(char) == 127 for char in disk_folder)):
        raise ValueError("invalid disk folder")


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,2048}", token):
        raise ValueError("invalid token")


def _store(vault, values):
    """Commit secret and display-only metadata together without changing the schema."""
    nonce = secrets.token_bytes(12)
    blob = nonce + vault.aead.encrypt(nonce, json.dumps(values).encode(), PROVIDER.encode())
    metadata = {key: values[key] for key in ("public_url", "disk_folder", "enabled")}
    metadata["version"] = hashlib.sha256(blob).hexdigest()
    nonce = secrets.token_bytes(12)
    meta_blob = nonce + vault.aead.encrypt(nonce, json.dumps(metadata).encode(), METADATA_PROVIDER.encode())
    with sqlite3.connect(vault.path, timeout=0.5) as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT OR REPLACE INTO vault VALUES (?,?)",
                       ((PROVIDER, blob), (METADATA_PROVIDER, meta_blob)))
        db.execute("DELETE FROM connection_checks WHERE provider IN (?,?)", (PROVIDER, METADATA_PROVIDER))


def _display_state(vault):
    """Read display-only metadata; never decrypt the stored OAuth token on GET."""
    with sqlite3.connect(vault.path, timeout=0.5) as db:
        rows = dict(db.execute("SELECT provider,blob FROM vault WHERE provider IN (?,?)", (PROVIDER, METADATA_PROVIDER)))
    if PROVIDER not in rows:
        return {}, False, False
    try:
        blob = bytes(rows[PROVIDER])
        meta_blob = bytes(rows[METADATA_PROVIDER])
        current = json.loads(vault.aead.decrypt(meta_blob[:12], meta_blob[12:], METADATA_PROVIDER.encode()))
        if (not isinstance(current, dict) or set(current) != {"public_url", "disk_folder", "enabled", "version"}
                or type(current["enabled"]) is not bool or not isinstance(current["version"], str)
                or not hmac.compare_digest(current["version"], hashlib.sha256(blob).hexdigest())):
            raise ValueError("invalid metadata")
        validate_folder(current["public_url"], current["disk_folder"])
    except (KeyError, TypeError, ValueError, InvalidTag, UnicodeError):
        return {}, True, True
    return current, True, False


@contextlib.contextmanager
def settings_lock(vault_path):
    if fcntl is None:
        raise OSError("settings lock is unavailable")
    path = Path(vault_path).absolute().parent / ".yandex-backup.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("unsafe settings lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save(vault, *, public_url, disk_folder, token, enabled):
    public_url, disk_folder, token = public_url.strip(), disk_folder.strip(), token.strip()
    if enabled not in ("yes", "no"):
        raise ValueError("invalid switch")
    validate_folder(public_url, disk_folder)
    if token:
        validate_token(token)
    with settings_lock(vault.path):
        previous = vault.read(PROVIDER) or {}
        selected_token = token or previous.get("token", "")
        validate_token(selected_token)
        _store(vault, {"public_url": public_url, "disk_folder": disk_folder,
                       "token": selected_token, "enabled": enabled == "yes"})


def disable(vault):
    with settings_lock(vault.path):
        previous = vault.read(PROVIDER)
        if previous:
            _store(vault, dict(previous, enabled=False))


def status(path=None):
    path = Path(path or os.environ.get("YANDEX_BACKUP_STATUS_FILE", STATUS_PATH))
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192 or info.st_mode & 0o022:
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def render(vault, csrf, fresh):
    current, configured, unknown = _display_state(vault)
    enabled = current.get("enabled") is True
    text = '<section class="mcp-card" aria-labelledby="backup-title"><h2 id="backup-title">Резервные копии на Яндекс Диск</h2>'
    label = "Состояние не определено" if unknown else ("Включены" if configured and enabled else "Выключены")
    text += '<p><strong>' + label + '</strong>. '
    text += 'Зашифрованная копия создаётся каждый день после 04:30 по Москве. Старые копии автоматически не удаляются.</p>'
    text += '<p>Ссылка на папку нужна для проверки её адреса и должна оставаться доступной. В папку загружаются только зашифрованные копии.</p>'
    report = status()
    if report:
        stamp = report.get("last_success")
        if isinstance(stamp, str) and re.fullmatch(r"[0-9T:./+Z-]{10,40}", stamp):
            text += '<p>Последняя подтверждённая копия: ' + html.escape(stamp) + '.</p>'
        if report.get("state") == "failed":
            text += '<p role="status">Последняя попытка завершилась ошибкой. Проверьте доступ к Диску, папку и свободное место.</p>'
    elif configured:
        text += '<p>Подтверждённых копий пока нет.</p>'
    csrf = html.escape(csrf, quote=True)
    if configured and (enabled or unknown):
        text += ('<form method="post" action="/settings/yandex-backup"><input type="hidden" name="csrf" value="' + csrf +
                 '"><input type="hidden" name="action" value="disable"><button>Отключить резервные копии</button></form>')
    if fresh:
        public_url = html.escape(str(current.get("public_url", "")), quote=True)
        folder = html.escape(str(current.get("disk_folder", "")), quote=True)
        text += ('<form method="post" action="/settings/yandex-backup"><input type="hidden" name="csrf" value="' + csrf +
                 '"><input type="hidden" name="action" value="save">'
                 '<label for="yandex-backup-link">Ссылка на папку Яндекс Диска</label>'
                 '<input id="yandex-backup-link" type="url" name="public_url" value="' + public_url + '" required>'
                 '<label for="yandex-backup-folder">Путь к папке на Диске</label>'
                 '<input id="yandex-backup-folder" name="disk_folder" value="' + folder + '" placeholder="disk:/Моя папка" required>'
                 '<label for="yandex-backup-token">OAuth-токен Яндекс Диска</label>'
                 '<input id="yandex-backup-token" type="password" name="token" autocomplete="off" maxlength="2048">'
                 '<p>Токен хранится зашифрованным и после сохранения не показывается. Оставьте поле пустым, чтобы сохранить прежний токен.</p>'
                 '<label for="yandex-backup-enabled">Автоматические копии</label>'
                 '<select id="yandex-backup-enabled" name="enabled"><option value="yes"' + (" selected" if enabled else "") + '>Включены</option>'
                 '<option value="no"' + (" selected" if not enabled else "") + '>Выключены</option></select>'
                 '<button>Сохранить настройки резервирования</button></form>')
    else:
        text += '<p>Чтобы изменить данные Диска или включить копии, войдите заново по ссылке из письма.</p>'
    return text + '</section>'
