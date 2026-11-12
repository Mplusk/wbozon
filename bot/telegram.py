"""Telegram Bot API long-polling adapter for owner decisions.

Network access is restricted to the fixed official Telegram API host and fixed
methods. Owner identity and the authoritative decision/stop stores are checked
again on the server. This module does not register or expose a webhook.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
import unicodedata

from gates.store import GateError, GateStore

TOKEN_PATTERN = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}\Z")
CALLBACK_PATTERN = re.compile(r"g:([A-Za-z0-9_-]{24}):([ar])\Z")
ALLOWED_METHODS = {"getUpdates", "sendMessage", "answerCallbackQuery"}
MAX_RESPONSE = 1024 * 1024


class BotError(Exception):
    """Fixed safe error: never propagate a token, URL or raw Telegram response."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramClient:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise BotError("Некорректный токен Telegram")
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def call(self, method, payload):
        if method not in ALLOWED_METHODS:
            raise BotError("Недопустимый метод Telegram")
        # The token is embedded in Telegram's official URL. Do not print,
        # log or propagate this request, response, underlying exception or URL.
        url = "https://api.telegram.org/bot" + self._token + "/" + method
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=40) as response:
                if not 200 <= response.status < 300:
                    raise BotError("Ошибка Telegram API")
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BotError("Ответ Telegram превышает допустимый размер")
            answer = json.loads(raw)
            if not isinstance(answer, dict) or answer.get("ok") is not True or "result" not in answer:
                raise BotError("Ошибка Telegram API")
            return answer["result"]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BotError("Не удалось выполнить запрос Telegram API") from None

    def updates(self, offset):
        payload = {"timeout": 25, "limit": 100, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise BotError("Некорректный ответ Telegram")
        return result

    def answer(self, callback_id, text):
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2 ** 63


class Cursor:
    """Private durable offset and exclusive process lock for one polling bot."""
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir() or self.path.is_symlink():
            raise BotError("Недопустимый путь состояния бота")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def lock(self):
        return _CursorLock(self)

    def read(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS telegram_cursor (id INTEGER PRIMARY KEY CHECK(id=1), next_offset INTEGER NOT NULL)")
            row = con.execute("SELECT next_offset FROM telegram_cursor WHERE id=1").fetchone()
            return row[0] if row else None

    def save(self, next_offset):
        if not _positive_id(next_offset):
            raise BotError("Некорректный offset")
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS telegram_cursor (id INTEGER PRIMARY KEY CHECK(id=1), next_offset INTEGER NOT NULL)")
            con.execute("INSERT INTO telegram_cursor(id,next_offset) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET next_offset=excluded.next_offset", (next_offset,))


    def start_delivery(self, nonce):
        """Persist the outbound intent before a non-idempotent Telegram call."""
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS telegram_delivery (nonce TEXT PRIMARY KEY, state TEXT NOT NULL)")
            cur = con.execute("INSERT OR IGNORE INTO telegram_delivery VALUES (?, 'started')", (nonce,))
            return cur.rowcount == 1

    def finish_delivery(self, nonce, state):
        if state not in {"sent", "unknown", "blocked"}:
            raise BotError("Некорректный статус доставки")
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE telegram_delivery SET state=? WHERE nonce=? AND state='started'", (state, nonce))

    def delivery_status(self, nonce):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS telegram_delivery (nonce TEXT PRIMARY KEY, state TEXT NOT NULL)")
            row = con.execute("SELECT state FROM telegram_delivery WHERE nonce=?", (nonce,)).fetchone()
            return row[0] if row else None


class _CursorLock:
    def __init__(self, cursor):
        self.cursor = cursor
        self.fd = None

    def __enter__(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(self.cursor.lock_path, flags, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise BotError("Другой процесс бота уже работает") from None
        return self.cursor

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)


class BotEngine:
    def __init__(self, client, gates, gateway, cursor, owner_user_id, owner_chat_id):
        if not _positive_id(owner_user_id) or not _positive_id(owner_chat_id):
            raise BotError("Не заданы идентификаторы владельца")
        if isinstance(gates, GateStore):
            gate_gateway = gates.gateway
            if gate_gateway is None or getattr(gate_gateway, "path", None) != getattr(gateway, "path", None):
                raise BotError("Бот и реестр используют разные базы шлюза")
        self.client = client
        self.gates = gates
        self.gateway = gateway
        self.cursor = cursor
        self.owner_user_id = owner_user_id
        self.owner_chat_id = owner_chat_id

    def _owner(self, message, sender):
        chat = message.get("chat") if isinstance(message, dict) else None
        return (isinstance(sender, dict) and sender.get("is_bot") is False
                and type(sender.get("id")) is int and sender["id"] == self.owner_user_id
                and isinstance(chat, dict) and chat.get("type") == "private"
                and type(chat.get("id")) is int and chat["id"] == self.owner_chat_id)

    def _stopped(self):
        """Fail closed when the persistent gateway stop state cannot be read."""
        try:
            state = self.gateway.status()
        except (OSError, sqlite3.Error, ValueError):
            raise BotError("Не удалось проверить режим шлюза") from None
        if not isinstance(state, dict) or type(state.get("stopped")) is not bool:
            raise BotError("Не удалось проверить режим шлюза")
        return state["stopped"]

    def _best_effort_answer(self, callback_id, text):
        try:
            self.client.answer(callback_id, text)
        except BotError:
            pass  # Decision is already persisted: never undo it or log secret-bearing errors.

    def _best_effort_message(self, text):
        try:
            self.client.message(self.owner_chat_id, text)
        except BotError:
            pass

    def _record_decision(self, nonce, approve, update_id):
        """Called under the gateway coordination lock, without network calls."""
        if self._stopped():
            return "Система остановлена; решение не принято"
        try:
            result = self.gates.decide(nonce, user_id=self.owner_user_id, chat_id=self.owner_chat_id,
                                       update_id=update_id, approve=approve)
        except GateError:
            return "Запрос не принят или уже недействителен"
        if isinstance(result, dict) and result.get("status") in {"approved", "rejected", "duplicate"}:
            if approve:
                if self._stopped():
                    return "Решение записано; переход остановлен"
                try:
                    transition = self.gates.transition_decided(nonce)
                except GateError:
                    return "Решение записано, переход не выполнен"
                if not isinstance(transition, dict) or transition.get("status") not in {"advanced", "already_advanced", "rights_recorded"}:
                    raise BotError("Не подтверждено состояние перехода")
            return "Решение записано"
        return "Запрос не принят или уже недействителен"

    def process(self, update):
        if not isinstance(update, dict) or not _positive_id(update.get("update_id")):
            raise BotError("Некорректное обновление Telegram")
        update_id = update["update_id"]
        if "callback_query" in update:
            q = update["callback_query"]
            if not isinstance(q, dict) or not self._owner(q.get("message"), q.get("from")):
                return
            cb_id, data = q.get("id"), q.get("data")
            if not isinstance(cb_id, str) or not cb_id or len(cb_id) > 256 or not isinstance(data, str):
                return
            match = CALLBACK_PATTERN.fullmatch(data)
            if not match:
                self._best_effort_answer(cb_id, "Запрос недействителен")
                return
            # Coordinate with the stop path using the same protected lock.
            # No Telegram requests occur while the critical section is held.
            try:
                if self._stopped():
                    response = "Система остановлена; решение не принято"
                else:
                    try:
                        with self.gates.coordinated():
                            response = self._record_decision(match.group(1), match.group(2) == "a", update_id)
                    except GateError:
                        # Stop may race the initial check. A confirmed stop is a
                        # terminal rejection; unavailable state remains retryable.
                        if self._stopped():
                            response = "Система остановлена; решение не принято"
                        else:
                            raise BotError("Не удалось проверить режим шлюза") from None
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                raise BotError("Не удалось проверить режим шлюза") from None
            self._best_effort_answer(cb_id, response)
        elif "message" in update:
            message = update["message"]
            if not isinstance(message, dict) or not self._owner(message, message.get("from")):
                return
            if message.get("text") == "/stop":
                result = self.gateway.stop_and_probe("tg-update-" + str(update_id))
                if not isinstance(result, dict) or result.get("stopped") is not True or result.get("probe_denied") is not True:
                    raise BotError("Не подтверждена остановка записи")
                self._best_effort_message("Запись остановлена; проверочный запрос отклонён.")

    def _gate_message(self, nonce):
        """Prepare all bound fields for display, or fail without truncating."""
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce):
            raise BotError("Некорректный идентификатор решения")
        try:
            request = self.gates.describe(nonce)
        except GateError:
            raise BotError("Решение недоступно или истекло") from None
        if not isinstance(request, dict) or request.get("nonce") != nonce:
            raise BotError("Некорректный запрос решения")
        # The whole request must be visible; never hide or truncate bound fields.
        visible = {k: request.get(k) for k in ("candidate_id", "revision_id", "gate", "payload", "expires_at")}
        if (not isinstance(visible["candidate_id"], str) or not visible["candidate_id"]
                or not _positive_id(visible["revision_id"])
                or visible["gate"] not in {"sample", "launch", "publication", "rights"}
                or not isinstance(visible["payload"], dict) or not visible["payload"]
                or not _positive_id(visible["expires_at"])):
            raise BotError("Некорректный запрос решения")
        try:
            data = json.dumps(visible, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        except (TypeError, ValueError):
            raise BotError("Некорректный запрос решения") from None
        # Unicode format controls can make the visible order differ from stored data.
        if any(unicodedata.category(ch) == "Cf" for ch in data):
            raise BotError("Пакет содержит недопустимые управляющие символы")
        text = "Решение владельца. Проверьте весь пакет:\n" + data
        if len(text.encode("utf-8")) > 4096:
            raise BotError("Пакет слишком велик для полного отображения в Telegram")
        keyboard = {"inline_keyboard": [[
            {"text": "Подтвердить", "callback_data": "g:" + nonce + ":a"},
            {"text": "Отклонить", "callback_data": "g:" + nonce + ":r"},
        ]]}
        return text, keyboard

    def send_gate_request(self, nonce):
        """Send an explicit request once; unknown results require reconciliation."""
        with self.cursor.lock() as cursor:
            if cursor.delivery_status(nonce) is not None:
                raise BotError("Запрос уже отправлен или его исход неизвестен")
            text, keyboard = self._gate_message(nonce)
            if not cursor.start_delivery(nonce):
                raise BotError("Запрос уже отправлен или его исход неизвестен")
            try:
                self.client.message(self.owner_chat_id, text, keyboard)
            except BotError:
                cursor.finish_delivery(nonce, "unknown")
                raise
            cursor.finish_delivery(nonce, "sent")

    def deliver_pending_once(self):
        """Deliver each pending request at most once, never retry unknown sends."""
        with self.cursor.lock() as cursor:
            try:
                pending = self.gates.list_pending()
            except GateError:
                raise BotError("Не удалось получить ожидающие запросы") from None
            if not isinstance(pending, list):
                raise BotError("Некорректный список запросов")
            for item in pending:
                nonce = item.get("nonce") if isinstance(item, dict) else item
                if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce):
                    raise BotError("Некорректный запрос решения")
                if cursor.delivery_status(nonce) is not None:
                    continue
                try:
                    text, keyboard = self._gate_message(nonce)
                except BotError:
                    # May be expired/stale or unrepresentable. Never send a
                    # truncated request; record the operational failure.
                    if cursor.start_delivery(nonce):
                        cursor.finish_delivery(nonce, "blocked")
                    continue
                if not cursor.start_delivery(nonce):
                    continue
                try:
                    self.client.message(self.owner_chat_id, text, keyboard)
                except BotError:
                    # The server may have received the request even when its
                    # response was lost. Reconcile before issuing a fresh one.
                    cursor.finish_delivery(nonce, "unknown")
                    continue
                cursor.finish_delivery(nonce, "sent")

    def poll_once(self):
        # No process should acknowledge an update until its durable effects have
        # been recorded. On crash, gates and gateway must deduplicate update_id.
        try:
            with self.cursor.lock() as cursor:
                offset = cursor.read()
                updates = self.client.updates(offset)
                previous = offset - 1 if offset is not None else 0
                # Validate order before processing any side effect. Telegram
                # update_id is unique and monotonically increasing per batch.
                check_previous = previous
                for update in updates:
                    if not isinstance(update, dict) or not _positive_id(update.get("update_id")):
                        raise BotError("Некорректное обновление Telegram")
                    uid = update["update_id"]
                    if uid < check_previous:
                        raise BotError("Нарушен порядок обновлений Telegram")
                    check_previous = uid
                # Emergency stop must not be trapped behind an earlier update
                # whose candidate database is temporarily unavailable. Perform
                # authenticated stops first, without advancing the cursor; stop
                # is idempotent and may safely replay after a crash.
                handled_stops = set()
                for update in updates:
                    uid = update["update_id"]
                    message = update.get("message")
                    if (uid > previous and isinstance(message, dict) and message.get("text") == "/stop"
                            and self._owner(message, message.get("from"))):
                        self.process(update)
                        handled_stops.add(uid)
                for update in updates:
                    uid = update["update_id"]
                    if uid == previous:
                        continue
                    if uid not in handled_stops:
                        self.process(update)
                    cursor.save(uid + 1)
                    previous = uid
        except BotError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise BotError("Не удалось обработать обновление Telegram") from None
