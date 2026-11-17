"""Local-only state machine for collecting synthetic projected accrual pages.

This module performs no I/O and is not exposed by MCP. The caller supplies
typed pages produced by a fake transport and must drive/check the deadline;
it cannot interrupt a blocked transport. No provider completeness is inferred.
"""
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from functools import wraps

import ozon_details

SOURCE = 'synthetic_projected_accrual_pages_only'
DAY_SOURCE = 'Ozon Seller API /v1/finance/accrual/by-day'


def _single_caller(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Concurrent synthetic collector use')
        try:return method(self,*args,**kwargs)
        finally:self._lock.release()
    return guarded


@dataclass(frozen=True)
class SyntheticRequest:
    sequence: int
    date: str
    cursor: str
    skus: tuple[str, ...]
    posting_filter: str | None
    deadline_monotonic: float


@dataclass(frozen=True)
class SyntheticPage:
    """Explicitly caller-supplied synthetic projection, not provider proof."""
    projection: dict


class SyntheticAccrualCollector:
    """One pending request at a time; all transport and scheduling is external."""
    def __init__(self,date_from,date_to,skus,*,posting_filter=None,max_pages_per_day=5,
                 max_total_pages=20,deadline_seconds=15,clock=time.monotonic):
        try:
            if any(type(x) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',x)
                   for x in (date_from,date_to)): raise ValueError()
            start,end=date.fromisoformat(date_from),date.fromisoformat(date_to)
        except (ValueError,TypeError): raise ValueError('Invalid synthetic period') from None
        if not 0 <= (end-start).days < 31: raise ValueError('Invalid synthetic period')
        if type(skus) not in (list,tuple):
            raise ValueError('Invalid synthetic SKUs')
        # Bound the copy before any callbacks can mutate the caller's list.
        sku_snapshot=skus[:101]
        if not 1 <= len(sku_snapshot) <= 100 or any(type(x) not in (str,int) for x in sku_snapshot):
            raise ValueError('Invalid synthetic SKUs')
        normalized=tuple(sorted((ozon_details._id(x) for x in sku_snapshot),key=int))
        if len(set(normalized))!=len(normalized): raise ValueError('Duplicate synthetic SKU')
        if posting_filter is not None:
            if type(posting_filter) is not str: raise ValueError('Invalid synthetic posting')
            ozon_details._text(posting_filter,128)
        if (type(max_pages_per_day) is not int or not 1 <= max_pages_per_day <= 20 or
                type(max_total_pages) is not int or not 1 <= max_total_pages <= 100 or
                type(deadline_seconds) not in (int,float) or not math.isfinite(deadline_seconds) or
                not 0 < deadline_seconds <= 60 or not callable(clock)):
            raise ValueError('Invalid synthetic budget')
        self._lock=threading.Lock();self.clock=clock
        self.started=self._clock_value()
        self.deadline=self.started+deadline_seconds
        if not math.isfinite(self.deadline): raise ValueError('Invalid synthetic deadline')
        self.days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
        self.skus=normalized;self.posting_filter=posting_filter
        self.max_pages_per_day=max_pages_per_day;self.max_total_pages=max_total_pages
        self.index=0;self.cursor='';self.cursors={''};self.pending=None;self.abandoned=None;self.issued=0
        self.last_clock=self.started;self.stop_reason=None;self.retry_after_seconds=None
        self.observed_error=None;self.late_page_discarded=False
        self.ids_by_day={day:set() for day in self.days};self.all_ids={};self.cross_day_ids=set()
        self.states={day:{'date':day,'accepted_pages':0,'observed_all_page_accrual_ids':0,
                          'terminal_cursor_observed':False,'state':'not_started'} for day in self.days}

    def _clock_value(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid synthetic clock')
        return value

    def _now(self):
        try: now=self._clock_value()
        except Exception:
            self._stop('clock_invalid')
            return None
        if now < self.last_clock:
            self._stop('clock_regressed')
            return None
        self.last_clock=now
        if now >= self.deadline:
            self._stop('deadline_exceeded')
            return None
        return now

    def _stop(self,reason,retry_after=None):
        if self.stop_reason is None:
            self.stop_reason=reason;self.retry_after_seconds=retry_after
            if self.index<len(self.days):self.states[self.days[self.index]]['state']=reason
            if self.pending is not None and reason in ('deadline_exceeded','clock_regressed','clock_invalid'):
                self.abandoned=self.pending
        self.pending=None

    @_single_caller
    def next_request(self):
        if self.stop_reason or self.index==len(self.days):return None
        if self.pending is not None: raise ValueError('Synthetic request already pending')
        if self._now() is None:return None
        day=self.days[self.index]
        if self.issued>=self.max_total_pages:
            self._stop('total_page_limit');return None
        if self.states[day]['accepted_pages']>=self.max_pages_per_day:
            self._stop('day_page_limit');return None
        self.issued+=1
        self.pending=SyntheticRequest(self.issued,day,self.cursor,self.skus,self.posting_filter,self.deadline)
        self.states[day]['state']='request_pending'
        return self.pending

    @_single_caller
    def accept_error(self,request,reason,*,retry_after_seconds=None):
        if reason not in ('rate_limited','provider_error','unknown_outcome'):
            raise ValueError('Invalid synthetic error')
        if retry_after_seconds is not None and (reason!='rate_limited' or type(retry_after_seconds) is not int or
                                                 not 0 <= retry_after_seconds <= 2147483647):
            raise ValueError('Invalid synthetic retry interval')
        if self.pending is None and request == self.abandoned and self.abandoned is not None:
            self.observed_error={'reason':reason,'retry_after_seconds':retry_after_seconds,'after_stop':True}
            self.abandoned=None;return
        if request != self.pending or self.pending is None: raise ValueError('Unexpected synthetic request')
        if self._now() is None:
            self.observed_error={'reason':reason,'retry_after_seconds':retry_after_seconds,'after_stop':True}
            self.abandoned=None;return
        self.observed_error={'reason':reason,'retry_after_seconds':retry_after_seconds,'after_stop':False}
        self._stop(reason,retry_after_seconds)

    @_single_caller
    def accept_page(self,request,page):
        if self.pending is None and request == self.abandoned and self.abandoned is not None:
            self.late_page_discarded=True;self.abandoned=None;return
        if request != self.pending or self.pending is None: raise ValueError('Unexpected synthetic request')
        if self._now() is None:
            self.late_page_discarded=True;self.abandoned=None;return
        if type(page) is not SyntheticPage or type(page.projection) is not dict or len(page.projection)>64:
            self._stop('invalid_page');return
        value=page.projection
        required={'source','period_basis','date','requested_cursor','selected_skus',
                  'posting_filter','next_cursor','page_accrual_ids'}
        selected_skus=value.get('selected_skus')
        if type(selected_skus) is list:selected_skus=selected_skus[:101]
        if (not all(key in value for key in required) or type(selected_skus) is not list or
                any(type(sku) is not str for sku in selected_skus) or
                any(type(value.get(key)) is not str for key in ('source','period_basis','date','requested_cursor')) or
                type(value.get('posting_filter')) not in (str,type(None)) or
                value.get('source')!=DAY_SOURCE or value.get('period_basis')!='accrual_date' or
                value.get('date')!=request.date or value.get('requested_cursor')!=request.cursor or
                selected_skus!=list(request.skus) or
                value.get('posting_filter')!=request.posting_filter):
            self._stop('request_response_mismatch');return
        cursor=value.get('next_cursor')
        try:
            if type(cursor) is not str or type(value.get('page_accrual_ids')) is not list:
                raise ValueError('Invalid synthetic identifiers')
            ozon_details._text(cursor,512,empty=True)
            # A caller may mutate its list while the clock is consulted. Copy a
            # bounded slice before any callbacks; never iterate the original.
            raw_ids=value['page_accrual_ids'][:5001]
            ozon_details._items(raw_ids,5000)
            ids=[]
            for index,item in enumerate(raw_ids):
                if index%128==0 and self._now() is None:
                    self.late_page_discarded=True;self.abandoned=None;return
                if type(item) not in (str,int): raise ValueError('Invalid synthetic identifier')
                ids.append(ozon_details._id(item))
        except (ValueError,TypeError):
            self._stop('invalid_page');return
        if len(ids)!=len(set(ids)) or self.ids_by_day[request.date].intersection(ids):
            self._stop('duplicate_accrual_id_same_day');return
        if cursor and cursor in self.cursors:
            self._stop('cursor_cycle');return
        if sum(len(items) for items in self.ids_by_day.values())+len(ids)>10000:
            self._stop('accrual_id_limit');return
        new_all=dict(self.all_ids);new_cross=set(self.cross_day_ids)
        for index,identifier in enumerate(ids):
            if index%128==0 and self._now() is None:
                self.late_page_discarded=True;self.abandoned=None;return
            if identifier in new_all and new_all[identifier]!=request.date:
                new_cross.add(identifier)
            new_all.setdefault(identifier,request.date)
        new_day_ids=self.ids_by_day[request.date].union(ids)
        if self._now() is None:
            self.late_page_discarded=True;self.abandoned=None;return
        self.all_ids=new_all;self.cross_day_ids=new_cross;self.ids_by_day[request.date]=new_day_ids
        day=self.states[request.date];day['accepted_pages']+=1
        day['observed_all_page_accrual_ids']+=len(ids)
        self.pending=None
        if cursor:
            self.cursors.add(cursor);self.cursor=cursor;day['state']='more_cursor_observed'
        else:
            day['terminal_cursor_observed']=True;day['state']='terminal_cursor_observed'
            self.index+=1;self.cursor='';self.cursors={''}

    @_single_caller
    def snapshot(self):
        """No financial total, completeness claim, raw payload or secret."""
        if self.index<len(self.days) and self.stop_reason is None:self._now()
        return {'source_scope':SOURCE,'period_basis':'accrual_date',
                'days':[dict(self.states[day]) for day in self.days],
                'requests_issued':self.issued,'stop_reason':self.stop_reason,
                'retry_after_seconds':self.retry_after_seconds,
                'observed_error':dict(self.observed_error) if self.observed_error is not None else None,
                'late_page_discarded':self.late_page_discarded,
                'cross_day_reused_id_count':len(self.cross_day_ids),
                'id_count_scope':'all_synthetic_page_accruals_not_sku_filtered',
                'all_requested_days_had_terminal_cursor':self.index==len(self.days),
                'complete_period':False,'provider_completeness':'not_verified',
                'financial_completeness':'not_verified','financial_total':None,
                'deadline_enforcement':'cooperative_caller_driven',
                'note':('Только подставные страницы; пустой курсор не доказывает '
                        'полноту источника, начислений или финансового периода.')}
