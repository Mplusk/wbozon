import contextlib
import io
import os
import unittest
from unittest import mock
import urllib.error
import urllib.request

import marketplace_check as check


class MarketplaceCheckTests(unittest.TestCase):
    def test_requests_are_fixed_and_read_only(self):
        with mock.patch.dict(os.environ, {"WB_API_TOKEN": "test-token", "OZON_CLIENT_ID": "123", "OZON_API_KEY": "test-key"}, clear=True):
            wb = check.make_request("wb")
            ozon = check.make_request("ozon")
        self.assertEqual((wb.full_url, wb.method), ("https://common-api.wildberries.ru/ping", "GET"))
        self.assertEqual((ozon.full_url, ozon.method), ("https://api-seller.ozon.ru/v3/product/list", "POST"))
        self.assertIn(b'"limit": 1', ozon.data)

    def test_redirect_is_rejected(self):
        req = urllib.request.Request("https://common-api.wildberries.ru/ping", headers={"Authorization": "secret"})
        self.assertIsNone(check.NoRedirect().redirect_request(req, None, 302, "", {}, "https://example.org"))

    def test_secret_is_not_printed_when_request_fails(self):
        err = urllib.error.URLError("secret-token")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"WB_API_TOKEN": "secret-token"}, clear=True), \
             mock.patch("urllib.request.build_opener") as build, \
             contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            build.return_value.open.side_effect = err
            self.assertEqual(check.main(["wb"]), 1)
        self.assertNotIn("secret-token", output.getvalue())

    def test_invalid_credential_is_not_printed(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"WB_API_TOKEN": "secret\nvalue"}, clear=True), contextlib.redirect_stderr(output):
            self.assertEqual(check.main(["wb"]), 2)
        self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
