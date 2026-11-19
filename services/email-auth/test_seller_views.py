import contextlib
from datetime import date
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
import market_specs
import seller_insights
import seller_ozon_views
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
