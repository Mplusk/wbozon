"""Synthetic contract and security tests for Wildberries read-only operations."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import wb_mcp

class WBOperations(unittest.TestCase):
    def setUp(self): self.secret='example-token-is-private-12345'; self.creds={'api_token':self.secret}
    def read(self,op,args,payload):
        with patch('wb_mcp.urllib.request.build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
            result=wb_mcp.read(op,args,self.creds)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(opener.return_value.open.call_count,1)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],15)
            self.assertTrue(any(handler is wb_mcp.NoRedirect for handler in opener.call_args.args))
            self.assertNotIn(self.secret,json.dumps(result,ensure_ascii=False))
            return result,request
    def test_products_cursor_request_and_projection(self):
        payload={'cards':[{'nmID':123,'title':'item','description':self.secret,'phone':'private','sizes':[{'skus':['private']}]}], 'cursor':{'updatedAt':'2026-11-15T12:00:00Z','nmID':123,'total':1},'error':False}
        args={'limit':1,'updated_at':'2026-11-14T12:00:00Z','nm_id':100}
        result,request=self.read('wb_products',args,payload)
        self.assertEqual(request.full_url,'https://content-api.wildberries.ru/content/v2/get/cards/list')
        self.assertEqual(request.method,'POST')
        self.assertEqual(json.loads(request.data)['settings']['cursor'],{'limit':1,'updatedAt':args['updated_at'],'nmID':100})
        self.assertEqual(result['cursor']['nmID'],123)
        self.assertFalse(result['first_page_only'])
        self.assertEqual(result['items'][0]['description'],'[REDACTED]')
        self.assertNotIn('phone',result['items'][0]); self.assertEqual(result['items'][0]['sizes'],[{'skus':['private']}])
    def test_products_nested_fields_projected_and_redacted(self):
        payload={'cards':[{'nmID':123,'dimensions':{'length':1,'width':2,'phone':self.secret},'sizes':[{'chrtID':5,'skus':[self.secret],'other':self.secret}],'characteristics':[{'id':3,'name':'n','value':['text',self.secret],'other':self.secret}],'photos':[{'big':'https://example.invalid/'+self.secret,'other':self.secret}]}], 'cursor':{'updatedAt':'2026-11-15T12:00:00Z','nmID':123,'total':1}}
        result,_=self.read('wb_products',{'limit':1},payload)
        item=result['items'][0]
        self.assertEqual(item['dimensions'],{'length':1,'width':2})
        self.assertEqual(item['sizes'],[{'chrtID':5,'skus':['[REDACTED]']}])
        self.assertEqual(item['characteristics'],[{'id':3,'name':'n','value':['text','[REDACTED]']}])
        self.assertEqual(item['photos'],[{'big':'https://example.invalid/[REDACTED]'}])
    def test_large_integers_are_decimal_strings_without_rounding(self):
        huge=9007199254740993
        payload={'cards':[{'nmID':huge,'imtID':1,'sizes':[{'chrtID':huge,'skus':[huge]}],'characteristics':[{'id':1,'value':[huge,-huge,True,42]}]}], 'cursor':{'updatedAt':'2026-11-15T12:00:00Z','nmID':123,'total':1}}
        result,_=self.read('wb_products',{'limit':1},payload)
        item=result['items'][0]
        self.assertEqual(item['nmID'],str(huge))
        self.assertEqual(item['imtID'],1)
        self.assertEqual(item['sizes'][0]['chrtID'],str(huge))
        self.assertEqual(item['sizes'][0]['skus'],[str(huge)])
        self.assertEqual(item['characteristics'][0]['value'],[str(huge),str(-huge),True,42])
        self.assertEqual(result['cursor']['nmID'],123)
        payload['cursor']['nmID']=huge
        with self.assertRaises(ValueError):self.read('wb_products',{'limit':1},payload)
    def test_prices_offset_and_exact_product(self):
        payload={'data':{'listGoods':[{'nmID':13,'currencyIsoCode4217':'RUB','vendorCode':self.secret,'sizes':[{'price':10,'other':self.secret}]}]},'error':False}
        result,request=self.read('wb_prices',{'limit':1,'offset':20},payload)
        self.assertEqual(urlsplit(request.full_url).hostname,'discounts-prices-api.wildberries.ru')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),{'limit':['1'],'offset':['20']})
        self.assertEqual(result['offset'],20); self.assertEqual(result['next_offset'],21)
        self.assertEqual(result['items'][0]['currencyIsoCode4217'],'RUB')
        self.assertEqual(result['items'][0]['vendorCode'],'[REDACTED]')
        result,request=self.read('wb_prices',{'limit':1,'nm_id':13},payload)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['filterNmID'],['13'])
        self.assertIsNone(result['next_offset'])
    def test_commissions_filters_before_bounded_page(self):
        payload={'report':[{'subjectID':1,'subjectName':'A','kgvpMarketplace':12,'other':self.secret},{'subjectID':2,'subjectName':'AA','kgvpMarketplace':13}]}
        result,request=self.read('wb_commissions',{'search':'a','limit':1},payload)
        self.assertEqual(request.full_url,'https://common-api.wildberries.ru/api/v1/tariffs/commission')
        self.assertEqual(result['matched_rows'],2); self.assertEqual(result['returned_rows'],1);self.assertTrue(result['truncated'])
        self.assertNotIn('other',result['items'][0])
        result,_=self.read('wb_commissions',{'subject_id':2},payload)
        self.assertEqual(result['items'][0]['subjectID'],2)
    def test_box_tariff_filter_and_date(self):
        payload={'response':{'data':{'currency':'RUB','dtNextBox':'2026-12-01','warehouseList':[{'warehouseName':'A','boxDeliveryBase':'100','other':self.secret},{'warehouseName':'B'}]}}}
        result,request=self.read('wb_box_tariffs',{'date':'2026-11-15','warehouse':'a'},payload)
        self.assertEqual(urlsplit(request.full_url).hostname,'common-api.wildberries.ru')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),{'date':['2026-11-15']})
        self.assertEqual(result['currency'],'RUB');self.assertEqual(result['items'],[{'warehouseName':'A','boxDeliveryBase':'100'}])
    def test_sales_funnel_period_pagination_and_separate_metrics(self):
        args={'date_from':'2026-11-09','date_to':'2026-11-15','limit':1}
        row={'product':{'nmId':13,'vendorCode':self.secret,'title':'item','phone':self.secret},
             'statistic':{'selected':{'period':{'start':args['date_from'],'end':args['date_to']},
                'orderCount':4,'orderSum':400,'buyoutCount':2,'buyoutSum':200,'cancelCount':1,'cancelSum':100}}}
        result,request=self.read('wb_sales_summary',args,{'data':{'currency':'RUB','products':[row]}})
        self.assertEqual(request.full_url,'https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products')
        self.assertEqual(request.method,'POST')
        self.assertEqual(json.loads(request.data),{'selectedPeriod':{'start':args['date_from'],'end':args['date_to']},'skipDeletedNm':False,'limit':1,'offset':0})
        self.assertEqual(result['items'][0]['product'],{'nmId':13,'vendorCode':'[REDACTED]','title':'item'})
        self.assertEqual(result['items'][0]['metrics'],{'orderCount':4,'orderSum':400,'buyoutCount':2,'buyoutSum':200,'cancelCount':1,'cancelSum':100})
        self.assertEqual(result['page_totals'],result['items'][0]['metrics'])
        self.assertEqual(result['currency'],'RUB'); self.assertFalse(result['complete']);self.assertTrue(result['has_more']);self.assertEqual(result['next_offset'],1)
        result,_=self.read('wb_sales_summary',{**args,'offset':1},{'data':{'currency':'RUB','products':[]}})
        self.assertFalse(result['complete']);self.assertFalse(result['has_more']);self.assertIsNone(result['next_offset'])
        result,_=self.read('wb_sales_summary',{**args,'limit':2},{'data':{'currency':'RUB','products':[row]}})
        self.assertTrue(result['complete'])
        result,request=self.read('wb_sales_summary',{'date_from':args['date_from'],'date_to':args['date_to']},{'data':{'currency':'RUB','products':[row,row]}})
        self.assertEqual(json.loads(request.data)['limit'],100)
        self.assertEqual(result['page_totals']['orderSum'],800)
        self.assertEqual(result['page_totals']['buyoutSum'],400)
        self.assertEqual(result['page_totals']['cancelSum'],200)
        self.assertTrue(result['complete'])
    def test_sales_funnel_incomplete_schema_never_implies_zero(self):
        args={'date_from':'2026-11-09','date_to':'2026-11-15'}
        for payload in ({'data':{'products':[]}}, {'data':{'currency':'RUB','products':[{'product':{},'statistic':{'selected':{}}}]}}, {'data':{'currency':'RUB','products':'bad'}}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.read('wb_sales_summary',args,payload)
    def test_invalid_inputs_fail_before_network(self):
        invalid=[('wb_products',{'nm_id':1}),('wb_products',{'updated_at':'not-date','nm_id':1}),('wb_products',{'updated_at':'2026-01-01T00:00:00Z','nm_id':True}),('wb_prices',{'offset':-1}),('wb_prices',{'offset':1,'nm_id':1,'limit':1}),('wb_prices',{'limit':True}),('wb_prices',{'url':'https://example.com'}),('wb_commissions',{'search':'x'*101}),('wb_commissions',{'subject_id':True}),('wb_box_tariffs',{'date':'2026-02-30'}),('wb_box_tariffs',{'date':'2026-01-01','warehouse':'x'*101}),('wb_sales_summary',{}),('wb_sales_summary',{'date_from':'2026-02-30','date_to':'2026-03-01'}),('wb_sales_summary',{'date_from':'2026-11-15','date_to':'2026-11-09'}),('wb_sales_summary',{'date_from':'2024-01-01','date_to':'2026-01-01'}),('wb_sales_summary',{'date_from':'2026-01-01','date_to':'2026-01-02','offset':True})]
        with patch('wb_mcp.urllib.request.build_opener') as opener:
            for op,args in invalid:
                with self.subTest(op=op,args=args),self.assertRaises(ValueError):wb_mcp.read(op,args,self.creds)
            opener.assert_not_called()
    def test_errors_invalid_numbers_and_size_are_rejected(self):
        for payload in ({'error':True,'data':{'listGoods':[]}}, {'error':False,'errorText':self.secret,'data':{'listGoods':[]}}, b'{"data":{"listGoods":[]},"bad":NaN}', b'{"data":{"listGoods":[]},"bad":1e9999}', b'x'*512001):
            with self.subTest(payload=type(payload)),self.assertRaises(ValueError) as caught:self.read('wb_prices',{},payload)
            self.assertNotIn(self.secret,str(caught.exception))
    def test_outbound_redirects_disabled(self):
        self.assertIsNone(wb_mcp.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid'))
    def test_projected_response_is_bounded(self):
        item={'nmID':1,'sizes':[{'sizeID':1,'techSizeName':'ш'*256,'price':'ш'*256} for _ in range(100)]}
        payload={'data':{'listGoods':[item]*3}}
        raw=json.dumps(payload,ensure_ascii=False).encode()
        self.assertLess(len(raw),wb_mcp.MAX_ROW_BODY)
        with self.assertRaisesRegex(ValueError,'Projected response too large'):self.read('wb_prices',{'limit':3},raw)

if __name__=='__main__':unittest.main()
