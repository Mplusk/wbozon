#!/usr/bin/env python3
"""Check two fixed, read-only marketplace API operations without printing data."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to a redirect target.
        return None


def required_env(name):
    value = os.environ.get(name, "")
    if not value or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(name)
    return value


def make_request(marketplace):
    if marketplace == "wb":
        return urllib.request.Request(
            "https://common-api.wildberries.ru/ping",
            headers={"Authorization": required_env("WB_API_TOKEN")},
            method="GET",
        )
    if marketplace == "ozon":
        # This POST reads a single product; it does not change the catalog.
        body = json.dumps({"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1}).encode()
        return urllib.request.Request(
            "https://api-seller.ozon.ru/v3/product/list",
            data=body,
            headers={"Client-Id": required_env("OZON_CLIENT_ID"),
                     "Api-Key": required_env("OZON_API_KEY"),
                     "Content-Type": "application/json"},
            method="POST",
        )
    raise ValueError("Неизвестный маркетплейс")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверить доступ к API, не выводя ключи и данные кабинета.")
    parser.add_argument("marketplace", choices=("wb", "ozon"))
    args = parser.parse_args(argv)
    try:
        request = make_request(args.marketplace)
    except ValueError as exc:
        print(f"Не задана или неверно задана переменная: {exc}", file=sys.stderr)
        return 2

    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            # No API response body, response headers, or credentials are printed.
            if 200 <= response.status < 300:
                print(f"Запрос завершился с HTTP {response.status}.")
                return 0
            print(f"Запрос завершился с HTTP {response.status}.", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        print(f"Запрос завершился с HTTP {exc.code}. Проверьте ключ и необходимые права.", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # Underlying exceptions can contain URLs or other sensitive details.
        print("Не удалось связаться с API. Проверьте сеть и настройки TLS.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
