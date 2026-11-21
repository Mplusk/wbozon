"""Only fixed marketplace method templates may leave a deadline-bounded worker."""

import re
import sys
from urllib.parse import urlsplit
import urllib.request


SELLER_SOURCE_PATHS = {
    "common-api.wildberries.ru": {("GET", "/api/communications/v2/news"), ("GET", "/api/common/v1/subscriptions")},
    "feedbacks-api.wildberries.ru": {("GET", "/api/common/v1/rating"), ("GET", "/api/v1/questions")},
    "seller-analytics-api.wildberries.ru": {("GET", "/api/v1/analytics/banned-products/blocked"),
                                           ("POST", "/api/analytics/v2/item-rating")},
    "content-api.wildberries.ru": {("POST", "/api/content/v1/recommendations/list")},
    "discounts-prices-api.wildberries.ru": {("GET", "/api/v2/quarantine/goods")},
    "documents-api.wildberries.ru": {("GET", "/api/v1/documents/categories"), ("GET", "/api/v1/documents/list")},
}


def method(req):
    from market_transport import allowed_read

    parts = urlsplit(req.full_url)
    path = parts.path
    allowed = allowed_read(parts.netloc, req.get_method(), path)
    if (req.get_method(), path) in SELLER_SOURCE_PATHS.get(parts.netloc, ()):
        allowed = True
    if not allowed and parts.netloc == "api-seller.ozon.ru":
        import ozon_details, ozon_fbo, ozon_accrual_day, ozon_return_events, ozon_realization, ozon_unallocated

        fixed = (
            set(ozon_details.ENDPOINTS.values())
            | set(ozon_fbo.ENDPOINTS.values())
            | {"/v3/product/list", "/v5/product/info/prices", "/v4/posting/fbs/list"}
        )
        for mod in (ozon_accrual_day, ozon_return_events, ozon_realization, ozon_unallocated):
            fixed.add(urlsplit(getattr(mod, "ENDPOINT", "")).path)
        allowed = path in fixed
    if parts.netloc == "common-api.wildberries.ru" and path in ("/api/v1/tariffs/box", "/api/v1/tariffs/commission"):
        allowed = True
    if not allowed:
        return None
    path = re.sub(r"/tasks/[^/]+/", "/tasks/{task}/", path)
    path = re.sub(r"/supplies/[0-9]+", "/supplies/{id}", path)
    path = re.sub(r"/statistics/[a-fA-F0-9-]{32,36}$", "/statistics/{task}", path)
    return req.get_method() + " " + parts.hostname + path


def enable():
    original = urllib.request.OpenerDirector.open

    def traced(self, req, *args, **kwargs):
        label = method(req) if isinstance(req, urllib.request.Request) else None
        if label:
            print(label, file=sys.stderr, flush=True)
        return original(self, req, *args, **kwargs)

    urllib.request.OpenerDirector.open = traced


def timeout(operation, elapsed, trace):
    import finance_gate

    fields = (trace or b"").decode("ascii", "ignore").splitlines()
    last = fields[-1] if fields else None
    if not last or not re.fullmatch(r"(?:POST|GET) [a-z0-9.-]+/[A-Za-z0-9_/{}/.-]{1,160}", last):
        last = None
    guarded = operation in finance_gate.FINANCE_OPERATIONS or operation in {
        "wb_stocks",
        "wb_paid_storage",
        "wb_acceptance_report",
        "ozon_ads",
        "supply_plan",
        "unit_economics",
        "archive_market_day",
    }
    exc = TimeoutError("Marketplace request timed out")
    exc.diagnostic = {
        "provider_method": last,
        "tool": operation,
        "elapsed_seconds": round(elapsed, 2),
        "safe_to_retry": not guarded,
        "retry_after_seconds": None if guarded else 30,
        "retry_basis": "reconcile_gate_or_report_task" if guarded else "local_timeout_backoff",
    }
    return exc
