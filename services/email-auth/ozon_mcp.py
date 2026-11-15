"""Read-only Ozon request validation and bounded response projection for the bridge.

Transport, TLS, deadline and response-size limits remain the portal's responsibility.
These functions never select an arbitrary URL or expose untrusted provider errors.
"""
import json
import math
import re
import urllib.request

OPERATIONS = frozenset({'ozon_products', 'ozon_prices'})
PRICE_FIELDS = ('auto_action_enabled', 'currency_code', 'marketing_seller_price',
                'min_price', 'net_price', 'old_price', 'price', 'retail_price',
                'vat')
COMMISSION_FIELDS = ('fbo_deliv_to_customer_amount', 'fbo_direct_flow_trans_max_amount',
                     'fbo_direct_flow_trans_min_amount', 'fbo_return_flow_amount',
                     'fbs_deliv_to_customer_amount', 'fbs_direct_flow_trans_max_amount',
                     'fbs_direct_flow_trans_min_amount', 'fbs_first_mile_max_amount',
                     'fbs_first_mile_min_amount', 'fbs_return_flow_amount',
                     'sales_percent_fbo', 'sales_percent_fbs', 'sales_percent_rfbs',
                     'sales_percent_fbp')
PRODUCT_FIELDS = ('product_id', 'offer_id', 'archived', 'is_fbo_visible',
                  'is_fbs_visible', 'has_fbo_stocks', 'has_fbs_stocks', 'is_discounted')
PRICE_ITEM_FIELDS = ('product_id', 'offer_id', 'acquiring', 'volume_weight')


def _string(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and not re.search(r'[\x00-\x1f\x7f]', value)


def validate_args(operation, args):
    """Reject unexpected keys and types. One request reads at most 100 rows."""
    if operation not in OPERATIONS or not isinstance(args, dict): raise ValueError('Invalid Ozon operation')
    allowed = {'limit', 'last_id'} if operation == 'ozon_products' else {'limit', 'cursor', 'offer_ids'}
    if set(args) - allowed: raise ValueError('Invalid arguments')
    limit = args.get('limit', 20)
    if type(limit) is not int or not 1 <= limit <= 100: raise ValueError('Invalid limit')
    cursor_key = 'last_id' if operation == 'ozon_products' else 'cursor'
    if not _string(args.get(cursor_key, ''), 512): raise ValueError('Invalid cursor')
    if 'offer_ids' in args:
        offers = args['offer_ids']
        if not isinstance(offers, list) or len(offers) > 100 or any(not _string(item, 256) for item in offers):
            raise ValueError('Invalid offer IDs')


def build_request(operation, args, creds):
    """Construct a request to a fixed Ozon endpoint with credentials in headers."""
    validate_args(operation, args)
    filt = {'visibility': 'ALL'}
    if operation == 'ozon_products':
        url = 'https://api-seller.ozon.ru/v3/product/list'
        body = {'filter': filt, 'limit': args.get('limit', 20), 'last_id': args.get('last_id', '')}
    else:
        url = 'https://api-seller.ozon.ru/v5/product/info/prices'
        if args.get('offer_ids'): filt['offer_id'] = args['offer_ids']
        body = {'filter': filt, 'limit': args.get('limit', 20), 'cursor': args.get('cursor', '')}
    return urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'],
                 'Content-Type': 'application/json'}, method='POST')


def project_response(operation, args, data, creds):
    """Return allowlisted fields and an intact next-page cursor without secrets."""
    validate_args(operation, args)
    if not isinstance(data, dict): raise ValueError('Invalid provider response')
    parent = data.get('result') if operation == 'ozon_products' else data
    if not isinstance(parent, dict) or not isinstance(parent.get('items'), list):
        raise ValueError('Unexpected Ozon schema')
    rows = parent['items']
    limit = args.get('limit', 20)
    if len(rows) > limit: raise ValueError('Unexpected row count')
    secrets = [v for v in creds.values() if isinstance(v, str) and v]

    def clean(value):
        if isinstance(value, str):
            for secret in secrets: value = value.replace(secret, '[REDACTED]')
            return value[:256]
        if value is None or type(value) is bool: return value
        # JSON consumers in JavaScript cannot represent larger integers exactly.
        if type(value) is int: return str(value) if abs(value) > 9007199254740991 else value
        if type(value) is float and math.isfinite(value): return value
        raise ValueError('Invalid provider field')

    def pick(row, keys):
        if not isinstance(row, dict): raise ValueError('Invalid provider item')
        return {key: clean(row[key]) for key in keys if key in row}

    out = []
    for row in rows:
        item = pick(row, PRODUCT_FIELDS if operation == 'ozon_products' else PRICE_ITEM_FIELDS)
        if operation == 'ozon_prices':
            for key, fields in (('price', PRICE_FIELDS), ('commissions', COMMISSION_FIELDS)):
                if key in row and row[key] is not None: item[key] = pick(row[key], fields)
            if isinstance(row.get('price'), dict) and 'declared_price' in row['price']:
                declared = row['price']['declared_price']
                # Current API returns a currency/amount object; preserve null or
                # scalar legacy values without accepting arbitrary nested data.
                item['price']['declared_price'] = (pick(declared, ('amount', 'currency'))
                    if isinstance(declared, dict) else clean(declared))
        out.append(item)
    cursor_key = 'last_id' if operation == 'ozon_products' else 'cursor'
    cursor = parent.get(cursor_key, '')
    if not _string(cursor, 512): raise ValueError('Invalid provider cursor')
    # Never turn a redacted cursor into a false end-of-pagination signal.
    if any(secret in cursor for secret in secrets): raise ValueError('Unsafe provider cursor')
    return {'items': out, cursor_key: cursor, 'first_page_only': not bool(args.get(cursor_key)),
            'read_only': True, 'untrusted_data': True}
