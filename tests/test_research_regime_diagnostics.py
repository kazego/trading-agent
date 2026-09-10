"""Synthetic-only diagnostic contracts. No preserved market files or strategies run."""
import ast
from dataclasses import replace
from decimal import Decimal as D
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest.data import Candle
from research.spec import DEV_START,VALIDATION_START,HOLDOUT_START,fingerprint
import research.regime_diagnostics as d
from test_research_protection import fixture


def trade(index=0,pnl='10',symbol='BTCUSDT',direction='LONG',fee='1'):
    t=DEV_START+index*14400000
    opening=dict(side='BUY' if direction=='LONG' else 'SELL',candle_open_time_ms=t,phase='OPEN',
                 reference_price='100',price='100',quantity='1',fee=fee)
    price=D(100)+(D(pnl) if direction=='LONG' else -D(pnl))
    exit=dict(side='SELL' if direction=='LONG' else 'BUY',candle_open_time_ms=t,phase='INTRABAR',
              reference_price=str(price),price=str(price),quantity='1',fee=fee)
    raw=dict(position=dict(decision=dict(symbol=symbol,signal=direction,open_time_ms=t,
        signal_timeframe='4h',decision_id=f'{index:064x}'),entry=opening,stop_loss='98',take_profit='104',planned_stop_risk='2'),
        exit=exit,reason='TAKE_PROFIT' if D(pnl)>0 else 'STOP_LOSS',gross_pnl=pnl,net_pnl=str(D(pnl)-2*D(fee)))
    return d.decode_trade(raw)


def row(index=0,pnl='10',symbol='ADAUSDT',direction='SHORT'):
    return dict(trade_id=str(index),entry=dict(symbol=symbol,direction=direction,ribbon='MIXED',
        ema200_relation='BEARISH',ema200_slope='FALLING',bullish_breakout='FALSE',bearish_breakout='STALE'),
        descriptive=dict(volatility_quartile='Q1'),outcome=dict(gross_pnl=D(pnl)+2,net_pnl=D(pnl),fees=D(2),
        month='2024-01',quarter='2024-Q1',exit_boundary_ms=DEV_START+index*14400000,
        duration_lower_minutes=D(0),duration_upper_minutes=D(15),mfe_pct=D(2),mae_pct=D(1)))


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.view,_,_,cls.snapshot=fixture('LONG')

    def test_population_duplicate_and_financial_integrity(self):
        d.reconcile([trade()])
        with self.assertRaises(ValueError):d.reconcile([trade(),trade()])
        t=trade();t.net_pnl+=1
        with self.assertRaises(ValueError):d.reconcile([t])
        with self.assertRaises(ValueError):d.reconcile([trade()],authoritative=True)

    def test_ada_canonical_guard_independent_population(self):
        ts=[trade(i,'0','ADAUSDT','SHORT','0') for i in range(29)]
        ts[0]=trade(0,'80','ADAUSDT','SHORT','8.734791878875')
        ts[1]=trade(1,'80','ADAUSDT','SHORT','0')
        ts[2]=trade(2,'29.93465862','ADAUSDT','SHORT','0')
        ts.extend(trade(i,'0',fee='0') for i in range(29,174))
        d.reconcile(ts,authoritative=True)
        ts[-1]=trade(173,'0','ADAUSDT','SHORT','0')
        with self.assertRaises(ValueError):d.reconcile(ts,authoritative=True)

    def test_exact_feature_boundary_and_symbol(self):
        fields=d.entry_features(self.view,'BTCUSDT',DEV_START)
        self.assertEqual(fields['boundary_ms'],DEV_START)
        with self.assertRaises(PermissionError):d.entry_features(self.view,'ADAUSDT',DEV_START)
        with self.assertRaises(PermissionError):d.entry_features(self.view,'BTCUSDT',DEV_START+900000)

    def test_locked_and_history_boundaries_rejected(self):
        for t in (DEV_START-14400000,VALIDATION_START,HOLDOUT_START):
            with self.assertRaises(PermissionError):d.entry_features(self.view,'BTCUSDT',t)
            x=trade();x.position.decision.open_time_ms=t
            with self.assertRaises(PermissionError):d.reconcile([x])

    def test_slope_sign_no_threshold(self):
        for delta,expected in [(D('.0000001'),'RISING'),(D('-.0000001'),'FALLING'),(D(0),'FLAT')]:
            emas=tuple(replace(e,latest=e.previous+delta) if e.span==200 else e for e in self.snapshot.emas)
            self.assertEqual(d.feature_fields(replace(self.snapshot,emas=emas))['ema200_slope'],expected)

    def test_normalized_atr_and_signed_distance(self):
        s=replace(self.snapshot,atr14=D(2),breakout=replace(self.snapshot.breakout,completed_close=D(100)))
        for ema,distance in [(D(90),D('.1')),(D(110),D('-.1')),(D(100),D(0))]:
            s=replace(s,emas=tuple(replace(e,latest=ema) if e.span==200 else e for e in s.emas))
            f=d.feature_fields(s)
            self.assertEqual(f['normalized_atr'],D('.02'));self.assertEqual(f['signed_ema200_distance'],distance)

    def test_mixed_versus_equality(self):
        s=replace(self.snapshot,ribbon='NEUTRAL')
        emas=tuple(replace(e,latest=D(e.span)) for e in s.emas)
        self.assertEqual(d.feature_fields(replace(s,emas=emas))['ribbon'],'MIXED')
        emas=tuple(replace(e,latest=D(50)) if e.span==20 else e for e in emas)
        self.assertEqual(d.feature_fields(replace(s,emas=emas))['ribbon'],'EQUALITY_NEUTRAL')

    def test_pf_expectancy_empty_zero_and_no_losses(self):
        self.assertIsNone(d.summary([])['net_expectancy'])
        self.assertEqual(d.summary([row(pnl='0')])['net_pf']['status'],'NO_NONZERO_OUTCOMES')
        self.assertEqual(d.summary([row()])['net_pf']['status'],'NO_LOSSES')
        s=d.summary([row(pnl='10'),row(1,pnl='-5')])
        self.assertEqual(s['net_pf']['value'],D(2));self.assertEqual(s['net_expectancy'],D('2.5'))
        self.assertEqual(s['gross_pf']['value'],D(4));self.assertEqual(s['gross_expectancy'],D('4.5'))

    def test_group_order_and_cross_cohorts(self):
        rows=[row(),row(1,pnl='-5',symbol='BTCUSDT'),row(2,pnl='2',direction='LONG')]
        self.assertEqual(d.groups(rows,lambda r:r['entry']['symbol']),d.groups(rows[::-1],lambda r:r['entry']['symbol']))
        c=d.comparisons(rows)['ribbon']['MIXED']
        self.assertEqual(c['ADAUSDT SHORT']['net_pnl'],D(10))
        self.assertEqual(c['BTCUSDT SHORT']['net_pnl'],D(-5))
        self.assertEqual(c['ADAUSDT LONG']['net_pnl'],D(2))
        self.assertEqual(c['NON_ADA_SHORT']['net_pnl'],D(-3))
        self.assertEqual(c['ALL']['net_pnl'],D(7))

    def test_quartiles_are_descriptive_and_ties_lower(self):
        self.assertEqual(d.partition(D(2),[D(2),D(3),D(4)]),'Q1')
        self.assertEqual(d.partition(D('2.1'),[D(2),D(3),D(4)]),'Q2')
        self.assertEqual(d.partition(D(5),[D(2),D(3),D(4)]),'Q4')

    def test_concentration_and_zero_exclusion(self):
        c=d.concentration_report([row()], [trade(pnl='12')])
        self.assertEqual(c['best_month']['result_excluding'],D(0))
        self.assertEqual(c['ada_short_share_pct'],D(100))
        self.assertEqual(c['result_excluding_ada_short'],D(0))
        self.assertEqual(c['tails']['best_5pct']['trades'],1)

    def test_temporal_zero_months_and_winner_gap(self):
        t=d.temporal([row(),row(1,pnl='-1'),row(2)])
        self.assertEqual(len(t['months']),12);self.assertEqual(len(t['quarters']),4)
        self.assertEqual(t['profitable_months'],1)
        self.assertEqual(t['longest_nonprofitable_sequence_between_winners']['nonprofitable_trades'],1)

    def report(self,bars=None):
        t=trade();bars=bars or {DEV_START:Candle('BTCUSDT','15m',DEV_START,DEV_START+899999,'100','999','1','100','1')}
        return d.analyze([t],{'BTCUSDT':self.view},{'BTCUSDT':bars},{'run_fingerprint':'synthetic'})

    def test_exit_extrema_and_namespace_separation(self):
        r=self.report()['records'][0]
        self.assertEqual(r['outcome']['mfe_pct'],'10');self.assertEqual(r['outcome']['mae_pct'],'0')
        for key in ('net_pnl','exit_reason','mfe_pct','mae_pct','duration_lower_minutes','volatility_quartile'):
            self.assertNotIn(key,r['entry'])
        self.assertIn('volatility_quartile',r['descriptive'])

    def test_no_future_candles_in_entry_window(self):
        before=d.entry_features(self.view,'BTCUSDT',DEV_START)
        # Private synthetic mutation only: preserve all completed candles, replace current/future.
        cs=self.view._candles[:600]+tuple(replace(c,high=D(999),low=D(1),close=D(800)) for c in self.view._candles[600:])
        from copy import copy
        other=copy(self.view);object.__setattr__(other,'_candles',cs)
        self.assertEqual(before,d.entry_features(other,'BTCUSDT',DEV_START))

    def test_deterministic_identity_and_exclusive_output(self):
        a=self.report();b=self.report();self.assertEqual(a,b)
        self.assertEqual(a['diagnostic_fingerprint'],fingerprint({k:v for k,v in a.items() if k!='diagnostic_fingerprint'}))
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'output';d.write_report(a,p)
            before=(p/'report.json').read_bytes()
            with self.assertRaises(FileExistsError):d.write_report(a,p)
            self.assertEqual(before,(p/'report.json').read_bytes())

    def test_tampered_source_denied_before_payload_access(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'run-manifest.json').write_text('{"manifest_fingerprint":"bad"}')
            with self.assertRaises(ValueError):d.verify_source(Path(root))

    def test_no_signal_execution_or_network_calls(self):
        tree=ast.parse(inspect.getsource(d))
        calls={n.func.attr if isinstance(n.func,ast.Attribute) else n.func.id if isinstance(n.func,ast.Name) else ''
               for n in ast.walk(tree) if isinstance(n,ast.Call)}
        self.assertTrue(calls.isdisjoint({'get_signal','evaluate','simulate','simulate_portfolio','execute','open_position','urlopen','get'}))
