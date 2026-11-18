"""Track public API schemas, retaining a fingerprint and last successful fetch."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import urllib.error
import urllib.request

WB = "https://dev.wildberries.ru/api/swagger/yaml/ru/"
SOURCES = {
    "ozon_seller": "https://docs.ozon.com/api/seller/swagger.json",
    **{
        doc: WB + doc + ".yaml"
        for doc in (
            "07-orders-fbw",
            "08-promotion",
            "10-rates",
            "11-analytics",
            "12-reports",
            "13-finances",
        )
    },
}


def write(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fchmod(f.fileno(), 0o644)
        os.fsync(f.fileno())
    os.replace(f.name, path)


def main():
    private = Path(os.environ["STATE_DIRECTORY"]) / "observations.json"
    public = Path(os.environ["RUNTIME_DIRECTORY"]) / "status.json"
    records = json.loads(private.read_text()) if private.is_file() else {}
    for name, url in SOURCES.items():
        prev = records.get(name, {})
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            headers = {"User-Agent": "wbozon-schema-monitor/2"}
            if prev.get("etag"):
                headers["If-None-Match"] = prev["etag"]
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=18
            ) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024 or not (
                    b'"openapi"' in raw[:100000] or b"openapi:" in raw[:1000]
                ):
                    raise ValueError("Unexpected public schema")
                digest = hashlib.sha256(raw).hexdigest()
                prev.update(
                    changed_at=prev.get("changed_at") if prev.get("digest") == digest else stamp,
                    digest=digest,
                    etag=response.headers.get("ETag"),
                )
            status = "available"
        except urllib.error.HTTPError as exc:
            status = "unchanged" if exc.code == 304 and prev.get("digest") else "http_" + str(exc.code)
            exc.close()
        except (OSError, ValueError):
            status = "unavailable"
        prev.update(source=url, checked_at=stamp, status=status)
        if status in ("available", "unchanged"):
            prev["last_success"] = stamp
        records[name] = prev
        write(private, records)
        write(
            public,
            {
                k: {f: v.get(f) for f in ("source", "checked_at", "status", "last_success", "changed_at")}
                for k, v in records.items()
            },
        )


if __name__ == "__main__":
    main()
