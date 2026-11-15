"""Synthetic tests: external MCP credentials never contact real services."""
import gc
import os
import secrets
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from external_keys import KeyStore, KeyLimitError, DuplicateRequestError, render_section, render_created


class ExternalKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'vault.db')
        self.store = KeyStore(self.path)

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def create(self, name='Assistant', at=1000):
        return self.store.create(name, secrets.token_urlsafe(16), at)

    def test_issue_auth_use_revoke_and_no_plaintext_at_rest(self):
        key_id, token = self.create()
        self.assertEqual(self.store.check_token(token), key_id)
        self.assertIsNone(self.store.check_token(token + 'x'))
        with open(self.path, 'rb') as stored:
            self.assertNotIn(token.encode(), stored.read())
        self.assertTrue(self.store.mark_used(key_id, 1100))
        self.assertTrue(self.store.mark_used(key_id, 1001))
        self.assertEqual(self.store.list_active()[0][3], 1100)
        self.assertTrue(self.store.revoke(key_id, 1200))
        self.assertIsNone(self.store.check_token(token))
        self.assertFalse(self.store.mark_used(key_id, 1300))
        self.assertFalse(self.store.revoke(key_id, 1301))
        self.assertEqual(self.store.list_active(), [])

    def test_duplicate_post_cannot_issue_another_secret(self):
        req = secrets.token_urlsafe(16)
        key_id, token = self.store.create('Client', req, 1000)
        with self.assertRaises(DuplicateRequestError):
            self.store.create('Client', req, 1001)
        self.assertEqual(len(self.store.list_active()), 1)
        self.assertTrue(self.store.revoke(key_id, 1002))
        with self.assertRaises(DuplicateRequestError):
            self.store.create('Client', req, 1003)
        self.assertIsNone(self.store.check_token(token))

    def test_creation_limit_is_serialized_across_workers(self):
        def create(index):
            # A competing SQLite writer may exceed the deliberately short 0.5s
            # production busy timeout on a loaded Windows CI worker. Retry the
            # same idempotency key, so a retry cannot create another credential.
            request_id = secrets.token_urlsafe(16)
            for attempt in range(10):
                try:
                    return self.store.create('Client '+str(index), request_id, 1000)
                except KeyLimitError:
                    return None
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or attempt == 9:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(create, range(12)))
        self.assertEqual(sum(item is not None for item in results), 10)
        self.assertEqual(len(self.store.list_active()), 10)
        old = next(item for item in results if item)
        self.assertTrue(self.store.revoke(old[0],1001))
        self.assertTrue(self.create())

    def test_untrusted_input_and_rendering_are_safe(self):
        for name in ('', ' trailing ', 'bad\nname', 'x'*61):
            with self.assertRaises(ValueError): self.create(name)
        for bad in ('', 'mcp_bad', '\u2603', 123, None):
            self.assertIsNone(self.store.check_token(bad))
        key_id, token = self.create('<script>alert(1)</script>')
        page = render_section(self.store, 'a'*32, 'https://3dprinta.shop')
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>', page)
        self.assertNotIn(token, page)
        self.assertIn('https://3dprinta.shop/mcp',page)
        self.assertIn(key_id,page)
        once = render_created('<script>alert(1)</script>', token, 'https://3dprinta.shop')
        self.assertIn(token,once)
        self.assertNotIn('<script>',once)
        self.assertNotIn(token,render_section(self.store, 'a'*32,'https://3dprinta.shop'))
        self.assertNotIn('disabled',render_section(self.store, 'a'*32,'https://3dprinta.shop'))

    def test_storage_can_be_reopened_without_schema_changes(self):
        key_id, token = self.create()
        other = KeyStore(self.path)
        self.assertEqual(other.check_token(token),key_id)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM external_mcp_keys').fetchone()[0],1)


if __name__ == '__main__': unittest.main()
