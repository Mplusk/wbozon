"""Synthetic checks for WB office snapshots and unavailable card sources."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from market_specs import MOSCOW
from seller_views import Reader, SourceIssue
import seller_wb_views as wb


class FakeReader:
    section = Reader.section

    def __init__(self, args=None):
        self.args=args or {}
        self.first=self.last=datetime.now(MOSCOW).date()
        self.ids={"123"}
        self.article=None
        self.directory="synthetic"
        self.wire=SimpleNamespace(creds={"api_token":"synthetic-test-token"})
        self.issues=[]
        self.meta={}
        self.stamps=[]
        self.office_result={"data":{"regions":[]}}

    def product(self, row, article, ident):
        if str(ident) not in self.ids:return []
        return [dict(row, article=article, nm_id=ident)]

    def pages(self, group, choice, *args):
        if choice=="stock_now":
            yield {"nmId":123,"vendorCode":"test-article","warehouseName":"Склад WB РФ","quantity":10}
        elif choice=="item_rating":
            raise SourceIssue(source="/api/analytics/v2/item-rating",status="provider_error",http_status=400)
        elif choice=="content_recommendations":
            raise SourceIssue(source="/api/content/v1/recommendations/list",status="provider_access_denied",http_status=403)

    def fetch(self, group, choice, *args):
        if choice=="offices":
            if isinstance(self.office_result,Exception):raise self.office_result
            self.stamps.append(datetime.now(MOSCOW).isoformat())
            return self.office_result
        if choice=="blocked":return {"report":[]}
        if choice=="price_quarantine":return {"data":{"quarantineGoods":[]}}
        raise AssertionError(choice)


def office(ident, name, stock):
    return {"officeID":ident,"officeName":name,"metrics":{"stockCount":stock}}


def region(stock, offices):
    return {"regionName":"test-region","metrics":{"stockCount":stock},"offices":offices}


class FakeHistory:
    def __init__(self):
        self.records={}
        self.confirmed=set()

    def save_day(self, owner, dataset, day, rows, stamp):
        self.records[owner,dataset,day]=(stamp,[dict(row) for row in rows])

    def confirm(self, creds, owner):
        self.confirmed.add((wb.market_history.credential_id(creds),owner))

    def verified_days(self, provider, creds, dataset, start, end):
        owner=wb.market_history.scope(provider,creds)
        if (wb.market_history.credential_id(creds),owner) not in self.confirmed:return {}
        return {day:value for (own,ds,day),value in self.records.items()
                if own==owner and ds==dataset and start<=day<=end}


class InventoryCardFixTests(unittest.TestCase):
    def inventory(self, reader):
        with patch.object(wb,"_history",return_value={}), patch.object(wb.market_history,"History",return_value=FakeHistory()):
            return list(wb._inventory(reader))

    def test_filtered_warehouse_uses_offices_even_without_article_argument(self):
        reader=FakeReader({"warehouse_id":2})
        reader.office_result={"data":{"regions":[region(10,[office(1,"A",4),office(2,"B",6)])]}}
        rows=self.inventory(reader)
        self.assertEqual([(row["warehouse"],row["stock"]) for row in rows],[("B",6)])
        self.assertEqual(reader.meta["office_data_scope"],"current_day_only")
        self.assertTrue(reader.meta["warehouse_snapshot_comparison"]["totals_match"])

    def test_different_snapshot_totals_are_explicit(self):
        reader=FakeReader()
        reader.office_result={"data":{"regions":[region(8,[office(1,"A",8)])]}}
        rows=self.inventory(reader)
        self.assertEqual(rows[0]["stock"],8)
        self.assertEqual(reader.meta["warehouse_snapshot_comparison"]["stock_now"],10)
        self.assertFalse(reader.meta["warehouse_snapshot_comparison"]["totals_match"])

    def test_invalid_or_inaccessible_offices_keep_aggregate_and_report_problem(self):
        for response in (SourceIssue(source="offices",status="provider_access_denied",http_status=403),
                         {"data":{"regions":[region(10,[office(1,"A",4),office(1,"A",6)])]}},
                         {"data":{"regions":[region(10,[office(1,"A",4)])]}}):
            with self.subTest(response=response):
                reader=FakeReader()
                reader.office_result=response
                rows=self.inventory(reader)
                self.assertEqual(rows[0]["warehouse"],"Склад WB РФ")
                self.assertTrue(reader.issues)
                self.assertEqual(reader.meta["warehouse_scope"],"provider_aggregate")

    def test_region_without_offices_is_not_falsely_split(self):
        reader=FakeReader()
        reader.office_result={"data":{"regions":[region(10,[])]}}
        rows=self.inventory(reader)
        self.assertEqual(rows[0]["basis"],"current_region_aggregate")
        self.assertEqual(reader.meta["warehouse_scope"],"provider_aggregate")

    def test_old_aggregate_is_not_claimed_as_filtered_warehouse_history(self):
        reader=FakeReader({"warehouse_id":1})
        reader.first=reader.last-timedelta(days=1)
        reader.office_result={"data":{"regions":[region(10,[office(1,"A",10)])]}}
        yesterday=reader.first.isoformat()
        old={yesterday:(None,[{"nmId":123,"vendorCode":"test-article","warehouseName":"Склад WB РФ","quantity":9}])}
        with patch.object(wb,"_history",side_effect=lambda r,d:old if d=="wb_warehouse_daily" else {}), patch.object(wb.market_history,"History",return_value=FakeHistory()):
            rows=list(wb._inventory(reader))
        self.assertEqual(len(rows),1)
        self.assertEqual(reader.meta["observed_days"],1)
        self.assertEqual(reader.meta["missing_dates"],[yesterday])
        self.assertTrue(any(i["status"]=="aggregate_not_attributable" for i in reader.issues))

    def test_saved_offices_are_used_in_later_historical_query_for_same_account_only(self):
        history=FakeHistory()
        reader=FakeReader()
        reader.office_result={"data":{"regions":[region(10,[office(1,"A",4),office(2,"B",6)])]}}
        first_day=reader.first
        with patch.object(wb.market_history,"History",return_value=history):
            self.assertEqual(len(list(wb._inventory(reader))),2)
            self.assertTrue(reader.meta["office_snapshot_archived"])
            class Tomorrow(datetime):
                @classmethod
                def now(cls,tz=None):return datetime.now(tz)+timedelta(days=1)
            later=FakeReader()
            later.first=later.last=first_day
            with patch.object(wb,"datetime",Tomorrow):
                rows=list(wb._inventory(later))
                self.assertEqual({row["warehouse"] for row in rows},{"A","B"})
                self.assertTrue(all(row.get("observed_at") for row in rows))
                other=FakeReader()
                other.first=other.last=first_day
                other.wire.creds={"api_token":"synthetic-other-account"}
                self.assertEqual(list(wb._inventory(other)),[])
                self.assertEqual(other.meta["missing_dates"],[first_day.isoformat()])

    def test_failed_office_response_does_not_save_partial_snapshot(self):
        history=FakeHistory()
        reader=FakeReader()
        reader.office_result={"data":{"regions":[region(10,[office(1,"A",4)])]}}
        with patch.object(wb.market_history,"History",return_value=history):
            list(wb._inventory(reader))
        self.assertEqual(history.records,{})

    def test_different_analytics_account_is_not_archived(self):
        history=FakeHistory()
        reader=FakeReader()
        reader.wire.category_keys={"wb_analytics":{"api_token":"synthetic-other-account"}}
        reader.office_result={"data":{"regions":[region(10,[office(1,"A",10)])]}}
        with patch.object(wb.market_history,"History",return_value=history):
            list(wb._inventory(reader))
        self.assertEqual(history.records,{})
        self.assertFalse(reader.meta["office_snapshot_archived"])

    def test_card_issues_explain_400_and_403_without_claiming_success(self):
        reader=FakeReader()
        list(wb._card_issues(reader))
        rating=next(i for i in reader.issues if i.get("http_status")==400)
        rec=next(i for i in reader.issues if i.get("http_status")==403)
        self.assertEqual(rating["reason"],"provider_rejected_request")
        self.assertEqual(rec["required_category"],"Контент")
        self.assertIn("Джем",rec["required_subscription"])
        self.assertEqual(rec["supported_token_types"],["Персональный","Сервисный"])
        self.assertNotIn("item_rating",reader.meta["counts"])
        self.assertNotIn("content_recommendations",reader.meta["counts"])


if __name__=="__main__":unittest.main()
