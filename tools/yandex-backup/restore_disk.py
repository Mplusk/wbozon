"""Bounded, read-only access to the configured Yandex Disk backup folder."""

import hashlib
import http.client
from pathlib import Path
import re
import ssl
from urllib.parse import urljoin, urlsplit

import backup


NAME = re.compile(r"wbozon-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.tar\.gz\.age\Z")
MAX_BACKUP = 512 * 1024 * 1024
PAGE_SIZE = 100
MAX_FILES = 2000


class RestoreDiskError(ValueError):
    pass


def settings(config):
    value = backup.load_settings(config["data_root"], config["credentials"])
    if not value:
        raise RestoreDiskError("backup_not_configured")
    identity = backup.resolve_folder_identity(value["public_url"])
    backup.verify_folder(value["token"], value["disk_folder"], identity)
    return value


def _metadata(item, name=None):
    if not isinstance(item, dict):
        raise RestoreDiskError("invalid_metadata")
    filename = item.get("name")
    size, md5 = item.get("size"), item.get("md5")
    if (not isinstance(filename, str) or not NAME.fullmatch(filename) or (name is not None and name != filename) or
            item.get("type") != "file" or type(size) is not int or not 0 < size <= MAX_BACKUP or
            not isinstance(md5, str) or not re.fullmatch(r"[a-f0-9]{32}", md5)):
        raise RestoreDiskError("invalid_metadata")
    return {"name": filename, "size": size, "md5": md5}


def list_backups(config):
    value = settings(config)
    result = []
    for offset in range(0, MAX_FILES, PAGE_SIZE):
        page = backup.json_request("/resources", {"path": value["disk_folder"], "offset": offset,
                                                 "limit": PAGE_SIZE}, value["token"])
        embedded = page.get("_embedded")
        if not isinstance(embedded, dict) or not isinstance(embedded.get("items"), list):
            raise RestoreDiskError("invalid_listing")
        entries = embedded["items"]
        if len(entries) > PAGE_SIZE:
            raise RestoreDiskError("invalid_listing")
        for item in entries:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and NAME.fullmatch(item["name"]):
                result.append(_metadata(item))
        if len(entries) < PAGE_SIZE:
            break
    else:
        raise RestoreDiskError("too_many_files")
    return sorted(result, key=lambda item: item["name"], reverse=True)


def _download_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise RestoreDiskError("invalid_download_url")
    try:
        url = urlsplit(value)
        hostname, port = url.hostname, url.port
    except ValueError:
        raise RestoreDiskError("invalid_download_url") from None
    if (url.scheme != "https" or not hostname or url.username or url.password or port not in (None, 443) or
            url.fragment or not re.fullmatch(r"[a-z0-9.-]+", hostname) or
            not (hostname.endswith(".yandex.ru") or hostname.endswith(".yandex.net"))):
        raise RestoreDiskError("invalid_download_url")
    return url


def download(config, filename, output):
    if not isinstance(filename, str) or not NAME.fullmatch(filename):
        raise RestoreDiskError("invalid_filename")
    value = settings(config)
    disk_path = value["disk_folder"].rstrip("/") + "/" + filename
    expected = _metadata(backup.json_request("/resources", {"path": disk_path}, value["token"]), filename)
    link = backup.json_request("/resources/download", {"path": disk_path}, value["token"])
    if link.get("method") != "GET" or link.get("templated") is not False:
        raise RestoreDiskError("invalid_download_response")
    url = _download_url(link.get("href"))
    digest, downloaded = hashlib.md5(), 0
    for _ in range(4):
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=45,
                                                 context=ssl.create_default_context())
        try:
            request_path = url.path + ("?" + url.query if url.query else "")
            connection.request("GET", request_path, headers={"Accept": "application/octet-stream"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                redirect = response.getheader("Location")
                if not redirect:
                    raise RestoreDiskError("invalid_redirect")
                url = _download_url(urljoin(url.geturl(), redirect))
                continue
            if response.status != 200:
                raise RestoreDiskError("download_failed")
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > expected["size"] or downloaded > MAX_BACKUP:
                    raise RestoreDiskError("download_size_mismatch")
                digest.update(chunk)
                output.write(chunk)
            break
        except (OSError, TimeoutError, http.client.HTTPException):
            raise RestoreDiskError("download_failed") from None
        finally:
            connection.close()
    else:
        raise RestoreDiskError("too_many_redirects")
    if downloaded != expected["size"] or digest.hexdigest() != expected["md5"]:
        raise RestoreDiskError("download_digest_mismatch")
    observed = _metadata(backup.json_request("/resources", {"path": disk_path}, value["token"]), filename)
    if observed != expected:
        raise RestoreDiskError("backup_changed_during_download")
    return expected
