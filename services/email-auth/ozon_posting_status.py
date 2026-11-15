"""Read-only current status counts for one bounded page of Ozon postings.

Only counts are projected; posting numbers, product IDs, names, prices and
buyer data are deliberately excluded. The caller supplies the bounded HTTPS
transport and enforces deadlines.
"""
import json
import re
import urllib.request
from datetime import date, datetime, timezone

OPERATION = 'ozon_posting_status'
ENDPOINTS = {
    'FBO': 'https://api-seller.ozon.ru/v3/posting/fbo/list',
    'FBS': 'https://api-seller.ozon.ru/v4/posting/fbs/list',
}
KNOWN_STATUSES = frozenset(('awaiting_registration', 'acceptance_in_progress', 'awaiting_approve',
    'awaiting_packaging', 'awaiting_deliver', 'arbitration', 'client_arbitration',
    'delivering', 'driver_pickup', 'delivered', 'cancelled', 'not_accepted', 'sent_by_seller'))


def _cursor(value):
    return isinstance(value, str) and len(value) <= 512 and not re.search(r'[\x00-\x1f\x7f]', value)


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'date_from', 'date_to', 'scheme', 'cursor', 'limit'}:
        raise ValueError('Invalid arguments')
    if not {'date_from', 'date_to', 'scheme'} <= set(args) or not isinstance(args['scheme'], str) or args['scheme'] not in ENDPOINTS:
        raise ValueError('Invalid arguments')
    values = []
    for field in ('date_from', 'date_to'):
        value = args[field]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Invalid date')
        try:
            values.append(date.fromisoformat(value))
        except ValueError:
            raise ValueError('Invalid date') from None
    if not 0 <= (values[1] - values[0]).days < 90:
        raise ValueError('Period must be 1 to 90 days')
    if not _cursor(args.get('cursor', '')):
        raise ValueError('Invalid cursor')
    if type(args.get('limit', 100)) is not int or not 1 <= args.get('limit', 100) <= 100:
        raise ValueError('Invalid limit')


def build_request(args, creds):
    validate_args(args)
    body = {'cursor': args.get('cursor', ''), 'limit': args.get('limit', 100),
            'filter': {'since': args['date_from'] + 'T00:00:00Z',
                       'to': args['date_to'] + 'T23:59:59.999999999Z'},
            'sort_dir': 'asc', 'with': {'financial_data': False, 'analytics_data': False,
                                         'legal_info': False}}
    return urllib.request.Request(ENDPOINTS[args['scheme']], data=json.dumps(body).encode('utf-8'),
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'],
                 'Content-Type': 'application/json'}, method='POST')


def project_response(args, data, creds):
    validate_args(args)
    if not isinstance(data, dict):
        raise ValueError('Invalid response')
    parent = data.get('result') if isinstance(data.get('result'), dict) else data
    rows, has_next = parent.get('postings'), parent.get('has_next')
    if not isinstance(rows, list) or len(rows) > args.get('limit', 100) or type(has_next) is not bool:
        raise ValueError('Unexpected posting schema')
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid posting')
        status = row.get('status')
        if not isinstance(status, str) or not re.fullmatch(r'[a-z_]{1,50}', status):
            raise ValueError('Invalid status')
        if status not in KNOWN_STATUSES:
            status = 'other'
        products = row.get('products')
        if not isinstance(products, list) or len(products) > 1000:
            raise ValueError('Invalid products')
        quantity = 0
        for product in products:
            if not isinstance(product, dict) or type(product.get('quantity')) is not int or not 0 <= product['quantity'] <= 1000000:
                raise ValueError('Invalid product quantity')
            quantity += product['quantity']
        bucket = counts.setdefault(status, {'postings': 0, 'units': 0})
        bucket['postings'] += 1
        bucket['units'] += quantity
    next_cursor = parent.get('cursor') if has_next else None
    if has_next:
        if not rows or not _cursor(next_cursor) or not next_cursor or next_cursor == args.get('cursor', ''):
            raise ValueError('Invalid next cursor')
        if any(value in next_cursor for value in creds.values() if isinstance(value, str) and value):
            raise ValueError('Unsafe next cursor')
    return {'source': 'Ozon Seller API ' + ENDPOINTS[args['scheme']].split('.ru', 1)[1],
            'scheme': args['scheme'], 'date_from': args['date_from'], 'date_to': args['date_to'],
            'period_timezone': 'UTC', 'as_of': datetime.now(timezone.utc).isoformat(),
            'page_counts': counts, 'page_postings': len(rows), 'has_more': has_next,
            'next_cursor': next_cursor, 'complete': not args.get('cursor') and not has_next,
            'read_only': True, 'untrusted_data': True,
            'note': ('Текущие статусы отправлений на момент запроса; это не окончательные выкупы, '
                     'возвраты, прибыль или сумма выплат. Период отбора API в UTC может не совпадать '
                     'с датой заказа в аналитике. Не смешивайте показатели с выкупами других площадок. '
                     'Для итогов схемы получите все страницы и сложите page_counts по статусам; '
                     'затем запросите вторую схему. Статусы могут изменяться между страницами.')}
