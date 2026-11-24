import io, os, re, smtplib, ssl, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlsplit
from app import LoginApp, MailDeliveryUncertain, send_login_email

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
  self.app.mailer=fail
  with self.assertLogs('app',level='WARNING') as logs:r=self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'})
  self.assertNotIn('secret',r['body']);self.assertNotIn('secret',' '.join(logs.output))
  with self.app.connect() as c:self.assertEqual(c.execute('select count(*) from tokens').fetchone()[0],0)
 def test_uncertain_delivery_preserves_link_without_logging_secrets(self):
  def uncertain(*args):raise MailDeliveryUncertain('secret')
  self.app.mailer=uncertain
  with self.assertLogs('app',level='WARNING') as logs:r=self.call('/auth/request','POST',{'email':'maksim@kuchermg.ru'})
  self.assertNotIn('secret',r['body']);self.assertNotIn('secret',' '.join(logs.output))
  with self.app.connect() as c:self.assertEqual(c.execute('select count(*) from tokens').fetchone()[0],1)
 def test_invalid_config(self):
  with self.assertRaises(ValueError):LoginApp(self.tmp.name+'/other','x'*40,'http://example.com')
 def test_oversize_and_many_fields(self):
  self.assertTrue(self.call('/auth/request','POST',{'email':'a'*5000})['status'].startswith('413'))
  self.assertTrue(self.call('/auth/request','POST',{str(i):'x' for i in range(9)})['status'].startswith('400'))
 def test_invalid_token_and_unknown_path(self):
  self.assertTrue(self.call('/auth/link?token=%3Cscript%3E')['status'].startswith('400'));self.assertTrue(self.call('/unknown')['status'].startswith('404'))

class SMTPMailerTests(unittest.TestCase):
 def setUp(self):
  self.sender='no-reply@example.test';self.recipient='owner@example.test';self.link='https://example.test/synthetic'
 def send(self):send_login_email(self.recipient,self.link,self.sender,'synthetic-password')
 def test_retry_transient_login_before_sending_only_once(self):
  first=Mock();first.login.side_effect=TimeoutError('synthetic-secret')
  second=Mock();second.send_message.return_value={}
  with patch('app.smtplib.SMTP_SSL',side_effect=[first,second]) as factory, patch('app.time.sleep') as delay:
   self.send()
  self.assertEqual(factory.call_count,2);delay.assert_called_once_with(0.5)
  first.send_message.assert_not_called();first.close.assert_called_once()
  second.login.assert_called_once_with(self.sender,'synthetic-password');second.send_message.assert_called_once();second.close.assert_called_once()
  message=second.send_message.call_args.args[0]
  self.assertEqual(message['From'],self.sender);self.assertEqual(message['To'],self.recipient)
  self.assertIn(self.link,message.get_content())
 def test_authentication_failure_is_not_retried(self):
  smtp=Mock();smtp.login.side_effect=smtplib.SMTPAuthenticationError(535,b'synthetic-secret')
  with patch('app.smtplib.SMTP_SSL',return_value=smtp) as factory, self.assertRaises(smtplib.SMTPAuthenticationError):self.send()
  self.assertEqual(factory.call_count,1);smtp.send_message.assert_not_called();smtp.close.assert_called_once()
 def test_invalid_certificate_is_not_retried(self):
  with patch('app.smtplib.SMTP_SSL',side_effect=ssl.SSLCertVerificationError('synthetic')) as factory, self.assertRaises(ssl.SSLCertVerificationError):self.send()
  self.assertEqual(factory.call_count,1)
 def test_retry_is_bounded(self):
  with patch('app.smtplib.SMTP_SSL',side_effect=TimeoutError('synthetic-secret')) as factory, patch('app.time.sleep') as delay, self.assertRaises(TimeoutError):self.send()
  self.assertEqual(factory.call_count,2);delay.assert_called_once()
 def test_uncertain_submission_is_not_retried(self):
  smtp=Mock();smtp.send_message.side_effect=TimeoutError('synthetic-secret')
  with patch('app.smtplib.SMTP_SSL',return_value=smtp) as factory, self.assertRaises(MailDeliveryUncertain):self.send()
  self.assertEqual(factory.call_count,1);smtp.send_message.assert_called_once();smtp.close.assert_called_once()
 def test_explicit_rejection_is_not_uncertain(self):
  smtp=Mock();smtp.send_message.side_effect=smtplib.SMTPDataError(550,b'synthetic-secret')
  with patch('app.smtplib.SMTP_SSL',return_value=smtp), self.assertRaises(smtplib.SMTPDataError):self.send()
  smtp.send_message.assert_called_once();smtp.close.assert_called_once()
 def test_close_failure_does_not_undo_an_accepted_email(self):
  smtp=Mock();smtp.send_message.return_value={};smtp.close.side_effect=RuntimeError('synthetic-secret')
  with patch('app.smtplib.SMTP_SSL',return_value=smtp):self.send()
  smtp.send_message.assert_called_once()

if __name__=='__main__':unittest.main()
