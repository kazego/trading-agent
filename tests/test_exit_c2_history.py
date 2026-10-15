"""Synthetic end-to-end tests. Never opens a dataset or operational database."""
from dataclasses import replace
from decimal import Decimal as D
import json
import socket
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from backtest.data import Candle, SYMBOLS
from forward.model import HISTORY, signal_snapshot
from research.exit_c2 import STEP, FOUR_H
from research.exit_c2_history import History, run, compare, signals_at, load_reviewed_files
from test_exit_c2 import rules, S


def fixture():
    rows = []
    for symbol in SYMBOLS:
        for t in range(S-HISTORY, S+2*FOUR_H, STEP):
            price = '100'
            if symbol == 'BTCUSDT':
                if S-FOUR_H <= t < S: price = '101'
                if t >= S: price = '90'
            rows.append(Candle(symbol,'15m',t,t+STEP-1,price,'110','89',price,'1'))
    return History(S,S+2*FOUR_H,tuple(rows),'SYNTHETIC_ONLY')


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = fixture()
        cls.results = compare(cls.history, rules())

    def test_real_kernel_equivalence(self):
        series = {s: tuple(c for c in self.history.candles if c.symbol==s) for s in SYMBOLS}
        batch = signals_at(series,S)
        self.assertEqual(batch[0].direction,'LONG')
        for sig in batch:
            expected = signal_snapshot(series[sig.symbol][:HISTORY//STEP],sig.symbol,S)
            self.assertEqual(sig.evidence,expected['signal_fingerprint'])
            self.assertEqual(sig.atr,D(expected['atr14']))

    def test_end_to_end_opposite_close_reversal(self):
        a, ar = self.results['A']; c, cr = self.results['C2']
        self.assertEqual(ar['closed_trades'],0)
        self.assertEqual(cr['exit_reasons'],{'OPPOSITE_SIGNAL':1})
        self.assertEqual(cr['reversal_commitments'],1)
        self.assertEqual(c.position.decision.direction,'SHORT')
        self.assertEqual(c.position.entry.candle_open_time_ms,S+FOUR_H+3*STEP)
        self.assertEqual(c.trades[0]['exit']['candle_open_time_ms'],S+FOUR_H+STEP)
        self.assertEqual(a.position.decision.direction,'LONG')

    def test_shared_entry_no_future_price_selection(self):
        a,_ = self.results['A']; c,_ = self.results['C2']
        self.assertEqual(a.position.entry.price,D(c.trades[0]['position']['entry']['price']))
        self.assertEqual(a.position.entry.quantity,D(c.trades[0]['position']['entry']['quantity']))

    def test_report_financial_conservation(self):
        c,r = self.results['C2']
        self.assertEqual(D(r['realized_equity']),D(1000)+sum(D(t['net_pnl']) for t in c.trades))
        p=c.position
        expected=c.equity+(p.entry.price-D(90))*p.entry.quantity-p.entry.fee
        self.assertEqual(D(r['final_mtm_equity']),expected)
        self.assertEqual(r['closed_trades'],1)
        self.assertIsNotNone(r['open_position'])
        self.assertGreater(D(r['fees']),0)
        self.assertGreater(D(r['embedded_slippage']),0)
        self.assertGreaterEqual(D(r['max_drawdown_close_mtm']),0)

    def test_no_trade_metrics_are_not_fabricated(self):
        _,r=self.results['A']
        self.assertIsNone(r['profit_factor']); self.assertIsNone(r['expectancy'])
        self.assertEqual(r['realized_net_pnl'],'0')

    def test_future_change_cannot_change_first_decision(self):
        altered=replace(self.history,candles=tuple(replace(c,close=D('110')) if c.open_time_ms>=S else c for c in self.history.candles))
        def batch(h):
            return signals_at({s:tuple(c for c in h.candles if c.symbol==s) for s in SYMBOLS},S)
        self.assertEqual(batch(self.history),batch(altered))

    def test_coverage_rejects_gap_duplicate_and_out_of_order(self):
        cs=self.history.candles
        for broken in (cs[1:],cs+(cs[0],),(cs[1],cs[0])+cs[2:]):
            with self.assertRaises(ValueError): replace(self.history,candles=broken)

    def test_coverage_includes_exact_warmup(self):
        coverage=self.history.coverage()
        self.assertEqual(coverage['per_symbol'],9632)
        self.assertEqual(coverage['gaps'],0)

    def test_replay_deterministic(self):
        c,r=run(self.history,rules(),'C2')
        self.assertEqual(c.journal,self.results['C2'][0].journal)
        self.assertEqual(r,self.results['C2'][1])

    def test_run_has_no_io_network_process_or_sqlite(self):
        with patch('builtins.open',side_effect=AssertionError('file')), patch('pathlib.Path.open',side_effect=AssertionError('path')), patch.object(socket,'socket',side_effect=AssertionError('network')), patch.object(sqlite3,'connect',side_effect=AssertionError('sqlite')), patch.object(subprocess,'Popen',side_effect=AssertionError('process')):
            _,r=run(self.history,rules(),'A')
            self.assertEqual(r['closed_trades'],0)

    def test_strict_resolution_failure_propagates(self):
        with patch('pathlib.Path.resolve',side_effect=PermissionError('sandbox')):
            with self.assertRaises(PermissionError):
                load_reviewed_files('synthetic',[],start=S,end=S+FOUR_H)

    def test_chronological_fills_and_one_slot(self):
        c,_=self.results['C2']
        events=[json.loads(raw) for raw in c.journal]
        exposure=0
        for e in events:
            if e['kind']=='ENTRY': exposure+=1
            if e['kind']=='CLOSE_CONFIRMED': exposure-=1
            self.assertIn(exposure,(0,1))
        self.assertEqual(exposure,1)


if __name__=='__main__': unittest.main()
