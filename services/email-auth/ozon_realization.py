"""Bounded projections of Ozon's monthly realization report.

The report's month is a realization-report period, not an order-date filter.
It does not establish later returns or the completeness of historical SKUs.
"""
import calendar
import json
import re
import urllib.request
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext

import ozon_details

OPERATION = 'ozon_realization_article_month'
ENDPOINT = 'https://api-seller.ozon.ru/v1/finance/realization/posting'


def validate_args(args):
    if not isinstance(args, dict) or set(args) != {'offer_id', 'year', 'month'}:
        raise ValueError('Invalid realization arguments')
    ozon_details._text(args['offer_id'], 256)
    if type(args['year']) is not int or not 2023 <= args['year'] <= 9999:
        raise ValueError('Invalid year')
    if type(args['month']) is not int or not 1 <= args['month'] <= 12:
        raise ValueError('Invalid month')


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
    fields = ('price_per_instance', 'amount', 'compensation', 'commission', 'bonus',
              'standard_fee', 'total', 'stars', 'bank_coinvestment', 'pick_up_point_coinvestment')
    return {'quantity': quantity, **{field: _decimal(value[field]) if value.get(field) is not None else None
                                      for field in fields}}


def project_response(args, data, creds):
    validate_args(args)
    if not isinstance(data, dict) or not isinstance(data.get('header'), dict):
        raise ValueError('Invalid realization report')
    rows = ozon_details._items(data.get('rows'), 10000)
    header = data['header']
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
    currency = header.get('currency_sys_name')
    if currency is not None:
        currency = ozon_details._text(currency, 16, secret=secret)
        if not re.fullmatch(r'[A-Z]{3,16}', currency): raise ValueError('Invalid report currency')
    selected, row_numbers, missing_identity = [], set(), 0
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid realization row')
        number = row.get('row_number')
        if type(number) is not int or number < 1 or number in row_numbers:
            raise ValueError('Invalid realization row number')
        row_numbers.add(number)
        item = row.get('item')
        if not isinstance(item, dict) or not isinstance(item.get('offer_id'), str):
            missing_identity += 1
            continue
        offer_id = ozon_details._text(item['offer_id'], 256, secret=secret)
        if offer_id != args['offer_id']: continue
        sku = ozon_details._optional_sku(item.get('sku'), secret)
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
    return {'source': 'Ozon Seller API /v1/finance/realization/posting',
            'offer_id': args['offer_id'], 'year': args['year'], 'month': args['month'],
            'period_basis': 'realization_report_month', 'order_date_filter_used': False,
            'report_start_date': report_start, 'report_end_date': report_stop, 'currency': currency,
            'observed_delivery_units': delivered, 'observed_return_units_signed': returned,
            'observed_delivery_rows': len(delivery_rows), 'observed_return_rows': len(return_rows),
            'returned_units_sign_convention': 'provider_value', 'selected_rows': selected,
            'matched_rows': len(selected), 'report_rows_with_missing_offer_id': missing_identity,
            'financial_completeness': 'not_verified', 'row_identity': 'not_verified',
            'quantity_additivity': 'not_verified', 'subsequent_returns': 'not_verified',
            'note': ('Месяц относится к отчёту реализации, а не к дате создания заказа. '
                     'Количество — арифметика полученных строк; уникальность и аддитивность операций '
                     'не подтверждены. Будущие возвраты и полнота исторических SKU не подтверждены. '
                     'Возвраты переданы со знаком Ozon, без вычитания по догадке.'),
            'read_only': True, 'untrusted_data': True}
