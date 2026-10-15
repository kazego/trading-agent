"""Direct Windows synthetic test; retains new fixtures, no operational access."""
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from research.incremental_snapshot import (capture_sealed, verify_sealed, validate_manifest,
                                           CaptureRejected)
from research.spec import encoded, fingerprint
from test_incremental_snapshot import new_directory, envelope, append


def reopen(path, anchor, output):
    try:output.send(('PASS',verify_sealed(Path(path),anchor)))
    except BaseException as exc:output.send(('FAIL',repr(exc)))
    finally:output.close()


class ManifestTests(unittest.TestCase):
    def test_exact_and_mutated_manifest(self):
        anchor=envelope(348)
        m=dict(version='synthetic-snapshot-v1',snapshot_id='test',sequence=348,
               head=anchor['state']['head'],state_hash=fingerprint(anchor),
               file_sha256='a'*64,file_size=4096,captured_at_ns=123,anchor=anchor)
        seal=dict(version='snapshot-seal-v1',manifest_hash=fingerprint(m))
        validate_manifest(m,seal,anchor,'test')
        for key,value in [('sequence',349),('head','bad'),('state_hash','bad'),
                          ('snapshot_id','foreign'),('captured_at_ns',1.5)]:
            changed=dict(m,**{key:value})
            with self.subTest(key=key),self.assertRaises((CaptureRejected,ValueError)):
                validate_manifest(changed,seal,anchor,'test')
        with self.assertRaises(CaptureRejected):validate_manifest(m,seal,envelope(347),'test')
        with self.assertRaises(CaptureRejected):validate_manifest(m,{},anchor,'test')

    def test_rehashed_invalid_fields_still_rejected(self):
        anchor=envelope(348)
        base=dict(version='synthetic-snapshot-v1',snapshot_id='test',sequence=348,
                  head=anchor['state']['head'],state_hash=fingerprint(anchor),
                  file_sha256='a'*64,file_size=4096,captured_at_ns=123,anchor=anchor)
        for key,value in [('sequence',349),('head','wrong'),('state_hash','wrong'),
                          ('snapshot_id','foreign'),('file_size',-1),('captured_at_ns',True),
                          ('file_sha256','invalid')]:
            m=dict(base,**{key:value})
            seal=dict(version='snapshot-seal-v1',manifest_hash=fingerprint(m))
            with self.subTest(key=key),self.assertRaises(CaptureRejected):
                validate_manifest(m,seal,anchor,'test')


class DurableSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root=new_directory()
        path=self.root/'source.sqlite'
        with path.open('xb'):pass
        self.db=sqlite3.connect(path)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('CREATE TABLE snapshot_fixture(kind TEXT);'
                             "INSERT INTO snapshot_fixture VALUES ('synthetic-snapshot-v1');"
                             'CREATE TABLE journal(sequence INTEGER PRIMARY KEY,digest TEXT);'
                             'CREATE TABLE incremental_state(payload TEXT);'
                             "INSERT INTO incremental_state VALUES ('{}');")
        for n in range(1,349):append(self.db,n)
        self.db.commit()
        self.reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        self.reader.execute('PRAGMA query_only=ON')
        self.addCleanup(self.reader.close)

    def make(self,**kwargs):
        return capture_sealed(self.reader,threading.Lock(),lambda:envelope(348),self.root,**kwargs)

    def test_reopen_in_second_process_and_measure(self):
        path,manifest,metrics=self.make()
        ctx=multiprocessing.get_context('spawn');receive,send=ctx.Pipe(duplex=False)
        child=ctx.Process(target=reopen,args=(str(path),envelope(348),send))
        try:
            child.start();send.close()
            self.assertTrue(receive.poll(20),'Reopen timeout')
            self.assertEqual(receive.recv(),('PASS',manifest))
            child.join(3);self.assertEqual(child.exitcode,0)
        finally:
            if child.pid is not None and child.is_alive():child.terminate();child.join(3)
            receive.close();send.close()
        with (self.root/'measurements.json').open('x',encoding='utf-8') as f:
            json.dump(dict(events=348,metrics=metrics,bytes=manifest['file_size']),f,sort_keys=True)
        print('SEALED_SNAPSHOT_METRICS',json.dumps(metrics,sort_keys=True),flush=True)

    def test_interruption_stages(self):
        for stage in ('after_capture','after_sqlite','after_manifest','after_seal'):
            before=set(self.root.iterdir())
            def fail(point):
                if point==stage:raise RuntimeError('injected interruption')
            with self.assertRaisesRegex(RuntimeError,'injected'):self.make(fault=fail)
            path=(set(self.root.iterdir())-before).pop()
            if stage=='after_seal':verify_sealed(path,envelope(348))
            else:
                with self.assertRaises(CaptureRejected):verify_sealed(path,envelope(348))

    def test_modifications_partial_records_and_restoration(self):
        for name in ('snapshot.sqlite','manifest.json','seal.json'):
            path,_,_=self.make()
            # Deliberate corruption ONLY of this test's newly returned fixture.
            with (path/name).open('wb') as f:f.write(b'{partial')
            with self.subTest(name=name),self.assertRaises((CaptureRejected,ValueError)):
                verify_sealed(path,envelope(348))
        path,_,_=self.make()
        with self.assertRaises(CaptureRejected):verify_sealed(path,envelope(349))
        with self.assertRaises(CaptureRejected):verify_sealed(path,envelope(347))

    def test_disk_divergence_before_seal_is_rejected(self):
        def corrupt(stage):
            if stage=='after_sqlite':
                path=next(self.root.glob('sealed-*'))/'snapshot.sqlite'
                db=sqlite3.connect(path)
                try:
                    changed=envelope(348);changed['state']['equity']='7'
                    db.execute('UPDATE incremental_state SET payload=?',(encoded(changed).decode(),))
                    db.commit()
                finally:db.close()
        with self.assertRaisesRegex(CaptureRejected,'SQLite/anchor'):self.make(fault=corrupt)
        self.assertFalse(any(self.root.glob('sealed-*/seal.json')))


if __name__=='__main__':unittest.main()
