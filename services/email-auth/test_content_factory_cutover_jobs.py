import os
import tempfile
import time
import unittest
from content_factory_cutover_jobs import Store, start


def fake_success(operation,owner,payload):
    return {'kind':'draft_preview','value':payload['value']}


def fake_slow(operation,owner,payload):
    time.sleep(5)
    return {'kind':'draft_preview'}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.tmp.name,'jobs.db')
        self.store=Store(self.path,b'k'*32); self.owner='authenticated-owner-identity'
    def tearDown(self): self.tmp.cleanup()

    def wait(self, ident, seconds=6):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            value=self.store.status(self.owner,ident)
            if value['state'] in ('completed','unknown'): return value
            time.sleep(.05)
        self.fail('Job did not finish')

    def test_spawned_job_is_owner_bound_and_payload_encrypted(self):
        secret='synthetic-secret-not-for-disk'
        ident=start(self.store,self.owner,'draft_prepare',{'value':secret},deadline=4,function=fake_success)
        with self.assertRaises(PermissionError): self.store.status('different-owner-identity',ident)
        value=self.wait(ident)
        self.assertEqual(value['state'],'completed')
        self.assertEqual(value['result']['value'],secret)
        with open(self.path,'rb') as f: self.assertNotIn(secret.encode(),f.read())

    def test_deadline_marks_unknown_and_blocks_new_jobs(self):
        ident=start(self.store,self.owner,'draft_prepare',{},deadline=1,function=fake_slow)
        result=self.wait(ident)
        self.assertEqual(result['state'],'unknown')
        with self.assertRaises(PermissionError): self.store.create(self.owner,'draft_prepare',{})

    def test_stale_job_becomes_unknown_without_retry(self):
        clock=[100]
        store=Store(self.path,b'k'*32,clock=lambda:clock[0])
        ident=store.create(self.owner,'draft_prepare',{})
        clock[0]+=131
        self.assertEqual(store.status(self.owner,ident)['state'],'unknown')
        with self.assertRaises(PermissionError): store.create(self.owner,'draft_prepare',{})


if __name__ == '__main__': unittest.main()
