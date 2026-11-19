"""Private per-seller observations. Completed daily slices replace earlier revisions atomically."""

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone


def utc():
    return datetime.now(timezone.utc).isoformat()


def claims(token):
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, IndexError, AttributeError):
        return {}


def scope(provider, creds):
    # Public account claims identify the archive only after authenticated data has been fetched.
    identity = creds.get("client_id") if provider != "wb" else claims(creds.get("api_token")).get("sid")
    identity = identity or creds.get("api_token") or creds.get("api_key")
    if not identity:
        raise ValueError("Missing account identity")
    return hashlib.sha256(json.dumps([provider, identity], sort_keys=True).encode()).hexdigest()


def credential_id(creds):
    return hashlib.sha256(json.dumps(creds, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class History:
    def __init__(self, directory):
        self.root = Path(directory).parent / ".market-reports"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "history-v2.db"
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS days(scope TEXT,dataset TEXT,day TEXT,observed TEXT,blob BLOB NOT NULL,PRIMARY KEY(scope,dataset,day))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS checks(credential TEXT,tool TEXT,outcome TEXT,checked TEXT,PRIMARY KEY(credential,tool))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS archive_credentials(credential TEXT PRIMARY KEY,owner TEXT NOT NULL)"
            )

    def confirm(self, creds, owner):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO archive_credentials VALUES(?,?)", (credential_id(creds), owner)
            )

    def authorized(self, creds, owner):
        with self.db() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM archive_credentials WHERE credential=? AND owner=?",
                    (credential_id(creds), owner),
                ).fetchone()
            )

    def verified_days(self, provider, creds, dataset, start, end):
        owner = scope(provider, creds)
        return self.read_days(owner, dataset, start, end) if self.authorized(creds, owner) else {}

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=3)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _cipher(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        path = self.root / "history-v2.key"
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            with self.db() as db:
                if db.execute("SELECT 1 FROM days LIMIT 1").fetchone():
                    raise RuntimeError("Archive encryption key is unavailable")
            temp = self.root / (".history-key-" + secrets.token_hex(12))
            try:
                with os.fdopen(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temp, path)
                except FileExistsError:
                    pass
                if os.name == "posix":
                    handle = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(handle)
                    finally:
                        os.close(handle)
                key = path.read_bytes()
            finally:
                temp.unlink(missing_ok=True)
        if path.is_symlink() or len(key) != 32:
            raise RuntimeError("Archive encryption key is invalid")
        return AESGCM(key)

    def save_day(self, owner, dataset, day, records, observed):
        date.fromisoformat(day)
        at = datetime.fromisoformat(observed)
        if at.utcoffset() is None:
            raise ValueError("Archive observations require a timezone")
        observed = at.astimezone(timezone.utc).isoformat()
        aad = json.dumps([owner, dataset, day, observed]).encode()
        nonce = os.urandom(12)
        raw = json.dumps(
            records, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":")
        ).encode()
        blob = nonce + self._cipher().encrypt(nonce, raw, aad)
        with self.db() as db:
            db.execute(
                """INSERT INTO days VALUES(?,?,?,?,?) ON CONFLICT(scope,dataset,day)
                DO UPDATE SET observed=excluded.observed,blob=excluded.blob WHERE days.observed<excluded.observed""",
                (owner, dataset, day, observed, blob),
            )

    def read_days(self, owner, dataset, start, end):
        with self.db() as db:
            pages = db.execute(
                "SELECT day,observed,blob FROM days WHERE scope=? AND dataset=? AND day BETWEEN ? AND ? ORDER BY day",
                (owner, dataset, start, end),
            ).fetchall()
        result = {}
        cipher = self._cipher() if pages else None
        for day, stamp, blob in pages:
            aad = json.dumps([owner, dataset, day, stamp]).encode()
            result[day] = (stamp, json.loads(cipher.decrypt(blob[:12], blob[12:], aad)))
        return result

    def coverage(self, owner, dataset):
        with self.db() as db:
            return dict(
                db.execute("SELECT day,observed FROM days WHERE scope=? AND dataset=?", (owner, dataset))
            )

    def record(self, creds, name, status):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO checks VALUES(?,?,?,?)", (credential_id(creds), name, status, utc())
            )

    def check(self, creds, name):
        with self.db() as db:
            found = db.execute(
                "SELECT outcome,checked FROM checks WHERE credential=? AND tool=?",
                (credential_id(creds), name),
            ).fetchone()
        return (
            {"status": found[0], "last_checked_at": found[1]}
            if found
            else {"status": "untested", "last_checked_at": None}
        )


def days_between(start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def order_event(cancelled):
    if type(cancelled) is bool:
        return 'cancelled' if cancelled else 'order'
    return 'cancellation_unknown'


def grouped_orders(args, events, complete_days):
    """Group reconciled order units, inserting zeroes only for days covered by the source."""
    groups = {}
    covered = set(complete_days)
    def bucket(article, day):
        start = date.fromisoformat(day)
        if args["group_by"] == "week":
            start -= timedelta(days=start.weekday())
        key = article, start.isoformat()
        return groups.setdefault(key, dict(article=article, date=key[1], ordered_units=0,
                                           confirmed_cancelled_units=0, unknown_cancellation_units=0))

    for article, day, event, units in events:
        row = bucket(article, day)
        row["ordered_units"] += units
        if event == "cancelled":
            row["confirmed_cancelled_units"] += units
        elif event != "order":
            row["unknown_cancellation_units"] += units
    article = args.get("article")
    if article is None:
        articles = {a for a, _ in groups}
        if len(articles) == 1:
            article = articles.pop()
    if article is not None:
        for day in covered:
            bucket(article, day)
    for row in groups.values():
        row["cancelled_units"] = None if row['unknown_cancellation_units'] else row['confirmed_cancelled_units']
        if args['group_by'] == 'week':
            monday = date.fromisoformat(row['date'])
            start = max(row['date'], args.get('date_from', row['date']))
            sunday = (monday + timedelta(days=6)).isoformat()
            end = min(sunday, args.get('date_to', sunday))
            requested = days_between(start, end)
            count = sum(day in covered for day in requested)
            row.update(period_start=start, period_end=end, covered_days=count, requested_days=len(requested),
                       complete=count == len(requested), observed_ordered_units=row['ordered_units'])
            if not row['complete']:
                row['ordered_units'] = row['cancelled_units'] = None
    return [groups[k] for k in sorted(groups, key=lambda k: (k[1], str(k[0])))]


def historical_read(history, owner, name, args):
    from market_specs import MOSCOW, matches, result

    pages = history.read_days(owner, name, args["date_from"], args["date_to"])
    days = days_between(args["date_from"], args["date_to"])
    missing = [d for d in days if d not in pages]
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    selected = []
    unresolved = 0
    check_article = name == "ozon_sku_daily" and "article" in args
    for _, records in pages.values():
        for row in records:
            if check_article and not row.get("article"):
                sku = row.get("sku")
                if "sku" not in args or sku is None or str(sku) == str(args["sku"]):
                    unresolved += 1
            elif matches(row, args):
                selected.append(row)
    if name == "wb_orders" and "group_by" in args:
        events = ((r.get("article"), r["date"][:10], order_event(r.get("cancelled")), r["quantity"])
                  for r in selected)
        selected = grouped_orders(args, events, pages)
    elif name in ("wb_orders", "wb_sales") and args.get("mode") == "summary":
        groups = {}
        for r in selected:
            key = (
                r.get("article"),
                r["date"][:10],
                r.get("warehouse"),
                r.get("event_type", order_event(r.get("cancelled"))),
            )
            groups[key] = groups.get(key, 0) + r["quantity"]
        selected = [
            dict(article=a, date=d, warehouse=w, event=e, units=n)
            for (a, d, w, e), n in sorted(groups.items(), key=str)
        ]
    more = offset + limit < len(selected)
    provisional = (datetime.now(MOSCOW).date() - timedelta(days=1)).isoformat()
    return result(
        selected[offset : offset + limit],
        offset + limit if more else None,
        complete=not missing and not more and not unresolved,
        source="daily_archive",
        missing_dates=missing,
        requested_days=len(days),
        archived_days=len(pages),
        **({"issues": ["archived_article_identity_missing"], "unresolved_article_rows": unresolved}
           if unresolved else {}),
        data_as_of=min((s for s, _ in pages.values()), default=None),
        provisional_dates=[d for d in pages if name.startswith("wb_") and d >= provisional],
    )
