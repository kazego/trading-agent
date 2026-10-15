"""Memory cursor protocol tests; large suffixes are continuity-only fixtures."""
from pathlib import Path
import sys
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from research.incremental_ledger import ExternalMemory, Blocked
from research.incremental_certificate import continuity, CertifiedSyntheticLedger
from research.spec import encoded, fingerprint
from test_incremental_certificate import Integration


class CursorTests(unittest.TestCase):
    def fixture(self, suffix):
        external=ExternalMemory();db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE journal(sequence INTEGER,digest TEXT,request TEXT,parent TEXT,payload TEXT)')
        previous=None;head='genesis'
        owner=object();session='session';snapshot='snapshot';n=20
        for seq in range(1,n+suffix+1):
            event=dict(parent=head,request=str(seq),kind='synthetic-continuity-only')
            head=fingerprint(event)
            db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',(seq,head,str(seq),event['parent'],encoded(event).decode()))
            value=dict(version='test',reducer='test',authorization='test',schema='test',policy={},
                       sequence=seq,state=dict(identity='genesis',head=head))
            external.publish(previous,value);previous=value
            if seq==n:
                anchor=value;token=external.issue_cursor(anchor,owner,session,snapshot)
        db.commit()
        core=SimpleNamespace(db=db,external=external,_load=lambda:external.anchor)
        # Cursor is bound to actual core, not an independently supplied owner.
        token=external.issue_cursor(anchor,core,session,snapshot)
        return core,dict(anchor=anchor,captured_at_ns=100),(token,session,snapshot)

    def test_suffix_counts_and_full_equivalence(self):
        for size in (0,1,100,999,1000):
            with self.subTest(size=size):
                core,cert,cursor=self.fixture(size)
                expected=continuity(core,cert)
                core.clock_ns=lambda:100;core._guard=lambda:None
                core._certificate=None;core._certificate_ids=set()
                core.external.cursor_reads.clear()
                with patch.object(ExternalMemory,'records',property(lambda _: (_ for _ in ()).throw(AssertionError('Historical scan')))):
                    if size==1000:
                        with self.assertRaises(Blocked):CertifiedSyntheticLedger._install(core,cert,cursor=cursor)
                        self.assertEqual(core.external.cursor_reads,[])
                    else:
                        CertifiedSyntheticLedger._install(core,cert,cursor=cursor)
                        self.assertEqual(core.external.anchor,expected)
                        self.assertEqual(core.external.cursor_reads,list(range(21,21+size)))
                print('CURSOR_READS',size,len(core.external.cursor_reads),flush=True)

    def test_replacement_loss_of_ownership_and_forgery(self):
        for mode in ('replace','lock','foreign','session','snapshot','fake','blocked','publication'):
            core,cert,cursor=self.fixture(1)
            if mode=='replace':core.external._records=core.external.records
            elif mode=='lock':core.external.lock=threading.RLock()
            elif mode=='foreign':core=SimpleNamespace(**vars(core))
            elif mode=='session':cursor=(cursor[0],'other',cursor[2])
            elif mode=='snapshot':cursor=(cursor[0],cursor[1],'other')
            elif mode=='fake':cursor=(object(),cursor[1],cursor[2])
            elif mode=='blocked':core.external.blocked=True
            elif mode=='publication':
                with self.assertRaises(Blocked):core.external.publish(None,{})
            with self.subTest(mode=mode),self.assertRaises(Blocked):continuity(core,cert,cursor=cursor)

    def test_unknown_cursor_and_duplicate(self):
        core,cert,cursor=self.fixture(1)
        continuity(core,cert,cursor=cursor)
        with self.assertRaises(Blocked):continuity(core,cert,cursor=cursor)
        with self.assertRaises(TypeError):core.external._records[0]=b'changed'

    def test_temporary_loss_still_revokes(self):
        core,cert,cursor=self.fixture(0)
        original=core.external.lock
        core.external.lock=threading.RLock();core.external.lock=original
        with self.assertRaises(Blocked):continuity(core,cert,cursor=cursor)
        core,cert,cursor=self.fixture(0)
        core.external.blocked=True;core.external.blocked=False
        with self.assertRaises(Blocked):continuity(core,cert,cursor=cursor)

    def test_pending_or_forked_sqlite(self):
        for query in ("DELETE FROM journal WHERE sequence=21", "UPDATE journal SET parent='fork' WHERE sequence=21"):
            core,cert,cursor=self.fixture(1);core.db.execute(query);core.db.commit()
            with self.assertRaises(Blocked):continuity(core,cert,cursor=cursor)


class FinancialCursorTests(Integration):
    def test_restart_revokes_old_owner_cursor(self):
        token=self.external.issue_cursor(self.anchor,self.core,'session','snapshot')
        self.new()
        with self.assertRaises(Blocked):
            continuity(self.core,self.proposal,cursor=(token,'session','snapshot'))

    def test_rollback_revokes_cursor(self):
        self.install();token=self.external.issue_cursor(self.anchor,self.core,'session','snapshot')
        def fault(point):
            if point=='after_event':raise RuntimeError('rollback')
        with self.assertRaises(RuntimeError):
            self.core.apply('BAR',dict(owner=self.owner,candle=self.candles['BTCUSDT',self.stamp].record),fault=fault)
        with self.assertRaises(Blocked):continuity(self.core,self.proposal,cursor=(token,'session','snapshot'))
        self.v._verify()
    def test_financial_advance_then_cursor_install_no_replay(self):
        self.install()  # Existing reference certificate permits the synthetic writer.
        token=self.external.issue_cursor(self.anchor,self.core,'session','snapshot')
        self.bar()
        # Separate newer snapshot identity, same prefix: direct continuity comparison.
        expected=continuity(self.core,self.proposal)
        with patch('paper.ledger.PaperLedger._verify',side_effect=AssertionError('Replay during cursor check')):
            actual=continuity(self.core,self.proposal,cursor=(token,'session','snapshot'))
        self.assertEqual(actual,expected)
        self.v._verify()


if __name__=='__main__':unittest.main()
