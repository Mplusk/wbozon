"""Offline tests of the per-client finance gate and Retry-After handling."""
import email.message
import email.utils
import io
import os
import subprocess
import shutil
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import finance_gate as gate
import portal


class FinanceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, 'finance')
        self.client = '10001000100010001000100010001'
    def tearDown(self):
        self.temp.cleanup()

    def test_short_429_retries_only_after_shared_cooldown(self):
        clock={'wall':1000.0,'mono':100.0};calls=[]
        def read():
            calls.append('read')
            if len(calls)==1: raise urllib.error.HTTPError('',429,'',{'Retry-After':'1'},io.BytesIO(b''))
            return {'ok':True}
        def sleep(seconds):
            with self.assertRaises(gate.GateError) as blocked:
                gate.run(self.root,self.client,lambda:self.fail('early request'))
            self.assertEqual(blocked.exception.reason,'cooldown')
            clock['wall']+=seconds;clock['mono']+=seconds
        creds={'client_id':self.client,'api_key':'private'}
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), \
             patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']), \
             patch('portal.time.sleep',side_effect=sleep):
            self.assertEqual(portal._finance_read_with_retry(self.root,creds,read,110),{'ok':True})
        self.assertEqual(len(calls),2)

    def test_second_429_is_not_retried_and_remains_recorded(self):
        calls=[]
        def read():
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{'Retry-After':'0'},io.BytesIO(b''))
        with patch('portal.time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                portal._finance_read_with_retry(self.root,{'client_id':self.client,'api_key':'private'},read,time.monotonic()+10)
        self.assertEqual(len(calls),2)
        with open(os.path.join(self.root,gate._key(self.client)+'.json'),encoding='utf8') as f:
            self.assertEqual(__import__('json').load(f)['status'],'retry')

    def test_unknown_long_deadline_and_composite_429_are_not_retried(self):
        for retry,available,allowed in [(None,10,True),('3',10,True),('1',3,True),('1',10,False)]:
            with self.subTest(retry=retry,available=available,allowed=allowed), tempfile.TemporaryDirectory() as temp:
                calls=[]
                def read():
                    calls.append(1);headers={} if retry is None else {'Retry-After':retry}
                    raise urllib.error.HTTPError('',429,'',headers,io.BytesIO(b''))
                with patch('portal.time.sleep') as sleep, self.assertRaises(urllib.error.HTTPError):
                    portal._finance_read_with_retry(os.path.join(temp,'gate'),
                        {'client_id':self.client,'api_key':'private'},read,time.monotonic()+available,allow_retry=allowed)
                self.assertEqual(len(calls),1);sleep.assert_not_called()

    def test_late_wakeup_does_not_start_another_request(self):
        clock={'wall':1000.0,'mono':100.0};calls=[]
        def read():
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{'Retry-After':'1'},io.BytesIO(b''))
        def late_sleep(_):
            clock['wall']+=8;clock['mono']+=8
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), \
             patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']), \
             patch('portal.time.sleep',side_effect=late_sleep),self.assertRaises(urllib.error.HTTPError):
            portal._finance_read_with_retry(self.root,{'client_id':self.client,'api_key':'private'},read,110)
        self.assertEqual(len(calls),1)

    def test_retry_after_seconds_http_date_and_server_clock_skew(self):
        now = 1794880000
        self.assertEqual(gate.parse_retry_after({'Retry-After':'17'},now), (17,'delta_seconds'))
        later = email.utils.format_datetime(datetime.fromtimestamp(now+45,timezone.utc),usegmt=True)
        server = email.utils.format_datetime(datetime.fromtimestamp(now-30,timezone.utc),usegmt=True)
        self.assertEqual(gate.parse_retry_after({'Retry-After':later,'Date':server},now), (75,'http_date'))
        earlier = email.utils.format_datetime(datetime.fromtimestamp(now-1,timezone.utc),usegmt=True)
        self.assertEqual(gate.parse_retry_after({'Retry-After':earlier},now), (None,'unknown'))
        present = email.utils.format_datetime(datetime.fromtimestamp(now,timezone.utc),usegmt=True)
        self.assertEqual(gate.parse_retry_after({'Retry-After':earlier,'Date':present},now), (0,'http_date'))

    def test_retry_after_invalid_absent_duplicate_and_overflow_are_unknown(self):
        headers = [{}, {'Retry-After':'-1'}, {'Retry-After':'1.5'}, {'Retry-After':'  '},
                   {'Retry-After':'9'*11}, {'Retry-After':'2147483648'},
                   {'Retry-After':'0\r\nInjected: value'}, {'Retry-After':5},
                   {'Retry-After':'date without timezone'}]
        message=email.message.Message()
        message.add_header('Retry-After','1')
        message.add_header('Retry-After','2')
        headers.append(message)
        for item in headers:
            with self.subTest(headers=item): self.assertEqual(gate.parse_retry_after(item,100), (None,'unknown'))

    def test_known_429_survives_new_gate_invocation_and_blocks_early_retries(self):
        clock={'wall':1000.0,'mono':100.0}
        calls=[]
        def request():
            calls.append('once')
            raise urllib.error.HTTPError('',429,'not exported',{'Retry-After':'5'},io.BytesIO(b'not exported'))
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                gate.run(self.root,self.client,request)
            self.assertEqual(caught.exception.retry_after_seconds,5)
            clock.update(wall=1003.0,mono=103.0)
            with self.assertRaises(gate.GateError) as blocked:
                gate.run(self.root,self.client,lambda:calls.append('too early'))
            self.assertEqual((blocked.exception.reason,blocked.exception.retry_after_seconds),('cooldown',2))
            clock.update(wall=5000.0,mono=104.0)
            with self.assertRaises(gate.GateError) as blocked:
                gate.run(self.root,self.client,lambda:calls.append('wall jump'))
            self.assertEqual(blocked.exception.retry_after_seconds,1)
            clock.update(wall=5001.0,mono=105.0)
            self.assertEqual(gate.run(self.root,self.client,lambda:'ok'),'ok')
        self.assertEqual(calls,['once'])

    def test_cooldown_is_enforced_after_a_fresh_process_starts(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        code = ('import finance_gate,sys\n'
                'try: finance_gate.run(sys.argv[1],"10001000100010001000100010001",lambda:print("called"))\n'
                'except finance_gate.GateError as e: print(e.reason)\n')
        child=subprocess.run([sys.executable,'-c',code,self.root],capture_output=True,text=True,timeout=5)
        self.assertEqual(child.returncode,0)
        self.assertEqual(child.stdout.strip(),'cooldown')

    def test_http_date_429_is_persisted_and_enforced_using_server_date(self):
        clock={'wall':1794880000.0,'mono':100.0}
        date = email.utils.format_datetime(datetime.fromtimestamp(clock['wall'],timezone.utc),usegmt=True)
        retry = email.utils.format_datetime(datetime.fromtimestamp(clock['wall']+8,timezone.utc),usegmt=True)
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':retry,'Date':date},io.BytesIO())
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']):
            with self.assertRaises(urllib.error.HTTPError) as caught: gate.run(self.root,self.client,request)
            self.assertEqual((caught.exception.retry_after_seconds,caught.exception.retry_after_source),(8,'http_date'))
            clock.update(wall=clock['wall']+7,mono=107.0)
            with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
            self.assertEqual(blocked.exception.retry_after_seconds,1)
            clock.update(wall=clock['wall']+1,mono=108.0)
            self.assertEqual(gate.run(self.root,self.client,lambda:'ok'),'ok')

    def test_missing_retry_after_blocks_blind_retries(self):
        def request(): raise urllib.error.HTTPError('',429,'',{},io.BytesIO(b''))
        with self.assertRaises(urllib.error.HTTPError) as caught: gate.run(self.root,self.client,request)
        self.assertIsNone(caught.exception.retry_after_seconds)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')

    def test_retry_value_matching_key_is_not_persisted_as_delay(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'45'},io.BytesIO(b''))
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request,('45',))
        state_path=os.path.join(self.root,gate._key(self.client)+'.json')
        with open(state_path,'rb') as file: state=file.read()
        self.assertNotIn(b'45',state)
        self.assertIn(b'retry_unknown',state)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')

    def test_verified_local_recovery_never_retries_before_the_new_wait(self):
        clock={'wall':1000.0,'mono':100.0}
        def request(): raise urllib.error.HTTPError('',429,'',{},io.BytesIO(b''))
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']):
            with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
            gate.schedule_verified_recovery(self.root,self.client,10)
            with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
            self.assertEqual(blocked.exception.retry_after_seconds,10)
            clock.update(wall=1010.0,mono=110.0)
            self.assertEqual(gate.run(self.root,self.client,lambda:'ok'),'ok')

    def test_recovery_cannot_shorten_a_known_same_boot_provider_wait(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        with self.assertRaises(ValueError): gate.schedule_verified_recovery(self.root,self.client,1)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'cooldown')

    def test_failure_or_cancel_after_start_leaves_uncertain_marker(self):
        with self.assertRaises(TimeoutError):
            gate.run(self.root,self.client,lambda:(_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(gate.GateError) as blocked:
            gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')

    def test_known_non_429_response_errors_release_gate(self):
        for index,error in enumerate((gate.ProviderResponseTooLarge(''),
                                      gate.ProviderResponseInvalid(''),
                                      gate.ProviderBodyTimeout(''),
                                      gate.LocalPreparationError(''))):
            client=str(index+1)
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                gate.run(self.root,client,lambda:(_ for _ in ()).throw(error))
            self.assertEqual(gate.run(self.root,client,lambda:'next'),'next')

    def test_read_failure_after_200_does_not_block_narrower_request(self):
        class Response:
            def __init__(self, content): self.content=content
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def read(self,limit):
                if isinstance(self.content,BaseException): raise self.content
                return self.content
        class Opener:
            def __init__(self,content): self.content=content
            def open(self,request,timeout): return Response(self.content)
        creds={'client_id':self.client,'api_key':'synthetic-private'}
        for error, content in ((gate.ProviderResponseTooLarge,b'x'*512001),
                               (gate.ProviderResponseInvalid,b'{bad json'),
                               (gate.ProviderBodyTimeout,TimeoutError())):
            with self.subTest(error=error.__name__):
                with patch('portal.urllib.request.build_opener',return_value=Opener(content)):
                    with self.assertRaises(error):
                        gate.run(self.root,self.client,lambda:portal._provider_read('ozon_accrual_types',{},creds))
                with patch('portal.urllib.request.build_opener',return_value=Opener(b'{"accrual_types":[]}')):
                    result=gate.run(self.root,self.client,lambda:portal._provider_read('ozon_accrual_types',{},creds))
                self.assertEqual(result['types'],[])

    def test_local_tls_setup_failure_does_not_leave_uncertain_state(self):
        creds={'client_id':self.client,'api_key':'synthetic-private'}
        with patch('market_https._context',side_effect=OSError('not exported')), \
             patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('unexpected request')):
            with self.assertRaises(gate.LocalPreparationError):
                gate.run(self.root,self.client,lambda:portal._provider_read('ozon_accrual_types',{},creds))
        self.assertEqual(gate.run(self.root,self.client,lambda:'next'),'next')

    def test_cancellation_does_not_release_an_unknown_request_for_retry(self):
        with self.assertRaises(KeyboardInterrupt):
            gate.run(self.root,self.client,lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(gate.GateError) as blocked:
            gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')

    def test_lost_gate_directory_is_not_silently_recreated(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        os.replace(self.root,self.root+'-lost')
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')
        self.assertFalse(os.path.exists(self.root))

    def test_replaced_gate_directory_is_not_accepted(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        os.replace(self.root,self.root+'-lost')
        os.mkdir(self.root)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_rollback_of_only_client_directory_cannot_rewind_cooldown(self):
        self.assertEqual(gate.run(self.root,self.client,lambda:'ok'),'ok')
        snapshot=self.root+'-snapshot'
        shutil.copytree(self.root,snapshot)
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        os.replace(self.root,self.root+'-newer')
        os.replace(snapshot,self.root)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_loss_of_both_per_client_files_is_detected_by_registry(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        key=gate._key(self.client)
        for suffix in ('.lock','.json'):
            path=os.path.join(self.root,key+suffix)
            os.replace(path,path+'.lost')
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_lost_lock_file_is_not_recreated_next_to_existing_state(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        lock=os.path.join(self.root,gate._key(self.client)+'.lock')
        os.replace(lock,lock+'.lost')
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_recovery_of_uninitialized_state_does_not_poison_first_request(self):
        with self.assertRaises(ValueError): gate.schedule_verified_recovery(self.root,self.client,10)
        self.assertEqual(gate.run(self.root,self.client,lambda:'ok'),'ok')

    def test_new_boot_does_not_trust_a_forward_wall_clock_jump(self):
        clock={'wall':1000.0,'mono':5000.0}
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), \
             patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']), \
             patch('finance_gate._boot_identity',return_value='a'*64):
            with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,self.client,request)
        clock.update(wall=100000.0,mono=10.0)
        with patch('finance_gate.time.time',side_effect=lambda:clock['wall']), \
             patch('finance_gate.time.monotonic',side_effect=lambda:clock['mono']), \
             patch('finance_gate._boot_identity',return_value='b'*64):
            with self.assertRaises(gate.GateError) as blocked:
                gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')

    def test_two_vaults_in_same_service_directory_share_client_gate(self):
        first_vault=types.SimpleNamespace(path=os.path.join(self.temp.name,'a.sqlite'))
        second_vault=types.SimpleNamespace(path=os.path.join(self.temp.name,'b.sqlite'))
        calls=[]
        def blocked(*args):
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{'Retry-After':'60'},io.BytesIO())
        one=portal.Portal(object(),first_vault,'a'*64,gateway=blocked,external_key_store=object(),oauth_store=object())
        two=portal.Portal(object(),second_vault,'b'*64,gateway=blocked,external_key_store=object(),oauth_store=object())
        self.assertEqual(one.finance_gate_dir,two.finance_gate_dir)
        creds={'client_id':'123','api_key':'fake'}
        with self.assertRaises(urllib.error.HTTPError): one.read_gateway('ozon_accrual_types',{},creds)
        with self.assertRaises(gate.GateError) as exc: two.read_gateway('ozon_accrual_types',{},creds)
        self.assertEqual(exc.exception.reason,'cooldown')
        self.assertEqual(len(calls),1)

    def test_explicit_shared_gate_coordinates_distinct_vault_directories(self):
        first_vault=types.SimpleNamespace(path=os.path.join(self.temp.name,'one','a.sqlite'))
        second_vault=types.SimpleNamespace(path=os.path.join(self.temp.name,'two','b.sqlite'))
        calls=[]
        def blocked(*args):
            calls.append(1)
            raise urllib.error.HTTPError('',429,'',{'Retry-After':'60'},io.BytesIO())
        one=portal.Portal(object(),first_vault,'a'*64,gateway=blocked,external_key_store=object(),oauth_store=object(),finance_gate_dir=self.root)
        two=portal.Portal(object(),second_vault,'b'*64,gateway=blocked,external_key_store=object(),oauth_store=object(),finance_gate_dir=self.root)
        creds={'client_id':'123','api_key':'fake'}
        with self.assertRaises(urllib.error.HTTPError): one.read_gateway('ozon_accrual_types',{},creds)
        with self.assertRaises(gate.GateError) as blocked_exc: two.read_gateway('ozon_accrual_types',{},creds)
        self.assertEqual(blocked_exc.exception.reason,'cooldown')
        self.assertEqual(len(calls),1)

    def test_invalid_local_credentials_do_not_reserve_or_poison_gate(self):
        vault=types.SimpleNamespace(path=os.path.join(self.temp.name,'a.sqlite'))
        calls=[]
        instance=portal.Portal(object(),vault,'a'*64,gateway=lambda *a:calls.append(1) or {'types':[]},
                               external_key_store=object(),oauth_store=object(),finance_gate_dir=self.root)
        for creds in ({'client_id':'123'}, {'client_id':'123','api_key':'x\nother'},
                      {'client_id':'bad','api_key':'fake'}):
            with self.subTest(creds=creds), self.assertRaises(ValueError):
                instance.read_gateway('ozon_accrual_types',{},creds)
            self.assertFalse(os.path.exists(self.root))
        self.assertEqual(instance.read_gateway('ozon_accrual_types',{},
                                               {'client_id':'123','api_key':'fake'}),{'types':[]})
        self.assertEqual(calls,[1])

    def test_storage_failure_prevents_request_and_lost_state_does_not_reset(self):
        with patch('finance_gate.os.replace',side_effect=OSError('synthetic')):
            with self.assertRaises(gate.GateError): gate.run(self.root,self.client,lambda:self.fail('called'))
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_missing_boot_identity_fails_closed(self):
        with patch('finance_gate._boot_identity',side_effect=gate.GateError('storage_unavailable')):
            with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_storage_failure_after_429_keeps_in_flight_marker(self):
        original = gate.os.replace
        count = []
        def replace(first, second):
            if os.path.normpath(second)==os.path.join(self.root,gate._key(self.client)+'.json'):
                count.append(1)
                if len(count)==3: raise OSError('synthetic')
            return original(first, second)
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'3600'},io.BytesIO())
        with patch('finance_gate.os.replace',side_effect=replace):
            with self.assertRaises(gate.GateError): gate.run(self.root,self.client,request)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'storage_unavailable')

    def test_gate_is_held_across_processes_and_separate_clients_are_independent(self):
        code = ('import finance_gate,sys\n'
                'def work():\n print("locked",flush=True)\n sys.stdin.readline()\n'
                'finance_gate.run(sys.argv[1],"10001000100010001000100010001",work)\n')
        child = subprocess.Popen([sys.executable,'-c',code,self.root],stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            with self.assertRaises(gate.GateError) as blocked:
                gate.run(self.root,self.client,lambda:self.fail('concurrent call'))
            self.assertEqual(blocked.exception.reason,'busy')
            self.assertEqual(gate.run(self.root,'synthetic-client-2',lambda:'other'),'other')
            child.stdin.write('\n'); child.stdin.flush()
            self.assertEqual(child.wait(timeout=5),0)
            self.assertEqual(gate.run(self.root,self.client,lambda:'next'),'next')
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close()

    def test_numeric_client_id_alternative_forms_share_cooldown(self):
        def request(): raise urllib.error.HTTPError('',429,'',{'Retry-After':'60'},io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError): gate.run(self.root,'00123',request)
        with self.assertRaises(gate.GateError) as blocked:
            gate.run(self.root,'123',lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'cooldown')

    def test_killed_process_leaves_uncertain_state(self):
        code = ('import finance_gate,sys,os\n'
                'finance_gate.run(sys.argv[1],"10001000100010001000100010001",lambda:os._exit(23))\n')
        child=subprocess.run([sys.executable,'-c',code,self.root],capture_output=True,timeout=5)
        self.assertEqual(child.returncode,23)
        with self.assertRaises(gate.GateError) as blocked: gate.run(self.root,self.client,lambda:self.fail('called'))
        self.assertEqual(blocked.exception.reason,'retry_time_unknown')
        for name in os.listdir(self.root):
            with open(os.path.join(self.root,name),'rb') as file:
                self.assertNotIn(self.client.encode(),file.read())

    def test_worker_preserves_429_header_without_provider_body(self):
        script = ('import sys\nsys.path.insert(0,'+repr(os.path.dirname(portal.__file__))+')\n'
                  'import io,portal,urllib.error\n'
                  'def fail(*a,**kw): raise urllib.error.HTTPError("",429,"private",{"Retry-After":"37"},io.BytesIO(b"private"))\n'
                  'portal._provider_read=fail\n'
                  'portal._provider_worker()\n')
        worker=os.path.join(self.temp.name,'fake_worker.py')
        with open(worker,'w',encoding='utf-8') as file: file.write(script)
        # This fixture checks HTTP error serialization across a cold child.
        # The hard cancellation deadline is exercised in test_provider_deadline.
        with patch('portal.PROVIDER_WORKER',worker), patch('portal.PROVIDER_DEADLINE_SECONDS',60):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                portal.provider_read('ozon_accrual_types',{}, {'client_id':self.client,'api_key':'fake'},
                                     finance_gate_dir=self.root)
        self.assertEqual(caught.exception.code,429)
        self.assertEqual(caught.exception.headers.get('Retry-After'),'37')
        self.assertNotIn('private',str(caught.exception))

    def test_worker_preserves_known_provider_size_error_and_releases_gate(self):
        script = ('import sys\nsys.path.insert(0,'+repr(os.path.dirname(portal.__file__))+')\n'
                  'import portal,finance_gate\n'
                  'def fail(*a,**kw): raise finance_gate.ProviderResponseTooLarge("private")\n'
                  'portal._provider_read=fail\n'
                  'portal._provider_worker()\n')
        worker=os.path.join(self.temp.name,'fake_worker.py')
        with open(worker,'w',encoding='utf-8') as file: file.write(script)
        with patch('portal.PROVIDER_WORKER',worker), patch('portal.PROVIDER_DEADLINE_SECONDS',60):
            with self.assertRaises(gate.ProviderResponseTooLarge) as caught:
                portal.provider_read('ozon_accrual_types',{}, {'client_id':self.client,'api_key':'fake'},
                                     finance_gate_dir=self.root)
        self.assertNotIn('private',str(caught.exception))
        self.assertEqual(gate.run(self.root,self.client,lambda:'next'),'next')


if __name__ == '__main__': unittest.main()
