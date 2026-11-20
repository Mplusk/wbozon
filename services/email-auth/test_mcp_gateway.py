"""Exercise the authenticated portal boundary for every read-only MCP operation."""
import unittest
import urllib.error
from unittest.mock import patch
import test_portal


class GatewayTests(unittest.TestCase):
    setUp = test_portal.Tests.setUp
    tearDown = test_portal.Tests.tearDown
    request = test_portal.Tests.request

    def test_fast_seller_reads_bypass_saturated_report_pool_and_old_receipts_resume(self):
        import portal
        import report_jobs
        calls = []
        def direct(operation, args, creds, **kwargs):
            calls.append((operation, args))
            return {'status':'ready', 'complete':True, 'items':[]}
        self.app.gateway = direct
        with (patch('portal.provider_read', direct), patch('portal.os.name', 'posix'),
              patch.dict('portal.os.environ', {'MCP_PREPARE_MARKET_WORKERS':'1'}),
              patch.object(report_jobs, 'run', return_value={'status':'pending','reason':'waiting_worker'}) as queued):
            for name in ('wb_questions', 'wb_claims', 'wb_seller_info', 'ozon_supply_acts',
                         'ozon_promotions', 'ozon_competitor_prices'):
                with self.subTest(name=name):
                    self.assertEqual(self.app._dispatch_read(name, {}, {} )['status'], 'ready')
            queued.assert_not_called()
            # Receipts created by the previous release must remain resumable.
            self.app._dispatch_read('wb_questions', {'task_id':'mcpjob_'+'a'*32}, {})
            queued.assert_called_once()
        self.assertEqual(len(calls), 6)

    def test_all_operations_route_to_the_correct_saved_credentials(self):
        self.vault.save('ozon', {'client_id':'synthetic-id', 'api_key':'synthetic-ozon'})
        self.vault.save('wb', {'api_token':'synthetic-wb'})
        requests = [
            ('ozon_products', {'last_id':'cursor', 'limit':1}, 'client_id'),
            ('ozon_prices', {'cursor':'cursor', 'offer_ids':['SKU'], 'limit':1}, 'client_id'),
            ('wb_products', {'updated_at':'2026-11-15T00:00:00Z', 'nm_id':42, 'limit':1}, 'api_token'),
            ('wb_prices', {'offset':5, 'limit':1}, 'api_token'),
            ('wb_commissions', {'search':'тест', 'limit':1}, 'api_token'),
            ('wb_box_tariffs', {'date':'2026-11-15', 'warehouse':'склад', 'limit':1}, 'api_token'),
        ]
        for operation,args,credential_field in requests:
            with self.subTest(operation=operation):
                result=self.request('/api/read','POST',{'operation':operation,'arguments':args},auth=self.token)
                self.assertTrue(result['status'].startswith('200'))
                self.assertEqual(self.calls[-1][0:2],(operation,args))
                self.assertIn(credential_field,self.calls[-1][2])
                self.assertEqual(self.vault.bridge_activity('bridge')[0],operation)

    def test_upstream_permission_limit_and_timeout_are_safe_and_distinct(self):
        self.vault.save('wb', {'api_token':'synthetic-wb'})
        errors = [
            (urllib.error.HTTPError('',403,'secret',{},None), '403', 'provider_access_denied'),
            (urllib.error.HTTPError('',429,'secret',{},None), '429', 'provider_rate_limited'),
            (TimeoutError('secret'), '504', 'provider_timeout'),
        ]
        for error,code,message in errors:
            with self.subTest(code=code):
                def fail(*args): raise error
                self.app.gateway=fail
                result=self.request('/api/read','POST',{'operation':'wb_prices','arguments':{}},auth=self.token)
                self.assertTrue(result['status'].startswith(code))
                self.assertIn(message,result['body'])
                self.assertNotIn('secret',result['body'])
                self.assertEqual(self.vault.bridge_activity('bridge')[1],'failed')
