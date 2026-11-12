"""Capture one Ozon Seller API product-list page, without exposing credentials.

The contract and pagination still need verification against Ozon's official
Seller API documentation and a real account. This is a partial snapshot, not
an assertion that all products were collected.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from marketplace_check import NoRedirect, make_request

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SOURCE = "https://api-seller.ozon.ru/v3/product/list"


class CollectionError(Exception):
    pass


def collect_page(opener=None):
    """Return one raw page and collection metadata; never log response/credentials."""
    try:
        request = make_request("ozon")
    except ValueError:
        raise CollectionError("Не заданы или неверно заданы OZON_CLIENT_ID и OZON_API_KEY") from None
    if opener is None:
        opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise CollectionError(f"Ozon вернул HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise CollectionError(f"Ozon вернул HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise CollectionError("Не удалось получить данные Ozon; проверьте сеть и настройки доступа") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CollectionError("Ответ Ozon слишком велик")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CollectionError("Ozon вернул некорректный JSON") from None
    if not isinstance(body, dict):
        raise CollectionError("Неожиданный формат ответа Ozon")
    return {
        "source": SOURCE,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "first_page_only_limit_1",
        "complete": False,
        "data": body,
    }


def save_new_snapshot(path, snapshot):
    """Create an owner-only file; never overwrite an existing file or symlink."""
    raw = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(Path(path), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    except BaseException:
        # Remove a partial new file on write failure; never touch an existing file.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сохранить одну страницу списка товаров Ozon; данные неполные.")
    parser.add_argument("--output", required=True, help="Путь к новому файлу вне репозитория")
    args = parser.parse_args(argv)
    try:
        snapshot = collect_page()
        save_new_snapshot(args.output, snapshot)
    except CollectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось создать файл снимка; укажите новый доступный путь", file=sys.stderr)
        return 1
    print("Сохранена одна страница. Снимок не является полным списком товаров.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
