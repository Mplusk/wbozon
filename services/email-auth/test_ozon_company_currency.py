"""Seller currency is optional context, not a claim about analytics currency."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

import ozon_mcp
import portal


ARGS={'date_from':'2026-11-02','date_to':'2026-11-08'}
CREDS={'client_id':'123','api_key':'synthetic-secret'}
ANALYTICS=b'{"result":{"data":[{"dimensions":[{"id":"2026-11-02"}],"metrics":[123.45,2]}]}}'


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class CurrencyTests(unittest.TestCase):
    def run_read(self, profile):
        requests=[]
        class Opener:
            def open(self, request, timeout):
                requests.append((request,timeout))
                if request.full_url.endswith('/v1/analytics/data'): return Response(ANALYTICS)
                if isinstance(profile, Exception): raise profile
                return Response(profile)
        with patch('portal.urllib.request.build_opener', return_value=Opener()):
            result=portal._provider_read('ozon_orders_analytics',ARGS,CREDS)
        return result,requests

    def test_success_is_seller_currency_not_analytics_currency(self):
        result,requests=self.run_read(b'{"company":{"currency":"RUB","name":"private"}}')
        self.assertEqual(result['ordered_amount'],'123.45')
        self.assertIsNone(result['currency'])
        self.assertEqual(result['company_currency'],'RUB')
        self.assertIn('not a guaranteed analytics currency',result['company_currency_source'])
        self.assertNotIn('private',json.dumps(result))
        req,timeout=requests[1]
        self.assertEqual(req.full_url,'https://api-seller.ozon.ru/v1/seller/info')
        self.assertEqual(req.data,b'{}')
        self.assertEqual(req.get_method(),'POST')
        self.assertLessEqual(timeout,3)

    def test_http_errors_timeouts_bad_payloads_and_unsafe_values_leave_analytics_intact(self):
        profiles=[urllib.error.HTTPError('',401,'synthetic-secret',{},io.BytesIO(b'private')),
                  TimeoutError('synthetic-secret'), b'invalid', b'[]',
                  b'{"company":{"currency":"synthetic-secret"}}',
                  b'{"company":{"currency":"RUB","name":"synthetic-secret"}}'+b'x'*65536]
        for profile in profiles:
            with self.subTest(profile=type(profile)):
                result,_=self.run_read(profile)
                self.assertEqual(result['ordered_amount'],'123.45')
                self.assertNotIn('company_currency',result)
                self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_currency_that_contains_a_secret_is_not_exposed(self):
        self.assertIsNone(ozon_mcp.project_company_currency({'company':{'currency':'RUB'}},{'api_key':'RUB'}))

    def test_hung_optional_lookup_does_not_hold_the_worker(self):
        # Exercise a real worker subprocess: a stuck resolver must not hold
        # the process, even though the network thread is still asleep.
        source_dir=str(Path(__file__).resolve().parent)
        code='''import io,json,sys,time
sys.path.insert(0,{source_dir!r})
import portal
portal.PROVIDER_DEADLINE_SECONDS=3
class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
class Opener:
    def open(self,request,timeout):
        if request.full_url.endswith('/seller/info'): time.sleep(60)
        return Response({analytics!r})
portal.urllib.request.build_opener=lambda *args:Opener()
portal._provider_worker()
'''.format(source_dir=source_dir,analytics=ANALYTICS)
        with tempfile.TemporaryDirectory() as tmp:
            worker=Path(tmp)/'worker.py'; worker.write_text(code,encoding='utf-8')
            started=time.monotonic()
            with patch('portal.PROVIDER_WORKER',str(worker)), patch('portal.PROVIDER_DEADLINE_SECONDS',30):
                result=portal.provider_read('ozon_orders_analytics',ARGS,CREDS)
            self.assertLess(time.monotonic()-started,25)
            self.assertEqual(result['ordered_amount'],'123.45')
            self.assertNotIn('company_currency',result)


if __name__=='__main__': unittest.main()
