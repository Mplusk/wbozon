"""Checkpoint exact-article FBO/FBS scans before reading accruals under the finance gate."""

import time
import ozon_posting_status


def active(args):
    return isinstance(args, dict) and args.get("discover_postings") is True


def validate(operation, args):
    allowed = {"offer_id", "date_from", "date_to", "discover_postings", "include_types", "batch"}
    if not active(args) or set(args) - allowed or not {"offer_id", "date_from", "date_to"} <= args.keys():
        raise ValueError("Discovery requires an exact offer and both dates")
    if (
        type(args.get("include_types", True)) is not bool
        or type(args.get("batch", 1)) is not int
        or not 1 <= args.get("batch", 1) <= 500
    ):
        raise ValueError("Invalid finance batch")
    ozon_posting_status.validate_args(
        dict(scheme="FBO", article=args["offer_id"], date_from=args["date_from"], date_to=args["date_to"])
    )


def read(operation, args, wire, legacy):
    validate(operation, args)
    offer = args["offer_id"]
    period = {k: args[k] for k in ("date_from", "date_to")}
    with wire.progress("article-postings-v2", [offer, period]) as (progress, save):
        state = progress or {"next": {"FBO": "", "FBS": ""}, "seen": {"FBO": [], "FBS": []}, "numbers": []}
        for scheme in ("FBO", "FBS"):
            for _ in range(10):
                cursor = state["next"][scheme]
                if cursor is None or time.monotonic() + 2 >= wire.deadline:
                    break
                page = legacy(
                    "ozon_posting_candidates",
                    dict(period, scheme=scheme, article=offer, limit=100, cursor=cursor),
                )
                ids, marker = page["posting_numbers"], page["next_cursor"]
                if (
                    set(ids) & set(state["numbers"])
                    or marker is not None
                    and marker in state["seen"][scheme]
                ):
                    return {"status": "ambiguous_pagination", "complete": False}
                state["numbers"].extend(ids)
                if len(state["numbers"]) > 10000:
                    return {"status": "narrow_period", "complete": False}
                state["next"][scheme] = marker
                if marker is not None:
                    state["seen"][scheme].append(marker)
                save(state, 1800 if all(v is None for v in state["next"].values()) else 86400)
        complete = all(v is None for v in state["next"].values())
        numbers = sorted(state["numbers"])
    meta = {
        "matched_postings": len(numbers),
        "discovery_complete": complete,
        "discovery_basis": "FBO_FBS_posting_filter_UTC",
        "finance_basis": "accrual_date",
        "period": period,
        "article": offer,
        "financial_completeness": "not_verified",
    }
    if not complete:
        return dict(meta, status="discovery_pending", complete=False)
    if not numbers:
        return dict(meta, status="no_matching_postings", complete=True, items=[])
    batch = args.get("batch", 1)
    selected = numbers[(batch - 1) * 100 : batch * 100]
    if not selected:
        raise ValueError("Batch outside selection")
    if time.monotonic() + 2 >= wire.deadline:
        return dict(meta, status="finance_pending", complete=False)
    query = dict(period, posting_numbers=selected, include_types=args.get("include_types", True))
    if operation == "ozon_article_summary":
        query["offer_id"] = offer
    else:
        import ozon_expansion

        skus = ozon_expansion.lookup(offer, wire)
        if not skus:
            return dict(meta, status="identity_unavailable", complete=False)
        query.update(skus=list(map(str, skus)), view="summary")
    report = legacy(operation, query)
    return dict(
        meta,
        status=report.get("status", "observed"),
        report=report,
        batch=batch,
        next_batch=batch + 1 if len(numbers) > batch * 100 else None,
        complete=batch == 1 and len(numbers) <= 100,
    )
