"""Server-side creation of a gate request; no Telegram or marketplace access."""
import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys
from gateway.store import GatewayStore
from .store import GateError, GateStore


def main(argv=None):
    parser = argparse.ArgumentParser(description='Создать серверный запрос решения владельца')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--gateway-db', required=True, type=Path)
    parser.add_argument('--candidate-id', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--gate', choices=('sample', 'launch', 'publication', 'rights'), required=True)
    args = parser.parse_args(argv)
    if not args.db.is_file() or not args.gateway_db.is_absolute() or not args.gateway_db.is_file():
        print('База кандидатов не найдена.', file=sys.stderr)
        return 1
    try:
        with GateStore(args.db, os.environ.get('TELEGRAM_OWNER_USER_ID'), os.environ.get('TELEGRAM_OWNER_CHAT_ID'), gateway=GatewayStore(args.gateway_db)) as store:
            payload = store.proposal(args.candidate_id, args.revision, args.gate)
            result = store.request_gate(args.candidate_id, args.revision, args.gate, payload)
    except (GateError, OSError, ValueError, sqlite3.Error):
        print('Не удалось создать запрос. Проверьте владельца, стадию и полноту данных.', file=sys.stderr)
        return 1
    # The nonce is not an owner decision or credential; keep it server-side and
    # send it only to the authenticated bot process.
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
