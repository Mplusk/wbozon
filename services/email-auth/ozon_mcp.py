"""Read-only Ozon request validation and bounded response projection for the bridge.

Transport, TLS, deadline and response-size limits remain the portal's responsibility.
These functions never select an arbitrary URL or expose untrusted provider errors.
"""
import json
import math
import re
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, localcontext
import ozon_posting_status
import ozon_details
import ozon_article_summary
import ozon_realization
import ozon_accrual_day
import ozon_return_events

OPERATIONS = frozenset({'ozon_products', 'ozon_prices', 'ozon_orders_analytics', 'ozon_posting_status', 'ozon_posting_candidates',
                        ozon_article_summary.OPERATION, ozon_realization.OPERATION,
                        ozon_accrual_day.OPERATION, ozon_return_events.OPERATION}) | ozon_details.OPERATIONS
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
                  'is_fbs_visible', 'has_fbo_stocks', 'has_fbs_stocks', 'is_discounted', 'sku')
PRODUCT_FLAGS = ('archived', 'is_fbo_visible', 'is_fbs_visible',
                 'has_fbo_stocks', 'has_fbs_stocks', 'is_discounted')
PRICE_ITEM_FIELDS = ('product_id', 'offer_id', 'acquiring', 'volume_weight')


def build_seller_info_request(creds):
    """Read the seller profile from a fixed endpoint; never accept a caller URL."""
    return urllib.request.Request('https://api-seller.ozon.ru/v1/seller/info',
        data=b'{}', headers={'Client-Id':creds['client_id'], 'Api-Key':creds['api_key'],
                           'Content-Type':'application/json'}, method='POST')


def project_company_currency(data, creds):
    """Return only a safe ISO-style company currency, not an analytics currency."""
    company = data.get('company') if isinstance(data, dict) else None
    currency = company.get('currency') if isinstance(company, dict) else None
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        return None
    if any(secret and secret in currency for secret in creds.values() if isinstance(secret, str)):
        return None
    return currency


def _string(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and not re.search(r'[\x00-\x1f\x7f]', value)


def _exact_identifier(value, api_key, *, list_allowed=False):
    """Keep identifiers intact; an unsafe or inexact value is never repaired."""
    if list_allowed and isinstance(value, list):
        if len(value) > 1000: raise ValueError('Too many provider identifiers')
        return [_exact_identifier(item, api_key) for item in value]
    if value is None: return None
    if type(value) is int:
        if value < 0 or len(str(value)) > 128: raise ValueError('Invalid provider identifier')
        text = str(value)
        if api_key and api_key in text: raise ValueError('Unsafe provider identifier')
        return text if value > 9007199254740991 else value
    if not _string(value, 256) or (api_key and api_key in value):
        raise ValueError('Unsafe provider identifier')
    return value


def _page_identity(items):
    """Report observable conflicts only inside this one untrusted page."""
    by_product, by_offer, by_sku, repeated = {}, {}, {}, {}
    incomplete, unverified_format, unverified_flags = [], [], []
    def absent(value):
        # Preserve these provider values, but never treat placeholders as IDs.
        return (value is None or value == '' or value == 0 or
                isinstance(value, str) and re.fullmatch(r'0+', value) is not None)

    def unverified_number(value):
        return (not re.fullmatch(r'[0-9]+', str(value)) or
                isinstance(value, str) and len(value) > 1 and value.startswith('0'))

    for index, item in enumerate(items):
        product, offer = item.get('product_id'), item.get('offer_id')
        sku = item.get('sku')
        sku_values = sku if isinstance(sku, list) else [sku]
        usable_skus = [str(value) for value in sku_values if not absent(value)]
        missing = []
        if absent(product): missing.append('product_id')
        if offer is None or offer == '': missing.append('offer_id')
        if not usable_skus or any(absent(value) for value in sku_values):
            missing.append('sku')
        if missing: incomplete.append({'row_index': index, 'fields': missing})
        uncertain = []
        if not absent(product) and unverified_number(product):
            uncertain.append('product_id')
        if any(not absent(value) and unverified_number(value) for value in sku_values):
            uncertain.append('sku')
        if uncertain: unverified_format.append({'row_index': index, 'fields': uncertain})
        unusual_flags = [key for key in PRODUCT_FLAGS
                         if key in item and item[key] is not None and type(item[key]) is not bool]
        if unusual_flags: unverified_flags.append({'row_index': index, 'fields': unusual_flags})
        if 'product_id' in missing: continue
        product = str(product)
        if 'offer_id' not in missing:
            by_product.setdefault(product, {}).setdefault(offer, set()).add(index)
            by_offer.setdefault(offer, {}).setdefault(product, set()).add(index)
            repeated.setdefault((product, offer), []).append(index)
        for value in usable_skus:
            by_sku.setdefault(value, {}).setdefault(product, set()).add(index)
    conflicts, seen = [], set()
    for kind, mapping in (('product_with_multiple_offers', by_product),
                          ('offer_with_multiple_products', by_offer),
                          ('sku_with_multiple_products', by_sku)):
        for groups in mapping.values():
            if len(groups) > 1:
                indices = tuple(sorted(set().union(*groups.values())))
                if (kind, indices) not in seen:
                    conflicts.append({'kind': kind, 'row_indices': list(indices)})
                    seen.add((kind, indices))
    return {'scope': 'current_page_only', 'row_indices_start_at': 0,
            'incomplete_rows': incomplete, 'conflicts': conflicts,
            'unverified_identifier_formats': unverified_format,
            'unverified_flag_formats': unverified_flags,
            'repeated_identity_rows': [indices for indices in repeated.values() if len(indices) > 1],
            'sku_coverage': 'not_verified', 'cross_page_conflicts': 'not_checked',
            'alternative_numeric_forms': 'not_checked',
            'name_in_projection': False}


def validate_args(operation, args):
    """Reject unexpected keys and types. One request reads at most 100 rows."""
    if operation not in OPERATIONS or not isinstance(args, dict): raise ValueError('Invalid Ozon operation')
    if operation == ozon_article_summary.OPERATION:
        return ozon_article_summary.validate_args(args)
    if operation == ozon_realization.OPERATION:
        return ozon_realization.validate_args(args)
    if operation == ozon_accrual_day.OPERATION:
        return ozon_accrual_day.validate_args(args)
    if operation == ozon_return_events.OPERATION:
        return ozon_return_events.validate_args(args)
    if operation in ozon_details.OPERATIONS:
        return ozon_details.validate_args(operation, args)
    if operation in ('ozon_posting_status','ozon_posting_candidates'):
        return ozon_posting_status.validate_args(args)
    if operation == 'ozon_orders_analytics':
        if set(args) != {'date_from', 'date_to'}: raise ValueError('Invalid arguments')
        try:
            if any(not isinstance(args[k], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args[k]) for k in ('date_from', 'date_to')):
                raise ValueError('Invalid date')
            start, end = date.fromisoformat(args['date_from']), date.fromisoformat(args['date_to'])
        except (TypeError, ValueError): raise ValueError('Invalid date') from None
        if not 0 <= (end - start).days < 90: raise ValueError('Period must be 1 to 90 days')
        return
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
    if operation == ozon_article_summary.OPERATION:
        raise ValueError('Composite operation has no single request')
    if operation == ozon_realization.OPERATION:
        return ozon_realization.build_request(args, creds)
    if operation == ozon_accrual_day.OPERATION:
        return ozon_accrual_day.build_request(args, creds)
    if operation == ozon_return_events.OPERATION:
        return ozon_return_events.build_request(args, creds)
    if operation in ozon_details.OPERATIONS:
        return ozon_details.build_request(operation, args, creds)
    if operation in ('ozon_posting_status','ozon_posting_candidates'):
        return ozon_posting_status.build_request(args, creds)
    filt = {'visibility': 'ALL'}
    if operation == 'ozon_orders_analytics':
        url = 'https://api-seller.ozon.ru/v1/analytics/data'
        # One row per day and at most 90 days: one request cannot truncate.
        body = {'date_from': args['date_from'], 'date_to': args['date_to'],
                'metrics': ['revenue', 'ordered_units'], 'dimension': ['day'],
                'limit': 100, 'offset': 0}
    elif operation == 'ozon_products':
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
    if operation == ozon_article_summary.OPERATION:
        raise ValueError('Composite operation has no single response')
    if operation == ozon_realization.OPERATION:
        return ozon_realization.project_response(args, data, creds)
    if operation == ozon_accrual_day.OPERATION:
        return ozon_accrual_day.project_response(args, data, creds)
    if operation == ozon_return_events.OPERATION:
        return ozon_return_events.project_response(args, data, creds)
    if operation in ozon_details.OPERATIONS:
        return ozon_details.project_response(operation, args, data, creds)
    if operation == 'ozon_posting_status':
        return ozon_posting_status.project_response(args, data, creds)
    if operation == 'ozon_posting_candidates':
        return ozon_posting_status.project_candidates(args, data, creds)
    if operation == 'ozon_orders_analytics':
        return _project_orders_analytics(args, data, creds)
    parent = data.get('result') if operation == 'ozon_products' else data
    if not isinstance(parent, dict) or not isinstance(parent.get('items'), list):
        raise ValueError('Unexpected Ozon schema')
    rows = parent['items']
    limit = args.get('limit', 20)
    if len(rows) > limit: raise ValueError('Unexpected row count')
    secrets = [v for v in creds.values() if isinstance(v, str) and v]
    api_key = creds.get('api_key') if isinstance(creds.get('api_key'), str) else None

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
        result = {}
        for key in keys:
            if key not in row: continue
            value = row[key]
            if key == 'offer_id' and value is not None:
                if not _string(value, 256) or (api_key and api_key in value):
                    raise ValueError('Unsafe provider identifier')
                result[key] = value
            elif operation == 'ozon_products' and key in ('product_id', 'sku'):
                result[key] = _exact_identifier(value, api_key, list_allowed=key == 'sku')
            else:
                result[key] = clean(value)
        return result

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
    missing_cursor = operation == 'ozon_products' and cursor_key not in parent
    cursor = None if missing_cursor else parent.get(cursor_key, '')
    if not missing_cursor and not _string(cursor, 512): raise ValueError('Invalid provider cursor')
    # Never turn a redacted cursor into a false end-of-pagination signal.
    if cursor is not None and api_key and api_key in cursor: raise ValueError('Unsafe provider cursor')
    result = {'items': out, cursor_key: cursor, 'first_page_only': not bool(args.get(cursor_key)),
              'read_only': True, 'untrusted_data': True}
    if operation == 'ozon_products':
        result['pagination_state'] = ('cursor_missing' if missing_cursor else
                                      'empty_cursor' if not cursor else
                                      'cursor_repeated' if cursor == args.get(cursor_key) else 'cursor_present')
        result['catalog_completeness'] = 'not_verified'
        result['identity'] = _page_identity(out)
    return result


def _project_orders_analytics(args, data, creds):
    """Summarize ordered amounts, not delivered sales or payouts."""
    parent = data.get('result')
    if not isinstance(parent, dict) or not isinstance(parent.get('data'), list):
        raise ValueError('Unexpected Ozon analytics schema')
    rows = parent['data']
    start, end = date.fromisoformat(args['date_from']), date.fromisoformat(args['date_to'])
    if len(rows) > (end - start).days + 1 or len(rows) >= 100:
        raise ValueError('Unexpected Ozon analytics row count')

    def number(value):
        if type(value) not in (int, float, Decimal) or (type(value) is float and not math.isfinite(value)):
            raise ValueError('Invalid Ozon analytics metric')
        if len(str(value)) > 80: raise ValueError('Invalid Ozon analytics metric')
        result = Decimal(str(value))
        if not result.is_finite() or len(result.as_tuple().digits) > 80 or abs(result.as_tuple().exponent) > 80 or abs(result.adjusted()) > 80:
            raise ValueError('Invalid Ozon analytics metric')
        return result

    def add_exact(a, b):
        with localcontext() as context:
            # Supports 80-digit inputs, their exponents and up to 90 rows.
            context.prec = 256
            return a + b

    items, seen = [], set()
    amount = Decimal(0)
    units = Decimal(0)
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('dimensions'), list) or len(row['dimensions']) != 1:
            raise ValueError('Unexpected Ozon analytics dimension')
        dimension = row['dimensions'][0]
        metrics = row.get('metrics')
        if not isinstance(dimension, dict) or not isinstance(dimension.get('id'), str):
            raise ValueError('Invalid Ozon analytics date')
        day = dimension['id']
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day): raise ValueError('Invalid date')
            parsed = date.fromisoformat(day)
        except ValueError: raise ValueError('Invalid Ozon analytics date') from None
        if not start <= parsed <= end or day in seen: raise ValueError('Unexpected Ozon analytics date')
        seen.add(day)
        if not isinstance(metrics, list) or len(metrics) != 2: raise ValueError('Invalid Ozon analytics metrics')
        revenue, ordered = map(number, metrics)
        if ordered != ordered.to_integral_value(): raise ValueError('Invalid order count')
        amount = add_exact(amount, revenue)
        units = add_exact(units, ordered)
        items.append({'date': day, 'ordered_amount': str(revenue), 'ordered_units': str(ordered)})
    timestamp = data.get('timestamp')
    api_key = creds.get('api_key')
    if timestamp is not None and (not _string(timestamp, 64) or (isinstance(api_key, str) and api_key and api_key in timestamp)):
        raise ValueError('Invalid Ozon report timestamp')
    missing_dates = [(start + timedelta(days=offset)).isoformat()
                     for offset in range((end - start).days + 1)
                     if (start + timedelta(days=offset)).isoformat() not in seen]
    return {'source': 'Ozon Seller API /v1/analytics/data', 'date_from': args['date_from'],
            'date_to': args['date_to'], 'dates_inclusive': True, 'report_timestamp': timestamp,
            'ordered_amount': str(amount) if items else None, 'ordered_units': str(units) if items else None,
            'currency': None, 'days': items, 'missing_dates': missing_dates,
            'complete': False, 'period_completeness': 'not_verified', 'read_only': True, 'untrusted_data': True,
            'meaning': 'Observed order amount and units only, not delivered sales, payout or profit. Missing days and an empty response are not verified zeros; period completeness is not verified. Currency is not supplied by this API response; verify it before combining sums. Ozon analytics may update later.'}
