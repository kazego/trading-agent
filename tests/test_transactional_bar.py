"""Synthetic SQLite-memory BAR tests; no filesystem locks/operational sources.

The filesystem latch is replaced by an in-memory list. Real journal verification,
source-evidence comparison, projection writes and SQLite transactions are retained.
"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_consumer_budget as base
from test_consumer_budget import b2
from paper.ledger import PaperLedger
from paper.market import CandleEvidence
from backtest.data import Candle
from research.spec import encoded, fingerprint


def fixture(bars=2, accepts=3, backlog=2):
    f=base.CheckpointTests();f.setUp();state,_=f.read();db=f.db
    v=object.__new__(PaperLedger);v._db=db;v.identity_id=state.identity
    v.identity=json.loads(db.execute('select payload from identity').fetchone()[0])
    v.binding=f.binding;v.rules=f.rules;v._runtime_identity=v.identity['implementation']
    v.fault=None;v.authorization=None;v.verified_source=None
    v._check_latch=lambda:None;v.errors=[];v._block=lambda error:v.errors.append(type(error).__name__)
    observations=[b2.item(boundary=b2.T+i*14400000,signals=('NONE',)*3) for i in range(2)]
    def event(kind,data):
        request=v._request(state,kind,data);parent=state.journal_head;result=state.apply(kind,data)
        body=dict(label='FORWARD_PAPER / NON_EXECUTING',parent=parent,request=request,kind=kind,input=data,result=result)
        digest=fingerprint(body);n=db.execute('select count(*) from journal').fetchone()[0]+1
        db.execute('insert into journal values(?,?,?,?,?)',(n,digest,request,parent,encoded(body).decode()))
        state.journal_head=digest;state.event_ids[request]=digest
    for i in range(2,accepts):
        t=b2.T+i*14400000
        item=b2.item(boundary=t,signals=('SHORT','NONE','NONE') if i==accepts-1 else ('NONE',)*3)
        observations.append(item)
        event('ACCEPT',dict(source=item.record,accepted_ms=t+240000,equity=state.basis))
    owner=state.slot[1];event('CONFIRM',dict(owner=owner,durable_ms=t+240001,elapsed_ns=1000000));entry=t+900000
    candles={}
    for i in range(bars+backlog):
        stamp=entry+i*900000;c=Candle('BTCUSDT','15m',stamp,stamp+899999,'100','101','99','100','1')
        candles['BTCUSDT',stamp]=CandleEvidence(f.binding.draft.source,c,fingerprint(c.record()),stamp+900000)
    event('ENTRY',dict(owner=owner,candle=candles['BTCUSDT',entry].record))
    for i in range(bars):event('BAR',dict(owner=owner,candle=candles['BTCUSDT',entry+i*900000].record))
    v._materialize(state);db.commit()
    v.source=SimpleNamespace(snapshot=lambda:tuple(observations))
    v.market=SimpleNamespace(at=lambda symbol,stamp:candles.get((symbol,stamp)))
    return f,v,owner,entry+bars*900000,candles


def previous_path(v,owner,stamp):
    """Exact prior processing order for differential memory measurement."""
    state=v.reconcile();v._sources(state)
    r=state.reservations[owner]['reservation'];item=v._candle(r['symbol'],stamp)
    p=state.positions[owner]
    expected=p.last_processed_ms+900000 if p.last_processed_ms is not None else r['entry_boundary_ms']
    request='BAR:'+owner+':'+str(stamp)
    if v._db.execute('select 1 from journal where request=?',(request,)).fetchone() is None and stamp!=expected:
        raise ValueError('Must process exact next candle')
    if item is None:request='WAIT:'+request
    return v._append('BAR',dict(owner=owner,candle=item.record if item else None),request)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.f,self.v,self.owner,self.stamp,self.candles=fixture()
        self.addCleanup(self.f.doCleanups)

    def rows(self):
        return {t:self.v._db.execute('select * from '+t).fetchall() for t in
                ('journal','positions','fills','account','global_slot','processed','trades','equity')}

    def test_equivalence_and_two_verifications(self):
        f,v,owner,stamp,_=fixture();self.addCleanup(f.doCleanups)
        old=previous_path(v,owner,stamp)
        with patch.object(self.v,'_verify',wraps=self.v._verify) as verify:
            new=self.v.process_candle(self.owner,self.stamp)
            self.assertEqual(verify.call_count,2)
        self.assertEqual(old,new)
        for table,rows in self.rows().items():self.assertEqual(rows,v._db.execute('select * from '+table).fetchall())

    def test_idempotency(self):
        result=self.v.process_candle(self.owner,self.stamp);rows=self.rows()
        self.assertEqual(result,self.v.process_candle(self.owner,self.stamp))
        self.assertEqual(rows,self.rows());self.assertFalse(self.v._db.in_transaction)

    def test_rollback_each_stage_and_fresh_retry(self):
        for stage in ('BAR.journal','MATERIALIZE.positions','BAR.state','BAR.before_commit'):
            before=self.rows()
            def fault(point):
                if point==stage:raise RuntimeError('injected')
            self.v.fault=fault
            with self.assertRaises(RuntimeError):self.v.process_candle(self.owner,self.stamp)
            self.assertEqual(before,self.rows());self.assertFalse(self.v._db.in_transaction)
            self.v.fault=None;self.v.reconcile()
        self.v.process_candle(self.owner,self.stamp)

    def test_corruption_rejected_initial_and_after_materialize(self):
        before=self.rows()
        def fault(point):
            if point=='BAR.state':self.v._db.execute("update account set amount='999'")
        self.v.fault=fault
        with self.assertRaises(ValueError):self.v.process_candle(self.owner,self.stamp)
        self.assertEqual(before,self.rows());self.v.fault=None
        self.v._db.execute("update account set amount='999'");self.v._db.commit()
        with self.assertRaises(ValueError):self.v.process_candle(self.owner,self.stamp)

    def test_source_change_rejected_without_write(self):
        before=self.rows();self.candles.clear()
        with self.assertRaises(ValueError):self.v.process_candle(self.owner,self.stamp)
        self.assertEqual(before,self.rows())

    def test_gap_rejected(self):
        before=self.rows()
        with self.assertRaises(ValueError):self.v.process_candle(self.owner,self.stamp+900000)
        self.assertEqual(before,self.rows())

    def test_next_transaction_rechecks_state(self):
        self.v.process_candle(self.owner,self.stamp)
        self.v._db.execute("update account set amount='997'");self.v._db.commit()
        with self.assertRaises(ValueError):self.v.process_candle(self.owner,self.stamp+900000)

    def test_closed_trade_financial_replay_equivalence(self):
        f,v,owner,stamp,cs=fixture();self.addCleanup(f.doCleanups)
        p=self.v.reconcile().positions[self.owner]
        price=p.take_profit
        c=Candle('BTCUSDT','15m',stamp,stamp+899999,price,price,price,price,'1')
        e=CandleEvidence(self.f.binding.draft.source,c,fingerprint(c.record()),stamp+900000)
        self.candles['BTCUSDT',stamp]=e;cs['BTCUSDT',stamp]=e
        self.assertEqual(previous_path(v,owner,stamp),self.v.process_candle(self.owner,self.stamp))
        state=self.v.reconcile()
        self.assertEqual(state.slot,('EMPTY',None,None));self.assertEqual(len(state.trades),1)
        for table,rows in self.rows().items():self.assertEqual(rows,v._db.execute('select * from '+table).fetchall())
