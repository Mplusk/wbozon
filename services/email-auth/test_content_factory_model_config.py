import os
import gc
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from portal import Vault
from content_factory_model_config import read, compare_and_save, create_reader_token, authenticate, READER_SLOT, ReaderProvision
from content_factory_fence import Fence


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.vault=Vault(os.path.join(self.tmp.name,'vault.db'),secrets.token_bytes(32))
        self.models={'text':'gpt-5.4-mini','image':'models/gemini-3.1-flash-image'}

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def test_revision_and_allowlist(self):
        with self.assertRaises(ValueError): read(self.vault)
        self.assertEqual(compare_and_save(self.vault,0,self.models),1)
        self.assertEqual(read(self.vault),{'revision':1,'models':self.models})
        with self.assertRaises(ValueError): compare_and_save(self.vault,0,self.models)
        with self.assertRaises(ValueError): compare_and_save(self.vault,1,{'text':'sk-secret','image':self.models['image']})
        self.assertEqual(read(self.vault)['revision'],1)

    def test_concurrent_same_revision_allows_one(self):
        def save(_):
            try: return compare_and_save(self.vault,0,self.models)
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(save,range(2)))
        self.assertEqual(results.count(1),1)
        self.assertEqual(results.count(None),1)

    def test_distinct_bearer_and_no_persisted_plaintext(self):
        create_reader_token(self.vault)
        token=self.vault.read(READER_SLOT)['token']
        self.assertGreaterEqual(len(token),43)
        with self.assertRaises(ValueError): create_reader_token(self.vault)
        self.assertTrue(authenticate(self.vault,'Bearer '+token))
        self.assertFalse(authenticate(self.vault,'Bearer '+'x'*43))
        with open(self.vault.path,'rb') as f: self.assertNotIn(token.encode(),f.read())

    def test_reader_provision_is_two_phase_and_bound_to_generated_token(self):
        create_reader_token(self.vault)
        class Gateway:
            def __init__(inner):
                inner.model_reader_id='ReaderID123'
                inner.model_reader_probe=lambda provider, ident: True
                inner.fence=Fence(os.path.join(self.tmp.name,'fence.db'),b'x'*32)
                inner.fence.arm(lambda:True)
            def ready(inner): return inner.fence.is_ready()
            def token_reader(inner): return 'n'*32
        gateway=Gateway(); calls=[]
        def transport(provider, ident, token, key, proposal):
            calls.append((provider,ident,proposal))
            return 'accepted_unconfirmed'
        provision=ReaderProvision(self.vault,gateway,transport)
        proposal=provision.prepare('owner-identity-example')
        self.assertEqual(calls,[])
        self.assertEqual(provision.confirm('owner-identity-example',proposal),'accepted_unconfirmed')
        self.assertEqual(calls,[('reader','ReaderID123',proposal)])
        with self.assertRaises(PermissionError): provision.confirm('owner-identity-example',proposal)


if __name__ == '__main__': unittest.main()
