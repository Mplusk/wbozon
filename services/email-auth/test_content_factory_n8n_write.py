import io
import json
import time
import unittest
from urllib.error import HTTPError
from content_factory_n8n_write import update_and_verify, provision_and_verify, _run_bounded


def _slow_worker():
    time.sleep(5)
    return 'verified'


def _good_worker(): return 'verified'


class Response:
    status = 200
    def __init__(self, body): self.body = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, length): return self.body[:length]


class Opener:
    def __init__(self, answers): self.answers, self.requests = iter(answers), []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = next(self.answers)
        if isinstance(result, Exception): raise result
        return Response(result)


class AdapterTests(unittest.TestCase):
    def test_wall_deadline_returns_unknown(self):
        before = time.monotonic()
        self.assertEqual(_run_bounded(_slow_worker, (), 1), 'unknown')
        self.assertLess(time.monotonic() - before, 4)

    def test_bounded_process_returns_outcome(self):
        self.assertEqual(_run_bounded(_good_worker, (), 3), 'verified')
    def test_exact_target_and_merge(self):
        opener = Opener([{'id':'A123456789', 'type':'openAiApi', 'updatedAt':'v1'},
                         {'id':'A123456789', 'type':'openAiApi', 'updatedAt':'v1'}])
        result = update_and_verify('openai', 'A123456789', 't'*32, 'k'*32, opener=opener)
        self.assertEqual(result, 'accepted_unconfirmed')
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, 'https://n8n.gamelife.shop/api/v1/credentials/A123456789')
        self.assertEqual(request.get_method(), 'PATCH')
        self.assertEqual(json.loads(request.data), {'type':'openAiApi',
                         'data':{'apiKey':'k'*32, 'url':'https://api.openai.com/v1'}, 'isPartialData':False})
        self.assertEqual(timeout, 5)

    def test_error_does_not_echo_secret_or_retry(self):
        opener = Opener([RuntimeError('secret synthetic')])
        self.assertEqual(update_and_verify('gemini', 'A123456789', 't'*32, 'k'*32, opener=opener), 'unknown')
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_id_is_never_sent(self):
        opener = Opener([])
        with self.assertRaises(ValueError): update_and_verify('openai', '../other', 't'*32, 'k'*32, opener=opener)
        self.assertEqual(opener.requests, [])

    def test_expression_key_is_rejected(self):
        opener = Opener([])
        with self.assertRaises(ValueError): update_and_verify('gemini', 'A123456789', 't'*32, '='+'k'*32, opener=opener)
        self.assertEqual(opener.requests, [])

    def test_followup_failure_is_ambiguous(self):
        opener = Opener([{'id':'A123456789', 'type':'googlePalmApi', 'updatedAt':'v1'}, RuntimeError('failed')])
        self.assertEqual(update_and_verify('gemini', 'A123456789', 't'*32, 'k'*32, opener=opener), 'unknown')

    def test_concurrent_update_is_ambiguous(self):
        opener = Opener([{'id':'A123456789', 'type':'openAiApi', 'updatedAt':'v1'},
                         {'id':'A123456789', 'type':'openAiApi', 'updatedAt':'v2'}])
        self.assertEqual(update_and_verify('openai', 'A123456789', 't'*32, 'k'*32, opener=opener), 'unknown')

    def test_provision_only_if_absent_with_fixed_destination(self):
        meta = {'id':'A123456789', 'name':'SIDZEN CF OpenAI', 'type':'openAiApi', 'updatedAt':'v1'}
        absent = HTTPError('https://n8n.gamelife.shop', 404, 'not found', {}, io.BytesIO())
        opener = Opener([absent, meta, meta])
        self.assertEqual(provision_and_verify('openai', 'A123456789', 't'*32, 'k'*32, 'p'*24, opener=opener), 'accepted_unconfirmed')
        request = opener.requests[1][0]
        self.assertEqual(request.full_url, 'https://n8n.gamelife.shop/api/v1/credentials')
        self.assertEqual(json.loads(request.data), {'id':'A123456789', 'name':'SIDZEN CF OpenAI', 'type':'openAiApi',
                         'data':{'apiKey':'k'*32, 'url':'https://api.openai.com/v1'}})
        self.assertEqual(request.get_header('Idempotency-key'), 'p'*24)
        self.assertFalse(any(r.get_method() == 'POST' and r.full_url.endswith('/test') for r, _ in opener.requests))

    def test_provision_denied_or_existing_never_creates(self):
        for first in (HTTPError('https://n8n.gamelife.shop', 403, 'forbidden', {}, io.BytesIO()), {'id':'A123456789'}):
            opener = Opener([first])
            self.assertEqual(provision_and_verify('gemini', 'A123456789', 't'*32, 'k'*32, 'p'*24, opener=opener), 'unknown')
            self.assertEqual(len(opener.requests), 1)

    def test_reader_credential_uses_fixed_authorization_header(self):
        meta = {'id':'ReaderID123', 'name':'SIDZEN CF Model Config Reader', 'type':'httpTemplatedCustomAuth', 'updatedAt':'v1'}
        absent = HTTPError('https://n8n.gamelife.shop',404,'not found',{},io.BytesIO())
        opener = Opener([absent,meta,meta])
        reader_token='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg'
        self.assertEqual(provision_and_verify('reader','ReaderID123','t'*32,reader_token,'p'*24,opener=opener),'accepted_unconfirmed')
        data=json.loads(opener.requests[1][0].data)['data']
        self.assertEqual(json.loads(data['template']),{'headers':{'Authorization':'Bearer {{api_key}}'}})
        self.assertEqual(json.loads(data['placeholderValues']),{'api_key':reader_token})
        self.assertEqual(data['serviceHost'],'3dprinta.shop')
        self.assertFalse(any(request.full_url.endswith('/test') for request, _ in opener.requests))


if __name__ == '__main__': unittest.main()
