"""End-to-end projections of posting categories using synthetic Ozon responses."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import finance_gate
import portal

CREDS={'client_id':'777','api_key':'synthetic-private-key'}
ARGS={'posting_numbers':['A-1'],'date_from':'2026-11-01','date_to':'2026-11-07',
      'view':'summary','skus':['10','11']}
TYPES={'accrual_types':[
    {'id':1,'name':'SaleCommission','description':'Вознаграждение за продажу'},
    {'id':2,'name':'Logistic','description':'Логистика'},
    {'id':3,'name':'LastMile','description':'Последняя миля'},
    {'id':4,'name':'ClientReturn','description':'Обработка возвратов'},
    {'id':5,'name':'FutureType','description':'Unknown'}]}


def row(kind,amount,sku=10,currency='RUB',day='2026-11-02'):
    return {'accrual_date':day,'type_id':kind,'quantity':1,'sku':sku,
            'accrued':{'amount':amount,'currency':currency},'seller_price':None}


class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class Opener:
    def __init__(self,fail_types=False):self.calls=[];self.fail_types=fail_types
    def open(self,req,timeout):
        self.calls.append(req.full_url)
        if req.full_url.endswith('/postings'):
            payload={'posting_accruals':[{'posting_number':'A-1','accruals':[
                row(1,'-1.10'),row(2,'-2.20'),row(3,'-3.30'),row(4,'-4.40'),
                row(5,'-5.50'),row(99,'-6.60'),row(2,'-1.00',sku=11),
                row(2,None),row(3,'-100.00',day='2026-11-08')]}]}
        elif req.full_url.endswith('/types'):
            if self.fail_types:raise urllib.error.HTTPError('',429,'private',{'Retry-After':'60'},None)
            payload=TYPES
        else:raise AssertionError('Unexpected endpoint')
        return Response(json.dumps(payload,ensure_ascii=False).encode())


class OzonPostingCategoriesTests(unittest.TestCase):
    def execute(self,opener,args=None):
        with patch('portal.urllib.request.build_opener',return_value=opener):
            return portal._provider_read('ozon_posting_accruals',args or ARGS,CREDS)

    def test_separate_observed_categories_keep_sku_and_unknown_types(self):
        result=self.execute(Opener())
        groups={(g['sku'],g['category'],g['currency']):g for g in result['observed_category_breakdown']}
        self.assertEqual(groups[('10','commission','RUB')]['observed_row_sum'],'-1.10')
        self.assertEqual(groups[('10','logistics','RUB')]['observed_row_sum'],'-2.20')
        self.assertTrue(groups[('10','logistics','RUB')]['incomplete'])
        self.assertEqual(groups[('11','logistics','RUB')]['observed_row_sum'],'-1.00')
        self.assertEqual(groups[('10','last_mile','RUB')]['observed_row_sum'],'-3.30')
        self.assertEqual(groups[('10','returns','RUB')]['observed_row_sum'],'-4.40')
        self.assertEqual(groups[('10','other','RUB')]['observed_row_sum'],'-12.10')
        self.assertTrue(all(g['financial_total'] is None for g in groups.values()))
        self.assertEqual(result['category_completeness'],'not_verified')
        missing=next(g for g in result['breakdown'] if g['type_id']==99)
        self.assertEqual(missing['type_classification'],'not_reported_by_provider')
        self.assertNotIn(CREDS['api_key'],json.dumps(result,ensure_ascii=False))

    def test_details_label_each_accrual_without_changing_its_amount(self):
        result=self.execute(Opener(),{'posting_numbers':['A-1'],'view':'details'})
        rows=result['posting_accruals'][0]['accruals']
        self.assertEqual(rows[0]['category'],'commission')
        self.assertEqual(rows[0]['accrued']['amount'],'-1.10')
        self.assertEqual(rows[5]['type_classification'],'not_reported_by_provider')
        self.assertNotIn('observed_category_breakdown',result)

    def test_types_can_be_disabled(self):
        opener=Opener();result=self.execute(opener,{**ARGS,'include_types':False})
        self.assertEqual(len(opener.calls),1)
        self.assertNotIn('observed_category_breakdown',result)

    def test_unattributed_other_type_makes_category_incomplete(self):
        import ozon_details
        args={'posting_numbers':['A-1'],'view':'summary','skus':['10']}
        payload={'posting_accruals':[{'posting_number':'A-1','accruals':[
            row(2,'-2.00'),row(6,'-5.00',sku=None)]}]}
        types={'accrual_types':[TYPES['accrual_types'][1],
                {'id':6,'name':'Shipment','description':'Магистраль'}]}
        projected=ozon_details.project_response('ozon_posting_accruals',args,payload,CREDS)
        projected_types=ozon_details.project_response('ozon_accrual_types',{},types,CREDS)
        result=ozon_details.label_posting_accruals(projected,projected_types)
        group=result['observed_category_breakdown'][0]
        self.assertEqual((group['category'],group['observed_row_sum']),('logistics','-2.00'))
        self.assertTrue(group['incomplete'])
        self.assertTrue(group['unattributed_rows_present'])
        self.assertEqual(group['completeness'],'not_verified')

    def test_separate_postings_are_not_combined_in_per_posting_view(self):
        import ozon_details
        args={'posting_numbers':['A-1','B-1'],'view':'summary','skus':['10']}
        payload={'posting_accruals':[{'posting_number':'A-1','accruals':[row(2,'-1.00')]},
                                     {'posting_number':'B-1','accruals':[row(2,'-2.00')]}]}
        projected=ozon_details.project_response('ozon_posting_accruals',args,payload,CREDS)
        projected_types=ozon_details.project_response('ozon_accrual_types',{},TYPES,CREDS)
        result=ozon_details.label_posting_accruals(projected,projected_types)
        by_posting={p['posting_number']:p['observed_category_breakdown'][0]['observed_row_sum']
                    for p in result['by_posting']}
        self.assertEqual(by_posting,{'A-1':'-1.00','B-1':'-2.00'})
        self.assertEqual(result['observed_category_breakdown'][0]['observed_row_sum'],'-3.00')

    def test_direct_posting_summary_does_not_expose_other_sku_duplicate_groups(self):
        import ozon_details
        args={'posting_numbers':['A-1'],'view':'summary','skus':['10']}
        other=row(2,'-2.00',sku=99)
        payload={'posting_accruals':[{'posting_number':'A-1',
                  'accruals':[row(2,'-1.00'),other,other.copy()]}]}
        result=ozon_details.project_response('ozon_posting_accruals',args,payload,CREDS)
        self.assertEqual(result['same_posting_equal_projection_groups'],[])
        self.assertEqual(result['by_posting'][0]['same_posting_equal_projection_groups'],[])

    def test_two_hundred_postings_with_five_types_have_compact_summary(self):
        import ozon_details
        names=['P-'+str(n) for n in range(200)]
        args={'posting_numbers':names,'view':'summary','skus':['10']}
        payload={'posting_accruals':[{'posting_number':name,
                  'accruals':[row(kind,'-1.00') for kind in range(1,6)]} for name in names]}
        projected=ozon_details.project_response('ozon_posting_accruals',args,payload,CREDS)
        types=ozon_details.project_response('ozon_accrual_types',{},TYPES,CREDS)
        result=ozon_details.label_posting_accruals(projected,types)
        self.assertEqual(len(result['by_posting']),200)
        self.assertLess(len(json.dumps(result,ensure_ascii=True).encode()),524288)

    def test_rate_limit_during_type_lookup_is_persisted_by_gate(self):
        opener=Opener(fail_types=True)
        with tempfile.TemporaryDirectory() as tmp, patch('portal.urllib.request.build_opener',return_value=opener):
            gate=os.path.join(tmp,'gate')
            with self.assertRaises(urllib.error.HTTPError):
                finance_gate.run(gate,CREDS['client_id'],
                    lambda:portal._provider_read('ozon_posting_accruals',ARGS,CREDS),(CREDS['api_key'],))
            self.assertEqual(len(opener.calls),2)
            with self.assertRaises(finance_gate.GateError) as error:
                finance_gate.run(gate,CREDS['client_id'],lambda:'unexpected')
            self.assertEqual(error.exception.reason,'cooldown')


if __name__=='__main__':unittest.main()
