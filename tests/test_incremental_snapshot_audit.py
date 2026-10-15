"""Synthetic financial replay in memory and manual Windows process test."""
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from copy import deepcopy

sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from test_transactional_bar import fixture
from test_incremental_snapshot import new_directory
from research.incremental_ledger import SyntheticLedger, ExternalMemory
from research.incremental_snapshot import capture_sealed, CaptureRejected
from research.incremental_snapshot_audit import replay_snapshot, audit_sealed
from research.spec import encoded
from paper.market import CandleEvidence
from backtest.data import Candle
from research.spec import fingerprint


def audit_worker(directory,anchor,binding,rules,output):
    try:output.send(('PASS',audit_sealed(Path(directory),anchor,binding,rules)))
    except BaseException as exc:output.send(('FAIL',repr(exc)))
    finally:output.close()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        f,self.v,self.owner,self.stamp,_=fixture(bars=8,backlog=2)
        self.addCleanup(f.doCleanups)
        self.v._db.execute('CREATE TABLE snapshot_fixture(kind TEXT)')
        self.v._db.execute("INSERT INTO snapshot_fixture VALUES ('synthetic-snapshot-v1')")
        self.v._db.commit()
        self.external=ExternalMemory()
        self.core=SyntheticLedger(self.v,self.external,authorization_id='synthetic-audit',
                                  authorize=lambda:'synthetic-audit',evidence=lambda k,d:None,clock=lambda:100)
        self.core.bootstrap()

    def clone(self):
        db=sqlite3.connect(':memory:');self.v._db.backup(db);self.addCleanup(db.close)
        db.execute('PRAGMA query_only=ON');return db

    def test_open_position_exact_replay(self):
        db=self.clone();before=db.serialize()
        started=time.perf_counter_ns()
        result=replay_snapshot(db,self.external.anchor,self.v.binding,self.v.rules)
        print('MEMORY_REPLAY_NS',time.perf_counter_ns()-started,'EVENTS',result['sequence'],flush=True)
        self.assertEqual(result['equity'],str(self.v._verify().equity))
        self.assertEqual(result['financial_rows']['fills'],self.v._verify().rows()['fills'])
        self.assertTrue(result['financial_rows']['positions'])
        self.assertEqual(db.serialize(),before)

    def test_closed_short_fees_equity_exact(self):
        c=Candle('BTCUSDT','15m',self.stamp,self.stamp+899999,'100','101','1','50','1')
        evidence=CandleEvidence(self.v.binding.draft.source,c,fingerprint(c.record()),self.stamp+900000)
        self.core.apply('BAR',dict(owner=self.owner,candle=evidence.record))
        result=replay_snapshot(self.clone(),self.external.anchor,self.v.binding,self.v.rules)
        expected=self.v._verify().rows()
        self.assertTrue(expected['trades'])
        for table,rows in result['financial_rows'].items():self.assertEqual(rows,expected[table])
        self.assertEqual(len(expected['fills']),2)

    def test_wrong_identity_and_compact_state(self):
        for key,value in [('reducer','wrong'),('sequence',999),('authorization','wrong')]:
            anchor=deepcopy(self.external.anchor);anchor[key]=value
            with self.subTest(key=key),self.assertRaises((ValueError,CaptureRejected)):
                replay_snapshot(self.clone(),anchor,self.v.binding,self.v.rules)

    def test_corrupt_financial_projection(self):
        db=self.clone();db.execute('PRAGMA query_only=OFF')
        db.execute("UPDATE account SET amount='7'");db.commit();db.execute('PRAGMA query_only=ON')
        with self.assertRaises(ValueError):replay_snapshot(db,self.external.anchor,self.v.binding,self.v.rules)

    def test_truncated_history(self):
        db=self.clone();db.execute('PRAGMA query_only=OFF')
        db.execute('DROP TRIGGER no_DELETE_journal')
        db.execute('DELETE FROM journal WHERE sequence=(SELECT max(sequence) FROM journal)');db.commit()
        db.execute('PRAGMA query_only=ON')
        with self.assertRaises(ValueError):replay_snapshot(db,self.external.anchor,self.v.binding,self.v.rules)


class ProcessTests(ReplayTests):
    # Keep manual disk test separate from the memory-only command.
    def test_sealed_financial_snapshot_in_independent_process(self):
        root=new_directory();path=root/'source.sqlite'
        with path.open('xb'):pass
        db=sqlite3.connect(path);self.addCleanup(db.close)
        self.v._db.backup(db);db.execute('PRAGMA journal_mode=WAL')
        reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);self.addCleanup(reader.close)
        reader.execute('PRAGMA query_only=ON')
        directory,manifest,metrics=capture_sealed(reader,threading.Lock(),lambda:self.external.anchor,root)
        ctx=multiprocessing.get_context('spawn');receive,send=ctx.Pipe(duplex=False)
        child=ctx.Process(target=audit_worker,args=(str(directory),self.external.anchor,self.v.binding,self.v.rules,send))
        try:
            child.start();send.close()
            self.assertTrue(receive.poll(60),'Auditor timeout')
            status,result=receive.recv();self.assertEqual(status,'PASS',result)
            child.join(5);self.assertEqual(child.exitcode,0)
            self.assertEqual(result['head'],manifest['head']);self.assertEqual(result['authority'],'NONE')
            self.assertEqual(result['financial_rows']['fills'],self.v._verify().rows()['fills'])
            with (root/'audit-result.json').open('xb') as f:f.write(encoded(result))
            print('AUDIT_TIMINGS_NS',result['replay_ns'],result['total_ns'],flush=True)
        finally:
            if child.pid is not None and child.is_alive():child.terminate();child.join(5)
            receive.close();send.close()


if __name__=='__main__':unittest.main()
