import base64,hashlib,re,unittest
from pathlib import Path
from ui import CSS,CSP,SCRIPT,document
import test_portal
class DesignTests(unittest.TestCase):
    def test_runtime_includes_styles_module(self):
        self.assertIn("gunicorn.conf.py ui.py",Path(__file__).with_name("Dockerfile").read_text())
    def test_csp_exact_static_css(self):
        expected=base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
        self.assertIn("style-src 'sha256-"+expected+"'",CSP)
        script_hash=base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-"+script_hash+"'",CSP)
        for forbidden in ('unsafe-inline','unsafe-eval','https:'): self.assertNotIn(forbidden,CSP)
        self.assertIn("default-src 'none'",CSP)
    def test_template_no_external_resources(self):
        page=document('<bad>','<main id="content">Test</main>')
        self.assertIn('&lt;bad&gt;',page)
        for word in ('<script','<iframe','@import','url('):self.assertNotIn(word,page)
        self.assertEqual(page.count('<style>'),1)
        self.assertIn('href="#content"',page)
    def test_responsive_rules(self):
        self.assertIn('@media(max-width:650px)',CSS)
        self.assertIn(':focus-visible',CSS)
        self.assertIn('min-height:44px',CSS)
    def test_settings_styles_forms_and_no_secrets(self):
        x=test_portal.Tests();x.setUp()
        try:
            r=x.request();self.assertEqual(r['headers']['Content-Security-Policy'],CSP)
            self.assertEqual(r['headers']['Referrer-Policy'],'strict-origin')
            self.assertIn('market-card ozon',r['body']);self.assertIn('market-card wb',r['body'])
            self.assertEqual(r['body'].count('action="/settings"'),3)
            self.assertIn('id="performance-client_secret"',r['body'])
            self.assertNotIn('unsafe-inline',r['headers']['Content-Security-Policy'])
            self.assertIn('label for="ozon-client_id"',r['body'])
            x.save();self.assertNotIn('test-secret-value',x.request()['body'])
            self.assertIn('<script>'+SCRIPT+'</script>',x.request()['body'])
        finally:x.tearDown()
    def test_login_label_and_policy(self):
        x=test_portal.Tests();x.setUp()
        try:
            r=x.request('/',cookie=False)
            self.assertIn('auth-layout',r['body']);self.assertIn('label for="login-email"',r['body'])
            self.assertEqual(r['headers']['Content-Security-Policy'],CSP)
            self.assertIn('action="/auth/request"',r['body'])
        finally:x.tearDown()

    def test_lookup_input_matches_shared_control_style(self):
        self.assertIn('input[type=text]{',CSS)
        x=test_portal.Tests();x.setUp()
        try:
            x.save()
            page=x.request()['body']
            self.assertIn('<input id="wb-lookup-id" type="text" name="nm_id"',page)
            self.assertIn('inputmode="numeric"',page)
            self.assertIn('maxlength="19"',page)
        finally:x.tearDown()
