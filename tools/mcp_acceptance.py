"""Collect read-only acceptance evidence from one explicitly selected staging MCP scenario."""

import argparse
import calendar
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from . import mcp_acceptance_checks as checks
else:
    import mcp_acceptance_checks as checks

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "2025-11-25"
SERVER = {"name": "sidzen-marketplace-readonly", "version": "1.0.0"}
BUILD_HEADER = "X-Sidzen-Service-SHA256"
CATALOG = frozenset(
    """
wb_supplies wb_supply_goods wb_stocks wb_orders wb_sales wb_acceptance_slots
ozon_supply_orders ozon_supply_items ozon_stocks ozon_stocks_by_cluster ozon_sku_daily
wb_finance_report wb_paid_storage wb_acceptance_report ozon_cash_flow ozon_fbo_postings_geo ozon_clusters
wb_funnel wb_ads ozon_ads wb_feedbacks ozon_reviews ozon_rating supply_plan ozon_product_info wb_products
""".split()
)


def _digest(files):
    # CRLF checkout conversion does not change Python source on Windows.
    h = hashlib.sha256()
    for name, content in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(content.replace(b"\r\n", b"\n")).digest())
    return h.hexdigest()


def _runtime_file(name):
    return "/" not in name and name.endswith(".py") and not name.startswith("test_")


def trusted_commit(revision):
    if revision != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("expected_commit")
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", revision + "^{commit}"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def expected_service_digest(revision="HEAD"):
    """Fingerprint the service modules from a trusted Git revision, independent of local edits."""
    revision = trusted_commit(revision)
    git = ["git", "-C", str(ROOT), "-c", "core.autocrlf=false"]
    archive = subprocess.check_output(
        git + ["archive", "--format=tar", revision + ":services/email-auth"], stderr=subprocess.DEVNULL
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = [(m.name, tar.extractfile(m).read()) for m in tar if m.isfile() and _runtime_file(m.name)]
    if len(files) < 2 or not {"portal.py", "external_mcp.py"} <= {name for name, _ in files}:
        raise ValueError("service_tree_missing")
    return _digest(files)


def loopback(url, path):
    try:
        u = urlsplit(url)
        return (
            u.scheme in {"http", "https"}
            and ipaddress.ip_address(u.hostname).is_loopback
            and u.username is None
            and u.password is None
            and u.port is not None
            and u.path == path
            and not u.query
            and not u.fragment
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def create_stage_app():
    """Optional gunicorn factory on an already provisioned isolated host. Use its existing vault and gate."""
    origin = os.environ.get("BASE_URL", "")
    if not loopback(origin, "") or urlsplit(origin).scheme != "https":
        raise RuntimeError("isolated HTTPS loopback BASE_URL required")
    root = Path(os.environ["SIDZEN_ACCEPTANCE_ROOT"]).resolve(strict=True)
    if root == root.parent:
        raise RuntimeError("dedicated staging directory required")
    for key in ("DATABASE_PATH", "VAULT_PATH", "OZON_FINANCE_GATE_DIR"):
        if root not in Path(os.environ[key]).resolve(strict=True).parents:
            raise RuntimeError("isolated database, vault and gate required")
    credential_root = Path(os.environ.get("CREDENTIALS_DIRECTORY", root)).resolve(strict=True)
    if credential_root == credential_root.parent:
        raise RuntimeError("dedicated credential directory required")
    for key in (
        "SESSION_SECRET_FILE",
        "VAULT_KEY_FILE",
        "READ_TOKEN_HASH_FILE",
        "MCP_READ_TOKEN_HASH_FILE",
    ):
        if key == "MCP_READ_TOKEN_HASH_FILE" and key not in os.environ:
            continue
        location = Path(os.environ[key]).resolve(strict=True)
        if not location.is_file() or not {root, credential_root}.intersection(location.parents):
            raise RuntimeError("dedicated staging credential mounts required")
    import portal  # Resolve the module from the isolated service's own working directory.

    service = Path(portal.__file__).resolve().parent
    fingerprint = _digest((p.name, p.read_bytes()) for p in service.iterdir() if _runtime_file(p.name))
    app = portal.create_portal()

    def staging(environ, start):
        if environ.get("PATH_INFO") != "/mcp":
            start("404 Not Found", [("Content-Length", "0")])
            return [b""]

        def with_build(status, headers, exc_info=None):
            return start(status, [*headers, (BUILD_HEADER, fingerprint)], exc_info)

        return app(environ, with_build)

    return staging


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, endpoint, digest, token):
        if not loopback(endpoint, "/mcp"):
            raise checks.Stop("blocked", "loopback_endpoint_required")
        if not token or len(token) > 8192 or not re.fullmatch(r"[!-~]+", token):
            raise checks.Stop("blocked", "staging_token_unavailable")
        self.url, self.digest, self.token = endpoint, digest, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.seq = 0

    def rpc(self, method, params=None, *, anonymous=False, notification=False):
        self.seq += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            message["id"] = self.seq
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if not anonymous:
            headers["Authorization"] = "Bearer " + self.token
        if method != "initialize":
            headers["MCP-Protocol-Version"] = PROTOCOL
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
        try:
            response = self.opener.open(req, timeout=45)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, TimeoutError):
            raise checks.Stop("blocked", "transport_unavailable") from None
        with response:
            if response.headers.get(BUILD_HEADER) != self.digest:
                raise checks.Stop("blocked", "source_version_mismatch")
            code = response.status
            if anonymous:
                if code != 401 or not re.match(
                    r"(?i)^Bearer(?:\s|$)", response.headers.get("WWW-Authenticate", "")
                ):
                    raise checks.Stop("blocked", "bearer_challenge_missing")
                return {}
            if code == 429:
                raise checks.Stop(
                    "pending",
                    "rate_limited",
                    checks.retry_seconds(response.headers.get("Retry-After")),
                    http=429,
                )
            if code in (401, 403):
                raise checks.Stop("blocked", "access_denied", http=code)
            if notification and code == 202:
                return {}
            if code != 200 or notification or response.headers.get_content_type() != "application/json":
                raise checks.Stop("blocked", "unexpected_mcp_response", http=code)
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise checks.Stop("partial", "response_size_limit")
        obj = json.loads(raw)
        if (
            not isinstance(obj, dict)
            or obj.get("jsonrpc") != "2.0"
            or obj.get("id") != self.seq
            or "error" in obj
            or not isinstance(obj.get("result"), dict)
        ):
            raise checks.Stop("blocked", "invalid_rpc_response")
        return obj["result"]

    def preflight(self, evidence):
        params = {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "sidzen-staging-acceptance", "version": "1"},
        }
        self.rpc("initialize", params, anonymous=True)
        server = self.rpc("initialize", params)
        if (
            server.get("protocolVersion") != PROTOCOL
            or not isinstance(server.get("serverInfo"), dict)
            or any(server["serverInfo"].get(k) != v for k, v in SERVER.items())
            or not isinstance(server.get("capabilities", {}).get("tools"), dict)
        ):
            raise checks.Stop("blocked", "mcp_version_mismatch")
        self.rpc("notifications/initialized", notification=True)
        names, cursors, args = set(), set(), {}
        for _ in range(64):
            page = self.rpc("tools/list", args)
            for tool in page["tools"]:
                name = tool["name"]
                if name in names:
                    raise checks.Stop("partial", "duplicate_catalog_tool")
                names.add(name)
                if name in CATALOG:
                    annotations = tool.get("annotations", {})
                    if (
                        annotations.get("readOnlyHint") is not True
                        or annotations.get("destructiveHint") is not False
                    ):
                        raise checks.Stop("blocked", "read_only_contract_missing")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise checks.Stop("partial", "catalog_cursor_invalid")
            cursors.add(cursor)
            args = {"cursor": cursor}
        else:
            raise checks.Stop("partial", "catalog_limit")
        if not CATALOG <= names:
            raise checks.Stop("blocked", "catalog_incomplete")
        evidence.update(
            authenticated=True,
            protocol=PROTOCOL,
            server=SERVER,
            service_source_sha256=self.digest,
            catalog_tools=len(names),
        )


class Reader:
    def __init__(self, client, options, evidence):
        self.client, self.options, self.evidence = client, options, evidence
        self.calls = 0
        self.wait_left = options.max_wait
        self.names = checks.SCENARIOS.get(options.scenario, ())

    def call(self, name, args):
        if name not in self.names or name not in CATALOG:
            raise checks.Stop("blocked", "scenario_tool_not_allowed")
        record = self.evidence["tools"].setdefault(
            name,
            {"calls": 0, "pages": 0, "rows": 0, "category": checks.category(name), "access": "untested"},
        )
        while True:
            if self.calls >= self.options.max_calls:
                raise checks.Stop("partial", "call_budget_reached")
            self.calls += 1
            record["calls"] += 1
            self.evidence["last_tool"] = name
            try:
                response = self.client.rpc("tools/call", {"name": name, "arguments": args})
                content = response.get("content", [])
                if len(content) != 1 or content[0].get("type") != "text":
                    raise checks.Stop("blocked", "unexpected_tool_response")
                data = json.loads(content[0]["text"])
                if not isinstance(data, dict):
                    raise checks.Stop("partial", "invalid_tool_data")
                missing = data.get("missing_or_partial_dates")
                if isinstance(missing, list) and "date_from" in args and "date_to" in args:
                    record["missing_observation_dates"] = sorted(
                        {
                            d
                            for d in missing
                            if isinstance(d, str)
                            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
                            and args["date_from"] <= d <= args["date_to"]
                        }
                    )
                if (
                    data.get("status") == "outside_provider_retention"
                    and type(data.get("retention_days")) is int
                ):
                    record["retention_days"] = data["retention_days"]
                if (
                    name in checks.ASYNC_TOOLS
                    and "task_id" in args
                    and data.get("task_id", args["task_id"]) != args["task_id"]
                ):
                    raise checks.Stop("partial", "task_identity_changed")
                outcome = checks.source_outcome(data, response.get("isError") is True)
                if outcome:
                    if data.get("task_id") and name in checks.ASYNC_TOOLS:
                        task = data["task_id"]
                        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", task):
                            raise checks.Stop("partial", "invalid_task_id")
                        if args.get("task_id", task) != task:
                            raise checks.Stop("partial", "task_identity_changed")
                        args["task_id"] = task
                        record["task_identified"] = True
                    raise outcome
                if data.get("read_only", True) is not True or data.get("tool", name) != name:
                    raise checks.Stop("blocked", "tool_identity_mismatch")
                record["access"] = "observed_for_method"
                return data
            except checks.Stop as exc:
                if exc.code in ("access_denied", "provider_access_denied", "not_configured"):
                    record["access"] = exc.code
                    if exc.category:
                        record["denied_category"] = exc.category
                delay = exc.delay
                if exc.status == "pending":
                    record["pending_responses"] = record.get("pending_responses", 0) + 1
                    record["last_pending_reason"] = exc.code
                    record["last_retry_after_seconds"] = delay
                if exc.status != "pending" or delay is None or max(1, delay) > self.wait_left:
                    raise
                wait = max(1, delay)
                self.wait_left -= wait
                time.sleep(wait)

    def pages(self, name, **args):
        seen = set()
        while True:
            data = self.call(name, args)
            rows = data.get("items")
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise checks.Stop("partial", "missing_rows")
            stats = self.evidence["tools"][name]
            stats["pages"] += 1
            stats["rows"] += len(rows)
            cursor = data.get("next_cursor")
            if type(data.get("complete")) is not bool:
                raise checks.Stop("partial", "completion_flag_missing")
            if "more_supply_details_pending" in data.get("issues", []):
                raise checks.Stop(
                    "pending",
                    "supply_details_pending",
                    checks.retry_seconds(data.get("retry_after_seconds")),
                )
            if data.get("issues") or data.get("missing_skus") or data.get("invalid_stock_skus"):
                raise checks.Stop("partial", "source_coverage_incomplete")
            if cursor is None:
                if data["complete"] is not True:
                    raise checks.Stop("partial", "incomplete_terminal_page")
                yield data
                return
            if (
                data["complete"]
                or type(cursor) not in (int, str)
                or cursor in ("", 0)
                or str(cursor) in seen
                or isinstance(cursor, int)
                and cursor < 0
            ):
                raise checks.Stop("partial", "nonadvancing_cursor")
            key = checks.CURSORS.get(name)
            if key is None or (isinstance(cursor, int) and cursor <= args.get(key, 0)):
                raise checks.Stop("partial", "nonadvancing_cursor")
            yield data
            seen.add(str(cursor))
            if data.get("task_id") and name in checks.ASYNC_TOOLS:
                if args.get("task_id", data["task_id"]) != data["task_id"]:
                    raise checks.Stop("partial", "task_identity_changed")
                args["task_id"] = data["task_id"]
            args[key] = cursor

    def rows(self, name, **args):
        return [row for page in self.pages(name, **args) for row in page["items"]]


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://127.0.0.1:8765/mcp")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file for this single run")
    parser.add_argument("--expected-commit", default="HEAD", help="Trusted release SHA, default Git HEAD")
    parser.add_argument("--scenario", type=int, choices=range(1, 10))
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--month", default="2026-10")
    parser.add_argument("--warehouse-id", type=checks.positive, action="append", default=[])
    parser.add_argument("--expect-ozon-sku", type=checks.positive, action="append", default=[])
    parser.add_argument("--wb-campaign-id", type=checks.positive, action="append", default=[])
    parser.add_argument("--ozon-campaign-id", type=checks.positive, action="append", default=[])
    parser.add_argument("--max-calls", type=checks.positive, default=512)
    parser.add_argument(
        "--max-wait", type=int, default=0, help="Total seconds allowed for explicit retry delays"
    )
    opts = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", opts.month) or not 0 <= opts.max_wait <= 604800:
        parser.error("invalid month or wait budget")
    opts.month_start = date.fromisoformat(opts.month + "-01")
    opts.month_end = opts.month_start.replace(
        day=calendar.monthrange(opts.month_start.year, opts.month_start.month)[1]
    )
    now = datetime.now(timezone.utc)
    opts.order_as_of = {
        p: opts.as_of or now.astimezone(z).date()
        for p, z in (("ozon", timezone.utc), ("wb", checks.MOSCOW))
    }
    opts.as_of = opts.as_of or opts.order_as_of["wb"]
    return opts


def main(argv=None):
    opts = arguments(argv)
    try:
        opts.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(opts.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        print("A new writable evidence file is required.", file=sys.stderr)
        return 2
    evidence = {
        "scenario": opts.scenario,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "status": "blocked",
        "authenticated": False,
        "tools": {},
        "checks": [],
        "business_acceptance": "requires_independent_review",
    }
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        try:
            evidence["expected_git_commit"] = trusted_commit(opts.expected_commit)
            digest = expected_service_digest(evidence["expected_git_commit"])
            client = Client(opts.endpoint, digest, os.environ.get("SIDZEN_ACCEPTANCE_TOKEN"))
            client.preflight(evidence)
            if opts.scenario:
                checks.evaluate(Reader(client, opts, evidence), opts)
                evidence["status"] = "evidence_ready"
            else:
                evidence["status"] = "preflight_ok"
        except checks.Stop as exc:
            evidence.update(status=exc.status, code=exc.code)
            if exc.http is not None:
                evidence["http_status"] = exc.http
            if exc.status == "pending":
                evidence["retry_after_seconds"] = exc.delay
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            IndexError,
            ArithmeticError,
            OSError,
            subprocess.SubprocessError,
            http.client.HTTPException,
            tarfile.TarError,
        ):
            evidence.update(status="blocked", code="invalid_source_or_runtime")
        finally:
            evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
            json.dump(evidence, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write("\n")
    print(evidence["status"], evidence.get("code", ""))
    return 0 if evidence["status"] in ("evidence_ready", "preflight_ok") else 2


if __name__ == "__main__":
    sys.exit(main())
