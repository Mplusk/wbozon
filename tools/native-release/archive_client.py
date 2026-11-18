"""Run bounded archive reads through the portal's normal, unmarked ingress."""

import http.client
import json
from pathlib import Path
import re


def main():
    settings = json.loads(Path("/etc/wbozon-cd.json").read_text())
    token = Path("/etc/3dprinta-portal/api-client-token").read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise RuntimeError("Archive client token is unavailable")
    datasets = (
        "wb_orders",
        "ozon_sku_daily",
        "wb_sales",
        "wb_finance_report",
        "ozon_availability",
        "wb_availability",
    )
    for dataset in datasets:
        connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=24)
        try:
            body = json.dumps({"operation": "archive_market_day", "arguments": {"dataset": dataset}})
            connection.request(
                "POST",
                "/api/read",
                body,
                {
                    "Host": settings["host"],
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                },
            )
            reply = connection.getresponse()
            raw = reply.read(2048)
            if reply.status in (400, 401, 404, 503):
                return
            status = json.loads(raw).get("status") if reply.status == 200 else str(reply.status)
            print(
                dataset,
                status
                if isinstance(status, str) and re.fullmatch(r"[a-z0-9_]{1,64}", status)
                else "unknown",
                flush=True,
            )
        except (OSError, ValueError, http.client.HTTPException):
            return
        finally:
            connection.close()


if __name__ == "__main__":
    main()
