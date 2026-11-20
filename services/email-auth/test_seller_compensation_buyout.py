"""Synthetic read-only checks for compensation guidance and Ozon buyout currency."""
from datetime import date
import time
import unittest
from unittest.mock import patch

import seller_ozon_views
import seller_views


class Reader:
    def __init__(self):
        self.name="ozon_buyout"
        self.market="ozon"
        self.args={}
        self.first=date(2026,10,1)
        self.last=date(2026,10,31)
        self.deadline=time.monotonic()+15
        self.meta={}
        self.issues=[]
        self.stamps=[]
        self.calls=[]
        self.products=[]
        self.profile={"company":{"currency":"RUB"}}
        self.wire=Wire(self)

    def fetch(self,group,choice,inputs):
        self.calls.append((group,choice,inputs))
        return {"products":self.products}

    def cached(self,operation,inputs):
        self.calls.append((operation,inputs))
        return self.profile

    def product(self,row,article,sku):
        return [dict(row,article=article,sku=sku)]

    def section(self,source,compute):
        result=[]
        try:
            result.extend(compute())
        except seller_views.SourceIssue as exc:
            self.issues.append(exc.info)
        return result


class Wire:
    def __init__(self,reader):
        self.reader=reader
        self.creds={"api_key":"fixture-secret"}
        self.deadline=reader.deadline
        self.scope="fixture"
        self.fetch_times=[]
        self.failure=None

    def __call__(self,method,url,**kwargs):
        self.reader.calls.append((method,url,kwargs))
        if self.failure:raise self.failure
        return self.reader.profile


class BuyoutAndCompensationTests(unittest.TestCase):
    def test_missing_export_has_location_and_explanation_without_creation(self):
        r=Reader()
        r.pages=lambda *args:iter([])
        self.assertEqual(list(seller_ozon_views._compensations(r)),[])
        self.assertEqual(len(r.issues),2)
        for issue in r.issues:
            self.assertEqual(issue["status"],"no_confirmed_export_for_month")
            self.assertEqual(issue["month"],"2026-10")
            self.assertIn("Финансы",issue["guidance"]["where"])
            self.assertIn("не означает",issue["guidance"]["retry"])
        self.assertEqual(r.calls,[])

    def test_unready_export_also_explains_what_to_do(self):
        r=Reader()
        r.pages=lambda *args:iter([{"report_type":args[1],"params":{"month":"2026-10"}}])
        def missing(*_):
            raise seller_views.SourceIssue(status="export_not_ready")
        with patch("seller_workbooks.settlements",side_effect=missing):
            self.assertEqual(list(seller_ozon_views._compensations(r)),[])
        self.assertEqual(len(r.issues),2)
        self.assertTrue(all("guidance" in x for x in r.issues))

    def test_explicit_row_currency_is_preserved_for_scalar_amounts(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":2,"amount":100,"buyout_price":50,
                     "currency_code":"RUB"}]
        rows=list(seller_ozon_views._buyouts(r))
        self.assertEqual((rows[0]["amount"],rows[0]["currency"]),("100","RUB"))
        self.assertEqual((rows[0]["buyout_price"],rows[0]["buyout_price_currency"]),("50","RUB"))
        self.assertFalse(any(call[0]=="POST" for call in r.calls))

    def test_nested_currency_and_missing_currency_are_distinct(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":1,"amount":{"value":"10","currency":"USD"},
                     "buyout_price":{"value":"10","currency_code":"USD"}},
                    {"offer_id":"y","sku":8,"quantity":1,"amount":"5","buyout_price":"5"}]
        original_deadline=r.wire.deadline
        rows=list(seller_ozon_views._buyouts(r))
        self.assertEqual(rows[0]["currency"],"USD")
        self.assertEqual(rows[0]["buyout_price_currency"],"USD")
        self.assertIsNone(rows[1]["currency"])
        self.assertEqual(r.meta["company_currency"],"RUB")
        self.assertEqual(r.meta["company_currency_status"],"provided_unverified_for_buyout")
        self.assertIn("не подтверждает",r.meta["currency_note"])
        self.assertEqual(r.wire.deadline,original_deadline)
        self.assertTrue(any(call[0]=="POST" and call[2]["body"]=={} for call in r.calls))

    def test_unknown_currency_is_not_added_to_period_amount(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":2,"amount":"15"}]
        report=seller_views.render(r,lambda _:seller_ozon_views._buyouts(r))
        self.assertTrue(report["complete"])
        self.assertEqual(report["company_currency"],"RUB")
        self.assertIsNone(report["items"][0]["currency"])
        self.assertNotIn("amount",report["period_totals"][0])
        self.assertEqual(report["monetary_coverage"],"currency_unconfirmed")

    def test_unavailable_company_currency_does_not_discard_buyout_rows(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":1,"amount":"5"}]
        original_deadline=r.wire.deadline
        r.wire.failure=seller_views.SourceIssue(status="provider_access_denied")
        rows=list(seller_ozon_views._buyouts(r))
        self.assertEqual(rows[0]["amount"],"5")
        self.assertIsNone(rows[0]["currency"])
        self.assertEqual(r.meta["company_currency_status"],"unavailable")
        self.assertEqual(r.wire.deadline,original_deadline)

    def test_short_deadline_skips_optional_currency_lookup(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":1,"amount":"5"}]
        r.deadline=time.monotonic()+1
        rows=list(seller_ozon_views._buyouts(r))
        self.assertIsNone(rows[0]["currency"])
        self.assertEqual(r.meta["company_currency_status"],"unavailable")
        self.assertFalse(any(call[0]=="POST" for call in r.calls))

    def test_inconsistent_currency_is_not_guessed(self):
        r=Reader()
        r.products=[{"offer_id":"x","sku":7,"quantity":1,"amount":{"value":5,"currency":"USD"},
                     "currency_code":"RUB"}]
        with self.assertRaisesRegex(ValueError,"Conflicting buyout currency"):
            list(seller_ozon_views._buyouts(r))

    def test_currency_must_not_contain_a_credential(self):
        r=Reader()
        r.wire.creds={"api_key":"RUB"}
        r.products=[{"offer_id":"x","sku":7,"quantity":1,"amount":5,"currency_code":"RUB"}]
        with self.assertRaisesRegex(ValueError,"Unsafe buyout currency"):
            list(seller_ozon_views._buyouts(r))


if __name__=="__main__":
    unittest.main()
