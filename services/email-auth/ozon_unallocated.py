"""Read one page of the five requested accrual types without inferring a SKU or supply."""
import json
import urllib.request
from datetime import date

import ozon_accrual_day
from ozon_details import _items, _money, _optional_sku, _text

OPERATION = 'ozon_unallocated_accruals'
TYPE_IDS = (12, 133, 134, 77, 46)


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'date', 'last_id', 'include_types'}:
        raise ValueError('Invalid accrual arguments')
    day = args.get('date')
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day < '2022-01-01':
        raise ValueError('Invalid accrual date')
    _text(args.get('last_id', ''), 512, empty=True)
    if type(args.get('include_types', True)) is not bool:
        raise ValueError('Invalid dictionary option')


def build_request(args, creds):
    validate_args(args)
    cursor = _text(args.get('last_id', ''), 512, empty=True, secret=creds['api_key'])
    return urllib.request.Request(ozon_accrual_day.ENDPOINT, method='POST',
        data=json.dumps({'date': args['date'], 'last_id': cursor}).encode(),
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'], 'Content-Type': 'application/json'})


def _typed_fees(row, category, secret, issues):
    """The category selects the documented payload. Row and delivery totals are not typed fees."""
    def children(node, key):
        value = node.get(key) if isinstance(node, dict) else None
        if value is None:
            issues.add('missing_' + key)
            return []
        return _items(value, 5000)

    if category == 'NON_ITEM':
        yield 'non_item_fee', None, row.get('non_item_fee')
    elif category == 'CONTAINER_FEES':
        for index, fee in enumerate(children(row.get('container_fees'), 'fees')):
            yield f'container_fees.fees[{index}]', None, fee
    elif category in ('ITEM', 'POSTING'):
        posting = category == 'POSTING'
        group, key = ('posting', 'products') if posting else ('item_fees', 'fees')
        for index, product in enumerate(children(row.get(group), key)):
            if not isinstance(product, dict):
                raise ValueError('Invalid accrual product')
            sku = _optional_sku(product.get('sku'), secret)
            if posting and product.get('delivery') is None:
                issues.add('missing_delivery')
                continue  # Commission fields have no documented type_id.
            container = product.get('delivery') if posting else product
            leaf = 'services' if posting else 'fees'
            path = f'{group}.{key}[{index}]' + ('.delivery' if posting else '')
            for pos, fee in enumerate(children(container, leaf)):
                yield f'{path}.{leaf}[{pos}]', sku, fee
    else:
        issues.add('unidentified_accrual_category')


def project_response(args, data, creds):
    validate_args(args)
    secret = creds['api_key']
    cursor = data.get('last_id')
    page_issues = set()
    if cursor is None:
        page_issues.add('missing_cursor')
    else:
        cursor = _text(cursor, 512, empty=True, secret=secret)
        if cursor and cursor == args.get('last_id'):
            page_issues.add('repeated_cursor')
            cursor = None
    operations = _items(data['accruals'], 5000) if data.get('accruals') is not None else []
    if data.get('accruals') is None:
        page_issues.add('missing_accruals')
    observed_ids, rows, direct_rows, gaps = set(), [], [], []
    for index, row in enumerate(operations):
        if not isinstance(row, dict):
            raise ValueError('Invalid accrual')
        issues = set()
        accrual_id = _optional_sku(row.get('accrual_id'), secret)
        if accrual_id is None:
            issues.add('missing_accrual_id')
        elif accrual_id in observed_ids:
            issues.add('repeated_accrual_id')
        else:
            observed_ids.add(accrual_id)
        if row.get('date') not in (None, args['date']):
            raise ValueError('Accrual date mismatch')
        if row.get('date') is None:
            issues.add('missing_accrual_date')
        unit = row.get('unit_number')
        if unit is not None:
            unit = _text(unit, 512, empty=True, secret=secret) or None
        if unit is None:
            issues.add('missing_unit_number')
        category = row.get('accrued_category')
        if category is not None:
            category = _text(category, 64, secret=secret)
        for path, sku, fee in _typed_fees(row, category, secret, issues):
            if not isinstance(fee, dict) or type(fee.get('type_id')) is not int:
                issues.add('missing_or_invalid_type_id')
                continue
            kind = fee['type_id']
            if kind not in TYPE_IDS:
                continue
            money = _money(fee.get('accrued'), secret) or {}
            if money.get('amount') is None or not money.get('currency'):
                issues.add('missing_amount_or_currency')
            (direct_rows if sku is not None else rows).append({
                'accrual_id': accrual_id, 'date': row.get('date'), 'type_id': kind,
                'amount': money.get('amount'), 'currency': money.get('currency'), 'category': category,
                'unit_number': unit, 'posting_number': None, 'posting_number_candidate': unit if category == 'POSTING' else None,
                'supply_id': None, 'sku': sku, 'row_index': index, 'fee_path': path})
        if issues:
            gaps.append({'row_index': index, 'accrual_id': accrual_id, 'issues': sorted(issues)})
    return {'source': 'Ozon Seller API /v1/finance/accrual/by-day', 'period_basis': 'accrual_date',
            'date': args['date'], 'type_ids': list(TYPE_IDS), 'requested_cursor': args.get('last_id', ''),
            'next_cursor': cursor, 'page_accrual_ids': sorted(observed_ids, key=int),
            'page_issues': sorted(page_issues), 'row_issues': gaps, 'unallocated_fees': rows,
            'fees_with_explicit_sku': direct_rows, 'type_dictionary_status': 'not_requested',
            'type_dictionary': None, 'period_completeness': 'not_verified', 'supply_link': 'not_provided_by_endpoint',
            'posting_link': 'not_independently_verified', 'read_only': True, 'untrusted_data': True,
            'note': 'Одна страница. Для продолжения используйте ту же дату и новый курсор в течение 15 минут. '
                    'Проверяйте повторы курсоров и accrual_id между страницами. unit_number обозначает заказ '
                    'или услугу. Источник не указывает supply_id. Строки с явным SKU показаны отдельно. '
                    'Общие суммы операций и доставки исключены. Связь с артикулом и полный итог неизвестны.'}


def attach_types(result, dictionary):
    types = {item['id']: item for item in dictionary['types']}
    result['type_dictionary'] = [{'type_id': kind, 'name': types.get(kind, {}).get('name'),
                                  'description': types.get(kind, {}).get('description')} for kind in TYPE_IDS]
    result['type_dictionary_status'] = ('complete' if all(row['name'] for row in result['type_dictionary'])
                                        else 'incomplete')
    result['type_dictionary_source'] = dictionary['source']
    return result
