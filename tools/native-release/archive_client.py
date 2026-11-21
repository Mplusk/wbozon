"""Run bounded archive reads through the portal's normal, unmarked ingress."""

import http.client
import json
from pathlib import Path
import re
import sys


def main():
    settings = json.loads(Path("/etc/wbozon-cd.json").read_text())
    token = Path("/etc/3dprinta-portal/api-client-token").read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise RuntimeError("Archive client token is unavailable")
    datasets = (
        "wb_orders",
        "ozon_sku_daily",
        "ozon_posting_sku_daily",
        "wb_sales",
        "wb_finance_report",
        "ozon_availability",
        "wb_availability",
        "wb_warehouse_daily",
        "wb_funnel_daily",
    )
    requests = (("calculator", "sync_cost_catalog", {}),) if sys.argv[1:] == ["--calculator-sync"] else (
        (dataset, "archive_market_day", {"dataset": dataset}) for dataset in datasets)
    for dataset, operation, arguments in requests:
        connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=50)
        try:
            body = json.dumps({"operation": operation, "arguments": arguments})
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
            if reply.status == 401:
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
            print(dataset, "archive_request_incomplete", flush=True)
        finally:
            connection.close()


if __name__ == "__main__":
    main()
