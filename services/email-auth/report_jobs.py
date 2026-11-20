"""Resume a bounded seller report without holding a marketplace client's HTTP request."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error

import finance_gate
from market_transport import Wire, _id


PREFIX = "mcpjob_"
WAIT_SECONDS = 3
# Keep every background thread within the native deployment drain budget.
MAX_BACKGROUND_SECONDS = 125
MAX_JOBS = 4
MAX_WORKERS = 2
MIN_RETRY_SECONDS = 5


class InvalidTask(ValueError):
    pass


def _pending(name, task, reason, wait=5):
    return dict(tool=name, status="pending", task_id=task, items=[], complete=False,
                next_cursor=None, reason=reason, http_status=None, retry_after_seconds=wait,
                observed_at=datetime.now(timezone.utc).isoformat(), data_as_of=None,
                read_only=True, untrusted_data=True)


def _sources(value):
    # These containers hold our projected source statuses. Marketplace rows may
    # contain user/provider data and must never be rewritten as diagnostics.
    def walk(entry):
        if isinstance(entry, list):
            for child in entry:
                yield from walk(child)
        elif isinstance(entry, dict):
            if "http_status" in entry or "retry_after_seconds" in entry or "status" in entry:
                yield entry
            for field, child in entry.items():
                if field not in ("items", "rows", "data", "response", "payload"):
                    yield from walk(child)

    for field in ("sources", "source_issues", "unavailable_marketplaces", "catalog_verification_issues"):
        yield from walk(value.get(field))


def _ttl(name, args, value):
    waits = [row.get("retry_after_seconds") for row in (value, *_sources(value))]
    delays = [n for n in waits if type(n) is int and n >= 0]
    if delays or value.get("status") in ("pending", "rate_limited", "provider_timeout", "unavailable"):
        return max([5] + delays)
    month = args.get("month")
    if name == "unit_economics" and month and month < time.strftime("%Y-%m") and not value.get("unavailable_marketplaces"):
        rows = value.get("items") or []
        if rows and all(row.get("sold_units") is not None and
                        (row.get("net_after_expenses") or {}).get("amount") is not None for row in rows):
            return 900
    return 60


def _replay(raw):
    record = json.loads(raw)
    previous = record["value"]
    now = time.time()
    previous["served_from_cache"] = True
    for row in (previous, *_sources(previous)):
        if type(row.get("retry_after_seconds")) is int:
            row["retry_after_seconds"] = max(0, math.ceil(record["saved_at"] + row["retry_after_seconds"] - now))
        if type(row.get("http_status")) is int and row["http_status"] >= 400:
            row["previous_http_status"] = row["http_status"]
            row["provider_observed_at"] = row.get("observed_at") or previous.get("observed_at") or datetime.fromtimestamp(
                record["saved_at"], timezone.utc).isoformat()
            row["http_status"] = None
            if row["previous_http_status"] == 429:
                row["reason"] = "local_limiter"
    return previous


def _failure(name, exc, creds):
    from portal import public_retry_delay
    result = dict(tool=name, items=[], complete=False, next_cursor=None, http_status=None,
                  observed_at=datetime.now(timezone.utc).isoformat(), data_as_of=None,
                  read_only=True, untrusted_data=True)
    if isinstance(exc, urllib.error.HTTPError):
        delay = getattr(exc, "retry_after_seconds", None)
        if exc.code == 429 and delay is None:
            delay = finance_gate.parse_retry_after(exc.headers)[0]
        status = "provider_access_denied" if exc.code in (401, 402, 403) else "provider_error"
        result.update(status="rate_limited" if exc.code == 429 else status, http_status=exc.code)
        if exc.code == 429:
            result["retry_after_seconds"] = public_retry_delay(delay if delay is not None else 60, creds)
        exc.close()
    elif isinstance(exc, finance_gate.GateError):
        limited = exc.reason in ("busy", "cooldown", "retry_time_unknown")
        result.update(status="rate_limited" if limited else "unavailable",
                      reason="local_limiter" if limited else exc.reason,
                      retry_after_seconds=public_retry_delay(exc.retry_after_seconds, creds))
    elif isinstance(exc, TimeoutError):
        result.update(status="provider_timeout", **getattr(exc, "diagnostic", {}))
    elif isinstance(exc, finance_gate.ProviderResponseInvalid):
        result.update(status="provider_response_invalid", field="response")
    elif isinstance(exc, finance_gate.ProviderResponseTooLarge):
        result.update(status="provider_response_too_large")
    elif isinstance(exc, urllib.error.URLError):
        result.update(status="transport_error")
    else:
        result.update(status="unavailable")
    return result


def run(name, args, creds, directory, dispatch, observe=None, *, deadline_aware=False):
    from expansion_gateway import _seller_mappings
    args = dict(args)
    supplied = args.pop("task_id", None)
    mappings = _seller_mappings(directory)
    wire = Wire("mcp-report", creds, directory, time.monotonic())
    scope = _id([wire.scope, "mcp-public-report-v2", name, args, mappings])
    default_task = PREFIX + _id([scope, None])[:32]

    def lookup(label, token):
        raw = wire.cached(_id([scope, label, token]))
        return json.loads(raw) if raw is not None else None

    def bind(label, token, value, ttl=86400):
        wire.store(_id([scope, label, token]), json.dumps(value).encode(), ttl)

    task = default_task
    if supplied and supplied.startswith(PREFIX):
        if supplied != default_task and lookup("known", supplied) is None:
            raise InvalidTask("Task belongs to another report or cabinet")
        task = supplied
    elif supplied:
        # Older clients may already have an upstream receipt. Bind it to this exact query.
        task = lookup("native", supplied) or PREFIX + _id([scope, supplied])[:32]
        if task != default_task and lookup("known", task) is None:
            bind("known", task, supplied)
            bind("native", supplied, task)

    key = _id([scope, "result", task])
    # Use at most 4096 lock files, independent of the number of expired reports.
    lock_name = "report-" + key[:3]
    owner_key = _id(["report-lock-owner", lock_name])
    owner = key.encode("ascii")

    def saved(check_worker=True):
        raw = wire.cached(key)
        if raw is None:
            return None
        value = _replay(raw)
        if value.get("background_retry") is True:
            # A cached receipt can outlive a crashed Gunicorn worker. Only
            # advertise an active retry while its job lock is held.
            active = False
            if check_worker:
                try:
                    root = finance_gate._root(str(Path(directory).parent / ".mcp-report-locks"))
                    with finance_gate._lock(root, lock_name):
                        pass
                except finance_gate.GateError as exc:
                    if exc.reason == "busy":
                        active = wire.cached(owner_key) == owner
            if not active:
                value.update(background_retry=False, reason="retry_later")
        return value

    value = saved()
    if value is not None:
        return value
    locks = ExitStack()
    try:
        root = finance_gate._root(str(Path(directory).parent / ".mcp-report-locks"))
        locks.enter_context(finance_gate._lock(root, lock_name))
        value = saved(check_worker=False)
        if value is not None:
            locks.close()
            return value
        # Reserve a bounded job place independently of the two provider slots.
        # A queued job can therefore start without requiring another client poll.
        for slot in ("job-" + str(i) for i in range(MAX_JOBS)):
            try:
                locks.enter_context(finance_gate._lock(root, slot))
                break
            except finance_gate.GateError as exc:
                if exc.reason != "busy":
                    raise
        else:
            locks.close()
            return _pending(name, task, "local_limiter", 3)
        native = lookup("resume", task) or lookup("known", task)
        if native:
            args["task_id"] = native
        try:
            wire.store(owner_key, owner, MAX_BACKGROUND_SECONDS + 2)
        except (OSError, sqlite3.Error):
            pass
    except finance_gate.GateError as exc:
        locks.close()
        if exc.reason != "busy":
            raise
        return _pending(name, task, "report_in_progress")
    except BaseException:
        locks.close()
        raise

    finished = threading.Event()
    result = {}
    end = time.monotonic() + MAX_BACKGROUND_SECONDS

    def work():
        try:
            current_native = native
            while time.monotonic() < end - 2:
                if mappings != _seller_mappings(directory):
                    result["value"] = _pending(name, default_task, "seller_mapping_changed")
                    result["value"].pop("task_id")
                    finished.set()
                    return
                worker = ExitStack()
                acquired = False
                try:
                    for slot in ("worker-" + str(i) for i in range(MAX_WORKERS)):
                        try:
                            worker.enter_context(finance_gate._lock(root, slot))
                            acquired = True
                            break
                        except finance_gate.GateError as exc:
                            if exc.reason != "busy":
                                raise
                    if not acquired:
                        result["value"] = _pending(name, task, "waiting_worker", 3)
                        try:
                            wire.store(key, json.dumps({"value": result["value"], "saved_at": time.time()}).encode(), 3)
                        except (OSError, sqlite3.Error):
                            pass
                        finished.set()
                        time.sleep(min(1, max(0, end - time.monotonic() - 2)))
                        continue
                    query = dict(args)
                    if current_native:
                        query["task_id"] = current_native
                    try:
                        value = (dispatch(query, mappings, end) if deadline_aware else dispatch(query, mappings))
                    except Exception as exc:
                        value = _failure(name, exc, creds)
                finally:
                    worker.close()
                if mappings != _seller_mappings(directory):
                    result["value"] = _pending(name, default_task, "seller_mapping_changed")
                    result["value"].pop("task_id")  # Restart with the current seller mapping.
                    finished.set()
                    return
                ttl = _ttl(name, args, value)
                next_task = value.get("task_id")
                pending = value.get("status") == "pending"
                issues = list(_sources(value))
                # Do not turn transport failures into a tight or blind retry loop.
                retriable = pending and not any(row.get("status") == "source_unavailable" for row in issues)
                waits = [row.get("retry_after_seconds") for row in (value, *issues)]
                delay = max([MIN_RETRY_SECONDS] + [n for n in waits if type(n) is int and n >= 0])
                retry = retriable and time.monotonic() + delay + 2 < end
                if next_task or pending:
                    value = dict(value, task_id=task)
                if pending:
                    value.update(reason="retry_scheduled" if retry else "retry_later",
                                 retry_after_seconds=delay, background_retry=retry)
                record = {"value": value, "saved_at": time.time()}
                try:
                    if next_task:
                        bind("resume", task, next_task, max(86400, ttl + 3600))
                        bind("native", next_task, task, max(86400, ttl + 3600))
                        current_native = next_task
                    elif not current_native or value.get("complete") is True:
                        bind("resume", task, None)
                    wire.store(key, json.dumps(record, ensure_ascii=False, allow_nan=False).encode(), ttl)
                except (OSError, sqlite3.Error):
                    # Do not dispatch another page when its continuation was not persisted.
                    retry = False
                    if pending:
                        value.update(reason="retry_later", background_retry=False)
                result["value"] = value
                if observe:
                    try:
                        observe(value.get("status") or ("partial" if value.get("complete") is False else "ok"))
                    except Exception:
                        pass  # Diagnostics must not stop the report.
                finished.set()
                if not retry:
                    return
                time.sleep(delay)
            value = _pending(name, task, "retry_later", 3)
            value["background_retry"] = False
            result["value"] = value
            try:
                wire.store(key, json.dumps({"value": value, "saved_at": time.time()}).encode(), 3)
            except (OSError, sqlite3.Error):
                pass
        except Exception as exc:
            # A failed scheduler/lock must not leave a phantom active receipt.
            value = _failure(name, exc, creds)
            result["value"] = value
            try:
                wire.store(key, json.dumps({"value": value, "saved_at": time.time()}).encode(), 5)
            except (OSError, sqlite3.Error):
                pass
        finally:
            locks.close()
            finished.set()

    # Thread.start returns after the OS thread exists. The native drain checks
    # every worker thread and provider child before switching the release.
    thread = threading.Thread(target=work, name="mcp-report", daemon=True)
    try:
        if observe:
            try:
                observe("pending")
            except Exception:
                pass
        thread.start()
    except BaseException:
        locks.close()
        raise
    if finished.wait(WAIT_SECONDS) and "value" in result:
        return result["value"]
    return _pending(name, task, "report_in_progress")
