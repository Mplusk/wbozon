import base64
import gc
import hashlib
import io
import json
import secrets
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app import LoginApp
from portal import Portal, Vault
import portal
import market_cooldown
import yandex_restore_admin as restore


ARCHIVE = "wbozon-20261127T013053Z-0123456789ab.tar.gz.age"
IDENTITY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-private-key-not-real\n-----END OPENSSH PRIVATE KEY-----\n"


def multipart(fields):
    boundary = "synthetic-test-boundary"
    body = bytearray()
    for key, value in fields:
        body.extend(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + key + "\"").encode())
        if key == "identity":
            body.extend(b'; filename="key"\r\nContent-Type: application/octet-stream')
        body.extend(b"\r\n\r\n")
        body.extend(value if isinstance(value, bytes) else value.encode())
        body.extend(b"\r\n")
    body.extend(("--" + boundary + "--\r\n").encode())
    return bytes(body), "multipart/form-data; boundary=" + boundary


class RestoreAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100000
        self.login = LoginApp(self.tmp.name + "/auth.sqlite", "x" * 32, "https://3dprinta.shop", clock=lambda: self.now)
        self.vault = Vault(self.tmp.name + "/vault.sqlite", b"v" * 32)
        self.portal = Portal(self.login, self.vault, hashlib.sha256(b"synthetic").hexdigest())
        self.sid, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.login.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (self.login.digest(self.sid), self.now + 43200, self.csrf))

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def request(self, path, body, content_type, *, cookie=True, origin=True):
        env = {"PATH_INFO": path, "REQUEST_METHOD": "POST", "HTTP_HOST": "3dprinta.shop",
               "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if cookie:
            env["HTTP_COOKIE"] = "__Host-sid=" + self.sid
        if origin:
            env["HTTP_ORIGIN"] = "https://3dprinta.shop"
        result = {}
        def start(status, headers):
            result.update(status=status, headers=dict(headers))
        result["body"] = b"".join(self.portal(env, start)).decode()
        return result

    def fields(self, **overrides):
        values = {"csrf": self.csrf, "request_id": secrets.token_urlsafe(16), "archive": ARCHIVE,
                  "confirm": "yes", "identity": IDENTITY}
        values.update(overrides)
        return list(values.items())

    def test_list_and_submit_use_fresh_owner_and_do_not_echo_key(self):
        with patch.object(restore, "broker", side_effect=[{"status": "ok", "backups": [{"name": ARCHIVE, "size": 1234}]},
                                                         {"status": "accepted"}]) as broker:
            body = urlencode({"csrf": self.csrf}).encode()
            listing = self.request("/settings/yandex-restore/list", body, "application/x-www-form-urlencoded")
            self.assertTrue(listing["status"].startswith("200"))
            self.assertIn("27.11.2026, 04:30 МСК", listing["body"])
            self.assertIn("Восстановить выбранную копию", listing["body"])
            body, typ = multipart(self.fields())
            result = self.request("/settings/yandex-restore/submit", body, typ)
            self.assertTrue(result["status"].startswith("202"))
            self.assertNotIn("synthetic-private-key", result["body"])
            self.assertEqual(broker.call_args.args[0]["archive"], ARCHIVE)
            self.assertEqual(base64.b64decode(broker.call_args.args[0]["identity"]), IDENTITY)
            self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def test_no_session_origin_bad_csrf_or_stale_session_reaches_broker(self):
        body, typ = multipart(self.fields())
        with patch.object(restore, "broker", side_effect=AssertionError("unexpected restore")):
            self.assertTrue(self.request("/settings/yandex-restore/submit", body, typ, cookie=False)["status"].startswith("401"))
            self.assertTrue(self.request("/settings/yandex-restore/submit", body, typ, origin=False)["status"].startswith("403"))
            bad, _ = multipart(self.fields(csrf="bad"))
            self.assertTrue(self.request("/settings/yandex-restore/submit", bad, typ)["status"].startswith("403"))
            self.now += 301
            self.assertTrue(self.request("/settings/yandex-restore/submit", body, typ)["status"].startswith("403"))

    def test_invalid_archive_duplicate_fields_and_missing_confirmation_are_refused(self):
        for values in (self.fields(archive="../escape"), self.fields(confirm="no"),
                       self.fields() + [("archive", ARCHIVE)], self.fields(identity=b"x" * 9000)):
            body, typ = multipart(values)
            with self.subTest(fields=values[0][0]), patch.object(restore, "broker", side_effect=AssertionError("unexpected restore")):
                result = self.request("/settings/yandex-restore/submit", body, typ)
                self.assertTrue(result["status"].startswith("503"))
                self.assertNotIn("synthetic-private-key", result["body"])

    def test_bad_listing_is_not_rendered_as_html(self):
        with self.assertRaises(restore.RestoreError):
            restore.select_page(self.csrf, [{"name": "<script>", "size": 1}])

    def test_resume_requires_fresh_session_csrf_and_explicit_confirmation(self):
        for values in ({"csrf": self.csrf}, {"csrf": self.csrf, "confirm": "no"}):
            with patch.object(restore, "broker", side_effect=AssertionError("unexpected resume")):
                result = self.request('/settings/yandex-restore/resume', urlencode(values).encode(), 'application/x-www-form-urlencoded')
                self.assertTrue(result['status'].startswith('503'))
        with patch.object(restore, 'broker', return_value={'status': 'ok'}) as broker:
            result = self.request('/settings/yandex-restore/resume', urlencode({'csrf': self.csrf, 'confirm': 'yes'}).encode(), 'application/x-www-form-urlencoded')
            self.assertTrue(result['status'].startswith('303'))
            broker.assert_called_once_with({'action': 'resume'})

    def test_hold_blocks_ozon_wb_and_avito_workers_before_dispatch(self):
        from pathlib import Path
        marker = Path(self.tmp.name) / 'hold'
        marker.touch()
        for operation in ('ozon_products', 'wb_prices', 'avito_get_chats_v2'):
            output = io.StringIO()
            with self.subTest(operation=operation), patch.object(market_cooldown, 'BOOTSTRAP_HOLD', marker), \
                    patch('sys.stdin', io.StringIO(json.dumps({'operation': operation}))), patch('sys.stdout', output):
                portal._provider_worker()
            self.assertEqual(json.loads(output.getvalue())['reason'], 'retry_time_unknown')

    def test_hold_explains_how_to_resume(self):
        from pathlib import Path
        marker = Path(self.tmp.name) / 'hold'
        marker.touch()
        with patch.object(market_cooldown, 'BOOTSTRAP_HOLD', marker):
            self.assertIn('Разрешить запросы к маркетплейсам', restore.provider_hold(self.csrf, True))
            self.assertIn('войдите заново', restore.provider_hold(self.csrf, False))


if __name__ == "__main__":
    unittest.main()
