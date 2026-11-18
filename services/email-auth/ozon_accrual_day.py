"""One bounded page of Ozon accruals for a date and explicit SKU candidates."""
import json
import re
import urllib.request
from datetime import date
from decimal import Decimal, localcontext

import ozon_details

OPERATION = 'ozon_accrual_day'
ENDPOINT = 'https://api-seller.ozon.ru/v1/finance/accrual/by-day'


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'date', 'last_id', 'skus', 'view', 'posting_number'} or not {'date', 'skus'} <= set(args):
        raise ValueError('Invalid daily accrual arguments')
    day = args['date']
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('Invalid date')
    try: date.fromisoformat(day)
    except ValueError: raise ValueError('Invalid date') from None
    ozon_details._text(args.get('last_id', ''), 512, empty=True)
    if 'posting_number' in args: ozon_details._text(args['posting_number'], 128)
    skus = ozon_details._items(args['skus'], 100)
    if not skus: raise ValueError('No SKUs')
    normalized = [ozon_details._id(sku) for sku in skus]
    if len(set(normalized)) != len(normalized): raise ValueError('Duplicate SKUs')
    if args.get('view', 'summary') not in ('summary','details'): raise ValueError('Invalid view')


def build_request(args, creds):
    validate_args(args)
    body = {'date': args['date'], 'last_id': args.get('last_id', '')}
    return urllib.request.Request(ENDPOINT, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Client-Id': creds['client_id'], 'Api-Key': creds['api_key'],
                 'Content-Type': 'application/json'})


def _quantity(value):
    if value is None: return None
    if type(value) is not int or not -1000000 <= value <= 1000000:
        raise ValueError('Invalid accrual quantity')
    return value


def _fees(values, secret):
    result = []
    for value in ozon_details._items(values, 5000):
        if not isinstance(value, dict) or type(value.get('type_id')) is not int or not 0 <= value['type_id'] <= 2147483647:
            raise ValueError('Invalid accrual service')
        result.append({'type_id': value['type_id'], 'accrued': ozon_details._money(value.get('accrued'), secret)})
    return result


def project_response(args, data, creds):
    validate_args(args)
    if not isinstance(data, dict): raise ValueError('Invalid daily accrual response')
    secret = creds.get('api_key') if isinstance(creds.get('api_key'), str) else None
    cursor = ozon_details._text(data.get('last_id'), 512, empty=True, secret=secret)
    if cursor and cursor == args.get('last_id', ''): raise ValueError('Repeated daily cursor')
    requested = {ozon_details._id(sku) for sku in args['skus']}
    rows = ozon_details._items(data.get('accruals'), 5000)
    ids, selected, other_sku, unallocated = set(), [], 0, 0
    other_postings, without_posting, missing_reference = 0, 0, 0
    posting_filter = args.get('posting_number')
    if posting_filter is not None: ozon_details._text(posting_filter, 128, secret=secret)
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid daily accrual')
        accrual_id = ozon_details._id(row.get('accrual_id'), secret)
        if accrual_id in ids: raise ValueError('Repeated daily accrual ID')
        ids.add(accrual_id)
        if row.get('date') != args['date']: raise ValueError('Accrual date mismatch')
        category = row.get('accrued_category')
        if category == 'POSTING':
            posting = row.get('posting')
            if not isinstance(posting, dict): raise ValueError('Missing posting')
            unit_number = row.get('unit_number')
            if unit_number is not None:
                unit_number = ozon_details._text(unit_number, 512, empty=True, secret=secret) or None
            if unit_number is None:
                missing_reference += 1
                if posting_filter is not None:
                    continue
            elif posting_filter is not None and unit_number != posting_filter:
                other_postings += 1
                continue
            for product in ozon_details._items(posting.get('products'), 1000):
                if not isinstance(product, dict): raise ValueError('Invalid posting product')
                sku = ozon_details._optional_sku(product.get('sku'), secret)
                if sku not in requested:
                    if sku is None: unallocated += 1
                    else: other_sku += 1
                    continue
                delivery = product.get('delivery')
                if delivery is not None and not isinstance(delivery, dict): raise ValueError('Invalid delivery')
                commission = product.get('commission')
                if commission is not None and not isinstance(commission, dict): raise ValueError('Invalid commission')
                amount_fields = ('seller_price','sale_price','sale_commission','commission','sale_amount','coinvestment','bonus')
                selected.append({'accrual_id': accrual_id, 'category': category, 'unit_number': unit_number,
                    'posting_number_candidate': unit_number, 'posting_number': None,
                    'sku': sku, 'quantity': _quantity(product.get('quantity')),
                    'delivery_total_accrued': ozon_details._money(delivery.get('total_accrued'),secret) if delivery else None,
                    'delivery_services': _fees(delivery.get('services'),secret) if delivery and delivery.get('services') is not None else None,
                    'commission': {field: ozon_details._money(commission.get(field),secret) for field in amount_fields} if commission else None})
        elif category == 'ITEM':
            item_fees = row.get('item_fees')
            if not isinstance(item_fees, dict): raise ValueError('Missing item fees')
            if posting_filter is not None:
                without_posting += 1
                continue
            for item in ozon_details._items(item_fees.get('fees'), 1000):
                if not isinstance(item, dict): raise ValueError('Invalid item fee')
                sku = ozon_details._optional_sku(item.get('sku'),secret)
                if sku not in requested:
                    if sku is None: unallocated += 1
                    else: other_sku += 1
                    continue
                selected.append({'accrual_id': accrual_id, 'category': category, 'sku': sku,
                                 'quantity': _quantity(item.get('quantity')),
                                 'fees': _fees(item.get('fees'),secret)})
        elif category in ('NON_ITEM','CONTAINER_FEES','UNSPECIFIED'):
            unallocated += 1
            if posting_filter is not None: without_posting += 1
        else:
            raise ValueError('Unknown accrual category')
    result = {'source': 'Ozon Seller API /v1/finance/accrual/by-day', 'date': args['date'],
            'period_basis': 'accrual_date', 'selected_skus': sorted(requested,key=int),
            'requested_cursor': args.get('last_id',''), 'next_cursor': cursor,
            'provider_cursor_nonempty': bool(cursor), 'page_accrual_ids': sorted(ids,key=int),
            'other_sku_products_excluded': other_sku,
            'unallocated_entries_excluded': unallocated, 'period_completeness': 'not_verified',
            'posting_filter': posting_filter,
            'posting_filter_basis': 'exact_unit_number_in_posting_category',
            'posting_link': 'not_independently_verified',
            'posting_entries_missing_unit_number': missing_reference,
            'other_posting_entries_excluded': other_postings,
            'different_unit_number_entries_excluded': other_postings,
            'non_posting_category_entries_excluded': without_posting,
            'row_identity': 'not_verified',
            'note': ('Одна страница начислений за дату. Получите справочник типов; объединяйте страницы '
                     'только после проверки идентификаторов и курсоров. Пустой курсор и отсутствие строк '
                     'не подтверждают полный период. Итог доставки может включать перечисленные услуги; '
                     'не суммируйте его с ними повторно. Поля комиссии, цены и скидок не доказаны '
                     'как независимые операции; не складывайте их друг с другом или с итогами без сверки. '
                     'Расходы без SKU не распределяются. Поле commission_ratio не возвращается.'),
            'read_only': True, 'untrusted_data': True}
    if posting_filter is not None:
        result['posting_filter_note'] = ('Точное совпадение с unit_number в строках POSTING за дату начисления. '
            'Ozon использует это поле для заказов и услуг. Начисления отправления можно посмотреть '
            'отдельно через /v1/finance/accrual/postings. В этом ответе нет общего accrual_id для сверки '
            'конкретной дневной строки. Счётчик other_posting_entries_excluded сохраняет прежнее имя '
            'для числа непустых несовпавших unit_number. Остальные категории исключены. Проверьте все страницы.')
    if args.get('view','summary') == 'details':
        result['matching_rows'] = selected
    else:
        groups = {}
        coverage = {sku:{'sku':sku,'matching_posting_products':0,'matching_item_entries':0,
                         'missing_delivery_services':0,'missing_commission':0,
                         'completeness':'not_verified'} for sku in sorted(requested,key=int)}
        def add(sku, component, kind, amount):
            currency = amount.get('currency') if amount else None
            key = (sku, component, kind, currency)
            group = groups.setdefault(key, {'sku':sku,'component':component,'type_id':kind,
                'currency':currency,'row_count':0,'known_amounts':0,'missing_amounts':0,
                'missing_currencies':0,'sum':Decimal(0)})
            group['row_count'] += 1
            amount_missing = not amount or amount.get('amount') is None
            currency_missing = not currency
            if amount_missing:
                group['missing_amounts'] += 1
            if currency_missing:
                group['missing_currencies'] += 1
            if amount_missing or currency_missing:
                return
            group['known_amounts'] += 1
            with localcontext() as context:
                context.prec = 80
                group['sum'] += Decimal(amount['amount'])
        for row in selected:
            if row['category'] == 'POSTING':
                coverage[row['sku']]['matching_posting_products'] += 1
                if row['delivery_services'] is None:
                    coverage[row['sku']]['missing_delivery_services'] += 1
                if row['commission'] is None:
                    coverage[row['sku']]['missing_commission'] += 1
                for fee in row['delivery_services'] or []:
                    add(row['sku'],'delivery_service',fee['type_id'],fee['accrued'])
                for field in ('sale_commission','commission'):
                    if row['commission'] is not None:
                        add(row['sku'],'commission.'+field,None,row['commission'][field])
            else:
                coverage[row['sku']]['matching_item_entries'] += 1
                for fee in row['fees']:
                    add(row['sku'],'item_fee',fee['type_id'],fee['accrued'])
        breakdown = []
        for group in groups.values():
            amount = group.pop('sum')
            group['observed_row_sum'] = format(amount,'f') if group['known_amounts'] else None
            group['financial_total'] = None
            group['completeness'] = 'not_verified'
            breakdown.append(group)
        result['breakdown'] = breakdown
        result['coverage_by_sku'] = list(coverage.values())
        result['financial_additivity'] = 'not_verified'
        result['financial_total'] = None
        result['not_in_summary'] = ['delivery_total_accrued','seller_price','sale_price',
                                    'sale_amount','coinvestment','bonus']
        result['summary_note'] = 'Эти поля доступны в details; их отсутствие в summary не означает ноль.'
    return result
