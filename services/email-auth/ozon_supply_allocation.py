"""Allocate a caller-documented single-supply cost over the observed FBO bundle by volume."""
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from math import ceil

import ozon_fbo as fbo
from ozon_details import _id, _money, _text

OPERATION = 'ozon_fbo_volume_allocation'
MAX_PAGES = 10
DOCUMENT_FIELDS = ('document_id', 'line_id', 'document_date', 'supply_id', 'scope', 'amount', 'currency')
# ISO 4217 List One, SIX Maintenance Agency, published 2026-09-17. Codes with no minor unit are omitted.
MINOR_UNITS_SOURCE = 'https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml'
MINOR_UNITS = {code: places for places, codes in {
    0: 'BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF',
    2: ('AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF '
        'CHE CHF CHW CNY COP COU CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD '
        'GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL '
        'MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR '
        'PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP '
        'TRY TTD TWD TZS UAH USD USN UYU UZS VED VES WST XAD XCD XCG YER ZAR ZMW ZWG'),
    3: 'BHD IQD JOD KWD LYD OMR TND',
    4: 'CLF UYW',
}.items() for code in codes.split()}


def validate_args(args):
    if not isinstance(args, dict) or set(args) - {'order_id', 'supply_id', 'expense'}:
        raise ValueError('Invalid allocation arguments')
    _id(args.get('order_id'))
    _id(args.get('supply_id'))
    if 'expense' not in args:
        return
    expense = args['expense']
    if not isinstance(expense, dict) or set(expense) - set(DOCUMENT_FIELDS) - {'accrual_id', 'period_from', 'period_to'}:
        raise ValueError('Invalid expense')
    for field in ('document_id', 'line_id'):
        if field in expense and not _text(expense[field], 128).strip():
            raise ValueError('Empty document reference')
    for field in ('supply_id', 'accrual_id'):
        if field in expense:
            _id(expense[field])
    if 'scope' in expense and expense['scope'] not in ('single_supply', 'shared_or_unknown'):
        raise ValueError('Invalid document scope')
    for field in ('document_date', 'period_from', 'period_to'):
        if field in expense:
            value = expense[field]
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError('Invalid document date')
    if ('period_from' in expense) != ('period_to' in expense) or expense.get('period_from', '') > expense.get('period_to', ''):
        raise ValueError('Invalid document period')
    _money(expense, None)


def _exact_decimal(value):
    # Only called for quantities and litre volumes obtained by multiplying/summing bounded decimal inputs.
    with localcontext() as ctx:
        ctx.prec = max(len(str(abs(value.numerator))) + len(str(value.denominator)) + 2, 40)
        return format(Decimal(value.numerator) / Decimal(value.denominator), 'f')


def _ratio(value):
    return {'numerator': str(value.numerator), 'denominator': str(value.denominator)}


def _money_string(units, places):
    value = str(abs(units)).zfill(places + 1)
    text = (value[:-places] + '.' + value[-places:]) if places else value
    return ('-' if units < 0 else '') + text


def _distribute(rows, volumes, total_volume, expense, places):
    cost = Decimal(expense['amount'])
    quanta = abs(int(Fraction(cost) * 10 ** places))
    exact = [quanta * volume / total_volume for volume in volumes]
    floor = [share.numerator // share.denominator for share in exact]
    order = sorted(range(len(rows)), key=lambda i: (-(exact[i] - floor[i]), int(rows[i]['sku'])))
    for i in order[:quanta - sum(floor)]:
        floor[i] += 1
    for row, volume, allocated in zip(rows, volumes, floor):
        unit_cost = Fraction(cost) * volume / (total_volume * row['quantity'])
        row['cost_per_unit_exact'] = _ratio(unit_cost)
        row['allocated_amount'] = _money_string(allocated if cost >= 0 else -allocated, places)


def _select_supply(details, supply_id):
    if details['issues']:
        return None, details['issues']
    supplies = details['orders'][0]['supplies']
    matches = [s for s in supplies if s['supply_id'] == supply_id]
    if len(matches) != 1 or not matches[0]['bundle_id']:
        return None, ['supply_bundle_link_unconfirmed']
    supply, = matches
    if sum(s['bundle_id'] == supply['bundle_id'] for s in supplies) != 1:
        return None, ['bundle_shared_between_supplies']
    return supply, []


def _read_bundle(bundle_id, read):
    result = {'items': [], 'pages': 0, 'first_read': None, 'last_read': None, 'issues': set()}
    requested, counts, seen, cursor = set(), set(), set(), ''
    for _ in range(MAX_PAGES):
        requested.add(cursor)
        try:
            page = read(fbo.BUNDLE, {'bundle_id': bundle_id, 'last_id': cursor, 'limit': 100})
        except TimeoutError:
            result['issues'].add('composition_read_timeout')
            break
        result['pages'] += 1
        result['first_read'] = result['first_read'] or page['observed_at']
        result['last_read'] = page['observed_at']
        result['items'].extend(page['items'])
        counts.add(page['total_count'])
        result['issues'].update(page['issues'])
        for row in page['items']:
            if row['sku'] in seen:
                result['issues'].add('repeated_sku')
            seen.add(row['sku'])
        if result['issues'] or page['has_next'] is False:
            break
        cursor = page['next_cursor']
        if not cursor or cursor in requested:
            result['issues'].add('unusable_bundle_cursor')
            break
    else:
        result['issues'].add('bundle_page_limit_reached')
    if not result['items']:
        result['issues'].add('empty_composition')
    if counts != {len(result['items'])}:
        result['issues'].add('unreconciled_bundle_total_count')
    return result


def calculate(args, read):
    validate_args(args)
    order_id, supply_id = _id(args['order_id']), _id(args['supply_id'])
    expense = args.get('expense', {})
    issues = set()
    if any(expense.get(field) in (None, '') for field in DOCUMENT_FIELDS):
        issues.add('missing_supply_cost_document_fields')
    if expense.get('scope') != 'single_supply' or str(expense.get('supply_id')) != supply_id:
        issues.add('expense_link_to_single_supply_unconfirmed')
    if 'accrual_id' in expense:
        issues.add('ozon_accrual_to_supply_link_unverified')
    places = MINOR_UNITS.get(expense.get('currency'))
    if expense.get('currency') and places is None:
        issues.add('currency_minor_unit_unknown')
    if expense.get('amount') is not None and places is not None and (Fraction(expense['amount']) * 10 ** places).denominator != 1:
        issues.add('expense_exceeds_currency_precision')
    result = {'kind': 'volume_allocation_model', 'status': 'incomplete', 'source_basis': 'caller_supplied_document_line',
              'order_id': order_id, 'supply_id': supply_id, 'expense': expense or None,
              'bundle_id': None, 'supply_state': None, 'composition_observed_from': None, 'composition_observed_to': None,
              'pages_observed': 0, 'validation_pages': 0, 'composition_check': 'not_completed',
              'snapshot_consistency': 'not_guaranteed_by_ozon', 'items': [], 'total_volume_litres': None, 'allocated_total': None,
              'volume_basis': 'quantity_times_volume_in_litres', 'volume_differences': [],
              'ozon_expense_link_verified': False, 'included_in_article_actuals': False, 'read_only': True,
              'untrusted_data': True, 'quantity_basis': 'declared_bundle', 'volume_unit': 'litre',
              'sources': [fbo.ENDPOINTS[fbo.GET], fbo.ENDPOINTS[fbo.BUNDLE]],
              'formula': 'V = sum(quantity_i * litres_per_unit_i); cost_per_unit_i = cost * litres_per_unit_i / V',
              'currency_minor_unit_digits': places, 'currency_minor_unit_source': MINOR_UNITS_SOURCE,
              'rounding': 'Суммы по SKU распределены методом наибольших остатков до минимальной единицы валюты. '
                          'При равном остатке первым идёт меньший SKU. Ставка на штуку дана точной дробью в валюте строки.',
              'note': 'Оценка одной строки документа. Расход и его привязку указывает вызывающее приложение. '
                      'База модели — количество × объём штуки. Если итог строки Ozon совпадает с округлением этой '
                      'базы вверх до литра, оба значения видны в volume_differences. Способ тарификации неизвестен. '
                      'Количество и объём происходят из состава заявки за интервал чтения. Ozon не гарантирует '
                      'единого снимка между страницами. Фактическая приёмка, исторический состав, независимое '
                      'подтверждение документа и полнота всех затрат неизвестны.'}

    def incomplete(*reasons):
        result['issues'] = sorted(issues.union(reasons))
        return result

    try:
        details = read(fbo.GET, {'order_ids': [order_id]})
        supply, supply_issues = _select_supply(details, supply_id)
        if supply_issues:
            return incomplete(*supply_issues)
        bundle_id = supply['bundle_id']
        result.update(bundle_id=bundle_id, supply_state=supply['state'])
        bundle = _read_bundle(bundle_id, read)
        issues.update(bundle['issues'])
        result.update(pages_observed=bundle['pages'], items=bundle['items'], composition_observed_from=bundle['first_read'],
                      composition_observed_to=bundle['last_read'])
    except TimeoutError:
        return incomplete('composition_read_timeout')

    rows = result['items']
    volumes = []
    for row in rows:
        if not row['sku'] or not row['offer_id']:
            issues.add('incomplete_item_identity')
        if row['contractor_item_code'] and row['contractor_item_code'] != row['offer_id']:
            issues.add('conflicting_article_identifiers')
        if not row['quantity']:
            issues.add('missing_or_zero_quantity')
        if row['volume_in_litres'] is None or Decimal(row['volume_in_litres']) <= 0:
            issues.add('missing_or_zero_unit_volume')
            continue
        if row['quantity']:
            volume = Fraction(row['volume_in_litres']) * row['quantity']
            volumes.append(volume)
            reported = row['total_volume_in_litres']
            if reported is not None and Fraction(reported) != volume:
                matches_ceiling = Fraction(reported) == ceil(volume)
                result['volume_differences'].append({
                    'sku': row['sku'], 'quantity_times_unit_litres': _exact_decimal(volume), 'reported_total_litres': reported,
                    'relation': 'equals_whole_litre_ceiling' if matches_ceiling else 'unreconciled'})
                if not matches_ceiling:
                    issues.add('inconsistent_provider_volumes')
    if issues:
        return incomplete()
    try:
        if bundle['pages'] > 1:
            repeated = _read_bundle(bundle_id, read)
            result['validation_pages'] = repeated['pages']
            result['composition_observed_to'] = repeated['last_read'] or bundle['last_read']
            if repeated['issues']:
                return incomplete(*repeated['issues'])
            if {r['sku']: r for r in rows} != {r['sku']: r for r in repeated['items']}:
                return incomplete('composition_changed_during_read')
        verified, supply_issues = _select_supply(read(fbo.GET, {'order_ids': [order_id]}), supply_id)
        if supply_issues or verified != supply:
            return incomplete('supply_changed_during_read', *supply_issues)
    except TimeoutError:
        return incomplete('composition_read_timeout')
    total_volume = sum(volumes)
    for row, volume in zip(rows, volumes):
        row['derived_line_volume_litres'] = _exact_decimal(volume)
    _distribute(rows, volumes, total_volume, expense, places)
    result['composition_check'] = 'matched_two_traversals' if bundle['pages'] > 1 else 'single_provider_page'
    result.update(status='modeled_on_document', issues=[], total_volume_litres=_exact_decimal(total_volume),
                  allocated_total={'amount': expense['amount'], 'currency': expense['currency']})
    return result
