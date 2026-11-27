"""Owner-only interface to the privileged, independently installed restore broker."""

import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import html
import json
import re
import secrets
import socket
import struct
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from ui import document


SOCKET_PATH = "/run/wbozon-restore/restore.sock"
NAME = re.compile(r"wbozon-([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{12}\.tar\.gz\.age\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{22}\Z")
MAX_BODY = 32768
MAX_REPLY = 262144
STATUS_PATH = Path("/var/lib/wbozon-yandex-backup/restore-status.json")


class RestoreError(ValueError):
    pass


def _read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        data = sock.recv(size - len(result))
        if not data:
            raise RestoreError("service_unavailable")
        result.extend(data)
    return bytes(result)


def broker(request):
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_BODY:
        raise RestoreError("invalid_request")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(20)
            sock.connect(SOCKET_PATH)
            sock.sendall(struct.pack("!I", len(payload)) + payload)
            size = struct.unpack("!I", _read_exact(sock, 4))[0]
            if not 0 < size <= MAX_REPLY:
                raise RestoreError("service_unavailable")
            response = json.loads(_read_exact(sock, size))
    except (OSError, TimeoutError, ValueError, UnicodeError, struct.error):
        raise RestoreError("service_unavailable") from None
    if not isinstance(response, dict) or response.get("status") not in ("ok", "accepted"):
        raise RestoreError("service_unavailable")
    return response


def parse_multipart(env):
    """Parse only the small fixed form; never retain uploads in a temporary file."""
    content_type = env.get("CONTENT_TYPE", "")
    if (not isinstance(content_type, str) or len(content_type) > 200 or
            not re.fullmatch(r'multipart/form-data;\s*boundary=(?:"[A-Za-z0-9\x27()+_,./:=?-]{1,70}"|[A-Za-z0-9\x27()+_,./:=?-]{1,70})', content_type, re.I)):
        raise RestoreError("invalid_form")
    try:
        length = int(env.get("CONTENT_LENGTH", "0"))
        if not 0 < length <= MAX_BODY:
            raise RestoreError("invalid_form")
        raw = env["wsgi.input"].read(length)
        if len(raw) != length:
            raise RestoreError("invalid_form")
        prefix = ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii")
        message = BytesParser(policy=policy.default).parsebytes(prefix + raw)
        if message.defects or not message.is_multipart():
            raise RestoreError("invalid_form")
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if (part.defects or part.is_multipart() or part.get_content_disposition() != "form-data" or
                    name not in {"csrf", "request_id", "archive", "confirm", "identity"} or name in fields or
                    part.get("Content-Transfer-Encoding", "").lower() not in ("", "binary", "8bit")):
                raise RestoreError("invalid_form")
            value = part.get_payload(decode=True)
            if not isinstance(value, bytes):
                raise RestoreError("invalid_form")
            if name == "identity":
                if not part.get_filename() or not 32 <= len(value) <= 8192 or b"\0" in value:
                    raise RestoreError("invalid_identity")
                fields[name] = value
            else:
                if part.get_filename() is not None or len(value) > 256:
                    raise RestoreError("invalid_form")
                fields[name] = value.decode("utf-8")
        if set(fields) != {"csrf", "request_id", "archive", "confirm", "identity"}:
            raise RestoreError("invalid_form")
        if not REQUEST_ID.fullmatch(fields["request_id"]) or not NAME.fullmatch(fields["archive"]) or fields["confirm"] != "yes":
            raise RestoreError("invalid_form")
        return fields
    except (KeyError, TypeError, ValueError, UnicodeError, OSError):
        raise RestoreError("invalid_form") from None


def _stamp(filename):
    match = NAME.fullmatch(filename) if isinstance(filename, str) else None
    if not match:
        raise RestoreError("invalid_listing")
    try:
        moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        try:
            moment, zone = moment.astimezone(ZoneInfo("Europe/Moscow")), "МСК"
        except ZoneInfoNotFoundError:
            zone = "UTC"
        return moment.strftime("%d.%m.%Y, %H:%M") + " " + zone
    except (ValueError, OverflowError):
        raise RestoreError("invalid_listing") from None


def button(csrf, fresh):
    if not fresh:
        return '<p class="settings-help">Для восстановления войдите заново по ссылке из письма.</p>'
    return ('<form method="post" action="/settings/yandex-restore/list">'
            '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
            '<button class="settings-secondary" type="submit">Восстановить из копии</button></form>')


def provider_hold(csrf, fresh):
    import market_cooldown
    path = market_cooldown.BOOTSTRAP_HOLD
    if path is None:
        return ""
    try:
        path.lstat()
    except FileNotFoundError:
        return ""
    except OSError:
        return '<p class="settings-warning">Не удалось проверить состояние запросов к маркетплейсам.</p>'
    message = ('<p class="settings-warning" role="status">После восстановления обращения к маркетплейсам остановлены. '
               'Перед возобновлением проверьте ограничения площадок и операции с неизвестным результатом. '
               'Отправка изменений останется выключенной отдельно.</p>')
    if not fresh:
        return message + '<p class="settings-help">Для возобновления войдите заново по ссылке из письма.</p>'
    return (message + '<form method="post" action="/settings/yandex-restore/resume">'
            '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
            '<label class="restore-check"><input type="checkbox" name="confirm" value="yes" required> '
            'Я проверил ограничения и операции на площадках.</label>'
            '<button class="settings-secondary" type="submit">Разрешить запросы к маркетплейсам</button></form>')


def status_text():
    try:
        if STATUS_PATH.stat().st_size > 4096:
            return ""
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or type(value.get("at")) is not int:
            return ""
        stamp = datetime.fromtimestamp(value["at"], timezone.utc)
        try:
            stamp = stamp.astimezone(ZoneInfo("Europe/Moscow"))
        except ZoneInfoNotFoundError:
            pass
        shown = stamp.strftime("%d.%m.%Y, %H:%M")
        state = value.get("state")
        if state == "completed":
            message = "Восстановление завершено"
        elif state == "failed":
            reasons = {
                "decryption_failed": "Не удалось расшифровать копию. Проверьте файл закрытого ключа",
                "invalid_private_key": "Файл закрытого ключа не распознан",
                "unsupported_database_schema": "Эта копия несовместима с текущей версией кабинета",
                "vault_key_mismatch": "Копия не прошла проверку ключа хранилища",
                "history_key_mismatch": "Копия не прошла проверку архива истории",
            }
            message = reasons.get(value.get("reason"), "Восстановление не завершилось. Проверьте копию и доступ к Диску")
        elif state == "unknown":
            message = "Результат восстановления неизвестен. Проверьте состояние сервера перед повторным запуском"
        elif state == "started":
            message = ("Восстановление выполняется" if time.time() - value["at"] < 15 * 60 else
                       "Результат восстановления неизвестен. Проверьте состояние сервера перед повторным запуском")
        else:
            return ""
        return '<p class="settings-help" role="status">' + html.escape(message) + ' · ' + shown + '</p>'
    except (OSError, ValueError, TypeError, OverflowError):
        return ""


def select_page(csrf, backups):
    if not isinstance(backups, list) or len(backups) > 2000:
        raise RestoreError("invalid_listing")
    options = []
    for row in backups:
        if not isinstance(row, dict) or set(row) != {"name", "size"} or type(row["size"]) is not int or row["size"] <= 0:
            raise RestoreError("invalid_listing")
        label = _stamp(row["name"]) + " · " + str((row["size"] + 1023) // 1024) + " КБ"
        options.append('<option value="' + html.escape(row["name"], quote=True) + '">' + html.escape(label) + '</option>')
    if not options:
        return document("Восстановление", '<main id="content" class="calculator"><h1>Восстановление из копии</h1>'
                        '<p>В выбранной папке нет подходящих копий.</p><p><a href="/settings">Вернуться в настройки</a></p></main>')
    request_id = secrets.token_urlsafe(16)
    return document("Восстановление", '<main id="content" class="calculator restore-page"><h1>Восстановление из копии</h1>'
        '<p>Выберите копию по дате и укажите закрытый ключ, которым она зашифрована.</p>'
        '<div class="calc-panel"><p>Карточки, история и ключи площадок будут восстановлены на дату копии. Текущие ограничения запросов и журнал операций сохранятся. Перед заменой текущие данные сохранятся для отката.</p>'
        '<p>Кабинет временно отключится. Затем потребуется войти заново и создать новые ключи MCP. Запросы к маркетплейсам и отправка изменений будут остановлены до вашей проверки.</p>'
        '<form class="settings-form" method="post" action="/settings/yandex-restore/submit" enctype="multipart/form-data">'
        '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
        '<input type="hidden" name="request_id" value="' + request_id + '">'
        '<div class="field"><label for="restore-archive">Резервная копия</label><select id="restore-archive" name="archive">' + ''.join(options) + '</select></div>'
        '<div class="field"><label for="restore-identity">Файл закрытого ключа</label><input id="restore-identity" name="identity" type="file" aria-describedby="restore-key-help" required>'
        '<p class="restore-help" id="restore-key-help">Выберите закрытый ключ без парольной фразы, не файл .pub. Ключ передаётся серверу для расшифровки и не сохраняется в настройках.</p></div>'
        '<label class="restore-check"><input type="checkbox" name="confirm" value="yes" required><span>Я понимаю, что данные после выбранной даты будут заменены.</span></label>'
        '<div class="calc-actions"><button type="submit">Восстановить выбранную копию</button></div></form></div>'
        '<p><a href="/settings">Отмена</a></p></main>')


def started_page():
    return document("Восстановление", '<main id="content" class="calculator"><h1>Восстановление запущено</h1>'
                    '<p>Копия будет проверена перед заменой данных. Кабинет временно отключится; не запускайте восстановление повторно.</p>'
                    '<p>После завершения откройте <a href="/settings">настройки</a> и при необходимости войдите заново.</p></main>')


def start_request(fields):
    return {"action": "restore", "request_id": fields["request_id"], "archive": fields["archive"],
            "identity": base64.b64encode(fields["identity"]).decode("ascii")}
