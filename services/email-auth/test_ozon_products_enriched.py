"""Product-page identity enrichment through a fake Ozon transport."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import ozon_mcp
import portal

CREDS={'client_id':'777','api_key':'synthetic-private-key'}
PAGE={'result':{'items':[{'product_id':7,'offer_id':'0030'},
                          {'product_id':8,'offer_id':'30'}],'last_id':'next'}}
CARD={'id':7,'offer_id':'0030','name':'First', 'sku':10,
      'sources':[{'source':'FBO','sku':11}],
      'stocks':{'stocks':[{'source':'fbs','sku':12}]}}
OTHER={'id':8,'offer_id':'30','name':'Second','sku':13,
       'sources':[{'source':'FBO','sku':13}]}


class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class Opener:
    def __init__(self,page=None,cards=None,fail=False):
        self.page=PAGE if page is None else page
        self.cards={'items':[CARD,OTHER]} if cards is None else cards
        self.fail=fail;self.calls=[]
    def open(self,req,timeout):
        body=json.loads(req.data);self.calls.append((req.full_url,body))
        if req.full_url.endswith('/v3/product/list'):
            payload=self.page
        elif req.full_url.endswith('/v3/product/info/list'):
            if self.fail:raise urllib.error.HTTPError('',429,'private',{},None)
            payload=self.cards
        else:raise AssertionError('Unexpected endpoint')
        return Response(json.dumps(payload,ensure_ascii=False).encode())


class DeniedInfoOpener(Opener):
    def open(self,req,timeout):
        if req.full_url.endswith('/v3/product/info/list'):
            raise urllib.error.HTTPError('',403,'private',{},None)
        return super().open(req,timeout)


class OzonProductsEnrichedTests(unittest.TestCase):
    def execute(self,opener,args=None):
        with patch('portal.urllib.request.build_opener',return_value=opener):
            return portal._provider_read('ozon_products',args or {'limit':2},CREDS)

    def test_page_enriches_all_observed_skus_and_preserves_exact_offers(self):
        opener=Opener();result=self.execute(opener)
        first,second=result['items']
        self.assertEqual(first['offer_id'],'0030')
        self.assertEqual(first['name'],'First')
        self.assertEqual(first['sku_candidates'],['10','11','12'])
        self.assertEqual(first['identity_status'],'observed')
        self.assertEqual(second['offer_id'],'30')
        self.assertEqual(second['sku_candidates'],['13'])
        self.assertEqual(result['identity_enrichment']['items_enriched'],2)
        self.assertEqual(result['identity_enrichment']['sku_coverage'],'not_verified')
        self.assertEqual(result['identity']['scope'],'list_response_only')
        self.assertEqual(result['identity_enrichment']['items_with_name'],2)
        self.assertEqual([body for _,body in opener.calls],[
            {'filter':{'visibility':'ALL'},'limit':2,'last_id':''},
            {'product_id':['7','8']}])
        self.assertNotIn(CREDS['api_key'],json.dumps(result))

    def test_offer_mismatch_and_shared_sku_do_not_attach_identity(self):
        for cards in ({'items':[{**CARD,'offer_id':'30'},OTHER]},
                      {'items':[CARD,{**OTHER,'sku':10,'sources':[{'source':'FBO','sku':10}]}]}):
            with self.subTest(cards=cards):
                result=self.execute(Opener(cards=cards))
                self.assertNotIn('name',result['items'][0])
                self.assertNotIn('sku_candidates',result['items'][0])

    def test_conflict_with_sku_in_page_blocks_attachment(self):
        page={'result':{'items':[{**PAGE['result']['items'][0],'sku':99}],'last_id':''}}
        result=self.execute(Opener(page=page,cards={'items':[CARD]}),{'limit':1})
        self.assertEqual(result['items'][0]['identity_status'],'sku_mismatch')
        self.assertNotIn('name',result['items'][0])

    def test_repeated_product_id_is_not_silently_associated(self):
        page={'result':{'items':[{'product_id':7,'offer_id':'0030'},
                                 {'product_id':7,'offer_id':'0030'}],'last_id':''}}
        opener=Opener(page=page);result=self.execute(opener)
        self.assertEqual(len(opener.calls),1)
        self.assertEqual([r['identity_status'] for r in result['items']],['unavailable','unavailable'])

    def test_missing_product_info_is_explicit(self):
        result=self.execute(Opener(cards={'items':[CARD]}))
        self.assertEqual(result['items'][1]['identity_status'],'unavailable')
        self.assertNotIn('sku_candidates',result['items'][1])

    def test_missing_name_is_not_reported_as_an_observed_name(self):
        page={'result':{'items':[{'product_id':7,'offer_id':'0030'}],'last_id':''}}
        result=self.execute(Opener(page=page,cards={'items':[{**CARD,'name':None}]}),{'limit':1})
        self.assertFalse(result['identity']['name_in_projection'])
        self.assertEqual(result['identity_enrichment']['items_with_name'],0)
        self.assertEqual(result['items'][0]['identity_status'],'incomplete')

    def test_card_sku_cannot_conflict_with_page_sku_for_missing_card(self):
        page={'result':{'items':[{'product_id':7,'offer_id':'0030'},
                                 {'product_id':8,'offer_id':'30','sku':'10'}],'last_id':''}}
        result=self.execute(Opener(page=page,cards={'items':[CARD]}))
        self.assertEqual([item['identity_status'] for item in result['items']],['sku_conflict','sku_conflict'])
        self.assertEqual(result['identity_enrichment']['conflicting_skus'],['10'])
        self.assertNotIn('sku_candidates',result['items'][0])

    def test_incomplete_sku_owner_prevents_attribution(self):
        page={'result':{'items':[{'product_id':7,'offer_id':'0030'},
                                 {'product_id':None,'offer_id':None,'sku':10}],'last_id':''}}
        result=self.execute(Opener(page=page,cards={'items':[CARD]}))
        self.assertEqual(result['items'][0]['identity_status'],'sku_owner_incomplete')
        self.assertEqual(result['identity_enrichment']['sku_owners_incomplete'],['10'])
        self.assertNotIn('sku_candidates',result['items'][0])

    def test_enrichment_can_be_disabled_and_failure_is_not_a_success(self):
        opener=Opener();result=self.execute(opener,{'limit':2,'include_identity':False})
        self.assertEqual(len(opener.calls),1)
        self.assertNotIn('identity_enrichment',result)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.execute(Opener(fail=True))
        self.assertEqual(error.exception.code,429)
        with self.assertRaises(ValueError):
            ozon_mcp.validate_args('ozon_products',{'include_identity':'true'})

    def test_denied_card_lookup_preserves_page_with_explicit_error(self):
        result=self.execute(DeniedInfoOpener())
        self.assertEqual(len(result['items']),2)
        self.assertEqual(result['identity_enrichment']['error'],'provider_access_denied')
        self.assertTrue(all(item['identity_status']=='unavailable' for item in result['items']))
        self.assertTrue(all('sku_candidates' not in item for item in result['items']))


if __name__=='__main__':unittest.main()
