"""Public argument contracts for the read-only warehouse, analytics and advertising tools."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


def integer(low, high):
    return {"type": "integer", "minimum": low, "maximum": high}


def choices(*values):
    return {"type": "string", "enum": list(values)}


def array(items, limit):
    return {"type": "array", "items": items, "minItems": 1, "maxItems": limit, "uniqueItems": True}


ID = {
    "type": ["integer", "string"],
    "pattern": "^[1-9][0-9]{0,18}$",
    "minimum": 1,
    "maximum": 9223372036854775807,
}
TEXT = {"type": "string", "minLength": 1, "maxLength": 256}
DATE = {"type": "string", "format": "date"}
DOWNLOAD_ID = {"type": "string", "pattern": r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", "maxLength": 36}
OFFSET = integer(0, 10000000)
FIELDS = {
    "article": TEXT,
    "nm_id": ID,
    "sku": ID,
    "skus": array(ID, 100),
    "offer_ids": array(TEXT, 100),
    "date_from": DATE,
    "date_to": DATE,
    "warehouse_id": ID,
    "cluster_id": ID,
    "supply_id": ID,
    "order_id": ID,
    "bundle_id": TEXT,
    "offset": OFFSET,
    "limit": integer(1, 100),
    "page": integer(1, 1000000),
    "cursor": {"type": ["string", "integer"], "minLength": 1, "maxLength": 2048, "minimum": 0},
    "mode": choices("summary", "details"),
    "status_ids": array(integer(1, 6), 6),
    "is_preorder": {"type": "boolean"},
    "states": array(TEXT, 12),
    "report_scope": choices("unanswered", "answered", "archive"),
    "review_status": choices("ALL","NEW","VIEWED","PROCESSED"),
    "include_count": {"type":"boolean"},
    "task_id": TEXT,
    "download_id": DOWNLOAD_ID,
    "wb_stock_history_id": DOWNLOAD_ID,
    "campaign_ids": array(ID, 50),
    "include_funnel": {"type": "boolean"},
    "include_order_share": {"type": "boolean"},
    "include_warehouses": {"type": "boolean"},
    "report": choices("auto", "campaigns", "statistics", "spend_ledger", "clusters", "bids", "balance"),
    "cluster_type": choices("CLUSTER_TYPE_OZON", "CLUSTER_TYPE_CIS"),
    "horizon_days": integer(1, 365),
    "lead_time_days": integer(0, 365),
    "safety_stock_percent": integer(0, 500),
    "bulk_order_min_units": integer(2, 100000),
    "min_batch_units": integer(1, 100000),
    "month": {"type":"string", "pattern":r"^[0-9]{4}-(?:0[1-9]|1[0-2])$", "maxLength":7},
    "cost_per_unit": {"type":"string", "pattern":r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$", "maxLength":19},
    "currency": {"type":"string", "pattern":r"^[A-Z]{3}$", "maxLength":3},
    "previous_from": DATE, "previous_to": DATE,
    "query": TEXT, "nm_ids": array(ID, 50), "vendor_codes": array(TEXT, 50),
    "group_by": choices("day", "week"),
    "retention_type": choices("all", "penalty", "deduction","measurement_penalty","measurement","antifraud"),
    "return_status": choices("archive", "active"),
    "snapshot_time": TEXT,
    "document": choices("spend", "payments", "documents"),
    "search_limit": choices("openCard", "addToCart", "openToCart", "orders", "cartToOrder"),
    "tool_name": {"type":"string", "maxLength":80},
    "probe_arguments":{"type":"object","additionalProperties":True},
    "refresh_capabilities": {"type":"boolean"},
    "archive": {"type":"boolean"},
    "date": DATE, "marketplace": choices("ozon", "wb", "site"),
    "product_status": choices("draft", "active", "archived"), "has_cost": {"type": "boolean"},
    "mapping": {
        "type": "object",
        "properties": {"ozon_offer_id": TEXT, "wb_vendor_code": TEXT, "wb_nm_id": ID,"avito_item_id":ID,"avito_article":TEXT},
        "required": ["ozon_offer_id", "wb_vendor_code"],
        "additionalProperties": False,
    },
}

# Parameters in the third column are mandatory; modes are available on every tool.
TOOLS = {
    "cost_card": ("Себестоимость, состав товара и покупные комплектующие по внутреннему артикулу или коду площадки на дату.",
                  "article marketplace date", "article"),
    "cost_list": ("Товары калькулятора с себестоимостью, статусом и привязками.",
                  "product_status has_cost marketplace query limit offset", ""),
    "cost_history": ("Версии расчёта себестоимости товара за период.",
                     "article marketplace date_from date_to limit offset", "article"),
    "cost_inbox": ("Предложения по товарам с привязками Ozon и WB, габаритами и журналом последних синхронизаций.",
                   "limit offset", ""),
    "wb_supplies": (
        "Поставки FBW за период по дате создания. Склады, даты, статус и коэффициент из деталей.",
        "date_from date_to status_ids warehouse_id limit offset article",
        "date_from date_to",
    ),
    "wb_supply_goods": (
        "Состав одной поставки WB, поступление в продажу, фактическая приёмка и короба.",
        "supply_id is_preorder article nm_id limit offset",
        "supply_id",
    ),
    "wb_stocks": (
        "Остатки WB по складам. Транзит к покупателю и возвраты выделены отдельно. Незавершённый отчёт можно продолжить.",
        "article nm_id task_id limit offset",
        "",
    ),
    "wb_orders": (
        "Заказы по дате, складу и региону. За полностью архивированные закрытые дни возвращает подтверждённые записи и время снимка. Для сводки по артикулу за 90 дней передайте group_by=day и limit=100. archive=false выбирает текущую ленту в доступном периоде.",
        "date_from date_to article nm_id cursor limit offset archive group_by",
        "date_from date_to",
    ),
    "wb_sales": (
        "События продаж и возвратов WB за период, по дням и складам.",
        "date_from date_to article nm_id cursor limit offset archive",
        "date_from date_to",
    ),
    "wb_acceptance_slots": (
        "Доступность опубликованных WB коэффициентов приёмки на 14 дней. include_warehouses добавляет справочник складов FBW.",
        "warehouse_id include_warehouses",
        "",
    ),
    "ozon_supply_orders": (
        "Заявки FBO с деталями, складами и интервалами. Период относится к началу таймслота.",
        "date_from date_to states limit cursor offset article",
        "",
    ),
    "ozon_supply_items": (
        "Заявленный состав FBO. Для фактической приёмки укажите также order_id и supply_id: данные из актов возвращаются отдельно.",
        "bundle_id order_id supply_id article sku limit cursor",
        "bundle_id",
    ),
    "ozon_stocks": (
        "Остаток и резерв Ozon по SKU и схеме отгрузки. Список складов относится ко всей строке.",
        "article offer_ids sku cursor limit",
        "",
    ),
    "ozon_stocks_by_cluster": (
        "Текущие остатки и транзит по складам и кластерам Ozon. С датами возвращает сохранённые снимки и пропуски по дням UTC.",
        "sku skus cluster_id warehouse_id article date_from date_to offset limit",
        "",
    ),
    "ozon_sku_daily": (
        "Ежедневные заказанные единицы и сумма по SKU. include_funnel добавляет просмотры и корзину. За дни с пропуском аналитики архив показывает количество и стоимость товаров в отправлениях FBO и FBS отдельными полями posting_units и posting_item_value.",
        "date_from date_to sku article offset limit include_funnel archive",
        "date_from date_to",
    ),
    "wb_finance_report": (
        "Детализация реализации WB с расходами и показателями на проданную единицу по артикулу и валюте. Категория Финансы.",
        "date_from date_to article nm_id cursor limit offset archive",
        "date_from date_to",
    ),
    "wb_paid_storage": (
        "Фактические расходы на хранение WB, отчёт за период до 8 дней.",
        "date_from date_to article nm_id task_id limit offset",
        "date_from date_to",
    ),
    "wb_acceptance_report": (
        "Платная приёмка WB по товару и поставке за период до 31 дня.",
        "date_from date_to article nm_id task_id limit offset",
        "date_from date_to",
    ),
    "ozon_cash_flow": (
        "Финансовый отчёт Ozon по расчётным периодам. Фактические выплаты выделены по валютам.",
        "date_from date_to page limit",
        "date_from date_to",
    ),
    "ozon_fbo_postings_geo": (
        "Отправления FBO по дате, складу и кластерам, с количествами отдельных SKU.",
        "date_from date_to sku article cursor limit",
        "date_from date_to",
    ),
    "ozon_clusters": ("Кластеры Ozon. По умолчанию показывает количество складов. Для списка складов укажите cluster_id и mode=details.", "cluster_type cluster_id offset limit", ""),
    "wb_funnel": (
        "Воронка WB по дням или календарным неделям. Для продолжения передайте next_cursor в cursor. Длинный период доступен по дням через агрегаты WB. Готовый CSV DETAIL_HISTORY_REPORT можно прочитать по download_id и group_by=day.",
        "date_from date_to article nm_id nm_ids group_by limit offset cursor download_id",
        "date_from date_to",
    ),
    "wb_ads": (
        "Кампании WB, списания и статистика по товару. include_order_share сопоставляет прямые рекламные единицы с заказанными единицами воронки WB.",
        "date_from date_to campaign_ids article nm_id report limit offset cursor task_id include_order_share",
        "date_from date_to",
    ),
    "ozon_ads": (
        "Кампании и отчёты CPC по SKU и периоду через Ozon Performance. include_order_share сверяет прямые заказы с уникальными order_id FBO и FBS по SKU через Seller API.",
        "date_from date_to campaign_ids article sku task_id limit offset include_order_share",
        "date_from date_to",
    ),
    "wb_feedbacks": (
        "Отзывы и оценки в выбранном разделе WB. Средний рейтинг рассчитывается по показанной выборке.",
        "date_from date_to article nm_id report_scope limit offset",
        "date_from date_to",
    ),
    "ozon_reviews": (
        "Отзывы и оценки Ozon по периоду и SKU. Требуется доступ к управлению отзывами.",
        "date_from date_to sku article limit cursor review_status include_count",
        "date_from date_to",
    ),
    "ozon_rating": ("Текущие группы рейтингов продавца Ozon и индекс локализации по датам.", "", ""),
    "supply_plan": (
        "План по складам и кластерам с учётом срока доставки и страхового запаса. Отправления одного заказа Ozon объединяются по номеру. min_batch_units — минимальная партия в штуках, по умолчанию 2. Для поставок по одной штуке укажите 1.",
        "article horizon_days mapping lead_time_days safety_stock_percent bulk_order_min_units min_batch_units wb_stock_history_id",
        "article horizon_days",
    ),
    "article_map": ("Точные артикулы и все найденные SKU Ozon и nmID WB по артикулу, названию или настроенной паре. Неоднозначные совпадения показаны отдельно.", "article mapping", "article"),
    "unit_economics": ("Расчёт по артикулу за месяц. net_after_expenses — сумма после расходов площадки. Если маржа отсутствует, production_cost_status и margin_status объясняют причину. Ручная себестоимость вводится с валютой.", "article month cost_per_unit currency mapping", "article month"),
    "sales_drop_check": ("Сопоставить заказы, воронку, наличие, рекламу и отзывы товара за два периода. Различает наблюдения, пробелы и возможные объяснения.", "article date_from date_to previous_from previous_to mapping", "article date_from date_to previous_from previous_to"),
    "mcp_health": ("Проверки с текущими токенами, разрешения Ozon и подписка WB Джем. Для обновления прав передайте refresh_capabilities. Для проверки метода передайте tool_name и probe_arguments.", "tool_name probe_arguments refresh_capabilities", ""),
    "ozon_stocks_analytics": ("Остатки FBO, транзит, среднесуточные продажи и запас в днях по кластерам и складам. С датами показывает историю снимков.", "sku skus cluster_id warehouse_id article date_from date_to offset limit", ""),
    "ozon_turnover": ("Оборачиваемость, среднесуточные продажи и текущий запас товара из отчёта Ozon.", "sku skus article offset limit", ""),
    "ozon_delivery_time": ("Доля локальных продаж по кластерам назначения. Ozon не публикует фактическую длительность доставки покупателю в этом отчёте.", "date_from date_to article sku cluster_id limit offset", "date_from date_to"),
    "wb_retentions": ("Отчёты WB об удержаниях, причины и суммы по указанному виду; при доступности источника возвращает артикул.", "date_from date_to article nm_id retention_type limit offset cursor task_id", "date_from date_to"),
    "wb_region_sales": ("Заказы по регионам назначения и текущим статусам из ленты WB за последние 31 день.", "date_from date_to article nm_id limit offset snapshot_time", "date_from date_to"),
    "wb_returns_report": ("Возвраты товаров продавцу, даты и состояние по артикулу.", "date_from date_to article nm_id return_status limit offset", "date_from date_to"),
    "wb_ads_finance": ("Списания, пополнения и документы рекламы WB. Кампании и документы не распределяются автоматически между артикулами.", "date_from date_to campaign_ids document limit offset", "date_from date_to"),
    "wb_search_queries": ("Поисковые запросы и позиции товара. Нужны категория Аналитика и подписка Джем.", "date_from date_to nm_id nm_ids article search_limit limit", "date_from date_to"),
}


import seller_reports
import seller_views

for _group, (_description, _) in seller_reports.GROUPS.items():
    TOOLS[_group] = (_description + " Укажите report для схемы, добавьте input для чтения.", "", "")
for _view, (_description, _keys, _enums) in seller_views.VIEWS.items():
    TOOLS[_view]=(_description, "", "")

FAST_SELLER_READS = frozenset({
    "wb_questions", "wb_claims", "wb_seller_info", "ozon_supply_acts",
    "ozon_promotions", "ozon_competitor_prices",
})
RESUMABLE_READS = (frozenset(seller_views.VIEWS) - FAST_SELLER_READS) | {
    "unit_economics", "sales_drop_check", "supply_plan", "wb_retentions", "wb_ads",
    "ozon_reviews", "ozon_delivery_time", "wb_acceptance_slots", "ozon_stocks_by_cluster",
    "ozon_stocks_analytics", "ozon_sku_daily", "wb_orders", "wb_sales",
}
for _name in RESUMABLE_READS:
    _description, _keys, _required = TOOLS[_name]
    TOOLS[_name] = (_description + " При status=pending повторите вызов с теми же аргументами и task_id через retry_after_seconds секунд.", _keys, _required)

def schema(name):
    if name in seller_views.VIEWS:
        return seller_views.schema(name)
    if name in seller_reports.GROUPS:
        return seller_reports.schema(name)
    _, keys, required = TOOLS[name]
    props = {k: FIELDS[k] for k in (*keys.split(), "mode")}
    if name in RESUMABLE_READS:
        props["task_id"] = TEXT
    return props, required.split()


def _valid(value, spec):
    types = spec["type"]
    if types == "object":
        if spec.get("additionalProperties") is True:
            return isinstance(value,dict) and len(value)<=20
        return (
            isinstance(value, dict)
            and set(spec["required"]) <= value.keys() <= spec["properties"].keys()
            and all(_valid(v, spec["properties"][k]) for k, v in value.items())
        )
    if types == "array":
        return (
            isinstance(value, list)
            and 1 <= len(value) <= spec["maxItems"]
            and all(_valid(v, spec["items"]) for v in value)
            and len(set(map(str, value))) == len(value)
        )
    if types == "boolean":
        return type(value) is bool
    if type(value) is int:
        return "integer" in types and spec.get("minimum", 0) <= value <= spec.get("maximum", 2**63 - 1)
    if (
        not isinstance(value, str)
        or "string" not in types
        or not 1 <= len(value) <= spec.get("maxLength", 256)
    ):
        return False
    if any(ord(x) < 32 or ord(x) == 127 for x in value):
        return False
    if spec.get("format") == "date":
        return date.fromisoformat(value).isoformat() == value
    if "pattern" in spec:
        return bool(re.fullmatch(spec["pattern"], value)) and ("maximum" not in spec or int(value) <= spec["maximum"])
    return "enum" not in spec or value in spec["enum"]


def validate(name, args):
    if name in seller_views.VIEWS:
        seller_views.validate(name,args)
        return
    if name in seller_reports.GROUPS:
        seller_reports.request(name,args)
        return
    props, required = schema(name)
    if not isinstance(args, dict) or not set(required) <= args.keys() <= props.keys():
        raise ValueError("Invalid tool arguments")
    if not all(_valid(value, props[k]) for k, value in args.items()):
        raise ValueError("Invalid parameter value")
    if name == "wb_funnel":
        if "cursor" in args and (not args.get("group_by") or "offset" in args):
            raise ValueError("Use a grouped funnel cursor without offset")
        if "download_id" in args and (args.get("group_by") != "day" or "article" in args and "nm_id" not in args):
            raise ValueError("Use a daily detail CSV with nm_id for a seller article")
    if name == "wb_orders" and "group_by" in args:
        if not ("article" in args or "nm_id" in args) or "cursor" in args or args.get("mode") == "details":
            raise ValueError("Grouped orders require a product selector and summary pagination")
    if "sku" in args and "skus" in args and {str(args["sku"])} != set(map(str, args["skus"])):
        raise ValueError("Conflicting SKU selectors")
    if "nm_id" in args and "nm_ids" in args and {str(args["nm_id"])} != set(map(str,args["nm_ids"])):
        raise ValueError("Conflicting WB selectors")
    if ("cost_per_unit" in args) != ("currency" in args):
        raise ValueError("Unit cost requires its currency")
    if "probe_arguments" in args and (not args.get("tool_name") or args["tool_name"]=="mcp_health"):
        raise ValueError("Select another tool to probe")
    if name == "sales_drop_check" and not 0 <= (date.fromisoformat(args["previous_to"]) - date.fromisoformat(args["previous_from"])).days < 365:
        raise ValueError("Invalid comparison period")
    if ("date_from" in args) != ("date_to" in args):
        raise ValueError("Both dates required")
    if "date_from" in args:
        days = (date.fromisoformat(args["date_to"]) - date.fromisoformat(args["date_from"])).days + 1
        limit = {
            "wb_paid_storage": 8,
            "wb_acceptance_report": 31,
            "wb_ads": 31,
            "wb_ads_finance": 31,
            "ozon_ads": 62,
        }.get(name, 365)
        if name in ("wb_orders","wb_sales","ozon_sku_daily","wb_finance_report"):
            limit=1827
        if not 1 <= days <= limit:
            raise ValueError("Period out of range")
    if name == "ozon_supply_items" and ("supply_id" in args) != ("order_id" in args):
        raise ValueError("Both supply and order required for acceptance")
    if name == "ozon_ads" and len(args.get("campaign_ids", [])) > 10:
        raise ValueError("At most ten campaigns per report")
    if args.get("include_order_share") and (
        not args.get("campaign_ids")
        or not any(k in args for k in ("sku", "article", "nm_id"))
        or args.get("report", "statistics") in ("campaigns", "spend_ledger")
    ):
        raise ValueError("Order share requires a product and campaign statistics")


def direct_attribution(items, product_key, metric):
    """Aggregate the entire export before paging. Do not attribute multicard/model transactions."""
    totals = {}
    seen = set()
    for row in items:
        campaign, product = row.get("campaign_id"), row.get(product_key)
        if campaign is None or product is None:
            raise ValueError("Advertising row without identity")
        key = str(campaign), str(product)
        total = totals.setdefault(key, 0)
        if product_key == "nm_id":
            kind = row.get("conversion_type")
            if kind in ("associated", "multicard"):
                continue
            if kind != "direct":
                totals[key] = None
                continue
        identity = (*key, row.get("date"), row.get("placement"))
        count = money(row.get(metric))
        if identity in seen or count is None or count < 0 or count != count.to_integral_value():
            totals[key] = None
        elif total is not None:
            totals[key] += int(count)
        seen.add(identity)
    return [{"campaign_id": c, product_key: p, "direct_attributed": v} for (c, p), v in totals.items()]


def rows(value):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise ValueError("Invalid rows in marketplace response")
    return value


def select(row, names):
    return {k: row.get(k) for k in names.split()}


def result(items, cursor=None, *, complete=None, **extra):
    return {
        "items": items,
        "complete": cursor is None if complete is None else complete,
        "next_cursor": cursor,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def matches(row, args):
    return all(str(row.get(k)) == str(args[k]) for k in ("article", "sku", "nm_id") if k in args)


def money(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid amount")
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid amount") from None
    if not n.is_finite() or abs(n.adjusted()) > 30 or len(n.as_tuple().digits) > 32:
        raise ValueError("Invalid amount")
    return n


def iso(value, default_zone=None):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if default_zone is None:
            raise ValueError("Missing timestamp offset")
        parsed = parsed.replace(tzinfo=default_zone)
    return parsed.isoformat()


MOSCOW = timezone(timedelta(hours=3))
