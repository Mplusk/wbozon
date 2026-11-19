"""Read completed Ozon settlement exports from the provider report directory."""
import csv
from datetime import date, datetime, timedelta
import io
import json
import re
import time
import urllib.request
from urllib.parse import urlsplit
from xml.etree import ElementTree
import zipfile

from market_https import VerifiedHTTPS
from seller_views import SourceIssue,amount
from wb_mcp import NoRedirect


def _download(url,deadline):
    parts=urlsplit(url)
    if (parts.scheme!="https" or parts.netloc not in ("ir-20.ozone.ru","cdn1.ozone.ru") or parts.fragment
            or not parts.path.startswith("/s3/")):
        raise SourceIssue(status="unsupported_report_storage",source="/v1/report/info")
    opener=urllib.request.build_opener(NoRedirect,VerifiedHTTPS())
    with opener.open(urllib.request.Request(url,headers={"Accept":"application/octet-stream"}),timeout=max(.2,min(8,deadline-time.monotonic()))) as response:
        blob=response.read(16*1024*1024+1)
    if len(blob)>16*1024*1024:raise ValueError("Report size")
    return blob


def _xml(archive,path):
    node=archive.getinfo(path)
    if node.file_size>32*1024*1024:raise ValueError("Worksheet size")
    xml=archive.read(node)
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:raise ValueError("Worksheet XML")
    return ElementTree.fromstring(xml)


def _lines(blob):
    if not blob.startswith(b"PK"):
        text=blob.decode("utf-8-sig")
        yield from csv.reader(io.StringIO(text),delimiter=";" if ";" in text.splitlines()[0] else ",")
        return
    with zipfile.ZipFile(io.BytesIO(blob)) as book:
        strings=[]
        if "xl/sharedStrings.xml" in book.namelist():
            for s in _xml(book,"xl/sharedStrings.xml").findall("{*}si"):
                strings.append("".join(t.text or "" for t in s.iterfind(".//{*}t")))
        for sheet in sorted(n for n in book.namelist() if re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml",n)):
            for row in _xml(book,sheet).iterfind(".//{*}sheetData/{*}row"):
                cells=[]
                for cell in row.findall("{*}c"):
                    col=re.match(r"[A-Z]{1,3}",cell.attrib["r"])
                    if not col:raise ValueError("Cell address")
                    index=0
                    for letter in col[0]:index=index*26+ord(letter)-64
                    if index>256:raise ValueError("Column count")
                    cells.extend([""]*(index-len(cells)))
                    value=cell.findtext("{*}v","")
                    if cell.get("t")=="s":value=strings[int(value)]
                    elif cell.get("t")=="inlineStr":value="".join(t.text or "" for t in cell.iterfind(".//{*}t"))
                    cells[index-1]=value
                yield cells


COLS={"дата":"date","дата операции":"date","дата начисления":"date","sku":"sku","ozon id":"sku","артикул":"article",
      "артикул продавца":"article","количество":"quantity","количество шт":"quantity","сумма":"amount","сумма компенсации":"amount",
      "сумма декомпенсации":"amount","основание":"reason","причина":"reason","тип операции":"reason","валюта":"currency",
      "номер операции":"operation_id","номер отправления":"posting_number"}


def report_month(params):
    if isinstance(params,str):
        try:params=json.loads(params)
        except ValueError:params={"date":params}
    if not isinstance(params,dict):return None
    return str(params.get("date") or params.get("month") or params.get("period") or "")[:7]


def operation_date(raw):
    text=str(raw).strip()
    if re.fullmatch(r"[0-9]{5}(?:\.0+)?",text):
        return date(1899,12,30)+timedelta(days=int(float(text)))
    try:return date.fromisoformat(text[:10])
    except ValueError:return datetime.strptime(text,"%d.%m.%Y").date()


def settlements(r,reports,month,report_type):
    matches=[]
    for report in reports:
        if report.get("report_type")!=report_type:continue
        if report_month(report.get("params"))!=month:continue
        matches.append(report)
    if not matches:
        raise SourceIssue(source="/v1/report/list",status="no_confirmed_export_for_month",month=month,report_type=report_type)
    for report in sorted(matches,key=lambda x:x.get("created_at",""),reverse=True):
        data=r.fetch("ozon_settlement_reports","report_info",{"body":{"code":report["code"]}})["result"]
        if data.get("report_type")!=report_type:raise ValueError("report_type")
        if not report.get("code") or data.get("code")!=report["code"]:raise ValueError("report_code")
        if report_month(data.get("params"))!=month:raise ValueError("report_month")
        if str(data.get("status","")).lower() not in ("success","ready","completed") or not data.get("file"):continue
        with r.wire.progress("ozon-settlement-export-v2",[month,report_type,report["code"]]) as (saved,save):
            if saved is None or "rows" not in saved:
                cols=None;known_currency=None;items=[];by_operation={}
                for line in _lines(_download(data["file"],r.deadline)):
                    if not any(line):continue
                    headers=[re.sub(r"[^а-яa-z0-9]+"," ",str(x).lower().replace("₽"," руб")).strip() for x in line]
                    mapping=[COLS.get(h) or COLS.get(h.removesuffix(" руб").removesuffix(" rub")) for h in headers]
                    if "amount" in mapping and ("sku" in mapping or "article" in mapping):
                        cols=mapping;known_currency="RUB" if any(("руб" in h or "rub" in h) and m=="amount" for h,m in zip(headers,cols)) else None;continue
                    if cols is None:continue
                    if any(h in ("итого","всего") for h in headers[:2]):continue
                    row={k:v for k,v in zip(cols,line) if k and v!=""}
                    if not row:continue
                    if "amount" not in row or not (row.get("sku") or row.get("article")):raise ValueError("Settlement amount or SKU")
                    row["amount"]=amount(str(row["amount"]).replace(" ","").replace("\xa0","").replace(",","."))
                    if "quantity" in row:row["quantity"]=int(row["quantity"])
                    if "date" in row:
                        stamp=operation_date(row["date"])
                        if stamp.strftime("%Y-%m")!=month:raise ValueError("settlement_row_month")
                        row["date"]=stamp.isoformat()
                    row.update(currency=row.get("currency") or known_currency,month=month,kind=report_type)
                    if row.get("operation_id"):
                        key=(row["operation_id"],row.get("sku"),row.get("article"),row.get("posting_number"))
                        previous=by_operation.get(key)
                        if previous is not None:
                            if previous!=row:raise ValueError("conflicting_settlement_operation")
                            continue
                        by_operation[key]=row
                    items.append(row)
                if cols is None:raise ValueError("Settlement header")
                saved={"rows":items};save(saved,86400)
            for row in saved["rows"]:
                if not row.get("date"):
                    raise ValueError("settlement_operation_date")
                if not r.first.isoformat()<=row["date"]<=r.last.isoformat():continue
                yield from r.product(row,row.get("article"),row.get("sku"))
            r.meta.setdefault("verified_exports",[]).append({"month":month,"type":report_type})
            return
    raise SourceIssue(status="export_not_ready",source="/v1/report/info",month=month,report_type=report_type)
