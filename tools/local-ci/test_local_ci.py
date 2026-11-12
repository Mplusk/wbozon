import os,pathlib,subprocess,sys,tempfile,unittest
from unittest.mock import patch
import run_local_ci as ci

class Tests(unittest.TestCase):
 def fixture(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);p=pathlib.Path(t.name)
  (p/'code.py').write_text('reviewed');m=p/'SHA256SUMS';m.write_text(ci.sha(p/'code.py')+'  code.py\n')
  return p,m,ci.sha(m)
 def test_changed_copy_refused_before_execution(self):
  p,m,h=self.fixture();source=p/'code.py';destination=p/'copy.py';expected=ci.sha(source)
  realcopy=ci.shutil.copyfile
  def mutate(src,dst):
   realcopy(src,dst);pathlib.Path(dst).write_text('changed during transfer')
  with patch.object(ci.shutil,'copyfile',side_effect=mutate):
   with self.assertRaises(ValueError):ci.copy_verified(source,destination,expected)
  self.assertEqual(source.read_text(),'reviewed')
 def test_skipped_test_cannot_report_pass(self):
  p,m,h=self.fixture();(p/'test_skip.py').write_text('import unittest\nclass T(unittest.TestCase):\n @unittest.skip("not executed")\n def test_ok(self):pass\n')
  with self.assertRaises(RuntimeError):ci.suite(pathlib.Path(sys.executable),p,1,ci.environment(p))
 def test_valid_manifest(self):
  p,m,h=self.fixture();self.assertEqual(set(ci.manifest(p,m,h)[1]),{'code.py'})
 def test_wrong_manifest_pin(self):
  p,m,h=self.fixture()
  with self.assertRaises(ValueError):ci.manifest(p,m,'0'*64)
 def test_tamper_rejected(self):
  p,m,h=self.fixture();(p/'code.py').write_text('changed')
  with self.assertRaises(ValueError):ci.manifest(p,m,h)
 def test_symlink_rejected(self):
  p,m,h=self.fixture();f=p/'code.py';f.rename(p/'other');f.symlink_to('other')
  with self.assertRaises(ValueError):ci.manifest(p,m,h)
 def test_traversal_rejected(self):
  p,m,h=self.fixture();m.write_text('0'*64+'  ../outside\n')
  with self.assertRaises(ValueError):ci.manifest(p,m,ci.sha(m))
 def test_environment_does_not_inherit(self):
  with patch.dict(os.environ,{'SMTP_PASSWORD':'synthetic','PYTHONPATH':'untrusted','HTTP_PROXY':'bad'}):
   env=ci.environment(pathlib.Path('/tmp/test'))
  self.assertNotIn('SMTP_PASSWORD',env);self.assertNotIn('PYTHONPATH',env);self.assertNotIn('HTTP_PROXY',env)
 def blocked(self,code,guard=ci.DENY):
  r=subprocess.run([sys.executable,'-c',guard+code],capture_output=True,text=True,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
  self.assertNotEqual(r.returncode,0);self.assertIn('offline',r.stderr)
 def test_network_socket_blocked_before_connect(self):self.blocked("\nimport socket; socket.socket()")
 def test_dns_blocked(self):self.blocked("\nimport socket; socket.getaddrinfo('example.com',443)")
 def test_external_process_blocked(self):self.blocked("\nimport subprocess; subprocess.run(['/bin/true'])")
 def test_shell_blocked(self):self.blocked("\nimport os; os.system('true')")
 def test_unexpected_count_refused(self):
  p,m,h=self.fixture();(p/'test_example.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self):pass\n')
  with self.assertRaises(RuntimeError):ci.suite(pathlib.Path(sys.executable),p,59,ci.environment(p))
 def test_subprocess_timeout_fails(self):
  with patch.object(ci.subprocess,'run',side_effect=subprocess.TimeoutExpired('test',180)):
   with self.assertRaises(subprocess.TimeoutExpired):ci.command(['test'],{})
 def test_system_source_refused(self):
  with self.assertRaises(ValueError):ci.manifest('/etc','/etc/hosts','0'*64)

if __name__=='__main__':unittest.main()
