import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from collectors import ozon


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n):
        return self.body[:n]


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, req, timeout):
        self.requests.append(req)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class OzonCollectorTests(unittest.TestCase):
    def test_collect_is_fixed_read_operation_and_marks_partial(self):
        opener = Opener(Response(b'{"result":{"items":[]}}'))
        with patch.dict(os.environ, {"OZON_CLIENT_ID": "123", "OZON_API_KEY": "secret"}, clear=True):
            snapshot = ozon.collect_page(opener)
        self.assertEqual(len(opener.requests), 1)
        req = opener.requests[0]
        self.assertEqual((req.full_url, req.method), (ozon.SOURCE, "POST"))
        self.assertEqual(json.loads(req.data), {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1})
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["data"], {"result": {"items": []}})
        self.assertNotIn("secret", json.dumps(snapshot))

    def test_bad_json_and_oversized_response_fail_closed(self):
        with patch.dict(os.environ, {"OZON_CLIENT_ID": "123", "OZON_API_KEY": "secret"}, clear=True):
            for data in (b'not json', b'[]', b'x' * (ozon.MAX_RESPONSE_BYTES + 1)):
                with self.subTest(size=len(data)), self.assertRaises(ozon.CollectionError):
                    ozon.collect_page(Opener(Response(data)))

    def test_failures_do_not_print_credentials(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"OZON_CLIENT_ID": "123", "OZON_API_KEY": "secret"}, clear=True), \
             patch("urllib.request.build_opener", return_value=Opener(urllib.error.URLError("secret"))), \
             contextlib.redirect_stderr(output), tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ozon.main(["--output", str(Path(tmp) / 'capture.json')]), 1)
            self.assertFalse((Path(tmp) / 'capture.json').exists())
        self.assertNotIn("secret", output.getvalue())

    def test_save_is_private_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.json'
            ozon.save_new_snapshot(path, {"data": {}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                ozon.save_new_snapshot(path, {"data": {"changed": True}})
            self.assertEqual(json.loads(path.read_text()), {"data": {}})

    def test_redirect_is_not_followed(self):
        import urllib.request
        req = urllib.request.Request(ozon.SOURCE, headers={"Api-Key": "secret"})
        self.assertIsNone(ozon.NoRedirect().redirect_request(req, None, 302, "", {}, "https://example.org"))


if __name__ == "__main__":
    unittest.main()
