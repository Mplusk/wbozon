"""Synthetic transport only; no real network or marketplace credentials."""
import threading
import unittest
from unittest.mock import patch

import ozon_accrual_day
import ozon_details
from ozon_synthetic_accrual import SyntheticAccrualCollector, SyntheticPage


class FakeClock:
    def __init__(self):self.now=100.0
    def __call__(self):return self.now


class FakeTransport:
    def __init__(self,steps):self.steps=list(steps);self.requests=[]
    def fetch(self,request):
        self.requests.append(request)
        return self.steps.pop(0)


def page(request,cursor,identifiers):
    args={'date':request.date,'skus':list(request.skus),'last_id':request.cursor}
    if request.posting_filter is not None:args['posting_number']=request.posting_filter
    data={'last_id':cursor,'accruals':[{'accrual_id':item,'date':request.date,
                                      'accrued_category':'NON_ITEM'} for item in identifiers]}
    projection=ozon_accrual_day.project_response(args,data,{'client_id':'123','api_key':'synthetic-key'})
    return SyntheticPage(projection)


class SyntheticAccrualCollectorTests(unittest.TestCase):
    def collector(self,**kwargs):
        clock=kwargs.pop('clock',FakeClock())
        return SyntheticAccrualCollector('2026-11-01','2026-11-02',['11','10'],clock=clock,**kwargs),clock

    def test_bounded_scripted_pages_are_not_a_complete_financial_period(self):
        collector,_=self.collector()
        transport=FakeTransport([('next',[1]),('',[2]),('',[3])])
        while request:=collector.next_request():
            cursor,ids=transport.fetch(request)
            collector.accept_page(request,page(request,cursor,ids))
        result=collector.snapshot()
        self.assertEqual([(x.date,x.cursor) for x in transport.requests],
                         [('2026-11-01',''),('2026-11-01','next'),('2026-11-02','')])
        self.assertEqual([x['accepted_pages'] for x in result['days']],[2,1])
        self.assertTrue(result['all_requested_days_had_terminal_cursor'])
        self.assertEqual(result['source_scope'],'synthetic_projected_accrual_pages_only')
        self.assertFalse(result['complete_period'])
        self.assertEqual(result['provider_completeness'],'not_verified')
        self.assertIsNone(result['financial_total'])

    def test_rate_limit_preserves_partial_pages_and_stops_without_retry(self):
        collector,_=self.collector()
        first=collector.next_request();collector.accept_page(first,page(first,'next',[1]))
        second=collector.next_request();collector.accept_error(second,'rate_limited',retry_after_seconds=45)
        self.assertIsNone(collector.next_request())
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'rate_limited')
        self.assertEqual(result['retry_after_seconds'],45)
        self.assertEqual(result['requests_issued'],2)
        self.assertEqual(result['days'][0]['accepted_pages'],1)
        self.assertEqual(result['days'][1]['state'],'not_started')
        self.assertFalse(result['complete_period'])
        result['observed_error']['reason']='provider_error'
        result['observed_error']['retry_after_seconds']=0
        self.assertEqual(collector.snapshot()['observed_error']['reason'],'rate_limited')
        self.assertEqual(collector.snapshot()['observed_error']['retry_after_seconds'],45)

    def test_cursor_cycle_and_duplicate_accrual_id_stop_without_silent_deduplication(self):
        collector,_=self.collector()
        for cursor,ids in [('A',[1]),('B',[2])]:
            request=collector.next_request();collector.accept_page(request,page(request,cursor,ids))
        request=collector.next_request();collector.accept_page(request,page(request,'A',[3]))
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'cursor_cycle')
        self.assertEqual(result['days'][0]['accepted_pages'],2)
        self.assertEqual(result['days'][0]['observed_all_page_accrual_ids'],2)
        self.assertIsNone(collector.next_request())
        collector,_=self.collector()
        first=collector.next_request();collector.accept_page(first,page(first,'next',[1]))
        second=collector.next_request()
        collector.accept_page(second,SyntheticPage({**page(second,'', [2]).projection,'page_accrual_ids':['1']}))
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'duplicate_accrual_id_same_day')
        self.assertEqual(result['days'][0]['accepted_pages'],1)

    def test_reused_id_across_days_is_flagged_not_deduplicated(self):
        collector,_=self.collector()
        first=collector.next_request();collector.accept_page(first,page(first,'',[1]))
        second=collector.next_request();collector.accept_page(second,page(second,'',[1]))
        result=collector.snapshot()
        self.assertEqual(result['cross_day_reused_id_count'],1)
        self.assertEqual([day['observed_all_page_accrual_ids'] for day in result['days']],[1,1])
        self.assertEqual(result['id_count_scope'],'all_synthetic_page_accruals_not_sku_filtered')
        self.assertFalse(result['complete_period'])

    def test_page_limits_stop_before_issuing_more_work(self):
        collector,_=self.collector(max_pages_per_day=1)
        first=collector.next_request();collector.accept_page(first,page(first,'next',[1]))
        self.assertIsNone(collector.next_request())
        self.assertEqual(collector.snapshot()['stop_reason'],'day_page_limit')
        collector,_=self.collector(max_total_pages=1)
        first=collector.next_request();collector.accept_page(first,page(first,'',[1]))
        self.assertIsNone(collector.next_request())
        self.assertEqual(collector.snapshot()['stop_reason'],'total_page_limit')

    def test_concurrent_calls_cannot_issue_two_pending_requests(self):
        entered=threading.Event();release=threading.Event();calls=[]
        class BlockingClock:
            def __init__(self):self.calls=0
            def __call__(self):
                self.calls+=1
                if self.calls>1:
                    entered.set()
                    if not release.wait(2):raise RuntimeError('test timed out')
                return 100.0
        collector,_=self.collector(clock=BlockingClock())
        thread=threading.Thread(target=lambda:calls.append(collector.next_request()))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(RuntimeError):collector.next_request()
        finally:
            release.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].sequence,1)
        self.assertEqual(collector.snapshot()['requests_issued'],1)

    def test_late_page_is_discarded_and_late_429_is_retained(self):
        collector,clock=self.collector(deadline_seconds=3)
        first=collector.next_request();clock.now+=4
        collector.accept_page(first,page(first,'',[1]))
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'deadline_exceeded')
        self.assertTrue(result['late_page_discarded'])
        self.assertEqual(result['days'][0]['accepted_pages'],0)
        collector,clock=self.collector(deadline_seconds=3)
        first=collector.next_request();clock.now+=4
        self.assertEqual(collector.snapshot()['stop_reason'],'deadline_exceeded')
        collector.accept_error(first,'rate_limited',retry_after_seconds=60)
        result=collector.snapshot()
        self.assertEqual(result['observed_error'],{'reason':'rate_limited','retry_after_seconds':60,'after_stop':True})
        self.assertIsNone(collector.next_request())

    def test_response_mismatch_and_clock_regression_stop_the_collection(self):
        collector,clock=self.collector(posting_filter='A-1')
        first=collector.next_request()
        wrong={**page(first,'',[1]).projection,'posting_filter':'A-10'}
        collector.accept_page(first,SyntheticPage(wrong))
        self.assertEqual(collector.snapshot()['stop_reason'],'request_response_mismatch')
        collector,clock=self.collector()
        first=collector.next_request();clock.now-=1
        collector.accept_page(first,page(first,'',[1]))
        self.assertEqual(collector.snapshot()['stop_reason'],'clock_regressed')

    def test_missing_posting_filter_and_invalid_clock_fail_closed(self):
        collector,clock=self.collector()
        first=collector.next_request();value=page(first,'',[1]).projection
        value.pop('posting_filter')
        collector.accept_page(first,SyntheticPage(value))
        self.assertEqual(collector.snapshot()['stop_reason'],'request_response_mismatch')
        collector,clock=self.collector()
        first=collector.next_request();synthetic=page(first,'',[1]);clock.now=float('nan')
        collector.accept_page(first,synthetic)
        clock.now=101
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'clock_invalid')
        self.assertEqual(result['days'][0]['accepted_pages'],0)
        self.assertIsNone(collector.next_request())

    def test_excessive_projection_fields_fail_before_processing(self):
        collector,_=self.collector()
        first=collector.next_request();value=page(first,'',[1]).projection
        value.update({'extra_'+str(n):None for n in range(65)})
        collector.accept_page(first,SyntheticPage(value))
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'invalid_page')
        self.assertEqual(result['days'][0]['accepted_pages'],0)

    def test_custom_list_cannot_bypass_sku_or_page_id_limits(self):
        class MisleadingList(list):
            def __len__(self):return 1
            def __iter__(self):return iter(range(1,5002))
        with self.assertRaises(ValueError):
            SyntheticAccrualCollector('2026-11-01','2026-11-02',MisleadingList([1]))
        collector,_=self.collector()
        first=collector.next_request();value=page(first,'',[1]).projection
        value['page_accrual_ids']=MisleadingList([1])
        collector.accept_page(first,SyntheticPage(value))
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'invalid_page')
        self.assertEqual(result['days'][0]['accepted_pages'],0)

    def test_mutating_skus_during_normalization_cannot_expand_input(self):
        skus=[10];original=ozon_details._id
        def mutate(value,*args):
            if len(skus)==1:skus.extend(range(11,112))
            return original(value,*args)
        with patch('ozon_synthetic_accrual.ozon_details._id',side_effect=mutate):
            collector=SyntheticAccrualCollector('2026-11-01','2026-11-02',skus,clock=FakeClock())
        self.assertGreater(len(skus),100)
        self.assertEqual(collector.next_request().skus,('10',))

    def test_mutating_original_page_ids_cannot_expand_the_bounded_snapshot(self):
        class MutatingClock:
            def __init__(self):self.calls=0;self.target=None
            def __call__(self):
                self.calls+=1
                if self.calls==4 and self.target is not None:self.target.extend(range(2,5003))
                return 100.0
        clock=MutatingClock();collector,_=self.collector(clock=clock)
        first=collector.next_request();synthetic=page(first,'',[1])
        clock.target=synthetic.projection['page_accrual_ids']
        collector.accept_page(first,synthetic)
        result=collector.snapshot()
        self.assertGreater(len(clock.target),5000)
        self.assertEqual(result['days'][0]['observed_all_page_accrual_ids'],1)
        self.assertEqual(result['days'][0]['accepted_pages'],1)

    def test_deadline_during_page_processing_does_not_commit_a_late_page(self):
        collector,clock=self.collector(deadline_seconds=3)
        first=collector.next_request();synthetic=page(first,'',[1])
        original=ozon_details._id
        def slow_id(value,*args):
            clock.now+=4
            return original(value,*args)
        with patch('ozon_synthetic_accrual.ozon_details._id',side_effect=slow_id):
            collector.accept_page(first,synthetic)
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'deadline_exceeded')
        self.assertTrue(result['late_page_discarded'])
        self.assertEqual(result['days'][0]['accepted_pages'],0)
        self.assertFalse(result['all_requested_days_had_terminal_cursor'])

    def test_snapshot_expires_even_between_requests(self):
        collector,clock=self.collector(deadline_seconds=3)
        first=collector.next_request();collector.accept_page(first,page(first,'next',[1]))
        clock.now+=4
        result=collector.snapshot()
        self.assertEqual(result['stop_reason'],'deadline_exceeded')
        self.assertEqual(result['days'][0]['accepted_pages'],1)
        self.assertIsNone(collector.next_request())

    def test_multiple_product_rows_inside_one_accrual_are_not_mistaken_for_duplicate_ids(self):
        collector,_=self.collector()
        first=collector.next_request()
        args={'date':first.date,'skus':list(first.skus),'last_id':first.cursor}
        product={'sku':10,'quantity':1,'delivery':None,'commission':None}
        data={'last_id':'','accruals':[{'accrual_id':1,'date':first.date,'unit_number':'A-1',
                                       'accrued_category':'POSTING','posting':{'products':[product,product.copy()]}}]}
        projection=ozon_accrual_day.project_response(args,data,{'client_id':'123','api_key':'synthetic-key'})
        collector.accept_page(first,SyntheticPage(projection))
        result=collector.snapshot()
        self.assertEqual(result['days'][0]['observed_all_page_accrual_ids'],1)
        self.assertEqual(result['days'][0]['accepted_pages'],1)
        self.assertIsNone(result['stop_reason'])

    def test_empty_page_and_nested_rows_never_become_a_financial_zero(self):
        collector,_=self.collector()
        first=collector.next_request();collector.accept_page(first,page(first,'',[]))
        second=collector.next_request();collector.accept_page(second,page(second,'',[]))
        result=collector.snapshot()
        self.assertTrue(result['all_requested_days_had_terminal_cursor'])
        self.assertFalse(result['complete_period'])
        self.assertIsNone(result['financial_total'])
        self.assertEqual(result['financial_completeness'],'not_verified')


if __name__=='__main__':unittest.main()
