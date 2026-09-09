"""Synthetic-only feature tests. No saved development results or market files."""
from dataclasses import asdict, replace, FrozenInstanceError
from decimal import Decimal as D, localcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from research.data import AggregateCandle, SignalDataView, VERSION as DATA_VERSION
from research.spec import DEV_START, VALIDATION_START, HOLDOUT_START, fingerprint, policy
import research.features as f


def candle(index, price=100):
    t=DEV_START+index*f.STEP;p=D(str(price))
    return AggregateCandle('BTCUSDT','4h',t,t+f.STEP-1,p,p+1,p-1,p,D(1))


def window(prices=None):
    cs=tuple(candle(i,(prices or [100]*600)[i+600]) for i in range(-600,0))
    return f._window('BTCUSDT',DEV_START,cs)


def view(cs):
    meta=dict(role='DEVELOPMENT',symbol='BTCUSDT',signal_timeframe='4h',execution_resolution='15m',
        start_ms=DEV_START,end_ms=VALIDATION_START,warmup_start_ms=DEV_START-99*f.STEP,
        warmup_signal_candles=99,aggregation_version=DATA_VERSION,policy_fingerprint=fingerprint(policy()),
        normalized_aggregate_fingerprint=fingerprint([c.record() for c in cs]))
    meta['aggregate_fingerprint']=fingerprint(meta)
    return SignalDataView(tuple(cs),json.dumps(meta))


class CalculationTests(unittest.TestCase):
    def test_immutable_types_and_exact_parameters(self):
        w=window();s=f.calculate_features(w)
        for obj,key,value in ((w,'symbol','ETHUSDT'),(s,'atr14',D(8)),(s.parameters,'atr_period',12),(s.emas[0],'span',8)):
            with self.assertRaises(FrozenInstanceError):setattr(obj,key,value)
        for kwargs in (dict(ema_spans=(9,20,21,50,100,201)),dict(atr_period=15),dict(breakout_lookback=21),dict(trigger_rows=100),dict(regime_rows=599),dict(atr_period=14.0)):
            with self.assertRaises(ValueError):f.FeatureParameters(**kwargs)
        self.assertEqual(tuple(e.span for e in s.emas),(9,20,21,50,100,200))

    def test_ema_exact_rolling_adjust_false_and_trigger_mapping(self):
        prices=[100+(i%11) for i in range(600)]
        s=f.calculate_features(window(prices))
        for e in s.emas:
            direct=pd.Series(prices[-e.completed_rows:],dtype='float64').ewm(span=e.span,adjust=False).mean()
            self.assertEqual(e.latest,D(str(direct.iloc[-1])))
            self.assertEqual(e.previous,D(str(direct.iloc[-2])))
        for forming in (1,100000):
            series=pd.Series(prices[-99:]+[forming],dtype='float64')
            for e in (s.emas[0],s.emas[2]):
                direct=series.ewm(span=e.span,adjust=False).mean()
                self.assertEqual((e.previous,e.latest),(D(str(direct.iloc[-3])),D(str(direct.iloc[-2]))))

    def test_ribbon_and_trend_relations(self):
        for prices,expected in ((list(range(10,610)),'BULLISH'),(list(range(610,10,-1)),'BEARISH'),([100]*600,'NEUTRAL')):
            s=f.calculate_features(window(prices))
            self.assertEqual(s.ribbon,expected);self.assertEqual(s.close_vs_ema200,expected)
        for values in ([4,3,3,1],[1,2,2,4],[4,2,3,1]):self.assertEqual(f._ribbon(values),'NEUTRAL')

    def test_breakout_excludes_tested_bar_and_equality(self):
        w=window();cs=list(w.completed)
        cs[-1]=replace(cs[-1],close=D(102),high=D(999))
        b=f.calculate_features(f._window(w.symbol,w.open_time_ms,cs)).breakout
        self.assertEqual(b.preceding_high,D(101));self.assertTrue(b.fresh_bullish);self.assertFalse(b.bearish)
        cs[-1]=replace(cs[-1],close=D(101))
        self.assertFalse(f.calculate_features(f._window(w.symbol,w.open_time_ms,cs)).breakout.bullish)

    def test_fresh_bearish_and_repeated_breakout(self):
        cs=list(window().completed)
        cs[-1]=replace(cs[-1],close=D(98),low=D(97))
        self.assertTrue(f.calculate_features(f._window('BTCUSDT',DEV_START,cs)).breakout.fresh_bearish)
        cs[-2]=replace(cs[-2],close=D(98),low=D(97))
        cs[-1]=replace(cs[-1],close=D(96),low=D(95))
        b=f.calculate_features(f._window('BTCUSDT',DEV_START,cs)).breakout
        self.assertTrue(b.bearish and b.previous_bearish);self.assertFalse(b.fresh_bearish)
        cs=list(window().completed)
        cs[-2]=replace(cs[-2],close=D(102),high=D(103))
        cs[-1]=replace(cs[-1],close=D(104),high=D(105))
        b=f.calculate_features(f._window('BTCUSDT',DEV_START,cs)).breakout
        self.assertTrue(b.bullish and b.previous_bullish);self.assertFalse(b.fresh_bullish)

    def test_atr_wilder_gap_edges_seed_and_recurrence(self):
        cs=[candle(i) for i in range(-100,0)]
        self.assertEqual(f._atr(cs),D(2))
        # First observed TR is an upward gap of 11 instead of 2.
        cs[1]=candle(-99,110)
        # Returning to 100 makes the second TR a downward gap of 11.
        with localcontext() as ctx:
            ctx.prec=50
            expected=(D(11)+D(11)+D(2)*12)/14
            for _ in range(85):expected=(13*expected+2)/14
        self.assertEqual(f._atr(cs),expected)
        with self.assertRaises(ValueError):f._atr(cs[1:])

    def test_fingerprint_context_independence_and_no_mutation(self):
        w=window([100+i%9 for i in range(600)]);before=tuple(c.record() for c in w.completed)
        s=f.calculate_features(w)
        with localcontext() as ctx:
            ctx.prec=3
            self.assertEqual(s,f.calculate_features(w))
        record=asdict(s);digest=record.pop('snapshot_fingerprint')
        self.assertEqual(fingerprint(record),digest)
        self.assertEqual(before,tuple(c.record() for c in w.completed))

    def test_window_tampering_and_unissued_input_denied(self):
        w=window()
        with self.assertRaises(PermissionError):f.calculate_features(replace(w,_issuer=None))
        with self.assertRaises(ValueError):f.calculate_features(replace(w,visible_fingerprint='0'*64))
        with self.assertRaises(ValueError):f.calculate_features(replace(w,completed=w.completed[:-1]))
        with self.assertRaises(PermissionError):f.calculate_features([])
        with self.assertRaises(ValueError):f.calculate_features(w,parameters={})

    def test_no_current_open_or_forming_data_in_window(self):
        w=window()
        self.assertEqual(w.completed[-1].close_time_ms,DEV_START-1)
        self.assertFalse(hasattr(w,'opening_price'))
        self.assertFalse(hasattr(w,'current_candle'))
        self.assertFalse(hasattr(f.calculate_features(w),'signal'))


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history=tuple(candle(i) for i in range(-600,0))
        cls.development=view(tuple(candle(i) for i in range(-99,(VALIDATION_START-DEV_START)//f.STEP)))
        cls.meta=dict(history_start_ms=f.START,history_end_ms=DEV_START,role='HISTORY_ONLY',
            evaluable_boundaries=[],pre2024_evaluation_allowed=False,development_interval=[DEV_START,VALIDATION_START],
            normalized_4h_fingerprint=fingerprint([c.record() for c in cls.history]))
        cls.hh=fingerprint(cls.meta);cls.meta['history_view_fingerprint']=cls.hh
        cls.manifest=dict(symbols={'BTCUSDT':cls.meta})
        cls.mh=fingerprint(cls.manifest);cls.manifest['fingerprint']=cls.mh

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        (self.root/'BTCUSDT-4h-history.json').write_text(json.dumps(dict(provenance=self.meta,candles=[c.record() for c in self.history])))
        self.hashpatch=patch.dict(f.HISTORY_HASHES,{'BTCUSDT':self.hh});self.hashpatch.start();self.addCleanup(self.hashpatch.stop)
        self.manifestpatch=patch.object(f,'MANIFEST_HASH',self.mh);self.manifestpatch.start();self.addCleanup(self.manifestpatch.stop)
        self.h=f.load_history(self.root,'BTCUSDT')

    def test_600_history_first_boundary_and_rolling(self):
        v=f.FeatureView(self.h,self.development)
        for index in (0,1,601,2195):
            w=v.at_open(DEV_START+index*f.STEP)
            self.assertEqual(len(w.completed),600)
            self.assertEqual(w.completed[-1].close_time_ms,w.open_time_ms-1)
        self.assertEqual(v.at_open(DEV_START).completed,self.history)

    def test_future_and_forming_mutation_cannot_change_snapshot(self):
        original=f.FeatureView(self.h,self.development)
        cs=list(self.development.candles)
        for i in range(99,len(cs)):
            cs[i]=replace(cs[i],high=D(999),low=D(1),close=D(800),volume=D(555))
        changed=f.FeatureView(self.h,view(cs))
        self.assertNotEqual(original.provenance,changed.provenance)
        self.assertEqual(f.calculate_features(original.at_open(DEV_START)),f.calculate_features(changed.at_open(DEV_START)))

    def test_history_and_future_boundaries_denied(self):
        v=f.FeatureView(self.h,self.development)
        for t in (f.START,DEV_START-f.STEP,DEV_START+900000,VALIDATION_START,HOLDOUT_START):
            with self.assertRaises(PermissionError):v.at_open(t)

    def test_raw_view_and_locked_role_denied(self):
        with self.assertRaises(PermissionError):f.FeatureView(self.h,[])
        with self.assertRaises(PermissionError):f.FeatureView(self.history,self.development)
        for role in ('LOCKED_PREVIOUSLY_OBSERVED_VALIDATION','TRUE_HOLDOUT','HISTORY_ONLY'):
            meta=self.development.provenance;meta['role']=role
            with self.assertRaises((ValueError,PermissionError)):f.FeatureView(self.h,SignalDataView((),json.dumps(meta)))

    def test_manifest_and_history_fingerprint_corruption(self):
        (self.root/'manifest.json').write_text('{}')
        with self.assertRaises(ValueError):f.load_history(self.root,'BTCUSDT')
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        doc=json.loads((self.root/'BTCUSDT-4h-history.json').read_text());doc['candles'][0]['close']='100.5'
        (self.root/'BTCUSDT-4h-history.json').write_text(json.dumps(doc))
        with self.assertRaises(ValueError):f.load_history(self.root,'BTCUSDT')

    def test_verified_history_cannot_be_replaced_after_load(self):
        cs=list(self.h.candles);cs[0]=replace(cs[0],close=D('100.5'))
        with self.assertRaises(ValueError):f.FeatureView(replace(self.h,candles=tuple(cs)),self.development)

    def test_overlap_and_development_integrity_rejected(self):
        cs=list(self.development.candles);cs[0]=replace(cs[0],close=D('100.5'))
        with self.assertRaises(ValueError):f.FeatureView(self.h,view(cs))
        with self.assertRaises(ValueError):f.FeatureView(self.h,replace(self.development,candles=self.development.candles[:-1]))
