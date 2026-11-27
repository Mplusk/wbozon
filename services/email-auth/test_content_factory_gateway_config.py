import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from content_factory_gateway_config import configure


class GatewayConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = os.path.join(self.tmp.name,'gateway')
        os.mkdir(self.directory)
        self.env = {'CF_GATEWAY_ENABLED':'1', 'CF_GATEWAY_STAGING_VERIFIED':'1',
                    'CF_GATEWAY_OWNER_SCOPE_CONFIRMED':'1', 'CF_GATEWAY_DATA_DIR':self.directory,
                    'CF_N8N_OPENAI_ID':'OpenID123', 'CF_N8N_GEMINI_ID':'GemID1234',
                    'CF_GATEWAY_HMAC_KEY_FILE':os.path.join(self.tmp.name,'hmac.key'),
                    'VAULT_KEY_FILE':os.path.join(self.tmp.name,'vault.key')}
        self.reader = lambda name, binary: b'v'*32 if name == 'VAULT_KEY_FILE' else b'k'*32
        class Vault:
            def read(self, name): return None
        self.vault = Vault()

    def tearDown(self): self.tmp.cleanup()

    def test_default_off_and_requires_explicit_staging_and_scope(self):
        self.assertEqual(configure({},self.vault,self.reader),(None,None,False))
        for missing in ('CF_GATEWAY_STAGING_VERIFIED','CF_GATEWAY_OWNER_SCOPE_CONFIRMED'):
            env=dict(self.env); env.pop(missing)
            with self.assertRaises(ValueError): configure(env,self.vault,self.reader)
        self.assertEqual(os.listdir(self.directory),[])

    def test_configured_gateway_starts_halted(self):
        control, inspector, setup = configure(self.env,self.vault,self.reader)
        self.assertTrue(setup)
        self.assertTrue(callable(inspector))
        self.assertFalse(control.ready())
        control2, _, _ = configure(self.env,self.vault,self.reader)
        self.assertFalse(control2.ready())

    def test_rejects_other_files_and_unrelated_database(self):
        with open(os.path.join(self.directory,'unrelated.txt'),'w') as f: f.write('x')
        with self.assertRaises(ValueError): configure(self.env,self.vault,self.reader)
        os.unlink(os.path.join(self.directory,'unrelated.txt'))
        with closing(sqlite3.connect(os.path.join(self.directory,'content-factory-gateway.sqlite3'))) as db:
            db.execute('CREATE TABLE unrelated(x)'); db.commit()
        with self.assertRaises(ValueError): configure(self.env,self.vault,self.reader)

    def test_key_must_be_distinct_and_full_length(self):
        env=dict(self.env, CF_GATEWAY_HMAC_KEY_FILE=self.env['VAULT_KEY_FILE'])
        with self.assertRaises(ValueError): configure(env,self.vault,self.reader)
        with self.assertRaises(ValueError): configure(self.env,self.vault,lambda *_: b'short')
        self.assertEqual(os.listdir(self.directory),[])


if __name__ == '__main__': unittest.main()
