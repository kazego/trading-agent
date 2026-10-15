"""Actual consumer position loop with isolated in-memory financial backends.

No consume(), filesystem, activation loader or scheduled task is invoked.
"""
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from test_transactional_bar import fixture
from paper.consumer import _drain_position, StageTimings
from paper.ledger import PaperLedger
from paper.market import CandleEvidence
from backtest.data import Candle
from research.spec import fingerprint
from research.incremental_consumer import MemoryPositionLedger


class ConsumerIntegration(unittest.TestCase):
    def test_nonmemory_database_rejected_before_bootstrap(self):
        # Simulated PRAGMA metadata: no disk file is opened for this rejection.
        db=SimpleNamespace(execute=lambda _:SimpleNamespace(fetchall=lambda:[('0','main','C:/forbidden.sqlite')]))
        verifier=SimpleNamespace(_db=db)
        with self.assertRaisesRegex(ValueError,'in-memory database only'):
            MemoryPositionLedger(verifier,authorization_id='synthetic',authorize=lambda:'synthetic',clock=lambda:100)

    def prepare(self,closing=False):
        f,v,owner,stamp,candles=fixture(bars=2,backlog=3);self.addCleanup(f.doCleanups)
        if closing:
            c=Candle('BTCUSDT','15m',stamp,stamp+899999,'100','101','1','50','1')
            candles['BTCUSDT',stamp]=CandleEvidence(v.binding.draft.source,c,fingerprint(c.record()),stamp+900000)
        return v,owner,stamp,candles

    def run_loop(self,backend,stamp,candles,*,now=None,available=True,guard=lambda:None):
        count=[]
        view=SimpleNamespace(at=lambda s,t:candles.get((s,t)) if available else None)
        state=backend.reconcile()
        result=_drain_position(backend,view,{'BTCUSDT':{'last_open_ms':stamp}},state,stamp+900000,
            clock=lambda:now if now is not None else stamp+900000,budget=lambda:len(count)<1,
            authorization_guard=guard,timings=StageTimings(time.monotonic()),committed=lambda:count.append(1))
        return result,len(count)

    def rows(self,v):
        tables=('identity','journal','reservations','positions','fills','candles','processed','trades','equity','global_slot','account')
        return {t:sorted(v._db.execute('SELECT * FROM '+t).fetchall()) for t in tables}

    def test_bar_and_closed_trade_exact_against_traditional_consumer(self):
        for closing in (False,True):
            with self.subTest(closing=closing):
                traditional,owner,stamp,candles=self.prepare(closing)
                incremental,owner2,stamp2,candles2=self.prepare(closing)
                self.assertEqual((owner,stamp),(owner2,stamp2))
                adapter=MemoryPositionLedger(incremental,authorization_id='synthetic',
                    authorize=lambda:'synthetic',clock=lambda:100)
                self.run_loop(traditional,stamp,candles)
                with patch.object(PaperLedger,'_verify',side_effect=AssertionError('Full replay in incremental transition')) as replay:
                    (_,within),count=self.run_loop(adapter,stamp,candles2)
                    self.assertEqual(count,1);self.assertFalse(within);replay.assert_not_called()
                    before=self.rows(incremental)
                    adapter.process_candle(owner,stamp)  # Identical retry including after close.
                    self.assertEqual(self.rows(incremental),before)
                self.assertEqual(self.rows(incremental),self.rows(traditional))
                expected=traditional._verify();actual=incremental._verify()
                self.assertEqual(actual.rows(),expected.rows())
                self.assertEqual(actual.equity,expected.equity)
                self.assertEqual(len(actual.rows()['fills']),2 if closing else 1)
                self.assertEqual(len(actual.rows()['trades']),1 if closing else 0)

    def test_causality_and_guard_before_mutation(self):
        v,owner,stamp,candles=self.prepare()
        adapter=MemoryPositionLedger(v,authorization_id='synthetic',authorize=lambda:'synthetic',clock=lambda:100)
        before=self.rows(v)
        for kwargs in ({'now':stamp},{'available':False},{'guard':lambda: (_ for _ in ()).throw(ValueError('auth'))}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.run_loop(adapter,stamp,candles,**kwargs)
            self.assertEqual(self.rows(v),before)

    def test_transaction_rollback_and_retry(self):
        v,owner,stamp,candles=self.prepare()
        adapter=MemoryPositionLedger(v,authorization_id='synthetic',authorize=lambda:'synthetic',clock=lambda:100)
        before=self.rows(v);apply=adapter.core.apply
        def fail(point):
            if point=='after_state':raise RuntimeError('synthetic crash')
        with patch.object(adapter.core,'apply',side_effect=lambda k,d:apply(k,d,fault=fail)):
            with self.assertRaises(RuntimeError):self.run_loop(adapter,stamp,candles)
        self.assertEqual(self.rows(v),before)
        self.run_loop(adapter,stamp,candles);v._verify()


if __name__=='__main__':unittest.main()
