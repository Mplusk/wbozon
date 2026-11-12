"""Run a server-side owner bot. No webhook is registered or accepted."""
import argparse
import os
from pathlib import Path
import sqlite3
import sys
import time

from gates.store import GateStore, GateError
from gates.worker import scan as scan_gate_requests
from gateway.store import GatewayStore
from .telegram import BotEngine, BotError, Cursor, TelegramClient


def _env_id(name):
    value = os.environ.get(name, "")
    if not value.isascii() or not value.isdigit():
        raise BotError("Не настроены идентификаторы владельца Telegram")
    number = int(value)
    if number <= 0 or number >= 2 ** 63:
        raise BotError("Не настроены идентификаторы владельца Telegram")
    return number


def _absolute_file_env(name, *, existing=False):
    value = os.environ.get(name, "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise BotError("Не настроены защищённые файлы состояния")
    if existing and not path.is_file():
        raise BotError("Не настроена база кандидатов")
    return path


def create_engine():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    user_id, chat_id = _env_id("TELEGRAM_OWNER_USER_ID"), _env_id("TELEGRAM_OWNER_CHAT_ID")
    gate_path = _absolute_file_env("TELEGRAM_GATE_DB", existing=True)
    gateway_path = _absolute_file_env("TELEGRAM_GATEWAY_DB")
    cursor_path = _absolute_file_env("TELEGRAM_CURSOR_DB")
    # Refuse aliasing cursor/gateway and candidate databases to prevent schema
    # collision or accidentally storing operational state in the wrong file.
    if len({str(p.resolve()) for p in (gate_path, gateway_path, cursor_path)}) != 3:
        raise BotError("Файлы состояния должны быть разными")
    client = TelegramClient(token)
    gateway = GatewayStore(gateway_path)
    gates = GateStore(gate_path, user_id, chat_id, gateway=gateway)
    return BotEngine(client, gates, gateway, Cursor(cursor_path), user_id, chat_id)


def run_iteration(engine):
    """Handle updates including stop, then prepare and send owner requests."""
    engine.poll_once()
    worker_failed = False
    try:
        gateway_status = engine.gateway.status()
        if not isinstance(gateway_status, dict) or type(gateway_status.get("stopped")) is not bool:
            raise BotError("Не удалось проверить режим шлюза")
        if gateway_status["stopped"]:
            return
        # The worker guards each candidate independently. Do not hold the
        # guard across the whole scan: /stop must not wait for a large queue.
        try:
            scan_gate_requests(engine.gates)
        except GateError:
            # Stop may occur after the first status check.
            if engine.gateway.status().get("stopped") is True:
                return
            worker_failed = True
        except (OSError, sqlite3.Error, ValueError):
            worker_failed = True
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        raise BotError("Не удалось проверить режим шлюза") from None
    # A stop may have arrived during the scan. No fresh deliveries after a
    # confirmed stop; an already-started network request cannot be recalled.
    try:
        status_after_scan = engine.gateway.status()
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        raise BotError("Не удалось проверить режим шлюза") from None
    if not isinstance(status_after_scan, dict) or type(status_after_scan.get("stopped")) is not bool:
        raise BotError("Не удалось проверить режим шлюза")
    if status_after_scan["stopped"]:
        return
    try:
        engine.deliver_pending_once()
    except (GateError, OSError, sqlite3.Error, ValueError) as exc:
        raise BotError("Не удалось доставить ожидающие решения") from None
    if worker_failed:
        raise BotError("Не удалось подготовить ожидающие решения")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Бот владельца: безопасный long-polling без webhook")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("poll", help="принимать обновления Telegram")
    p.add_argument("--once", action="store_true", help="один вызов getUpdates")
    send = sub.add_parser("send", help="показать владельцу существующий запрос целиком")
    send.add_argument("--nonce", required=True)
    status = sub.add_parser("delivery-status", help="показать статус доставки запроса")
    status.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = create_engine()
        if args.command == "delivery-status":
            print(engine.cursor.delivery_status(args.nonce) or "not_started")
        elif args.command == "send":
            engine.send_gate_request(args.nonce)
            print("Запрос на решение отправлен владельцу.")
        else:
            while True:
                try:
                    run_iteration(engine)
                except BotError:
                    if args.once:
                        raise
                    print("Ошибка при опросе Telegram; повтор через 5 секунд.", file=sys.stderr)
                    time.sleep(5)
                if args.once:
                    break
        return 0
    except (BotError, GateError, OSError, sqlite3.Error, ValueError):
        print("Бот не запущен или операция не выполнена. Проверьте серверную конфигурацию и доступы.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if engine is not None:
            engine.gates.close()


if __name__ == "__main__":
    sys.exit(main())
