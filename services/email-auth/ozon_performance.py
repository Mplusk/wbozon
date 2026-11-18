"""Ozon Performance OAuth and bounded product-level CPC statistics downloads."""

import csv
import io
import re
import urllib.error
import uuid
import zipfile
from datetime import date, datetime

from market_specs import direct_attribution, money, result, rows, validate

BASE = "https://api-performance.ozon.ru/api/client"
FILE_LIMIT = 24 * 1024 * 1024


def _normal(s):
    return " ".join(s.lower().replace("ё", "е").strip().split())


def _numeric(value):
    if value is None or not value.strip() or value.strip() in ("—", "-"):
        return None
    return str(money(value.replace("\xa0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")))


def _day(value):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    return date.fromisoformat(value).isoformat()


def _csv(raw, allowed, filename=None, period=None):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=";")
    header = None
    output = []
    campaign = None
    # A date in stencils16-02.csv is not a campaign number. Only an explicitly
    # labelled ID or an ID-only filename can identify a member of a multi-campaign ZIP.
    match = re.fullmatch(r"(?:campaign[-_ ])?([1-9]\d*)\.csv", (filename or "").rsplit("/", 1)[-1], re.I)
    file_id = match.group(1) if match else None
    for row in reader:
        if len(row) == 1 and ";" in row[0]:
            row = next(csv.reader([row[0]], delimiter=";"))
        normalized = [_normal(c) for c in row]
        if header is None and "sku" not in normalized:
            preamble = " ".join(normalized)
            match = re.search(r"(?:кампания|кампании)[^№;]{0,160}№\s*(\d+)\b", preamble)
            if match:
                if campaign is not None and campaign != match.group(1):
                    raise ValueError("Conflicting campaign identities")
                campaign = match.group(1)
            dates = re.search(
                r"период\s*[: ]*\s*(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})", preamble
            )
            if dates and period and tuple(_day(d) for d in dates.groups()) != period:
                raise ValueError("Unexpected advertising report period")
            continue
        if "sku" in normalized and (header is None or normalized == header):
            header = normalized
            if (
                len(set(header)) != len(header)
                or not {"sku", "показы", "клики", "заказы"} <= set(header)
                or not any(
                    k.startswith("расход") and ("ндс" in k or "руб" in k or "₽" in k) for k in header
                )
            ):
                raise ValueError("Unrecognised CPC export header")
            continue
        if not header or not row or not any(normalized) or normalized[0] in ("всего", "итого"):
            continue
        if len(row) != len(header):
            raise ValueError("Unknown advertising report row")
        fields = dict(zip(header, row))
        sku = fields.get("sku", "").strip()
        if not re.fullmatch("[1-9][0-9]{0,18}", sku):
            raise ValueError("Missing advertising SKU")

        def col(*keys):
            return next((fields[k] for k in keys if k in fields), None)

        # The export explicitly labels RUB. Preserve an unknown currency for any other header.
        spend_key = next(
            (k for k in header if k.startswith("расход") and ("ндс" in k or "руб" in k or "₽" in k)), None
        )
        if not spend_key or col("показы") is None or col("клики") is None or col("заказы") is None:
            raise ValueError("Unrecognised CPC export format")
        day = _day(col("дата", "день"))
        if day and period and not period[0] <= day <= period[1]:
            raise ValueError("Unexpected advertising day")
        item = {
            "sku": sku,
            "article": None,
            "nm_id": None,
            "date": day,
            "views": _numeric(col("показы")),
            "clicks": _numeric(col("клики")),
            "orders": _numeric(col("заказы")),
            "model_orders": _numeric(col("заказы модели")),
            "spend": _numeric(fields[spend_key]),
            "ordered_amount": _numeric(
                next((v for k, v in fields.items() if k.startswith("продажи,")), None)
            ),
            "model_ordered_amount": _numeric(
                next((v for k, v in fields.items() if k.startswith("продажи с заказов модели,")), None)
            ),
            "currency": "RUB" if "₽" in spend_key or "руб" in spend_key else None,
            "tax_basis": "including_vat" if "с ндс" in spend_key else None,
        }
        for key in ("views", "clicks", "orders", "model_orders"):
            number = money(item[key])
            if number is not None and (number < 0 or number != number.to_integral_value()):
                raise ValueError("Invalid advertising counter")
        output.append(item)
    if header is None:
        raise ValueError("Missing CPC export header")
    if campaign and file_id and file_id != campaign:
        raise ValueError("Conflicting campaign identities")
    identity = (
        campaign or file_id or (next(iter(allowed)) if len(allowed) == 1 and filename is None else None)
    )
    if identity not in allowed:
        raise ValueError("Unexpected or missing campaign identity")
    for item in output:
        item.update(
            campaign_id=identity,
            campaign_basis="csv_preamble"
            if campaign
            else "explicit_filename"
            if file_id
            else "single_campaign_request",
        )
    return identity, output


def parse_report(raw, campaigns, period=None):
    if not isinstance(raw, bytes) or len(raw) > FILE_LIMIT:
        raise ValueError("Invalid report")
    allowed = set(map(str, campaigns))
    if not raw.startswith(b"PK"):
        if len(campaigns) != 1:
            raise ValueError("Missing campaign identity")
        return _csv(raw, allowed, period=period)[1]
    output = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = [
            f
            for f in archive.infolist()
            if not f.is_dir()
            and not f.filename.startswith("__MACOSX/")
            and f.filename.lower().endswith(".csv")
        ]
        if not files or len(files) > 50 or sum(f.file_size for f in files) > FILE_LIMIT:
            raise ValueError("Invalid zipped report")
        seen = set()
        for entry in files:
            ident, items = _csv(archive.read(entry), allowed, filename=entry.filename, period=period)
            if ident in seen:
                raise ValueError("Duplicate campaign report")
            seen.add(ident)
            output.extend(items)
        if seen != allowed:
            raise ValueError("Incomplete campaign report")
    return output


def read(args, request, creds, *, allowed_skus=None):
    validate("ozon_ads", args)
    token_body = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "grant_type": "client_credentials",
    }
    token_url = BASE + "/token"
    auth = request("POST", token_url, body=token_body, token_request=True, ttl=3600)
    token = auth.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token):
        raise ValueError("Invalid Performance token")
    request.token = token
    try:
        return _statistics(args, request, allowed_skus)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and hasattr(request, "forget"):
            request.forget("POST", token_url, body=token_body)
        raise


def _statistics(args, request, allowed_skus):
    ids = args.get("campaign_ids")
    if not ids:
        raw = request("GET", BASE + "/campaign", query={"advObjectType": "SKU"}, ttl=1800)
        items = [
            {k: r.get(k) for k in ("id", "title", "state", "advObjectType", "paymentType")}
            for r in rows(raw.get("list", []))
        ]
        index, limit = args.get("offset", 0), args.get("limit", 100)
        return result(
            items[index : index + limit],
            index + limit if len(items) > index + limit else None,
            status="choose_campaign_ids",
        )
    params = {
        "campaigns": list(map(str, ids)),
        "dateFrom": args["date_from"],
        "dateTo": args["date_to"],
        "groupBy": "DATE",
    }
    create_url = BASE + "/statistics"
    task = args.get("task_id")
    if not task:
        created = request("POST", create_url, body=params, ttl=3600, interval=60, job=True)
        task = created.get("UUID")
    if not isinstance(task, str) or uuid.UUID(task).hex != task.replace("-", "").lower():
        raise ValueError("Invalid report ID")
    if hasattr(request, "bind_job"):
        request.bind_job(create_url, params, task, existing="task_id" in args)

    def pending(exc, phase):
        if exc.code != 429:
            raise exc
        seconds = exc.headers.get("Retry-After", "")
        exc.close()
        return result(
            [],
            complete=False,
            status="pending",
            phase=phase,
            task_id=task,
            retry_after_seconds=int(seconds) if seconds.isdigit() else None,
        )

    try:
        reported = request("GET", BASE + "/statistics/" + task, group="performance-status", interval=5)
    except urllib.error.HTTPError as exc:
        return pending(exc, "status")
    if reported.get("UUID", task) != task:
        raise ValueError("Unexpected status report identity")
    state = reported.get("state")
    if state in ("NOT_STARTED", "IN_PROGRESS"):
        return result([], complete=False, status="pending", task_id=task, retry_after_seconds=5)
    if state in ("OK", "ERROR") and hasattr(request, "finish_job"):
        request.finish_job(create_url, params, task, failed=state == "ERROR")
    if state != "OK":
        if state == "ERROR" and not hasattr(request, "finish_job") and hasattr(request, "forget"):
            request.forget("POST", create_url, body=params)
        return result([], complete=False, status="report_unavailable", provider_status=state, task_id=task)
    try:
        raw = request("GET", BASE + "/statistics/report", query={"UUID": task}, ttl=1800, binary=True)
    except urllib.error.HTTPError as exc:
        return pending(exc, "download")
    items = [
        r
        for r in parse_report(raw, ids, (args["date_from"], args["date_to"]))
        if ("sku" not in args or str(args["sku"]) == r["sku"])
        and (allowed_skus is None or r["sku"] in allowed_skus)
    ]
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    missing = any(
        row[k] is None for row in items for k in ("spend", "currency", "clicks", "views", "orders")
    )
    extra = (
        {"direct_attribution": direct_attribution(items, "sku", "orders")}
        if args.get("include_order_share")
        else {}
    )
    return result(
        items[offset : offset + limit],
        offset + limit if len(items) > offset + limit else None,
        task_id=task,
        **extra,
        complete=not missing and len(items) <= offset + limit,
        issues=["missing_report_values"] if missing else [],
        source="Ozon Performance",
        attribution_basis="product_rows_of_campaign_export",
        period_from=args["date_from"],
        period_to=args["date_to"],
    )
