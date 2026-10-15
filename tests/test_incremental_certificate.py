"""Memory financial integration; sealed-file I/O substituted, not certified here."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from test_transactional_bar import fixture
from research.incremental_ledger import ExternalMemory, Blocked
from research.incremental_certificate import CertifiedSyntheticLedger, candidate, freshness
from research.incremental_snapshot_audit import replay_snapshot
import sqlite3


class Limits(unittest.TestCase):
    def test_exact_boundaries(self):
        cert={'captured_at_ns':100,'anchor':{'sequence':10}}
        for now,n in [(100,10),(100+86400*10**9-1,1009)]:freshness(cert,n,now)
        for now,n in [(99,10),(100+86400*10**9,10),(100,1010),(100,9)]:
            with self.subTest(now=now,n=n),self.assertRaises(Blocked):freshness(cert,n,now)


class Integration(unittest.TestCase):
    def setUp(self):
        f,self.v,self.owner,self.stamp,self.candles=fixture(bars=2,backlog=3)
        self.addCleanup(f.doCleanups);self.external=ExternalMemory();self.now=100
        self.core=self.new();self.core.bootstrap();self.anchor=self.external.anchor
        db=sqlite3.connect(':memory:');self.v._db.backup(db);db.execute('PRAGMA query_only=ON')
        try:self.result=replay_snapshot(db,self.anchor,self.v.binding,self.v.rules)
        finally:db.close()
        self.result.update(status='PASS',authority='NONE',snapshot_id='synthetic-test',manifest_hash='a'*64,
                           file_sha256='b'*64,captured_at_ns=100,reducer=self.anchor['reducer'])
        self.proposal=candidate(self.result,self.anchor)
        self.mock=patch('research.incremental_certificate.audit_sealed',return_value=self.result).start()
        self.addCleanup(patch.stopall)

    def new(self):
        return CertifiedSyntheticLedger(self.v,self.external,authorization_id='synthetic',
            authorize=lambda:'synthetic',evidence=lambda k,d:None,clock=lambda:100,clock_ns=lambda:self.now)

    def install(self,**kwargs):return self.core.certify(None,self.anchor,self.proposal,**kwargs)

    def bar(self):
        return self.core.apply('BAR',dict(owner=self.owner,candle=self.candles['BTCUSDT',self.stamp].record))

    def test_uninstalled_block_then_progress_and_expiry(self):
        with self.assertRaises(Blocked):self.bar()
        self.install();self.bar();self.v._verify()
        self.now=100+86400*10**9
        with self.assertRaises(Blocked):self.bar()

    def test_duplicate_no_renewal_and_restart(self):
        self.install();self.now+=1
        with self.assertRaises(Blocked):self.install()
        self.core=self.new()
        with self.assertRaises(Blocked):self.bar()
        self.install();self.bar()

    def test_late_result_and_forged_proposal(self):
        self.now=100+86400*10**9
        with self.assertRaises(Blocked):self.install()
        self.now=100;self.proposal['captured_at_ns']=101
        with self.assertRaises(Blocked):self.install()

    def test_interruption_before_and_after_commit(self):
        for stage in ('before_install','after_commit'):
            def fail(point):
                if point==stage:raise RuntimeError('interrupted')
            with self.assertRaises(RuntimeError):self.install(fault=fail)
            with self.assertRaises(Blocked):self.bar()
        self.install();self.bar()

    def test_external_fork(self):
        self.external._records=[]
        with self.assertRaises(Blocked):self.install()

    def test_valid_suffix_certifies_only_original_prefix(self):
        self.install();self.bar()
        self.core=self.new();self.install()
        self.assertEqual(self.core._certificate['anchor']['sequence'],self.anchor['sequence'])
        self.assertGreater(self.external.anchor['sequence'],self.anchor['sequence'])

    def test_out_of_order_distinct_snapshot(self):
        self.install()
        self.result['snapshot_id']='another-snapshot'
        self.proposal=candidate(self.result,self.anchor)
        with self.assertRaisesRegex(Blocked,'Out of order'):self.install()

    def test_expiry_during_install_does_not_grant_authority(self):
        def advance(stage):
            if stage=='after_commit':self.now=100+86400*10**9
        with self.assertRaises(Blocked):self.install(fault=advance)
        self.assertIsNone(self.core._certificate)
        with self.assertRaises(Blocked):self.bar()


class SealedIntegration(unittest.TestCase):
    def test_real_sealed_audit_and_install(self):
        from test_incremental_snapshot import new_directory
        from research.incremental_snapshot import capture_sealed
        from research.incremental_snapshot_audit import audit_sealed
        import threading
        import time
        root=new_directory()  # Permission failure remains a test error, not PASS.
        f,v,owner,stamp,candles=fixture(bars=2,backlog=2);self.addCleanup(f.doCleanups)
        v._db.execute('CREATE TABLE snapshot_fixture(kind TEXT)')
        v._db.execute("INSERT INTO snapshot_fixture VALUES ('synthetic-snapshot-v1')");v._db.commit()
        external=ExternalMemory()
        core=CertifiedSyntheticLedger(v,external,authorization_id='synthetic',authorize=lambda:'synthetic',
                                      evidence=lambda k,d:None)
        core.bootstrap();anchor=external.anchor
        path=root/'source.sqlite'
        with path.open('xb'):pass
        disk=sqlite3.connect(path);self.addCleanup(disk.close);v._db.backup(disk)
        disk.execute('PRAGMA journal_mode=WAL')
        reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);self.addCleanup(reader.close)
        reader.execute('PRAGMA query_only=ON')
        directory,manifest,metrics=capture_sealed(reader,threading.Lock(),lambda:anchor,root)
        result=audit_sealed(directory,anchor,v.binding,v.rules);proposal=candidate(result,anchor)
        identity=core.certify(directory,anchor,proposal)
        core.apply('BAR',dict(owner=owner,candle=candles['BTCUSDT',stamp].record))
        v._verify()
        self.assertEqual(core._certificate['captured_at_ns'],manifest['captured_at_ns'])
        print('EXPERIMENTAL_CERTIFICATE',identity,flush=True)


if __name__=='__main__':unittest.main()
