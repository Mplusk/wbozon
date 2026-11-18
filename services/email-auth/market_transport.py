"""Credential-scoped report cache and method quotas shared by short-lived provider workers."""

import base64
import contextlib
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import finance_gate
import market_cooldown
from wb_mcp import NoRedirect

WB_HOSTS = frozenset(
    x + ".wildberries.ru"
    for x in (
        "supplies-api",
        "common-api",
        "seller-analytics-api",
        "statistics-api",
        "finance-api",
        "feedbacks-api",
        "advert-api",
        "content-api",
        "documents-api",
        "discounts-prices-api",
    )
)
SELLER = "api-seller.ozon.ru"
PERFORMANCE = "api-performance.ozon.ru"
MAX_REPORT = 16 * 1024 * 1024
MAX_CACHE_BYTES = 96 * 1024 * 1024
UNCONFIRMED_EXPIRY = 253402300799  # Unknown creation survives process restarts and report retention.
SELLER_READS = frozenset(
    (
        "/v3/supply-order/list",
        "/v3/supply-order/get",
        "/v1/supply-order/bundle",
        "/v1/supply-order/act/product/get",
        "/v1/supply-order/act/summary/get",
        "/v3/product/info/list",
        "/v4/product/info/stocks",
        "/v1/analytics/stocks",
        "/v1/analytics/turnover/stocks",
        "/v1/analytics/local-sale/clusters-items/info",
        "/v1/analytics/data",
        "/v3/posting/fbo/list",
        "/v3/posting/fbs/list",
        "/v1/finance/cash-flow-statement/list",
        "/v1/cluster/list",
        "/v2/review/list",
        "/v1/rating/summary",
        "/v1/seller/info",
    )
)
WB_READS = {
    "supplies-api": {
        "POST": r"/api/v1/supplies",
        "GET": r"/api/v1/(?:warehouses|supplies/[1-9]\d{0,18}(?:/(?:goods|package))?)",
    },
    "seller-analytics-api": {
        "POST": r"/api/(?:analytics/v3/sales-funnel/products(?:/history)?|analytics/v1/order-feed|v2/search-report/product/search-texts)",
        "GET": r"/api/(?:v1/(?:warehouse_remains|paid_storage|acceptance_report)(?:/tasks/[A-Za-z0-9_-]{1,256}/(?:status|download))?|analytics/v1/item-returns)",
    },
    "statistics-api": {"GET": r"/api/v1/supplier/(?:orders|sales)"},
    "finance-api": {"POST": r"/api/finance/v1/sales-reports/detailed"},
    "feedbacks-api": {"GET": r"/api/v1/feedbacks(?:/archive)?"},
    "advert-api": {"GET": r"/(?:adv/v1/(?:promotion/count|upd|payments|balance)|api/advert/v4/fullstats|api/advert/v3/adverts)"},
    "documents-api": {"GET": r"/api/v1/documents/(?:list|categories)"},
    "content-api": {"POST": r"/content/v2/get/cards/list"},
    "discounts-prices-api":{"GET":r"/api/v2/list/goods/filter"},
    "common-api": {"GET": r"/api/tariffs/v1/acceptance/coefficients"},
}


def allowed_read(host, method, path):
    if host == SELLER:
        return method == "POST" and path in SELLER_READS
    if host == PERFORMANCE:
        if method == "POST":
            return path in ("/api/client/token", "/api/client/statistics")
        if method != "GET":
            return False
        return path in ("/api/client/campaign", "/api/client/statistics/report") or (
            path.startswith("/api/client/statistics/")
            and valid_task("performance", path.removeprefix("/api/client/statistics/"))
        )
    pattern = WB_READS.get(host.split(".")[0], {}).get(method)
    return bool(pattern and host in WB_HOSTS and re.fullmatch(pattern, path))


def valid_task(provider, value):
    if not isinstance(value, str):
        return False
    if provider == "wb":
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value))
    try:
        return uuid.UUID(value).hex == value.replace("-", "").lower()
    except ValueError:
        return False


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    )


def _no_duplicates(entries):
    result = {}
    for key, value in entries:
        if key in result:
            raise ValueError("Repeated marketplace field")
        result[key] = value
    return result


def _id(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _wb_personal(token):
    # Token class only selects a local pacing policy. The provider checks authorization.
    try:
        chunk = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(chunk + "=" * (-len(chunk) % 4)))
        return payload.get("acc") in (3, 4) and payload.get("t") is not True
    except (ValueError, IndexError, AttributeError):
        return False


class _AvailableResult(Exception):
    def __init__(self, result):
        self.result = result


class Wire:
    def __init__(self, provider, creds, directory, deadline):
        self.provider, self.creds, self.deadline = provider, creds, deadline
        self.gate_dir = directory
        self.token = None
        self.fetch_times = []
        self.scope = _id([provider, creds])
        self.aead = AESGCM(hashlib.sha256(("market-cache-v1:" + self.scope).encode()).digest())
        self.root = Path(directory).parent / ".market-reports"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "reports.db"
        self.opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, expiry REAL NOT NULL, blob BLOB NOT NULL)"
            )
            # Kept apart from cache: previous releases insert exactly three columns.
            db.execute("CREATE TABLE IF NOT EXISTS cache_observations(k TEXT PRIMARY KEY, expiry REAL NOT NULL, fetched REAL NOT NULL, digest BLOB NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS quota(k TEXT PRIMARY KEY, until_at REAL, unknown INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(k TEXT PRIMARY KEY, binding TEXT NOT NULL, expiry REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS analytics_requests(scope TEXT NOT NULL, at REAL NOT NULL)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS stock_history (
                series TEXT NOT NULL, day TEXT NOT NULL, observed TEXT NOT NULL, blob BLOB NOT NULL,
                PRIMARY KEY (series, day))""")
            db.execute("""CREATE TABLE IF NOT EXISTS stock_coverage (
                series TEXT NOT NULL, day TEXT NOT NULL, observed TEXT NOT NULL, PRIMARY KEY (series, day))""")

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def progress(self, name, args):
        """Serialize a resumable scan across workers. Kernel releases the lock after a crash."""
        key = _id([self.scope, name, args])
        with (self.root / ("scan-" + key[:2] + ".lock")).open("ab") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise finance_gate.GateError("busy", 1) from None
            else:
                import fcntl

                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise finance_gate.GateError("busy", 1) from None
            raw = self.cached(key)
            yield (
                json.loads(raw) if raw is not None else {},
                lambda data, ttl: self.store(key, _json(data).encode(), ttl),
            )

    def wait(self, seconds):
        if seconds < 0 or time.monotonic() + seconds + 1.2 >= self.deadline:
            return False
        time.sleep(seconds)
        return True

    def bind_job(self, endpoint, query, task, *, existing):
        key, binding = _id([self.scope, endpoint, task]), _id(query)
        with self.db() as db:
            if existing:
                row = db.execute(
                    "SELECT binding FROM jobs WHERE k=? AND expiry>?", (key, time.time())
                ).fetchone()
                if not row:
                    creation = self._job_key(endpoint, query)
                    row = (
                        (binding,)
                        if db.execute(
                            "SELECT 1 FROM jobs WHERE k=? AND binding=?", (creation, "id:" + task)
                        ).fetchone()
                        else None
                    )
                if not row or row[0] != binding:
                    raise ValueError("Report belongs to different query")
            else:
                db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)", (key, binding, time.time() + 86400))
            db.execute("DELETE FROM jobs WHERE expiry<?", (time.time(),))

    def _job_key(self, endpoint, params):
        if urlsplit(endpoint).netloc == PERFORMANCE:
            return self._key("POST", endpoint, None, params)
        return self._key("GET", endpoint, params, None)

    def finish_job(self, endpoint, params, task, *, failed=False):
        key = self._job_key(endpoint, params)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT binding FROM jobs WHERE k=?", (key,)).fetchone()
            if not row or row[0] not in ("id:" + task, "done:" + task):
                return
            db.execute(
                "DELETE FROM jobs WHERE k=? AND binding=?", (_id([self.scope, "active-report"]), key)
            )
            db.execute("DELETE FROM jobs WHERE binding=?", ("submission-owner:" + key,))
            if failed:
                db.execute("DELETE FROM jobs WHERE k=?", (key,))
                db.execute("DELETE FROM cache WHERE k=?", (key,))
            elif row[0] == "id:" + task:
                db.execute(
                    "UPDATE jobs SET binding=?,expiry=? WHERE k=?",
                    ("done:" + task, time.time() + 1800, key),
                )

    def _key(self, method, url, query, body):
        return _id([self.scope, method, url, query or {}, body])

    def forget(self, method, url, *, query=None, body=None):
        key = self._key(method, url, query, body)
        with self.db() as db:
            db.execute("DELETE FROM cache WHERE k=?", (key,))
            db.execute("DELETE FROM jobs WHERE k=? AND binding LIKE ?", (key, "id:%"))

    def cached(self, key):
        with self.db() as db:
            row = db.execute("SELECT blob FROM cache WHERE k=? AND expiry>?", (key, time.time())).fetchone()
        if row is None:
            return None
        return self.aead.decrypt(row[0][:12], row[0][12:], key.encode())

    def cached_download(self, url):
        return self._http_cache(self._key("GET", url, None, None), binary=False)

    def _http_cache(self, key, binary):
        with self.db() as db:
            row = db.execute("SELECT c.blob,o.fetched,o.digest FROM cache c LEFT JOIN cache_observations o "
                             "ON o.k=c.k AND o.expiry=c.expiry WHERE c.k=? AND c.expiry>?",
                             (key,time.time())).fetchone()
        if row is None:
            return None
        blob, fetched, digest = row
        if digest != hashlib.sha256(blob).digest():
            fetched = None
        raw = self.aead.decrypt(blob[:12], blob[12:], key.encode())
        self.fetch_times.append(fetched)
        # Older entries stored both HTTP 204 and a JSON null as b"null".
        return raw if binary or raw is None else json.loads(raw, parse_float=Decimal)

    def _existing_job(self, key, reservations):
        with self.db() as db:
            row = db.execute(
                "SELECT binding FROM jobs WHERE k=? AND expiry>?", (key, time.time())
            ).fetchone()
            if not row:
                if self.provider == "performance":
                    self._check_active(db, key)
                self._check_reservations(db, reservations)
                return None
        if not row[0].startswith(("id:", "done:")):
            raise finance_gate.GateError("retry_time_unknown")
        task = row[0].partition(":")[2]
        return {"UUID": task} if self.provider == "performance" else {"data": {"taskId": task}}

    def store(self, key, data, ttl, *, fetched=None):
        nonce = os.urandom(12)
        blob = nonce + self.aead.encrypt(nonce, data, key.encode())
        with self.db() as db:
            now = time.time()
            expiry = now + ttl
            db.execute("DELETE FROM cache WHERE expiry<=?", (now,))
            db.execute("DELETE FROM cache_observations WHERE expiry<=?", (now,))
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, expiry, blob))
            if fetched is not None:
                db.execute("INSERT OR REPLACE INTO cache_observations VALUES(?,?,?,?)",(key,expiry,fetched,hashlib.sha256(blob).digest()))
            total = db.execute("SELECT COALESCE(SUM(length(blob)),0) FROM cache").fetchone()[0]
            while total > MAX_CACHE_BYTES:
                old, size = db.execute(
                    "SELECT k,length(blob) FROM cache ORDER BY expiry LIMIT 1"
                ).fetchone()
                db.execute("DELETE FROM cache WHERE k=?", (old,))
                total -= size

    def record_stocks(self, skus, article, observations, observed):
        """Store the latest complete, unfiltered warehouse observation per SKU and UTC day."""
        expected = {str(sku) for sku in skus}
        series = {}
        for row in observations:
            sku = str(row["sku"])
            if sku not in expected:
                raise ValueError("Unexpected SKU in stock observation")
            series.setdefault(("sku", sku), []).append(row)
        if not series:
            return
        if (
            article is not None
            and len(series) == len(expected)
            and all(r.get("article") == article for r in observations)
        ):
            series["article", article] = observations
        day = observed[:10]
        cutoff = (date.fromisoformat(day) - timedelta(days=365)).isoformat()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM stock_history WHERE day<?", (cutoff,))
            db.execute("DELETE FROM stock_coverage WHERE day<?", (cutoff,))
            for (kind, value), items in series.items():
                key = _id([self.scope, "stock-history-v1", kind, value])
                prior = db.execute(
                    "SELECT observed,blob FROM stock_history WHERE series=? AND day=?", (key, day)
                ).fetchone()
                identity = (key + day + observed).encode()
                if prior and prior[0] >= observed:
                    if prior[0] == observed:
                        blob = prior[1]
                        old = json.loads(self.aead.decrypt(blob[:12], blob[12:], identity))
                        if sorted(map(_json, old)) != sorted(map(_json, items)):
                            db.execute("DELETE FROM stock_coverage WHERE series=? AND day=?", (key, day))
                    continue
                nonce = os.urandom(12)
                blob = nonce + self.aead.encrypt(nonce, _json(items).encode(), identity)
                db.execute(
                    "INSERT OR REPLACE INTO stock_history VALUES(?,?,?,?)", (key, day, observed, blob)
                )
                db.execute("INSERT OR REPLACE INTO stock_coverage VALUES(?,?,?)", (key, day, observed))
            size = db.execute("SELECT COALESCE(SUM(length(blob)),0) FROM stock_history").fetchone()[0]
            while size > MAX_CACHE_BYTES:
                key, day, length = db.execute(
                    "SELECT series,day,length(blob) FROM stock_history ORDER BY day,observed LIMIT 1"
                ).fetchone()
                db.execute("DELETE FROM stock_history WHERE series=? AND day=?", (key, day))
                db.execute("DELETE FROM stock_coverage WHERE series=? AND day=?", (key, day))
                size -= length

    def stock_history(self, args):
        from market_specs import result

        skus = args.get("skus", [args["sku"]] if "sku" in args else [])
        if skus:
            keys = [_id([self.scope, "stock-history-v1", "sku", str(s)]) for s in skus]
        elif "article" in args:
            keys = [_id([self.scope, "stock-history-v1", "article", args["article"]])]
        else:
            return result([], complete=False, status="sku_or_article_required")
        start, end = date.fromisoformat(args["date_from"]), date.fromisoformat(args["date_to"])
        days = [(start + timedelta(days=i)).isoformat() for i in range(1 + (end - start).days)]
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        where = "series IN (" + ",".join("?" for _ in keys) + ") AND day BETWEEN ? AND ?"
        params = (*keys, days[0], days[-1])
        matched = 0
        items = []
        filtered_scope = "warehouse_id" in args or "cluster_id" in args or bool(skus and "article" in args)
        observed_series = {}
        with self.db() as db:
            db.execute("BEGIN")
            coverage = dict(
                db.execute(
                    "SELECT day,COUNT(*) FROM stock_history JOIN stock_coverage USING(series,day,observed) WHERE "
                    + where
                    + " GROUP BY day",
                    params,
                )
            )
            for key, day, stamp, blob in db.execute(
                "SELECT series,day,observed,blob FROM stock_history WHERE "
                + where
                + " ORDER BY day,series",
                params,
            ):
                raw = self.aead.decrypt(blob[:12], blob[12:], (key + day + stamp).encode())
                for row in json.loads(raw):
                    if any(
                        str(row.get(k)) != str(args[k])
                        for k in ("article", "warehouse_id", "cluster_id", "sku")
                        if k in args
                    ):
                        continue
                    matched += 1
                    if filtered_scope:
                        observed_series.setdefault(day, set()).add(key)
                    if matched > offset and len(items) < limit:
                        items.append(dict(row, observation_date=day, stock_observed_at=stamp))
                    if matched > offset + limit and not filtered_scope:
                        break
                if matched > offset + limit and not filtered_scope:
                    break
        missing = [
            day
            for day in days
            if coverage.get(day, 0) != len(keys)
            or (filtered_scope and len(observed_series.get(day, ())) != len(keys))
        ]
        more = matched > offset + limit
        return result(
            items,
            offset + limit if more else None,
            complete=not missing and not more,
            status="partial_history" if missing else "observed_history",
            missing_or_partial_dates=missing,
            requested_series=len(keys),
            source="locally_observed_ozon_warehouse_snapshots",
            history_basis="latest_observed_unfiltered_read_per_utc_day",
            quantity_unit="pcs",
        )

    def _admit(self, key, interval):
        while True:
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                row = db.execute("SELECT until_at,unknown FROM quota WHERE k=?", (key,)).fetchone()
                if row and row[1]:
                    raise finance_gate.GateError("retry_time_unknown")
                wait = max(0, (row[0] - now) if row else 0)
                if not wait:
                    db.execute("INSERT OR REPLACE INTO quota VALUES(?,?,0)", (key, now + interval))
                    return
            if wait>2 or not self.wait(wait):
                delay = math.ceil(wait)
                raise urllib.error.HTTPError(
                    "", 429, "Method request interval", {"Retry-After": str(delay)}, None
                )

    def _analytics_budget(self):
        market_cooldown._analytics_slot(self.gate_dir, self.creds)

    def _check_active(self, db, key):
        slot = _id([self.scope, "active-report"])
        row = db.execute("SELECT binding FROM jobs WHERE k=? AND expiry>?", (slot, time.time())).fetchone()
        if row:
            if row[0] == key:
                raise finance_gate.GateError("retry_time_unknown")
            raise finance_gate.GateError("busy", 60)
        return slot

    def _submission_locks(self, method, url, query, body):
        scope = market_cooldown.method_key(self.provider, self.creds, url)
        keys = [_id(["submission-v1", scope, method, query or {}, body])]
        if self.provider == "performance":
            keys.append(_id(["performance-slot-v1", scope]))
        return keys

    @staticmethod
    def _check_reservations(db, reservations):
        for position, reservation in enumerate(reservations):
            if db.execute("SELECT 1 FROM jobs WHERE k=?", (reservation,)).fetchone():
                if position == 0:
                    raise finance_gate.GateError("retry_time_unknown")
                raise finance_gate.GateError("busy", 60)

    def _reserve_submission(self, key, reservations):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT binding FROM jobs WHERE k=? AND expiry>?", (key, time.time())).fetchone()
            if pending:
                if pending[0].startswith(("id:", "done:")):
                    task = pending[0].partition(":")[2]
                    raise _AvailableResult({"UUID": task} if self.provider == "performance" else {"data": {"taskId": task}})
                raise finance_gate.GateError("retry_time_unknown")
            self._check_reservations(db, reservations)
            if self.provider == "performance":
                slot = self._check_active(db, key)
                db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)", (slot, key, UNCONFIRMED_EXPIRY))
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)", (key, "awaiting_provider_id", UNCONFIRMED_EXPIRY))
            db.executemany("INSERT INTO jobs VALUES(?,?,?)", [
                (reservation, "submission-owner:" + key, UNCONFIRMED_EXPIRY) for reservation in reservations
            ])

    def __call__(
        self,
        method,
        url,
        *,
        query=None,
        body=None,
        group=None,
        interval=0.1,
        basic_interval=None,
        ttl=0,
        job=False,
        binary=False,
        token_request=False,
    ):
        parts = urlsplit(url)
        permitted = {"wb": WB_HOSTS, "ozon": {SELLER}, "performance": {PERFORMANCE}}[self.provider]
        if (
            parts.scheme != "https"
            or parts.netloc not in permitted
            or parts.query
            or parts.fragment
            or not allowed_read(parts.netloc, method, parts.path)
        ):
            raise ValueError("Invalid provider endpoint")
        key = self._key(method, url, query, body)
        if job:
            reservations = self._submission_locks(method, url, query, body)
            previous = self._existing_job(key, reservations)
            if previous is not None:
                return previous
        elif ttl:
            hit = self._http_cache(key, binary)
            if hit is not None:
                return hit
        if self.provider == "wb" and basic_interval and not _wb_personal(self.creds["api_token"]):
            interval = basic_interval
        quota = _id([self.scope, parts.netloc, group or parts.path])
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.provider == "wb":
            headers["Authorization"] = self.creds["api_token"]
        elif self.provider == "ozon":
            headers.update({"Client-Id": self.creds["client_id"], "Api-Key": self.creds["api_key"]})
        elif token_request:
            if url != "https://" + PERFORMANCE + "/api/client/token":
                raise ValueError("Invalid token endpoint")
        elif self.token:
            headers["Authorization"] = "Bearer " + self.token
        else:
            raise ValueError("Performance authentication required")
        req = urllib.request.Request(
            url + ("?" + urlencode(query) if query else ""),
            method=method,
            headers=headers,
            data=_json(body).encode() if body is not None else None,
        )
        def prepare():
            self._admit(quota, interval)
            if ttl and not job:
                hit = self._http_cache(key, binary)
                if hit is not None:
                    raise _AvailableResult(hit)
            if self.deadline - time.monotonic() < 0.3:
                raise TimeoutError()
            if job:
                self._reserve_submission(key, reservations)

        def send():
            try:
                return self.opener.open(req, timeout=min(12, max(0.1, self.deadline - time.monotonic())))
            except urllib.error.HTTPError as exc:
                exc.provider_replied = True
                raise

        for attempt in range(2):
            try:
                response_context = market_cooldown.request(
                    self.gate_dir, self.provider, self.creds, req, send, before_send=prepare,
                )
                with response_context as response:
                    raw = response.read(MAX_REPORT + 1)
                    if len(raw) > MAX_REPORT:
                        raise finance_gate.ProviderResponseTooLarge("Report exceeds read bound")
                    no_content = response.status == 204
                    if no_content:
                        raw = (
                            b"[]" if self.provider == "wb" and parts.path.endswith("/download") else b"null"
                        )
                value = (
                    raw
                    if binary
                    else json.loads(
                        raw,
                        parse_float=Decimal,
                        object_pairs_hook=_no_duplicates,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")),
                    )
                )
                if value is None and not no_content:
                    raise ValueError("Unexpected JSON null in marketplace response")
                if token_request:
                    ttl = max(0, min(86400, int(value["expires_in"])) - 60)
                if job:
                    task = (
                        value.get("UUID")
                        if self.provider == "performance"
                        else (value.get("data") or {}).get("taskId")
                    )
                    if not valid_task(self.provider, task):
                        raise ValueError("Provider did not confirm report identity")
                    with self.db() as db:
                        db.execute("UPDATE jobs SET binding=? WHERE k=?", ("id:" + task, key))
                fetched = time.time()
                self.fetch_times.append(fetched)
                if ttl and not job and value is not None:
                    self.store(key, raw, ttl, fetched=fetched)
                return value
            except _AvailableResult as existing:
                return existing.result
            except urllib.error.HTTPError as exc:
                if not getattr(exc, 'provider_replied', False):
                    raise
                rejected_job = job and exc.code in (400, 401, 403, 404, 422, 429)
                if not rejected_job and exc.code != 429:
                    raise
                if exc.code == 429:
                    delay = exc.retry_after_seconds
                    if delay is not None:
                        if "Retry-After" in exc.headers:
                            del exc.headers["Retry-After"]
                        exc.headers["Retry-After"] = str(delay)
                # The durable creation marker also protects a worker killed before this commit.
                with self.db() as db:
                    if exc.code == 429:
                        if delay is None:
                            db.execute("UPDATE quota SET unknown=1 WHERE k=?", (quota,))
                        else:
                            db.execute(
                                "UPDATE quota SET until_at=MAX(until_at,?) WHERE k=?",
                                (time.time() + max(1, delay), quota),
                            )
                    if rejected_job:
                        db.execute("DELETE FROM jobs WHERE k=? AND binding='awaiting_provider_id'", (key,))
                        db.execute("DELETE FROM jobs WHERE binding=?", ("submission-owner:" + key,))
                        if self.provider == "performance":
                            db.execute(
                                "DELETE FROM jobs WHERE k=? AND binding=?",
                                (_id([self.scope, "active-report"]), key),
                            )
                if exc.code != 429:
                    raise
                if (
                    delay is None
                    or attempt
                    or job
                    or max(delay, interval) > 3
                    or not self.wait(max(1, delay))
                ):
                    raise
                exc.close()
