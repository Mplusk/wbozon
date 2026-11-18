"""Offline Avito contract, ownership, OAuth and upstream quota checks."""
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

import avito_mcp
import external_mcp
import finance_gate
import market_transport
import test_external_mcp_integration
import test_oauth_mcp
import test_portal


class Response(io.BytesIO):
    status = 200


class SellerAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = str(Path(self.tmp.name) / "gate")
        self.creds = {"client_id":"test-avito-client", "client_secret":"test-avito-secret"}
        self.token = "sample-private-avito-access"

    def read(self, name, args):
        return avito_mcp.read(name, args, self.creds, self.dir, time.monotonic()+15)

    def test_each_published_schema_builds_a_request_to_its_fixed_endpoint(self):
        def sample(prop):
            if "enum" in prop:
                return prop["enum"][0]
            if prop["type"] == "object":
                return {key:sample(prop["properties"][key]) for key in prop.get("required",())}
            if prop["type"] == "array":
                return [sample(prop["items"])]
            if prop["type"] == "string":
                return {"date":"2026-11-01","date-time":"2026-11-01T12:00:00Z"}.get(prop.get("format"), "fixture")
            return True if prop["type"] == "boolean" else max(1,prop.get("minimum",1))
        self.assertEqual(len(avito_mcp.CONTRACTS),33)
        for name, op in avito_mcp.CONTRACTS.items():
            with self.subTest(tool=name):
                args = sample(avito_mcp.input_schema(name))
                request = avito_mcp.build_read(name, args, "fixture", 71)
                self.assertEqual(request.host,"api.avito.ru")
                self.assertNotIn("{",request.selector)
                self.assertEqual(request.get_method(),op["method"])
                if "{user_id}" in op["path"]:
                    self.assertIn("/accounts/71/",request.selector)

    def test_token_own_account_message_projection_and_encrypted_cache(self):
        paths = []
        def open_request(req, timeout):
            paths.append(req.selector)
            if req.selector == "/token":
                self.assertEqual(req.get_method(),"POST")
                self.assertIn(b"grant_type=client_credentials",req.data)
                self.assertNotIn(self.creds["client_secret"],req.full_url)
                data = {"access_token":self.token,"expires_in":3600,"token_type":"Bearer"}
            elif req.selector == "/core/v1/accounts/self":
                self.assertEqual(req.get_header("Authorization"),"Bearer "+self.token)
                data = {"id":97,"email":"private@example.test"}
            else:
                self.assertEqual(req.selector, "/messenger/v3/accounts/97/chats/chat_1/messages/?limit=2&offset=0")
                self.assertEqual(req.get_method(),"GET")
                data = [{"id":"m1","content":{"text":"Тест","url":"https://files.example/private?signature=test"}}]
            return Response(json.dumps(data).encode())
        with patch.object(market_transport.urllib.request.OpenerDirector,"open",side_effect=open_request):
            for _ in range(2):
                report = self.read("avito_get_messages_v3",{"chat_id":"chat_1","limit":2,"offset":0})
                self.assertFalse(report["pagination"]["has_more"])
                self.assertEqual(report["data"][0]["content"],{"text":"Тест"})
        self.assertEqual(paths.count("/token"),1)
        self.assertEqual(paths.count("/core/v1/accounts/self"),1)
        db=(Path(self.dir).parent/".market-reports"/"reports.db").read_bytes()
        self.assertNotIn(self.token.encode(),db)
        self.assertNotIn(self.creds["client_secret"].encode(),db)

    def test_unknown_and_known_429_stop_next_worker(self):
        for index, headers in enumerate(({},{"Retry-After":"900"})):
            creds=dict(self.creds,client_id="synthetic-"+str(index))
            def open_request(req, timeout):
                if req.selector == "/token":
                    return Response(json.dumps({"access_token":self.token,"expires_in":1200}).encode())
                raise urllib.error.HTTPError(req.full_url,429,"quota",headers,None)
            with patch.object(market_transport.urllib.request.OpenerDirector,"open",side_effect=open_request) as http:
                for attempt, expected in enumerate((urllib.error.HTTPError, finance_gate.GateError)):
                    with self.assertRaises(expected) as raised:
                        avito_mcp.read("avito_get_items_info",{},creds,self.dir,time.monotonic()+12)
                    if attempt:
                        self.assertEqual(raised.exception.reason,"cooldown" if headers else "retry_time_unknown")
                self.assertEqual(http.call_count,2)

    def test_no_custom_account_urls_or_write_commands(self):
        for extra in ({"user_id":1},{"url":"https://example.test"},{"chat_id":"../users"},{"chat_id":"chat/other"}):
            with self.assertRaises(ValueError):
                avito_mcp.build_read("avito_get_messages_v3",extra,"token",77)
        with self.assertRaises(ValueError):
            avito_mcp.validate("avito_get_stocks_info",{"item_ids":[True]})
        req=avito_mcp.build_read("avito_get_stocks_info",{"item_ids":[321]},"token",None)
        self.assertEqual((req.get_method(),req.selector,json.loads(req.data)),("POST","/stock-management/1/info",{"item_ids":[321]}))
        self.assertNotIn("avito_post_send_message",avito_mcp.CONTRACTS)
        self.assertNotIn("avito_update_price",avito_mcp.CONTRACTS)

    def test_pagination_uses_provider_totals_and_nested_request_shapes(self):
        uploads = {"meta":{"page":1,"pages":5,"total":47}, "uploads":[{}]*10}
        page = avito_mcp._pagination("avito_get_uploads", {}, uploads)
        self.assertEqual((page["limit"], page["next"], page["has_more"]), (10, 2, True))
        analytics = {"result":{"dataTotalCount":3,"groupings":[{},{}]}}
        page = avito_mcp._pagination("avito_item_analytics", {"limit":2}, analytics)
        self.assertEqual((page["next"],page["has_more"]), (2,True))
        page = avito_mcp._pagination("avito_list_orders_by_user_v1", {"pagination":{"page":2}},
                                     {"pagination":{"hasNextPage":True},"orders":[]})
        self.assertEqual((page["parameter"],page["next"]), ("pagination.page",3))
        no_next = avito_mcp._pagination("avito_get_messages_v3", {"offset":1000,"limit":1}, [{"id":"a"}])
        self.assertIsNone(no_next["next"])
        request = avito_mcp.build_read("avito_get_items_info", {"status":"active,old"}, "fixture", None)
        self.assertIn("status=active%2Cold", request.selector)
        with self.assertRaises(ValueError):
            avito_mcp.validate("avito_get_items_info", {"status":"active,wrong"})

    def test_money_preserves_decimal_digits_and_filters_nested_links(self):
        def open_request(req, timeout):
            if req.selector == "/token":
                return Response(json.dumps({"access_token":self.token,"expires_in":3600}).encode())
            if req.selector == "/core/v1/accounts/self":
                return Response(b'{"id":94}')
            self.assertEqual(req.selector, "/core/v1/accounts/94/balance/")
            return Response(b'{"real":17.100000000000001,"urls":["https://files.example/?sig=hidden"],"extra":{"buyerPhone":"private"}}')
        with patch.object(market_transport.urllib.request.OpenerDirector, "open", side_effect=open_request):
            data = self.read("avito_get_user_balance", {})["data"]
        self.assertEqual(data, {"real":"17.100000000000001", "urls":[], "extra":{}})

    def test_returned_credentials_with_json_escaping_are_rejected(self):
        self.creds["client_secret"] = 'synthetic-"quoted-secret'
        def open_request(req, timeout):
            if req.selector == "/token":
                return Response(json.dumps({"access_token":self.token,"expires_in":3600}).encode())
            return Response(json.dumps({"description":"private " + self.creds["client_secret"]}).encode())
        with patch.object(market_transport.urllib.request.OpenerDirector, "open", side_effect=open_request):
            with self.assertRaises(finance_gate.ProviderResponseInvalid):
                self.read("avito_get_items_info", {})
        delivery = {"courierInfo":{"address":"private"},"terminalInfo":{"address":"Public pickup point","code":"1"},
                    "buyerInfo":{"fullName":"private","phoneNumber":"private"}}
        self.assertEqual(avito_mcp._project({"delivery":delivery})["delivery"],
                         {"terminalInfo":{"address":"Public pickup point","code":"1"}})


class KeyAccess(unittest.TestCase):
    setUp = test_portal.Tests.setUp
    tearDown = test_portal.Tests.tearDown
    request = test_portal.Tests.request
    mcp = test_external_mcp_integration.ExternalMCPIntegrationTests.mcp

    def rpc(self, token, method, **params):
        return json.loads(self.mcp({"jsonrpc":"2.0","id":1,"method":method,"params":params},token)["body"])

    def test_owner_can_save_and_check_avito_and_issue_key_in_normal_settings(self):
        saved = {"provider":"avito","client_id":"synthetic-owner","client_secret":"private-synthetic",
                 "csrf":self.csrf}
        self.assertTrue(self.request(method="POST",data=saved)["status"].startswith("200"))
        with open(self.vault.path, "rb") as db:
            self.assertNotIn(b"private-synthetic", db.read())
        def gateway(name, args, creds):
            self.assertEqual(name, "avito_get_user_info_self")
            self.assertEqual(creds["client_secret"], "private-synthetic")
            return {"items":[{"id":38}],"data":{"id":38}}
        self.app.gateway = gateway
        checked = self.request("/settings/check","POST",{"provider":"avito","csrf":self.csrf})
        self.assertTrue(checked["status"].startswith("303"))
        self.assertEqual(self.vault.check_status("avito")[0],"ok")
        page = self.request()["body"]
        self.assertIn("Последняя проверка",page)
        self.assertNotIn(saved["client_secret"],page)
        self.now += 301
        self.assertTrue(self.request(method="POST",data=saved)["status"].startswith("403"))
        created = self.request("/settings/mcp/create","POST",{"csrf":self.csrf,"request_id":secrets.token_urlsafe(16),
                                                              "name":"Avito reader","access":"avito"})
        self.assertEqual(created["status"],"200 OK")
        token = re.search(r'mcp_[A-Za-z0-9_-]{43}',created["body"]).group()
        response = self.rpc(token,"tools/call",name="avito_get_user_info_self",arguments={})
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["data"], {"id":38})
        self.assertEqual(self.app.external_keys.list_active()[0][3],self.now)
        old = self.request("/api/read","POST",{"operation":"avito_get_user_info_self","arguments":{}},
                           auth=self.token,cookie=False)
        self.assertTrue(old["status"].startswith("400"))

    def test_catalog_and_direct_calls_are_scoped_before_credential_lookup(self):
        for area, show_avito, show_legacy in (("marketplaces",False,True),("avito",True,False),("all",True,True)):
            key_id, token=self.app.external_keys.create(area,secrets.token_urlsafe(16),self.now,area)
            tools=self.rpc(token,"tools/list")["result"]["tools"]
            names={v["name"] for v in tools}
            self.assertEqual("avito_get_orders" in names,show_avito)
            self.assertEqual("mcp_health" in names,show_legacy)
            self.assertEqual(len(names),(len(avito_mcp.CONTRACTS) if show_avito else 0)+(len(external_mcp.TOOLS) if show_legacy else 0))
            for name, permitted in (("avito_get_orders",show_avito),("ozon_products",show_legacy)):
                data=self.rpc(token,"tools/call",name=name,arguments={})
                if permitted:
                    self.assertEqual(json.loads(data["result"]["content"][0]["text"])["error"],"not_configured")
                else:
                    self.assertEqual(data["error"]["code"],-32602)
            if area != "marketplaces":
                with sqlite3.connect(self.vault.path) as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM external_mcp_keys WHERE public_id=?",(key_id,)).fetchone())
            self.assertTrue(self.app.external_keys.revoke(key_id,self.now))
            self.assertEqual(self.rpc(token,"tools/list")["error"],"unauthorized")


class ScopedOAuth(unittest.TestCase):
    setUp = test_oauth_mcp.Tests.setUp
    tearDown = test_oauth_mcp.Tests.tearDown
    auth_params = test_oauth_mcp.Tests.auth_params
    request = test_oauth_mcp.Tests.request
    approve = test_oauth_mcp.Tests.approve
    token_request = test_oauth_mcp.Tests.token_request

    def test_avito_key_remains_bound_on_exchange_refresh_and_revocation(self):
        self.key_id,self.secret=self.keys.create("Avito",secrets.token_urlsafe(16),self.now,"avito")
        code,_=self.approve()
        tokens=self.store.exchange(self.token_request(code),self.resource)
        refreshed=self.store.exchange(dict(grant_type="refresh_token",client_id=self.client,
                                           refresh_token=tokens["refresh_token"]), self.resource)
        self.assertEqual(self.store.verify(refreshed["access_token"],self.resource),self.key_id)
        self.keys.revoke(self.key_id,self.now)
        self.assertIsNone(self.store.verify(refreshed["access_token"],self.resource))
