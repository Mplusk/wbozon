"""Synthetic runtime checks. Never sends SMTP or requests seller APIs."""
import urllib.request
import urllib.error

def check():
    for path, method, expected in [('/', 'GET', 200), ('/settings', 'GET', 401), ('/api/read', 'POST', 401)]:
        req = urllib.request.Request('http://127.0.0.1:8000'+path, method=method,
                                     headers={'Host':'3dprinta.shop', 'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=2) as response: status=response.status
        except urllib.error.HTTPError as exc: status=exc.code
        assert status == expected, 'Unexpected readiness response'

if __name__ == '__main__': check()
