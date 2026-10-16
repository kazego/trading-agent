"""Genesis-only synthetic ledger: no files or operational authorization."""
import sqlite3
import unittest
from copy import deepcopy

from test_transactional_bar import fixture
import test_paper_acceptance as b2
from paper.ledger import SCHEMA, IMMUTABLE
from paper.journal import Replay
from research.incremental_ledger import SyntheticLedger, ExternalMemory, Blocked
from research.spec import encoded


class EmptyJournalTests(unittest.TestCase):
    def setUp(self):
        f,self.v,*_=fixture();self.addCleanup(f.doCleanups)
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.executescript(SCHEMA)
        for table in IMMUTABLE:
            for op in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER no_{op}_{table} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END")
        db.execute('INSERT INTO identity VALUES(1,?,?)',(self.v.identity_id,encoded(self.v.identity).decode()))
        self.v._db=db
        self.v._materialize(Replay(self.v.binding,self.v.rules,self.v.identity_id));db.commit()
        self.external=ExternalMemory()
        self.core=SyntheticLedger(self.v,self.external,authorization_id='empty-test',
            authorize=lambda:'empty-test',evidence=lambda k,d:None,clock=lambda:100)
        self.core.bootstrap()

    def test_empty_recovery_then_first_accept_matches_replay(self):
        self.assertIsNone(self.v._db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone())
        before=self.core.recover()
        self.assertEqual((before['sequence'],before['state']['head']),(0,self.v.identity_id))
        item=b2.item(signals=('NONE',)*3)
        data=dict(source=item.record,accepted_ms=item.boundary_ms+240000,equity=self.v._verify().basis)
        self.core.apply('ACCEPT',data)
        self.assertEqual(self.core.recover()['sequence'],1)
        self.assertEqual(self.core._state(self.core.recover()).rows(),self.v._verify().rows())
        self.core.apply('ACCEPT',data)
        self.assertEqual(self.core.recover()['sequence'],1)

    def test_empty_with_nonzero_sequence_or_wrong_head_rejected(self):
        original=self.external.anchor
        for field,value in (('sequence',1),('head','0'*64)):
            bad=deepcopy(original)
            if field=='head':bad['state']['head']=value
            else:bad[field]=value
            self.v._db.execute('UPDATE incremental_state SET payload=?',(encoded(bad).decode(),));self.v._db.commit()
            with self.subTest(field=field),self.assertRaisesRegex(Blocked,'Journal tail mismatch'):
                self.core._load()

    def test_persisted_genesis_tampering_rejected(self):
        # Synthetic corruption only; restore the exact trigger so the tail
        # validation, not a schema difference, must detect the altered identity.
        db=self.v._db
        trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='no_UPDATE_identity'").fetchone()[0]
        db.execute('DROP TRIGGER no_UPDATE_identity')
        db.execute("UPDATE identity SET digest=?",('0'*64,))
        db.execute(trigger);db.commit()
        with self.assertRaisesRegex(Blocked,'Journal tail mismatch'):self.core._load()


if __name__=='__main__':unittest.main()
