"""Authenticated tool observations and credential metadata, without exposing credentials."""

from datetime import datetime, timezone
import market_history
from market_specs import result


WB_CATEGORIES = {
    "wb_content": "Контент",
    "wb_supply": "Поставки",
    "wb_statistics": "Статистика",
    "wb_analytics": "Аналитика",
    "wb_finance": "Финансы",
    "wb_advert": "Продвижение",
    "wb_feedback": "Вопросы и отзывы",
    "wb_documents": "Документы",
    "wb_prices_key": "Цены и скидки",
}
# WB documents these zero-based positions in the access token's s claim.
WB_CATEGORY_BITS = {"wb_content":1, "wb_analytics":2, "wb_prices_key":3, "wb_statistics":5,
                    "wb_advert":6, "wb_feedback":7, "wb_supply":10, "wb_documents":12, "wb_finance":13}
HOST_KEYS = {
    "supplies-api": "wb_supply",
    "statistics-api": "wb_statistics",
    "finance-api": "wb_finance",
    "feedbacks-api": "wb_feedback",
    "advert-api": "wb_advert",
    "advert-media-api": "wb_advert",
    "dp-calendar-api": "wb_prices_key",
    "seller-analytics-api": "wb_analytics",
    "content-api": "wb_content",
    "documents-api": "wb_documents",
    "discounts-prices-api": "wb_prices_key",
}
WB_TOOLS = {
    name: key
    for key, names in {
        "wb_finance": "wb_finance_report wb_balance",
        "wb_statistics": "wb_orders wb_sales",
        "wb_content": "wb_products",
        "wb_prices_key": "wb_prices",
        "wb_supply": "wb_supply_goods wb_supplies",
        "wb_feedback": "wb_feedbacks",
        "wb_analytics": "wb_funnel wb_paid_storage wb_stocks wb_region_sales wb_returns_report wb_acceptance_report wb_sales_summary wb_search_queries wb_warehouse_daily wb_funnel_daily wb_stocks_history wb_funnel_history wb_retentions wb_card_issues",
        "wb_advert": "wb_ads wb_ads_finance",
    }.items()
    for name in names.split()
}
COMPOSITE = {"supply_plan", "article_map", "unit_economics", "sales_drop_check", "mcp_health"}


def read(vault, directory, args, names, credentials):
    from functools import lru_cache
    from types import SimpleNamespace

    vault = SimpleNamespace(read=lru_cache(maxsize=32)(vault.read))
    db = market_history.History(directory)
    all_names = sorted(names)
    chosen = [args["tool_name"]] if "tool_name" in args else all_names
    if not set(chosen) <= set(all_names):
        raise ValueError("Unknown report")
    items = []
    for name in chosen:
        creds = credentials(vault, name)
        status = db.check(creds or None, name)
        items.append(dict(tool=name, category=WB_CATEGORIES.get(WB_TOOLS.get(name)), **status))
    tokens = []
    for key, label in {
        "wb": "WB общий",
        "ozon": "Ozon Seller",
        "performance": "Ozon Performance",
        **WB_CATEGORIES,
    }.items():
        dedicated = vault.read(key)
        acct = dedicated or (vault.read("wb") if key in WB_CATEGORIES else None)
        claims = market_history.claims(acct.get("api_token") if acct else None)
        exp = claims.get("exp")
        mask = claims.get("s")
        permitted = bool(mask & (1 << WB_CATEGORY_BITS[key])) if key in WB_CATEGORY_BITS and type(mask) is int else None
        stamp = (
            datetime.fromtimestamp(exp, timezone.utc).isoformat()
            if type(exp) is int and 0 < exp < 253402300800
            else None
        )
        tokens.append(
            dict(
                category=label,
                configured=bool(acct) and permitted is not False,
                credential_source=key if dedicated else "wb" if acct else None,
                category_allowed_by_token=permitted,
                expires_at=stamp,
                expiry_basis="token_claim" if stamp else None,
            )
        )
    archive = []
    from market_archive import DATASETS
    import json
    from pathlib import Path

    try:
        updates = json.loads(Path("/run/wbozon-market-updates/status.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        updates = {}

    for dataset in DATASETS:
        name = {"wb_availability": "wb_stocks", "ozon_availability": "ozon_stocks_analytics"}.get(
            dataset, dataset
        )
        account = credentials(vault, name)
        if not account:
            archive.append({"dataset": dataset, "status": "not_configured"})
            continue
        provider = dataset.split("_")[0]
        main = {k: v for k, v in account.items() if k != "category_keys"}
        owner = market_history.scope(provider, main)
        authorized = db.authorized(main, owner)
        coverage = db.coverage(owner, dataset) if authorized else {}
        product_scope = {}
        if dataset == "wb_funnel_daily" and authorized:
            products = db.wb_funnel_product_coverage(owner)
            product_scope = {"product_archive_count": len(products), "product_archives": products[:50]}
        archive.append(
            dict(
                dataset=dataset,
                days=len(coverage),
                first_day=min(coverage, default=None),
                last_day=max(coverage, default=None),
                **product_scope,
                **db.check(account, "archive_" + dataset),
            )
        )
    return result(
        items,
        tool_count=len(all_names),
        tokens=tokens,
        archive=archive,
        api_updates=updates,
        status_basis="last_observed_authorized_call_with_current_credentials",
        changelogs=["https://dev.wildberries.ru/release-notes", "https://docs.ozon.ru/api/seller/"],
        data_as_of=market_history.utc(),
        read_only=True,
        untrusted_data=True,
    )
