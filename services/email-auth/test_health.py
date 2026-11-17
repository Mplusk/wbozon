"""Liveness regression tests: no DB, credentials or network needed."""
import io,json,os,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from portal import Portal

class HealthTests(unittest.TestCase):
 def setUp(self):
  self.login=SimpleNamespace(origin='https://3dprinta.shop',cookies=Mock(side_effect=AssertionError('must not inspect sessions')),connect=Mock(side_effect=AssertionError('must not open database')))
  self.vault=Mock();self.vault.path=os.path.join(tempfile.gettempdir(),'health-test-vault.sqlite')
  self.gateway=Mock(side_effect=AssertionError('must not call seller API'))
  self.external_keys=Mock()
  self.oauth=Mock()
  self.app=Portal(self.login,self.vault,'0'*64,self.gateway,external_key_store=self.external_keys,oauth_store=self.oauth)
 def request(self,method='GET',host='3dprinta.shop',cookie=''):
  stream=Mock();stream.read.side_effect=AssertionError('must not read request body')
  e={'PATH_INFO':'/healthz','REQUEST_METHOD':method,'HTTP_HOST':host,'HTTP_COOKIE':cookie,'wsgi.input':stream,'CONTENT_LENGTH':'999999'}
  result={}
  def start(status,headers):result.update(status=status,headers=dict(headers))
  result['body']=b''.join(self.app(e,start));stream.read.assert_not_called();return result
 def test_get_minimal_no_sensitive_metadata(self):
  r=self.request();self.assertEqual(r['status'],'200 OK');self.assertEqual(json.loads(r['body']),{'status':'ok'})
  self.assertEqual(r['headers']['Cache-Control'],'no-store');self.assertEqual(r['headers']['Content-Type'],'application/json');self.assertEqual(r['headers']['X-Content-Type-Options'],'nosniff')
  self.assertNotIn('Set-Cookie',r['headers']);self.assertNotIn('Location',r['headers'])
 def test_head_same_length_no_body(self):
  get=self.request();head=self.request('HEAD');self.assertEqual(head['status'],'200 OK');self.assertEqual(head['body'],b'');self.assertEqual(head['headers'],get['headers'])
 def test_writes_refused(self):
  for method in ('POST','PUT','PATCH','DELETE','OPTIONS'):
   with self.subTest(method=method):
    r=self.request(method);self.assertEqual(r['status'],'405 Method Not Allowed');self.assertEqual(r['headers']['Allow'],'GET, HEAD')
 def test_foreign_host_rejected(self):
  for host in ('evil.example','3dprinta.shop.evil.example','','3dprinta.shop:1234'):
   with self.subTest(host=host):self.assertEqual(self.request(host=host)['status'],'400 Bad Request')
 def test_cookie_never_changes_health_or_reads_session(self):
  self.assertEqual(self.request(cookie='__Host-sid='+'a'*43)['body'],b'{"status":"ok"}')
  self.login.cookies.assert_not_called();self.login.connect.assert_not_called();self.gateway.assert_not_called();self.assertEqual(self.vault.mock_calls,[]);self.assertEqual(self.external_keys.mock_calls,[]);self.assertEqual(self.oauth.mock_calls,[])

if __name__=='__main__':unittest.main()
