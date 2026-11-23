"""Verify a review's seller comments before and after one approved reply."""

import http.client
import urllib.error

import finance_gate


TOOL = "ozon_reply_to_review"
OPERATION = "ozon_leave_comment"
COMMENT_LIST = "/v1/review/comment/list"
COMMENT_CREATE = "/v1/review/comment/create"
BASE = "https://api-seller.ozon.ru"
MAX_ID = 128
MAX_TEXT = 8192  # The full text fits the existing owner approval view.
PAGE_SIZE = 100
MAX_PAGES = 10
READ_ERRORS = (OSError, http.client.HTTPException, ValueError, finance_gate.GateError)
REJECTED_HTTP = frozenset((400, 401, 403, 404, 405, 413, 415, 422, 429))


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= MAX_ID and value.isprintable()
            and not any(character.isspace() for character in value))


def validate(review_id, text):
    if not valid_id(review_id):
        raise ValueError("Invalid Ozon review ID")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
            or any(ord(ch) < 32 and ch not in "\t\n\r" or ord(ch) == 127 for ch in text)):
        raise ValueError("Invalid review reply text")
    text.encode("utf-8")


def is_reply_route(route):
    return route.get("market") == "ozon" and route.get("path") == COMMENT_CREATE


def seller_comments(request, review_id):
    """An empty final page is required to conclude that no seller replied."""
    if not valid_id(review_id):
        raise ValueError("Invalid Ozon review ID")
    offset = 0
    seen = set()
    for _ in range(MAX_PAGES):
        page = request("POST", BASE + COMMENT_LIST,
                       body={"review_id": review_id, "limit": PAGE_SIZE, "offset": offset, "sort_dir": "ASC"})
        comments = page.get("comments") if isinstance(page, dict) else None
        if not isinstance(comments, list) or len(comments) > PAGE_SIZE:
            raise finance_gate.ProviderResponseInvalid("Invalid Ozon comment page")
        if "offset" in page and (type(page["offset"]) is not int or
                                 page["offset"] not in (len(comments), offset + len(comments))):
            raise finance_gate.ProviderResponseInvalid("Unexpected Ozon comment offset")
        if not comments:
            return
        for comment in comments:
            ident = comment.get("id") if isinstance(comment, dict) else None
            if not valid_id(ident) or ident in seen or type(comment.get("is_owner")) is not bool:
                raise finance_gate.ProviderResponseInvalid("Unverifiable Ozon comment")
            if "review_id" in comment and comment["review_id"] != review_id:
                raise finance_gate.ProviderResponseInvalid("Unexpected Ozon review ID")
            seen.add(ident)
            if comment["is_owner"]:
                yield comment
        offset += len(comments)
    raise finance_gate.ProviderResponseInvalid("Ozon comment scan exceeded its limit")


def seller_reply_present(request, review_id):
    try:
        return next(seller_comments(request, review_id), None) is not None
    except READ_ERRORS as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        return None


def error_metadata(error):
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        delay = getattr(error, "retry_after_seconds", None)
        error.close()
        return {"http_status": code, **({"retry_after_seconds": delay} if code == 429 else {})}
    if isinstance(error, finance_gate.GateError):
        return {"retry_after_seconds": error.retry_after_seconds}
    return {}


def publish(request, submit, review_id, text):
    """Run with the owner journal's dispatch fence held. Submit at most once."""
    base = {"review_id": review_id, "comment_id": None, "publication_confirmed": False}
    try:
        existing = next(seller_comments(request, review_id), None)
    except READ_ERRORS as error:
        return dict(base, operation_status="held", reason="comment_check_unavailable", stage="precheck",
                    **error_metadata(error))
    if existing:
        return dict(base, operation_status="denied", reason="seller_reply_exists", stage="precheck",
                    existing_comment_id=existing["id"])

    metadata = {}
    try:
        sent = submit()
    except (finance_gate.LocalPreparationError, finance_gate.LocalPreparationTimeout):
        return dict(base, operation_status="held", reason="local_preparation", stage="create")
    except READ_ERRORS as error:
        metadata = error_metadata(error)
        if isinstance(error, finance_gate.GateError) and error.reason in ("busy", "cooldown", "retry_time_unknown"):
            return dict(base, operation_status="held", reason=error.reason, stage="create", **metadata)
        code = metadata.get("http_status")
        if code in REJECTED_HTTP:
            reason = "provider_access_denied" if code in (401, 403) else "provider_rejected"
            return dict(base, operation_status="denied", reason=reason, stage="create", **metadata)
        sent = {}
    if sent.get("operation_status") == "denied":
        return dict(base, operation_status="denied", reason="provider_rejected", stage="create",
                    http_status=sent.get("http_status"))

    payload = sent.get("data")
    candidate = payload.get("comment_id") if isinstance(payload, dict) else None
    comment_id = candidate if valid_id(candidate) else None
    try:
        comments = seller_comments(request, review_id)
        if comment_id is not None:
            match = next((comment for comment in comments if comment["id"] == comment_id), None)
        else:
            matches = [comment for comment in comments if comment.get("text") == text]
            match = matches[0] if len(matches) == 1 else None
        reason = "comment_not_found" if match is None else None
    except READ_ERRORS as error:
        metadata.update(error_metadata(error))
        match, reason = None, "verification_unavailable"

    if match is not None:
        comment_id = match["id"]
    published = match is not None and match.get("is_published") is True and match.get("is_rejected") is not True
    if match is not None and not published:
        reason = "comment_rejected" if match.get("is_rejected") is True else "publication_not_confirmed"
    return dict(base, comment_id=comment_id, publication_confirmed=published, comment_verified=match is not None,
                operation_status="accepted" if comment_id else "unknown", stage="verification", reason=reason,
                **metadata)
