import contextlib
import copy
from datetime import date, datetime, timedelta
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import avito_mcp
import finance_gate
import market_catalog
import market_specs
import seller_insights
import seller_ozon_views
import seller_reports
import seller_views
import seller_wb_views
import seller_workbooks


class Contracts(unittest.TestCase):
    def test_all_scoped_tools_publish_read_interfaces(self):
        self.assertEqual(len(seller_views.VIEWS),24)
        self.assertEqual(len(avito_mcp.PUBLIC_CONTRACTS.keys()-avito_mcp.CONTRACTS.keys()),8)
        for name in seller_views.VIEWS:
            props,_=market_specs.schema(name)
            self.assertTrue({"mode","limit","offset","cursor","task_id"}<=set(props))
            self.assertIn(name,market_specs.TOOLS)
        for name in avito_mcp.PUBLIC_CONTRACTS.keys()-avito_mcp.CONTRACTS.keys():
            self.assertNotIn(name,market_specs.TOOLS)

    def reader(self,name,query):
        r=seller_views.Reader.__new__(seller_views.Reader)
        r.name,r.args,r.market=name,query,name.split("_")[0]
        r.article,r.ids,r.products="fixture",{"101","102"},{"101":"fixture","102":"fixture"}
        r.first,r.last=date(2026,10,10),date(2026,10,12)
        r.meta,r.issues,r.stamps={},[],[]
        r.deadline=time.monotonic()+10
        r.wire=SimpleNamespace(scope="fixture",progress=lambda *a:contextlib.nullcontext((None,lambda *a:None)))
        return r

    def test_partial_rows_survive_source_failure(self):
        r=self.reader("ozon_buyout",{})
        def numbers():
            yield {"quantity":1}
            raise finance_gate.GateError("cooldown",50)
        self.assertEqual(r.section("sample",numbers),[{"quantity":1}])
        self.assertEqual((r.issues[0]["reason"],r.issues[0]["http_status"]),("local_limiter",None))

    def test_acceptance_scan_retains_acts_and_stops_after_three_upstream_failures(self):
        state = {}
        calls = []

        @contextlib.contextmanager
        def progress(*_):
            yield copy.deepcopy(state), lambda value, ttl: state.update(copy.deepcopy(value))

        def fetch(group, choice, params):
            body = params["body"]
            calls.append((choice, body))
            if choice == "accepted_products":
                return {"supply_acts": [{"act_id": 7, "items": [
                    {"sku_info": {"sku": 101, "offer_id": "fixture"}, "declared_quantity": 4, "fact_quantity": 3}]}]}
            if body["order_id"] == 1:
                return {"supplies_acts": [{"supply_id": 61}]}
            raise seller_views.SourceIssue(source="/v1/supply-order/act/summary/get",
                                          status="rate_limited", http_status=429, retry_after_seconds=60)

        def read():
            reader = self.reader("ozon_supply_acts", {})
            reader.wire.progress = progress
            reader.cached = lambda *_: {"order_ids": [1, 2]}
            reader.fetch = fetch
            return seller_views.render(reader, seller_ozon_views._acts)

        with patch.object(seller_ozon_views.time, "time") as clock:
            clock.return_value = 1000
            first = read()
            self.assertEqual((first["status"], first["items"][0]["difference"], first["unread_order_ids"]),
                             ("pending", -1, [2]))
            clock.return_value = 1040
            self.assertEqual(read()["source_issues"][-1]["retry_after_seconds"], 20)
            self.assertEqual(len(calls), 3)
            clock.return_value = 1060
            self.assertEqual(read()["status"], "pending")
            clock.return_value = 1120
            final = read()
            self.assertEqual((final["status"], final["unread_order_ids"], len(final["items"])), ("partial", [2], 1))
            self.assertEqual((read()["status"], len(calls)), ("partial", 5))

    def test_account_expenses_allocate_signed_costs_without_repeating_item_ads(self):
        window = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
        rows = [dict(window, currency="RUB", kind="sales", amount="1000"),
                dict(window, currency="RUB", kind="service", name="cross_docking", amount="-50"),
                dict(window, currency="RUB", kind="service", name="pay_per_click", amount="-20")]
        row = {"currency": "RUB", "sold_units": 10, "amounts": {"gross_sales": "300"},
               "net_after_expenses": {"amount": "150", "deducted": ["ads"]}}
        seller_insights._account_expense_estimate(row, {"source_complete": True, "items": rows}, window)
        actual = row["net_after_account_expenses_estimate"]
        self.assertEqual((actual["amount"], actual["per_sold_unit"], actual["allocated_costs"]),
                         ("135", "13.5", {"cross_docking": "15"}))
        self.assertEqual(row["net_after_expenses"]["amount"], "150")

    def test_wb_rating_requests_latest_closed_day_for_today(self):
        reader = self.reader("wb_card_issues", {})
        today = datetime.now(market_specs.MOSCOW).date()
        reader.first = reader.last = today
        reader.fetch = lambda *_: {"report": [], "data": {"quarantineGoods": []}}
        requests = {}
        def pages(group, choice, arguments, *_):
            requests[choice] = arguments
            return []
        reader.pages = pages
        list(seller_wb_views._card_issues(reader))
        closed_day = (today-timedelta(days=1)).isoformat()
        self.assertEqual(requests["item_rating"]["body"]["currentPeriod"], {"start":closed_day,"end":closed_day})
        self.assertEqual(reader.meta["rating_period_requested"], {"start":today.isoformat(),"end":today.isoformat()})
        reader.ids = {"101"}
        list(seller_wb_views._card_issues(reader))
        self.assertEqual(requests["content_recommendations"]["body"]["search"],"101")

    def test_card_issues_summary_covers_all_rows_and_details_are_paginated(self):
        reader = self.reader("wb_card_issues", {"limit":1})
        card = {"nmId":101,"vendorCode":"fixture"}
        reader.fetch = lambda group, choice, *_: {
            "blocked":{"report":[card]},"price_quarantine":{"data":{"quarantineGoods":[card]}},
        }[choice]
        reader.pages = lambda group, choice, *_: {
            "item_rating":[dict(card,isShadowed=True),dict(card,nmId=102,isShadowed=False)],
            "content_recommendations":[card],
        }[choice]
        summary = seller_views.render(reader)
        self.assertEqual(summary["counts"],dict(blocked=1,item_rating=2,shadowed=1,price_quarantine=1,content_recommendations=1))
        self.assertEqual(summary["items"],[])
        self.assertIsNone(summary["next_cursor"])
        self.assertEqual(summary["total_rows"],5)
        self.assertTrue(summary["complete"])
        reader.args.update(mode="details",cursor="1")
        details = seller_views.render(reader)
        self.assertEqual(details["items"][0]["kind"],"item_rating")
        self.assertEqual((len(details["items"]),details["next_cursor"]),(1,2))

    def test_wb_news_matches_visible_words_and_returns_a_body_excerpt(self):
        reader = self.reader("wb_seller_info", {"query":" aPi "})
        news = [
            {"id":1,"header":"Чат с покупателями","content":'<a href="https://example.test/API">Чат</a> https://example.test/api'},
            {"id":2,"header":"Кроссплатформенный AppAPI","content":"<style>API</style><!-- API -->"},
            {"id":3,"header":"Изменения в <b>API</b>"},
            {"id":4,"header":"Новая выгрузка","content":"<p>Отчёт доступен через <b>API</b>.</p>"},
        ]
        for row in news:
            row["date"]="2026-10-11T03:00:00+03:00"
        reader.fetch = lambda group, choice, *_: {"data":news} if choice=="news" else {}
        reader.pages = lambda *_: []
        rows = [row for row in seller_wb_views._seller_info(reader) if row["kind"]=="news"]
        self.assertEqual([row["news_id"] for row in rows],[3,4])
        self.assertEqual(rows[1]["content_excerpt"],"Отчёт доступен через API.")

    def test_wb_chat_message_time_is_an_iso_utc_timestamp(self):
        reader = self.reader("wb_questions", {})
        reader.ids.clear()
        reader.pages = lambda *_: []
        chats = [{"chatID":"fixture-chat","lastMessage":{"addTimestamp":1766138234889}},{"chatID":"empty"}]
        reader.fetch = lambda group, choice, *_: {"result":chats} if choice=="chats" else {"data":{}}
        self.assertEqual([row["last_message_at"] for row in seller_wb_views._questions(reader)],
                         ["2025-12-19T09:57:14.889Z",None])

    def test_ozon_promotions_keep_actual_prices_separate_from_candidate_limits(self):
        reader = self.reader("ozon_promotions", {"article":"fixture"})
        reader.fetch = lambda *_, **__: {"result":[{"id":12,"title":"fixture"}]}
        def price(value):
            return {"amount":value,"currency":"RUB"}
        reader.pages = lambda group, kind, *_: [{"id":77, "price":price("500"),
            "action_price":price("299.90" if kind=="participating_products" else "0"),
            "max_action_price":price("350"), "alert_max_action_price":price("330")}]
        active, candidate = seller_ozon_views._promotions(reader,{"product_id":77})
        self.assertEqual(active["action_price"],price("299.90"))
        self.assertEqual((active["amount"],active["currency"]),("299.90","RUB"))
        self.assertIsNone(candidate["action_price"])
        self.assertIsNone(candidate["amount"])
        self.assertEqual(candidate["regular_price"],price("500"))
        self.assertEqual(candidate["max_action_price"],price("350"))
        self.assertEqual(candidate["recommended_action_price"],price("330"))

    def test_ozon_fbs_stock_requests_match_catalog_for_article_sku_and_warehouse(self):
        cases = [
            ({},None,set(),[("warehouses",{"limit":100}),("warehouse_stock",{"limit":100,"warehouse_id":7})]),
            ({"warehouse_id":7},None,set(),[("warehouse_stock",{"limit":100,"warehouse_id":7})]),
            ({"sku":101},"fixture",{"101"},[("stock_by_warehouse",{"limit":100,"sku":["101"]})]),
            ({"article":"fixture"},"fixture",{"101","102"},[("stock_by_warehouse",{"limit":100,"offer_id":["fixture"]})]),
        ]
        for args, article, ids, expected in cases:
            with self.subTest(args=args):
                reader = self.reader("ozon_fbs_stocks", args)
                reader.article, reader.ids = article, ids
                calls = []
                def fetch(group, choice, inputs, *_):
                    calls.append((choice, inputs["body"]))
                    op = seller_reports.GROUPS[group][1][choice]
                    market_catalog.validate("market_api_read",{"operation_id":op,"input":inputs})
                    if choice == "warehouses":
                        return {"warehouses":[{"warehouse_id":7,"name":"fixture warehouse"}],"has_next":False}
                    field = "stocks" if choice=="warehouse_stock" else "products"
                    return {field:[{"sku":101,"offer_id":"fixture","free_stock":0,"warehouse_id":7}],"has_next":False}
                reader.fetch = fetch
                rows = list(seller_ozon_views._fbs_stocks(reader))
                self.assertEqual(calls,expected)
                self.assertEqual((rows[0]["warehouse_id"],rows[0]["free_stock"]),(7,0))
                if not args:
                    self.assertEqual(rows[0]["warehouse_name"],"fixture warehouse")

    def test_compensation_dates_identity_and_duplicate_operations(self):
        r=self.reader("ozon_compensations",{"article":"fixture"})
        typ="COMPENSATION_REPORT"
        report=dict(report_type=typ,params={"month":"2026-10"},code="sample",status="ready",file="fixture")
        r.fetch=lambda *a:{"result":report}
        raw=("Дата;Артикул;Сумма, ₽;Номер операции\n2026-10-12;fixture;100;op1\n"
             "2026-10-12;fixture;100;op1\n2026-10-25;fixture;50;op2\n").encode()
        with patch.object(seller_workbooks,"_download",return_value=raw):
            items=list(seller_workbooks.settlements(r,[report],"2026-10",typ))
            self.assertEqual([(i["article"],i["amount"],i["currency"]) for i in items],[("fixture","100","RUB")])
            r.args["sku"]=101
            self.assertFalse(list(seller_workbooks.settlements(r,[report],"2026-10",typ)))
            self.assertEqual(r.meta["unresolved_product_rows"],1)
            r.fetch=lambda *a:{"result":dict(report,params={"month":"2026-11"})}
            with self.assertRaisesRegex(ValueError,"report_month"):
                list(seller_workbooks.settlements(r,[report],"2026-10",typ))

    def test_missing_marketplace_card_is_visible(self):
        r=self.reader("ozon_card_quality",{"article":"fixture"})
        r.fetch=lambda group,choice,query:{"products" if choice=="content_rating" else "items":[{"sku":"101"}]}
        with patch.object(seller_ozon_views,"_identity",return_value={}):
            result=seller_views.render(r)
        self.assertEqual(result["status"],"partial")
        self.assertEqual([e["missing_skus"] for e in result["source_issues"]],[["102"],["102"]])

    def test_date_qualified_net_keeps_retail_units_and_confirmed_ledger_amount(self):
        row=dict(marketplace="ozon",sold_units=19,net_after_expenses={"per_sold_unit":"161.8158"})
        calls={}
        def get(name,query):
            calls[name]=query
            return {"status":"ready","complete":True,"items":[{"quantity":2}]} if name=="ozon_buyout" else {
                "status":"no_confirmed_export_for_month","complete":False,"items":[]}
        seller_insights._units(row,get,{"month":"2026-10"})
        self.assertEqual(row["sold_units"],19)
        self.assertEqual(row["net_after_expenses"]["per_sold_unit"],"161.8158")
        self.assertFalse(row["compensation_income"]["included_in_net"])
        self.assertIsNone(row["wrong_volume"])
        self.assertEqual(calls["ozon_removals"],{"date_from":"2026-10-01","date_to":"2026-10-31"})

    def test_buyout_without_amount_preserves_quantity_and_marks_money_missing(self):
        r=self.reader("ozon_buyout",{})
        report=seller_views.render(r,lambda _: [dict(article="fixture",quantity=2,amount=None,currency=None)])
        self.assertTrue(report["complete"])
        self.assertEqual(report["monetary_coverage"],"amount_missing")
        self.assertEqual(report["period_totals"][0]["quantity"],2)

    def test_compensation_changes_net_only_for_exact_incoming_item_fee(self):
        entry=dict(operation_id="301",sku="101",date="2026-10-12",currency="RUB",amount="10",kind="COMPENSATION_REPORT")
        def ledger(tool,args):
            if tool=="ozon_accrual_types":return {"types":[{"id":5,"name":"Компенсация за утрату"}]}
            return {"date":args["date"],"requested_cursor":args.get("last_id",""),"next_cursor":"","matching_rows":[dict(category="ITEM",accrual_id="301",sku="101",fees=[
                dict(type_id=5,accrued=dict(amount="10",currency="RUB"))])]}
        for operation,expected in (("301","110"),("302","100")):
            row=dict(sold_units=2,currency="RUB",net_after_expenses=dict(amount="100",per_sold_unit="50"),compensation_income={})
            page={"complete":True,"items":[dict(entry,operation_id=operation)]}
            seller_insights._compensation_adjustment(row,page,ledger)
            seller_insights._compensation_adjustment(row,page,ledger)
            self.assertEqual(row["net_after_expenses"]["amount"],expected)
            def partial(tool,args):
                if tool=="ozon_accrual_types":return ledger(tool,args)
                if args.get("last_id"):return {"status":"pending"}
                return dict(ledger(tool,args),next_cursor="p2")
            seller_insights._compensation_adjustment(row,page,partial)
            self.assertEqual(row["net_after_expenses"]["amount"],"100")
            self.assertFalse(row["compensation_income"]["included_in_net"])

    def test_missing_retry_header_resumes_only_after_one_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=str(Path(tmp)/"gate")
            now=[100.]
            with patch.object(finance_gate.time,"monotonic",side_effect=lambda:now[0]), patch.object(
                    finance_gate.time,"time",side_effect=lambda:1000+now[0]), patch.object(finance_gate,"_boot_identity",return_value="a"*64):
                def fail():raise urllib.error.HTTPError("",429,"",{},io.BytesIO())
                with self.assertRaises(urllib.error.HTTPError):
                    finance_gate.run(gate,"1",fail,read_recovery=True)
                now[0]+=59
                with self.assertRaises(finance_gate.GateError) as blocked:
                    finance_gate.run(gate,"1",lambda:self.fail("early API call"),read_recovery=True)
                self.assertEqual(blocked.exception.retry_after_seconds,1)
                now[0]+=1
                self.assertEqual(finance_gate.run(gate,"1",lambda:7,read_recovery=True),7)

    def test_avito_item_income_keeps_seller_commission_out_of_second_deduction(self):
        group_type="item"
        duplicate_promotion=False
        def provider(tool,args,*_):
            if tool=="avito_get_item_info":
                self.assertEqual(args,{"item_id":123})
                return {"data":{"autoload_item_id":"sample","status":"active"}}
            if tool=="avito_item_analytics":
                self.assertNotIn("filter",args)
                values={"deliveredItems":2,"deliveredItemsPrice":10000,"promoSpending":1000,"presenceSpending":500,
                        "commission":200,"restSpending":0}
                self.assertEqual(set(args["metrics"]),set(values))
                metrics=[{"slug":k,"value":v} for k,v in values.items()]
                if duplicate_promotion:
                    metrics.append({"slug":"promoSpending","value":1000})
                return {"data":{"result":{"groupings":[{"id":"123","type":group_type,"metrics":metrics}]}}}
            self.fail("Unexpected source for item expenses")
        def calculate():
            return seller_insights._avito("unit_economics",{"article":"sample","month":"2026-10"},
                {"avito_item_id":123,"avito_article":"sample"},{"client_id":"fixture"},"fixture",time.monotonic()+10)
        with patch.object(avito_mcp,"read",side_effect=provider):
            actual=calculate()
            self.assertEqual(actual["net_after_expenses"]["per_sold_unit"],"42.5")
            self.assertEqual(actual["sold_units"],2)
            group_type="month"
            self.assertIsNone(calculate()["delivered_payout"])
            group_type="item"
            duplicate_promotion=True
            partial=calculate()
            self.assertEqual((partial["delivered_payout"],partial["sold_units"]),("100",2))
            self.assertIsNone(partial["net_after_expenses"]["amount"])
            self.assertNotIn("promotion",partial["expenses"])
