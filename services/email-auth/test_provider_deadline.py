"""Deadline and subprocess boundary tests use synthetic data only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from portal import provider_read


class ProviderDeadlineTests(unittest.TestCase):
    def test_simple_reads_start_without_loading_portal_or_cache_encryption(self):
        portal_path = Path(__file__).with_name('portal.py').resolve()
        script = """import io, json, pathlib, runpy, sys, urllib.request
from email.message import Message
for name in ('app', 'ui', 'oauth_mcp', 'avito_mcp', 'cryptography'):
    sys.modules[name] = None
class Response(io.BytesIO):
    status = 200
    headers = Message()
    headers['Content-Type'] = 'application/json'
class Opener:
    def open(self, req, timeout):
        body = {'data': {'listGoods': []}, 'result': {'items': [], 'total': 0, 'last_id': ''}}
        return Response(json.dumps(body).encode())
urllib.request.build_opener = lambda *handlers: Opener()
path = sys.argv[1]
sys.path.insert(0, str(pathlib.Path(path).parent))
sys.argv = [path, '--provider-worker', *sys.argv[2:]]
runpy.run_path(path, run_name='__main__')
"""
        ozon = {'client_id':'8123', 'api_key':'synthetic-private-key'}
        reads = [('wb_prices', {'limit':1}, {'api_token':'synthetic-private-key'}),
                 ('ozon_products', {'limit':1,'include_identity':False}, ozon),
                 ('market_api_read', {'operation_id':'ozon_product_list','input':{'body':{'limit':1}}}, ozon)]
        for tool, arguments, credentials in reads:
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmp:
                request = dict(operation=tool, args=arguments, creds=credentials,
                               finance_gate_dir=str(Path(tmp)/'gates'), deadline_monotonic=time.monotonic()+60)
                for prepare in (False, True):
                    command = [sys.executable, '-B', '-I', '-c', script, str(portal_path)]
                    if prepare:
                        command.append(tool)
                    result = subprocess.run(command,
                                            input=json.dumps(request), capture_output=True, text=True, timeout=60, check=True)
                    if prepare:
                        self.assertTrue(result.stdout.startswith('R'))
                    self.assertEqual(json.loads(result.stdout.removeprefix('R'))['kind'], 'ok')

    @unittest.skipUnless(os.name=='posix', 'native Linux worker readiness pipe')
    def test_preparation_withholds_credentials_and_network_work_has_its_own_deadline(self):
        script = """import json, os, select, sys, time
if select.select([sys.stdin.buffer], [], [], 0.6)[0]:
    raise SystemExit(2)
os.write(1, b'R')
request = json.load(sys.stdin)
if request['args']['limit'] == 2:
    time.sleep(5)
print(json.dumps({'kind':'ok','result':{'private':request['creds']['api_token']=='test-private'}}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp)/'worker.py'
            worker.write_text(script, encoding='utf-8')
            with (patch.dict(os.environ, {'MCP_PREPARE_MARKET_WORKERS':'1'}),
                  patch('portal.PROVIDER_WORKER',str(worker)), patch('portal.PROVIDER_STARTUP_SECONDS',10),
                  patch('portal.PROVIDER_DEADLINE_SECONDS',0.4)):
                for operation in ('wb_prices', 'supply_plan', 'unit_economics', 'wb_supplies',
                                  'ozon_sku_daily', 'wb_funnel', 'archive_market_day'):
                    with self.subTest(operation=operation):
                        self.assertEqual(provider_read(operation,{'limit':1},{'api_token':'test-private'}),{'private':True})
                with self.assertRaises(TimeoutError):
                    provider_read('wb_prices',{'limit':2},{'api_token':'test-private'})

    @unittest.skipUnless(os.name=='posix', 'native Linux worker readiness pipe')
    def test_unready_worker_never_receives_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp)/'received'
            worker = Path(tmp)/'worker.py'
            worker.write_text("import sys,pathlib\nif sys.stdin.buffer.read(): pathlib.Path("+
                              repr(str(marker))+").touch()\n", encoding='utf-8')
            with (patch.dict(os.environ, {'MCP_PREPARE_MARKET_WORKERS':'1'}),
                  patch('portal.PROVIDER_WORKER',str(worker)), patch('portal.PROVIDER_STARTUP_SECONDS',0.3)):
                with self.assertRaises(TimeoutError):
                    provider_read('wb_prices',{'limit':1},{'api_token':'test-private'})
            self.assertFalse(marker.exists())

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
                    # Each synthetic outcome represents a distinct installation.
                    # A preceding unknown 429 must not make the next scenario unreachable.
                    state = Path(tmp) / str(scenario)
                    state.mkdir()
                    # This matrix checks the public-safe outcome for each response.
                    # Its simulated timeouts come from the transport. The real
                    # process deadline and kill behavior have their own test below.
                    with patch('portal.PROVIDER_WORKER', str(worker)), patch('portal.PROVIDER_DEADLINE_SECONDS',60), patch.dict(
                            os.environ, {'OZON_FINANCE_GATE_DIR': str(state / 'gate')}):
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

    def test_expired_worker_startup_prevents_dispatch_and_reaps_child(self):
        # A controlled monotonic clock reproduces a slow launch. The subprocess,
        # its stdin, the kill signal and the final wait are all real.
        with tempfile.TemporaryDirectory() as tmp:
            received = Path(tmp) / 'received'
            worker = Path(tmp) / 'worker.py'
            worker.write_text('import pathlib,sys,time\nif sys.stdin.buffer.read():\n'
                              '    pathlib.Path('+repr(str(received))+').touch()\n'
                              'time.sleep(60)\n', encoding='utf-8')
            spawn = subprocess.Popen
            children = []
            with patch('portal.time') as clock, patch('portal.PROVIDER_DEADLINE_SECONDS',20):
                clock.monotonic.return_value = 10.0
                def delayed_start(*args, **kwargs):
                    child = spawn(*args, **kwargs)
                    child.communicate = Mock(wraps=child.communicate)
                    children.append(child)
                    clock.monotonic.return_value = 31.0
                    return child
                with patch('portal.PROVIDER_WORKER',str(worker)), patch('portal.subprocess.Popen',side_effect=delayed_start):
                    with self.assertRaises(TimeoutError):
                        provider_read('wb_prices', {'limit':1}, {'api_token':'synthetic-secret'})
            self.assertEqual(len(children), 1)
            children[0].communicate.assert_not_called()
            self.assertIsNotNone(children[0].returncode)
            self.assertNotEqual(children[0].returncode, 0)
            self.assertFalse(received.exists())

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
