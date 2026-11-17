"""Bounded projections of Ozon's monthly realization report.

The report's month is a realization-report period, not an order-date filter.
It does not establish later returns or the completeness of historical SKUs.
"""
import calendar
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum

import ozon_details

OPERATION = 'ozon_realization_article_month'
ENDPOINT = 'https://api-seller.ozon.ru/v1/finance/realization/posting'
COMMISSION_FIELDS = ('price_per_instance', 'amount', 'compensation', 'commission', 'bonus',
                     'standard_fee', 'total', 'stars', 'bank_coinvestment', 'pick_up_point_coinvestment')


class IdentityScope(Enum):
    """The monthly endpoint supplies one response; other pages are synthetic."""
    MONTHLY_RESPONSE = 'entire_available_monthly_response'
    SYNTHETIC_PAGES = 'synthetic_pages_only'


@dataclass(frozen=True)
class IdentityObservation:
    row_number: int
    offer_id: str | None
    sku: int | str | None


@dataclass(frozen=True)
class IdentityPage:
    scope: IdentityScope
    sequence: int
    observations: tuple[IdentityObservation, ...]
    dataset_id: str
    report_month: str


def scan_identity_pages(pages, scope, *, secret=None):
    """Inspect every supplied identity; synthetic pages are not an Ozon API.

    The result describes only supplied observations. A clean scan cannot prove
    complete provider data, historical ownership, or an atomic snapshot.
    """
    if not isinstance(scope,IdentityScope) or type(pages) not in (list,tuple):
        raise ValueError('Invalid identity scope')
    pages=pages[:33]
    if (not 1 <= len(pages) <= 32 or
            (scope is IdentityScope.MONTHLY_RESPONSE and len(pages)!=1)):
        raise ValueError('Invalid identity scope')
    by_sku, missing, total, sequences = {}, [], 0, set()
    page_order, seen_rows, repeated_rows = [], set(), set()
    dataset_id=report_month=None
    for page in pages:
        if (type(page) is not IdentityPage or page.scope is not scope or
                type(page.sequence) is not int or not 1 <= page.sequence <= 32 or page.sequence in sequences or
                type(page.observations) is not tuple or type(page.dataset_id) is not str or
                not re.fullmatch(r'[A-Za-z0-9:_-]{1,64}',page.dataset_id) or
                type(page.report_month) is not str or
                not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',page.report_month)):
            raise ValueError('Invalid identity page')
        try:date.fromisoformat(page.report_month+'-01')
        except ValueError:raise ValueError('Invalid identity page') from None
        ozon_details._text(page.dataset_id,64,secret=secret)
        ozon_details._text(page.report_month,7,secret=secret)
        if dataset_id is None:dataset_id,report_month=page.dataset_id,page.report_month
        elif (dataset_id,report_month)!=(page.dataset_id,page.report_month):
            raise ValueError('Mixed identity datasets')
        sequences.add(page.sequence)
        page_order.append(page.sequence)
        row_numbers=set()
        for item in page.observations:
            total+=1
            if (total>10000 or type(item) is not IdentityObservation or
                    type(item.row_number) is not int or not 1 <= item.row_number <= 9007199254740991 or
                    item.row_number in row_numbers or type(item.offer_id) not in (str,type(None)) or
                    type(item.sku) not in (str,int,type(None))):
                raise ValueError('Invalid identity observation')
            row_numbers.add(item.row_number)
            if item.row_number in seen_rows: repeated_rows.add(item.row_number)
            seen_rows.add(item.row_number)
            offer=(ozon_details._text(item.offer_id,256,secret=secret)
                   if item.offer_id is not None and item.offer_id != '' else None)
            sku=None if item.sku == '' else ozon_details._optional_sku(item.sku,secret)
            location={'page':page.sequence,'row_number':item.row_number}
            absent=[key for key,value in (('offer_id',offer),('sku',sku)) if value is None]
            if absent:
                missing.append({**location,'missing':absent})
            if offer is not None and sku is not None:
                by_sku.setdefault(sku,{}).setdefault(offer,[]).append(location)
    conflicts=[]
    for sku in sorted(by_sku,key=int):
        offers=by_sku[sku]
        if len(offers)>1:
            conflicts.append({'sku':sku,'offers':[{'offer_id':offer,'locations':offers[offer][:3]}
                for offer in sorted(offers)[:5]],
                'locations_truncated':any(len(locations)>3 for locations in offers.values()),
                'offers_truncated':len(offers)>5})
    return {'scope':scope.value,'dataset_id':dataset_id,'report_month':report_month,
            'observations_scanned':total,'conflict_count':len(conflicts),
            'identity_conflicts':conflicts[:20],'missing_count':len(missing),
            'identity_incomplete_rows':missing[:50],
            'page_sequence_contiguous':page_order==list(range(1,len(page_order)+1)),
            'repeated_row_numbers':sorted(repeated_rows)[:50],
            'row_identity':'not_verified',
            'diagnostics_truncated':(len(conflicts)>20 or len(missing)>50 or len(repeated_rows)>50 or
                                     any(c['offers_truncated'] or c['locations_truncated'] for c in conflicts)),
            'source_completeness':'not_verified','snapshot_consistency':'not_verified'}


def _monthly_identity_page(rows,secret,args):
    observations=[]
    for row in rows:
        if not isinstance(row,dict): raise ValueError('Invalid realization row')
        item=row.get('item')
        if item is not None and not isinstance(item,dict): raise ValueError('Invalid realization item')
        item=item or {}
        observations.append(IdentityObservation(row.get('row_number'),item.get('offer_id'),item.get('sku')))
    return IdentityPage(IdentityScope.MONTHLY_RESPONSE,1,tuple(observations),
                        'single_monthly_response',f"{args['year']:04d}-{args['month']:02d}")


def _snapshot_row(row):
    """Copy only used fields; caller must not mutate input during this copy.

    The provider worker owns its decoded JSON. This is not an atomic provider
    snapshot and does not establish historical or cross-request consistency.
    """
    if type(row) is not dict: raise ValueError('Invalid realization row')
    def capture(value,fields):
        if value is None:return None
        if type(value) is not dict:return object()
        return {field:value.get(field) for field in fields if field in value}
    return {'row_number':row.get('row_number'),
            'item':capture(row.get('item'),('offer_id','sku','name')),
            'order':capture(row.get('order'),('posting_number','created_date')),
            'delivery_commission':capture(row.get('delivery_commission'),('quantity',)+COMMISSION_FIELDS),
            'return_commission':capture(row.get('return_commission'),('quantity',)+COMMISSION_FIELDS)}


def validate_args(args):
    if (not isinstance(args, dict) or
            set(args) - {'offer_id', 'year', 'month', 'view'} or
            not {'offer_id', 'year', 'month'} <= set(args)):
        raise ValueError('Invalid realization arguments')
    ozon_details._text(args['offer_id'], 256)
    if type(args['year']) is not int or not 2023 <= args['year'] <= 9999:
        raise ValueError('Invalid year')
    if type(args['month']) is not int or not 1 <= args['month'] <= 12:
        raise ValueError('Invalid month')
    if args.get('view', 'summary') not in ('summary', 'details'):
        raise ValueError('Invalid report view')


def build_request(args, creds):
    validate_args(args)
    body = {'year': args['year'], 'month': args['month']}
    return urllib.request.Request(ENDPOINT, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'],
                 'Content-Type': 'application/json'})


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError('Invalid report number')
    try: number = Decimal(str(value))
    except InvalidOperation: raise ValueError('Invalid report number') from None
    if not number.is_finite() or len(number.as_tuple().digits) > 50 or abs(number.as_tuple().exponent) > 20:
        raise ValueError('Invalid report number')
    return format(number, 'f')


def _commission(value):
    if value is None: return None
    if not isinstance(value, dict): raise ValueError('Invalid report commission')
    quantity = value.get('quantity')
    if type(quantity) is not int or not -1000000 <= quantity <= 1000000:
        raise ValueError('Invalid report quantity')
    return {'quantity': quantity, **{field: _decimal(value[field]) if value.get(field) is not None else None
                                      for field in COMMISSION_FIELDS}}


def project_response(args, data, creds):
    validate_args(args)
    args=dict(args)
    if not isinstance(data, dict) or not isinstance(data.get('header'), dict):
        raise ValueError('Invalid realization report')
    source_rows=data.get('rows')
    if type(source_rows) is not list: raise ValueError('Invalid realization rows')
    source_rows=source_rows[:10001]
    ozon_details._items(source_rows,10000)
    rows=[_snapshot_row(row) for row in source_rows]
    header = {key:data['header'].get(key) for key in ('start_date','stop_date','currency_sys_name')}
    secret = creds.get('api_key') if isinstance(creds.get('api_key'), str) else None
    start = date(args['year'], args['month'], 1).isoformat()
    stop = date(args['year'], args['month'], calendar.monthrange(args['year'], args['month'])[1]).isoformat()
    # Ozon may add an ISO time component; reject arbitrary suffixes.
    report_start = ozon_details._text(header.get('start_date'), 64, secret=secret)
    report_stop = ozon_details._text(header.get('stop_date'), 64, secret=secret)
    def report_date(value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?)?', value):
            raise ValueError('Invalid report date')
        try:
            return (date.fromisoformat(value) if len(value) == 10 else
                    datetime.fromisoformat(value.replace('Z','+00:00')).date()).isoformat()
        except ValueError: raise ValueError('Invalid report date') from None
    if report_date(report_start) != start or report_date(report_stop) != stop:
        raise ValueError('Report period mismatch')
    # The endpoint returns a single monthly report, not pages. Scan every
    # available row before selecting the requested offer or summing quantities.
    identity_page=_monthly_identity_page(rows,secret,args)
    identity=scan_identity_pages([identity_page],IdentityScope.MONTHLY_RESPONSE,secret=secret)
    if identity['conflict_count'] or identity['missing_count']:
        return {'status':'blocked',
            'reason':'sku_offer_id_conflict' if identity['conflict_count'] else 'identity_incomplete',
            'source':'Ozon Seller API /v1/finance/realization/posting',
            'offer_id':args['offer_id'],'year':args['year'],'month':args['month'],
            'period_basis':'realization_report_month','order_date_filter_used':False,
            'identity_scan':identity,'identity_conflicts':identity['identity_conflicts'],
            'observed_delivery_units':None,'observed_return_units_signed':None,
            'matched_rows':None,'financial_completeness':'not_verified',
            'quantity_additivity':'not_verified','read_only':True,'untrusted_data':True,
            'note':('Расчёт остановлен: в доступном ответе SKU связан с разными артикулами '
                    'или отсутствует идентификатор строки. Сомнительные количества не выдаются.')}
    currency = header.get('currency_sys_name')
    if currency is not None:
        currency = ozon_details._text(currency, 16, secret=secret)
        if not re.fullmatch(r'[A-Z]{3,16}', currency): raise ValueError('Invalid report currency')
    selected, row_numbers, missing_identity = [], set(), 0
    for row,observed in zip(rows,identity_page.observations):
        if not isinstance(row, dict): raise ValueError('Invalid realization row')
        number = observed.row_number
        if type(number) is not int or number < 1 or number in row_numbers:
            raise ValueError('Invalid realization row number')
        row_numbers.add(number)
        item = row.get('item')
        if (not isinstance(item,dict) or type(row.get('row_number')) is not int or
                row['row_number'] != observed.row_number or
                type(item.get('offer_id')) is not type(observed.offer_id) or
                item.get('offer_id') != observed.offer_id or
                type(item.get('sku')) is not type(observed.sku) or item.get('sku') != observed.sku):
            raise ValueError('Identity changed during projection')
        offer_id = ozon_details._text(observed.offer_id, 256, secret=secret)
        if offer_id != args['offer_id']: continue
        sku = ozon_details._optional_sku(observed.sku, secret)
        name = item.get('name')
        if name is not None: name = ozon_details._text(name, 1024, empty=True, secret=secret)
        order = row.get('order')
        if not isinstance(order, dict): raise ValueError('Invalid report order')
        posting = order.get('posting_number')
        if posting is not None: posting = ozon_details._text(posting, 128, secret=secret)
        created = order.get('created_date')
        if created is not None: created = ozon_details._text(created, 64, secret=secret)
        selected.append({'row_number': number, 'offer_id': offer_id, 'name': name, 'sku': sku,
                         'posting_number': posting, 'order_created_date': created,
                         'delivery_commission': _commission(row.get('delivery_commission')),
                         'return_commission': _commission(row.get('return_commission'))})
    delivery_rows = [row['delivery_commission'] for row in selected if row['delivery_commission'] is not None]
    return_rows = [row['return_commission'] for row in selected if row['return_commission'] is not None]
    delivered = sum(row['quantity'] for row in delivery_rows) if delivery_rows else None
    returned = sum(row['quantity'] for row in return_rows) if return_rows else None
    result = {'status':'partial','source': 'Ozon Seller API /v1/finance/realization/posting',
            'offer_id': args['offer_id'], 'year': args['year'], 'month': args['month'],
            'period_basis': 'realization_report_month', 'order_date_filter_used': False,
            'report_start_date': report_start, 'report_end_date': report_stop, 'currency': currency,
            'observed_delivery_units': delivered, 'observed_return_units_signed': returned,
            'observed_delivery_rows': len(delivery_rows), 'observed_return_rows': len(return_rows),
            'returned_units_sign_convention': 'provider_value',
            'matched_rows': len(selected), 'report_rows_with_missing_offer_id': missing_identity,
            'identity_scan':identity,'identity_conflicts':[],
            'financial_completeness': 'not_verified', 'row_identity': 'not_verified',
            'quantity_additivity': 'not_verified', 'subsequent_returns': 'not_verified',
            'note': ('Месяц относится к отчёту реализации, а не к дате создания заказа. '
                     'Количество — арифметика полученных строк; уникальность и аддитивность операций '
                     'не подтверждены. Будущие возвраты и полнота исторических SKU не подтверждены. '
                     'Возвраты переданы со знаком Ozon, без вычитания по догадке.'),
            'read_only': True, 'untrusted_data': True}
    if args.get('view', 'summary') == 'details':
        result['selected_rows'] = selected
    else:
        groups = {}
        for row in selected:
            group = groups.setdefault(row['sku'], {'sku': row['sku'], 'names': set(),
                'matched_rows': 0, 'delivery_rows': 0, 'return_rows': 0,
                'observed_delivery_units': None, 'observed_return_units_signed': None})
            if row['name'] is not None: group['names'].add(row['name'])
            group['matched_rows'] += 1
            for field, count, total in (('delivery_commission', 'delivery_rows', 'observed_delivery_units'),
                                        ('return_commission', 'return_rows', 'observed_return_units_signed')):
                value = row[field]
                if value is not None:
                    group[count] += 1
                    group[total] = (group[total] or 0) + value['quantity']
        result['by_sku'] = []
        for sku in sorted(groups, key=lambda item: (item is None, int(item) if item else 0)):
            group = groups[sku]
            group['names'] = sorted(group['names'])
            result['by_sku'].append(group)
        result['summary_note'] = ('Группировка отражает только наблюдаемые количества; строки, '
                                  'цены и суммы доступны в details. Финансовые показатели на единицу '
                                  'не рассчитаны: полнота и правила сложения не подтверждены.')
    return result
