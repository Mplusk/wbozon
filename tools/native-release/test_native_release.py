import hashlib,json,pathlib,tempfile,unittest
from unittest.mock import patch
import native_release as n

class ReleaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=pathlib.Path(self.tmp.name)/'app';self.root.mkdir();(self.root/'releases').mkdir()
  self.state=pathlib.Path(self.tmp.name)/'state';self.state.mkdir()
  self.hashes={}
  for name in ('old','new'):
   r=self.root/'releases'/name;r.mkdir();files={}
   for f in ('service/portal.py','service/gunicorn.conf.py','venv/bin/gunicorn','venv/bin/python'):
    p=r/f;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name);files[f]=hashlib.sha256(name.encode()).hexdigest()
   raw=json.dumps(files).encode();(r/'MANIFEST.json').write_bytes(raw);self.hashes[name]=hashlib.sha256(raw).hexdigest()
  (self.root/'current').symlink_to('releases/old')
  self.calls=[];self.fail=None;self.healthfail=False
  self.c=n.Controller(self.root,self.state,self.fake_run,self.health)
  self.sec=patch.object(n,'secure',side_effect=lambda p,*_:pathlib.Path(p));self.sec.start();self.addCleanup(self.sec.stop)
  self.sleep=patch.object(n.time,'sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
  self.unit_hash=hashlib.sha256(b'approved unit').hexdigest()
 def fake_run(self,*args):
  self.calls.append(args)
  if args==self.fail: raise n.Refused('uncertain')
  if args[0]=='cat':return 'approved unit'
  if args[0]=='show':
   if '--value' in args:return 'inactive\n'
   return f'NeedDaemonReload=no\nWorkingDirectory={self.root}/current/service\nExecStart={self.root}/current/venv/bin/gunicorn'
  return ''
 def health(self):
  if self.healthfail:raise OSError('not ready')
 def deploy(self):self.c.deploy('new',self.hashes['new'],self.hashes['old'],self.unit_hash)
 def test_success(self):
  self.deploy();self.assertEqual(self.c.current(),'new');self.assertFalse((self.state/'pending.json').exists());self.assertTrue((self.state/'last.json').exists())
 def test_timeout_stop_retains_journal_without_start(self):
  self.fail=('stop',)
  with self.assertRaises(n.Refused):self.deploy()
  self.assertEqual(self.c.current(),'old');self.assertTrue((self.state/'pending.json').exists());self.assertNotIn(('start',),self.calls)
 def test_timeout_start_does_not_rollback(self):
  self.fail=('start',)
  with self.assertRaises(n.Refused):self.deploy()
  self.assertEqual(self.c.current(),'new');self.assertTrue((self.state/'pending.json').exists());self.assertEqual(self.calls.count(('stop',)),1)
 def test_health_failure_keeps_intent(self):
  self.healthfail=True
  with self.assertRaises(n.Refused):self.deploy()
  self.assertTrue((self.state/'pending.json').exists())
 def test_pending_blocks_retry(self):
  (self.state/'pending.json').write_text('{}')
  with self.assertRaises(n.Refused):self.deploy()
  self.assertEqual(self.calls,[])
 def test_unit_mismatch_no_stop(self):
  self.unit_hash='0'*64
  with self.assertRaises(n.Refused):self.deploy()
  self.assertNotIn(('stop',),self.calls)
 def test_tampered_release_no_stop(self):
  (self.root/'releases/new/service/portal.py').write_text('bad')
  with self.assertRaises(n.Refused):self.deploy()
  self.assertEqual(self.calls,[])
 def test_extra_file_refused(self):
  (self.root/'releases/new/extra').write_text('bad')
  with self.assertRaises(n.Refused):self.deploy()
 def test_manifest_hash_mismatch(self):
  self.hashes['new']='0'*64
  with self.assertRaises(n.Refused):self.deploy()
 def test_recover_old_after_health_fail(self):
  self.healthfail=True
  with self.assertRaises(n.Refused):self.deploy()
  self.healthfail=False;self.c.recover();self.assertEqual(self.c.current(),'old');self.assertFalse((self.state/'pending.json').exists())
 def test_rollback_as_reverse_release(self):
  self.deploy();self.c.deploy('old',self.hashes['old'],self.hashes['new'],self.unit_hash);self.assertEqual(self.c.current(),'old')
 def test_foreign_current_refused(self):
  (self.root/'current').unlink();(self.root/'current').symlink_to(self.state)
  with self.assertRaises(n.Refused):self.deploy()
 def test_stale_switch_link_fail_closed(self):
  (self.root/'current.pending').symlink_to('releases/old')
  with self.assertRaises(n.Refused):self.deploy()
  self.assertEqual(self.c.current(),'old');self.assertTrue((self.state/'pending.json').exists())
 def test_lock_excludes_second_operator(self):
  with n.lock(self.state):
   with self.assertRaises(BlockingIOError):
    with n.lock(self.state):pass
 def test_readiness_retries(self):
  calls=[]
  def delayed():
   calls.append(1)
   if len(calls)<3:raise ConnectionRefusedError()
  self.c.health=delayed;self.deploy();self.assertEqual(len(calls),3)
 def test_jobs_block_deploy(self):
  original=self.c.runner
  self.c.runner=lambda *args:'1 start running' if args[0]=='list-jobs' else original(*args)
  with self.assertRaises(n.Refused):self.deploy()
  self.assertNotIn(('stop',),self.calls)
 def test_pending_preserved_on_base_exception_after_intent(self):
  original=self.c.runner
  def killed(*args):
   if args[0]=='stop':raise KeyboardInterrupt()
   return original(*args)
  self.c.runner=killed
  with self.assertRaises(KeyboardInterrupt):self.deploy()
  self.assertTrue((self.state/'pending.json').exists())
 def test_recover_crash_between_symlink_and_replace(self):
  original_replace=n.os.replace
  def killed(src,dst):
   if pathlib.Path(dst)==self.root/'current':raise KeyboardInterrupt()
   return original_replace(src,dst)
  with patch.object(n.os,'replace',side_effect=killed):
   with self.assertRaises(KeyboardInterrupt):self.deploy()
  self.assertTrue((self.root/'current.pending').is_symlink())
  original_stat=pathlib.Path.lstat
  def root_stat(p):
   s=original_stat(p)
   values=list(s);values[4]=0
   return n.os.stat_result(values)
  with patch.object(pathlib.Path,'lstat',autospec=True,side_effect=root_stat):self.c.recover()
  self.assertEqual(self.c.current(),'old');self.assertFalse((self.state/'pending.json').exists())
 def test_foreign_pending_symlink_not_removed(self):
  self.healthfail=True
  with self.assertRaises(n.Refused):self.deploy()
  self.healthfail=False;(self.root/'current.pending').symlink_to('/unrelated')
  with self.assertRaises(n.Refused):self.c.recover()
  self.assertEqual(n.os.readlink(self.root/'current.pending'),'/unrelated')
 def test_manifest_traversal_refused_by_inventory(self):
  p=self.root/'releases/new/MANIFEST.json';p.write_text('{"../../bad":"'+'0'*64+'"}');self.hashes['new']=hashlib.sha256(p.read_bytes()).hexdigest()
  with self.assertRaises(n.Refused):self.deploy()

class CommandTests(unittest.TestCase):
 def test_timeout_sanitized(self):
  import subprocess
  with patch.object(n.subprocess,'run',side_effect=subprocess.TimeoutExpired('secret',60)):
   with self.assertRaises(n.Refused) as caught:n.Controller.command('stop')
  self.assertNotIn('secret',str(caught.exception))
 def test_no_other_unit(self):
  with patch.object(n.subprocess,'run',return_value=type('R',(),{'returncode':0,'stdout':''})()) as run:
   n.Controller.command('stop')
  self.assertEqual(run.call_args.args[0],['/usr/bin/systemctl','stop','3dprinta-portal.service'])
 def test_redirect_refused(self):
  with self.assertRaises(n.Refused):n.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')
 def test_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d)/'link';p.symlink_to('/etc')
   with self.assertRaises(n.Refused):n.secure(p)
 def test_secure_rejects_fifo_after_trusted_parents(self):
  import stat
  good=type('S',(),{'st_mode':stat.S_IFDIR|0o755,'st_uid':0})()
  bad=type('S',(),{'st_mode':stat.S_IFIFO|0o600,'st_uid':0})()
  with patch.object(pathlib.Path,'lstat',autospec=True,side_effect=lambda p:bad if str(p)=='/trusted/file' else good):
   with self.assertRaises(n.Refused):n.secure('/trusted/file')
 def test_secure_rejects_group_writable_leaf(self):
  import stat
  good=type('S',(),{'st_mode':stat.S_IFDIR|0o755,'st_uid':0})()
  bad=type('S',(),{'st_mode':stat.S_IFREG|0o664,'st_uid':0})()
  with patch.object(pathlib.Path,'lstat',autospec=True,side_effect=lambda p:bad if str(p)=='/trusted/file' else good):
   with self.assertRaises(n.Refused):n.secure('/trusted/file')
 def test_secure_rejects_symlink_leaf_after_trusted_parents(self):
  import stat
  good=type('S',(),{'st_mode':stat.S_IFDIR|0o755,'st_uid':0})()
  bad=type('S',(),{'st_mode':stat.S_IFLNK|0o777,'st_uid':0})()
  with patch.object(pathlib.Path,'lstat',autospec=True,side_effect=lambda p:bad if str(p)=='/trusted/file' else good):
   with self.assertRaises(n.Refused):n.secure('/trusted/file')
 def test_world_writable_parent_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(n.Refused):n.secure(pathlib.Path(d))

if __name__=='__main__':unittest.main()
