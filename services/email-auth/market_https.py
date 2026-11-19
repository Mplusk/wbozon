"""Prepare verified marketplace TLS before dispatch and reuse trust roots in each worker."""
from functools import lru_cache
import os
import ssl
import urllib.request


@lru_cache(maxsize=2)
def _context(cafile, capath):
    # The arguments key the cache. OpenSSL applies the environment overrides
    # and its default CA directory with exactly its standard verification policy.
    return ssl.create_default_context()


class VerifiedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=_context(os.environ.get('SSL_CERT_FILE') or None, os.environ.get('SSL_CERT_DIR') or None))
