import hashlib
from unittest.mock import patch
import unittest
import portal
import test_portal


class BudgetVaultPortalTests(test_portal.Tests):
    def setUp(self):
        super().setUp()
        original_gateway=self.app.gateway
        self.app=portal.Portal(self.login,self.vault,hashlib.sha256(self.token.encode()).hexdigest(),
            gateway=original_gateway,budget_vault_feature=True,budget_vault_path=self.vault.path)

    def test_owner_setup_csrf_scope_and_recovery_confirmation(self):
        enable='/settings/content-factory/budget/vault-enable'
        disable='/settings/content-factory/budget/vault-disable'
        arm='/settings/content-factory/ai/gateway'
        secret='synthetic-budget-control-key-123456'
        fields={'csrf':self.csrf,'api_key':secret,'ack_scope':'understood','ack_estimate':'understood'}
        self.assertTrue(self.request(enable,'POST',fields,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(enable,'POST',fields,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(enable,'POST',{**fields,'csrf':'bad'})['status'].startswith('403'))
        self.assertTrue(self.request(enable,'POST',{k:v for k,v in fields.items() if k!='ack_scope'})['status'].startswith('400'))
        result=self.request(enable,'POST',fields)
        self.assertTrue(result['status'].startswith('303'))
        self.assertNotIn(secret,result['body'])
        self.assertEqual(self.vault.read('n8n_content_factory_budget_write'),{'api_key':secret})
        self.assertNotIn('n8n_content_factory_write',self.vault.present())
        self.assertTrue(self.request(arm,'POST',{'csrf':self.csrf,'action':'arm'})['status'].startswith('400'))
        self.assertTrue(self.request(arm,'POST',{'csrf':self.csrf,'action':'arm','ack_recovery':'understood'})['status'].startswith('303'))
        self.assertTrue(self.app.budget_gateway_available())
        self.assertTrue(self.request('/settings/content-factory/ai/write-token','POST',{'csrf':self.csrf,'api_key':'other-token-1234567890'})['status'].startswith('503'))
        with patch('content_factory.snapshot',return_value={'status':'unavailable'}):
            page=self.request('/settings',headers={'QUERY_STRING':'tab=content-factory'})
        self.assertIn('Откат данных автоматически не обнаруживается',page['body'])
        self.assertIn('Если результат неизвестен, не включайте управление',page['body'])
        self.assertNotIn(secret,page['body'])
        self.assertIn('оценочный бюджет',page['body'])
        # Avoid the per-session rate limit from unrelated negative setup tests.
        self.now+=301
        # A fresh session is required again after five minutes.
        self.assertTrue(self.request(disable,'POST',{'csrf':self.csrf})['status'].startswith('403'))

    def test_default_feature_gate_fails_before_creating_setup(self):
        self.app.budget_vault_feature=False
        result=self.request('/settings/content-factory/budget/vault-enable','POST',{'csrf':self.csrf,'api_key':'x'*32,'ack_scope':'understood','ack_estimate':'understood'})
        self.assertTrue(result['status'].startswith('503'))
        self.assertNotIn('content_factory_budget_gateway_setup',self.vault.present())

    def test_busy_stop_and_disconnect_report_not_performed(self):
        import content_factory_budget_vault as setup
        setup.enable(self.vault,'synthetic-budget-control-key-123456',confirm_scope=True,confirm_estimate=True,expected_vault_path=self.vault.path)
        with patch.object(self.app.budget_gateway,'owner_halt',side_effect=setup.GatewayBusyError()):
            result=self.request('/settings/content-factory/ai/gateway','POST',{'csrf':self.csrf,'action':'halt'})
        self.assertTrue(result['status'].startswith('503'))
        self.assertIn('остановка или включение не выполнены',result['body'])
        with patch.object(setup,'disable',side_effect=setup.GatewayBusyError()):
            result=self.request('/settings/content-factory/budget/vault-disable','POST',{'csrf':self.csrf})
        self.assertTrue(result['status'].startswith('503'))
        self.assertIn('остановка или изменение подключения не выполнены',result['body'])


if __name__=='__main__': unittest.main()
