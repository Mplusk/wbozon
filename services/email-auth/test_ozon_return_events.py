"""Offline tests for return-event identity, date boundaries and pagination."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import finance_gate
import ozon_mcp
import ozon_return_events as returns
import portal

ARGS={'offer_id':'A-01','date_from':'2026-11-01','date_to':'2026-11-07','scheme':'FBO'}
CREDS={'client_id':'123','api_key':'synthetic-secret'}


def row(identifier=1,when='2026-11-02T10:00:00Z',offer='A-01',sku=9007199254740993):
    return {'id':identifier,'type':'ClientReturn','schema':'Fbo','posting_number':'A-1',
            'product':{'offer_id':offer,'sku':sku,'name':'Model','quantity':1,'barcode':'private-barcode'},
            'logistic':{'return_date':when,'final_moment':None,'barcode':'private-barcode'},
            'visual':{'status':{'sys_name':'ReceivedBySeller','display_name':'Получен'},
                      'change_moment':'2026-11-03T12:00:00Z'},
            'place':{'address':'private-address'},'return_reason_name':'private-reason'}


class ReturnEventTests(unittest.TestCase):
    def test_request_uses_fixed_endpoint_exact_offer_and_utc_boundaries(self):
        req=returns.build_request({**ARGS,'last_id':'9007199254740993','limit':25},CREDS)
        self.assertEqual(req.full_url,'https://api-seller.ozon.ru/v1/returns/list')
        self.assertEqual(json.loads(req.data),{'filter':{'offer_id':'A-01','return_schema':'FBO',
            'logistic_return_date':{'time_from':'2026-11-01T00:00:00Z',
                                    'time_to':'2026-11-08T00:00:00Z'}},
            'limit':25,'last_id':9007199254740993})

    def test_exact_match_utc_and_missing_values_are_counted_separately(self):
        data={'returns':[row(1,'2026-11-01T03:00:00+03:00'),
                         row(2,'2026-11-08T00:59:59+01:00'),
                         row(3,'2026-11-01T02:59:59+03:00'),
                         row(4,'2026-11-08T00:00:00Z'),
                         row(5,None),row(6,'bad-date'),row(7,offer='A-010'),
                         {**row(8),'schema':'Fbs'}], 'has_next':False}
        result=returns.project_response({**ARGS,'view':'details'},data,CREDS)
        self.assertEqual([x['return_id'] for x in result['events']],['1','2'])
        self.assertEqual(result['events'][0]['sku'],'9007199254740993')
        self.assertEqual(result['excluded'],{'different_or_missing_offer':1,
            'different_or_missing_scheme':1,'missing_or_invalid_return_date':2,'outside_utc_period':2})
        self.assertEqual(result['period_basis'],'logistic.return_date')
        self.assertEqual(result['period_completeness'],'not_verified')
        self.assertIsNone(result['financial_return_total'])
        for private in ('private-address','private-barcode','private-reason'):
            self.assertNotIn(private,json.dumps(result,ensure_ascii=False))

    def test_summary_keeps_return_types_and_statuses_separate(self):
        cancellation={**row(2),'type':'Cancellation'}
        pending=row(3);pending['visual']={'status':{'sys_name':'Pending','display_name':'Ожидает'}}
        missing=row(4);missing['product']['quantity']=None
        result=returns.project_response(ARGS,{'returns':[row(1),cancellation,pending,missing],'has_next':False},CREDS)
        groups={(g['type'],g['status_system_name']):g for g in result['breakdown']}
        self.assertEqual(groups[('ClientReturn','ReceivedBySeller')]['event_rows'],2)
        self.assertEqual(groups[('ClientReturn','ReceivedBySeller')]['observed_quantity_row_sum'],1)
        self.assertEqual(groups[('ClientReturn','ReceivedBySeller')]['missing_quantities'],1)
        self.assertEqual(groups[('Cancellation','ReceivedBySeller')]['event_rows'],1)
        self.assertEqual(groups[('ClientReturn','Pending')]['event_rows'],1)
        self.assertIsNone(result['financial_return_total'])
        self.assertEqual(result['quantity_additivity'],'not_verified')
        self.assertTrue(all(g['quantity_completeness']=='not_verified' for g in groups.values()))

    def test_pagination_uses_raw_page_last_id_and_rejects_ambiguous_page(self):
        result=returns.project_response(ARGS,{'returns':[row(1),row(9007199254740993,offer='different')],
                                              'has_next':True},CREDS)
        self.assertEqual(result['next_last_id_candidate'],'9007199254740993')
        for data,args in [({'returns':[],'has_next':True},ARGS),
                          ({'returns':[row(1),row(1)],'has_next':False},ARGS),
                          ({'returns':[row(1)],'has_next':True},{**ARGS,'last_id':'1'}),
                          ({'returns':[row(1)],'has_next':'false'},ARGS)]:
            with self.subTest(data=data),self.assertRaises(ValueError):
                returns.project_response(args,data,CREDS)

    def test_invalid_or_unsafe_inputs_fail_before_the_network(self):
        invalid=[{**ARGS,'date_from':'2026-11-08'}, {**ARGS,'scheme':'rfbs'},
                 {**ARGS,'limit':101},{**ARGS,'last_id':'01'},{**ARGS,'view':'full'},
                 {**ARGS,'offer_id':CREDS['api_key']}]
        for args in invalid:
            with self.subTest(args=args),self.assertRaises(ValueError):
                portal.finance_request_preflight('ozon_return_events',args,CREDS)

    def test_provider_duplicate_json_keys_are_rejected(self):
        class Opener:
            def open(self,request,timeout):
                class Response(io.BytesIO):
                    def __enter__(self):return self
                    def __exit__(self,*args):self.close()
                return Response(b'{"returns":[],"has_next":false,"has_next":true}')
        with patch('portal.urllib.request.build_opener',return_value=Opener()), \
             self.assertRaises(finance_gate.ProviderResponseInvalid):
            portal._provider_read('ozon_return_events',ARGS,CREDS)

    def test_invalid_optional_status_timestamps_are_not_emitted_as_dates(self):
        for field in ('final_moment','change_moment'):
            value=row()
            (value['logistic'] if field=='final_moment' else value['visual'])[field]='bad-date'
            with self.subTest(field=field),self.assertRaises(ValueError):
                returns.project_response(ARGS,{'returns':[value],'has_next':False},CREDS)


if __name__=='__main__':unittest.main()
