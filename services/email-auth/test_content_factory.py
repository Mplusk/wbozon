import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import content_factory as cf


class Vault:
    def read(self, provider):
        return {'api_key': 'synthetic-api-token-123456'} if provider == 'n8n' else None


def node(name, kind, parameters):
    return {'name': name, 'type': kind, 'parameters': parameters}


def workflow():
    return {'active': True, 'activeVersionId': 'published', 'versionId': 'published',
            'settings': {'timezone': 'Europe/Moscow'}, 'nodes': [
                node('Генерация — 10:00 МСК', 'n8n-nodes-base.scheduleTrigger',
                     {'rule': {'interval': [{'triggerAtHour': 10}]}}),
                node('Публикация — проверка каждые 5 минут', 'n8n-nodes-base.scheduleTrigger',
                     {'rule': {'interval': [{'field': 'cronExpression', 'expression': '*/5 * * * *'}]}}),
                node('OpenAI — идея и текст', '@n8n/n8n-nodes-langchain.openAi',
                     {'modelId': {'mode': 'id', 'value': 'gpt-5.4-mini'}, 'secret': 'never show'}),
                node('Gemini — изображение', '@n8n/n8n-nodes-langchain.googleGemini',
                     {'modelId': {'mode': 'id', 'value': 'models/gemini-3.1-flash-image'}}),
            ]}


class SnapshotTests(unittest.TestCase):
    def test_no_key_is_explicitly_not_configured(self):
        with patch.dict(os.environ, {'N8N_API_KEY': 'unrelated-environment-token-123'}, clear=True), patch.object(cf, '_request') as send:
            self.assertEqual(cf.snapshot()['status'], 'not_configured')
            send.assert_not_called()

    def test_projection_redaction_and_bounded_pagination(self):
        token = 'synthetic-api-token-123456'
        def request(path, supplied):
            self.assertEqual(supplied, token)
            if path.startswith('workflows/'):
                return workflow()
            if cf.QUEUE_ID in path:
                self.assertIn('sortBy=createdAt%3Adesc', path)
                return {'data': [{'caption': 'x' * 2195 + token, 'status': 'ready'}], 'nextCursor': 'next'}
            if cf.IDEAS_ID in path:
                return {'data': [{'title': 'idea'}], 'nextCursor': None}
            return {'data': [{'setting_key': 'pilot', 'monthly_budget_usd': 10, 'enabled': True}]}
        with patch.object(cf, '_request', side_effect=request):
            result = cf._collect(token)
        self.assertEqual(result['workflow']['config']['generation_time'], '10:00')
        self.assertEqual(result['workflow']['config']['models']['text'], 'gpt-5.4-mini')
        self.assertEqual(result['workflow']['config']['models']['image'], 'models/gemini-3.1-flash-image')
        self.assertIsNone(result['workflow']['config']['models']['video'])
        self.assertTrue(result['queue']['has_more'])
        self.assertIs(result['budget']['enabled'], True)
        self.assertNotIn('caption', result['queue']['items'][0])
        self.assertNotIn(token, str(result))
        self.assertNotIn('never show', str(result))

    def test_many_long_captions_are_not_returned_to_portal(self):
        def request(path, _token):
            if cf.QUEUE_ID in path:
                return {'data': [{'status': 'ready', 'caption': 'я' * 2200} for _ in range(50)]}
            raise RuntimeError('offline')
        with patch.object(cf, '_request', side_effect=request):
            result = cf._collect('synthetic-api-token-123456')
        self.assertEqual(len(result['queue']['items']), 50)
        self.assertLess(len(json.dumps(result, ensure_ascii=True)), cf.MAX_BYTES)

    def test_large_raw_queue_is_projected_and_oversized_response_is_rejected(self):
        token = 'synthetic-api-token-123456'
        payload = json.dumps({'data': [{'status': 'ready', 'caption': 'я' * 2200}
                                       for _ in range(50)]}).encode('utf-8')
        self.assertGreater(len(payload), cf.MAX_BYTES)
        class Opener:
            def open(self, request, timeout):
                if cf.QUEUE_ID not in request.full_url:
                    raise OSError('unavailable')
                return io.BytesIO(payload)
        with patch.object(cf.urllib.request, 'build_opener', return_value=Opener()):
            result = cf._collect(token)
        self.assertEqual(len(result['queue']['items']), 50)
        self.assertNotIn('caption', result['queue']['items'][0])
        self.assertLess(len(json.dumps(result).encode()), cf.MAX_BYTES)
        payload = b'x' * (cf.MAX_HTTP_BYTES + 1)
        with patch.object(cf.urllib.request, 'build_opener', return_value=Opener()):
            with self.assertRaisesRegex(ValueError, 'Response too large'):
                cf._request('data-tables/' + cf.QUEUE_ID + '/rows?limit=50&sortBy=createdAt%3Adesc', token)

    def test_version_mismatch_uses_verified_active_nodes_only(self):
        draft = workflow()
        draft['versionId'] = 'draft'
        self.assertEqual(cf._published_config(draft)['status'], 'unavailable')
        draft['activeVersion'] = {'versionId': 'published', 'nodes': workflow()['nodes']}
        result = cf._published_config(draft)
        self.assertEqual(result['generation_time'], '10:00')
        self.assertIsNone(result['timezone'])
        draft['activeVersion']['versionId'] = 'wrong'
        self.assertEqual(cf._published_config(draft)['status'], 'unavailable')

    def test_unknown_and_dynamic_values_are_not_reported_as_facts(self):
        draft = workflow()
        del draft['activeVersionId']
        self.assertEqual(cf._published_config(draft)['status'], 'unavailable')
        draft = workflow()
        draft['nodes'][0]['parameters']['rule']['interval'][0]['triggerAtHour'] = '={{ $json.hour }}'
        draft['nodes'][2]['parameters']['modelId']['value'] = '={{ $json.model }}'
        draft['nodes'].append(draft['nodes'][3])
        config = cf._published_config(draft)
        self.assertIsNone(config['generation_time'])
        self.assertIsNone(config['models']['text'])
        self.assertIsNone(config['models']['image'])
        self.assertIsNone(config['instagram_account'])

    def test_failed_api_and_host_allowlist(self):
        with patch.object(cf, '_request', side_effect=RuntimeError('private trace')):
            result = cf._collect('synthetic-api-token-123456')
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private trace', str(result))
        with self.assertRaises(ValueError):
            cf._request('https://bad.example/', 'synthetic-api-token-123456')
        self.assertIsNone(cf._NoRedirect().redirect_request(None, None, 302, None, None, 'https://bad.example/'))

    def test_vault_error_is_not_misreported_as_missing_key(self):
        class BrokenVault:
            def read(self, _):
                raise RuntimeError('private database failure')
        result = cf.snapshot(BrokenVault())
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private database failure', str(result))

    def test_budget_enabled_is_only_reported_from_a_boolean(self):
        for value, expected in ((False, False), (True, True), ('true', None), (None, None)):
            def request(path, _token):
                if cf.BUDGET_ID in path:
                    return {'data': [{'setting_key': 'pilot', 'monthly_budget_usd': 10, 'enabled': value}]}
                raise OSError('offline')
            with patch.object(cf, '_request', side_effect=request):
                result = cf._collect('synthetic-api-token-123456')
            self.assertIs(result['budget']['enabled'], expected)

    def test_malformed_rows_do_not_look_like_empty_queue(self):
        def request(path, _token):
            if cf.QUEUE_ID in path:
                return {'data': [None]}
            raise RuntimeError('offline')
        with patch.object(cf, '_request', side_effect=request):
            result = cf._collect('synthetic-api-token-123456')
        self.assertEqual(result['queue']['status'], 'unavailable')

    @unittest.skipUnless(os.name == 'posix', 'Cross-process lock is POSIX-only')
    def test_wall_timeout_fails_closed(self):
        class OnDiskVault(Vault):
            pass
        with tempfile.TemporaryDirectory() as directory:
            vault = OnDiskVault()
            vault.path = os.path.join(directory, 'vault.db')
            with patch.object(cf.subprocess, 'run', side_effect=subprocess.TimeoutExpired('test', cf.WALL_TIMEOUT)) as run:
                self.assertEqual(cf.snapshot(vault)['status'], 'unavailable')
                self.assertEqual(run.call_args.kwargs['timeout'], cf.WALL_TIMEOUT)
                self.assertNotIn('synthetic-api-token-123456', str(run.call_args.args))

    @unittest.skipUnless(os.name == 'posix', 'Cross-process lock is POSIX-only')
    def test_second_process_does_not_issue_request_while_lock_held(self):
        class OnDiskVault(Vault):
            pass
        with tempfile.TemporaryDirectory() as directory:
            vault = OnDiskVault()
            vault.path = os.path.join(directory, 'vault.db')
            lock = os.path.join(directory, '.content-factory-read.lock')
            code = ('import fcntl,sys; f=open(sys.argv[1],"w"); '
                    'fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read(1)')
            holder = subprocess.Popen([sys.executable, '-c', code, lock],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                self.assertEqual(holder.stdout.readline(), b'ready\n')
                os.chmod(lock, 0o600)
                with patch.object(cf.subprocess, 'run') as run:
                    self.assertEqual(cf.snapshot(vault)['status'], 'unavailable')
                    run.assert_not_called()
            finally:
                holder.communicate(input=b'x', timeout=5)

    @unittest.skipUnless(os.name == 'posix', 'Cross-process lock is POSIX-only')
    def test_real_hung_worker_is_killed_and_lock_released(self):
        class OnDiskVault(Vault):
            pass
        with tempfile.TemporaryDirectory() as directory:
            vault = OnDiskVault()
            vault.path = os.path.join(directory, 'vault.db')
            worker = os.path.join(directory, 'hanging_worker.py')
            marker = os.path.join(directory, 'started')
            with open(worker, 'w', encoding='utf-8') as script:
                script.write('import os, pathlib, sys, time\n'
                             'sys.stdin.buffer.read()\n'
                             'pathlib.Path(' + repr(marker) + ').write_text(str(os.getpid()))\n'
                             'time.sleep(60)\n')
            started = time.monotonic()
            with patch.object(cf, '__file__', worker), patch.object(cf, 'WALL_TIMEOUT', 1):
                result = cf.snapshot(vault)
            self.assertEqual(result['status'], 'unavailable')
            self.assertTrue(os.path.exists(marker), 'worker did not start before timeout')
            self.assertLess(time.monotonic() - started, 8)
            with open(marker, encoding='utf-8') as pid_file:
                worker_pid = int(pid_file.read())
            with self.assertRaises(ProcessLookupError, msg='worker survived the timeout'):
                os.kill(worker_pid, 0)
            lock = os.path.join(directory, '.content-factory-read.lock')
            probe = subprocess.run(
                [sys.executable, '-c',
                 'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); '
                 'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); os.close(fd)', lock],
                capture_output=True, timeout=5, check=False)
            self.assertEqual(probe.returncode, 0, 'lock remained held after worker timeout')


if __name__ == '__main__':
    unittest.main()
