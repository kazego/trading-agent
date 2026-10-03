"""Synthetic frozen-root/B6 fixtures. Never reads production artifacts."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from paper import binding as b,authorization as a
from paper.operations import record,paths,exclusive
from paper.journal import Replay,HISTORICAL
from paper.ledger import SCHEMA,IMMUTABLE
from paper.model import LABEL
from research.spec import encoded,fingerprint
import test_paper_foundation as b1
import test_paper_settlement as b3

T=b1.T


def fixture(root,run='live-0b-bound-test',source=None,t0=None):
    source=source or b1.identity();rules=b3.rules();t0=T-14400000 if t0 is None else t0
    (Path(root)/'artifacts/forward-paper/live-0a-001').mkdir(parents=True,exist_ok=True)
    v=record(run,t0,t0-14400000,source,rules)
    v['implementations'].update(b3=b.OLD_B3,b4=b.ROOT_B4,b5=b.ROOT_B5)
    control,directory=paths(run,root);control.mkdir(parents=True);directory.mkdir(parents=True)
    exclusive(control/'activation.json',dict(authorization=v,authorization_fingerprint=fingerprint(v)))
    auth=a._historical_auth(v);binding=auth.binding
    identity=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=b.ROOT_B4,historical=HISTORICAL,
        binding=binding.record,rules=rules.record,activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
        initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=t0))
    from paper import ledger as ledger_module
    db=sqlite3.connect(directory/'paper.sqlite');db.executescript(ledger_module.SCHEMA)
    db.execute('INSERT INTO identity VALUES(1,?,?)',(fingerprint(identity),encoded(identity).decode()))
    for table in IMMUTABLE:
        for op in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER no_{op}_{table} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END")
    state=Replay(binding,rules,fingerprint(identity))
    for table,rows in state.rows().items():
        for row in rows:db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')',row)
    db.commit();db.close()
    _,cp=a.state_at(run,root,v)
    from paper.transition import SCOPE
    body=dict(label=LABEL,version='paper-b6-transition-v1',scope=SCOPE,authorization=fingerprint(v),
        old_implementations=v['implementations'],new_implementations=dict(v['implementations'],b4=b.B6_B4,b5=b.B6_B5),
        t0_ms=t0,rules_fingerprint=rules.id,source=source.record,acceptance_policy=v['acceptance_policy'],
        policy_fingerprint=fingerprint(v['policy']),effective_ms=t0+600000,checkpoint=cp)
    exclusive(control/'transition-b6-v1.json',dict(body,transition_fingerprint=fingerprint(body)))
    return SimpleNamespace(root=Path(root),run=run,v=v,control=control,directory=directory,binding=binding,rules=rules,source=source)


def bind_clock(f,effective=T):
    import paper_admin as admin
    with patch.object(admin,'now_ms',return_value=effective-600000):
        p=admin.successor_proposal(f.run,f.root,effective)
        admin.write_proposal(p,f.root);admin.bind(f.run,b.SUCCESSOR,fingerprint(p),root=f.root)
    return p


# Existing B4 fault-injection tests use this harness to preserve their assertions.
# Historical v2 operations are explicitly synthesized with no successor execution
# authorization attached. New v3/resolution operations use real bound loaders.
import test_paper_ledger as legacy
from paper.source import SourceAdapter
from paper.market import CandleSource
from paper.ledger import PaperLedger

class BoundLedgerHarness(legacy.LedgerTests):
    def setUp(self):
        super().setUp();self.ledger.close()
        self.fixture=fixture(Path(self.temp.name)/'bound')
        self.path=self.fixture.directory;self.binding=self.fixture.binding;self.rules=self.fixture.rules
        bind_clock(self.fixture)
        self.clock_patch=patch.object(a,'now_ms',return_value=T+100000000);self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        self.ledger=self.open()

    def open(self,**kwargs):
        if not hasattr(self,'fixture'):return super().open(**kwargs)
        cap=a.load_execution(self.fixture.run,self.fixture.root)
        return PaperLedger(self.path,self.binding,self.rules,self.source,self.market,authorization=cap,**kwargs)

    def accept(self,boundary=T):
        cap=self.ledger.authorization;self.ledger.authorization=None
        try:return super().accept(boundary)
        finally:self.ledger.authorization=cap


def recovery_cap(ledger,owner):
    import paper_admin as admin
    cap=ledger.authorization;run=cap.record['run_id'];root=cap.root
    resolved=max(T+1000000,ledger.reconcile().state_available_ms+1)
    with patch.object(admin,'now_ms',return_value=T+100000000):
        p=admin.abandonment_proposal(run,root,owner,resolved)
        admin.write_proposal(p,root)
        ledger._lock.release()  # synthetic harness relinquishes exclusive ownership for admin bind
        try:admin.bind(run,b.ABANDONMENT,fingerprint(p),root=root,owner=owner)
        finally:ledger._lock.acquire()
    # load separately while no write transaction is open; loader is read-only.
    result=a.load_abandonment(run,owner,root)
    # The ledger must use the exact independently reloaded successor capability.
    ledger.authorization=result.successor
    return result
