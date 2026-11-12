import io, os, re, tempfile, unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from app import LoginApp

class AuthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.now=100000; self.sent=[]
  self.app=LoginApp(self.tmp.name+'/db','x'*40,'https://3dprinta.shop',mailer=lambda recipient,link:self.sent.append((recipient,link)),clock=lambda:self.now)
 def tearDown(self):self.tmp.cleanup()
 def call(self,path='/',method='GET',data=None,cookies='',origin='https://3dprinta.shop',host='3dprinta.shop'):
  u=urlsplit(path); raw=urlencode(data or {}).encode(); result={}
  e={'HTTP_HOST':host,'PATH_INFO':u.path,'QUERY_STRING':u.query,'REQUEST_METHOD':method,'HTTP_ORIGIN':origin,'CONTENT_TYPE':'application/x-www-form-urlencoded','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw),'REMOTE_ADDR':'127.0.0.1','HTTP_COOKIE':cookies}
  def start(status,headers):result.update(status=status,headers=headers)
  result['body']=b''.join(self.app(e,start)).decode();return result
 def request(self):
  self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'});return self.sent[-1][1]
 def landing(self):
  link=self.request(); r=self.call(link); cookie=next(v.split(';')[0] for k,v in r['headers'] if k=='Set-Cookie'); csrf=re.search('name="csrf" value="([^"]+)',r['body'])[1]; token=urlsplit(link).query.split('=')[1];return {'csrf':csrf,'token':token},cookie
 def test_allowlist(self):
  a=self.call('/auth/request','POST',{'email':'outsider@example.com'});b=self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'});self.assertEqual(a['body'],b['body']);self.assertEqual(len(self.sent),1)
 def test_no_raw_tokens_in_database(self):
  link=self.request();token=link.split('=')[1]
  self.assertNotIn(token,Path(self.tmp.name+'/db').read_bytes().decode(errors='ignore'))
 def test_scanner_get_does_not_consume(self):
  data,cookie=self.landing()
  with self.app.connect() as c:self.assertEqual(c.execute('select count(*) from tokens').fetchone()[0],1);self.assertEqual(c.execute('select count(*) from sessions').fetchone()[0],0)
  r=self.call('/auth/confirm','POST',data,cookie);self.assertTrue(r['status'].startswith('200'))
 def test_one_time(self):
  data,cookie=self.landing();self.call('/auth/confirm','POST',data,cookie);self.assertTrue(self.call('/auth/confirm','POST',data,cookie)['status'].startswith('400'))
 def test_concurrent_consumption(self):
  from concurrent.futures import ThreadPoolExecutor
  data,cookie=self.landing()
  with ThreadPoolExecutor(max_workers=2) as pool:
   results=list(pool.map(lambda _:self.call('/auth/confirm','POST',data,cookie)['status'],range(2)))
  self.assertEqual(sum(v.startswith('200') for v in results),1)
  self.assertEqual(sum(v.startswith('400') for v in results),1)
 def test_expired(self):
  data,cookie=self.landing();self.now+=901;self.assertTrue(self.call('/auth/confirm','POST',data,cookie)['status'].startswith('400'))
 def test_csrf(self):
  data,cookie=self.landing();self.assertTrue(self.call('/auth/confirm','POST',data)['status'].startswith('403'));self.assertTrue(self.call('/auth/confirm','POST',data,cookie,origin='https://evil.example')['status'].startswith('403'))
 def test_confirm_rejects_unicode_and_invalid_lengths(self):
  data,cookie=self.landing()
  for nonce in ['☃', 'é'*32, '', 'a'*31, 'a'*33, 'a.'*16]:
   bad=dict(data,csrf=nonce)
   self.assertTrue(self.call('/auth/confirm','POST',bad,cookie)['status'].startswith('403'))
  for token in ['é'*43, 'a'*42, 'a'*44]:
   self.assertTrue(self.call('/auth/confirm','POST',dict(data,token=token),cookie)['status'].startswith('403'))
  self.assertTrue(self.call('/auth/confirm','POST',data,'__Host-login="☃"')['status'].startswith('403'))
 def test_logout_rejects_unicode_and_invalid_lengths(self):
  data,cookie=self.landing();r=self.call('/auth/confirm','POST',data,cookie)
  sid=next(v.split(';')[0] for k,v in r['headers'] if k=='Set-Cookie' and v.startswith('__Host-sid='))
  for csrf in ['☃','é'*32,'','a'*31,'a'*33]:
   self.assertTrue(self.call('/logout','POST',{'csrf':csrf},sid)['status'].startswith('403'))
  self.assertIn('Вход выполнен',self.call(cookies=sid)['body'])
 def test_bad_host(self):self.assertTrue(self.call(host='evil.example')['status'].startswith('400'))
 def test_rate_limit(self):
  for _ in range(8):self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'})
  self.assertEqual(len(self.sent),5)
 def test_session_secure_and_logout(self):
  data,cookie=self.landing();r=self.call('/auth/confirm','POST',data,cookie);header=next(v for k,v in r['headers'] if k=='Set-Cookie' and v.startswith('__Host-sid='));self.assertIn('Secure; HttpOnly; SameSite=Strict',header)
  sid=header.split(';')[0];r=self.call(cookies=sid);self.assertIn('Вход выполнен',r['body']);csrf=re.search('name="csrf" value="([^"]+)',r['body'])[1]
  self.assertTrue(self.call('/logout','POST',{'csrf':'bad'},sid)['status'].startswith('403'))
  self.call('/logout','POST',{'csrf':csrf},sid);self.assertNotIn('Вход выполнен',self.call(cookies=sid)['body'])
 def test_expired_session(self):
  data,cookie=self.landing();r=self.call('/auth/confirm','POST',data,cookie);sid=next(v.split(';')[0] for k,v in r['headers'] if k=='Set-Cookie' and v.startswith('__Host-sid='));self.now+=43201;self.assertNotIn('Вход выполнен',self.call(cookies=sid)['body'])
 def test_mail_failure_removes_token(self):
  def fail(*args):raise RuntimeError('secret')
  self.app.mailer=fail;r=self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'});self.assertNotIn('secret',r['body'])
  with self.app.connect() as c:self.assertEqual(c.execute('select count(*) from tokens').fetchone()[0],0)
 def test_invalid_config(self):
  with self.assertRaises(ValueError):LoginApp(self.tmp.name+'/other','x'*40,'http://example.com')
 def test_oversize_and_many_fields(self):
  self.assertTrue(self.call('/auth/request','POST',{'email':'a'*5000})['status'].startswith('413'))
  self.assertTrue(self.call('/auth/request','POST',{str(i):'x' for i in range(9)})['status'].startswith('400'))
 def test_invalid_token_and_unknown_path(self):
  self.assertTrue(self.call('/auth/link?token=%3Cscript%3E')['status'].startswith('400'));self.assertTrue(self.call('/unknown')['status'].startswith('404'))

if __name__=='__main__':unittest.main()
