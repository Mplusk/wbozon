"""Bounded, read-only projections of Ozon product identity and accruals.

Only endpoints and fields documented by Ozon Seller API are used. No finance
category or buyout meaning is inferred from a type ID or a shipment status.
"""
import json
import re
import urllib.request
from datetime import date
from decimal import Decimal, localcontext


OPERATIONS = frozenset(('ozon_product_info', 'ozon_accrual_types', 'ozon_posting_accruals'))
BASE = 'https://api-seller.ozon.ru'
ENDPOINTS = {
    'ozon_product_info': '/v3/product/info/list',
    'ozon_accrual_types': '/v1/finance/accrual/types',
    'ozon_posting_accruals': '/v1/finance/accrual/postings',
}
IDENTIFIERS = {'offer_ids': 'offer_id', 'product_ids': 'product_id', 'skus': 'sku'}
MAX_INT64 = 9223372036854775807
CATEGORY_BY_OZON_TYPE = {
    ('SaleCommission','Вознаграждение за продажу'):'commission',
    ('Logistic','Логистика'):'logistics', ('Shipment','Магистраль'):'logistics',
    ('LastMile','Последняя миля'):'last_mile',
    ('ClientReturn','Обработка возвратов'):'returns',
    ('BackwardShipment','Обратная магистраль'):'returns',
    ('ReturnFlowLogistic','Обратная логистика'):'returns',
    ('PreparingToReturn','Подготовка товаров к возврату'):'returns',
    ('ReturnStorageInTheWarehouse','Долгосрочное размещение возврата FBS'):'returns',
}


def _text(value, limit, *, empty=False, secret=None, multiline=False):
    if (not isinstance(value, str) or len(value) > limit or
            (not empty and not value) or
            re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]' if multiline else r'[\x00-\x1f\x7f]', value) or
            (secret and secret in value)):
        raise ValueError('Invalid provider text')
    return value


def _id(value, secret=None):
    # Ozon documents int64. JSON strings are accepted so that JS cannot round it.
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,18}', value):
        number = int(value)
    else:
        raise ValueError('Invalid identifier')
    if not 0 < number <= MAX_INT64:
        raise ValueError('Invalid identifier')
    result = str(number)
    if secret and secret in result:
        raise ValueError('Unsafe identifier')
    return result


def _items(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('Invalid provider list')
    return value


def _optional_sku(value, secret):
    if value is None or type(value) is int and value == 0 or value == '0':
        return None
    return _id(value, secret)


def validate_args(operation, args):
    if operation not in OPERATIONS or not isinstance(args, dict):
        raise ValueError('Invalid Ozon operation')
    if operation == 'ozon_accrual_types':
        if args: raise ValueError('Invalid arguments')
        return
    if operation == 'ozon_product_info':
        if len(args) != 1 or next(iter(args), None) not in IDENTIFIERS:
            raise ValueError('Choose one identifier type')
        field, values = next(iter(args.items()))
        _items(values, 100)
        if not values: raise ValueError('No identifiers')
        normalized = [(_text(value, 256) if field == 'offer_ids' else _id(value)) for value in values]
    else:
        if set(args) - {'posting_numbers', 'date_from', 'date_to', 'view', 'skus'} or 'posting_numbers' not in args:
            raise ValueError('Invalid arguments')
        values = _items(args['posting_numbers'], 200)
        if not values: raise ValueError('No posting numbers')
        normalized = [_text(value, 128) for value in values]
        if ('date_from' in args) != ('date_to' in args):
            raise ValueError('Both period dates are required')
        if 'date_from' in args:
            days = []
            for name in ('date_from', 'date_to'):
                value = args[name]
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('Invalid date')
                try: days.append(date.fromisoformat(value))
                except ValueError: raise ValueError('Invalid date') from None
            if days[0] > days[1]: raise ValueError('Invalid period')
        if args.get('view', 'details') not in ('details', 'summary'):
            raise ValueError('Invalid view')
        if 'skus' in args:
            if args.get('view') != 'summary': raise ValueError('SKU filter requires summary view')
            sku_values = _items(args['skus'], 100)
            if not sku_values: raise ValueError('No SKUs')
            sku_values = [_id(value) for value in sku_values]
            if len(set(sku_values)) != len(sku_values): raise ValueError('Duplicate SKUs')
    if len(set(normalized)) != len(normalized):
        raise ValueError('Duplicate identifiers')


def build_request(operation, args, creds):
    validate_args(operation, args)
    if operation == 'ozon_product_info':
        field, values = next(iter(args.items()))
        body = {IDENTIFIERS[field]: values if field == 'offer_ids' else [_id(value) for value in values]}
    elif operation == 'ozon_posting_accruals':
        body = {'posting_numbers': args['posting_numbers']}
    else:
        body = {}
    return urllib.request.Request(BASE + ENDPOINTS[operation],
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'],
                 'Content-Type': 'application/json'}, method='POST')


def project_response(operation, args, data, creds):
    validate_args(operation, args)
    if not isinstance(data, dict): raise ValueError('Invalid provider response')
    secret = creds.get('api_key') if isinstance(creds.get('api_key'), str) else None
    if operation == 'ozon_product_info':
        result = _products(args, data, secret)
    elif operation == 'ozon_accrual_types':
        result = _types(data, secret)
    else:
        result = _postings(args, data, secret)
    result.update(source='Ozon Seller API ' + ENDPOINTS[operation],
                  read_only=True, untrusted_data=True)
    return result


def _products(args, data, secret):
    field, requested = next(iter(args.items()))
    request_set = set(requested if field == 'offer_ids' else [_id(v) for v in requested])
    rows = _items(data.get('items'), 100)
    products, matched, unmatched_rows, by_sku, by_product, by_offer = [], set(), [], {}, {}, {}
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid product')
        product = {'product_id': _id(row['id'], secret) if row.get('id') is not None else None,
                   'offer_id': _text(row['offer_id'], 256, secret=secret) if row.get('offer_id') is not None else None,
                   'name': _text(row['name'], 1024, empty=True, secret=secret) if row.get('name') is not None else None,
                   'sku': _optional_sku(row.get('sku'), secret),
                   'sources': [], 'sources_reported': 'sources' in row and row['sources'] is not None,
                   'stock_sources': [], 'stock_sources_reported': False}
        if product['sources_reported']:
            for source in _items(row['sources'], 1000):
                if not isinstance(source, dict): raise ValueError('Invalid product source')
                source_sku = source.get('sku')
                product['sources'].append({
                    'sku': _optional_sku(source_sku, secret),
                    'source': _text(source['source'], 128, empty=True, secret=secret)
                              if source.get('source') is not None else None})
        stocks = row.get('stocks')
        if stocks is not None:
            if not isinstance(stocks, dict): raise ValueError('Invalid product stocks')
            if stocks.get('stocks') is not None:
                product['stock_sources_reported'] = True
                for stock in _items(stocks['stocks'], 1000):
                    if not isinstance(stock, dict): raise ValueError('Invalid product stock')
                    product['stock_sources'].append({
                        'sku': _optional_sku(stock.get('sku'), secret),
                        'source': _text(stock['source'], 128, empty=True, secret=secret)
                                  if stock.get('source') is not None else None})
        candidates = {s['sku'] for s in product['sources'] if s['sku']}
        candidates.update(s['sku'] for s in product['stock_sources'] if s['sku'])
        if product['sku']: candidates.add(product['sku'])
        product['sku_candidates'] = sorted(candidates, key=int)
        incomplete = [key for key in ('product_id', 'offer_id', 'name') if not product[key]]
        if not product['sku_candidates']: incomplete.append('sku')
        if not product['sources_reported'] or any(not x['sku'] or not x['source'] for x in product['sources']):
            incomplete.append('sources')
        if product['stock_sources_reported'] and any(not x['sku'] or not x['source'] for x in product['stock_sources']):
            incomplete.append('stock_sources')
        product['incomplete_fields'] = incomplete
        index = len(products)
        products.append(product)
        found = ({product['offer_id']} & request_set if field == 'offer_ids' else
                 {product['product_id']} & request_set if field == 'product_ids' else
                 candidates & request_set)
        product['matched_identifiers'] = sorted(found)
        matched.update(found)
        if not found: unmatched_rows.append(index)
        if product['product_id']:
            by_product.setdefault(product['product_id'], set()).add(index)
        if product['offer_id']:
            by_offer.setdefault(product['offer_id'], set()).add(index)
        for sku in candidates: by_sku.setdefault(sku, set()).add(index)
    conflicts = []
    for kind, mapping in (('product_id', by_product), ('offer_id', by_offer), ('sku', by_sku)):
        for indices in mapping.values():
            if len(indices) > 1:
                conflicts.append({'identifier_type': kind, 'row_indices': sorted(indices)})
    return {'items': products, 'requested_identifier_type': field,
            'unmatched_identifiers': sorted(request_set - matched), 'unmatched_row_indices': unmatched_rows,
            'conflicts': conflicts,
            'sku_coverage': 'not_verified',
            'note': ('SKU перечислены по основному полю и источникам Ozon; первый автоматически не выбирается. '
                     'Отсутствие других SKU и совпадения вне ответа не подтверждены.')}


def _types(data, secret):
    rows = _items(data.get('accrual_types'), 2000)
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get('id')) is not int or not 0 <= row['id'] <= 2147483647:
            raise ValueError('Invalid accrual type')
        if row['id'] in seen: raise ValueError('Duplicate accrual type')
        seen.add(row['id'])
        name = _text(row['name'], 1024, empty=True, secret=secret) if row.get('name') is not None else None
        description = _text(row['description'], 4096, empty=True, secret=secret, multiline=True) if row.get('description') is not None else None
        out.append({'id': row['id'], 'name': name, 'description': description,
                    'category': CATEGORY_BY_OZON_TYPE.get((name,description), 'other')})
    return {'types': out, 'note': ('Название и описание — из Ozon. Категории назначены только известным '
             'точным парам названия и описания; остальные обозначены other как не классифицированные, '
             'а не как доказанно прочие расходы. Классификация неполная. '
             'Категория возвратов обозначает связанные начисления со знаком Ozon, а не число возвращённых товаров.')}


def _money(value, secret):
    if value is None: return None
    if not isinstance(value, dict): raise ValueError('Invalid money')
    amount, currency = value.get('amount'), value.get('currency')
    if amount is not None:
        amount = _text(amount, 80, secret=secret)
        if not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,49})(?:\.[0-9]{1,20})?', amount):
            raise ValueError('Invalid amount')
    if currency is not None:
        currency = _text(currency, 16, secret=secret)
        if not re.fullmatch(r'[A-Z]{3,16}', currency): raise ValueError('Invalid currency')
    return {'amount': amount, 'currency': currency}


def _postings(args, data, secret):
    requested = set(args['posting_numbers'])
    rows = _items(data.get('posting_accruals'), len(requested))
    seen, out, count = set(), [], 0
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid posting')
        number = _text(row.get('posting_number'), 128, secret=secret)
        if number not in requested or number in seen: raise ValueError('Unexpected posting')
        seen.add(number)
        accruals = _items(row.get('accruals'), 5000)
        count += len(accruals)
        if count > 5000: raise ValueError('Too many accruals')
        projected = []
        for accrual in accruals:
            if not isinstance(accrual, dict): raise ValueError('Invalid accrual')
            day = _text(accrual.get('accrual_date'), 10, secret=secret)
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day): raise ValueError('Invalid accrual date')
            try: date.fromisoformat(day)
            except ValueError: raise ValueError('Invalid accrual date') from None
            type_id = accrual.get('type_id')
            if type(type_id) is not int or not 0 <= type_id <= 2147483647: raise ValueError('Invalid accrual type')
            quantity = accrual.get('quantity')
            if quantity is not None and (type(quantity) is not int or not -2147483648 <= quantity <= 2147483647):
                raise ValueError('Invalid accrual quantity')
            sku = accrual.get('sku')
            projected.append({'accrual_date': day, 'type_id': type_id,
                'accrued': _money(accrual.get('accrued'), secret), 'quantity': quantity,
                'seller_price': _money(accrual.get('seller_price'), secret),
                'sku': _optional_sku(sku, secret)})
        if 'date_from' in args:
            projected = [item for item in projected if args['date_from'] <= item['accrual_date'] <= args['date_to']]
        out.append({'posting_number': number, 'accruals': projected})
    identical = []
    for posting in out:
        signatures = {}
        for index, accrual in enumerate(posting['accruals']):
            signature = json.dumps(accrual, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
            signatures.setdefault(signature, []).append(index)
        identical.extend({'posting_number':posting['posting_number'], 'row_indices':indices,
                          'sku':posting['accruals'][indices[0]]['sku']}
                         for indices in signatures.values() if len(indices) > 1)
    result = {'requested_posting_numbers': sorted(requested), 'returned_posting_numbers': sorted(seen),
            'unreturned_posting_numbers': sorted(requested - seen),
            'period_basis': 'accrual_date', 'date_from': args.get('date_from'), 'date_to': args.get('date_to'),
            'complete_for_seller': False, 'financial_completeness': 'not_verified',
            'row_identity': 'not_verified', 'financial_additivity': 'not_verified',
            'same_posting_equal_projection_groups': identical,
            'row_indices_scope': 'date_filtered_rows_before_sku_filter',
            'note': ('Только запрошенные отправления; пустой или отсутствующий ответ не подтверждает '
                     'отсутствие будущих начислений. Прочие расходы продавца могут не иметь отправления. '
                     'Количество в начислении не является подтверждённым числом выкупов. '
                     'Одинаковая проекция не доказывает ни дубль, ни отдельные операции. '
                     'Сумма строк не подтверждает полноту и аддитивность финансовых данных; '
                     'нельзя складывать результаты с пересекающимися отправлениями.')}
    if args.get('view', 'details') == 'details':
        result['posting_accruals'] = out
    else:
        groups = {}
        selected_skus = {_id(value) for value in args['skus']} if 'skus' in args else None
        unattributed, other_sku = 0, 0
        unattributed_types = {accrual['type_id'] for row in out for accrual in row['accruals']
                              if accrual['sku'] is None}
        for row in out:
            for accrual in row['accruals']:
                if selected_skus is not None and accrual['sku'] not in selected_skus:
                    if accrual['sku'] is None: unattributed += 1
                    else: other_sku += 1
                    continue
                money = accrual['accrued']
                key = (accrual['sku'], accrual['type_id'], money['currency'] if money else None)
                group = groups.setdefault(key, {'sku': key[0], 'type_id': key[1], 'currency': key[2],
                                                  'row_count': 0, 'amount': Decimal(0),
                                                  'known_amounts': 0, 'missing_amounts': 0,
                                                  'unknown_currency_amounts': 0})
                group['row_count'] += 1
                if money is None or money['amount'] is None:
                    group['missing_amounts'] += 1
                elif money['currency'] is None:
                    group['unknown_currency_amounts'] += 1
                else:
                    group['known_amounts'] += 1
                    with localcontext() as context:
                        context.prec = 80
                        group['amount'] += Decimal(money['amount'])
        breakdown = []
        uncertain_pairs = {(sku, type_id) for (sku, type_id, currency) in groups if currency is None}
        for group in groups.values():
            partial = format(group.pop('amount'), 'f')
            unknown_currency = (group['sku'], group['type_id']) in uncertain_pairs
            unknown_sku = group['sku'] is not None and group['type_id'] in unattributed_types
            missing_posting = bool(requested - seen)
            incomplete = group['missing_amounts'] or unknown_currency or unknown_sku or missing_posting
            group['unresolved_currency_for_type_and_sku'] = unknown_currency
            group['unattributed_rows_of_same_type'] = unknown_sku
            group['unreturned_postings'] = missing_posting
            group['observed_row_sum'] = partial if group['known_amounts'] else None
            group['observed_sum_has_missing_or_unattributed_data'] = bool(incomplete)
            # No claim is made that the visible lines are unique or additive.
            group['financial_total'] = None
            breakdown.append(group)
        result['breakdown'] = breakdown
        result['summary_scope'] = 'requested_postings_only'
        if selected_skus is not None:
            result['filtered_skus'] = sorted(selected_skus, key=int)
            result['unattributed_rows_excluded'] = unattributed
            result['other_sku_rows_excluded'] = other_sku
    return result
