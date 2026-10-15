"""Synthetic memory-only coordinator tests; no runner/dataset/SQLite use."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
import json
import socket
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest.data import Candle, SYMBOLS
from backtest.simulation import SymbolRules, process_bar
from paper.market import RuleSnapshot
from research.exit_c2 import Coordinator, Signal, STEP, FOUR_H

S = 1704067200000


def rules(maximum='100000'):
    return RuleSnapshot('SYNTHETIC', 'f'*64, tuple(SymbolRules(s,'SYNTHETIC',
        D('.01'),D('.001'),D('.001'),D(maximum),D('5')) for s in SYMBOLS))


def samples(t, delay=1):
    return (dict(before_ns=0,after_ns=0,utc_ms=t),
            dict(before_ns=delay*1000000,after_ns=delay*1000000,utc_ms=t+delay))


def signals(t=S, directions=('LONG','NONE','NONE'), timing='ONLINE', receipt=None, atr='1'):
    return tuple(Signal(s,t,d,D(atr),t+180000 if receipt is None else receipt,timing,
        str(i+1)*64) for i,(s,d) in enumerate(zip(SYMBOLS,directions)))


def bar(t, op='100', high='101', low='99', close='100', symbol='BTCUSDT'):
    return Candle(symbol,'15m',t,t+STEP-1,op,high,low,close,'1')


def trace(c, kind):
    return [e for raw in c.journal if (e:=json.loads(raw))['kind']==kind]


def opened(policy='C2', direction='LONG'):
    c=Coordinator(policy,rules(),S-FOUR_H)
    c.observe(signals(directions=(direction,'NONE','NONE')),*samples(S+240000))
    c.bar(bar(S+STEP),S+2*STEP)
    return c


def contrary(c, **kwargs):
    for t in range(S+2*STEP,S+FOUR_H,STEP):
        c.bar(bar(t),t+STEP)
    s=signals(S+FOUR_H,('SHORT','NONE','NONE'),**kwargs)
    c.observe(s,*samples(S+FOUR_H+240000))
    return S+FOUR_H


class CoordinationTests(unittest.TestCase):
    def test_differential_paper_entry_both_directions(self):
        import test_paper_settlement as ref
        from paper.settlement import settle
        for direction in ('LONG','SHORT'):
            pending=ref.pending(signals=(direction,'NONE','NONE'),atr='1')
            c=Coordinator('A',ref.rules(),ref.T-FOUR_H)
            c.observe(signals(ref.T,(direction,'NONE','NONE')),*samples(ref.T+240000))
            evidence=ref.candle(pending)
            actual=settle(pending,evidence,ref.rules()).position
            c.bar(evidence.candle,evidence.received_ms)
            self.assertEqual(c.position.entry,actual.entry)
            self.assertEqual(c.position.stop_loss,actual.stop_loss)
            self.assertEqual(c.position.take_profit,actual.take_profit)
            self.assertEqual(c.position.planned_stop_risk,actual.planned_stop_risk)

    def test_differential_paper_closure_both_directions(self):
        import test_paper_settlement as ref
        from paper.settlement import settle,protect
        for direction in ('LONG','SHORT'):
            pending=ref.pending(signals=(direction,'NONE','NONE'),atr='1')
            evidence=ref.candle(pending)
            actual=settle(pending,evidence,ref.rules()).position
            actual=protect(actual,evidence).position
            c=Coordinator('A',ref.rules(),ref.T-FOUR_H)
            c.observe(signals(ref.T,(direction,'NONE','NONE')),*samples(ref.T+240000))
            c.bar(evidence.candle,evidence.received_ms)
            exit_evidence=ref.candle(pending,time=ref.E+STEP,high='110',low='90')
            expected=protect(actual,exit_evidence).trade
            c.bar(exit_evidence.candle,exit_evidence.received_ms)
            self.assertEqual(D(c.trades[0]['net_pnl']),expected.net_pnl)
            self.assertEqual(D(c.trades[0]['gross_pnl']),expected.gross_pnl)
            self.assertEqual(D(c.trades[0]['exit']['fee']),expected.exit.fee)
            self.assertEqual(c.trades[0]['reason'],expected.reason)

    def test_priority_and_no_fallback(self):
        c=Coordinator('A',rules('.001'),S-FOUR_H)
        c.observe(signals(directions=('LONG','LONG','LONG')),*samples(S+240000))
        self.assertEqual([e['reason'] for e in trace(c,'DISPOSITION')],
                         ['PENDING_ENTRY','BLOCKED_PRIORITY','BLOCKED_PRIORITY'])
        c.bar(bar(S+STEP),S+2*STEP)
        self.assertEqual(c.slot,'EMPTY');self.assertEqual(len(trace(c,'REJECTED_SIZING')),1)
        self.assertEqual(len(trace(c,'ENTRY')),0)

    def test_baseline_and_c2_shared_path_exact(self):
        a,b=opened('A'),opened('C2')
        self.assertEqual(a.snapshot(),b.snapshot());self.assertEqual(a.journal,b.journal)
        for c in (a,b): c.bar(bar(S+2*STEP,high='105'),S+3*STEP)
        self.assertEqual(a.snapshot(),b.snapshot());self.assertEqual(a.journal,b.journal)

    def test_a_ignores_contrary(self):
        c=opened('A');contrary(c)
        self.assertIsNone(c.intent);self.assertEqual(c.slot,'OPEN')
        self.assertEqual(trace(c,'DISPOSITION')[-3]['reason'],'BLOCKED_GLOBAL_SLOT')

    def test_full_c2_separate_fill_times_and_costs(self):
        c=opened();s=contrary(c)
        c.bar(bar(s),s+STEP)
        c.bar(bar(s+STEP),s+2*STEP)
        self.assertEqual(c.slot,'EMPTY');self.assertEqual(len(c.trades),1)
        old=c.trades[0]
        self.assertEqual(old['reason'],'OPPOSITE_SIGNAL')
        self.assertEqual(old['exit']['candle_open_time_ms'],s+STEP)
        self.assertEqual(c.equity,D('1000')+D(old['net_pnl']))
        self.assertGreater(D(old['position']['entry']['fee']),0)
        self.assertGreater(D(old['exit']['fee']),0)
        c.decide_reversal(*samples(s+2*STEP))
        self.assertEqual(c.slot,'PENDING')
        c.bar(bar(s+3*STEP),s+4*STEP)
        self.assertEqual(c.position.decision.direction,'SHORT')
        self.assertEqual(c.position.entry.candle_open_time_ms,s+3*STEP)
        self.assertEqual(c.position.decision.atr,D(1))

    def test_no_reversal_before_closure(self):
        c=opened();s=contrary(c)
        with self.assertRaises(ValueError):c.decide_reversal(*samples(s+STEP))

    def test_entry_deadline_inclusive_and_expired(self):
        for offset,expected in [(839999,'PENDING'),(840000,'PENDING'),(840001,'EMPTY')]:
            c=Coordinator('A',rules(),S-FOUR_H)
            c.observe(signals(),*samples(S+offset,0))
            self.assertEqual(c.slot,expected)

    def test_durable_after_deadline_no_entry(self):
        c=Coordinator('A',rules(),S-FOUR_H)
        c.observe(signals(),*samples(S+840000,1))
        self.assertEqual(c.slot,'EMPTY')

    def test_reversal_deadline_and_late_confirmation(self):
        for offset,expected in [(2640000,'PENDING'),(2640001,'EMPTY')]:
            c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
            c.bar(bar(s+STEP),s+2*STEP)
            c.decide_reversal(*samples(s+offset,0));self.assertEqual(c.slot,expected)

    def test_close_intent_deadline_inclusive(self):
        for offset,delay,wanted in [(840000,0,True),(840001,0,False),(840000,1,False)]:
            c=opened();s=S+FOUR_H
            for t in range(S+2*STEP,s,STEP):c.bar(bar(t),t+STEP)
            c.observe(signals(s,('SHORT','NONE','NONE')),*samples(s+offset,delay))
            self.assertEqual(c.intent is not None,wanted)

    def test_reversal_retry_after_entry_does_not_reopen(self):
        c=opened();s=contrary(c);c.bar(bar(s),s+STEP);c.bar(bar(s+STEP),s+2*STEP)
        args=samples(s+2*STEP);result=c.decide_reversal(*args)
        c.bar(bar(s+3*STEP),s+4*STEP);before=c.snapshot(),c.journal
        self.assertEqual(c.decide_reversal(*args),result)
        self.assertEqual((c.snapshot(),c.journal),before)
        with self.assertRaises(ValueError):c.decide_reversal(*samples(s+2*STEP+1))

    def test_reversal_protection_rejection_stays_flat(self):
        c=opened();s=contrary(c);c.bar(bar(s),s+STEP);c.bar(bar(s+STEP),s+2*STEP)
        c.decide_reversal(*samples(s+2*STEP))
        # Tiny positive OPEN cannot represent a positive SHORT target at frozen ATR.
        c.bar(bar(s+3*STEP,op='1',high='1',low='1',close='1'),s+4*STEP)
        self.assertEqual(c.slot,'EMPTY');self.assertEqual(len(c.trades),1)
        self.assertEqual(len(trace(c,'REJECTED_EXECUTION')),1)

    def test_reversal_sizing_rejection_stays_flat(self):
        c=opened();s=contrary(c);c.bar(bar(s),s+STEP);c.bar(bar(s+STEP),s+2*STEP)
        c.decide_reversal(*samples(s+2*STEP))
        c.bar(bar(s+3*STEP,op='10000000',high='10000000',low='10000000',close='10000000'),s+4*STEP)
        self.assertEqual(c.slot,'EMPTY');self.assertEqual(len(c.trades),1)
        self.assertEqual(len(trace(c,'REJECTED_SIZING')),1)

    def test_priority_eth_over_ada_and_new_cross_only_interface(self):
        c=Coordinator('A',rules(),S-FOUR_H)
        c.observe(signals(directions=('NONE','SHORT','LONG')),*samples(S+240000))
        self.assertEqual(c.pending[0].symbol,'ETHUSDT')
        self.assertEqual(trace(c,'DISPOSITION')[-1]['reason'],'BLOCKED_PRIORITY')

    def test_specification_diff_is_policy_only(self):
        a=Coordinator('A',rules(),S-FOUR_H).specification
        b=Coordinator('C2',rules(),S-FOUR_H).specification
        self.assertEqual(a.pop('policy'),'A');self.assertEqual(b.pop('policy'),'C2')
        self.assertEqual(a,b)

    def test_no_fill_slippage_rounding(self):
        c=opened()
        self.assertEqual(c.position.entry.price,D('100.02'))
        self.assertEqual(c.position.planned_stop_risk,c.position.entry.quantity*D('2'))
        self.assertEqual(c.position.entry.fee,c.position.entry.price*c.position.entry.quantity*D('.0005'))

    def test_source_future_data_cannot_change_commitment(self):
        a,b=Coordinator('A',rules(),S-FOUR_H),Coordinator('C2',rules(),S-FOUR_H)
        for c in (a,b):c.observe(signals(),*samples(S+240000))
        self.assertEqual(a.journal,b.journal)
        a.bar(bar(S+STEP),S+2*STEP)
        b.bar(bar(S+STEP,high='110',low='90'),S+2*STEP)
        # Receipt bytes (and therefore hash ancestry) change with OHLC; the
        # precommitment and financial entry fields must not.
        self.assertEqual(trace(a,'ENTRY')[0]['position'],trace(b,'ENTRY')[0]['position'])
        self.assertEqual(trace(a,'ENTRY')[0]['signal'],trace(b,'ENTRY')[0]['signal'])

    def test_other_symbols_cannot_steal_open_slot(self):
        c=opened();s=S+FOUR_H
        for t in range(S+2*STEP,s,STEP):c.bar(bar(t),t+STEP)
        c.observe(signals(s,('SHORT','LONG','LONG')),*samples(s+240000))
        self.assertEqual([x['reason'] for x in trace(c,'DISPOSITION')[-3:]],
                         ['CLOSE_PENDING','BLOCKED_GLOBAL_SLOT','BLOCKED_GLOBAL_SLOT'])

    def test_finite_decimal_and_complete_batch(self):
        for atr in ('NaN','Infinity','-1','0'):
            with self.assertRaises(ValueError):signals(atr=atr)
        c=Coordinator('A',rules(),S-FOUR_H)
        with self.assertRaises(ValueError):c.observe(signals()[:2],*samples(S+240000))

    def test_journal_hash_linkage_and_readonly_bytes(self):
        from research.spec import fingerprint
        c=opened();last=None
        for i,raw in enumerate(c.journal,1):
            record=json.loads(raw);self.assertEqual(record['sequence'],i)
            self.assertEqual(record['previous'],last)
            last=fingerprint(raw.decode())
        copy=json.loads(c.journal[-1]);copy['kind']='TAMPER'
        self.assertNotEqual(json.loads(c.journal[-1])['kind'],'TAMPER')

    def test_failure_after_financial_change_rolls_back(self):
        for failure in ('CLOSE_CONFIRMED','SLOT_RELEASED'):
            c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
            before=c.snapshot(),c.journal
            original=c._event
            def fail(kind, **data):
                original(kind,**data)
                if kind==failure:raise RuntimeError('synthetic fault')
            with patch.object(c,'_event',side_effect=fail):
                with self.assertRaises(RuntimeError):c.bar(bar(s+STEP),s+2*STEP)
            self.assertEqual((c.snapshot(),c.journal),before)
            c.bar(bar(s+STEP),s+2*STEP)
            self.assertEqual(len(c.trades),1)

    def test_decimal_context_does_not_change_reversal_finances(self):
        results=[]
        for precision in (6,28,50):
            with localcontext() as ctx:
                ctx.prec=precision
                c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
                c.bar(bar(s+STEP,op='100.123456789',high='101',low='99'),s+2*STEP)
                c.decide_reversal(*samples(s+2*STEP))
                c.bar(bar(s+3*STEP),s+4*STEP)
                results.append(c.snapshot())
        self.assertEqual(results[0],results[1]);self.assertEqual(results[1],results[2])

    def test_late_close_does_not_move_entry(self):
        c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
        c.bar(bar(s+STEP),s+4*STEP)
        c.decide_reversal(*samples(s+4*STEP));self.assertEqual(c.slot,'EMPTY')

    def test_stop_before_close_cancels(self):
        c=opened();s=contrary(c)
        c.bar(bar(s,low='97'),s+STEP)
        self.assertEqual(c.slot,'EMPTY');self.assertIsNone(c.reversal)
        self.assertEqual(c.trades[0]['reason'],'STOP_LOSS')

    def test_open_gap_precedes_signal_and_cancels(self):
        for op,high,low,reason in [('97','98','96','STOP_LOSS'),('105','106','104','TAKE_PROFIT')]:
            c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
            c.bar(bar(s+STEP,op,high,low,op),s+2*STEP)
            self.assertEqual(c.trades[0]['reason'],reason);self.assertIsNone(c.reversal)

    def test_future_high_low_cannot_override_signal_open(self):
        c=opened();s=contrary(c);c.bar(bar(s),s+STEP)
        c.bar(bar(s+STEP,high='110',low='90'),s+2*STEP)
        self.assertEqual(c.trades[0]['reason'],'OPPOSITE_SIGNAL')

    def test_dual_touch_stop(self):
        c=opened();c.bar(bar(S+2*STEP,high='110',low='90'),S+3*STEP)
        self.assertEqual(c.trades[0]['reason'],'STOP_LOSS');self.assertTrue(c.trades[0]['dual_touch'])

    def test_shared_protection_reference(self):
        c=opened();b=bar(S+2*STEP,low='97')
        expected=process_bar(c.position,b,c.config)
        c.bar(b,S+3*STEP)
        self.assertEqual(D(c.trades[0]['net_pnl']),expected.net_pnl)
        self.assertEqual(D(c.trades[0]['exit']['price']),expected.exit.price)

    def test_retry_idempotent_conflict_atomic(self):
        c=opened();b=bar(S+2*STEP);first=c.bar(b,S+3*STEP)
        before=c.snapshot(),c.journal
        self.assertEqual(c.bar(b,S+3*STEP),first)
        with self.assertRaises(ValueError):c.bar(replace(b,close=D('100.1')),S+3*STEP)
        self.assertEqual((c.snapshot(),c.journal),before)

    def test_missing_candle_cannot_skip(self):
        c=opened();before=c.snapshot(),c.journal
        with self.assertRaises(ValueError):c.bar(bar(S+3*STEP),S+4*STEP)
        self.assertEqual((c.snapshot(),c.journal),before)

    def test_entry_wrong_open_cannot_substitute(self):
        c=Coordinator('A',rules(),S-FOUR_H);c.observe(signals(),*samples(S+240000))
        with self.assertRaises(ValueError):c.bar(bar(S+2*STEP),S+3*STEP)
        self.assertEqual(c.slot,'PENDING')

    def test_receipt_after_acceptance_and_delayed(self):
        for ss in [signals(receipt=S+240000),signals(timing='DELAYED_RECONSTRUCTED'),signals(timing='UNCONFIRMED')]:
            c=Coordinator('A',rules(),S-FOUR_H);c.observe(ss,*samples(S+240000))
            self.assertEqual(c.slot,'EMPTY')

    def test_pre_start_never_executes(self):
        c=Coordinator('A',rules(),S);c.observe(signals(),*samples(S+240000))
        self.assertEqual(c.slot,'EMPTY')

    def test_clock_regression_rolls_back(self):
        c=Coordinator('A',rules(),S-FOUR_H);a,d=samples(S+240000);d['before_ns']=-1
        with self.assertRaises(ValueError):c.observe(signals(),a,d)
        self.assertEqual(c.slot,'EMPTY');self.assertEqual(c.journal,())

    def test_incomplete_candle_rejected(self):
        c=opened()
        with self.assertRaises(ValueError):c.bar(bar(S+2*STEP),S+2*STEP)

    def test_replay_fresh_instance_identical(self):
        a,b=opened(),opened()
        for c in (a,b):
            s=contrary(c);c.bar(bar(s),s+STEP);c.bar(bar(s+STEP),s+2*STEP)
            c.decide_reversal(*samples(s+2*STEP));c.bar(bar(s+3*STEP),s+4*STEP)
        self.assertEqual(a.journal,b.journal);self.assertEqual(a.snapshot(),b.snapshot())

    def test_no_network_sqlite_subprocess_or_file_io_during_coordination(self):
        c=Coordinator('C2',rules(),S-FOUR_H)
        with patch('builtins.open',side_effect=AssertionError('filesystem')), \
             patch.object(Path,'open',side_effect=AssertionError('filesystem')), \
             patch.object(sqlite3,'connect',side_effect=AssertionError('sqlite')), \
             patch.object(socket,'socket',side_effect=AssertionError('network')), \
             patch.object(subprocess,'Popen',side_effect=AssertionError('process')):
            c.observe(signals(),*samples(S+240000));c.bar(bar(S+STEP),S+2*STEP)
        self.assertEqual(c.slot,'OPEN')


if __name__=='__main__':unittest.main()
