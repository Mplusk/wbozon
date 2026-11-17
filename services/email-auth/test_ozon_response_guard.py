"""Synthetic tests for Ozon provider-key collisions in output."""
import io
import json
import unittest
from unittest.mock import patch

import portal


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class OzonResponseGuardTests(unittest.TestCase):
    def read(self, operation, args, payload, key):
        class Opener:
            def open(self, request, timeout):
                if request.full_url.endswith('/seller/info'):
                    return Response(b'{"company":{"currency":"EUR"}}')
                return Response(json.dumps(payload).encode())
        with patch('portal.urllib.request.build_opener', return_value=Opener()):
            return portal._provider_read(operation,args,{'client_id':'123','api_key':key})

    def test_numeric_key_collision_in_analytics_fails_closed(self):
        args={'date_from':'2026-11-02','date_to':'2026-11-08'}
        payload={'result':{'data':[{'dimensions':[{'id':'2026-11-02'}],
                                    'metrics':[987654321,2]}]}}
        with self.assertRaises(ValueError) as caught:
            self.read('ozon_orders_analytics',args,payload,'987654321')
        self.assertNotIn('987654321',str(caught.exception))

    def test_posting_count_matching_numeric_key_fails_closed(self):
        args={'date_from':'2026-11-02','date_to':'2026-11-08','scheme':'FBO'}
        payload={'postings':[{'status':'delivered','products':[{'quantity':777777}]}],
                 'has_next':False}
        with self.assertRaises(ValueError) as caught:
            self.read('ozon_posting_status',args,payload,'777777')
        self.assertNotIn('777777',str(caught.exception))

    def test_key_across_compact_json_fields_fails_closed(self):
        args={'date_from':'2026-11-02','date_to':'2026-11-08','scheme':'FBO'}
        payload={'postings':[{'status':'delivered','products':[{'quantity':2}]}],
                 'has_next':False}
        key='1,"units":2'
        with self.assertRaises(ValueError) as caught:
            self.read('ozon_posting_status',args,payload,key)
        self.assertNotIn(key,str(caught.exception))

    def test_noncolliding_data_remains_unchanged(self):
        args={'date_from':'2026-11-02','date_to':'2026-11-08','scheme':'FBO'}
        payload={'postings':[{'status':'delivered','products':[{'quantity':2}]}],
                 'has_next':False}
        result=self.read('ozon_posting_status',args,payload,'not-in-response-secret')
        self.assertEqual(result['page_counts']['delivered'],{'postings':1,'units':2})
        self.assertTrue(result['complete'])


if __name__ == '__main__': unittest.main()
