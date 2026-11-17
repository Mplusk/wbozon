"""Synthetic discovery of Ozon posting numbers; no buyer data or network."""
import json
import unittest
from unittest.mock import patch

import ozon_mcp
import portal
import test_external_mcp
import test_ozon_article_summary as article_tests


CREDS={'client_id':'123','api_key':'synthetic-private-key'}
ARGS={'date_from':'2026-11-02','date_to':'2026-11-08','scheme':'FBO','limit':2}


class OzonPostingCandidateTests(unittest.TestCase):
    def test_fixed_fbo_fbs_endpoints_reuse_existing_request_contract(self):
        for scheme,path in [('FBO','/v3/posting/fbo/list'),('FBS','/v4/posting/fbs/list')]:
            args={**ARGS,'scheme':scheme,'cursor':'next'}
            request=ozon_mcp.build_request('ozon_posting_candidates',args,CREDS)
            self.assertEqual(request.full_url,'https://api-seller.ozon.ru'+path)
            body=json.loads(request.data)
            self.assertEqual(body['filter']['since'],'2026-11-02T00:00:00Z')
            self.assertEqual(body['filter']['to'],'2026-11-08T23:59:59.999999999Z')
            self.assertEqual(body['cursor'],'next')
            self.assertEqual(body['limit'],2)
            self.assertFalse(body['with']['financial_data'])
            self.assertFalse(body['with']['analytics_data'])
            self.assertFalse(body['with']['legal_info'])

    def test_page_contains_only_numbers_and_explicit_selection_limits(self):
        payload={'postings':[{'posting_number':'A-1','customer':{'name':'private'},
                              'products':[{'offer_id':'injected-offer','sku':10}]},
                             {'posting_number':'B-1','customer':{'phone':'private'}}],
                 'has_next':True,'cursor':'next-cursor'}
        result=ozon_mcp.project_response('ozon_posting_candidates',ARGS,payload,CREDS)
        self.assertEqual(result['posting_numbers'],['A-1','B-1'])
        self.assertEqual(result['next_cursor'],'next-cursor')
        self.assertEqual(result['requested_cursor'],'')
        self.assertTrue(result['provider_has_next'])
        self.assertEqual(result['selection_basis'],'provider_posting_filter_period')
        self.assertEqual(result['relationship_to_order_or_accrual_date'],'not_verified')
        self.assertEqual(result['page_or_period_completeness'],'not_verified')
        self.assertEqual(result['other_schemes'],'not_checked')
        self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('injected-offer',json.dumps(result))

    def test_missing_repeated_and_unsafe_values_fail_closed(self):
        cases=[{'postings':[{}],'has_next':False},
               {'postings':[{'posting_number':'x\nother'}],'has_next':False},
               {'postings':[{'posting_number':'synthetic-private-key'}],'has_next':False},
               {'postings':[{'posting_number':'A'},{'posting_number':'A'}],'has_next':False},
               {'postings':[{'posting_number':'A'}],'has_next':True,'cursor':''},
               {'postings':[],'has_next':True,'cursor':'next'},
               {'postings':[{'posting_number':'A'}],'has_next':True,'cursor':'same'},
               {'postings':[{'posting_number':'A'}],'has_next':'false'},
               {'postings':[{'posting_number':'A'}]*3,'has_next':False}]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ozon_mcp.project_response('ozon_posting_candidates',{**ARGS,'cursor':'same'},payload,CREDS)

    def test_final_page_does_not_claim_period_or_other_scheme_complete(self):
        result=ozon_mcp.project_response('ozon_posting_candidates',ARGS,
            {'postings':[],'has_next':False,'cursor':'irrelevant'},CREDS)
        self.assertEqual(result['posting_numbers'],[])
        self.assertFalse(result['provider_has_next'])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(result['page_or_period_completeness'],'not_verified')
        self.assertEqual(result['other_schemes'],'not_checked')

    def test_cursor_can_contain_client_id_but_not_api_key(self):
        payload={'postings':[{'posting_number':'A-1','status':'delivered','products':[]}],
                 'has_next':True,'cursor':'opaque-123-next'}
        for operation in ('ozon_posting_candidates','ozon_posting_status'):
            with self.subTest(operation=operation):
                result=ozon_mcp.project_response(operation,ARGS,payload,CREDS)
                self.assertEqual(result['next_cursor'],'opaque-123-next')
                with self.assertRaises(ValueError):
                    ozon_mcp.project_response(operation,ARGS,
                        {**payload,'cursor':'opaque-synthetic-private-key-next'},CREDS)

    def test_long_cursor_cycles_are_visible_and_must_be_stopped_by_client(self):
        first=ozon_mcp.project_response('ozon_posting_candidates',{**ARGS,'cursor':'A'},
            {'postings':[{'posting_number':'P-1'}],'has_next':True,'cursor':'B'},CREDS)
        second=ozon_mcp.project_response('ozon_posting_candidates',{**ARGS,'cursor':'B'},
            {'postings':[{'posting_number':'P-2'}],'has_next':True,'cursor':'A'},CREDS)
        self.assertEqual((first['requested_cursor'],first['next_cursor']),('A','B'))
        self.assertEqual((second['requested_cursor'],second['next_cursor']),('B','A'))
        self.assertIn('при любом повторе остановитесь',second['note'])
        self.assertEqual(second['page_or_period_completeness'],'not_verified')

    def test_candidate_page_can_feed_partial_article_summary(self):
        class Opener(article_tests.FakeOpener):
            def open(self,request,timeout):
                if request.full_url.endswith('/v3/posting/fbo/list'):
                    self.calls.append((request.full_url,json.loads(request.data),timeout))
                    payload={'postings':[{'posting_number':'A-1'},{'posting_number':'B-1'}],
                             'has_next':False,'cursor':''}
                    return article_tests.Response(json.dumps(payload).encode())
                return super().open(request,timeout)
        opener=Opener(postings={'posting_accruals':[{'posting_number':'A-1',
            'accruals':[article_tests.accrual(10,'5.00')]}]})
        with patch('portal.urllib.request.build_opener',return_value=opener):
            page=portal._provider_read('ozon_posting_candidates',ARGS,CREDS)
            result=portal._provider_read('ozon_article_summary',
                {**article_tests.ARGS,'posting_numbers':page['posting_numbers']},CREDS)
        self.assertEqual(result['status'],'partial')
        self.assertFalse(result['selection']['complete_period'])
        self.assertEqual(result['selection']['unreturned_posting_numbers'],['B-1'])
        self.assertIsNone(result['unknown_metrics']['financial_total'])
        self.assertEqual(result['observations']['breakdown'][0]['observed_row_sum'],'5.00')
        self.assertEqual(len(opener.calls),5)

    def test_mcp_advertises_and_validates_candidates(self):
        client=test_external_mcp.ExternalMCPTests()
        client.setUp()
        tools=client.request(client.rpc('tools/list'))['body']['result']['tools']
        description=next(tool['description'] for tool in tools if tool['name']=='ozon_posting_candidates')
        self.assertIn('не гарантирует полноту',description)
        self.assertIn('не более 90 дней',description)
        result=client.request(client.rpc('tools/call',{'name':'ozon_posting_candidates','arguments':ARGS}))
        self.assertNotIn('isError',result['body']['result'])
        before=len(client.calls)
        invalid=client.request(client.rpc('tools/call',{'name':'ozon_posting_candidates','arguments':{**ARGS,'scheme':'bad'}}))
        self.assertTrue(invalid['body']['result']['isError'])
        self.assertEqual(len(client.calls),before)


if __name__ == '__main__': unittest.main()
