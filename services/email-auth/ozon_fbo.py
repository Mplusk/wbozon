"""Read-only FBO supply orders, individual supplies and their declared, paged composition."""
import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from ozon_details import BASE, _id, _items, _optional_sku, _text

LIST = 'ozon_fbo_supply_orders'
GET = 'ozon_fbo_supply_details'
BUNDLE = 'ozon_fbo_supply_items'
ENDPOINTS = {LIST: '/v3/supply-order/list', GET: '/v3/supply-order/get', BUNDLE: '/v1/supply-order/bundle'}
STATES = ('DATA_FILLING', 'READY_TO_SUPPLY', 'ACCEPTED_AT_SUPPLY_WAREHOUSE', 'IN_TRANSIT',
          'ACCEPTANCE_AT_STORAGE_WAREHOUSE', 'REPORTS_CONFIRMATION_AWAITING', 'REPORT_REJECTED',
          'COMPLETED', 'REJECTED_AT_SUPPLY_WAREHOUSE', 'CANCELLED', 'OVERDUE')


def validate_args(operation, args):
    fields = {LIST: {'states', 'order_number', 'last_id', 'limit'}, GET: {'order_ids'},
              BUNDLE: {'bundle_id', 'last_id', 'limit'}}
    if operation not in fields or not isinstance(args, dict) or set(args) - fields[operation]:
        raise ValueError('Invalid supply arguments')
    if operation == GET:
        ids = [_id(value) for value in _items(args.get('order_ids'), 50)]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('Invalid supply order IDs')
        return
    _text(args.get('last_id', ''), 512, empty=True)
    limit = args.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid supply limit')
    if operation == BUNDLE:
        _text(args.get('bundle_id'), 512)
    elif 'states' in args:
        states = _items(args['states'], len(STATES))
        if not states or any(s not in STATES for s in states) or len(set(states)) != len(states):
            raise ValueError('Invalid supply states')
    if 'order_number' in args:
        _text(args['order_number'], 128)


def build_request(operation, args, creds):
    validate_args(operation, args)
    if operation == GET:
        body = {'order_ids': [_id(value) for value in args['order_ids']]}
    else:
        body = {'limit': args.get('limit', 50), 'last_id': args.get('last_id', '')}
        if operation == LIST:
            filters = {'states': list(args.get('states', STATES))}
            if 'order_number' in args:
                filters['order_number_search'] = args['order_number']
            body.update(filter=filters, sort_by='ORDER_CREATION', sort_dir='DESC')
        else:
            body.update(bundle_ids=[args['bundle_id']], sort_field='SKU', is_asc=True)
    return urllib.request.Request(BASE + ENDPOINTS[operation], method='POST', data=json.dumps(body).encode(),
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'], 'Content-Type': 'application/json'})


def _string(value, limit=512):
    if value is None:
        return None
    return _text(value, limit, empty=True) or None


def _count(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise ValueError('Invalid supply quantity')
    return value


def _litres(value):
    if value is None:
        return None
    if type(value) not in (Decimal, int) or not Decimal(value).is_finite() or value < 0:
        raise ValueError('Invalid supply volume')
    return format(Decimal(value), 'f')


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Invalid supply object')
    return value


def _id_or_none(value):
    return _optional_sku(value, None)


def _page_rows(data, key, limit, issues):
    if data.get(key) is None:
        issues.add('missing_' + key)
        return []
    return _items(data[key], limit)


def _supply(row):
    row = _object(row)
    warehouse = _object(row.get('storage_warehouse'))
    crossdock = row.get('is_crossdock')
    if crossdock is not None and type(crossdock) is not bool:
        raise ValueError('Invalid crossdock flag')
    content = _object(row.get('content'))
    if row.get('bundle_id') and content.get('bundle_id') and row['bundle_id'] != content['bundle_id']:
        raise ValueError('Contradictory supply composition identifiers')
    return {'supply_id': _id_or_none(row.get('supply_id')), 'bundle_id': _string(row.get('bundle_id') or content.get('bundle_id')),
            'state': _string(row.get('state') or row.get('supply_state')), 'is_crossdock': crossdock,
            'macrolocal_cluster_id': _id_or_none(row.get('macrolocal_cluster_id')),
            'storage_warehouse_id': _id_or_none(warehouse.get('warehouse_id')),
            'storage_warehouse_name': _string(warehouse.get('name')), 'arrival_date': _string(warehouse.get('arrival_date'))}


def _orders(args, data, issues):
    requested = {_id(value) for value in args['order_ids']}
    seen, supplies_seen, orders = set(), set(), []
    for raw in _page_rows(data, 'orders', 50, issues):
        row = _object(raw)
        order_id = _id_or_none(row.get('order_id'))
        if order_id not in requested or order_id in seen:
            raise ValueError('Unexpected or repeated supply order')
        seen.add(order_id)
        tags = _object(row.get('order_tags'))
        dropoff = _object(row.get('dropoff_warehouse'))
        slot = _object(row.get('timeslot'))
        if 'value' in slot:
            slot = _object(slot.get('value'))
        window, tz = _object(slot.get('timeslot')), _object(slot.get('timezone_info'))
        supplies = None
        if row.get('supplies') is not None:
            supplies = [_supply(s) for s in _items(row['supplies'], 5000)]
            for supply in supplies:
                sid = supply['supply_id']
                if sid is None or not supply['bundle_id']:
                    issues.add('missing_supply_or_bundle_id')
                if sid is not None and sid in supplies_seen:
                    issues.add('repeated_supply_id')
                supplies_seen.add(sid)
        else:
            issues.add('missing_supplies')
        orders.append({'order_id': order_id, 'order_number': _string(row.get('order_number')),
                       'state': _string(row.get('state')), 'created_date': _string(row.get('created_date')),
                       'state_updated_date': _string(row.get('state_updated_date')),
                       'data_filling_deadline_utc': _string(row.get('data_filling_deadline_utc')),
                       'original_supply_id': _id_or_none(tags.get('original_supply_id')),
                       'seller_warehouse_id': _id_or_none(tags.get('seller_warehouse_id')),
                       'dropoff_warehouse_id': _id_or_none(dropoff.get('warehouse_id') or row.get('dropoff_warehouse_id')),
                       'timeslot_from': _string(window.get('from')), 'timeslot_to': _string(window.get('to')),
                       'timezone': _string(tz.get('iana_name')), 'timezone_offset': _string(tz.get('offset')),
                       'supplies': supplies})
    missing = sorted(requested - seen, key=int)
    if missing:
        issues.add('requested_order_missing')
    return {'orders': orders, 'missing_order_ids': missing}


def _item(row):
    row = _object(row)
    return {'sku': _id_or_none(row.get('sku')), 'product_id': _id_or_none(row.get('product_id')),
            'offer_id': _string(row.get('offer_id'), 256), 'name': _string(row.get('name'), 2048),
            'contractor_item_code': _string(row.get('contractor_item_code'), 256), 'barcode': _string(row.get('barcode')),
            'quantity': _count(row.get('quantity')), 'quant': _count(row.get('quant')),
            'volume_in_litres': _litres(row.get('volume_in_litres')),
            'total_volume_in_litres': _litres(row.get('total_volume_in_litres'))}


def project_response(operation, args, data):
    validate_args(operation, args)
    issues = set()
    if operation == GET:
        result = _orders(args, data, issues)
    else:
        cursor = _string(data.get('last_id'))
        if operation == LIST:
            ids = [_id(value) for value in _page_rows(data, 'order_ids', args.get('limit', 50), issues)]
            if len(ids) != len(set(ids)):
                issues.add('repeated_order_id')
            if data.get('last_id') is None:
                issues.add('missing_cursor')
            result = {'order_ids': ids, 'coverage': 'one_page', 'states': list(args.get('states', STATES))}
        else:
            more = data.get('has_next')
            if more is not None and type(more) is not bool:
                raise ValueError('Invalid supply pagination flag')
            if more is None:
                issues.add('missing_has_next')
            if more and not cursor:
                issues.add('missing_next_cursor')
            items = [_item(row) for row in _page_rows(data, 'items', args.get('limit', 50), issues)]
            if more and not items:
                issues.add('empty_nonterminal_page')
            skus = [row['sku'] for row in items if row['sku'] is not None]
            if len(skus) != len(set(skus)):
                issues.add('repeated_sku')
            if any(not row['sku'] or not row['offer_id'] for row in items):
                issues.add('incomplete_item_identity')
            if any(not row['name'] for row in items):
                issues.add('missing_item_name')
            if any(row['quantity'] is None for row in items):
                issues.add('missing_item_quantity')
            result = {'bundle_id': args['bundle_id'], 'items': items, 'has_next': more,
                      'total_count': _count(data.get('total_count')), 'quantity_basis': 'declared_bundle',
                      'volume_unit': 'litre', 'coverage': 'one_page'}
            if more is False:
                cursor = None
        if cursor and cursor == args.get('last_id'):
            issues.add('repeated_cursor')
            cursor = None
        result.update(requested_cursor=args.get('last_id', ''), next_cursor=cursor)
    result.update(source='Ozon Seller API ' + ENDPOINTS[operation], issues=sorted(issues), read_only=True,
                  observed_at=datetime.now(timezone.utc).isoformat(), untrusted_data=True)
    return result
