"""Synthetic-only tests of the partial Ozon article summary."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import finance_gate
import ozon_article_summary as summary
import ozon_mcp
import portal
import test_external_mcp


ARGS={'offer_id':'0030','posting_numbers':['A-1','B-1'],
      'date_from':'2026-11-02','date_to':'2026-11-08'}
CREDS={'client_id':'123','api_key':'synthetic-private-key'}
ITEM={'id':7,'offer_id':'0030','name':'Модель','sku':10,
      'sources':[{'source':'FBS','sku':10},{'source':'FBO','sku':11}]}


def accrual(sku, amount, currency='RUB', day='2026-11-02', type_id=1):
    return {'sku':sku,'type_id':type_id,'accrual_date':day,'quantity':1,
            'accrued':{'amount':amount,'currency':currency},'seller_price':None}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*_): self.close()


class FakeOpener:
    def __init__(self,forward=None,reverse=None,postings=None,fail_at=None):
        self.forward={'items':[ITEM]} if forward is None else forward
        self.reverse={'items':[ITEM]} if reverse is None else reverse
        self.postings={'posting_accruals':[]} if postings is None else postings
        self.fail_at=fail_at
        self.calls=[]
    def open(self, request, timeout):
        body=json.loads(request.data)
        self.calls.append((request.full_url,body,timeout))
        if self.fail_at == len(self.calls):
            raise urllib.error.HTTPError('',429,'private',{'Retry-After':'60'},io.BytesIO())
        if request.full_url.endswith('/v3/product/info/list'):
            data=self.forward if 'offer_id' in body else self.reverse
        elif request.full_url.endswith('/v1/finance/accrual/postings'):
            data=self.postings
        else:
            raise AssertionError('Unexpected endpoint')
        return Response(json.dumps(data,ensure_ascii=False).encode('utf-8'))


class OzonArticleSummaryTests(unittest.TestCase):
    def execute(self, opener, args=None):
        with patch('portal.urllib.request.build_opener',return_value=opener):
            return portal._provider_read('ozon_article_summary',args or ARGS,CREDS)

    def test_multi_sku_article_has_only_observed_partial_accruals(self):
        same=accrual(10,'10.10')
        data={'posting_accruals':[{'posting_number':'A-1','accruals':[
            same,same.copy(),accrual(11,'2.20'),accrual(11,'3.00','EUR'),
            accrual(None,'-8.00'),accrual(99,'-9.00'),
            accrual(10,'999.00',day='2026-11-09')]}]}
        opener=FakeOpener(postings=data)
        result=self.execute(opener)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['identity']['offer_id'],'0030')
        self.assertEqual(result['identity']['name'],'Модель')
        self.assertEqual(result['identity']['sku_candidates'],['10','11'])
        self.assertEqual(result['identity']['current_sku_coverage'],'not_verified')
        self.assertEqual(result['identity']['historical_sku_coverage'],'not_verified')
        self.assertEqual(result['period']['basis'],'accrual_date')
        self.assertFalse(result['period']['order_date_used'])
        self.assertFalse(result['period']['realization_date_used'])
        self.assertFalse(result['selection']['complete_period'])
        self.assertEqual(result['selection']['unreturned_posting_numbers'],['B-1'])
        self.assertTrue(all(value is None for value in result['unknown_metrics'].values()))
        groups={(group['sku'],group['currency']):group for group in result['observations']['breakdown']}
        self.assertEqual(groups[('10','RUB')]['row_count'],2)
        self.assertEqual(groups[('10','RUB')]['observed_row_sum'],'20.20')
        self.assertEqual(groups[('11','RUB')]['observed_row_sum'],'2.20')
        self.assertEqual(groups[('11','EUR')]['observed_row_sum'],'3.00')
        self.assertTrue(all(group['financial_total'] is None for group in groups.values()))
        self.assertEqual(result['observations']['unattributed_rows_excluded'],1)
        self.assertEqual(result['observations']['other_sku_rows_excluded'],1)
        self.assertEqual(result['observations']['same_posting_equal_projection_groups'],
                         [{'posting_number':'A-1','row_indices':[0,1],'sku':'10'}])
        self.assertEqual([call[1] for call in opener.calls],
                         [{'offer_id':['0030']},{'sku':['10','11']},{'posting_numbers':['A-1','B-1']}])
        self.assertTrue(all(0 < call[2] <= 15 for call in opener.calls))
        self.assertNotIn(CREDS['api_key'],json.dumps(result,ensure_ascii=False))

    def test_no_matches_or_missing_fields_stop_before_finance(self):
        cases=[({'items':[]},'product_not_found'),
               ({'items':[{**ITEM,'sources':None}]},'identity_incomplete'),
               ({'items':[{**ITEM,'name':None}]},'identity_incomplete'),
               ({'items':[ITEM,{**ITEM,'id':8}]},'identity_ambiguous')]
        for response,reason in cases:
            with self.subTest(reason=reason):
                opener=FakeOpener(forward=response)
                result=self.execute(opener)
                self.assertEqual((result['status'],result['reason']),('blocked',reason))
                self.assertIsNone(result['identity'])
                self.assertIsNone(result['observations'])
                self.assertEqual(len(opener.calls),1)

    def test_stock_only_sku_is_rechecked_and_included_in_financial_filter(self):
        item={**ITEM,'stocks':{'stocks':[{'source':'fbo','sku':12}]}}
        opener=FakeOpener(forward={'items':[item]},reverse={'items':[item]})
        result=self.execute(opener)
        self.assertEqual(result['identity']['sku_candidates'],['10','11','12'])
        self.assertEqual(result['identity']['stock_sources'],[{'source':'fbo','sku':'12'}])
        self.assertEqual(opener.calls[1][1],{'sku':['10','11','12']})
        self.assertEqual(result['status'],'partial')

    def test_stock_source_changes_block_identity_before_finance(self):
        forward={**ITEM,'stocks':{'stocks':[{'source':'fbo','sku':11}]}}
        reverse={**ITEM,'stocks':{'stocks':[{'source':'fbs','sku':11}]}}
        opener=FakeOpener(forward={'items':[forward]},reverse={'items':[reverse]})
        result=self.execute(opener)
        self.assertEqual(result['reason'],'identity_changed_during_lookup')
        self.assertEqual(len(opener.calls),2)

    def test_reverse_conflict_or_changed_mapping_stops_before_finance(self):
        cases=[({'items':[ITEM,{**ITEM,'id':8}]},'reverse_identity_ambiguous'),
               ({'items':[{**ITEM,'name':'Другое'}]},'identity_changed_during_lookup'),
               ({'items':[{**ITEM,'sku':11}]},'identity_changed_during_lookup'),
               ({'items':[{**ITEM,'sources':ITEM['sources']+[{'source':'SDS','sku':12}]}]},'identity_changed_during_lookup'),
               ({'items':[{**ITEM,'sources':[{'source':'FBS','sku':10}]}]},'reverse_identity_ambiguous')]
        for response,reason in cases:
            with self.subTest(reason=reason):
                opener=FakeOpener(reverse=response)
                result=self.execute(opener)
                self.assertEqual((result['status'],result['reason']),('blocked',reason))
                self.assertIsNone(result['observations'])
                self.assertEqual(len(opener.calls),2)

    def test_empty_response_is_not_a_confirmed_zero_or_full_period(self):
        opener=FakeOpener(postings={'posting_accruals':[{'posting_number':'A-1','accruals':[]}]})
        result=self.execute(opener)
        self.assertEqual(result['observations']['breakdown'],[])
        self.assertEqual(result['selection']['unreturned_posting_numbers'],['B-1'])
        self.assertFalse(result['selection']['complete_period'])
        self.assertTrue(all(value is None for value in result['unknown_metrics'].values()))

    def test_assembled_output_cannot_reproduce_a_synthetic_api_key(self):
        creds={'client_id':'123','api_key':'false,"returned_posting_numbers"'}
        opener=FakeOpener()
        with tempfile.TemporaryDirectory() as tmp, \
             patch('portal.urllib.request.build_opener',return_value=opener):
            root=os.path.join(tmp,'gate')
            with self.assertRaises(finance_gate.ProviderResponseInvalid):
                finance_gate.run(root,creds['client_id'],
                    lambda:portal._provider_read('ozon_article_summary',ARGS,creds),
                    (creds['api_key'],))
            self.assertEqual(finance_gate.run(root,creds['client_id'],lambda:'next'),'next')

    def test_duplicate_rows_of_another_sku_are_not_attributed_to_article(self):
        other=accrual(99,'7.00')
        opener=FakeOpener(postings={'posting_accruals':[{'posting_number':'A-1',
            'accruals':[accrual(10,'1.00'),other,other.copy()]}]})
        result=self.execute(opener)
        observations=result['observations']
        self.assertEqual(observations['other_sku_rows_excluded'],2)
        self.assertEqual(observations['same_posting_equal_projection_groups'],[])
        self.assertEqual(observations['row_indices_scope'],'date_filtered_rows_before_sku_filter')

    def test_invalid_input_rejected_before_provider_call(self):
        cases=[{}, {**ARGS,'offer_id':''}, {**ARGS,'date_to':'2026-02-30'},
               {**ARGS,'date_from':'2026-11-09'}, {**ARGS,'posting_numbers':[]},
               {**ARGS,'posting_numbers':['A','A']}, {**ARGS,'url':'https://example.invalid'}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ozon_mcp.validate_args('ozon_article_summary',args)

    def test_finance_gate_retains_429_and_prevents_second_provider_call(self):
        for failure in (1,2,3):
            opener=FakeOpener(fail_at=failure)
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root=os.path.join(tmp,'gate')
                with patch('portal.urllib.request.build_opener',return_value=opener):
                    with self.assertRaises(urllib.error.HTTPError):
                        finance_gate.run(root,CREDS['client_id'],
                            lambda:portal._provider_read('ozon_article_summary',ARGS,CREDS),
                            (CREDS['api_key'],))
                    self.assertEqual(len(opener.calls),failure)
                    with self.assertRaises(finance_gate.GateError) as blocked:
                        finance_gate.run(root,CREDS['client_id'],
                            lambda:portal._provider_read('ozon_article_summary',ARGS,CREDS),
                            (CREDS['api_key'],))
                    self.assertEqual(blocked.exception.reason,'cooldown')
                    self.assertEqual(len(opener.calls),failure)

    def test_composite_runs_through_real_worker_with_fake_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=os.path.join(tmp,'worker.py')
            marker=os.path.join(tmp,'calls.json')
            script=('import io,json,sys\nsys.path.insert(0,'+repr(os.path.dirname(portal.__file__))+')\n'
                    'import portal\n'
                    'item='+repr(ITEM)+'\n'
                    'calls=[]\n'
                    'class Response(io.BytesIO):\n'
                    ' def __enter__(self): return self\n'
                    ' def __exit__(self,*a): self.close()\n'
                    'class Opener:\n'
                    ' def open(self,req,timeout):\n'
                    '  data=json.loads(req.data); calls.append(data)\n'
                    '  payload={"items":[item]} if req.full_url.endswith("/v3/product/info/list") else {"posting_accruals":[]}\n'
                    '  return Response(json.dumps(payload,ensure_ascii=False).encode())\n'
                    'portal.urllib.request.build_opener=lambda *a: Opener()\n'
                    'portal._provider_worker()\n'
                    'open('+repr(marker)+',"w",encoding="utf-8").write(json.dumps(calls))\n')
            with open(worker,'w',encoding='utf-8') as file: file.write(script)
            with patch('portal.PROVIDER_WORKER',worker):
                result=portal.provider_read('ozon_article_summary',ARGS,CREDS,
                                            finance_gate_dir=os.path.join(tmp,'gate'))
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['identity']['sku_candidates'],['10','11'])
            self.assertEqual(result['selection']['unreturned_posting_numbers'],['A-1','B-1'])
            with open(marker,encoding='utf-8') as file: calls=json.load(file)
            self.assertEqual(calls,[{'offer_id':['0030']},{'sku':['10','11']},
                                    {'posting_numbers':['A-1','B-1']}])
            self.assertEqual(finance_gate.run(os.path.join(tmp,'gate'),CREDS['client_id'],lambda:'next'),'next')

    def test_worker_deadline_includes_slow_gate_setup_before_any_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=os.path.join(tmp,'worker.py')
            marker=os.path.join(tmp,'network-called')
            script=('import sys,time\nsys.path.insert(0,'+repr(os.path.dirname(portal.__file__))+')\n'
                    'import portal,finance_gate\n'
                    'original=finance_gate._root\n'
                    'def slow(path):\n time.sleep(0.8)\n return original(path)\n'
                    'finance_gate._root=slow\n'
                    'class Opener:\n'
                    ' def open(self,req,timeout):\n'
                    '  open('+repr(marker)+',"w").write("called")\n'
                    '  raise AssertionError("network started")\n'
                    'portal.urllib.request.build_opener=lambda *a: Opener()\n'
                    'portal._provider_worker()\n')
            with open(worker,'w',encoding='utf-8') as file: file.write(script)
            with patch('portal.PROVIDER_WORKER',worker), patch('portal.PROVIDER_DEADLINE_SECONDS',2.5):
                with self.assertRaises(TimeoutError):
                    portal.provider_read('ozon_article_summary',ARGS,CREDS,
                                         finance_gate_dir=os.path.join(tmp,'gate'))
            self.assertFalse(os.path.exists(marker))
            self.assertEqual(finance_gate.run(os.path.join(tmp,'gate'),CREDS['client_id'],lambda:'next'),'next')

    def test_expired_internal_budget_does_not_start_network_and_releases_gate(self):
        opener=FakeOpener()
        with tempfile.TemporaryDirectory() as tmp:
            root=os.path.join(tmp,'gate')
            with patch('portal.urllib.request.build_opener',return_value=opener), \
                 patch('portal.time.monotonic',side_effect=[1.0,20.0]):
                with self.assertRaises(finance_gate.LocalPreparationTimeout):
                    finance_gate.run(root,CREDS['client_id'],
                        lambda:portal._provider_read('ozon_article_summary',ARGS,CREDS),
                        (CREDS['api_key'],))
            self.assertEqual(opener.calls,[])
            self.assertEqual(finance_gate.run(root,CREDS['client_id'],lambda:'next'),'next')

    def test_deadline_is_rechecked_after_local_tls_preparation(self):
        opener=FakeOpener()
        clock={'at':1.0}
        def slow_tls():
            clock['at']=2.1
            return object()
        with patch('portal.time.monotonic',side_effect=lambda:clock['at']), \
             patch('portal.ssl.create_default_context',side_effect=slow_tls), \
             patch('portal.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(finance_gate.LocalPreparationTimeout):
                portal._read_article_summary(ARGS,CREDS,deadline=2.0)
        self.assertEqual(opener.calls,[])

    def test_tool_is_discoverable_and_validated_in_mcp(self):
        client=test_external_mcp.ExternalMCPTests()
        client.setUp()
        tools=client.request(client.rpc('tools/list'))['body']['result']['tools']
        description=next(tool['description'] for tool in tools if tool['name']=='ozon_article_summary')
        self.assertIn('Частичная',description)
        self.assertIn('ozon_accrual_types',description)
        result=client.request(client.rpc('tools/call',{'name':'ozon_article_summary','arguments':ARGS}))
        self.assertNotIn('isError',result['body']['result'])
        self.assertEqual(client.calls[-1][1:],('ozon_article_summary',ARGS))


if __name__ == '__main__': unittest.main()
