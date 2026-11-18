"""Conservative composition of existing Ozon identity and accrual projections.

The caller supplies the posting numbers. This module does not fetch Ozon, infer
financial categories, deduplicate accruals or claim an exhaustive period.
"""
import ozon_details

OPERATION = 'ozon_article_summary'


def validate_args(args):
    required={'offer_id','posting_numbers','date_from','date_to'}
    if not isinstance(args,dict) or not required <= set(args) or set(args)-required-{'include_types'}:
        raise ValueError('Invalid summary arguments')
    if 'include_types' in args and type(args['include_types']) is not bool:
        raise ValueError('Invalid type enrichment flag')
    ozon_details.validate_args('ozon_product_info',{'offer_ids':[args['offer_id']]})
    ozon_details.validate_args('ozon_posting_accruals',{
        'posting_numbers':args['posting_numbers'],'date_from':args['date_from'],
        'date_to':args['date_to'],'view':'summary'})


def _base(args):
    return {
        'period':{'basis':'accrual_date','date_from':args['date_from'],'date_to':args['date_to'],
                  'bounds':'inclusive','timezone':'not_verified',
                  'order_date_used':False,'realization_date_used':False},
        'selection':{'basis':'caller_supplied_posting_numbers',
                     'requested_posting_numbers':sorted(args['posting_numbers']),
                     'complete_period':False},
        'unknown_metrics':{'buyout_units':None,'return_units':None,'return_rate':None,
                           'commission_per_unit':None,'logistics_per_unit':None,
                           'last_mile_per_unit':None,'payout_per_unit':None,'financial_total':None},
        'unknown_metrics_basis':'complete_period_and_verified_bought_units',
        'unknown_reason':('Источник не подтверждает полный набор начислений за период, '
                          'выкупы, возвраты, распределение расходов или финансовую аддитивность.'),
        'read_only':True,'untrusted_data':True,
    }


def _blocked(args, reason):
    result=_base(args)
    result.update(status='blocked',reason=reason,identity=None,observations=None)
    return result


def _one_identity(args, projected):
    if not isinstance(projected,dict) or not isinstance(projected.get('items'),list):
        raise ValueError('Invalid identity projection')
    rows=projected['items']
    if not rows: return None, 'product_not_found'
    if (len(rows)!=1 or projected.get('conflicts') or projected.get('unmatched_row_indices') or
            projected.get('unmatched_identifiers')):
        return None, 'identity_ambiguous'
    item=rows[0]
    if not isinstance(item,dict): raise ValueError('Invalid identity projection')
    if (item.get('offer_id') != args['offer_id'] or not item.get('product_id') or not item.get('name') or
            item.get('incomplete_fields') or item.get('sources_reported') is not True or
            not isinstance(item.get('sku_candidates'),list) or not item['sku_candidates'] or
            not isinstance(item.get('sources'),list) or
            any(not isinstance(source,dict) or not source.get('sku') or not source.get('source')
                for source in item['sources']) or
            not isinstance(item.get('stock_sources'),list) or
            any(not isinstance(source,dict) or not source.get('sku') or not source.get('source')
                for source in item['stock_sources'])):
        return None, 'identity_incomplete'
    if len(item['sku_candidates']) > 100:
        return None, 'too_many_observed_skus'
    return item, None


def prepare_identity(args, forward):
    """Return the exact reverse-lookup request or a closed result."""
    validate_args(args)
    item, reason = _one_identity(args,forward)
    if reason: return None, _blocked(args,reason)
    return {'skus':list(item['sku_candidates'])}, None


def confirm_identity(args, forward, reverse):
    """Reject differing or ambiguous current mappings; historical coverage stays unknown."""
    validate_args(args)
    first, reason = _one_identity(args,forward)
    if reason: return None, _blocked(args,reason)
    second, reason = _one_identity(args,reverse)
    if reason: return None, _blocked(args,'reverse_'+reason)
    if (first['product_id'] != second['product_id'] or first['offer_id'] != second['offer_id'] or
            first['name'] != second['name'] or first['sku'] != second['sku'] or
            set(first['sku_candidates']) != set(second['sku_candidates']) or
            {(source['source'],source['sku']) for source in first['sources']} !=
            {(source['source'],source['sku']) for source in second['sources']} or
            first['stock_sources_reported'] != second['stock_sources_reported'] or
            {(source['source'],source['sku']) for source in first['stock_sources']} !=
            {(source['source'],source['sku']) for source in second['stock_sources']}):
        return None, _blocked(args,'identity_changed_during_lookup')
    identity={'offer_id':first['offer_id'],'product_id':first['product_id'],'name':first['name'],
              'primary_sku':first['sku'],
              'sku_candidates':list(first['sku_candidates']),'sources':list(first['sources']),
              'stock_sources':list(first['stock_sources']),
              'stock_sources_reported':first['stock_sources_reported'],
              'mapping':'current_forward_and_reverse_only','current_sku_coverage':'not_verified',
              'historical_sku_coverage':'not_verified',
              'cross_product_conflicts_outside_response':'not_verified'}
    return identity, None


def accrual_args(args, identity):
    validate_args(args)
    result={'posting_numbers':list(args['posting_numbers']),'date_from':args['date_from'],
            'date_to':args['date_to'],'view':'summary','skus':list(identity['sku_candidates'])}
    if 'include_types' in args:result['include_types']=args['include_types']
    return result


def _article_unit_rates(breakdown):
    groups = {}
    unknown_currency = {row['type_id'] for row in breakdown if row['currency'] is None}
    for row in breakdown:
        key = (row['type_id'], row['currency'], row.get('category'))
        groups.setdefault(key, []).append(row)
    result = []
    for (kind, currency, category), rows in groups.items():
        issues = {problem for row in rows for problem in row['observed_unit_rate']['issues']}
        if kind in unknown_currency:
            issues.add('unresolved_currency')
        lines = [(row['observed_row_sum'], row['observed_unit_rate']['reported_quantity']) for row in rows]
        result.append({'type_id':kind, 'category':category, 'currency':currency,
                       'observed_unit_rate':ozon_details.observed_unit_rate(lines, issues)})
    return result


def finish(args, identity, accruals, observed_at):
    validate_args(args)
    if (not isinstance(identity,dict) or not isinstance(accruals,dict) or
            accruals.get('summary_scope') != 'requested_postings_only' or
            accruals.get('period_basis') != 'accrual_date' or
            set(accruals.get('requested_posting_numbers',[])) != set(args['posting_numbers']) or
            accruals.get('date_from') != args['date_from'] or accruals.get('date_to') != args['date_to'] or
            set(accruals.get('filtered_skus',[])) != set(identity['sku_candidates']) or
            not isinstance(accruals.get('breakdown'),list)):
        raise ValueError('Invalid accrual projection')
    result=_base(args)
    selected_skus=set(identity['sku_candidates'])
    by_posting=[{**posting,'same_posting_equal_projection_groups':[
                    group for group in posting.get('same_posting_equal_projection_groups',[])
                    if group.get('sku') in selected_skus],
                 'row_indices_scope':'date_filtered_rows_before_sku_filter'}
                for posting in accruals.get('by_posting',[])]
    result['selection'].update(returned_posting_numbers=accruals['returned_posting_numbers'],
                               unreturned_posting_numbers=accruals['unreturned_posting_numbers'])
    result.update(status='partial',identity=identity,observed_at=observed_at,
        observations={'breakdown':accruals['breakdown'], 'unit_rate_basis':accruals.get('unit_rate_basis'),
                      'article_unit_rates':_article_unit_rates(accruals['breakdown']),
                      'observed_category_breakdown':accruals.get('observed_category_breakdown'),
                      'by_posting':by_posting,
                      'category_completeness':accruals.get('category_completeness','not_verified'),
                      'category_classification':('current_type_dictionary' if accruals.get('type_source') else 'not_requested'),
                      'category_note':accruals.get('category_note'),
                      'type_history':accruals.get('type_history','not_verified'),
                      'type_snapshot_consistency':accruals.get('type_snapshot_consistency','not_verified'),
                      'unattributed_rows_excluded':accruals['unattributed_rows_excluded'],
                      'other_sku_rows_excluded':accruals['other_sku_rows_excluded'],
                      'same_posting_equal_projection_groups':[
                          group for group in accruals['same_posting_equal_projection_groups']
                          if group.get('sku') in selected_skus],
                      'row_indices_scope':'date_filtered_rows_before_sku_filter',
                      'row_identity':'not_verified','financial_additivity':'not_verified',
                      'financial_completeness':'not_verified'},
        sources=['Ozon Seller API /v3/product/info/list','Ozon Seller API /v1/finance/accrual/postings']+
                ([accruals['type_source']] if accruals.get('type_source') else []),
        snapshot_consistency='not_verified',
        note=('Только переданные отправления. Дата относится к начислению, а не к заказу или реализации. '
              'Ставка по виду услуги делит начисленную сумму на число единиц именно в строках этой услуги. '
              'Одни и те же товары могут участвовать в нескольких услугах. Число выкупов и выплата неизвестны. '
              'Расходы без SKU не распределяются; полнота текущих и исторических SKU и периода не доказана.'))
    return result
