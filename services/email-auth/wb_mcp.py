"""Fixed, read-only Wildberries operations for the marketplace bridge.

Responses are bounded and projected onto explicit field allowlists. Call from the
portal's deadline-bounded worker, not directly from a request thread.
"""
import json
import math
import re
import ssl
import urllib.request
from datetime import date, datetime
from urllib.parse import urlencode

OPERATIONS = frozenset(('wb_products', 'wb_prices', 'wb_commissions', 'wb_box_tariffs', 'wb_sales_summary'))
MAX_ROW_BODY = 512000
MAX_TABLE_BODY = 4000000

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _cursor_time(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('Invalid cursor timestamp')
    try:
        if datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is None:
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError('Invalid cursor timestamp') from None
    return value

def validate_args(operation, args):
    """Validate API args independently of MCP schemas and return a shallow copy."""
    if operation not in OPERATIONS or not isinstance(args, dict):
        raise ValueError('Invalid operation or arguments')
    allowed = {
        'wb_products': {'limit', 'updated_at', 'nm_id'},
        # nm_id is also used by the existing exact-article browser lookup.
        'wb_prices': {'limit', 'offset', 'nm_id'},
        'wb_commissions': {'limit', 'subject_id', 'search'},
        'wb_box_tariffs': {'limit', 'date', 'warehouse'},
        'wb_sales_summary': {'limit', 'offset', 'date_from', 'date_to'},
    }[operation]
    if set(args) - allowed:
        raise ValueError('Invalid arguments')
    limit = args.get('limit', 100 if operation == 'wb_sales_summary' else 20)
    if type(limit) is not int or not 1 <= limit <= (200 if operation in ('wb_commissions','wb_box_tariffs') else 100):
        raise ValueError('Invalid limit')
    if operation == 'wb_products':
        if ('updated_at' in args) != ('nm_id' in args):
            raise ValueError('Both cursor values required')
        if 'updated_at' in args:
            _cursor_time(args['updated_at'])
            if type(args['nm_id']) is not int or not 1 <= args['nm_id'] <= 9007199254740991:
                raise ValueError('Invalid cursor ID')
    if operation == 'wb_prices':
        offset = args.get('offset', 0)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('Invalid offset')
        if 'nm_id' in args and (type(args['nm_id']) is not int or not 1 <= args['nm_id'] <= 9223372036854775807 or limit != 1 or offset != 0):
            raise ValueError('Invalid WB article')
    if operation == 'wb_commissions':
        if 'subject_id' in args and (type(args['subject_id']) is not int or not 1 <= args['subject_id'] <= 9007199254740991):
            raise ValueError('Invalid subject')
        if 'search' in args and (not isinstance(args['search'], str) or len(args['search']) > 100):
            raise ValueError('Invalid search')
    if operation == 'wb_box_tariffs':
        value = args.get('date')
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Invalid date')
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError('Invalid date') from None
        if 'warehouse' in args and (not isinstance(args['warehouse'], str) or len(args['warehouse']) > 100):
            raise ValueError('Invalid warehouse')
    if operation == 'wb_sales_summary':
        dates = []
        for field in ('date_from', 'date_to'):
            value = args.get(field)
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Invalid date')
            try: dates.append(date.fromisoformat(value))
            except ValueError: raise ValueError('Invalid date') from None
        if dates[1] < dates[0] or (dates[1] - dates[0]).days > 364:
            raise ValueError('Invalid period')
        offset = args.get('offset', 0)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('Invalid offset')
    return dict(args)

def _decode(raw):
    def reject(_): raise ValueError('Invalid provider JSON number')
    def finite(raw):
        value = float(raw)
        if not math.isfinite(value): raise ValueError('Invalid provider JSON number')
        return value
    data = json.loads(raw, parse_constant=reject, parse_float=finite)
    if not isinstance(data, dict): raise ValueError('Invalid provider response')
    if ('error' in data and data['error'] is not False) or data.get('errorText') not in (None, ''):
        raise ValueError('Provider reported an error')
    return data

def read(operation, args, creds):
    """Execute one marketplace request and return bounded, read-only data."""
    args = validate_args(operation, args)
    token = creds['api_token']
    limit = args.get('limit', 100 if operation == 'wb_sales_summary' else 20)
    headers = {'Authorization': token, 'Accept': 'application/json'}
    if operation == 'wb_products':
        cursor = {'limit': limit}
        if 'updated_at' in args: cursor.update(updatedAt=args['updated_at'], nmID=args['nm_id'])
        body = json.dumps({'settings': {'cursor': cursor, 'filter': {'withPhoto': -1}}}).encode()
        headers['Content-Type'] = 'application/json'
        req = urllib.request.Request('https://content-api.wildberries.ru/content/v2/get/cards/list', data=body, headers=headers, method='POST')
    elif operation == 'wb_prices':
        query = {'limit': limit, 'offset': args.get('offset', 0)}
        if 'nm_id' in args: query['filterNmID'] = args['nm_id']
        req = urllib.request.Request('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?' + urlencode(query), headers=headers, method='GET')
    elif operation == 'wb_commissions':
        req = urllib.request.Request('https://common-api.wildberries.ru/api/v1/tariffs/commission', headers=headers, method='GET')
    elif operation == 'wb_sales_summary':
        body = json.dumps({'selectedPeriod': {'start': args['date_from'], 'end': args['date_to']},
                           'skipDeletedNm': False, 'limit': limit, 'offset': args.get('offset', 0)}).encode()
        headers['Content-Type'] = 'application/json'
        req = urllib.request.Request('https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products', data=body, headers=headers, method='POST')
    else:
        req = urllib.request.Request('https://common-api.wildberries.ru/api/v1/tariffs/box?' + urlencode({'date': args['date']}), headers=headers, method='GET')
    # The sales-funnel API includes many nested, unused statistics per product;
    # bound that raw envelope separately from our much smaller projected output.
    cap = MAX_TABLE_BODY if operation in ('wb_commissions','wb_box_tariffs','wb_sales_summary') else MAX_ROW_BODY
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=15) as response:
        raw = response.read(cap + 1)
    if len(raw) > cap: raise ValueError('Response too large')
    data = _decode(raw)
    def clean(value, max_length=256):
        if isinstance(value, str): return value.replace(token, '[REDACTED]')[:max_length] if token else value[:max_length]
        # JavaScript cannot safely represent all int64 values. Preserve larger
        # integers as decimal text rather than silently rounding identifiers.
        if type(value) is int and abs(value) > 9007199254740991: return str(value)
        return value if type(value) in (int, float, bool) or value is None else None
    def project(row, fields):
        if not isinstance(row, dict): raise ValueError('Unexpected row')
        projected = {}
        for key in fields:
            if key not in row: continue
            value = row[key]
            if key == 'vendorCode' and value is not None:
                if not isinstance(value, str) or len(value) > 256 or (token and token in value):
                    raise ValueError('Unsafe provider identifier')
                projected[key] = value
            else:
                projected[key] = clean(value)
        return projected
    result = {'read_only': True, 'untrusted_data': True}
    if operation == 'wb_products':
        rows = data.get('cards')
        if not isinstance(rows, list) or len(rows) > limit: raise ValueError('Unexpected cards')
        items=[]
        for row in rows:
            item=project(row, ('nmID','imtID','nmUUID','subjectID','subjectName','vendorCode','brand','title','createdAt','updatedAt'))
            if 'description' in row: item['description']=clean(row['description'],4096)
            if row.get('dimensions') is not None:
                item['dimensions']=project(row['dimensions'], ('length','width','height','weightBrutto','isValid'))
            if row.get('sizes') is not None:
                if not isinstance(row['sizes'],list): raise ValueError('Unexpected sizes')
                item['sizes']=[]
                for size in row['sizes'][:100]:
                    size_item=project(size, ('chrtID','techSize','wbSize'))
                    if 'skus' in size:
                        if not isinstance(size['skus'],list): raise ValueError('Unexpected barcodes')
                        size_item['skus']=[clean(value) for value in size['skus'][:100]]
                    item['sizes'].append(size_item)
            if row.get('characteristics') is not None:
                if not isinstance(row['characteristics'],list): raise ValueError('Unexpected characteristics')
                item['characteristics']=[]
                for characteristic in row['characteristics'][:100]:
                    entry=project(characteristic, ('id','name'))
                    if 'value' in characteristic:
                        value=characteristic['value']
                        entry['value']=[clean(x) for x in value[:100]] if isinstance(value,list) else clean(value)
                    item['characteristics'].append(entry)
            if row.get('photos') is not None:
                if not isinstance(row['photos'],list): raise ValueError('Unexpected photos')
                item['photos']=[project(photo, ('big','c516x688','c246x328','square','tm')) for photo in row['photos'][:20]]
            items.append(item)
        result['items'] = items
        cursor = data.get('cursor')
        if cursor is None:
            result['cursor'] = None
        elif isinstance(cursor, dict) and type(cursor.get('total')) is int and 0 <= cursor['total'] <= limit:
            if 'updatedAt' in cursor and 'nmID' in cursor and type(cursor['nmID']) is int and 1 <= cursor['nmID'] <= 9007199254740991:
                result['cursor'] = {'updatedAt': _cursor_time(cursor['updatedAt']), 'nmID': cursor['nmID'], 'total': cursor['total']}
            elif cursor['total'] == 0:
                result['cursor'] = {'total': 0}
            else: raise ValueError('Unexpected cursor')
        else: raise ValueError('Unexpected cursor')
        result['first_page_only'] = 'updated_at' not in args
    elif operation == 'wb_prices':
        nested = data.get('data')
        rows = nested.get('listGoods') if isinstance(nested, dict) else None
        if not isinstance(rows, list): raise ValueError('Unexpected schema')
        items=[]
        for row in rows[:limit]:
            item = project(row, ('nmID','vendorCode','discount','clubDiscount','currencyIsoCode4217'))
            sizes=row.get('sizes', [])
            if not isinstance(sizes, list): raise ValueError('Unexpected sizes')
            item['sizes'] = [project(size, ('sizeID','techSizeName','price','discountedPrice','clubDiscountedPrice')) for size in sizes[:100] if isinstance(size, dict)]
            items.append(item)
        result['items']=items
        offset=args.get('offset',0)
        result['offset']=offset
        result['next_offset']=(offset+len(items) if len(items)==limit and offset+len(items)<=1000000 and 'nm_id' not in args else None)
        result['first_page_only']=(offset==0)
    elif operation == 'wb_commissions':
        rows=data.get('report')
        if not isinstance(rows,list): raise ValueError('Unexpected commission schema')
        # Fields are projected before filtering; retain no untrusted extra keys.
        fields=('subjectID','subjectName','parentID','parentName','kgvpBooking','kgvpMarketplace','kgvpPickup','kgvpSupplier','kgvpSupplierExpress','paidStorageKgvp')
        items=[project(row, fields) for row in rows]
        if 'subject_id' in args: items=[row for row in items if row.get('subjectID')==args['subject_id']]
        if args.get('search'): items=[row for row in items if args['search'].casefold() in str(row.get('subjectName','')).casefold()]
        result.update(items=items[:limit], matched_rows=len(items), returned_rows=min(len(items),limit), truncated=len(items)>limit)
    elif operation == 'wb_sales_summary':
        nested = data.get('data')
        rows = nested.get('products') if isinstance(nested, dict) else None
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError('Unexpected sales schema')
        currency = nested.get('currency')
        if not isinstance(currency, str) or not re.fullmatch(r'[A-Za-z]{3}', currency):
            raise ValueError('Missing sales currency')
        # Reject instead of exposing credentials or silently inventing a
        # currency when normalization would reproduce part of a token.
        if token and token.casefold() in currency.casefold():
            raise ValueError('Unsafe sales currency')
        items = []
        metric_names = ('orderCount', 'orderSum', 'buyoutCount', 'buyoutSum', 'cancelCount', 'cancelSum')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('product'), dict):
                raise ValueError('Unexpected sales product')
            statistic = row.get('statistic')
            selected = statistic.get('selected') if isinstance(statistic, dict) else None
            if not isinstance(selected, dict) or any(type(selected.get(k)) is not int or selected[k] < 0 for k in metric_names):
                raise ValueError('Unexpected sales metrics')
            period = selected.get('period')
            if not isinstance(period, dict) or period.get('start') != args['date_from'] or period.get('end') != args['date_to']:
                raise ValueError('Unexpected sales period')
            items.append({'product': project(row['product'], ('nmId', 'title', 'vendorCode', 'brandName', 'subjectId', 'subjectName')),
                          'metrics': project(selected, metric_names)})
        offset = args.get('offset', 0)
        has_more = len(items) == limit
        next_offset = offset + len(items) if has_more and offset + len(items) <= 1000000 else None
        page_totals = {key: clean(sum(row['statistic']['selected'][key] for row in rows)) for key in metric_names}
        result.update(items=items, currency=currency.upper(), date_from=args['date_from'], date_to=args['date_to'],
                      offset=offset, next_offset=next_offset, has_more=has_more, page_totals=page_totals,
                      complete=(offset == 0 and not has_more),
                      note='Данные WB по датам заказа; выкупы, отмены и возвраты относятся к дате заказа. '
                           'Отчёт предварительный, обновляется с задержкой; это не выплаты и не прибыль. '
                           'page_totals относится только к текущей странице. Для итога сложите суммы всех страниц, '
                           'не представляйте неполную выборку как итог. '
                           'WB может обновить данные между страницами; при has_more и отсутствии next_offset выборка неполная.')
    else:
        envelope=data.get('response')
        table=envelope.get('data') if isinstance(envelope, dict) else None
        rows=table.get('warehouseList') if isinstance(table, dict) else None
        if not isinstance(rows,list): raise ValueError('Unexpected tariff schema')
        fields=('warehouseName','geoName','boxDeliveryBase','boxDeliveryCoefExpr','boxDeliveryLiter','boxDeliveryMarketplaceBase','boxDeliveryMarketplaceCoefExpr','boxDeliveryMarketplaceLiter','boxStorageBase','boxStorageCoefExpr','boxStorageLiter')
        items=[project(row, fields) for row in rows]
        if args.get('warehouse'): items=[row for row in items if args['warehouse'].casefold() in str(row.get('warehouseName','')).casefold()]
        result.update(project(table, ('currency','dtNextBox','dtTillMax')))
        result.update(items=items[:limit], matched_rows=len(items), returned_rows=min(len(items),limit), truncated=len(items)>limit)
    # The bridge and its caller enforce their own response budgets. Never let
    # a valid but very large page exceed that bound after JSON serialization.
    serialized = json.dumps(result, ensure_ascii=True, allow_nan=False)
    # Numeric provider values and cursors can also reproduce a credential,
    # even after the string-field projection above. Fail closed instead of
    # returning a misleading redacted amount or a broken pagination cursor.
    compact = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    escaped_token = json.dumps(token, ensure_ascii=True)[1:-1] if token else ''
    compact_token = json.dumps(token, ensure_ascii=False)[1:-1] if token else ''
    if escaped_token and any(value in representation
                             for value in (token, escaped_token, compact_token)
                             for representation in (serialized, compact)):
        raise ValueError('Unsafe provider response')
    if len(serialized.encode()) > MAX_ROW_BODY:
        raise ValueError('Projected response too large; request a smaller page')
    return result
