import copy
import base64
import json
import unittest
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import expansion_gateway
import finance_gate
import market_archive
import market_composites
import market_cooldown
import market_health
import market_history
import market_specs
import market_transport
import ozon_details
import ozon_expansion
import supply_history
import supply_plan
import wb_expansion
import wb_reports


class ResumeWire:
    deadline = float("inf")

    def __init__(self):
        self.records = {}

    @contextmanager
    def progress(self, name, args):
        key = (name, json.dumps(args, sort_keys=True))
        yield copy.deepcopy(self.records.get(key)), lambda value, ttl: self.records.update({key: copy.deepcopy(value)})


class Recovery(unittest.TestCase):
    def test_split_orders_are_detected_by_order_number_without_a_seller_override(self):
        state = {"locations":{}, "orders":{}, "issues":set(), "transit":{}}
        def posting(i):
            return {"warehouse_id":8, "warehouse_name":"N", "created_at":"2026-09-09T12:00:00Z",
                    "quantity":1, "posting_number":f"P{i}", "order_number":"bulk" if i<13 else f"normal-{i}",
                    "order_id":i+100, "sku":42, "status":"delivered"}
        rows = [posting(i) for i in range(15)]
        supply_plan._scan(state,"orders",{"items":rows+[rows[0]]},"ozon","2026-09-01","2026-09-30")
        loc = state["locations"]["8"]
        loc["wholesale_daily"] = {}
        seen = supply_plan._wholesale(state, {"bulk_order_min_units":10})
        self.assertEqual([(g["basis"],g["order_number"],g["postings"],g["units"]) for g in seen],
                         [("order_number","bulk",13,13)])
        self.assertEqual(loc["daily"]["2026-09-09"]-loc["wholesale_daily"]["2026-09-09"],2)

    def test_exhausted_sellable_inventory_replaces_one_snapshot_and_preserves_unknown_inbound(self):
        wire = ResumeWire()
        wire.creds, wire.gate_dir = {}, "/fixture"
        def pos(name, n):
            return {"sku":21,"item_code":"A","warehouse_name":name,"free_to_sell_amount":n}
        def data(rows):
            return {"http_status":200,"fetched_at":"2026-11-19T10:00:00Z","data":{"result":{"rows":rows}}}
        with patch("market_catalog.transfer",return_value=data([pos("One",12),pos("ONE",1)])):
            self.assertIsNone(supply_plan._warehouse_inventory("A",[21],wire))
        with patch("market_catalog.transfer",return_value=data([pos("One",12)])):
            snapshot = supply_plan._warehouse_inventory("A",[21],wire)
        state = {"locations":{"1":{"warehouse":"ONE","stock":{"21":{"available":14,"inbound":2}}},
                              "2":{"warehouse":"Two","stock":{}}}}
        self.assertTrue(supply_plan._apply_warehouse_inventory(state,snapshot))
        one, two = state["locations"].values()
        self.assertEqual((one["stock"]["21"]["available"],two["stock"]["21"]["available"]),(12,0))
        self.assertEqual((two["sellable_evidence"],two["stock"]["21"]["inbound"]),("absent_from_exhausted_inventory",None))
        previous = copy.deepcopy(state)
        self.assertFalse(supply_plan._apply_warehouse_inventory(state,dict(snapshot,positions={"unmapped":{"21":4}})))
        self.assertEqual(previous,state)

    def test_wb_net_accounts_once_for_commission_advertising_and_loyalty(self):
        raw = {v:0 for v in wb_expansion.FIN_FIELDS.values()}
        raw.update(vendorCode="A", nmId=7, currency="RUB", docTypeName="Продажа", sellerOperName="Продажа",
                   quantity=1, forPay=100, vw=7, vwNds=1, cashbackAmount=8, cashbackCommissionChange=1, cashbackDiscount=3)
        service = dict(raw,docTypeName="",sellerOperName="Возмещение издержек по перемещению и операционной обработке товара",
                       forPay=0,vw=-2,vwNds=-1,rebillLogisticCost=3,cashbackAmount=0,cashbackCommissionChange=0,
                       cashbackDiscount=0,deduction=10,bonusTypeName="Оплата WB Продвижение")
        def calc(reason=None):
            if reason is not None:
                service["bonusTypeName"]=reason
            converted,_=wb_expansion.finance_rows([raw,service],{"article":"A","nm_id":7})
            grouped=wb_expansion.finance_summary(converted)
            w=ResumeWire()
            w.creds,w.gate_dir={}, "/fixture"
            with patch.object(market_history,"History",return_value=SimpleNamespace(verified_days=lambda *_:{})), \
                    patch.object(market_composites,"advertising",return_value={"complete":True,"spend_by_currency":{"RUB":"10"}}), \
                    patch.object(wb_reports,"article_storage_period",return_value={"complete":True,"amount":"0","currency":"RUB"}):
                data=market_composites.accounting({"month":"2026-10","cost_per_unit":"5","currency":"RUB"},
                     {"wb":w},{"wb_vendor_code":"A","wb_nm_id":7},
                     lambda *_:{"items":grouped,"complete":True,"unallocated_rows":0},None)
            return data["items"][0]
        row=calc()
        self.assertEqual(row["amounts"]["commission"],"8")
        self.assertEqual(row["net_after_expenses"]["amount"],"84")
        self.assertIn("advertising_payment_document_unreconciled",row["net_after_expenses"]["unreconciled"])
        self.assertIsNone(row["margin_per_unit"])
        uncertain=calc("Неопознанное удержание")
        self.assertFalse(uncertain["net_after_expenses"]["complete"])
        self.assertIsNone(uncertain["margin_per_unit"])
        service["deduction"]=0
        documented=calc()
        self.assertTrue(documented["net_after_expenses"]["complete"])
        self.assertEqual(documented["margin_per_unit"],"79")

    def test_storage_month_resumes_disjoint_spans_and_undated_rows_are_incomplete(self):
        from test_wb_expansion_contracts import report_download, PERIOD
        wire = report_download("paid_storage",[{"nmId":7,"vendorCode":"A","warehousePrice":"7"}])
        data=wb_expansion._report("wb_paid_storage",dict(PERIOD,article="A",nm_id=7,mode="summary"),wire)
        self.assertFalse(data["complete"])
        self.assertIn("storage_charge_date_unconfirmed",data["issues"])
        wire=ResumeWire()
        query={"date_from":"2026-10-01","date_to":"2026-10-31"}
        selector={"article":"A","nm_id":7}
        calls=[]
        def delivered(_,args,_wire):
            calls.append((args["date_from"],args["date_to"],args.get("task_id")))
            if len(calls)==2:
                return {"complete":False,"status":"pending","task_id":"T"}
            return {"complete":True,"status":"ready","items":[{"currency":"RUB","amount":"1.01","source_rows":1}]}
        with patch.object(wb_expansion,"_report",side_effect=delivered):
            first=wb_reports.article_storage_period(query,selector,wire)
            self.assertIsNone(first["amount"])
            final=wb_reports.article_storage_period(query,selector,wire)
            self.assertEqual((final["amount"],final["source_rows"]),("4.04",4))
            self.assertEqual(calls[1:3],[("2026-10-09","2026-10-16",None),("2026-10-09","2026-10-16","T")])
            self.assertEqual(wb_reports.article_storage_period(query,selector,wire),final)

    def test_wb_loyalty_service_reversal_keeps_its_own_sign(self):
        raw = dict(vendorCode="A", nmId=7, currency="RUB", sellerOperName="Услуга",
                   bonusTypeName="Сумма баллов, удержанных для списания ВауВБ")
        lines=[dict(raw,docTypeName="Продажа",cashbackAmount=120,cashbackCommissionChange=12),
               dict(raw,docTypeName="Возврат",cashbackAmount=-120,cashbackCommissionChange=-12)]
        selected,_=wb_expansion.finance_rows(lines,{"article":"A","nm_id":7})
        by_type=wb_expansion.finance_summary(selected)[0]["amounts"]
        self.assertEqual((by_type["loyalty_points"],by_type["loyalty_fee"]),("0","0"))

    def test_closed_wb_orders_use_only_verified_complete_days_and_preserve_live_continuation(self):
        import time
        creds = {"api_token":"test-account"}
        end = (datetime.now(market_specs.MOSCOW).date() - timedelta(days=3))
        first = end - timedelta(days=1)
        args = {"article":"A","date_from":first.isoformat(),"date_to":end.isoformat(),"group_by":"day"}
        with TemporaryDirectory() as temp:
            directory=str(Path(temp,"gates"))
            history=market_history.History(directory)
            owner=market_history.scope("wb",creds)
            history.confirm(creds,owner)
            row = {"article":"A","date":first.isoformat()+"T13:00:00+03:00","quantity":1,
                   "cancelled":False,"srid":"order","region":"R","warehouse":"W"}
            history.save_day(owner,"wb_orders",first.isoformat(),[row],market_history.utc())
            def read(query):
                return expansion_gateway.execute("wb_orders",query,creds,directory,time.monotonic()+10)
            with patch.object(expansion_gateway,"_wb_daily_totals",side_effect=AssertionError("requires live summary")):
                with self.assertRaisesRegex(AssertionError,"requires live"):
                    read(args)
                history.save_day(owner,"wb_orders",end.isoformat(),[],market_history.utc())
                data=read(args)
                self.assertEqual((data["source"],data["status"],data["archived_days"]),("daily_archive","ready",2))
                self.assertEqual([i["ordered_units"] for i in data["items"]],[1,0])
                with self.assertRaisesRegex(AssertionError,"requires live"):
                    read(dict(args,archive=False))
            with patch.object(wb_expansion,"read",side_effect=AssertionError("live cursor")):
                raw=dict(args);raw.pop("group_by")
                self.assertEqual(read(raw)["items"],[row])
                with self.assertRaisesRegex(AssertionError,"live cursor"):
                    read(dict(raw,cursor=first.isoformat()+"|100"))

    def test_health_probe_distinguishes_local_hold_from_report_and_http_response(self):
        import portal
        from unittest.mock import Mock
        app = object.__new__(portal.Portal)
        app.vault,app.gateway,app.finance_gate_dir = Mock(),object(),"/fixture"
        app.vault.read.return_value = None
        app._observe = Mock()
        args={"tool_name":"wb_orders","probe_arguments":{"date_from":"2026-10-01","date_to":"2026-10-02"}}
        with patch.object(expansion_gateway,"credentials",return_value={"api_token":"fixture"}), \
                patch.object(market_health,"read",side_effect=lambda *a:{}):
            app._dispatch_read = Mock(side_effect=finance_gate.GateError("cooldown",30))
            probe=app.read_gateway("mcp_health",args,{})["probe"]
            self.assertEqual((probe["probe_origin"],probe["http_status"],probe["retry_after_seconds"]),("local_gate",None,30))
            app._dispatch_read = Mock(side_effect=urllib.error.HTTPError("",429,"",{},None))
            self.assertEqual(app.read_gateway("mcp_health",args,{})["probe"]["http_status"],429)
            app._dispatch_read = Mock(return_value={"complete":True,"source":"daily_archive"})
            probe=app.read_gateway("mcp_health",args,{})["probe"]
            self.assertEqual((probe["status"],probe["source"]),("ready","daily_archive"))

    def test_portal_can_serve_completed_supplies_when_refresh_worker_expires(self):
        import portal
        from unittest.mock import Mock
        args = {"article":"A","date_from":"2026-09-01","date_to":"2026-09-30","limit":1,"offset":1}
        stamp = 1790000000.0
        state = {"supplies": [], "last_ready": {"at": stamp, "scanned": 7,
                 "items": [{"supply_id": 10}, {"supply_id": 11}]}}
        wire = Mock()
        wire.progress_snapshot.return_value = state
        with patch.object(portal, "provider_read", side_effect=TimeoutError()) as reader, \
                patch.object(expansion_gateway, "make_wire", return_value=wire):
            app = SimpleNamespace(gateway=reader, finance_gate_dir="/fixture")
            page = portal.Portal._dispatch_read(app, "wb_supplies", args, {"api_token":"fixture"})
            self.assertEqual((page["status"],page["refresh_status"],page["items"]),("ready","pending",[{"supply_id":11}]))
            self.assertEqual(page["refresh_reason"],"provider_timeout")
            wire.progress_snapshot.assert_called_once_with("article-supplies-v2",["wb_supplies","A",
                {"date_from":"2026-09-01","date_to":"2026-09-30"}])
            wire.progress_snapshot.return_value = {}
            with self.assertRaises(TimeoutError):
                portal.Portal._dispatch_read(app,"wb_supplies",args,{"api_token":"different"})
            reader.side_effect = finance_gate.GateError("storage_unavailable")
            with self.assertRaises(finance_gate.GateError):
                portal.Portal._dispatch_read(app,"wb_supplies",args,{"api_token":"fixture"})

    def test_actual_last_mile_type_names_are_separate_from_main_logistics(self):
        dictionary = {"types":[
            {"id":1, "name":"LastMileCourier", "description":"Доставка до места выдачи", "category":"other"},
            {"id":2, "name":"DeliveryToHandoverPlaceByOzon", "description":"Доставка до места выдачи силами Ozon", "category":"other"},
            {"id":3, "name":"Logistic", "description":"Логистика", "category":"logistics"}]}
        breakdown = [dict(sku="20",type_id=i,currency="RUB",observed_row_sum=value)
                     for i,value in ((1,"-4"),(2,"-3"),(3,"-12"))]
        cost = ozon_details.label_posting_accruals({"breakdown":breakdown,"returned_posting_numbers":["P"]},dictionary)
        report = {"currency":"RUB","observed_delivery_units":2,"observed_return_rows":0,
                  "selected_rows":[{"sku":"20","posting_number":"P","delivery_commission":{"total":"90","amount":"100","standard_fee":"10"}}]}
        def finance(name, args, wire):
            return report if name=="ozon_realization_article_month" else cost
        result = market_composites.accounting({"month":"2026-10"},{"ozon":object()},
                                              {"ozon_offer_id":"A"},None,finance)
        row, = result["items"]
        self.assertEqual((row["amounts"]["logistics"],row["amounts"]["last_mile"]),("12","7"))
        self.assertEqual((row["per_sold_unit"]["last_mile"],row["reported_return_rows"]),( "3.5",0))
        self.assertEqual(result["sources"]["ozon"]["accrual_postings_returned"],1)

    def test_dictionary_retry_uses_the_previously_saved_postings(self):
        wire = ResumeWire()
        wire.creds,wire.gate_dir={"client_id":"17","api_key":"fixture"}, "/fixture"
        args=dict(posting_numbers=["P"],view="summary",include_types=True)
        def gate(_dir,_creds,read,_end,*,allow_retry):
            self.assertTrue(allow_retry)
            return read()
        with patch("portal._finance_read_with_retry",side_effect=gate), patch("portal._provider_read",
                side_effect=[{"breakdown":[]},finance_gate.GateError("cooldown",1),{"types":[]}]) as read:
            with self.assertRaises(finance_gate.GateError):
                expansion_gateway.regulated_ozon("ozon_posting_accruals",args,wire)
            final=expansion_gateway.regulated_ozon("ozon_posting_accruals",args,wire)
        self.assertEqual([a.args[0] for a in read.call_args_list],["ozon_posting_accruals","ozon_accrual_types","ozon_accrual_types"])
        self.assertFalse(read.call_args_list[0].args[1]["include_types"])
        self.assertEqual(final["observed_category_breakdown"],[])

    def test_received_malformed_cash_statement_releases_account_but_lost_headers_do_not(self):
        args = {"date_from":"2026-10-01","date_to":"2026-10-31"}
        with TemporaryDirectory() as tmp, patch.object(finance_gate,"_boot_identity",return_value="a"*64):
            gate = str(Path(tmp,"finance"))
            with self.assertRaises(finance_gate.ProviderResponseInvalid):
                finance_gate.run(gate,"17",lambda:ozon_expansion._cash(args,lambda *a,**k:{"result":[]}))
            def lost_headers(*args,**kwargs):
                raise TimeoutError()
            with self.assertRaises(TimeoutError):
                finance_gate.run(gate,"17",lambda:ozon_expansion._cash(args,lost_headers))
            with self.assertRaises(finance_gate.GateError) as blocked:
                finance_gate.run(gate,"17",lambda:self.fail("unknown upstream response"))
            self.assertEqual(blocked.exception.reason,"retry_time_unknown")

    def test_lost_body_after_successful_headers_keeps_finance_account_available(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as tmp, patch.object(finance_gate,"_boot_identity",return_value="a"*64):
            gate = str(Path(tmp,"finance"))
            wire = market_transport.Wire("ozon",{"client_id":"17","api_key":"fixture"},gate,float("inf"))
            reply = MagicMock()
            reply.__enter__.return_value=reply
            reply.status=200
            reply.read.side_effect=TimeoutError()
            wire.opener = SimpleNamespace(open=lambda *a,**kw:reply)
            with self.assertRaises(finance_gate.ProviderBodyTimeout):
                finance_gate.run(gate,"17",lambda:wire("POST",ozon_expansion.BASE+"/v1/finance/cash-flow-statement/list",body={}))
            self.assertEqual(finance_gate.run(gate,"17",lambda:"ready"),"ready")

    def test_recurring_display_rate_keeps_the_exact_source_total(self):
        record=market_composites.unit_row("ozon","A","RUB",3,None,{"logistics":"10","last_mile":"5.01"})
        self.assertEqual(record["per_sold_unit"]["logistics"],"3.3333")
        self.assertEqual((record["amounts"]["logistics"],record["per_sold_unit"]["last_mile"]),("10","1.67"))

    def test_general_token_only_marks_claimed_categories(self):
        token = base64.urlsafe_b64encode(json.dumps({"s": 1 << 2}).encode()).decode().rstrip("=")
        vault = SimpleNamespace(read=lambda name: {"api_token": "x." + token + ".x"} if name == "wb" else None)
        with patch.object(market_health.market_history, "History"):
            data = market_health.read(vault, "/fixture", {}, [], lambda *args: None)
        categories = {r["category"]: r for r in data["tokens"]}
        self.assertTrue(categories["Аналитика"]["configured"])
        self.assertEqual(categories["Аналитика"]["credential_source"], "wb")
        self.assertFalse(categories["Финансы"]["configured"])

    def test_storage_summary_includes_every_row_of_the_completed_report(self):
        from test_wb_expansion_contracts import report_download, PERIOD
        wire = report_download("paid_storage", [{"warehousePrice": n, "date": PERIOD["date_from"]} for n in (2.5, -0.25)])
        data = wb_expansion.read("wb_paid_storage", dict(PERIOD, mode="summary", limit=1), wire)
        self.assertEqual(data["items"], [{"currency":"RUB", "amount":"2.25", "source_rows":2}])
        self.assertTrue(data["complete"])

    def test_two_ozon_catalogs_confirm_all_skus(self):
        def wire(method, url, **kwargs):
            if url.endswith("/info/list"):
                raise market_cooldown.MethodLimited("retry_time_unknown")
            if url.endswith("/attributes"):
                return {"result": [{"id": 81, "offer_id": "001", "name": "Товар", "sku": 17}]}
            return {"items": [{"product_id": 81, "offer_id": "001", "stocks": [
                {"sku": 17, "type": "fbo"}, {"sku": 22, "type": "fbs"}]}]}
        self.assertEqual(ozon_expansion._article_card("001", wire)["sku_candidates"], ["17", "22"])
        self.assertIsNone(ozon_expansion._article_card("1", wire))

    def test_single_order_schema_preserves_nested_bundle_and_timeslot(self):
        class Details(ResumeWire):
            def __call__(self, method, url, **kwargs):
                if url.endswith("/supply-order/get"):
                    raise market_cooldown.MethodLimited("retry_time_unknown")
                assert url.endswith("/v1/supply-order/details") and kwargs["body"] == {"order_id":91}
                return {"order_id":91,"order_number":"S-91","timeslot":{"value":{"timeslot":{"from":"2026-10-02T08:00:00Z"}}},
                        "supplies":[{"supply_id":71,"content":{"bundle_id":"bundle-1"},"supply_state":"COMPLETED"}]}
        data = ozon_expansion._order_details({"order_ids":["91"]},Details())
        row, = data["orders"]
        self.assertEqual((row["supplies"][0]["bundle_id"],row["timeslot_from"]),("bundle-1","2026-10-02T08:00:00Z"))
        self.assertFalse(data["pending"])

    def test_delivered_postings_account_for_amounts_only_in_their_reported_currency(self):
        class Postings(ResumeWire):
            def __call__(self, method, url, **kwargs):
                assert kwargs["body"]["filter"]["statuses"] == ["delivered"]
                return {"has_next":False,"postings":[{"posting_number":"X-7","status":"delivered","products":[
                    {"offer_id":"A","quantity":1,"sku":71,"price":{"amount":"180","currency":"RUB"}},
                    {"offer_id":"A","quantity":2,"sku":72,"price":{"amount":"170","currency":"RUB"}}],
                    "financial_data":{"products":[{"product_id":71,"commission":{"amount":20,"currency":"RUB"},"payout":160},
                                                 {"product_id":72,"commission":{"amount":30,"currency":"RUB"},"payout":310}]}}]}
        data = ozon_expansion.delivered_economics(dict(date_from="2026-10-01",date_to="2026-10-31"),"A",Postings())
        self.assertTrue(data["complete"])
        self.assertEqual(data["observed_delivered_units"], 3)
        self.assertEqual(data["item_prices"]["RUB"]["amount"], "520")
        self.assertEqual(data["commissions"]["RUB"], {"amount":"20","covered_units":1,"per_covered_unit":"20.0000"})
        self.assertFalse(data["included_in_realization"])

    def test_wholesale_across_postings_requires_shared_order_identity(self):
        state = {"locations": {"W": {"wholesale_daily": {}}}, "orders": {}, "order_groups": {}}
        for i in range(20):
            key = str(i)
            state["orders"][key] = ["W", "2026-09-09", 1, False]
            state["order_groups"][key] = "bulk" if i < 13 else key
        supply_plan._wholesale(state, {"bulk_order_min_units": 10})
        self.assertEqual(state["locations"]["W"]["wholesale_daily"], {"2026-09-09": 13})
        state["locations"]["W"]["wholesale_daily"].clear()
        state["order_groups"].clear()
        supply_plan._wholesale(state, {"bulk_order_min_units": 10})
        self.assertFalse(state["locations"]["W"]["wholesale_daily"])

    def test_verified_seller_adjustment_preserves_other_orders_and_overlapping_group(self):
        with TemporaryDirectory() as folder:
            day = "2026-09-09"
            plan = {"done": {"orders": True}, "locations": {"W": {"daily": {day: 15}, "wholesale_daily": {day: 5}}}}
            entry = {"marketplace":"ozon","article":"01","warehouse":"W","date":day,"units":13,"source":"owner confirmation"}
            Path(folder,"supply-order-adjustments.json").write_text(json.dumps({"version":1,"entries":[entry]}), encoding="utf8")
            wire = SimpleNamespace(gate_dir=str(Path(folder, "gate")))
            check = lambda: supply_plan._seller_wholesale(wire,plan,"ozon","01",day,day)
            result = check()
            self.assertTrue(result[0]["applied"])
            self.assertEqual(result[0]["already_classified"], 5)
            self.assertEqual(plan["locations"]["W"]["wholesale_daily"][day], 13)
            self.assertEqual(plan["locations"]["W"]["daily"][day], 15)
            self.assertEqual(check()[0]["already_classified"], 13)
            plan["locations"]["W"].update(daily={day:8}, wholesale_daily={})
            self.assertFalse(check()[0]["applied"])
            self.assertEqual(plan["locations"]["W"]["wholesale_daily"], {})

    def test_exact_directory_join_and_bounded_catalog(self):
        warehouses = [{"warehouse_id": i, "name": str(i)} for i in range(200)]
        wire = lambda *a, **k: {"clusters": [{"id": 8, "name": "Центр", "logistic_clusters": [{"warehouses": warehouses}]}]}
        state = {"locations": {"111": {"cluster_id": None}}, "issues": set()}
        supply_plan._assign_clusters(state, wire)
        self.assertEqual(state["locations"]["111"], {"cluster_id": 8, "cluster": "Центр"})
        group, = ozon_expansion.read("ozon_clusters", {}, wire)["items"]
        self.assertEqual((group["warehouse_count"], len(group["warehouses"])), (200, 3))
        page = ozon_expansion.read("ozon_clusters", {"mode": "details", "offset": 100, "limit": 10}, wire)
        self.assertEqual((page["items"][0]["warehouse_id"], page["next_cursor"]), (100, 110))

    def test_order_feed_covers_creation_cohort_and_marks_older_days_unknown(self):
        today = datetime.now(market_specs.MOSCOW).date()
        yesterday, earlier = today - timedelta(days=1), today - timedelta(days=33)
        def wire(method, url, **kwargs):
            self.assertEqual(kwargs["body"]["selectedPeriod"]["end"][:10], today.isoformat())
            def order(i, created):
                return dict(srid=str(i), nmId=12, destinationDistrict="Центр", status="buyout", isMp=False,
                            warehouseName="Склад WB", createdAt=str(created)+"T08:00:00+03:00", updatedAt=str(today)+"T09:00:00+03:00")
            return {"data": {"orders": [order(1, yesterday), order(2, earlier)], "currency": "RUB", "snapshotTime": str(today)+"T09:01:00+03:00"}}
        args = dict(article="ABC", nm_id=12, date_from=str(today-timedelta(days=89)), date_to=str(yesterday))
        page = wb_expansion.feed_orders(args, wire)
        self.assertEqual([r["srid"] for r in page["items"]], ["1"])
        self.assertTrue(page["source_exhausted"])
        self.assertFalse(page["complete"])
        self.assertEqual(page["coverage_from"], str(today-timedelta(days=30)))

    def test_summary_resumes_goods_before_metadata_and_never_requests_packages(self):
        wire = ResumeWire()
        headers = market_specs.result([dict(supply_id=i, preorder_id=None) for i in range(1,5)])
        wait = finance_gate.GateError("cooldown", 60)
        page = market_specs.result([{"quantity": 77, "accepted_quantity": 76}])
        detail = lambda row, wire: dict(row, warehouse="Склад A", warehouse_id=1, actual_warehouse_id=None)
        args = dict(article="A", date_from="2026-10-01", date_to="2026-10-31")
        with patch.object(wb_expansion, "supply_headers", return_value=headers), patch.object(
                wb_expansion, "supply_detail", side_effect=detail), patch.object(
                wb_expansion, "_goods", side_effect=[page, wait, page, page, page]) as goods:
            first = supply_history.report("wb_supplies", args, wire)
            self.assertFalse(first["complete"])
            self.assertEqual(first["items"][0]["declared_units"], 77)
            last = supply_history.report("wb_supplies", args, wire)
            self.assertTrue(last["complete"])
            self.assertEqual([(r["supply_id"], r["warehouse"]) for r in last["items"]], [(i, "Склад A") for i in range(1, 5)])
            self.assertTrue(all(c.kwargs == {"include_packages": False} for c in goods.call_args_list))

    def test_ready_supply_snapshot_survives_a_refresh_and_can_be_replaced_by_confirmed_empty(self):
        wire = ResumeWire()
        args = dict(article="A",date_from="2026-09-01",date_to="2026-10-31")
        header = market_specs.result([dict(supply_id=93, preorder_id=None)])
        good = market_specs.result([{"quantity":77,"accepted_quantity":77}])
        with patch.object(wb_expansion,"supply_headers", side_effect=[header,finance_gate.GateError("cooldown",12),market_specs.result([])]), patch.object(
                wb_expansion,"_goods",return_value=good),patch.object(wb_expansion,"supply_detail",
                side_effect=lambda row, wire: dict(row,warehouse="W",warehouse_id=1,actual_warehouse_id=None)):
            with patch("supply_history.time.time",return_value=100000):
                first = supply_history.report("wb_supplies",args,wire)
            with patch("supply_history.time.time",return_value=104000):
                old = supply_history.report("wb_supplies",args,wire)
                new = supply_history.report("wb_supplies",args,wire)
        self.assertTrue(old["complete"])
        self.assertEqual((old["items"],old["data_as_of"],old["refresh_status"]), (first["items"],first["data_as_of"],"pending"))
        self.assertEqual((new["complete"],new["items"],new["refresh_status"]), (True,[],"ready"))

    def test_unavailable_warehouse_directory_keeps_ready_goods_and_reported_id(self):
        wire = ResumeWire()
        args = dict(article="A", date_from="2026-10-01", date_to="2026-10-31")
        headers = market_specs.result([dict(supply_id=i, preorder_id=None) for i in (1, 2)])
        goods = market_specs.result([dict(quantity=77, accepted_quantity=77)])
        detail = lambda parent, _: dict(parent, warehouse=None, warehouse_id=3026, actual_warehouse_id=3026)
        with patch.object(wb_expansion, "supply_headers", return_value=headers), patch.object(
                wb_expansion, "_goods", return_value=goods), patch.object(wb_expansion, "supply_detail",
                side_effect=detail), patch.object(wb_expansion, "_warehouse_directory", return_value={
                    "warehouses_status":"unavailable", "warehouse_catalog_http_status":503}) as lookup:
            report = supply_history.report("wb_supplies", args, wire)
            repeat = supply_history.report("wb_supplies", args, wire)
        self.assertTrue(report["complete"])
        self.assertEqual(report["items"], repeat["items"])
        self.assertEqual(lookup.call_count, 1)
        for row in report["items"]:
            self.assertEqual((row["warehouse"], row["warehouse_name"], row["warehouse_basis"]),
                             ("Склад WB №3026", None, "reported_id_only"))

    def test_warehouse_name_resumes_after_retry_after_without_reloading_goods(self):
        wire=ResumeWire()
        args=dict(article="A",date_from="2026-10-01",date_to="2026-10-31")
        headers=market_specs.result([dict(supply_id=7,preorder_id=None)])
        detail=lambda row,wire: dict(row,warehouse=None,warehouse_id=3026,actual_warehouse_id=None)
        limited=urllib.error.HTTPError("https://supplies-api.wildberries.ru/api/v1/warehouses",
                                      429,"limit",{"Retry-After":"60"},None)
        catalog={"warehouses_status":"reported","warehouses":[{"warehouse_id":3026,"name":"Склад A"}]}
        with patch.object(wb_expansion,"supply_headers", return_value=headers), patch.object(
                wb_expansion,"_goods",return_value=market_specs.result([{"quantity":77,"accepted_quantity":76}])) as goods, patch.object(
                wb_expansion,"supply_detail",side_effect=detail) as details, patch.object(
                wb_expansion,"_warehouse_directory",side_effect=[limited,catalog]) as directory, patch.object(supply_history.time,"time") as clock:
            clock.return_value=1000
            first=supply_history.report("wb_supplies",args,wire)
            self.assertFalse(first["warehouses_checked"])
            self.assertEqual(first["items"][0]["declared_units"],77)
            self.assertEqual(first["pending_sources"][0]["http_status"],429)
            clock.return_value=1040
            next_page=supply_history.report("wb_supplies",args,wire)
            self.assertEqual(next_page["pending_sources"][0]["retry_after_seconds"],20)
            self.assertIsNone(next_page["pending_sources"][0]["http_status"])
            self.assertEqual(directory.call_count,1)
            clock.return_value=1060
            done=supply_history.report("wb_supplies",args,wire)
            self.assertTrue(done["complete"] and done["warehouse_names_complete"])
            self.assertEqual(done["items"][0]["warehouse_name"],"Склад A")
            self.assertEqual((goods.call_count,details.call_count,directory.call_count),(1,1,2))

    def test_postings_archive_requires_both_schemes_and_keeps_financial_metrics_separate(self):
        class PostingWire(ResumeWire):
            provider, creds = "ozon", {"client_id":"1", "api_key":"fixture"}
            def __call__(self, method, url, **kwargs):
                scheme = "FBO" if "/fbo/" in url else "FBS"
                day_field = "created_at" if scheme == "FBO" else "in_process_at"
                item = {"sku":1, "offer_id":"01", "quantity":2, "price":{"amount":"12.50","currency":"RUB"}}
                return {"postings":[{"posting_number":scheme, day_field:"2026-11-15T06:00:00Z", "products":[item]}], "has_next":False}
        stored = {}
        history = SimpleNamespace(save_day=lambda owner, name, day, rows, stamp: stored.update({day:(stamp,rows)}),
                                  confirm=lambda *_:None,
                                  read_days=lambda _,name,*dates: stored if name=="ozon_posting_sku_daily" else {})
        self.assertEqual(market_archive.collect_posting_days(PostingWire(), history, "2026-11-15"), "complete")
        report = market_history.historical_read(history,"owner","ozon_sku_daily",
                        {"date_from":"2026-11-09","date_to":"2026-11-15","article":"01"})
        self.assertEqual((len(report["posting_archive_days"]),report["archived_days"],report["complete"]), (7,0,False))
        self.assertEqual({r["date_basis"] for r in report["items"]},{"created_at_utc","in_process_at_utc"})
        self.assertTrue(all(r["posting_units"]==2 and r["posting_item_value"]=="25.00" and
                            r["ordered_units"] is None and r["revenue"] is None for r in report["items"]))

    def test_only_an_interrupted_wb_orders_read_gets_a_bounded_refill(self):
        url = wb_expansion.STAT + "orders"
        creds = {"api_token":"x." + base64.urlsafe_b64encode(b'{"acc":3}').decode().rstrip("=") + ".x"}
        for interrupted in (True,False):
            with self.subTest(interrupted=interrupted), TemporaryDirectory() as folder, patch.object(
                    finance_gate, "_boot_identity", return_value="b" * 64):
                gate = str(Path(folder,".market-method-gates"))
                wire = market_transport.Wire("wb",creds,str(Path(folder,"finance")),float("inf"))
                guard = market_cooldown.method_key("wb",creds,url)
                slot = market_transport._id([wire.scope,"statistics-api.wildberries.ru","/api/v1/supplier/orders"])
                with wire.db() as db:
                    db.execute("INSERT INTO quota VALUES(?,?,0)", (slot, 0))
                exc = TimeoutError() if interrupted else urllib.error.HTTPError("",429,"",{},None)
                def fail():
                    raise exc
                with self.assertRaises(type(exc)):
                    finance_gate.run(gate, guard, fail)
                delay = wire.recover_interrupted_statistics()
                self.assertEqual(delay, 60 if interrupted else None)
                with self.assertRaises(finance_gate.GateError) as blocked:
                    finance_gate.run(gate,guard,lambda: self.fail("early read"))
                self.assertEqual(blocked.exception.reason,"cooldown" if interrupted else "retry_time_unknown")

    def test_weekly_funnel_fetches_calendar_intersections(self):
        def wire(method, url, **kwargs):
            self.assertTrue(url.endswith("/sales-funnel/products"))
            period = kwargs["body"]["selectedPeriod"]
            return {"data": {"currency": "RUB", "products": [{"product":{"nmId":12, "vendorCode":"A"},
                    "statistic":{"selected":{"period":period, "orderCount":3}}}]}}
        q = dict(date_from="2026-09-09", date_to="2026-09-18", group_by="week", nm_id=12)
        first = wb_reports.funnel_history(q, wire)
        last = wb_reports.funnel_history(dict(q, cursor=first["next_cursor"]), wire)
        self.assertEqual([(p["items"][0]["week_from"], p["items"][0]["week_to"], p["items"][0]["order_count"])
                          for p in (first,last)], [("2026-09-09","2026-09-13",3),("2026-09-14","2026-09-18",3)])
        self.assertTrue(last["complete"])

    def test_financial_checkpoint_uses_only_the_key_that_fetched_the_rows(self):
        base=ResumeWire()
        base.creds,base.gate_dir,base.fetch_times={"api_token":"base"},"/fixture",[]
        sources={token:ResumeWire() for token in ("A","B")}
        args=dict(article="same",nm_id=2,date_from="2026-10-01",date_to="2026-10-02")
        with patch.object(expansion_gateway,"Wire",side_effect=lambda p,c,*_:sources[c["api_token"]]), \
             patch.object(wb_expansion,"_finance",return_value={"items":[],"complete":True,"next_cursor":None}) as fetch:
            for token in ("A","B","A"):
                base.category_keys={"wb_finance":{"api_token":token}}
                self.assertTrue(expansion_gateway._finance_period(args,base)["complete"])
            self.assertEqual(fetch.call_count,2)
            self.assertEqual([call.args[1] for call in fetch.call_args_list],[sources["A"],sources["B"]])
            self.assertFalse(base.records)

    def test_by_id_finance_requires_end_marker_and_complete_date_coverage(self):
        wire = ResumeWire()
        wire.creds, wire.gate_dir = {}, "/fixture"
        args = dict(article="A", date_from="2026-10-01", date_to="2026-10-02")
        listed = {"http_status":200, "data":[{"reportId":7,"dateFrom":"2026-09-28","dateTo":"2026-10-04"}]}
        line = {"reportId":7, "rrdId":1, "vendorCode":"A", "nmId":2, "rrDate":"2026-10-01",
                "currency":"RUB", "quantity":1, "docTypeName":"Продажа", "sellerOperName":"Продажа", "forPay":90}
        with patch.object(expansion_gateway,"Wire",return_value=wire), patch("market_catalog.transfer",
                side_effect=[listed,{"http_status":200,"data":[line]},finance_gate.GateError("cooldown",60),
                             {"http_status":204,"data":None}]):
            first = expansion_gateway._finance_by_report(args, wire)
            self.assertFalse(first["complete"])
            last = expansion_gateway._finance_by_report(args, wire)
        self.assertTrue(last["complete"])
        self.assertEqual((last["rows_observed"],last["items"][0]["sold_units"]), (1,1))
        retained = next(iter(wire.records.values()))
        wire.progress_snapshot=lambda name, key: wire.records[(name,json.dumps(key,sort_keys=True))]
        with patch.object(wb_expansion,"_finance",side_effect=AssertionError("completed month queried again")):
            self.assertEqual(expansion_gateway._finance_period(args, wire),last)
        with patch.object(expansion_gateway.time,"time",return_value=retained["verified_at"]+86401), \
             patch.object(wb_expansion,"_finance",side_effect=finance_gate.GateError("cooldown",56)):
            pending=expansion_gateway._finance_period(args,wire)
            self.assertEqual((pending["items"],pending["refresh_status"],pending["retry_after_seconds"]),
                             (last["items"],"pending",56))
        with patch.object(expansion_gateway,"Wire",return_value=wire), patch("market_catalog.transfer",
                side_effect=finance_gate.GateError("cooldown",60)), patch.object(expansion_gateway.time,"time",
                return_value=retained["verified_at"]+86401):
            cached = expansion_gateway._finance_by_report(args, wire)
        self.assertEqual((cached["complete"],cached["refresh_status"],cached["data_as_of"],cached["items"]),
                         (True,"pending",last["data_as_of"],last["items"]))

    def test_weekly_order_archive_waits_for_terminal_page_and_reconciles_revisions(self):
        wire, saved = ResumeWire(), {}
        wire.provider, wire.creds = "wb", {"api_token":"fixture"}
        day = (datetime.now(market_specs.MOSCOW).date()-timedelta(days=1)).isoformat()
        history = SimpleNamespace(save_day=lambda owner,name,key,rows,stamp:saved.update({key:rows}), confirm=lambda *_:None)
        old = dict(date=day, last_change=day+"T01:00:00+03:00", srid="order1", cancelled=False)
        new = dict(old, last_change=day+"T01:01:00+03:00", cancelled=True)
        with patch.object(wb_expansion,"_stats",side_effect=[market_specs.result([old],"cursor"),
                market_specs.result([new],"cursor2"),market_specs.result([])]) as read:
            self.assertEqual(market_archive.collect(wire,history,"wb_orders",day),"pending")
            self.assertEqual(market_archive.collect(wire,history,"wb_orders",day),"pending")
            self.assertEqual(saved,{})
            self.assertEqual(market_archive.collect(wire,history,"wb_orders",day),"complete")
        self.assertEqual((len(saved),saved[day]),(7,[new]))
        self.assertTrue(all(not call.kwargs["exact_day"] for call in read.call_args_list))

    def test_seven_archived_ozon_days_come_from_one_complete_page(self):
        wire, saved = ResumeWire(), {}
        wire.provider, wire.creds = "ozon", {"client_id":"1"}
        day = "2026-11-15"
        history = SimpleNamespace(save_day=lambda owner, name, day, rows, stamp: saved.update({day: rows}), confirm=lambda *a: None)
        with patch.object(ozon_expansion, "_daily", return_value=market_specs.result([{"date":day,"article":"A","sku":1}] )) as report:
            self.assertEqual(market_archive.collect(wire, history, "ozon_sku_daily", day), "complete")
        self.assertEqual(report.call_args.args[0]["date_from"], "2026-11-09")
        self.assertEqual((len(saved),len(saved[day]),saved["2026-11-10"]), (7,1,[]))

    def test_availability_uses_current_verified_day_and_exact_seller_code(self):
        wire = SimpleNamespace(gate_dir="/fixture", creds={"client_id":"17"})
        day, stamp = "2026-11-19", "2026-11-19T06:01:11+00:00"
        data = [{"article":"01","warehouse_id":7,"sku":4,"available_stock_count":3},
                {"article":"1","warehouse_id":9,"sku":8,"available_stock_count":70}]
        with patch("market_history.History") as factory:
            db = factory.return_value
            db.verified_days.return_value = {day: (stamp,data)}
            page = supply_plan._archived_stock("ozon","01",None,wire,day)
            self.assertEqual(page["items"], data[:1])
            self.assertEqual(page["stock_observed_at"], stamp)
            db.verified_days.assert_called_once_with("ozon",wire.creds,"ozon_availability",day,day)
            db.verified_days.return_value = {}
            self.assertIsNone(supply_plan._archived_stock("ozon","01",None,wire,day))


if __name__ == "__main__":
    unittest.main()
