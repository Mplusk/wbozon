"""A bounded page of Ozon return events, not a financial return total."""
import json
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone

import ozon_details

OPERATION = 'ozon_return_events'
ENDPOINT = 'https://api-seller.ozon.ru/v1/returns/list'


def _period(args):
    days=[]
    for key in ('date_from','date_to'):
        value=args[key]
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise ValueError('Invalid return date')
        try: days.append(date.fromisoformat(value))
        except ValueError: raise ValueError('Invalid return date') from None
    if not (date(2020,1,1) <= days[0] <= days[1] <= date(2099,12,30)) or (days[1]-days[0]).days >= 90:
        raise ValueError('Invalid return period')
    return days[0],days[1]+timedelta(days=1)


def validate_args(args):
    required={'offer_id','date_from','date_to','scheme'}
    if not isinstance(args,dict) or not required <= set(args) or set(args)-required-{'last_id','limit','view'}:
        raise ValueError('Invalid return arguments')
    ozon_details._text(args['offer_id'],256)
    _period(args)
    if args['scheme'] not in ('FBO','FBS'): raise ValueError('Invalid return scheme')
    if 'last_id' in args: ozon_details._id(args['last_id'])
    if type(args.get('limit',100)) is not int or not 1 <= args.get('limit',100) <= 100:
        raise ValueError('Invalid return page size')
    if args.get('view','summary') not in ('summary','details'): raise ValueError('Invalid return view')


def build_request(args,creds):
    validate_args(args)
    start,stop=_period(args)
    body={'filter':{'return_schema':args['scheme'],'offer_id':args['offer_id'],
                    'logistic_return_date':{'time_from':start.isoformat()+'T00:00:00Z',
                                            'time_to':stop.isoformat()+'T00:00:00Z'}},
          'limit':args.get('limit',100)}
    if 'last_id' in args: body['last_id']=int(ozon_details._id(args['last_id']))
    return urllib.request.Request(ENDPOINT,data=json.dumps(body,ensure_ascii=False).encode('utf-8'),method='POST',
        headers={'Client-Id':creds['client_id'],'Api-Key':creds['api_key'],'Content-Type':'application/json'})


def _timestamp(value,secret):
    if value is None: return None,None
    value=ozon_details._text(value,64,secret=secret)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})',value):
        return value,None
    try: parsed=datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)
    except ValueError: return value,None
    return value,parsed


def project_response(args,data,creds):
    validate_args(args)
    if not isinstance(data,dict) or type(data.get('has_next')) is not bool:
        raise ValueError('Invalid return page')
    rows=ozon_details._items(data.get('returns'),args.get('limit',100))
    secret=creds.get('api_key') if isinstance(creds.get('api_key'),str) else None
    start,stop=_period(args)
    start_dt=datetime.combine(start,datetime.min.time(),tzinfo=timezone.utc)
    stop_dt=datetime.combine(stop,datetime.min.time(),tzinfo=timezone.utc)
    ids=[]; selected=[]
    excluded={'different_or_missing_offer':0,'different_or_missing_scheme':0,
              'missing_or_invalid_return_date':0,'outside_utc_period':0}
    for row in rows:
        if not isinstance(row,dict): raise ValueError('Invalid return row')
        identifier=ozon_details._id(row.get('id'),secret)
        if identifier in ids: raise ValueError('Duplicate return ID')
        ids.append(identifier)
        product=row.get('product')
        if not isinstance(product,dict):
            excluded['different_or_missing_offer']+=1;continue
        offer=product.get('offer_id')
        if offer is None: excluded['different_or_missing_offer']+=1;continue
        offer=ozon_details._text(offer,256,secret=secret)
        if offer != args['offer_id']:
            excluded['different_or_missing_offer']+=1;continue
        scheme=row.get('schema')
        if scheme is None: excluded['different_or_missing_scheme']+=1;continue
        scheme=ozon_details._text(scheme,32,secret=secret)
        if scheme.casefold()!=args['scheme'].casefold():
            excluded['different_or_missing_scheme']+=1;continue
        logistic=row.get('logistic')
        if not isinstance(logistic,dict):
            excluded['missing_or_invalid_return_date']+=1;continue
        returned,parsed=_timestamp(logistic.get('return_date'),secret)
        if parsed is None:
            excluded['missing_or_invalid_return_date']+=1;continue
        if not start_dt <= parsed < stop_dt:
            excluded['outside_utc_period']+=1;continue
        kind=row.get('type')
        if kind is not None: kind=ozon_details._text(kind,80,secret=secret)
        posting=row.get('posting_number')
        if posting is not None: posting=ozon_details._text(posting,128,secret=secret)
        sku=ozon_details._optional_sku(product.get('sku'),secret)
        name=product.get('name')
        if name is not None: name=ozon_details._text(name,1024,empty=True,secret=secret)
        quantity=product.get('quantity')
        if quantity is not None and (type(quantity) is not int or not 0 <= quantity <= 1000000):
            raise ValueError('Invalid return quantity')
        visual=row.get('visual')
        if visual is not None and not isinstance(visual,dict): raise ValueError('Invalid return status')
        status=visual.get('status') if visual else None
        if status is not None and not isinstance(status,dict): raise ValueError('Invalid return status')
        sys_name=status.get('sys_name') if status else None
        if sys_name is not None: sys_name=ozon_details._text(sys_name,128,secret=secret)
        status_name=status.get('display_name') if status else None
        if status_name is not None: status_name=ozon_details._text(status_name,256,empty=True,secret=secret)
        final,final_parsed=_timestamp(logistic.get('final_moment'),secret)
        changed,changed_parsed=_timestamp(visual.get('change_moment'),secret) if visual else (None,None)
        if (final is not None and final_parsed is None) or (changed is not None and changed_parsed is None):
            raise ValueError('Invalid return status date')
        selected.append({'return_id':identifier,'offer_id':offer,'sku':sku,'name':name,'type':kind,
                         'scheme':scheme,'posting_number':posting,'quantity':quantity,
                         'logistic_return_date':returned,'logistic_final_moment':final,
                         'status_system_name':sys_name,'status_display_name':status_name,
                         'status_change_moment':changed})
    next_id=ids[-1] if data['has_next'] and ids else None
    if data['has_next'] and (not next_id or next_id==str(args.get('last_id',''))):
        raise ValueError('Invalid return continuation')
    result={'source':'Ozon Seller API /v1/returns/list','offer_id':args['offer_id'],
        'date_from':args['date_from'],'date_to':args['date_to'],'scheme':args['scheme'],
        'period_basis':'logistic.return_date','timezone':'UTC','order_date_filter_used':False,
        'provider_upper_bound':'following_midnight_utc_boundary_not_verified',
        'requested_last_id':str(args['last_id']) if 'last_id' in args else None,
        'next_last_id_candidate':next_id,'provider_has_next':data['has_next'],
        'page_return_ids':ids,'excluded':excluded,'matched_rows':len(selected),
        'period_completeness':'not_verified','quantity_additivity':'not_verified',
        'quantity_completeness':'not_verified','financial_return_total':None,
        'note':('События по logistic.return_date и текущему статусу Ozon; не все являются '
                'завершёнными клиентскими возвратами. Не смешивайте с датой заказа или '
                'реализации. Проверьте обе схемы, страницы, повторы и изменения статусов; '
                'полнота периода и сумма финансового возврата не подтверждены.'),
        'read_only':True,'untrusted_data':True}
    if args.get('view','summary')=='details':
        result['events']=selected
    else:
        groups={}
        for row in selected:
            key=(row['sku'],row['type'],row['status_system_name'])
            group=groups.setdefault(key,{'sku':row['sku'],'type':row['type'],
                'status_system_name':row['status_system_name'],'event_rows':0,
                'observed_quantity_row_sum':None,'missing_quantities':0,
                'quantity_additivity':'not_verified','quantity_completeness':'not_verified'})
            group['event_rows']+=1
            if row['quantity'] is None:group['missing_quantities']+=1
            else:group['observed_quantity_row_sum']=(group['observed_quantity_row_sum'] or 0)+row['quantity']
        result['breakdown']=list(groups.values())
    return result
