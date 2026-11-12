"""Manual offline Linux CI. No GitHub runner registration, credentials or deployment."""
import argparse,hashlib,json,os,pathlib,shutil,subprocess,sys,tempfile,time

DENY = '''import sys

def guard(event,args):
    if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.exec','os.posix_spawn'):
        raise RuntimeError('offline CI forbidden network/process action: '+event)
sys.addaudithook(guard)
'''
PIP_GUARD = '''import sys,runpy

def guard(event,args):
    if event.startswith('socket.'):
        raise RuntimeError('offline install forbids network: '+event)
sys.addaudithook(guard)
sys.argv=['pip',*sys.argv[1:]]
runpy.run_module('pip',run_name='__main__')
'''
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def manifest(root,path,expected):
 root=pathlib.Path(root).resolve(strict=True);path=pathlib.Path(path).absolute()
 if root.parts[1] in {'etc','opt','var','usr'} or path.resolve().parts[1] in {'etc','opt','var','usr'}:
  raise ValueError('production/system inputs forbidden; use a reviewed local source copy')
 if path.is_symlink():raise ValueError('manifest symlink forbidden')
 raw=path.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('manifest pin mismatch')
 entries={}
 for line in raw.decode().splitlines():
  digest,name=line.split('  ',1);parts=pathlib.PurePosixPath(name)
  if parts.is_absolute() or '..' in parts.parts or name in entries:raise ValueError('invalid manifest path')
  f=root/name
  if not f.is_file() or f.is_symlink() or any(p.is_symlink() for p in f.parents if p!=root.parent) or sha(f)!=digest:raise ValueError('source file mismatch')
  entries[name]=digest
 if not entries:raise ValueError('empty manifest')
 return root,entries

def copy_verified(source,destination,expected):
 shutil.copyfile(source,destination)
 if sha(destination)!=expected:raise ValueError('copied file identity mismatch; nothing will execute')

def environment(work):
 return {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(work/'home'),'TMPDIR':str(work/'tmp'),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
def command(argv,env,cwd=None,timeout=180):
 r=subprocess.run(argv,env=env,cwd=cwd,capture_output=True,text=True,timeout=timeout,check=False)
 if r.returncode:raise RuntimeError('offline command failed: '+r.stderr[-3000:])
 return r

def suite(python,folder,expected,env):
 code=DENY+'''
import unittest,json
suite=unittest.defaultTestLoader.discover('.',pattern='test_*.py')
count=suite.countTestCases()
if count!=int(sys.argv[1]):raise RuntimeError('unexpected test count: '+str(count))
result=unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful() or result.testsRun!=count or result.skipped or result.expectedFailures:sys.exit(1)
print(json.dumps({'tests':count,'success':True}))
'''
 result=command([str(python),'-c',code,str(expected)],env,folder)
 return json.loads(result.stdout.splitlines()[-1])

def run(args):
 if sys.version_info[:2]!=(3,12) or sys.platform!='linux':raise ValueError('Linux CPython3.12 required')
 portal,pfiles=manifest(args.portal_bundle,pathlib.Path(args.portal_bundle)/'SHA256SUMS',args.portal_manifest_sha256)
 native,nfiles=manifest(args.native_dir,args.native_manifest,args.native_manifest_sha256)
 required={'native_release.py','test_native_release.py'}
 if not required<=nfiles.keys():raise ValueError('native source manifest incomplete')
 for name in ('requirements-offline.lock','service/app.py','service/portal.py'):
  if name not in pfiles:raise ValueError('portal bundle incomplete')
 with tempfile.TemporaryDirectory(prefix='offline-portal-ci-') as temp:
  work=pathlib.Path(temp)
  for name in ('home','tmp','portal','native','wheels'):(work/name).mkdir()
  env=environment(work)
  for name in pfiles:
   if name.startswith('service/'):
    out=work/'portal'/name[len('service/'):];out.parent.mkdir(parents=True,exist_ok=True);copy_verified(portal/name,out,pfiles[name])
   elif name.startswith('wheelhouse/'):
    copy_verified(portal/name,work/'wheels'/pathlib.Path(name).name,pfiles[name])
  copy_verified(portal/'requirements-offline.lock',work/'requirements.lock',pfiles['requirements-offline.lock'])
  for name in required:copy_verified(native/name,work/'native'/name,nfiles[name])
  command([sys.executable,'-m','venv','--copies',str(work/'venv')],env)
  python=work/'venv/bin/python'
  command([str(python),'-c',PIP_GUARD,'install','--isolated','--no-index','--no-cache-dir','--no-compile','--only-binary=:all:','--find-links',str(work/'wheels'),'--require-hashes','-r',str(work/'requirements.lock')],env)
  portal_result=suite(python,work/'portal',59,env)
  native_result=suite(python,work/'native',46,env)
  # Assert the input manifests still match after test execution. No source modifications.
  manifest(portal,pathlib.Path(args.portal_bundle)/'SHA256SUMS',args.portal_manifest_sha256)
  manifest(native,args.native_manifest,args.native_manifest_sha256)
  return {'status':'PASS','portal':portal_result,'native_cd':native_result,'portal_manifest_sha256':args.portal_manifest_sha256,'native_manifest_sha256':args.native_manifest_sha256,'network':'Python audit denies socket events in install/tests; no OS network sandbox','production':'not accessed','systemd':'mocked in tests','limitations':'not GitHub CI success; no real HTTP or systemd activation test'}

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--portal-bundle',required=True);p.add_argument('--portal-manifest-sha256',required=True)
 p.add_argument('--native-dir',required=True);p.add_argument('--native-manifest',required=True);p.add_argument('--native-manifest-sha256',required=True)
 args=p.parse_args()
 try:print(json.dumps(run(args),indent=2))
 except Exception as e:print('FAIL: '+str(e),file=sys.stderr);return 1
 return 0
if __name__=='__main__':sys.exit(main())
