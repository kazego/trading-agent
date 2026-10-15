"""Memory-only regression of shared checkpoint verification and diagnostics."""
from copy import deepcopy
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paper import authorization as a
from paper.consumer import StageTimings
from paper.ledger import PaperLedger, SCHEMA, IMMUTABLE
from paper.journal import Replay, HISTORICAL, implementation
from paper.acceptance import AcceptanceBinding, ProspectiveGate
from paper.model import DraftManifest, LABEL
from research.spec import encoded, fingerprint
import test_paper_acceptance as b2
import test_paper_foundation as b1
import test_paper_settlement as b3


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.connect=sqlite3.connect
        self.rules=b3.rules()
        self.binding=AcceptanceBinding(DraftManifest(b1.identity(),rules_fingerprint=self.rules.id),
                                       ProspectiveGate(b2.T-14400000,'b'*64))
        self.v=dict(implementations={'b4':implementation()},t0_ms=self.binding.prospective.start_ms)
        identity=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=implementation(),
            historical=HISTORICAL,binding=self.binding.record,rules=self.rules.record,
            activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=self.v['t0_ms']))
        self.db=self.connect(':memory:');self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        for table in IMMUTABLE:
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER no_{op}_{table} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END")
        genesis=fingerprint(identity);self.db.execute('insert into identity values(1,?,?)',(genesis,encoded(identity).decode()))
        state=Replay(self.binding,self.rules,genesis)
        for i in range(2):
            boundary=b2.T+i*14400000
            data=dict(source=b2.item(boundary=boundary,signals=('NONE',)*3).record,accepted_ms=boundary+240000,equity=state.basis)
            request='ACCEPT:'+str(boundary);parent=state.journal_head if i else genesis
            result=state.apply('ACCEPT',data)
            event=dict(label=LABEL,parent=parent,request=request,kind='ACCEPT',input=data,result=result)
            digest=fingerprint(event)
            self.db.execute('insert into journal values(?,?,?,?,?)',(i+1,digest,request,parent,encoded(event).decode()))
            state.journal_head=digest;state.event_ids[request]=digest
        for table,rows in state.rows().items():
            if table=='resolutions':continue
            for row in rows:self.db.execute(f"insert into {table} values({','.join('?' for _ in row)})",row)
        self.db.commit()

    def read(self,**kwargs):
        image=self.db.serialize()
        def connection(*args,**kw):
            self.assertIn('mode=ro',args[0]);self.assertTrue(kw['uri'])
            db=self.connect(':memory:');db.deserialize(image);return db
        with patch.object(a,'paths',return_value=(Path('C:/synthetic'),Path('C:/synthetic'))), \
             patch.object(a.b,'safe',side_effect=lambda p:p), \
             patch.object(a,'_historical_auth',return_value=SimpleNamespace(binding=self.binding,rules=self.rules)), \
             patch.object(a.sqlite3,'connect',side_effect=connection):
            return a.state_at('synthetic',Path('C:/synthetic'),self.v,**kwargs)

    def test_same_checkpoints_one_full_verification(self):
        cps=[self.read(count=n)[1] for n in (0,1,2)]
        original=PaperLedger._verify;calls=[]
        def verify(obj,**kwargs):calls.append(1);return original(obj,**kwargs)
        with patch.object(PaperLedger,'_verify',verify):
            state,_=self.read(checkpoints=cps)
        self.assertEqual(len(calls),1);self.assertEqual(state.equity,1000)
        self.assertEqual(state.slot,('EMPTY',None,None))

    def test_wrong_checkpoint_and_conflicting_same_count(self):
        cp=self.read(count=1)[1];bad=deepcopy(cp);bad['journal_fingerprint']='0'*64
        with self.assertRaises(ValueError):self.read(checkpoints=(cp,bad))

    def test_projection_corruption_not_hidden_by_valid_prefix(self):
        cp=self.read(count=1)[1]
        self.db.execute("update account set amount='999'");self.db.commit()
        with self.assertRaises(ValueError):self.read(checkpoints=(cp,))

    def test_truncation_rejected(self):
        cp=self.read(count=2)[1]
        self.db.execute('drop trigger no_DELETE_journal');self.db.execute('delete from journal where sequence=2')
        self.db.execute("CREATE TRIGGER no_DELETE_journal BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT,'Append only'); END")
        self.db.commit()
        with self.assertRaises(ValueError):self.read(checkpoints=(cp,))

    def test_retry_fresh_read_and_no_mutation(self):
        cp=self.read(count=1)[1];before=self.db.serialize()
        self.assertEqual(self.read(checkpoints=(cp,))[1],self.read(checkpoints=(cp,))[1])
        self.assertEqual(self.db.serialize(),before)
        self.db.execute("update account set amount='998'");self.db.commit()
        with self.assertRaises(ValueError):self.read(checkpoints=(cp,))

    def test_invalid_lengths(self):
        cp=self.read(count=1)[1]
        for n in (-1,3,True):
            with self.subTest(n=n),self.assertRaises(ValueError):self.read(checkpoints=(dict(cp,journal_count=n),))

    def test_genesis_identity_rejected(self):
        self.v['implementations']['b4']='0'*64
        with self.assertRaises(ValueError):self.read(checkpoints=())


class TimingTests(unittest.TestCase):
    def test_pre_started_time_and_exception_are_measured(self):
        samples=iter([2,7,8,11,13])
        timer=StageTimings(0,lambda:next(samples))
        with timer.measure('authorization_before_started_ms'):pass
        with self.assertRaises(ValueError),timer.measure('source_capture'):raise ValueError('failure')
        r=timer.report()
        self.assertEqual(r['total_seconds'],13)
        self.assertEqual(r['stage_seconds'],{'authorization_before_started_ms':5,'source_capture':3})
        self.assertEqual(r['unattributed_seconds'],5)
