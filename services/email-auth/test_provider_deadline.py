"""Deadline and subprocess boundary tests use synthetic data only."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from portal import provider_read


class ProviderDeadlineTests(unittest.TestCase):
    def test_real_child_serializes_provider_outcomes(self):
        # Launch the production worker entry point in a real child process, with
        # only its HTTP transport replaced by synthetic deterministic responses.
        source_dir = str(Path(__file__).resolve().parent)
        template = '''import io,json,sys,urllib.error
sys.path.insert(0, {source_dir!r})
import portal
scenario = {scenario!r}
class Response:
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,limit): return json.dumps({{'data':{{'listGoods':[{{'nmID':1,'vendorCode':'synthetic-secret' if scenario == 'unsafe_identifier' else 'safe-code','currencyIsoCode4217':'synthetic-secret'}}]}}}}).encode() if scenario in ('ok','unsafe_identifier') else b'invalid-json'
class Opener:
    def open(self,request,timeout):
        if isinstance(scenario,int): raise urllib.error.HTTPError('https://example.invalid',scenario,'synthetic',{{}},io.BytesIO(b'synthetic'))
        if scenario == 'timeout': raise TimeoutError('synthetic')
        if scenario == 'wrapped_timeout': raise urllib.error.URLError(TimeoutError('synthetic-secret'))
        if scenario == 'network': raise urllib.error.URLError('synthetic')
        return Response()
portal.urllib.request.build_opener=lambda *args: Opener()
portal._provider_worker()
'''
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / 'worker.py'
            for scenario in ('ok', 'unsafe_identifier', 401, 403, 429, 503, 'timeout', 'wrapped_timeout', 'network', 'invalid'):
                with self.subTest(scenario=scenario):
                    worker.write_text(template.format(source_dir=source_dir, scenario=scenario), encoding='utf-8')
                    with patch('portal.PROVIDER_WORKER', str(worker)):
                        if scenario == 'ok':
                            item = provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})['items'][0]
                            self.assertEqual(item['vendorCode'], 'safe-code')
                            self.assertEqual(item['currencyIsoCode4217'], '[REDACTED]')
                        elif scenario == 'unsafe_identifier':
                            with self.assertRaises(ValueError) as raised:
                                provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
                            self.assertNotIn('synthetic-secret', str(raised.exception))
                        elif isinstance(scenario,int):
                            with self.assertRaises(urllib.error.HTTPError) as raised:
                                provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
                            self.assertEqual(raised.exception.code, scenario)
                            self.assertNotIn('synthetic-secret', str(raised.exception))
                        elif scenario in ('timeout','wrapped_timeout'):
                            with self.assertRaises(TimeoutError) as raised:
                                provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
                            self.assertNotIn('synthetic-secret', str(raised.exception))
                        elif scenario == 'network':
                            with self.assertRaises(urllib.error.URLError):
                                provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
                        else:
                            with self.assertRaises(ValueError):
                                provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})

    def test_real_worker_fails_closed_on_invalid_operation(self):
        with self.assertRaises(ValueError):
            provider_read('not_an_operation', {'limit':1}, {'api_token':'synthetic-secret'})

    def test_worker_http_status_is_preserved_without_upstream_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / 'worker.py'
            worker.write_text('print(\'{"kind":"http","status":429}\')', encoding='utf-8')
            with patch('portal.PROVIDER_WORKER', str(worker)):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
            self.assertEqual(raised.exception.code, 429)
            self.assertNotIn('synthetic-secret', str(raised.exception))

    def test_hung_worker_is_killed_at_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'finished'
            worker = Path(tmp) / 'worker.py'
            worker.write_text('import pathlib,time\ntime.sleep(2)\npathlib.Path('+repr(str(marker))+').touch()\n', encoding='utf-8')
            start = time.monotonic()
            with patch('portal.PROVIDER_WORKER', str(worker)), patch('portal.PROVIDER_DEADLINE_SECONDS', 0.3):
                with self.assertRaises(TimeoutError):
                    provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
            self.assertLess(time.monotonic()-start, 1.5)
            time.sleep(2)
            self.assertFalse(marker.exists(), 'timed out worker was not stopped')

    def test_credentials_only_passed_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / 'worker.py'
            worker.write_text('''import json,os,sys
request=json.load(sys.stdin)
secret=request['creds']['api_token']
result={'kind':'ok','result':{'stdin_ok':secret=='synthetic-secret',
    'argv_clean':all(secret not in value for value in sys.argv),
    'env_clean':all(secret not in value for value in os.environ.values())}}
print(json.dumps(result))
''', encoding='utf-8')
            with patch('portal.PROVIDER_WORKER', str(worker)):
                self.assertEqual(provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'}),
                    {'stdin_ok':True, 'argv_clean':True, 'env_clean':True})

    def test_standard_proxy_and_ca_configuration_reaches_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp) / 'worker.py'
            worker.write_text('''import json,os
print(json.dumps({'kind':'ok','result':{
    'proxy_ok':os.environ.get('https_proxy')=='http://127.0.0.1:9999',
    'ca_ok':os.environ.get('SSL_CERT_FILE')=='/synthetic/ca.pem'}}))
''', encoding='utf-8')
            with patch.dict(os.environ, {'https_proxy':'http://127.0.0.1:9999', 'SSL_CERT_FILE':'/synthetic/ca.pem'}), patch('portal.PROVIDER_WORKER', str(worker)):
                self.assertEqual(provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'}), {'proxy_ok':True, 'ca_ok':True})
