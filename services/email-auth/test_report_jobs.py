import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import report_jobs


class ReportJobTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gate = str(Path(directory.name) / ".ozon-finance-gates")
        self.creds = {"wb": {"category_keys": {"wb_finance": {"api_token": "finance-one"}}}}
        self.args = {"article": "sample", "month": "2026-10"}
        self.started = []
        self.finish = threading.Event()
        self.addCleanup(self.finish.set)
        wait = patch.object(report_jobs, "WAIT_SECONDS", 0.02)
        wait.start()
        self.addCleanup(wait.stop)

    def dispatch(self, args, mappings):
        self.started.append(dict(args))
        self.finish.wait(5)
        return {"items": [{"sold_units": 2, "net_after_expenses": {"amount": "85"}}], "complete": True}

    def read(self, **kwargs):
        return report_jobs.run("unit_economics", kwargs or self.args, self.creds, self.gate, self.dispatch)

    def poll(self, **args):
        for _ in range(100):
            value = self.read(**args)
            if value.get("complete"):
                return value
            time.sleep(0.01)
        self.fail("Completed report was not visible")

    def test_exact_task_survives_a_second_caller_and_is_bound_to_finance_credentials(self):
        task = self.read()["task_id"]
        self.assertEqual(self.read(**self.args, task_id=task)["task_id"], task)
        other = {"wb": {"category_keys": {"wb_finance": {"api_token": "finance-two"}}}}
        with self.assertRaises(report_jobs.InvalidTask):
            report_jobs.run("unit_economics", dict(self.args, task_id=task), other, self.gate, self.dispatch)
        self.finish.set()
        self.assertEqual(self.poll(**self.args, task_id=task)["items"][0]["net_after_expenses"]["amount"], "85")
        self.assertTrue(self.read()["complete"])
        self.assertEqual(self.started, [self.args])

    def test_native_provider_continuation_survives_a_portal_pending_response(self):
        args = dict(self.args, task_id="provider-continuation")
        wrapper = self.read(**args)["task_id"]
        self.assertEqual(self.read(**dict(self.args, task_id=wrapper))["task_id"], wrapper)
        self.finish.set()
        self.assertTrue(self.poll(**dict(self.args, task_id=wrapper))["complete"])
        self.assertEqual(self.started, [args])

    def test_mapping_change_invalidates_pending_and_completed_financial_queries(self):
        path = Path(self.gate).parent / "supply-articles.json"
        path.write_text('{"sku":1}', encoding="utf8")
        task = self.read()["task_id"]
        self.finish.set()
        self.poll(**dict(self.args, task_id=task))
        path.write_text('{"sku":2}', encoding="utf8")
        with self.assertRaises(report_jobs.InvalidTask):
            self.read(**dict(self.args, task_id=task))
        self.assertTrue(self.poll(**self.args)["complete"])
        self.assertEqual(len(self.started), 2)

    def test_admission_does_not_dispatch_a_third_provider_child(self):
        first = self.read()
        second = self.read(article="other")
        third = self.read(article="third")
        fourth = self.read(article="fourth")
        fifth = self.read(article="fifth")
        self.assertIn(third["reason"], ("waiting_worker","report_in_progress"))
        self.assertIn(fourth["reason"], ("waiting_worker","report_in_progress"))
        self.assertEqual(fifth["reason"], "local_limiter")
        self.assertIsNone(third["http_status"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.started), 2)
        self.finish.set()
        self.poll(**self.args)
        self.poll(article="other")
        self.poll(article="third")
        self.poll(article="fourth")

    def test_provider_wait_is_replayed_without_another_outbound_request(self):
        import urllib.error
        calls = []
        def limited(args, mappings):
            calls.append(args)
            raise urllib.error.HTTPError("", 429, "limited", {"Retry-After": "120"}, None)
        args = {"article": "sample"}
        with patch.object(report_jobs, "WAIT_SECONDS", 2):
            first = report_jobs.run("ozon_wrong_volume", args, self.creds, self.gate, limited)
            self.assertEqual(first["http_status"], 429)
            second = report_jobs.run("ozon_wrong_volume", args, self.creds, self.gate, limited)
        self.assertEqual(second["status"], "rate_limited")
        self.assertIn(second["retry_after_seconds"], (119, 120))
        self.assertEqual(second["previous_http_status"], 429)
        self.assertIsNone(second["http_status"])
        self.assertEqual(second["reason"], "local_limiter")
        self.assertEqual(len(calls), 1)

    def test_native_and_public_receipts_share_provider_deadline_and_resume_id(self):
        calls = []
        now = time.time()
        def provider(args, mappings):
            calls.append(dict(args))
            if len(calls) > 1:
                return {"status": "ready", "complete": True, "items": []}
            return {"status": "pending", "task_id": "native-id", "retry_after_seconds": 30,
                    "source_issues": [{"http_status": 429, "retry_after_seconds": 32}]}
        with patch.object(report_jobs, "WAIT_SECONDS", 2), patch.object(report_jobs, "MAX_BACKGROUND_SECONDS", 20), patch("time.time", return_value=now) as clock:
            def read(**extra):
                return report_jobs.run("wb_funnel_history", dict(self.args, **extra), self.creds, self.gate, provider)
            receipt = read()["task_id"]
            clock.return_value = now + 10
            for given in (receipt, "native-id", None):
                page = read(**({"task_id": given} if given else {}))
                self.assertEqual(page["task_id"], receipt)
                self.assertEqual(page["retry_after_seconds"], 22)
                self.assertEqual(page["source_issues"][0]["retry_after_seconds"], 22)
                self.assertIsNone(page["source_issues"][0]["http_status"])
            self.assertEqual(len(calls), 1)
            clock.return_value = now + 33
            self.assertTrue(read(task_id=receipt)["complete"])
            self.assertEqual(calls, [self.args, dict(self.args, task_id="native-id")])

    def test_pending_report_continues_without_polling_and_keeps_provider_receipt(self):
        calls = []
        completed = threading.Event()
        def dispatch(args, mappings):
            calls.append(dict(args))
            if len(calls) == 1:
                return {"status": "pending", "task_id": "native-id", "retry_after_seconds": 0}
            completed.set()
            return {"status": "ready", "complete": True, "items": []}
        with patch.object(report_jobs, "WAIT_SECONDS", 0.5), patch.object(report_jobs, "MIN_RETRY_SECONDS", 0.02):
            initial = report_jobs.run("wb_funnel_history", self.args, self.creds, self.gate, dispatch)
            self.assertEqual(initial["reason"], "retry_scheduled")
            self.assertTrue(initial["background_retry"])
            self.assertTrue(completed.wait(3), "report did not continue without polling")
            for _ in range(100):
                result = report_jobs.run("wb_funnel_history", dict(self.args, task_id=initial["task_id"]), self.creds, self.gate, dispatch)
                if result.get("complete"): break
                time.sleep(0.01)
            self.assertTrue(result["complete"])
        self.assertEqual(calls, [self.args, dict(self.args, task_id="native-id")])

    def test_waiting_job_starts_without_another_client_poll(self):
        third_started = threading.Event()
        def dispatch(args, mappings):
            self.started.append(dict(args))
            if args['article'] == 'third':
                third_started.set()
            else:
                self.finish.wait(5)
            return {'status':'ready','complete':True,'items':[]}
        def read(article):
            return report_jobs.run('unit_economics', {'article':article}, self.creds, self.gate, dispatch)
        read('first'); read('second')
        queued = read('third')
        self.assertEqual(queued['reason'], 'waiting_worker')
        self.assertFalse(third_started.is_set())
        self.finish.set()
        self.assertTrue(third_started.wait(3), 'queued report did not start without polling')
        for _ in range(100):
            result = read('third')
            if result.get('complete'): break
            time.sleep(0.01)
        self.assertTrue(result['complete'])

    def test_long_provider_cooldown_is_never_shortened_or_held_past_drain(self):
        calls = []
        def dispatch(args, mappings):
            calls.append(args)
            return {"status": "pending", "task_id": "native-id", "source_issues": [
                {"status": "rate_limited", "retry_after_seconds": 3600, "http_status": 429}]}
        with patch.object(report_jobs, "WAIT_SECONDS", 1):
            result = report_jobs.run("wb_funnel_history", self.args, self.creds, self.gate, dispatch)
        self.assertEqual(result["reason"], "retry_later")
        self.assertFalse(result["background_retry"])
        self.assertEqual(result["retry_after_seconds"], 3600)
        self.assertEqual(len(calls), 1)

    def test_source_transport_failure_is_not_retried_blindly(self):
        calls = []
        def dispatch(args, mappings):
            calls.append(args)
            return {"status": "pending", "source_issues": [{"status": "source_unavailable"}]}
        with patch.object(report_jobs, "WAIT_SECONDS", 1):
            result = report_jobs.run("wb_funnel_history", self.args, self.creds, self.gate, dispatch)
        self.assertEqual(result["reason"], "retry_later")
        self.assertFalse(result["background_retry"])
        self.assertEqual(len(calls), 1)

    def test_cached_receipt_does_not_claim_a_worker_after_a_crash(self):
        import expansion_gateway
        from market_transport import Wire, _id
        name = 'wb_funnel_history'
        wire = Wire('mcp-report', self.creds, self.gate, time.monotonic())
        mappings = expansion_gateway._seller_mappings(self.gate)
        scope = _id([wire.scope, 'mcp-public-report-v2', name, self.args, mappings])
        task = report_jobs.PREFIX + _id([scope, None])[:32]
        key = _id([scope, 'result', task])
        wire.store(key, json.dumps({'saved_at':time.time(), 'value':{
            'status':'pending', 'task_id':task, 'background_retry':True,
            'reason':'retry_scheduled', 'retry_after_seconds':30}}).encode(), 60)
        result = report_jobs.run(name, self.args, self.creds, self.gate, self.dispatch)
        self.assertFalse(result['background_retry'])
        self.assertEqual(result['reason'], 'retry_later')
        self.assertEqual(self.started, [])

    def test_reports_in_same_lock_bucket_keep_distinct_receipts(self):
        original = report_jobs._id
        def short_collision(value):
            digest = original(value)
            return 'abc' + digest[3:] if len(value) == 3 and value[1] == 'result' else digest
        with patch.object(report_jobs, '_id', short_collision):
            first = self.read()
            second = self.read(article='other')
            self.assertNotEqual(first['task_id'], second['task_id'])
            self.assertEqual(len(self.started), 1)
            self.finish.set()
            self.poll(**self.args)
            self.poll(article='other')

    def test_cached_accounting_preserves_nested_provider_deadline_and_public_row(self):
        issue = {"status": "rate_limited", "http_status": 429, "retry_after_seconds": 3600}
        value = {"complete": False, "sources": {"ozon": {"finance": issue, "items": [dict(issue)]}}}
        self.assertEqual(report_jobs._ttl("unit_economics", self.args, value), 3600)
        with patch("time.time", return_value=130):
            replay = report_jobs._replay(json.dumps({"saved_at": 100, "value": value}))
        finance = replay["sources"]["ozon"]["finance"]
        self.assertEqual((finance["http_status"], finance["retry_after_seconds"]), (None, 3570))
        self.assertEqual(replay["sources"]["ozon"]["items"], [issue])

    def test_worker_uses_mapping_snapshot_even_if_configuration_is_replaced_twice(self):
        import expansion_gateway
        mapping = Path(self.gate).parent / "supply-articles.json"
        old = {"sample": {"ozon_offer_id": "from-A", "wb_vendor_code": "wb-A"}}
        new = {"sample": {"ozon_offer_id": "from-B", "wb_vendor_code": "wb-B"}}
        mapping.write_text(json.dumps(old))
        reached = threading.Event()
        changes = []
        def dispatch(args, snapshot):
            self.finish.wait(2)
            token = expansion_gateway.request_seller_mappings.set(snapshot)
            try:
                changes.append(expansion_gateway._mapping(args, self.gate)["ozon_offer_id"])
            finally:
                expansion_gateway.request_seller_mappings.reset(token)
            mapping.write_text(json.dumps(old))
            reached.set()
            return {"complete": True, "items": []}
        status = []
        receipt = report_jobs.run("unit_economics", self.args, self.creds, self.gate, dispatch, status.append)
        mapping.write_text(json.dumps(new))
        self.finish.set()
        self.assertTrue(reached.wait(2))
        self.poll(**dict(self.args, task_id=receipt["task_id"]))
        self.assertEqual(changes, ["from-A"])
        self.assertEqual(status, ["pending", "ok"])

    def test_busy_directory_and_slow_launch_return_actionable_receipts(self):
        import finance_gate
        with patch.object(finance_gate, "_root", side_effect=finance_gate.GateError("busy")):
            value = self.read()
        self.assertIsNone(value["http_status"])
        self.assertTrue(value["task_id"].startswith(report_jobs.PREFIX))
        before = time.monotonic()
        self.assertEqual(self.read()["task_id"], value["task_id"])
        self.assertLess(time.monotonic() - before, 1.5)
        self.finish.set()
        self.poll(**self.args)


if __name__ == "__main__":
    unittest.main()
