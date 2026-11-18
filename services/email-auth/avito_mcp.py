"""Seller-authorized, read-only Avito reports. No caller-supplied URLs or account IDs."""
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit

import finance_gate
import market_transport


BASE = "https://api.avito.ru"
CONTRACTS = json.loads(Path(__file__).with_name("avito_contracts.json").read_text(encoding="utf-8"))
MAX_REPORT = 1024 * 1024
SAFE_PATH_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SENSITIVE_FIELDS = {
    "token", "accesstoken", "refreshtoken", "apikey", "secret", "clientsecret", "authorization",
    "password", "login", "clientid", "email", "reportemail", "phone", "phones", "address", "coordinates",
    "addressdetails", "deliveryaddress", "recipient", "buyerinfo", "buyercontacts", "courierinfo",
    "phonenumber", "feedsdata",
    "feedurls", "webhook", "webhookurl",
}


def _check(value, schema):
    kind = schema["type"]
    if kind == "object":
        fields = schema.get("properties", {})
        if not isinstance(value, dict) or set(value) - fields.keys() or set(schema.get("required", ())) - value.keys():
            raise ValueError("Invalid object fields")
        for name, child in value.items():
            _check(child, fields[name])
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 1) <= len(value) <= schema["maxItems"]:
            raise ValueError("Invalid array")
        for item in value:
            _check(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 1) <= len(value) <= schema["maxLength"] or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid text")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError("Invalid parameter format")
        if schema.get("format") == "date":
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("Expected ISO date")
        elif schema.get("format") == "date-time":
            if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError("Expected timestamp with timezone")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Expected boolean")
    elif kind in ("integer", "number"):
        if (type(value) not in ((int,) if kind == "integer" else (int, float))
                or not schema.get("minimum", -(2**63)) <= value <= schema.get("maximum", 2**63-1)
                or not math.isfinite(value)):
            raise ValueError("Invalid number")
    else:
        raise ValueError("Unsupported parameter")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Unknown parameter value")


def input_schema(name):
    op = CONTRACTS[name]
    return {"type": "object", "properties": op["properties"], "required": op.get("required", []),
            "additionalProperties": False}


def validate(name, arguments):
    _check(arguments, input_schema(name))
    for key in CONTRACTS[name].get("path_params", ()):
        if not SAFE_PATH_VALUE.fullmatch(str(arguments[key])):
            raise ValueError("Invalid path parameter")
    for first, last in (("dateFrom", "dateTo"), ("dateTimeFrom", "dateTimeTo")):
        if first in arguments and last in arguments:
            a, b = arguments[first], arguments[last]
            if CONTRACTS[name]["properties"][first].get("format") == "date-time":
                a, b = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (a, b))
            if a > b:
                raise ValueError("Invalid date range")


def _request(method, path, *, token=None, form=None, query=(), body=None):
    url = BASE + path + ("?" + urlencode(query, doseq=True) if query else "")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        payload = urlencode(form).encode("ascii")
    elif body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return urllib.request.Request(url, method=method, data=payload, headers=headers)


def build_read(name, args, token, account):
    validate(name, args)
    op = CONTRACTS[name]
    path = op["path"]
    if "{user_id}" in path:
        if type(account) is not int or account <= 0:
            raise ValueError("Missing seller identity")
        path = path.replace("{user_id}", str(account))
    for key in op.get("path_params", ()):
        path = path.replace("{"+key+"}", quote(str(args[key]), safe=""))
    query = []
    args = {**{key: prop["default"] for key, prop in op["properties"].items() if "default" in prop}, **args}
    for key in op.get("query", ()):
        if key not in args:
            continue
        value = args[key]
        if type(value) is bool:
            value = str(value).lower()
        elif isinstance(value, list) and name == "avito_get_chats_v2":
            value = ",".join(map(str, value))
        query.append((key, value))
    body = {k: args[k] for k in op.get("body", ()) if k in args} if op.get("json_body") else None
    return _request(op["method"], path, token=token, query=query, body=body)


def _unsafe_link(value):
    if not isinstance(value, str) or not value.lower().startswith(("http://", "https://")):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme != "https" or url.hostname not in ("avito.ru", "www.avito.ru")
                or url.netloc != url.hostname or bool(url.query or url.fragment))
    except ValueError:
        return True


def _project(value, parent=()):
    if isinstance(value, list):
        return [_project(item, parent) for item in value if not _unsafe_link(item)]
    if isinstance(value, dict):
        data = {}
        for key, item in value.items():
            field = re.sub(r"[^a-z0-9]", "", key.lower())
            pickup_address = (parent == ("delivery", "terminalinfo") and field == "address" and isinstance(item, str))
            if ((field in SENSITIVE_FIELDS and not pickup_address)
                    or field.endswith(("password", "secret", "token", "phone", "email"))
                    or _unsafe_link(item)):
                continue
            data[key] = _project(item, (*parent[-1:], field))
        return data
    return value


def _pagination(name, args, data):
    op = CONTRACTS[name]
    if name == "avito_list_orders_by_user_v1" and isinstance(data, dict):
        requested = args.get("pagination", {})
        page = requested.get("page", 1)
        info = data.get("pagination")
        more = info.get("hasNextPage") if isinstance(info, dict) else None
        return {"parameter": "pagination.page", "current": page, "limit": requested.get("perPage", 20),
                "has_more": more if type(more) is bool else None, "next": page+1 if more is True else None}
    options = op.get("query", []) + op.get("body", [])
    step = next((k for k in ("perPage", "per_page", "limit") if k in options), None)
    marker = next((k for k in ("page", "offset") if k in options), None)
    if not marker or not step:
        return None
    size = args.get(step, op["properties"][step].get("default"))
    current = args.get(marker, 1 if marker == "page" else 0)
    collection = data if isinstance(data, list) else next(
        (data[k] for k in ("chats","orders","reviews","resources","items","uploads") if isinstance(data.get(k), list)), None)
    more = data.get("hasMore") if isinstance(data, dict) else None
    if isinstance(data, dict):
        meta = data.get("meta")
        if isinstance(meta, dict) and type(meta.get("pages")) is int:
            more = current < meta["pages"]
        if name == "avito_get_reviews_v1" and type(data.get("total")) is int and collection is not None:
            more = current + len(collection) < data["total"]
        if name == "avito_item_analytics" and isinstance(data.get("result"), dict):
            summary = data["result"]
            collection = summary.get("groupings")
            if isinstance(collection, list) and type(summary.get("dataTotalCount")) is int:
                more = current + len(collection) < summary["dataTotalCount"]
    if type(more) is not bool:
        more = False if collection is not None and size and len(collection) < size else None
    can_continue = more is True or (more is None and size and collection is not None and len(collection) >= size)
    next_value = current + (1 if marker == "page" else size) if can_continue and (marker == "page" or size) else None
    maximum = op["properties"][marker].get("maximum")
    result = {"parameter":marker, "current":current, "limit":size, "has_more":more, "next":next_value}
    if next_value is not None and maximum is not None and next_value > maximum:
        result.update(next=None, reason="offset_limit_reached")
    return result


def read(name, args, creds, directory, deadline):
    validate(name, args)
    client = creds.get("client_id")
    secret = creds.get("client_secret")
    if not all(isinstance(v,str) and 1 <= len(v) <= 2048 for v in (client, secret)):
        raise ValueError("Missing Avito application credentials")
    wire = market_transport.Wire("avito", {"client_id":client,"client_secret":secret}, directory, deadline)
    gate_dir = str(Path(directory).parent / ".avito-method-gates")
    seller = hashlib.sha256(client.encode()).hexdigest()

    def fetch(operation, request):
        def open_response():
            remaining = deadline - time.monotonic()
            if remaining < 0.2:
                raise finance_gate.LocalPreparationTimeout("Read deadline reached")
            return wire.opener.open(request, timeout=min(6, remaining))
        response = finance_gate.run(gate_dir, seller+":"+operation, open_response, (client, secret))
        with response:
            raw = response.read(MAX_REPORT + 1)
        if len(raw) > MAX_REPORT:
            raise finance_gate.ProviderResponseTooLarge("Avito response too large")
        try:
            def bad_number(_):
                raise ValueError("Invalid number")
            return json.loads(raw, parse_float=str, parse_constant=bad_number,
                              object_pairs_hook=market_transport._no_duplicates)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise finance_gate.ProviderResponseInvalid("Invalid Avito JSON") from exc

    with wire.progress("avito-oauth-client-v1", {}) as (saved, save):
        token = (saved or {}).get("token")
        if not token:
            grant = fetch("token", _request("POST", "/token", form={"grant_type":"client_credentials",
                          "client_id":client, "client_secret":secret}))
            if not isinstance(grant, dict):
                raise finance_gate.ProviderResponseInvalid("Invalid Avito authorization")
            token, expires = grant.get("access_token"), grant.get("expires_in")
            if (not isinstance(token,str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,4096}",token)
                    or type(expires) is not int or expires < 1 or str(grant.get("token_type","Bearer")).lower() != "bearer"):
                raise finance_gate.ProviderResponseInvalid("Invalid Avito authorization")
            save({"token":token}, max(0, min(expires, 86400)-60))
    path = CONTRACTS[name]["path"]
    account = None
    try:
        if "{user_id}" in path:
            with wire.progress("avito-verified-profile-v1",{}) as (stored, save):
                account = (stored or {}).get("id")
                if not account:
                    profile = fetch("avito_get_user_info_self", build_read("avito_get_user_info_self",{},token,None))
                    account = profile.get("id") if isinstance(profile,dict) else None
                    if type(account) is not int or account <= 0:
                        raise finance_gate.ProviderResponseInvalid("Avito seller identity missing")
                    save({"id":account},3600)
        raw = fetch(name, build_read(name,args,token,account))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            with wire.progress("avito-oauth-client-v1", {}) as (current, save):
                if (current or {}).get("token") == token: save({}, 1)
        raise
    if not isinstance(raw,(dict,list)):
        raise finance_gate.ProviderResponseInvalid("Unexpected Avito report")
    if name == "avito_get_user_info_self" and (not isinstance(raw,dict) or type(raw.get("id")) is not int or raw["id"] <= 0):
        raise finance_gate.ProviderResponseInvalid("Avito seller identity missing")
    data = _project(raw)
    encoded=json.dumps(data,ensure_ascii=False)
    if any(json.dumps(v, ensure_ascii=False)[1:-1] in encoded for v in (secret, token)):
        raise finance_gate.ProviderResponseInvalid("Unsafe Avito report")
    result = {"data":data, "data_as_of":datetime.now(timezone.utc).isoformat(),
              "read_only":True, "untrusted_data":True, "source":CONTRACTS[name]["path"]}
    if isinstance(data, dict) and (data.get("degraded") is True or data.get("errors")):
        result.update(status="partial", complete=False)
    page = _pagination(name, args, data)
    if page: result["pagination"] = page
    if name == "avito_get_user_info_self": result["items"] = [data]
    return result
