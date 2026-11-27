import hashlib
import io
import unittest
from unittest.mock import patch

import restore_disk as disk


NAME = "wbozon-20261127T013053Z-0123456789ab.tar.gz.age"
CONTENTS = b"synthetic encrypted backup"
META = {"name": NAME, "type": "file", "size": len(CONTENTS), "md5": hashlib.md5(CONTENTS).hexdigest()}
SETTINGS = {"disk_folder": "disk:/DB/backups", "token": "synthetic-token"}


class Response:
    def __init__(self, body=CONTENTS, status=200, location=None):
        self.status, self.body, self.location = status, io.BytesIO(body), location

    def read(self, size):
        return self.body.read(size)

    def getheader(self, name):
        return self.location if name == "Location" else None


class Connection:
    responses = []
    calls = []

    def __init__(self, host, port, **kwargs):
        self.host = host

    def request(self, method, path, headers):
        self.calls.append((self.host, method, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class RestoreDiskTests(unittest.TestCase):
    def setUp(self):
        Connection.responses = []
        Connection.calls = []

    def test_list_only_recognized_backups_in_reverse_date_order(self):
        older = dict(META, name="wbozon-20261126T013053Z-0123456789ab.tar.gz.age")
        page = {"_embedded": {"items": [older, {"name": "other.txt"}, META]}}
        with patch.object(disk, "settings", return_value=SETTINGS), patch.object(disk.backup, "json_request", return_value=page):
            self.assertEqual([row["name"] for row in disk.list_backups({})], [NAME, older["name"]])

    def test_download_checks_content_and_never_sends_oauth_to_download_host(self):
        link = {"method": "GET", "templated": False, "href": "https://downloader.disk.yandex.ru/secret-url"}
        Connection.responses = [Response()]
        with patch.object(disk, "settings", return_value=SETTINGS), \
             patch.object(disk.backup, "json_request", side_effect=[META, link, META]), \
             patch.object(disk.http.client, "HTTPSConnection", Connection):
            out = io.BytesIO()
            self.assertEqual(disk.download({}, NAME, out), disk._metadata(META))
        self.assertEqual(out.getvalue(), CONTENTS)
        self.assertEqual(Connection.calls, [("downloader.disk.yandex.ru", "GET", {"Accept": "application/octet-stream"})])

    def test_download_rejects_redirect_to_untrusted_host(self):
        link = {"method": "GET", "templated": False, "href": "https://downloader.disk.yandex.ru/file"}
        Connection.responses = [Response(status=302, location="https://example.com/file")]
        with patch.object(disk, "settings", return_value=SETTINGS), \
             patch.object(disk.backup, "json_request", side_effect=[META, link]), \
             patch.object(disk.http.client, "HTTPSConnection", Connection):
            with self.assertRaisesRegex(disk.RestoreDiskError, "invalid_download_url"):
                disk.download({}, NAME, io.BytesIO())
        self.assertEqual(len(Connection.calls), 1)

    def test_rejects_corrupt_content_and_changed_metadata(self):
        link = {"method": "GET", "templated": False, "href": "https://downloader.disk.yandex.ru/file"}
        for response, second, message in ((Response(body=b"broken"), META, "download_digest_mismatch"),
                                          (Response(), dict(META, md5="0" * 32), "backup_changed_during_download")):
            Connection.responses = [response]
            with patch.object(disk, "settings", return_value=SETTINGS), \
                 patch.object(disk.backup, "json_request", side_effect=[META, link, second]), \
                 patch.object(disk.http.client, "HTTPSConnection", Connection):
                with self.assertRaisesRegex(disk.RestoreDiskError, message):
                    disk.download({}, NAME, io.BytesIO())

    def test_rejects_path_injection_before_request(self):
        with patch.object(disk, "settings", side_effect=AssertionError("unexpected disk request")):
            with self.assertRaisesRegex(disk.RestoreDiskError, "invalid_filename"):
                disk.download({}, "../secret", io.BytesIO())


if __name__ == "__main__":
    unittest.main()
