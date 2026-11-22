"""Versioned production costs stored with the owner's portal data."""

from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import csv
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4

from market_specs import MOSCOW, money, result

STATUSES = ("draft", "active", "archived")
MARKETS = ("ozon", "wb", "site")
COST_FIELDS = ("materials", "printing", "defects", "components", "packaging", "marking", "labor", "other")
PURCHASE_UNITS = ("pcs", "m", "g")
REFERENCES = {
    "material": ("price_per_kg",),
    "printer": ("purchase_price", "service_cost", "life_hours", "power_w", "manual_rate"),
    "component": ("price",),
    "packaging": ("price",),
    "box": ("price", "length_cm", "width_cm", "height_cm", "units_per_box"),
    "marking": ("code_price", "application_price"),
    "settings": ("electricity_per_kwh", "defect_percent", "labor_per_hour"),
}
DEFAULTS = {"electricity_per_kwh": None, "defect_percent": None, "labor_per_hour": "0"}
TOOLS = frozenset(("cost_card", "cost_list", "cost_history", "cost_inbox"))


def database_path(vault_path):
    return str(vault_path) + ".cost-calculator"


class CatalogError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def today():
    return datetime.now(MOSCOW).date().isoformat()


def effective(value):
    try:
        return date.fromisoformat(value or today()).isoformat()
    except (ValueError, TypeError):
        raise CatalogError("Укажите дату в формате ГГГГ-ММ-ДД.") from None


def text(value, *, required=False, maximum=200):
    if not isinstance(value, str) or len(value) > maximum or any(ord(ch) < 32 for ch in value):
        raise CatalogError("Проверьте текстовые поля.")
    value = value.strip()
    if required and not value:
        raise CatalogError("Заполните обязательные поля.")
    return value


def number(value, *, optional=False, positive=False, integer=False):
    if value is None or value == "":
        if optional:
            return None
        raise CatalogError("Заполните числовые поля.")
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            if re.fullmatch(r"[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+(?:\.[0-9]+)?", value):
                value = re.sub(r"[ \u00a0\u202f]", "", value)
        parsed = money(value)
    except (ValueError, ArithmeticError):
        raise CatalogError("Укажите конечное число.") from None
    if parsed < 0 or positive and not parsed or parsed > Decimal("1000000000000"):
        raise CatalogError("Число выходит за допустимые пределы.")
    if integer and parsed != parsed.to_integral_value():
        raise CatalogError("Для этого поля требуется целое число.")
    return int(parsed) if integer else format(parsed, "f")


def _entries(value, maximum):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(x, dict) for x in value):
        raise CatalogError("Проверьте состав списка.")
    return value


def _photo(url):
    url = text(url, required=True, maximum=2048)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise CatalogError("Для фотографии укажите адрес HTTPS.")
    return url


def _link(row):
    market = row.get("marketplace")
    if market not in MARKETS:
        raise CatalogError("Выберите площадку в привязке.")
    code = text(row.get("code", ""), required=True)
    ident = row.get("marketplace_id")
    if market == "site":
        ident = None
    elif ident not in (None, ""):
        ident = str(ident)
        if not re.fullmatch(r"[1-9][0-9]{0,18}", ident) or int(ident) > 2**63 - 1:
            raise CatalogError("Укажите корректный SKU или nmID.")
    else:
        ident = None
    return {"marketplace": market, "code": code, "marketplace_id": ident}


def product(data):
    state = data.get("status", "draft")
    if state not in STATUSES:
        raise CatalogError("Выберите статус товара.")
    photos = data.get("photos", [])
    if not isinstance(photos, list) or len(photos) > 10:
        raise CatalogError("В карточку помещается до десяти фотографий.")
    parts = []
    for row in _entries(data.get("parts", []), 50):
        materials = [{"id": text(m.get("id", ""), required=True),
                      "grams": number(m.get("grams"), positive=True)}
                     for m in _entries(row.get("materials", []), 25)]
        if not materials:
            raise CatalogError("Укажите хотя бы один материал детали.")
        parts.append({"name": text(row.get("name", ""), required=True),
                      "quantity": number(row.get("quantity", 1), positive=True, integer=True),
                      "printer": text(row.get("printer", ""), required=True), "materials": materials,
                      "color": text(row.get("color", "")), "hours": number(row.get("hours"), positive=True)})
    links = [_link(row) for row in _entries(data.get("links", []), 50)]
    if len({_json(link) for link in links}) != len(links):
        raise CatalogError("Удалите повторяющиеся привязки.")
    package = data.get("package", {})
    if not isinstance(package, dict):
        raise CatalogError("Проверьте упаковку.")
    dimensions = {k: number(package.get(k), optional=True, positive=True)
                  for k in ("length_cm", "width_cm", "height_cm", "weight_g")}
    consumables = [{"id": text(r.get("id", ""), required=True), "quantity": number(r.get("quantity"), positive=True)}
                   for r in _entries(package.get("consumables", []), 50)]
    components = [{"id": text(row.get("id", ""), required=True), "quantity": number(row.get("quantity"), positive=True)}
                  for row in _entries(data.get("components", []), 50)]
    defect = number(data.get("defect_percent"), optional=True)
    if defect is not None and Decimal(defect) >= 100:
        raise CatalogError("Брак должен быть меньше 100%.")
    marking_required = data.get("marking_required") is True
    marking = text(data.get("marking", ""))
    if marking_required and not marking:
        raise CatalogError("Выберите позицию справочника маркировки.")
    return dict(article=text(data.get("article", ""), required=True, maximum=128),
                name=text(data.get("name", ""), required=True), status=state, links=links,
                photos=list(dict.fromkeys(_photo(p) for p in photos)),
                comment=text(data.get("comment", ""), maximum=1000), parts=parts, components=components,
                package=dict(dimensions, consumables=consumables, box=text(package.get("box", "")),
                             configured=package.get("configured") is True),
                marking_required=marking_required, marking=marking if marking_required else "",
                defect_percent=defect, manual_minutes=number(data.get("manual_minutes", 0)),
                other=number(data.get("other", 0)), other_comment=text(data.get("other_comment", ""), maximum=500))


def reference(kind, value):
    if kind not in REFERENCES:
        raise CatalogError("Неизвестный справочник.")
    if kind == "settings":
        data = dict(DEFAULTS, **{k: value[k] for k in REFERENCES[kind] if k in value})
        data = {k: number(v, optional=k != "labor_per_hour") for k, v in data.items()}
        if data["defect_percent"] is not None and Decimal(data["defect_percent"]) >= 100:
            raise CatalogError("Брак должен быть меньше 100%.")
        return dict(data, name="Общие настройки", currency="RUB", active=True)
    data = dict(name=text(value.get("name", ""), required=True), active=value.get("active", True) is True)
    for key in REFERENCES[kind]:
        default = 1 if key == "units_per_box" else 0 if key == "service_cost" else None
        data[key] = number(value.get(key, default), optional=key == "manual_rate",
                           positive=key in ("life_hours", "length_cm", "width_cm", "height_cm", "units_per_box"),
                           integer=key == "units_per_box")
    if kind == "material":
        material_type = value.get("type", "other")
        if material_type not in ("PLA", "PETG", "ABS", "TPU", "other"):
            raise CatalogError("Выберите тип пластика.")
        data.update(type=material_type, color=text(value.get("color", "")), supplier=text(value.get("supplier", "")))
    if kind in ("component", "packaging"):
        unit = value.get("unit", "pcs")
        if unit not in PURCHASE_UNITS:
            raise CatalogError("Укажите единицу измерения.")
        data["unit"] = unit
    return data


def dependencies(card):
    used = {("settings", "default")}
    for part in card["parts"]:
        used.add(("printer", part["printer"]))
        used.update(("material", item["id"]) for item in part["materials"])
    used.update(("packaging", item["id"]) for item in card["package"]["consumables"])
    used.update(("component", item["id"]) for item in card.get("components", []))
    if card["package"]["box"]:
        used.add(("box", card["package"]["box"]))
    if card["marking_required"]:
        used.add(("marking", card["marking"]))
    return used


def display(value):
    return format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def calculate(card, refs):
    settings = refs.get(("settings", "default"), DEFAULTS)
    defect = card["defect_percent"] if card["defect_percent"] is not None else settings["defect_percent"]
    gaps = [kind + "/" + ident for kind, ident in sorted(dependencies(card))
            if kind != "settings" and (kind, ident) not in refs]
    if not card["parts"]:
        gaps.append("parts")
    if not card["package"]["configured"]:
        gaps.append("package")
    if settings["electricity_per_kwh"] is None:
        gaps.append("electricity_per_kwh")
    if defect is None:
        gaps.append("defect_percent")
    if gaps:
        return {"status": "incomplete", "missing": gaps, "currency": "RUB"}
    with localcontext() as ctx:
        ctx.prec = 52
        costs = dict.fromkeys(COST_FIELDS, Decimal(0))
        grams = defaultdict(Decimal)
        colors, printers = set(), set()
        hours = Decimal(0)
        for part in card["parts"]:
            copies = part["quantity"]
            duration = Decimal(part["hours"]) * copies
            hours += duration
            printer = refs["printer", part["printer"]]
            printers.add(printer["name"])
            depreciation = (Decimal(printer["manual_rate"]) if printer["manual_rate"] is not None else
                            (Decimal(printer["purchase_price"]) + Decimal(printer["service_cost"])) / Decimal(printer["life_hours"]))
            energy = Decimal(printer["power_w"]) * Decimal(settings["electricity_per_kwh"]) / 1000
            costs["printing"] += duration * (depreciation + energy)
            for item in part["materials"]:
                material = refs["material", item["id"]]
                weight = Decimal(item["grams"]) * copies
                grams[item["id"]] += weight
                costs["materials"] += weight * Decimal(material["price_per_kg"]) / 1000
                if color := (part["color"] or material["color"]):
                    colors.add(color)
        defect = Decimal(defect)
        costs["defects"] = (costs["materials"] + costs["printing"]) * defect / (100 - defect)
        component_items = []
        for item in card.get("components", []):
            ref = refs["component", item["id"]]
            quantity = Decimal(item["quantity"])
            if ref["unit"] == "pcs" and quantity != quantity.to_integral_value():
                raise CatalogError("Штучные комплектующие указываются целым количеством.")
            amount = quantity * Decimal(ref["price"])
            costs["components"] += amount
            component_items.append(dict(id=item["id"], name=ref["name"], quantity=item["quantity"], unit=ref["unit"],
                                        unit_price=display(ref["price"]), amount=display(amount)))
        if not component_items:
            costs.pop("components")
        for item in card["package"]["consumables"]:
            ref = refs["packaging", item["id"]]
            quantity = Decimal(item["quantity"])
            if ref["unit"] == "pcs" and quantity != quantity.to_integral_value():
                raise CatalogError("Штучные расходники указываются целым количеством.")
            costs["packaging"] += quantity * Decimal(ref["price"])
        box_id = card["package"]["box"]
        if box_id:
            box = refs["box", box_id]
            costs["packaging"] += Decimal(box["price"]) / box["units_per_box"]
        if card["marking_required"]:
            mark = refs["marking", card["marking"]]
            costs["marking"] = Decimal(mark["code_price"]) + Decimal(mark["application_price"])
        costs["labor"] = Decimal(card["manual_minutes"]) * Decimal(settings["labor_per_hour"]) / 60
        costs["other"] = Decimal(card["other"])
        total = sum(costs.values())
        return dict(status="calculated", currency="RUB", amount=display(total), exact=format(total, "f"),
                    components={key: {"amount": display(value), "percent": display(value / total * 100 if total else 0)}
                                for key, value in costs.items()}, component_items=component_items, print_hours=format(hours, "f"),
                    plastic_grams=format(sum(grams.values()), "f"), colors=sorted(colors), printers=sorted(printers),
                    materials=[dict(id=key, name=refs["material", key]["name"], grams=format(weight, "f"))
                               for key, weight in sorted(grams.items())])


class Store:
    @staticmethod
    def exists(path):
        try:
            uri = Path(path).absolute().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
                return conn.execute("SELECT 1 FROM sqlite_master WHERE name='cost_products'").fetchone() is not None
        except (OSError, sqlite3.Error):
            return False

    def __init__(self, path):
        self.path = str(path)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cost_products(article TEXT PRIMARY KEY, data TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cost_references(kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS cost_reference_versions(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    effective TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cost_ref_day ON cost_reference_versions(kind,ref,effective,id);
                CREATE TABLE IF NOT EXISTS cost_versions(id INTEGER PRIMARY KEY, article TEXT NOT NULL, effective TEXT NOT NULL,
                    at TEXT NOT NULL, reason TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cost_product_day ON cost_versions(article,effective,id);
                CREATE TABLE IF NOT EXISTS cost_audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
                    entity TEXT NOT NULL, reference TEXT NOT NULL, action TEXT NOT NULL, fields TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cost_inbox(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, market TEXT NOT NULL,
                    status TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cost_sync(id INTEGER PRIMARY KEY, day TEXT NOT NULL, at TEXT NOT NULL,
                    trigger TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cost_market_snapshots(market TEXT NOT NULL, code TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(market,code));
                CREATE TABLE IF NOT EXISTS cost_margins(article TEXT NOT NULL, market TEXT NOT NULL, month TEXT NOT NULL,
                    saved_at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(article,market,month));
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def audit(conn, actor, entity, ident, action, fields):
        conn.execute("INSERT INTO cost_audit(at,actor,entity,reference,action,fields) VALUES(?,?,?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), actor, entity, ident, action, _json(fields)))

    @staticmethod
    def _refs_at(conn, card, day):
        refs = {}
        for kind, ident in dependencies(card):
            row = conn.execute("""SELECT data FROM cost_reference_versions WHERE kind=? AND ref=? AND effective<=?
                ORDER BY effective DESC,id DESC LIMIT 1""", (kind, ident, day)).fetchone()
            if row:
                refs[kind, ident] = json.loads(row["data"])
        return refs

    def _version(self, conn, card, day, reason):
        refs = self._refs_at(conn, card, day)
        snapshot = {"product": card, "calculation": calculate(card, refs),
                    "references": [dict(kind=kind, id=ident, values=value)
                                   for (kind, ident), value in sorted(refs.items())]}
        return conn.execute("INSERT INTO cost_versions(article,effective,at,reason,snapshot) VALUES(?,?,?,?,?)",
                            (card["article"], day, datetime.now(timezone.utc).isoformat(), reason, _json(snapshot))).lastrowid

    def _save_product(self, conn, data, day, actor, expected=None):
        card = product(data)
        previous = conn.execute("SELECT data,revision FROM cost_products WHERE article=?", (card["article"],)).fetchone()
        revision = previous["revision"] if previous else 0
        if expected is not None and expected != revision:
            raise CatalogError("Карточка уже изменилась. Откройте её заново.")
        old = json.loads(previous["data"]) if previous else {}
        entries = {(p["marketplace"], field, p[field]) for p in card["links"]
                   for field in ("code", "marketplace_id") if p[field]}
        for row in conn.execute("SELECT data FROM cost_products WHERE article<>?", (card["article"],)):
            for link in json.loads(row["data"])["links"]:
                if any((link["marketplace"], f, link[f]) in entries for f in ("code", "marketplace_id")):
                    raise CatalogError("Код площадки уже привязан к другому внутреннему артикулу.")
        if card["status"] == "active":
            if calculate(card, self._refs_at(conn, card, day))["status"] != "calculated":
                raise CatalogError("Заполните детали, упаковку и цены перед переводом товара в работу.")
        conn.execute("""INSERT INTO cost_products VALUES(?,?,?) ON CONFLICT(article)
            DO UPDATE SET data=excluded.data,revision=excluded.revision""", (card["article"], _json(card), revision + 1))
        version = self._version(conn, card, day, "product_saved")
        self.audit(conn, actor, "product", card["article"], "saved", sorted(k for k in card if card[k] != old.get(k)))
        return version

    def save_product(self, data, day, actor="owner", expected=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._save_product(conn, data, effective(day), actor, expected)

    def _save_reference(self, conn, kind, ident, value, day, actor):
        ident = "default" if kind == "settings" else text(ident, required=True, maximum=128)
        data = reference(kind, value)
        previous = conn.execute("SELECT data FROM cost_references WHERE kind=? AND id=?", (kind, ident)).fetchone()
        old = json.loads(previous[0]) if previous else {}
        stored = data
        changed_code = False
        if kind != "settings":
            old_code = old.pop("code", ident)
            code = text(value.get("code", old_code), required=True, maximum=128)
            other_rows = conn.execute("SELECT id,data FROM cost_references WHERE kind=? AND id<>?", (kind, ident))
            if any(json.loads(row["data"]).get("code", row["id"]) == code for row in other_rows):
                raise CatalogError("Этот код уже используется в справочнике.")
            if code != ident:
                stored = dict(data, code=code)
            changed_code = code != old_code
        encoded = _json(data)
        code_only = False
        if changed_code and data == old:
            existing = conn.execute("""SELECT data FROM cost_reference_versions WHERE kind=? AND ref=? AND effective<=?
                ORDER BY effective DESC,id DESC LIMIT 1""", (kind, ident, day)).fetchone()
            code_only = bool(existing and json.loads(existing["data"]) == data)
        conn.execute("""INSERT INTO cost_references VALUES(?,?,?) ON CONFLICT(kind,id)
            DO UPDATE SET data=excluded.data""", (kind, ident, _json(stored)))
        fields = [key for key in data if data[key] != old.get(key)]
        self.audit(conn, actor, kind, ident, "saved", sorted(fields + (["code"] if changed_code else [])))
        if code_only:
            return
        conn.execute("INSERT INTO cost_reference_versions(kind,ref,effective,data) VALUES(?,?,?,?)", (kind, ident, day, encoded))
        for row in conn.execute("SELECT article,data FROM cost_products").fetchall():
            card = json.loads(row["data"])
            earlier = conn.execute("""SELECT snapshot FROM cost_versions WHERE article=? AND effective<=?
                ORDER BY effective DESC,id DESC LIMIT 1""", (card["article"], day)).fetchone()
            if earlier:
                old_card = json.loads(earlier["snapshot"])["product"]
                if (kind, ident) in dependencies(old_card):
                    self._version(conn, old_card, day, f"{kind}/{ident}")
            later = conn.execute("""SELECT a.effective,a.snapshot FROM cost_versions a WHERE article=? AND effective>? AND id=(
                SELECT MAX(b.id) FROM cost_versions b WHERE b.article=a.article AND b.effective=a.effective) ORDER BY effective""",
                (card["article"], day)).fetchall()
            for version in later:
                old_card = json.loads(version["snapshot"])["product"]
                if (kind, ident) in dependencies(old_card):
                    self._version(conn, old_card, version["effective"], f"{kind}/{ident}")

    def save_reference(self, kind, ident, value, day, actor="owner", *, create=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if create and kind != "settings":
                ident = text(ident, required=True, maximum=128)
                value = dict(value, code=ident)
                occupied = conn.execute("SELECT 1 FROM cost_references WHERE kind=? AND id=?", (kind, ident)).fetchone()
                if occupied:
                    ident = uuid4().hex
            self._save_reference(conn, kind, ident, value, effective(day), actor)
        return "default" if kind == "settings" else ident

    def references(self):
        with self.connect() as conn:
            rows = conn.execute("""SELECT r.*, (SELECT v.effective FROM cost_reference_versions v
                    WHERE v.kind=r.kind AND v.ref=r.id ORDER BY v.id DESC LIMIT 1) AS effective_from
                FROM cost_references r ORDER BY r.kind,r.id""")
            items = []
            for row in rows:
                value = json.loads(row["data"])
                value.setdefault("code", row["id"])
                items.append(dict(value, id=row["id"], kind=row["kind"], effective_from=row["effective_from"]))
            return items

    def products(self):
        with self.connect() as conn:
            return [dict(json.loads(r["data"]), revision=r["revision"])
                    for r in conn.execute("SELECT data,revision FROM cost_products ORDER BY article")]

    def resolve(self, identifier, marketplace=None):
        rows = self.products()
        if not marketplace:
            exact = [r for r in rows if r["article"] == identifier]
            if exact:
                return exact[0]
        found = [r for r in rows if any((marketplace is None or link["marketplace"] == marketplace)
                  and identifier in (link["code"], link["marketplace_id"]) for link in r["links"])]
        if len(found) > 1:
            raise CatalogError("Укажите площадку для этого кода.")
        return found[0] if found else None

    @staticmethod
    def _output(row, *, redact_exact=True):
        if not row:
            return None
        data = json.loads(row["snapshot"])
        if redact_exact:
            data["calculation"].pop("exact", None)
        return dict(data, version=row["id"], effective_from=row["effective"], recorded_at=row["at"], reason=row["reason"])

    def at(self, article, day, *, redact_exact=True):
        with self.connect() as conn:
            row = conn.execute("""SELECT * FROM cost_versions WHERE article=? AND effective<=?
                ORDER BY effective DESC,id DESC LIMIT 1""", (article, effective(day))).fetchone()
        return self._output(row, redact_exact=redact_exact)

    def history(self, article, first="", last="9999-12-31"):
        with self.connect() as conn:
            rows = conn.execute("""SELECT * FROM cost_versions WHERE article=? AND effective BETWEEN ? AND ?
                ORDER BY effective DESC,id DESC""", (article, first, last)).fetchall()
        return [self._output(row) for row in rows]

    def uniform(self, article, first, last):
        version = self.at(article, first, redact_exact=False)
        if not version or version["product"]["status"] != "active" or version["calculation"]["status"] != "calculated":
            return None
        versions = [row for row in self.history(article, first, last) if row["effective_from"] > first]
        if versions:
            return None
        return version

    def save_margin(self, article, market, month, value):
        with self.connect() as conn:
            conn.execute("""INSERT INTO cost_margins VALUES(?,?,?,?,?) ON CONFLICT(article,market,month)
                DO UPDATE SET saved_at=excluded.saved_at,data=excluded.data""",
                (article, market, month, datetime.now(timezone.utc).isoformat(), _json(value)))

    def margins(self, month):
        with self.connect() as conn:
            return {(r["article"], r["market"]): dict(json.loads(r["data"]), saved_at=r["saved_at"]) for r in
                    conn.execute("SELECT * FROM cost_margins WHERE month=?", (month,))}

    def inbox(self, status="pending"):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM cost_inbox WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        return [dict(json.loads(r["data"]), id=r["id"], observed_at=r["at"], status=r["status"]) for r in rows]

    def journal(self, limit=100):
        with self.connect() as conn:
            return [dict(r, fields=json.loads(r["fields"])) for r in
                    conn.execute("SELECT * FROM cost_audit ORDER BY id DESC LIMIT ?", (limit,))]

    def csv_export(self):
        output = io.StringIO()
        sheet = csv.writer(output)
        sheet.writerow(("kind", "id", "data"))
        for row in self.references():
            kind, ident = row.pop("kind"), row.pop("id")
            row.pop("effective_from")
            sheet.writerow((kind, "'" + ident if ident.startswith(("=", "+", "-", "@")) else ident, _json(row)))
        for row in self.products():
            row.pop("revision")
            ident = row["article"]
            sheet.writerow(("product", "'" + ident if ident.startswith(("=", "+", "-", "@")) else ident, _json(row)))
        return output.getvalue()

    def csv_import(self, source, actor):
        sheet = csv.DictReader(io.StringIO(source.lstrip("\ufeff")))
        if sheet.fieldnames != ["kind", "id", "data"]:
            raise CatalogError("Используйте заголовки CSV kind,id,data.")
        rows = list(sheet)
        if not rows or len(rows) > 200:
            raise CatalogError("В одном файле должно быть от 1 до 200 строк.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in sorted(rows, key=lambda item: item["kind"] == "product"):
                data = json.loads(row["data"])
                if not isinstance(data, dict):
                    raise CatalogError("Данные в столбце data должны быть объектом.")
                if row["kind"] == "product":
                    self._save_product(conn, data, today(), actor)
                else:
                    ident = row["id"]
                    if ident.startswith(("'=", "'+", "'-", "'@")):
                        ident = ident[1:]
                    self._save_reference(conn, row["kind"], ident, data, today(), actor)
        return len(rows)


def read(store, name, args):
    market = args.get("marketplace")
    meta = {}
    if name == "cost_inbox":
        from cost_sync import sync_summary

        items = store.inbox()
        meta["calculator_sync"] = sync_summary(store)
    elif name == "cost_list":
        items = []
        for row in store.products():
            if market and not any(p["marketplace"] == market for p in row["links"]):
                continue
            if args.get("product_status", row["status"]) != row["status"]:
                continue
            if args.get("query", "").casefold() not in (row["article"] + " " + row["name"]).casefold():
                continue
            version = store.at(row["article"], today())
            amount = (version or {}).get("calculation", {}).get("amount")
            if "has_cost" in args and args["has_cost"] != (amount is not None):
                continue
            items.append({k: row[k] for k in ("article", "name", "status", "links", "photos")} |
                         {"cost_per_unit": amount, "currency": "RUB", "version": (version or {}).get("version"),
                          "updated_at": (version or {}).get("recorded_at")})
    else:
        row = store.resolve(args["article"], market)
        if not row:
            return result([], status="cost_card_not_found", complete=False, read_only=True)
        if name == "cost_card":
            version = store.at(row["article"], args.get("date"))
            return result([version] if version else [], internal_article=row["article"],
                          status=version["product"]["status"] if version else "no_effective_version",
                          read_only=True, untrusted_data=True)
        items = store.history(row["article"], args.get("date_from", ""), args.get("date_to", "9999-12-31"))
    offset, limit = args.get("offset", 0), args.get("limit", 50)
    end = offset + limit
    return result(items[offset:end], end if end < len(items) else None,
                  total_items=len(items), read_only=True, untrusted_data=True, **meta)
