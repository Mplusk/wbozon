"""Offline, synthetic HTTP contract tests for the independent MCP acceptance CLI."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "tools" / "mcp_acceptance.py"
TOKEN = "mcp_" + "S" * 43  # Synthetic value; never a real credential.
SKU_1 = "900100100100001"
SKU_2 = "900100100100002"
WB_NM = 900100100100003
WAREHOUSE = 900100100100004
PURGED_TASK = "synthetic-purged-task-900100100100005"
WAREHOUSE_NAME = "synthetic-private-warehouse-name"
AS_OF = "2026-11-18"

# Public discovery contract: the 23 expansion tools plus planning and the two
# existing catalogue tools needed to resolve a product. No service import or
# live seller data is used to construct this independent list.
TOOL_NAMES = (
    "wb_supplies",
    "wb_supply_goods",
    "wb_stocks",
    "wb_orders",
    "wb_sales",
    "wb_acceptance_slots",
    "ozon_supply_orders",
    "ozon_supply_items",
    "ozon_stocks",
    "ozon_stocks_by_cluster",
    "ozon_sku_daily",
    "wb_finance_report",
    "wb_paid_storage",
    "wb_acceptance_report",
    "ozon_cash_flow",
    "ozon_fbo_postings_geo",
    "ozon_clusters",
    "wb_funnel",
    "wb_ads",
    "ozon_ads",
    "wb_feedbacks",
    "ozon_reviews",
    "ozon_rating",
    "supply_plan",
    "ozon_product_info",
    "wb_products",
)


def slots(*, omit_day=False, incomplete_second_box=False):
    first = date.fromisoformat(AS_OF)
    data = [
        {
            "date": (first + timedelta(days=i)).isoformat(),
            "warehouse_id": WAREHOUSE,
            "warehouse": WAREHOUSE_NAME,
            "box_type_id": 1,
            "coefficient": 0,
            "allow_unload": True,
            "available": True,
        }
        for i in range(14)
        if not (omit_day and i == 5)
    ]
    if incomplete_second_box:
        data += [
            dict(row, box_type_id=2)
            for row in data
            if row["date"] != (first + timedelta(days=5)).isoformat()
        ]
    return {
        "items": data,
        "complete": True,  # The verifier must check the dates, not trust this flag.
        "next_cursor": None,
        "horizon_days": 14,
        "date_from": AS_OF,
        "date_to": (first + timedelta(days=13)).isoformat(),
        "status": "reported",
        "warehouses_status": "reported",
        "warehouses": [{"warehouse_id": WAREHOUSE, "name": WAREHOUSE_NAME}],
        "missing_dates_by_warehouse": {},
        "missing_dates_by_box_type": {},
    }


def product_catalog():
    return {
        "items": [
            {
                "offer_id": "300030",
                "sku": SKU_1,
                "sku_candidates": [SKU_1, SKU_2],
                "sources": [{"sku": SKU_1, "source": "FBO"}, {"sku": SKU_2, "source": "FBO"}],
                "incomplete_fields": [],
                "matched_identifiers": ["300030"],
            }
        ],
        "requested_identifier_type": "offer_ids",
        "unmatched_identifiers": [],
        "unmatched_row_indices": [],
        "conflicts": [],
        "sku_coverage": "not_verified",
    }


def stock_payloads(mode):
    ozon_cluster = {
        "items": [
            {
                "sku": SKU_1,
                "article": "300030",
                "warehouse_id": WAREHOUSE,
                "cluster_id": 29,
                "available_stock_count": 3,
                "transit_stock_count": 1,
            }
        ],
        "complete": True,
        "next_cursor": None,
        "missing_skus": [],
        "invalid_stock_skus": [],
        "fulfillment_scope": "FBO",
    }
    if mode != "missing_sku":
        ozon_cluster["items"].append(
            {
                "sku": SKU_2,
                "article": "300030",
                "warehouse_id": WAREHOUSE,
                "cluster_id": 29,
                "available_stock_count": 2,
                "transit_stock_count": 0,
            }
        )
    wb = {
        "items": [
            {
                "article": "3000030",
                "nm_id": WB_NM,
                "warehouse": WAREHOUSE_NAME,
                "kind": "warehouse",
                "quantity": 2,
            }
        ],
        "complete": True,
        "next_cursor": None,
    }
    if mode == "purged":
        wb = {
            "items": [],
            "complete": False,
            "next_cursor": None,
            "status": "report_unavailable",
            "provider_status": "purged",
            "task_id": PURGED_TASK,
        }
    return {
        "ozon_product_info": product_catalog(),
        "wb_products": {
            "items": [{"nmID": WB_NM, "vendorCode": "3000030"}],
            "cursor": {"updatedAt": "2026-11-17T12:00:00Z", "nmID": WB_NM, "total": 1},
            "first_page_only": False,
        },
        "wb_stocks": wb,
        "ozon_stocks_by_cluster": ozon_cluster,
        "ozon_stocks": {
            "items": [
                {"sku": SKU_1, "article": "300030", "scheme": "fbo", "present": 3},
                {"sku": SKU_2, "article": "300030", "scheme": "fbo", "present": 2},
            ],
            "complete": True,
            "next_cursor": None,
        },
    }


class SyntheticMCP:
    def __init__(self, digest, *, mode="ok", wrong_digest=False, bad_challenge=False, unsafe_tool=False):
        self.digest = digest
        self.mode = mode
        self.wrong_digest = wrong_digest
        self.bad_challenge = bad_challenge
        self.unsafe_tool = unsafe_tool
        self.events = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(self, status, body=None, **extra):
                raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                wrong = ("1" if state.digest == "0" * 64 else "0") * 64
                bad_digest = state.wrong_digest or getattr(self, "digest_drift", False)
                self.send_header("X-Sidzen-Service-SHA256", wrong if bad_digest else state.digest)
                for name, value in extra.items():
                    self.send_header(name.replace("_", "-"), str(value))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def handle_request(self):
                parsed = urlsplit(self.path)
                authorization = self.headers.get("Authorization")
                authenticated = authorization == "Bearer " + TOKEN
                length = int(self.headers.get("Content-Length", "0"))
                request_bytes = self.rfile.read(length) if length else b""
                if parsed.path != "/mcp" or parsed.query or parsed.fragment:
                    state.events.append(
                        {
                            "authenticated": authenticated,
                            "authorization_present": authorization is not None,
                            "unexpected_path": True,
                        }
                    )
                    self.reply(404, {"error": "unexpected_path"})
                    return
                if not authenticated:
                    state.events.append(
                        {
                            "authenticated": False,
                            "http_method": self.command,
                            "authorization_present": authorization is not None,
                        }
                    )
                    challenge = (
                        "Basic realm=synthetic"
                        if state.bad_challenge
                        else 'Bearer resource_metadata="http://127.0.0.1/.well-known/oauth-protected-resource/mcp"'
                    )
                    self.reply(401, {"error": "unauthorized"}, WWW_Authenticate=challenge)
                    return
                payload = json.loads(request_bytes or b"{}")
                method = payload.get("method")
                params = payload.get("params") or {}
                self.digest_drift = state.mode == "digest_drift" and method == "tools/list"
                event = {"authenticated": True, "method": method}
                if method == "tools/call":
                    event.update(name=params.get("name"), arguments=params.get("arguments") or {})
                state.events.append(event)
                ident = payload.get("id")
                if method == "notifications/initialized":
                    self.reply(202)
                    return
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "sidzen-marketplace-readonly", "version": "1.0.0"},
                    }
                    if state.mode == "wrong_protocol":
                        result["protocolVersion"] = "2024-11-05"
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": name,
                                "description": "synthetic",
                                "inputSchema": {"type": "object", "properties": {}},
                                "annotations": {
                                    "readOnlyHint": not (state.unsafe_tool and name == "wb_stocks"),
                                    "destructiveHint": False,
                                },
                            }
                            for name in TOOL_NAMES
                        ]
                    }
                elif method == "tools/call":
                    name = params.get("name")
                    if name == "wb_acceptance_slots":
                        if state.mode == "http_403":
                            self.reply(403, {"error": "forbidden"})
                            return
                        if state.mode == "http_429":
                            self.reply(429, {"error": "rate_limited"}, Retry_After="47")
                            return
                        if state.mode == "mcp_429":
                            result = {
                                "isError": True,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {"error": "rate_limited", "retry_after_seconds": 47}
                                        ),
                                    }
                                ],
                            }
                        elif state.mode == "forbidden":
                            result = {
                                "isError": True,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {
                                                "status": "provider_access_denied",
                                                "http_status": 403,
                                                "required_category": "Тарифы",
                                                "complete": False,
                                            },
                                            ensure_ascii=False,
                                        ),
                                    }
                                ],
                            }
                        else:
                            result = {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            slots(
                                                omit_day=state.mode == "partial",
                                                incomplete_second_box=state.mode == "partial_box",
                                            ),
                                            ensure_ascii=False,
                                        ),
                                    }
                                ]
                            }
                    else:
                        data = stock_payloads(state.mode).get(name)
                        if data is None:
                            self.reply(
                                200,
                                {
                                    "jsonrpc": "2.0",
                                    "id": ident,
                                    "error": {"code": -32601, "message": "unexpected synthetic tool"},
                                },
                            )
                            return
                        result = {
                            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
                        }
                else:
                    self.reply(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": ident,
                            "error": {"code": -32601, "message": "unexpected synthetic method"},
                        },
                    )
                    return
                self.reply(200, {"jsonrpc": "2.0", "id": ident, "result": result})

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    @property
    def endpoint(self):
        return "http://127.0.0.1:%d/mcp" % self.server.server_port

    @property
    def tool_calls(self):
        return [e for e in self.events if e.get("method") == "tools/call"]


def values_with_key(value, key):
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                yield item
            yield from values_with_key(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from values_with_key(item, key)


def scalar_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from scalar_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from scalar_values(item)
    else:
        yield value


class MCPAcceptanceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools import mcp_acceptance

        cls.digest = mcp_acceptance.expected_service_digest()
        if not re.fullmatch(r"[0-9a-f]{64}", cls.digest):
            raise AssertionError("invalid expected service digest")

    def run_cli(self, server, *arguments):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance-result.json"
            env = os.environ.copy()
            env.update(
                SIDZEN_ACCEPTANCE_TOKEN=TOKEN,
                NO_PROXY="127.0.0.1,localhost",
                no_proxy="127.0.0.1,localhost",
                HTTP_PROXY="",
                HTTPS_PROXY="",
                ALL_PROXY="",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--endpoint",
                    server.endpoint,
                    "--output",
                    str(output),
                    *arguments,
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                timeout=12,
            )
            self.assertTrue(output.is_file(), (result.returncode, result.stdout, result.stderr))
            raw = output.read_text(encoding="utf-8")
            document = json.loads(raw)
            for private in (TOKEN, SKU_1, SKU_2, str(WB_NM), str(WAREHOUSE), PURGED_TASK, WAREHOUSE_NAME):
                self.assertNotIn(private, raw)
            for value in scalar_values(document):
                if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                    continue  # The trusted source digest is public, not an identifier in the response.
                if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", value):
                    continue  # Dates and timestamps can coincidentally contain the sample digits.
                if isinstance(value, (str, int)) and type(value) is not bool:
                    for article in ("300030", "3000030"):
                        self.assertIsNone(re.search(r"(?<!\d)" + article + r"(?!\d)", str(value)))
            self.assertNotIn(TOKEN.encode(), result.stdout)
            self.assertNotIn(TOKEN.encode(), result.stderr)
            return result, document

    def test_preflight_discovers_only_after_unauthenticated_challenge(self):
        with SyntheticMCP(self.digest) as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "preflight_ok")
        self.assertTrue(server.events)
        self.assertFalse(server.events[0]["authenticated"])
        methods = [e.get("method") for e in server.events if e["authenticated"]]
        self.assertIn("initialize", methods)
        self.assertIn("notifications/initialized", methods)
        self.assertIn("tools/list", methods)
        self.assertLess(methods.index("initialize"), methods.index("notifications/initialized"))
        self.assertLess(methods.index("notifications/initialized"), methods.index("tools/list"))
        self.assertEqual(server.tool_calls, [])

    def test_wrong_service_digest_stops_before_bearer(self):
        with SyntheticMCP(self.digest, wrong_digest=True) as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("blocked", "source_version_mismatch"))
        self.assertTrue(server.events)
        self.assertFalse(any(e["authenticated"] for e in server.events))
        self.assertFalse(any(e.get("authorization_present") for e in server.events))

    def test_protocol_mismatch_stops_before_discovery(self):
        with SyntheticMCP(self.digest, mode="wrong_protocol") as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "mcp_version_mismatch")
        self.assertFalse(any(e.get("method") == "tools/list" for e in server.events))

    def test_unsupported_authentication_challenge_stops_before_bearer(self):
        with SyntheticMCP(self.digest, bad_challenge=True) as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("blocked", "bearer_challenge_missing"))
        self.assertFalse(any(e["authenticated"] for e in server.events))
        self.assertFalse(any(e.get("authorization_present") for e in server.events))

    def test_digest_change_during_preflight_stops_before_tool_calls(self):
        with SyntheticMCP(self.digest, mode="digest_drift") as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("blocked", "source_version_mismatch"))
        self.assertIn("initialize", [e.get("method") for e in server.events if e["authenticated"]])
        self.assertEqual(server.tool_calls, [])

    def test_unsafe_tool_annotation_blocks_preflight(self):
        with SyntheticMCP(self.digest, unsafe_tool=True) as server:
            result, report = self.run_cli(server)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("blocked", "read_only_contract_missing"))
        self.assertEqual(server.tool_calls, [])

    def test_scenario_4_requires_fourteen_real_calendar_dates(self):
        for mode, expected in (("ok", 0), ("partial", 2), ("partial_box", 2)):
            with self.subTest(mode=mode), SyntheticMCP(self.digest, mode=mode) as server:
                result, report = self.run_cli(
                    server, "--scenario", "4", "--as-of", AS_OF, "--warehouse-id", str(WAREHOUSE)
                )
                self.assertEqual(result.returncode, expected)
                self.assertEqual(report["status"], "evidence_ready" if mode == "ok" else "partial")
                if mode != "ok":
                    self.assertEqual(report["code"], "slot_dates_missing")
                (call,) = server.tool_calls
                self.assertEqual(call["name"], "wb_acceptance_slots")
                self.assertEqual(str(call["arguments"].get("warehouse_id")), str(WAREHOUSE))
                self.assertIs(call["arguments"].get("include_warehouses"), True)

    def test_scenario_4_permission_denial_is_blocked(self):
        for mode, code in (("forbidden", "provider_access_denied"), ("http_403", "access_denied")):
            with self.subTest(mode=mode), SyntheticMCP(self.digest, mode=mode) as server:
                result, report = self.run_cli(
                    server, "--scenario", "4", "--as-of", AS_OF, "--warehouse-id", str(WAREHOUSE)
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual((report["status"], report["code"]), ("blocked", code))
                self.assertEqual([c["name"] for c in server.tool_calls], ["wb_acceptance_slots"])
                self.assertIn(403, values_with_key(report, "http_status"))

    def test_scenario_4_preserves_http_and_mcp_retry_after_without_retrying(self):
        for mode in ("http_429", "mcp_429"):
            with self.subTest(mode=mode), SyntheticMCP(self.digest, mode=mode) as server:
                result, report = self.run_cli(
                    server, "--scenario", "4", "--as-of", AS_OF, "--warehouse-id", str(WAREHOUSE)
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual((report["status"], report["code"]), ("pending", "rate_limited"))
                self.assertEqual([c["name"] for c in server.tool_calls], ["wb_acceptance_slots"])
                self.assertIn(47, values_with_key(report, "retry_after_seconds"))

    def test_scenario_2_expired_wb_task_is_not_complete(self):
        with SyntheticMCP(self.digest, mode="purged") as server:
            result, report = self.run_cli(server, "--scenario", "2", "--as-of", AS_OF)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("blocked", "report_expired"))
        self.assertEqual([c["name"] for c in server.tool_calls].count("wb_stocks"), 1)

    def test_scenario_2_missing_expected_ozon_sku_is_not_complete(self):
        with SyntheticMCP(self.digest, mode="missing_sku") as server:
            result, report = self.run_cli(
                server, "--scenario", "2", "--as-of", AS_OF, "--expect-ozon-sku", SKU_2
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual((report["status"], report["code"]), ("partial", "expected_sku_missing"))
        self.assertIn("ozon_stocks_by_cluster", [c["name"] for c in server.tool_calls])


if __name__ == "__main__":
    unittest.main()
