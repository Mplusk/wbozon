"""No live network. Test authenticated explicitly requested read-only preview."""
import sqlite3, unittest, urllib.error
import test_portal
from portal import render_preview

class PreviewTests(unittest.TestCase):
    setUp=test_portal.Tests.setUp
    tearDown=test_portal.Tests.tearDown
    request=test_portal.Tests.request
    save=test_portal.Tests.save
    def preview(self,provider='wb',**kw):return self.request('/settings/preview','POST',{'csrf':self.csrf,'provider':provider},**kw)
    def test_get_does_not_fetch(self):
        self.save();self.request();r=self.request('/settings/preview')
        self.assertTrue(r['status'].startswith('405'));self.assertFalse(self.calls)
    def test_auth_csrf_origin(self):
        self.save()
        self.assertTrue(self.preview(cookie=False,auth=self.token)['status'].startswith('401'))
        self.assertTrue(self.preview(origin=False)['status'].startswith('403'))
        self.csrf='x'*32;self.assertTrue(self.preview()['status'].startswith('403'));self.assertFalse(self.calls)
    def test_extra_parameters_rejected(self):
        self.save()
        self.assertTrue(self.request('/settings/preview','POST',{'provider':'wb','csrf':self.csrf,'limit':100})['status'].startswith('400'));self.assertFalse(self.calls)
    def test_empty_and_missing_key(self):
        self.assertTrue(self.preview()['status'].startswith('409'))
        self.now+=61;self.save();r=self.preview()
        self.assertTrue(r['status'].startswith('200'));self.assertIn('В первой странице ответа нет товаров.',r['body'])
        self.assertEqual(self.calls[0][:2],('wb_prices',{'limit':20}))
    def test_data_html_escaped_and_secrets_redacted(self):
        self.save()
        self.app.gateway=lambda *args:{'items':[{'nmID':1,'vendorCode':'<script>alert(1)</script>test-secret-value','sizes':[{'price':0,'techSizeName':'<img src=x onerror=alert(1)>'}]}]}
        r=self.preview();page=r['body']
        self.assertTrue(r['status'].startswith('200'));self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('test-secret-value',page);self.assertIn('[REDACTED]',page)
        self.assertNotIn('<img',page);self.assertIn('<dd>0</dd>',page)
        self.assertIn('не указано',page);self.assertIn('UTC',page);self.assertIn('не полный ассортимент',page)
        self.assertEqual(r['headers']['Cache-Control'],'no-store');self.assertIn("default-src 'none'",r['headers']['Content-Security-Policy'])
        self.assertIsNone(self.vault.check_status('wb'))
    def test_ozon_output_only_allowlisted_fields(self):
        self.request(method='POST',data={'provider':'ozon','csrf':self.csrf,'client_id':'123','api_key':'key-value'})
        self.app.gateway=lambda op,args,creds:{'items':[{'product_id':7,'offer_id':'article','archived':False,'is_fbo_visible':True,'internal_secret':'hidden'}]}
        page=self.preview('ozon')['body'];self.assertIn('article',page);self.assertNotIn('hidden',page)
        self.assertIn('Видимость FBO по API',page);self.assertIn('<dd>нет</dd>',page)
    def test_shared_rate_limit_with_checks(self):
        self.save();self.preview()
        self.assertTrue(self.preview()['status'].startswith('429'))
        self.assertTrue(self.request('/settings/check','POST',{'provider':'wb','csrf':self.csrf})['status'].startswith('429'))
        self.assertEqual(len(self.calls),1)
    def test_no_raw_upstream_errors(self):
        self.save()
        def gateway(*args):raise urllib.error.URLError('SECRET-API-KEY')
        self.app.gateway=gateway;r=self.preview()
        self.assertTrue(r['status'].startswith('502'));self.assertNotIn('SECRET',r['body'])
    def test_replacement_midflight_no_old_data(self):
        self.save()
        def gateway(*args):self.vault.save('wb',{'api_token':'new'});return {'items':[{'vendorCode':'old-private-product'}]}
        self.app.gateway=gateway;r=self.preview()
        self.assertTrue(r['status'].startswith('409'));self.assertNotIn('old-private-product',r['body'])
    def test_logout_midflight_no_data(self):
        self.save()
        def gateway(*args):
            with self.login.connect() as c:c.execute('DELETE FROM sessions')
            return {'items':[{'vendorCode':'private-product'}]}
        self.app.gateway=gateway;r=self.preview()
        self.assertTrue(r['status'].startswith('401'));self.assertNotIn('private-product',r['body'])
    def test_expiry_midflight_no_data(self):
        self.save()
        def gateway(*args):self.now+=43201;return {'items':[{'vendorCode':'private-product'}]}
        self.app.gateway=gateway;r=self.preview()
        self.assertTrue(r['status'].startswith('401'));self.assertNotIn('private-product',r['body'])
    def test_render_limits_and_bad_shape(self):
        page=render_preview('wb',{'items':[{'sizes':[{}]*100}]*100},0)
        self.assertEqual(page.count('<h2>Товар '),20)
        self.assertEqual(page.count('<dt>ID размера</dt>'),200)
        for rows in ('bad',[None],[{'sizes':'bad'}]):
            self.now+=61
            self.app.gateway=lambda *args:{'items':rows}
            if not self.vault.present():self.save()
            self.assertTrue(self.preview()['status'].startswith('502'))
    def test_unsaved_has_no_preview_button(self):
        self.assertNotIn('action="/settings/preview"',self.request()['body'])
        self.save();self.assertIn('action="/settings/preview"',self.request()['body'])

    def test_post_call_storage_failure_is_sanitized(self):
        self.save()
        def broken(*args):raise sqlite3.OperationalError('SECRET-PATH')
        self.vault.version_matches=broken
        result=self.preview()
        self.assertTrue(result['status'].startswith('503'));self.assertNotIn('SECRET',result['body'])

    def test_prices_require_nonnegative_finite_json_numbers(self):
        for value in (True,False,float('nan'),float('inf'),float('-inf'),-1,'12.34',None,{},[]):
            page=render_preview('wb',{'items':[{'sizes':[{'price':value,'discountedPrice':value,'clubDiscountedPrice':value}]}]},0)
            for label in ('Цена','Цена со скидкой','Цена для WB Клуба'):
                self.assertIn('<dt>'+label+'</dt><dd>не указано</dd>',page)
        for value in (0,12,12.34):
            page=render_preview('wb',{'items':[{'sizes':[{'price':value}]}]},0)
            self.assertIn('<dt>Цена</dt><dd>'+str(value)+'</dd>',page)

    def test_discounts_require_numeric_percentage(self):
        for value in (True,float('nan'),float('inf'),-1,101,'10'):
            page=render_preview('wb',{'items':[{'discount':value,'clubDiscount':value}]},0)
            self.assertIn('<dt>Скидка по API, %</dt><dd>не указано</dd>',page)
            self.assertIn('<dt>Скидка WB Клуба по API, %</dt><dd>не указано</dd>',page)
        for value in (0,10.5,100):
            page=render_preview('wb',{'items':[{'discount':value}]},0)
            self.assertIn('<dt>Скидка по API, %</dt><dd>'+str(value)+'</dd>',page)
