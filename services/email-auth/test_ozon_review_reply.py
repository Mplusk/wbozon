import base64
import hashlib
import io
import json
import re
import secrets
import threading
import time
import unittest
import urllib.error
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import market_actions
import market_catalog
import market_transport
import oauth_mcp
import ozon_expansion
import ozon_review_reply as replies
import test_external_mcp_integration
import test_market_catalog
import test_oauth_integration
import test_portal


REVIEW = "review-903"
SKU = 9007199254740993
TEXT = "Спасибо за отзыв!\nУчтём замечание.  "
COMMENT = "comment-420"
REVIEWS = "/v2/review/list"
ARGS = {"review_id": REVIEW, "text": TEXT}
DATES = {"date_from": "2026-11-01", "date_to": "2026-11-15"}
EMPTY = {"comments": [], "offset": 0}


def comment(ident=COMMENT, text=TEXT, *, owner=True, published=True):
    return {"id": ident, "text": text, "is_owner": owner, "is_published": published, "is_rejected": False}


class OzonReviewReplyTests(unittest.TestCase):
    request = test_portal.Tests.request
    mcp = test_external_mcp_integration.ExternalMCPIntegrationTests.mcp
    call = test_market_catalog.CatalogTests.call
    key = test_market_catalog.CatalogTests.key
    oauth_http = test_oauth_integration.OAuthPortalIntegrationTests.call

    def setUp(self):
        test_portal.Tests.setUp(self)
        self.addCleanup(test_portal.Tests.tearDown, self)
        self.vault.save("ozon", {"client_id": "123", "api_key": "fixture-seller-credential"})
        self.key_id, self.token = self.key("catalog_operations")
        self.responses = defaultdict(deque)
        self.wire_calls = []
        mock = patch("urllib.request.OpenerDirector.open", side_effect=self.provider)
        mock.start()
        self.addCleanup(mock.stop)

        def gateway(operation, arguments, credentials, directory=None, *, dispatch=None):
            deadline = time.monotonic() + 15
            directory = directory or self.app.finance_gate_dir
            if operation == "ozon_reviews":
                return ozon_expansion.read(operation, arguments,
                    market_transport.Wire("ozon", credentials, directory, deadline))
            self.assertEqual(operation, "market_api_propose")
            with market_actions.transmission(dispatch, arguments):
                return market_catalog.transfer(operation, arguments, credentials, directory, deadline)

        self.app.gateway = self.app.native_provider = gateway

    def provider(self, request, timeout):
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc, request.get_method()), ("https", market_transport.SELLER, "POST"))
        self.assertEqual(request.get_header("Api-key"), "fixture-seller-credential")
        self.assertGreater(timeout, 0)
        body = json.loads(request.data)
        self.wire_calls.append((url.path, body))
        self.assertTrue(self.responses[url.path], f"Unexpected call: {url.path}")
        value = self.responses[url.path].popleft()
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        response = io.BytesIO(json.dumps(value).encode())
        response.status = 200
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        return response

    def proposal(self, arguments=ARGS, token=None):
        return self.call(token or self.token, replies.TOOL, arguments)

    def oauth_token(self, key, scope):
        client, redirects = next((client, redirects) for client, (redirects, name) in oauth_mcp.CIMD.items()
                                 if name == "ChatGPT")
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        resource = self.login.origin + "/mcp"
        common = {"client_id": client, "redirect_uri": redirects[0], "resource": resource}
        start = self.oauth_http("/oauth/authorize", query=dict(common, response_type="code", scope=scope,
            code_challenge=challenge, code_challenge_method="S256", state=secrets.token_urlsafe(24)))
        self.assertTrue(start["status"].startswith("200"))
        fields = {name: re.search(r'name="'+name+r'" value="([A-Za-z0-9_.-]+)"',start["body"]).group(1)
                  for name in ("csrf","ticket")}
        consent = self.oauth_http("/oauth/authorize", "POST", dict(fields,key=key), origin=self.login.origin,
                                  cookie=start["headers"]["Set-Cookie"].split(";",1)[0])
        self.assertTrue(consent["status"].startswith("303"))
        code = parse_qs(urlsplit(consent["headers"]["Location"]).query)["code"][0]
        token = self.oauth_http("/oauth/token", "POST", dict(common, grant_type="authorization_code",
                                                            code=code, code_verifier=verifier))
        self.assertTrue(token["status"].startswith("200"))
        result = json.loads(token["body"])
        self.assertEqual(set(result["scope"].split()),set(scope.split()))
        return result["access_token"]

    def approve(self, result, arguments=ARGS):
        inputs = {"body": dict(arguments, mark_review_as_processed=False)}
        form = {"action": "approve", "pid": result["proposal_id"], "csrf": self.csrf, "confirm": "yes",
                "fingerprint": market_actions.fingerprint(replies.OPERATION, inputs)}
        return self.request("/settings/catalog", "POST", form)

    def status(self, result):
        return self.call(self.token, "market_api_status", {"proposal_id": result["proposal_id"]})

    def fixture_reviews(self):
        return {"reviews": [{"id": REVIEW, "sku": SKU, "rating": 4, "text": "Тестовый отзыв",
                             "published_at": "2026-11-07T11:00:00Z", "status": "NEW"}],
                "has_next": False, "last_id": ""}

    def paths(self):
        return [path for path, _ in self.wire_calls]

    def test_find_review_show_draft_confirm_and_verify_exact_reply(self):
        raw_key = self.token
        read_token = self.oauth_token(raw_key, "mcp:read")
        self.token = self.oauth_token(raw_key, "mcp:read mcp:propose")
        list_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        readonly = json.loads(self.mcp(list_request, read_token)["body"])["result"]["tools"]
        self.assertNotIn(replies.TOOL,{item["name"] for item in readonly})
        self.assertEqual(self.proposal(token=read_token)["code"],-32602)
        listing = json.loads(self.mcp(list_request, self.token)["body"])
        write = next(item for item in listing["result"]["tools"] if item["name"] == replies.TOOL)
        self.assertFalse(write["annotations"]["readOnlyHint"])
        self.assertEqual(set(write["inputSchema"]["required"]), {"review_id", "text"})
        self.assertIn("mcp:propose", write["securitySchemes"][0]["scopes"])
        self.responses[REVIEWS].append(self.fixture_reviews())
        self.responses[replies.COMMENT_LIST].extend((EMPTY, EMPTY, {"comments": [comment()], "offset": 1}))
        self.responses[replies.COMMENT_CREATE].append({"comment_id": COMMENT})
        data = self.call(self.token, "ozon_reviews", dict(DATES, limit=1, include_seller_reply=True))
        review, = data["items"]
        self.assertEqual((review["id"], review["review_id"], review["sku"], review["sku_exact"]),
                         (REVIEW, REVIEW, SKU, str(SKU)))
        self.assertFalse(review["seller_reply_present"])

        draft = dict(review_id=review["review_id"], text=TEXT)
        self.assertEqual(self.paths(), [REVIEWS, replies.COMMENT_LIST])
        queued = self.proposal(draft)
        self.assertEqual(queued["status"], "pending")
        approval_url = self.login.origin+"/settings/catalog#proposal-"+queued["proposal_id"]
        self.assertEqual(queued["approval_url"],approval_url)
        self.assertNotIn(replies.COMMENT_CREATE, self.paths())
        self.assertTrue(self.request("/settings/catalog",cookie=False)["status"].startswith("401"))
        rendered = self.request("/settings/catalog")["body"]
        self.assertIn('id="proposal-'+queued["proposal_id"]+'"',rendered)
        self.assertIn(REVIEW, rendered)
        self.assertIn("Спасибо за отзыв!", rendered)
        self.assertTrue(self.approve(queued, draft)["status"].startswith("303"))

        result = self.status(queued)
        self.assertEqual((result["status"], result["comment_id"]), ("accepted", COMMENT))
        self.assertTrue(result["publication_confirmed"])
        self.assertTrue(result["outcome"]["comment_verified"])
        sent, = [body for path, body in self.wire_calls if path == replies.COMMENT_CREATE]
        self.assertEqual(sent, dict(draft, mark_review_as_processed=False))
        self.assertEqual(self.paths(), [REVIEWS, replies.COMMENT_LIST, replies.COMMENT_LIST,
                                        replies.COMMENT_CREATE, replies.COMMENT_LIST])
        self.assertEqual(self.proposal(draft)["proposal_id"], queued["proposal_id"])

    def test_existing_seller_comment_on_later_page_blocks_create(self):
        self.responses[replies.COMMENT_LIST].extend((
            {"comments": [comment("buyer-1", owner=False)], "offset": 1},
            {"comments": [comment(published=False)], "offset": 2},
        ))
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual((result["status"], result["outcome"]["reason"]), ("denied", "seller_reply_exists"))
        self.assertEqual(result["outcome"]["existing_comment_id"], COMMENT)
        self.assertEqual(self.paths(), [replies.COMMENT_LIST, replies.COMMENT_LIST])
        self.assertEqual([body["offset"] for _, body in self.wire_calls], [0, 1])

    def test_parallel_repeats_are_fenced_by_review_and_owner_approval(self):
        entered = threading.Event()
        release = threading.Event()

        def submit():
            entered.set()
            self.assertTrue(release.wait(6))
            return {"comment_id": COMMENT}

        self.responses[replies.COMMENT_LIST].extend((EMPTY, {"comments": [comment()], "offset": 1}))
        self.responses[replies.COMMENT_CREATE].append(submit)
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(self.proposal) for _ in range(2)]
            one, two = [future.result(timeout=10) for future in requests]
            self.assertEqual(one["proposal_id"], two["proposal_id"])
            first = pool.submit(self.approve, one)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.approve, two)
                _, other_token = self.key("catalog_operations")
                blocked = self.proposal(dict(ARGS, text="Другой текст"), other_token)
                self.assertEqual(blocked["status"], "action_unavailable")
            finally:
                release.set()
            self.assertTrue(first.result(timeout=10)["status"].startswith("303"))
            self.assertTrue(second.result(timeout=10)["status"].startswith("409"))
        self.assertEqual(self.paths().count(replies.COMMENT_CREATE), 1)
        self.assertTrue(self.status(one)["publication_confirmed"])

    def test_403_before_send_or_from_create_is_explicit(self):
        for stage in ("precheck", "create"):
            with self.subTest(stage=stage):
                self.wire_calls.clear()
                error = urllib.error.HTTPError("", 403, "Synthetic denial", {}, None)
                if stage == "precheck":
                    self.responses[replies.COMMENT_LIST].append(error)
                else:
                    self.responses[replies.COMMENT_LIST].append(EMPTY)
                    self.responses[replies.COMMENT_CREATE].append(error)
                queued = self.proposal()
                self.assertTrue(self.approve(queued)["status"].startswith("303"))
                result = self.status(queued)
                self.assertEqual((result["outcome"]["http_status"], result["outcome"]["stage"]), (403, stage))
                self.assertEqual(result["status"], "held" if stage == "precheck" else "denied")
                self.assertEqual(self.paths().count(replies.COMMENT_CREATE), int(stage == "create"))

    def test_unverifiable_comment_author_blocks_write(self):
        self.responses[replies.COMMENT_LIST].append({"comments": [{"id": "ambiguous-owner"}], "offset": 1})
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual((result["status"], result["outcome"]["reason"]), ("held", "comment_check_unavailable"))
        self.assertEqual(self.paths(), [replies.COMMENT_LIST])

    def test_create_timeout_reconciles_existing_comment_without_resubmitting(self):
        self.responses[replies.COMMENT_LIST].extend((EMPTY, {"comments": [comment()], "offset": 1}, EMPTY))
        self.responses[replies.COMMENT_CREATE].append(TimeoutError("synthetic timeout"))
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual((result["status"], result["comment_id"]), ("accepted", COMMENT))
        self.assertTrue(result["publication_confirmed"])
        self.assertEqual(self.paths().count(replies.COMMENT_CREATE), 1)

    def test_timeout_without_confirmation_blocks_replacement_text(self):
        self.responses[replies.COMMENT_LIST].extend((EMPTY, EMPTY))
        self.responses[replies.COMMENT_CREATE].append(TimeoutError("synthetic timeout"))
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual((result["status"], result["comment_id"]), ("unknown", None))
        self.assertFalse(result["publication_confirmed"])
        self.assertEqual(self.proposal()["proposal_id"], queued["proposal_id"])
        self.assertEqual(self.proposal(dict(ARGS, text="Другой текст"))["status"], "action_unavailable")
        self.assertEqual(self.paths().count(replies.COMMENT_CREATE), 1)

    def test_comment_id_is_retained_during_moderation(self):
        self.responses[replies.COMMENT_LIST].extend((EMPTY, {"comments": [comment(published=False)], "offset": 1}))
        self.responses[replies.COMMENT_CREATE].append({"comment_id": COMMENT})
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual(result["comment_id"], COMMENT)
        self.assertTrue(result["outcome"]["comment_verified"])
        self.assertFalse(result["publication_confirmed"])

    def test_different_listed_comment_does_not_confirm_create_id(self):
        self.responses[replies.COMMENT_LIST].extend((EMPTY, {"comments": [comment("other-comment")], "offset": 1}, EMPTY))
        self.responses[replies.COMMENT_CREATE].append({"comment_id": COMMENT})
        queued = self.proposal()
        self.assertTrue(self.approve(queued)["status"].startswith("303"))
        result = self.status(queued)
        self.assertEqual(result["comment_id"], COMMENT)
        self.assertFalse(result["outcome"]["comment_verified"])
        self.assertFalse(result["publication_confirmed"])
        self.assertEqual(self.proposal()["proposal_id"], queued["proposal_id"])

    def test_legacy_read_and_failed_optional_check_remain_usable(self):
        self.responses[REVIEWS].extend((self.fixture_reviews(), self.fixture_reviews()))
        self.responses[replies.COMMENT_LIST].append(TimeoutError("synthetic read timeout"))
        _, readonly = self.key("marketplaces")
        for verify in (False, True):
            args = dict(DATES, include_seller_reply=True) if verify else DATES
            result = self.call(readonly, "ozon_reviews", args)
            review, = result["items"]
            self.assertEqual((review["id"], review["sku"], review["rating"]), (REVIEW, SKU, 4))
            self.assertIsNone(review["seller_reply_present"])
            self.assertTrue(result["complete"])
        self.assertEqual(self.paths(), [REVIEWS, REVIEWS, replies.COMMENT_LIST])
        self.assertEqual(self.proposal(token=readonly)["code"], -32602)

    def test_generic_catalogue_cannot_change_status_or_bypass_fence(self):
        response = self.proposal()
        generic = {"operation_id": replies.OPERATION, "request_id": "test-review-generic-1",
                   "input": {"body": dict(ARGS, mark_review_as_processed=True)}}
        self.assertEqual(self.call(self.token, "market_api_propose", generic)["error"], "invalid_arguments")
        generic["input"]["body"] = dict(ARGS, text="Другой текст", mark_review_as_processed=False)
        self.assertEqual(self.call(self.token, "market_api_propose", generic)["status"], "action_unavailable")
        self.assertEqual(self.status(response)["status"], "pending")
        self.assertFalse(self.wire_calls)

    def test_previous_release_contract_cannot_approve_review_reply(self):
        queued = self.proposal()
        with patch.object(market_actions, "_contract_version", market_actions._version):
            self.assertTrue(self.approve(queued)["status"].startswith("409"))
        self.assertEqual(self.status(queued)["status"], "pending")
        self.assertFalse(self.wire_calls)
