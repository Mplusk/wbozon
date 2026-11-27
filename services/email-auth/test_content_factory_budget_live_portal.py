import re
import threading
import unittest
from unittest.mock import patch

import test_portal
from content_factory_n8n_budget import LiveBudgetGateway


class LivePortalTests(test_portal.Tests):
    def setUp(self):
        super().setUp()
        self.live = {'id':1,'enabled':True,'before_cents':1000,'updatedAt':'2026-11-27T10:00:00Z'}
        self.sent = []
        self.reads = 0
        self.ambiguous = False
        def transport(action, token, expected=None, after=None):
            if action == 'read': self.reads += 1; return dict(self.live)
            self.sent.append((dict(expected),after))
            if self.ambiguous: return 'unknown'
            if expected != self.live: return 'unknown'
            self.live['before_cents'] = after
            self.live['updatedAt'] = '2026-11-27T10:01:00Z'
            return 'verified'
        self.gateway = LiveBudgetGateway(self.vault.path,b'k'*32,token_reader=lambda:'x'*32,transport=transport)
        self.gateway.fence.arm(lambda:True)
        self.app.budget_gateway = self.gateway

    def test_refresh_requires_auth_origin_csrf_and_recent_session(self):
        path='/settings/content-factory/budget/refresh'; form={'csrf':self.csrf}
        with patch('content_factory.snapshot',return_value={'status':'unavailable'}):
            page=self.request('/settings',headers={'QUERY_STRING':'tab=content-factory'})
        self.assertIn('Текущий бюджет n8n ещё не проверен',page['body'])
        self.assertEqual(self.reads,0)
        self.assertTrue(self.request(path,'POST',form,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path,'POST',form,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',{'csrf':'bad'})['status'].startswith('403'))
        self.assertEqual(self.reads,0)
        self.assertTrue(self.request(path,'POST',form)['status'].startswith('303'))
        self.assertEqual(self.reads,1)
        with patch('content_factory.snapshot',return_value={'status':'unavailable'}):
            page=self.request('/settings',headers={'QUERY_STRING':'tab=content-factory'})
        self.assertIn('может устареть',page['body'])
        self.assertEqual(self.reads,1)
        self.now += 301
        self.assertTrue(self.request(path,'POST',form)['status'].startswith('403'))
        self.assertEqual(self.reads,1)

    def test_refresh_is_rate_limited_before_network(self):
        path='/settings/content-factory/budget/refresh'; form={'csrf':self.csrf}
        for _ in range(10): self.assertTrue(self.request(path,'POST',form)['status'].startswith('303'))
        self.assertTrue(self.request(path,'POST',form)['status'].startswith('429'))
        self.assertEqual(self.reads,10)

    def test_live_gateway_uses_owner_confirmation_and_honest_messages(self):
        path = '/settings/content-factory/budget/prepare'
        data = {'csrf':self.csrf,'revision':'1','monthly_limit_usd':'12.34'}
        self.gateway.refresh()
        self.assertTrue(self.request(path,'POST',data,cookie=False)['status'].startswith('401'))
        self.assertTrue(self.request(path,'POST',data,origin=False)['status'].startswith('403'))
        self.assertTrue(self.request(path,'POST',{**data,'csrf':'wrong'})['status'].startswith('403'))
        self.assertEqual(self.sent,[])
        with patch('content_factory.snapshot',return_value={'status':'unavailable'}):
            page = self.request('/settings',headers={'QUERY_STRING':'tab=content-factory'})
        self.assertIn('оценочный месячный лимит n8n',page['body'])
        self.assertIn('может устареть',page['body'])
        prepared = self.request(path,'POST',data)
        self.assertIn('Оценочный лимит n8n: 10.00 → 12.34 USD',prepared['body'])
        self.assertEqual(self.sent,[])
        proposal = re.search(r'name="proposal_id" value="([A-Za-z0-9_-]+)"',prepared['body']).group(1)
        result = self.request('/settings/content-factory/budget/confirm','POST',{'csrf':self.csrf,'proposal_id':proposal})
        self.assertTrue(result['status'].startswith('200'))
        self.assertIn('Оценочный лимит n8n изменён',result['body'])
        self.assertEqual(self.live['before_cents'],1234)
        self.assertEqual(len(self.sent),1)
        with patch('content_factory.snapshot',return_value={'status':'unavailable'}):
            page=self.request('/settings',headers={'QUERY_STRING':'tab=content-factory'})
        self.assertIn('Текущий бюджет n8n ещё не проверен',page['body'])
        self.assertTrue(self.request('/settings/content-factory/budget/confirm','POST',{'csrf':self.csrf,'proposal_id':proposal})['status'].startswith('503'))

    def test_unknown_is_reported_and_blocks_repeated_write(self):
        self.gateway.refresh(); self.ambiguous=True
        path='/settings/content-factory/budget/prepare'
        data={'csrf':self.csrf,'revision':'1','monthly_limit_usd':'15.00'}
        result=self.request(path,'POST',data)
        proposal=re.search(r'name="proposal_id" value="([A-Za-z0-9_-]+)"',result['body']).group(1)
        result=self.request('/settings/content-factory/budget/confirm','POST',{'csrf':self.csrf,'proposal_id':proposal})
        self.assertTrue(result['status'].startswith('503'))
        self.assertIn('Результат сохранения неизвестен',result['body'])
        self.assertFalse(self.gateway.ready())
        self.assertTrue(self.request('/settings/content-factory/budget/confirm','POST',{'csrf':self.csrf,'proposal_id':proposal})['status'].startswith('503'))
        self.assertEqual(len(self.sent),1)


if __name__ == '__main__': unittest.main()
