"""Synthetic tests for Ozon monthly realization and daily accrual pages."""
import json
import unittest
from decimal import Decimal

import ozon_mcp
import ozon_details
import portal


CREDS = {'client_id':'123', 'api_key':'synthetic-private-key'}
DAY_ARGS = {'date':'2026-11-15', 'skus':['10','11']}
MONTH_ARGS = {'offer_id':'0030', 'year':2026, 'month':10}


def money(value, currency='RUB'):
    return {'amount':value, 'currency':currency}


def daily_payload():
    return {'last_id':'next', 'accruals':[
        {'accrual_id':100, 'date':'2026-11-15', 'unit_number':'A-1', 'accrued_category':'POSTING',
         'posting':{'products':[{'sku':10,'quantity':1,
             'delivery':{'total_accrued':money('-4.00'),
                         'services':[{'type_id':32,'accrued':money('-2.00')},
                                     {'type_id':28,'accrued':money('-2.00')}]},
             'commission':{'sale_commission':money('-3.00'),'commission':money('-3.00')}}]}},
        {'accrual_id':101, 'date':'2026-11-15', 'unit_number':'ignore', 'accrued_category':'ITEM',
         'item_fees':{'fees':[{'sku':11,'quantity':1,'fees':[{'type_id':9,'accrued':money('-1.00')}]},
                              {'sku':99,'fees':[]}]}},
        {'accrual_id':102, 'date':'2026-11-15', 'unit_number':'not-returned', 'accrued_category':'NON_ITEM'}]}


def monthly_payload():
    return {'header':{'start_date':'2026-10-01T00:00:00Z','stop_date':'2026-10-31T23:59:59Z',
                      'currency_sys_name':'RUB','payer_inn':'private-payer'},
            'rows':[{'row_number':1,'item':{'offer_id':'0030','name':'Пример','sku':10,'barcode':'private-barcode'},
                     'order':{'posting_number':'A-1','created_date':'2026-09-30T09:00:00Z','customer':'private-customer'},
                     'delivery_commission':{'quantity':2,'amount':Decimal('100.25'),'commission':Decimal('-5.10')},
                     'return_commission':None},
                    {'row_number':2,'item':{'offer_id':'0030','name':'Пример','sku':11},
                     'order':{'posting_number':'B-1','created_date':'2026-10-01'},
                     'delivery_commission':None,'return_commission':{'quantity':-1,'amount':Decimal('-50.00')}},
                    {'row_number':3,'item':{'offer_id':'other','name':'Other','sku':99},
                     'order':{'posting_number':'C-1'},'delivery_commission':{'quantity':1}}]}


class OzonFinanceReportTests(unittest.TestCase):
    def test_daily_page_keeps_item_and_posting_fees_separate(self):
        req=ozon_mcp.build_request('ozon_accrual_day',DAY_ARGS,CREDS)
        self.assertEqual(req.full_url,'https://api-seller.ozon.ru/v1/finance/accrual/by-day')
        self.assertEqual(json.loads(req.data),{'date':'2026-11-15','last_id':''})
        result=ozon_mcp.project_response('ozon_accrual_day',{**DAY_ARGS,'view':'details'},daily_payload(),CREDS)
        self.assertEqual(result['period_basis'],'accrual_date')
        self.assertEqual(result['next_cursor'],'next')
        self.assertEqual(result['page_accrual_ids'],['100','101','102'])
        self.assertEqual(len(result['matching_rows']),2)
        self.assertEqual(result['matching_rows'][0]['delivery_total_accrued']['amount'],'-4.00')
        self.assertEqual(len(result['matching_rows'][0]['delivery_services']),2)
        self.assertEqual(result['matching_rows'][1]['fees'][0]['type_id'],9)
        self.assertEqual(result['other_sku_products_excluded'],1)
        self.assertEqual(result['unallocated_entries_excluded'],1)
        self.assertEqual(result['period_completeness'],'not_verified')
        self.assertNotIn('not-returned',json.dumps(result))

    def test_daily_summary_keeps_types_currencies_and_commission_fields_distinct(self):
        data=daily_payload()
        data['accruals'][1]['item_fees']['fees'][0]['fees'].append({'type_id':9,'accrued':money('-3.00','EUR')})
        result=ozon_mcp.project_response('ozon_accrual_day',DAY_ARGS,data,CREDS)
        groups={(g['sku'],g['component'],g['type_id'],g['currency']):g for g in result['breakdown']}
        self.assertEqual(groups[('10','delivery_service',32,'RUB')]['observed_row_sum'],'-2.00')
        self.assertEqual(groups[('11','item_fee',9,'EUR')]['observed_row_sum'],'-3.00')
        self.assertEqual(groups[('10','commission.commission',None,'RUB')]['observed_row_sum'],'-3.00')
        self.assertEqual(groups[('10','commission.sale_commission',None,'RUB')]['observed_row_sum'],'-3.00')
        self.assertTrue(all(g['financial_total'] is None for g in groups.values()))
        self.assertIsNone(result['financial_total'])
        self.assertNotIn('matching_rows',result)
        self.assertIn('sale_amount',result['not_in_summary'])
        self.assertIn('details',result['summary_note'])

    def test_daily_summary_exposes_missing_containers_for_the_same_sku(self):
        data=daily_payload()
        missing={'accrual_id':103,'date':'2026-11-15','unit_number':'B-1','accrued_category':'POSTING',
                 'posting':{'products':[{'sku':10,'quantity':1,'delivery':None,'commission':None}]}}
        data['accruals'].append(missing)
        result=ozon_mcp.project_response('ozon_accrual_day',DAY_ARGS,data,CREDS)
        coverage={c['sku']:c for c in result['coverage_by_sku']}
        self.assertEqual(coverage['10']['matching_posting_products'],2)
        self.assertEqual(coverage['10']['missing_delivery_services'],1)
        self.assertEqual(coverage['10']['missing_commission'],1)
        self.assertEqual(coverage['10']['completeness'],'not_verified')
        self.assertTrue(all(g['completeness']=='not_verified' for g in result['breakdown']))

    def test_daily_summary_distinguishes_missing_amount_and_missing_currency(self):
        data=daily_payload()
        fees=data['accruals'][1]['item_fees']['fees'][0]['fees']
        fees.append({'type_id':9,'accrued':{'amount':'-7.00','currency':None}})
        fees.append({'type_id':9,'accrued':{'amount':None,'currency':None}})
        result=ozon_mcp.project_response('ozon_accrual_day',DAY_ARGS,data,CREDS)
        group=next(g for g in result['breakdown'] if g['sku']=='11' and g['currency'] is None)
        self.assertEqual(group['missing_amounts'],1)
        self.assertEqual(group['missing_currencies'],2)
        self.assertIsNone(group['observed_row_sum'])
        self.assertEqual(result['row_identity'],'not_verified')

    def test_daily_page_refuses_bad_cursor_duplicate_ids_and_mismatched_date(self):
        data=daily_payload()
        for broken in ({**data,'last_id':'synthetic-private-key'},
                       {**data,'accruals':data['accruals']+[data['accruals'][0]]},
                       {**data,'accruals':[{**data['accruals'][0],'date':'2026-11-14'}]}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                ozon_mcp.project_response('ozon_accrual_day',DAY_ARGS,broken,CREDS)
        with self.assertRaises(ValueError):
            ozon_mcp.project_response('ozon_accrual_day',{**DAY_ARGS,'last_id':'next'},data,CREDS)

    def test_daily_filter_keeps_only_exact_posting_and_reports_unattributed_rows(self):
        data=daily_payload()
        other={**data['accruals'][0], 'accrual_id':103, 'unit_number':'A-10'}
        data['accruals'].append(other)
        args={**DAY_ARGS,'posting_number':'A-1','view':'details'}
        request=ozon_mcp.build_request('ozon_accrual_day',args,CREDS)
        self.assertEqual(json.loads(request.data),{'date':'2026-11-15','last_id':''})
        result=ozon_mcp.project_response('ozon_accrual_day',args,data,CREDS)
        self.assertEqual([r['posting_number'] for r in result['matching_rows']],['A-1'])
        self.assertEqual(result['other_posting_entries_excluded'],1)
        self.assertEqual(result['non_posting_category_entries_excluded'],2)
        self.assertEqual(result['posting_filter'],'A-1')
        self.assertEqual(result['period_completeness'],'not_verified')
        self.assertIn('не полный',result['posting_filter_note'])

    def test_daily_filter_rejects_secret_reflection(self):
        with self.assertRaises(ValueError):
            portal.finance_request_preflight('ozon_accrual_day',
                {**DAY_ARGS,'posting_number':CREDS['api_key']},CREDS)
        with self.assertRaises(ValueError):
            portal.finance_request_preflight('ozon_accrual_day',
                {**DAY_ARGS,'last_id':CREDS['api_key']},CREDS)
        with self.assertRaises(ValueError):
            ozon_mcp.project_response('ozon_accrual_day',
                {**DAY_ARGS,'posting_number':CREDS['api_key']},daily_payload(),CREDS)

    def test_monthly_report_uses_month_and_keeps_return_sign(self):
        req=ozon_mcp.build_request('ozon_realization_article_month',MONTH_ARGS,CREDS)
        self.assertEqual(req.full_url,'https://api-seller.ozon.ru/v1/finance/realization/posting')
        self.assertEqual(json.loads(req.data),{'year':2026,'month':10})
        result=ozon_mcp.project_response('ozon_realization_article_month',
                                         {**MONTH_ARGS,'view':'details'},monthly_payload(),CREDS)
        self.assertEqual(result['period_basis'],'realization_report_month')
        self.assertFalse(result['order_date_filter_used'])
        self.assertEqual(result['observed_delivery_units'],2)
        self.assertEqual(result['observed_return_units_signed'],-1)
        self.assertEqual([r['sku'] for r in result['selected_rows']],['10','11'])
        self.assertEqual(result['selected_rows'][0]['delivery_commission']['amount'],'100.25')
        self.assertEqual(result['selected_rows'][0]['order_created_date'],'2026-09-30T09:00:00Z')
        self.assertEqual(result['financial_completeness'],'not_verified')
        self.assertEqual(result['quantity_additivity'],'not_verified')
        text=json.dumps(result)
        self.assertNotIn('private-payer',text)
        self.assertNotIn('private-customer',text)
        self.assertNotIn('private-barcode',text)

    def test_monthly_summary_groups_each_sku_without_interpreting_returns(self):
        data=monthly_payload()
        result=ozon_mcp.project_response('ozon_realization_article_month',MONTH_ARGS,data,CREDS)
        self.assertNotIn('selected_rows',result)
        self.assertEqual([(row['sku'],row['observed_delivery_units'],row['observed_return_units_signed'])
                          for row in result['by_sku']],[('10',2,None),('11',None,-1)])
        self.assertEqual(result['by_sku'][0]['names'],['Пример'])
        self.assertEqual(result['quantity_additivity'],'not_verified')
        self.assertIn('не рассчитаны',result['summary_note'])

    def test_similar_report_rows_are_not_claimed_as_proven_unique_sales(self):
        data=monthly_payload()
        repeat={**data['rows'][0], 'row_number':4}
        result=ozon_mcp.project_response('ozon_realization_article_month',MONTH_ARGS,
                                         {**data,'rows':[data['rows'][0],repeat]},CREDS)
        self.assertEqual(result['observed_delivery_units'],4)
        self.assertEqual(result['matched_rows'],2)
        self.assertEqual(result['row_identity'],'not_verified')
        self.assertEqual(result['quantity_additivity'],'not_verified')

    def test_monthly_report_rejects_wrong_period_and_duplicate_row_numbers(self):
        data=monthly_payload()
        wrong={**data,'header':{**data['header'],'start_date':'2026-09-01'}}
        malformed={**data,'header':{**data['header'],'start_date':'2026-10-01not-a-timestamp'}}
        dup={**data,'rows':data['rows']+[data['rows'][0]]}
        for broken in (wrong,malformed,dup):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                ozon_mcp.project_response('ozon_realization_article_month',MONTH_ARGS,broken,CREDS)

    def test_known_type_names_are_categorized_and_unknown_stays_other(self):
        data={'accrual_types':[{'id':69,'name':'SaleCommission','description':'Вознаграждение за продажу'},
              {'id':32,'name':'Logistic','description':'Логистика'},
              {'id':28,'name':'LastMile','description':'Последняя миля'},
              {'id':9,'name':'ClientReturn','description':'Обработка возвратов'},
              {'id':500,'name':'FutureType','description':'Неизвестный тип'},
              {'id':501,'name':'SaleCommission','description':'Новое описание'}]}
        types=ozon_mcp.project_response('ozon_accrual_types',{},data,CREDS)['types']
        self.assertEqual([t['category'] for t in types],['commission','logistics','last_mile','returns','other','other'])

    def test_absent_delivery_or_return_rows_are_unknown_not_zero(self):
        data=monthly_payload()
        empty={**data,'rows':[]}
        result=ozon_mcp.project_response('ozon_realization_article_month',MONTH_ARGS,empty,CREDS)
        self.assertIsNone(result['observed_delivery_units'])
        self.assertIsNone(result['observed_return_units_signed'])
        one={**data,'rows':[data['rows'][0]]}
        result=ozon_mcp.project_response('ozon_realization_article_month',MONTH_ARGS,one,CREDS)
        self.assertEqual(result['observed_delivery_units'],2)
        self.assertIsNone(result['observed_return_units_signed'])


if __name__ == '__main__': unittest.main()
