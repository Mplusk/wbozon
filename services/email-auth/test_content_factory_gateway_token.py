import unittest
from content_factory_gateway_token import save, render, SLOT


class TokenTests(unittest.TestCase):
    def test_vault_slot_and_no_display_of_key(self):
        class Vault:
            def save(self, name, value): self.saved=(name,value)
        vault=Vault()
        save(vault,'t'*32)
        self.assertEqual(vault.saved,(SLOT,{'api_key':'t'*32}))
        page=render('c'*32,True)
        self.assertIn('type="password"',page)
        self.assertNotIn('t'*32,page)

    def test_invalid_secret_and_session_rejected(self):
        class Vault:
            def save(self, *_): raise AssertionError()
        for value in ('short','x'*16+'\n'):
            with self.assertRaises(ValueError): save(Vault(),value)
        with self.assertRaises(ValueError): render('bad',True)


if __name__ == '__main__': unittest.main()
