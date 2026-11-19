import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import finance_gate
import market_catalog
import market_specs
import seller_reports
import wb_reports


class ReportContracts(unittest.TestCase):
    def test_every_report_selects_a_reviewed_read_operation(self):
        for group, (_, choices) in seller_reports.GROUPS.items():
            props, required = market_specs.schema(group)
            self.assertEqual(required, ["report"])
            self.assertEqual(set(props["report"]["enum"]), set(choices))
            for option, op in choices.items():
                with self.subTest(group=group, report=option):
                    route, inp = seller_reports.request(group, {"report":option})
                    self.assertIsNone(inp)
                    self.assertEqual((route["category"],route["operation_id"]), ("read",op))
        group="ozon_settlement_reports"
        route,inputs=seller_reports.request(group, {"report":"decompensations","input":{}})
        self.assertEqual(inputs["body"],{"page":1,"page_size":100,"report_type":"DECOMPENSATION_REPORT"})
        with self.assertRaises(ValueError):
            seller_reports.request(group,{"report":"decompensations","input":{"body":{"report_type":"ALL"}}})
        with self.assertRaises(ValueError):
            seller_reports.request("wb_product_reports",{"report":"shadowed","input":{"body":{"onlyShadowedNms":False}}})

    def test_macrocluster_result_has_names_and_counts_without_warehouse_dump(self):
        raw={"data":{"result":[{"macrolocal_cluster_id":"4036","data":{
            "macrolocal_cluster":{"name":"Воронеж"},"fulfillments":[{"warehouse_id":1}]}},
            {"macrolocal_cluster_id":4039,"data":{"macrolocal_cluster":{"name":"Москва"},"fulfillments":[]}}]}}
        with patch.object(market_catalog,"transfer",return_value=raw):
            result=seller_reports.execute("ozon_fbo_reports",{"report":"macroclusters","input":{},"cluster_id":4036},{},None,0)
        self.assertEqual(result["data"],{"clusters":[{"cluster_id":4036,"name":"Воронеж","warehouse_count":1}]})

    def test_daily_history_uses_calendar_dates_and_product_pagination(self):
        def read(args, wire):
            self.assertEqual(args["date_from"],args["date_to"])
            return market_specs.result([{"order_count":7}], 10 if not args["offset"] else None)
        args={"date_from":"2025-10-01","date_to":"2025-10-02","nm_id":77,"group_by":"day"}
        with patch("wb_expansion._funnel",side_effect=read):
            first=wb_reports.funnel_history(args,None)
            second=wb_reports.funnel_history(dict(args,cursor=first["next_cursor"]),None)
            third=wb_reports.funnel_history(dict(args,cursor=second["next_cursor"]),None)
        self.assertEqual([first["next_cursor"],second["next_cursor"],third["items"][0]["date"]],
                         ["2025-10-01|10","2025-10-02|0","2025-10-02"])

    def test_csv_exact_product_date_page_and_no_disclosure_inside_zip(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,"w") as z:
            z.writestr("data.csv","nmID;dt;ordersCount;currency\n7;2025-10-01;5;RUB\n8;2025-10-02;9;RUB\n7;2025-10-02;2;RUB\n")
        wire=SimpleNamespace(creds={"api_token":"private-fixture"},gate_dir="test",deadline=time.monotonic()+10)
        args={"group_by":"day","date_from":"2025-10-01","date_to":"2025-10-02","nm_id":7,
              "download_id":"a25fae47-fb03-48fe-82a1-f814bae51f9f","limit":1}
        with patch.object(seller_reports,"_download",return_value=(raw.getvalue(),"observed")):
            first=seller_reports.funnel_csv(args,wire)
            second=seller_reports.funnel_csv(dict(args,cursor=first["next_cursor"]),wire)
        self.assertEqual(([r["order_count"] for r in first["items"]+second["items"]],second["complete"]),([5,2],True))
        with self.assertRaises(finance_gate.ProviderResponseInvalid):
            list(seller_reports._records(b"name\nprivate-fixture",wire.creds,wire.deadline))

    def test_stock_csv_uses_all_sizes_and_preserves_blank_values(self):
        raw=("VendorCode,NmID,ChrtID,OfficeName,15.11.2026,16.11.2026,17.11.2026\n"
             "A,12,1,Office,3,0,\nA,12,2,Office,2,0,1\nA,13,3,Office,99,99,99\n").encode()
        wire=SimpleNamespace(creds={},gate_dir="",deadline=time.monotonic()+10)
        with patch.object(seller_reports,"_download",return_value=(raw,"observed")):
            data=seller_reports.stock_csv("id","A",12,"2026-11-01","2026-11-18",wire)
        self.assertEqual(data["offices"]["Office"],{"2026-11-15":5,"2026-11-16":0,"2026-11-17":None})
        self.assertEqual(data["observation"],"stock_at_23_59_moscow")

    def test_full_day_stockout_requires_verified_duration_and_exact_product(self):
        def reply(args, query, *unused):
            period=query["input"]["body"]["currentPeriod"]
            h=-4 if period["start"]==period["end"]=="2026-11-18" else -2 if period["end"]=="2026-11-17" else 8
            return {"http_status":200,"data":{"data":{"items":[{"nmID":12,"vendorCode":"A",
                "metrics":{"officeMissingTime":{"days":0,"hours":h}}}]}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(market_catalog,"transfer",side_effect=reply):
            wire=SimpleNamespace(creds={"api_token":"synthetic"},gate_dir=str(Path(directory)/"gate"),
                                 deadline=time.monotonic()+15)
            for _ in range(3):
                out=seller_reports.availability_days("A",12,"2026-11-17","2026-11-18",wire)
            self.assertEqual(out["known_days"],["2026-11-17","2026-11-18"])
            self.assertEqual(out["whole_day_stockouts"],["2026-11-18"])
            self.assertEqual(out["pending_periods"],0)


if __name__ == "__main__":
    unittest.main()
