"""Owner-only calculator pages in the existing marketplace cabinet."""

from datetime import date, timedelta
from decimal import Decimal
from html import escape
import hmac
import sqlite3
from urllib.parse import parse_qs, urlencode

import cost_catalog as catalog
import cost_sync
from ui import CALCULATOR_CSP, document

BASE = "/settings?tab=calculator"
LABELS = {"draft": "Черновик", "active": "В работе", "archived": "Архив",
          "ozon": "Ozon", "wb": "Wildberries", "site": "Сайт",
          "material": "Материалы", "printer": "Принтеры", "packaging": "Расходники",
          "box": "Коробки", "marking": "Маркировка", "settings": "Общие настройки",
          "materials": "Материалы", "printing": "Печать", "defects": "Брак", "labor": "Работа", "other": "Прочее"}
FIELDS = {"price_per_kg": "Цена за кг, ₽", "purchase_price": "Цена покупки, ₽", "service_cost": "Обслуживание за весь ресурс, ₽",
          "life_hours": "Ресурс, ч", "power_w": "Мощность, Вт", "manual_rate": "Ручная амортизация, ₽/ч",
          "price": "Цена, ₽", "length_cm": "Длина, см", "width_cm": "Ширина, см", "height_cm": "Высота, см",
          "weight_g": "Вес с упаковкой, г", "units_per_box": "Товаров в коробке", "code_price": "Цена кода, ₽",
          "application_price": "Нанесение, ₽", "electricity_per_kwh": "Электроэнергия, ₽/кВт·ч",
          "defect_percent": "Брак, %", "labor_per_hour": "Работа, ₽/ч"}


def h(value):
    return escape("" if value is None else str(value), quote=True)


def url(**query):
    return BASE + ("&" + urlencode(query) if query else "")


def anchor(label, **query):
    return f'<a href="{h(url(**query))}">{h(label)}</a>'


def control(label, key, value="", typ="text", required=False):
    return (f'<label>{h(label)}<input type="{typ}" name="{h(key)}" value="{h(value)}"'
            + (" required" if required else "") + (" inputmode=\"decimal\"" if key in FIELDS else "") + "></label>")


def select(label, key, choices, current=None):
    options = "".join(f'<option value="{h(value)}"{" selected" if value == current else ""}>{h(name)}</option>'
                      for value, name in choices)
    return f'<label>{h(label)}<select name="{h(key)}">{options}</select></label>'


def hidden(name, value):
    return f'<input type="hidden" name="{h(name)}" value="{h(value)}">'


def form(csrf, action, content, **query):
    return f'<form method="post" action="{h(url(**query))}">{hidden("csrf", csrf)}{hidden("action", action)}{content}</form>'


def table(headers, lines):
    columns = "".join(f"<th scope=\"col\">{h(label)}</th>" for label in headers)
    return '<div class="calc-table"><table><thead><tr>' + columns + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join(f"<td>{col}</td>" for col in row) + "</tr>" for row in lines
    ) + "</tbody></table></div>"


def currency(value):
    return "—" if value is None else h(value) + " ₽"


def textarea(label, name, value=""):
    return f'<label>{h(label)}<textarea name="{h(name)}" rows="3">{h(value)}</textarea></label>'


def _product_list(store, query):
    filters = '<form method="get" action="/settings" class="calc-filters">' + hidden("tab", "calculator")
    filters += control("Поиск по артикулу и названию", "q", query.get("q", ""))
    filters += select("Статус", "status", [("", "Все")] + [(k, LABELS[k]) for k in catalog.STATUSES], query.get("status"))
    filters += select("Площадка", "market", [("", "Все")] + [(k, LABELS[k]) for k in catalog.MARKETS], query.get("market"))
    filters += select("Себестоимость", "cost", [("", "Все"), ("yes", "Рассчитана"), ("no", "Без расчёта")], query.get("cost"))
    filters += "<button>Найти</button></form>"
    last = date.fromisoformat(catalog.today()).replace(day=1) - timedelta(days=1)
    month = last.strftime("%Y-%m")
    margins = store.margins(month)
    rows = []
    for card in store.products():
        if query.get("status") and card["status"] != query["status"]:
            continue
        if query.get("market") and query["market"] not in {link["marketplace"] for link in card["links"]}:
            continue
        if query.get("q", "").casefold() not in (card["article"] + " " + card["name"]).casefold():
            continue
        version = store.at(card["article"], None)
        amount = (version or {}).get("calculation", {}).get("amount")
        if query.get("cost") in ("yes", "no") and (amount is not None) != (query["cost"] == "yes"):
            continue
        photo = ('<img class="calc-thumb" loading="lazy" referrerpolicy="no-referrer" alt="" src="' +
                 h(card["photos"][0]) + '">' if card["photos"] else '<span class="calc-picture"></span>')
        item = '<div class="calc-product">' + photo + '<div>' + anchor(card["name"], view="card", article=card["article"])
        item += "<small>" + h(card["article"]) + "</small></div></div>"
        channels = " ".join('<span class="calc-chip">' + h(LABELS[k]) + "</span>" for k in
                            sorted({link["marketplace"] for link in card["links"]}))
        row_margins = [margins.get((card["article"], market), {}) for market in ("ozon", "wb")]
        historical = store.uniform(card["article"], last.replace(day=1).isoformat(), last.isoformat()) if any(row_margins) else None
        rows.append((item, h(LABELS[card["status"]]), currency(amount), channels,
                     *(currency(r.get("margin_per_unit")) if (historical or {}).get("version") == r.get("cost_version")
                       else "—" for r in row_margins)))
    empty = '<div class="calc-panel">Товары по этим условиям не найдены.</div>'
    return ('<div class="calc-section-title"><h2>Товары</h2><a class="button" href="' + h(url(view="card")) +
            '">Добавить товар</a></div>' + filters +
            (table(("Товар", "Статус", "₽ за штуку", "Привязки", "Ozon · " + month, "WB · " + month), rows) if rows else empty))


def _rows(prefix, template, rendered):
    return (f'<div data-cost-rows="{prefix}">' + "".join(rendered) + template + "</div>"
            f'<template id="cost-{prefix}">{template}</template>'
            f'<button class="calc-secondary" type="button" data-cost-add="{prefix}">Добавить строку</button>')


def _box(reference_rows, kind):
    return [("", "—")] + [(p["id"], p["name"] + (" · архив" if not p["active"] else ""))
                            for p in reference_rows if p["kind"] == kind]


def _parts(card, refs):
    def row(data, material):
        return ('<div class="calc-input-row">' + control("Деталь", "part_name", data.get("name")) +
                control("Штук в товаре", "part_count", data.get("quantity", 1)) +
                select("Принтер", "part_printer", _box(refs, "printer"), data.get("printer")) +
                control("Печать, ч", "part_hours", data.get("hours")) +
                select("Пластик", "part_material", _box(refs, "material"), material.get("id")) +
                control("Вес, г", "part_weight", material.get("grams")) +
                control("Цвет", "part_color", data.get("color")) + "</div>")
    return _rows("parts", row({}, {}), [row(d, m) for d in card.get("parts", []) for m in d["materials"]])


def _links(card):
    def row(item):
        return ('<div class="calc-input-row">' +
                select("Площадка", "link_market", [("", "—")] + [(k, LABELS[k]) for k in catalog.MARKETS], item.get("marketplace")) +
                control("Код товара", "link_code", item.get("code")) +
                control("SKU или nmID", "link_id", item.get("marketplace_id")) + "</div>")
    return _rows("links", row({}), [row(item) for item in card.get("links", [])])


def _package(card, refs):
    data = card.get("package", {})
    def row(item):
        return ('<div class="calc-input-row">' + select("Расходник", "pack_id", _box(refs, "packaging"), item.get("id")) +
                control("Количество", "pack_quantity", item.get("quantity")) + "</div>")
    return (select("Упаковка задана", "pack_ready", [("0", "Заполнить позже"), ("1", "Готова")], str(int(data.get("configured", False)))) +
            '<div class="calc-grid">' + select("Коробка", "pack_box", _box(refs, "box"), data.get("box")) +
            "".join(control(FIELDS[k], k, data.get(k)) for k in ("weight_g", "length_cm", "width_cm", "height_cm")) +
            "</div>" + _rows("package", row({}), [row(i) for i in data.get("consumables", [])]))


def _breakdown(version):
    if not version:
        return '<p class="calc-muted">Для выбранной даты расчёта пока нет.</p>'
    calc = version["calculation"]
    if calc["status"] != "calculated":
        return '<p class="calc-muted">Заполните детали, упаковку и настройки расчёта.</p>'
    items = tuple((h(LABELS.get(key, key)), currency(value["amount"]), h(value["percent"]) + "%")
                  for key, value in calc["components"].items())
    return ('<div class="calc-totals"><div><small>Себестоимость</small><strong>' + currency(calc["amount"]) +
            '</strong></div><div><small>Пластик</small><strong>' + h(calc["plastic_grams"]) +
            ' г</strong></div><div><small>Печать</small><strong>' + h(calc["print_hours"]) +
            " ч</strong></div></div>" + table(("Статья", "Сумма", "Доля"), items) +
            "<p>Материалы · " + h(", ".join(m["name"] for m in calc["materials"])) + "<br>Цвета · " +
            h(", ".join(calc["colors"]) or "—") + "<br>Принтеры · " + h(", ".join(calc["printers"])) + "</p>")


def _history(store, article, query):
    versions = store.history(article)
    choices = [(str(v["version"]), f'№{v["version"]} от {v["effective_from"]}') for v in versions]
    rows = [(h(v["version"]), h(v["effective_from"]), currency(v["calculation"].get("amount")), h(v["reason"])) for v in versions]
    html = table(("Версия", "Действует с", "₽ за штуку", "Изменение"), rows)
    if len(versions) > 1:
        html += '<form method="get" action="/settings" class="calc-filters">' + hidden("tab", "calculator")
        html += hidden("view", "card") + hidden("article", article)
        html += select("Первая версия", "a", choices, query.get("a")) + select("Вторая версия", "b", choices, query.get("b"))
        html += "<button>Сравнить</button></form>"
        match = {str(v["version"]): v for v in versions}
        if query.get("a") in match and query.get("b") in match:
            a, b = (match[query[key]] for key in ("a", "b"))
            left, right = a["calculation"].get("components", {}), b["calculation"].get("components", {})
            html += table(("Статья", "№" + str(a["version"]), "№" + str(b["version"])),
                          [(h(LABELS[k]), currency(left.get(k, {}).get("amount")), currency(right.get(k, {}).get("amount")))
                           for k in catalog.COST_FIELDS])
    return html


def _card(store, query, csrf):
    original = store.resolve(query["article"]) if query.get("article") else None
    copying = query.get("copy") == "1"
    data = original or {}
    refs = store.references()
    editing = bool(original) and not copying
    title = original["name"] if editing else "Новый товар"
    heading = '<div class="calc-section-title"><h2>' + h(title) + "</h2>"
    if editing:
        heading += anchor("Создать похожий", view="card", article=original["article"], copy="1")
    heading += "</div>"
    legal = hidden("revision", original["revision"] if editing else 0)
    legal += hidden("existing", original["article"] if editing else "")
    fields = '<div class="calc-grid">' + control("Внутренний артикул", "article", data.get("article") if editing else "", required=True)
    fields += control("Название", "name", data.get("name"), required=True)
    fields += select("Статус", "status", [(s, LABELS[s]) for s in catalog.STATUSES], data.get("status") if editing else "draft")
    fields += control("Дата начала действия", "effective", catalog.today(), "date", True) + "</div>"
    fields += control("Комментарий", "comment", data.get("comment"))
    fields += textarea("Фото · один адрес HTTPS на строку", "photos", "\n".join(data.get("photos", [])))
    fields += '<h3>Привязки</h3>' + _links(data if not copying else {})
    fields += '<h3>Детали</h3><p class="calc-muted">Для нескольких материалов повторите название детали, количество и время печати.</p>' + _parts(data, refs)
    fields += "<h3>Упаковка</h3>" + _package(data, refs)
    fields += '<h3>Дополнительные расходы</h3><div class="calc-grid">' + select(
        "Честный знак", "marking_required", [("0", "Не требуется"), ("1", "Требуется")], str(int(data.get("marking_required", False))))
    fields += select("Позиция маркировки", "marking", _box(refs, "marking"), data.get("marking"))
    fields += control("Брак, %", "defect_percent", data.get("defect_percent"))
    fields += control("Ручная работа, минут", "manual_minutes", data.get("manual_minutes", 0))
    fields += control("Прочие затраты, ₽", "other", data.get("other", 0))
    fields += control("Комментарий к прочим затратам", "other_comment", data.get("other_comment")) + "</div>"
    fields += "<button>Сохранить карточку</button>"
    if editing:
        heading += _breakdown(store.at(original["article"], None))
    html = heading + '<div class="calc-panel">' + form(csrf, "save_product", legal + fields, view="card") + "</div>"
    if editing:
        html += "<h2>История себестоимости</h2>" + _history(store, original["article"], query)
    return html


def _reference_page(store, query, csrf):
    kind = query.get("kind", "material")
    if kind not in catalog.REFERENCES:
        raise ValueError()
    options = '<div class="calc-tabs">' + "".join(anchor(LABELS[k], view="references", kind=k) for k in catalog.REFERENCES) + "</div>"
    rows = [r for r in store.references() if r["kind"] == kind]
    data = next((r for r in rows if r["id"] == query.get("id") or kind == "settings"), {})
    fields = hidden("kind", kind) + hidden("fixed_id", data.get("id", ""))
    if kind != "settings":
        fields += control("Код позиции", "ref_id", data.get("id"), required=True) + control("Название", "ref_name", data.get("name"), required=True)
        fields += select("Статус", "active", [("1", "Активна"), ("0", "Архив")], str(int(data.get("active", True))))
    fields += "".join(control(FIELDS[key], key, data.get(key, 1 if key == "units_per_box" else 0 if key in ("service_cost", "labor_per_hour") else ""))
                      for key in catalog.REFERENCES[kind])
    if kind == "material":
        fields += select("Тип пластика", "material_type", [(k, k if k != "other" else "Другое") for k in ("PLA", "PETG", "ABS", "TPU", "other")],
                         data.get("type", "other"))
        fields += control("Цвет", "color", data.get("color")) + control("Поставщик", "supplier", data.get("supplier"))
    if kind == "packaging":
        fields += select("Единица", "unit", [("pcs", "шт."), ("m", "м"), ("g", "г")], data.get("unit", "pcs"))
    fields += control("Дата начала действия", "effective", catalog.today(), "date", True)
    listing = [(anchor(r["name"], view="references", kind=kind, id=r["id"]), h(r["id"]),
                "Активна" if r["active"] else "Архив") for r in rows]
    if data and kind == "printer":
        rate = (Decimal(data["manual_rate"]) if data["manual_rate"] is not None else
                (Decimal(data["purchase_price"]) + Decimal(data["service_cost"])) / Decimal(data["life_hours"]))
        tariff = next((item.get("electricity_per_kwh") for item in store.references() if item["kind"] == "settings"), None)
        energy = Decimal(tariff) * Decimal(data["power_w"]) / 1000 if tariff is not None else None
        fields += "<p>Амортизация · " + currency(catalog.display(rate)) + "/ч<br>Электроэнергия · " + (
            currency(catalog.display(energy)) + "/ч" if energy is not None else "укажите тариф") + "</p>"
    return options + "<h2>" + h(LABELS[kind]) + "</h2>" + (
        table(("Позиция", "Код", "Статус"), listing) if listing and kind != "settings" else "") + (
        "<p>" + anchor("Добавить позицию", view="references", kind=kind) + "</p>" if data and kind != "settings" else "") + (
        '<div class="calc-panel">' + form(csrf, "save_reference", '<div class="calc-grid">' + fields +
                                         '</div><button>Сохранить</button>', view="references", kind=kind) + "</div>")


def _inbox(store, csrf):
    lines = []
    for item in store.inbox():
        product = item["product"]
        categories = {"new": "Новый товар", "changed": "Изменение карточки", "missing": "Пропал из каталога", "dimensions": "Габариты"}
        basis = {"exact_article": "Совпал код", "similar_article": "Похожий артикул", "similar_title": "Похожее название",
                 "no_unique_match": "Создать новый артикул", "catalog_change": "Сверка каталогов",
                 "absent_from_complete_scan": "Полная загрузка каталога", "package_comparison": "Сверка упаковки",
                 "provider_wrong_volume": "Ozon отметил габариты. Единицы измерения не переданы"}
        number = h(item["id"])
        detail = ", ".join(FIELDS.get(k, k) for k in item["fields"])
        lines.append((f'<input type="checkbox" name="id" value="{number}" aria-label="Выбрать предложение {number}">',
                      h(LABELS[item["marketplace"]]), h(categories[item["kind"]]), h(product["name"]) + "<small>" +
                      h(product["code"]) + "</small>", h(item["suggested_article"] or product["code"]),
                      h(basis.get(item["basis"], item["basis"])) + (" · " + h(detail) if detail else "")))
    if not lines:
        return '<h2>Входящие</h2><p class="calc-muted">Предложений на подтверждение нет.</p>'
    options = table(("Выбор", "Площадка", "Предложение", "Карточка", "Внутренний артикул", "Основание"), lines)
    options += control("Другой внутренний артикул для выбранного предложения", "target")
    options += '<div class="calc-actions"><button name="decision" value="accept">Подтвердить</button><button class="calc-secondary" name="decision" value="reject">Отклонить</button><button class="calc-secondary" name="decision" value="ignore">Игнорировать</button></div>'
    return "<h2>Входящие</h2>" + form(csrf, "inbox", options, view="inbox")


def _journal(store, csrf):
    sync = []
    for run in cost_sync.runs(store):
        for market, row in run["marketplaces"].items():
            counts = row.get("counts", {})
            sync.append((h(run["started_at"]), h(LABELS[market]), h(row.get("state")), h(row.get("scanned")),
                        h(sum(counts.values()) if counts else 0), h(row.get("error") or "—")))
    audit = [(h(r["at"]), h(r["actor"]), h(r["entity"]), h(r["reference"]), h(r["action"]), h(", ".join(r["fields"])))
             for r in store.journal()]
    return ("<h2>Синхронизации</h2>" + form(csrf, "sync", "<button>Синхронизировать сейчас</button>", view="journal") +
            table(("Время", "Площадка", "Статус", "Проверено", "Предложения", "Ошибка"), sync) +
            "<h2>Изменения калькулятора</h2>" + table(("Время", "Кто", "Раздел", "Код", "Действие", "Поля"), audit))


def _form_product(values):
    def get(k, default=""):
        data = values.get(k, [default])
        if len(data) != 1:
            raise ValueError()
        return data[0]

    def lines(fields):
        columns = [values.get(field, []) for field in fields]
        if len({len(c) for c in columns}) != 1:
            raise ValueError()
        return zip(*columns)

    parts = {}
    for name, count, printer, hours, material, weight, color in lines(
            ("part_name", "part_count", "part_printer", "part_hours", "part_material", "part_weight", "part_color")):
        if not name and not material and not weight and not printer and not hours and not color:
            continue
        if not name:
            raise catalog.CatalogError("Введите название каждой детали.")
        fields = dict(name=name, quantity=catalog.number(count, positive=True, integer=True),
                      printer=printer, hours=catalog.number(hours, positive=True), color=color)
        if name in parts and any(parts[name][k] != v for k, v in fields.items()):
            raise catalog.CatalogError("Согласуйте количество, принтер, цвет и время в повторяющихся строках детали.")
        parts.setdefault(name, dict(fields, materials=[]))["materials"].append(dict(id=material, grams=weight))
    links = [dict(marketplace=market, code=code, marketplace_id=ident) for market, code, ident in
             lines(("link_market", "link_code", "link_id")) if market or code or ident]
    consumables = [dict(id=ident, quantity=quantity) for ident, quantity in lines(("pack_id", "pack_quantity")) if ident or quantity]
    article, existing, revision = get("article"), get("existing"), int(get("revision", "0"))
    if existing and existing != article:
        raise catalog.CatalogError("Внутренний артикул существующего товара изменить нельзя.")
    card = dict(article=article, name=get("name"), status=get("status"), comment=get("comment"), links=links,
                photos=[p.strip() for p in get("photos").splitlines() if p.strip()], parts=list(parts.values()),
                package=dict(configured=get("pack_ready") == "1", box=get("pack_box"), consumables=consumables,
                             **{k: get(k) for k in ("weight_g", "length_cm", "width_cm", "height_cm")}),
                marking_required=get("marking_required") == "1", marking=get("marking"), defect_percent=get("defect_percent"),
                manual_minutes=get("manual_minutes", "0"), other=get("other", "0"), other_comment=get("other_comment"))
    return card, get("effective"), revision


def handle(portal, env, method, session, query, body, reply, start):
    store = catalog.Store(portal.vault.path)
    csrf = session[1]
    if method == "POST":
        if env.get("HTTP_ORIGIN") != portal.login.origin:
            return reply("Доступ запрещён.", "403 Forbidden")
        values = parse_qs(body("application/x-www-form-urlencoded", 262144), max_num_fields=3000, keep_blank_values=True)

        def get(key, default=""):
            items = values.get(key, [default])
            if len(items) != 1:
                raise ValueError()
            return items[0]

        if not hmac.compare_digest(csrf, get("csrf")):
            return reply("Доступ запрещён.", "403 Forbidden")
        action = get("action")
        target = {"view": query.get("view", "products")}
        actor = "owner"
        if action == "save_product":
            card, day, rev = _form_product(values)
            store.save_product(card, day, actor, rev)
            target = dict(view="card", article=card["article"])
        elif action == "save_reference":
            kind, ident = get("kind"), get("ref_id")
            if get("fixed_id") and kind != "settings" and ident != get("fixed_id"):
                raise catalog.CatalogError("Код существующей позиции изменить нельзя.")
            data = dict(name=get("ref_name"), active=get("active") == "1", type=get("material_type"),
                        color=get("color"), supplier=get("supplier"), unit=get("unit"))
            data.update({key: get(key) for key in catalog.REFERENCES[kind]})
            store.save_reference(kind, ident, data, get("effective"), actor)
            target = dict(view="references", kind=kind, id=ident)
        elif action == "inbox":
            ids = [int(value) for value in values.get("id", [])]
            if get("target") and len(set(ids)) != 1:
                raise catalog.CatalogError("Другой артикул можно указать для одного предложения.")
            cost_sync.apply(store, ids, get("decision"), get("target") or None, actor)
        elif action == "sync":
            cost_sync.kick(portal, manual=True)
        elif action == "import":
            store.csv_import(get("csv"), actor)
        else:
            raise ValueError()
        start("303 See Other", [("Location", url(**target, saved="1")), ("Cache-Control", "no-store"), ("Content-Length", "0")])
        return [b""]

    view = query.get("view", "products")
    if view == "export":
        append = store.csv_export().encode("utf-8-sig")
        start("200 OK", [("Content-Type", "text/csv; charset=utf-8"),
                         ("Content-Disposition", "attachment; filename=calculator.csv"),
                         ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"), ("Content-Length", str(len(append)))])
        return [append]
    if view == "card":
        content = _card(store, query, csrf)
    elif view == "references":
        content = _reference_page(store, query, csrf)
    elif view == "inbox":
        content = _inbox(store, csrf)
    elif view == "journal":
        content = _journal(store, csrf)
    elif view == "import":
        content = "<h2>Импорт и экспорт</h2><p>" + anchor("Скачать CSV", view="export") + "</p>"
        content += form(csrf, "import", '<label>Файл CSV<input type="file" id="cost-csv-file" accept=".csv,text/csv"></label>' +
                        textarea("Содержимое CSV · kind,id,data", "csv") + "<button>Импортировать</button>", view="import")
    else:
        content = _product_list(store, query)
    nav = [(initial, label) for initial, label in (("products", "Товары"), ("references", "Справочники"),
                                                  ("inbox", "Входящие"), ("journal", "Журнал"), ("import", "Импорт и экспорт"))]
    tabs = "".join('<a href="' + h(url(view=key)) + '"' + (' aria-current="page"' if key == view else "") + ">" + h(label) + "</a>"
                   for key, label in nav)
    notice = '<p class="notice" role="status">Изменения приняты.</p>' if query.get("saved") == "1" else ""
    return reply(document("Калькулятор", '<main id="content" class="calculator"><h1>Калькулятор</h1><nav class="calc-tabs" aria-label="Калькулятор">' +
                          tabs + "</nav>" + notice + content + "</main>", calculator=True), csp=CALCULATOR_CSP)
