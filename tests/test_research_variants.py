"""Synthetic-only A/B decision tests; no saved research runs or result reads."""
from dataclasses import asdict, replace, FrozenInstanceError
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

from backtest.data import Candle
from research.data import aggregate_bucket, SignalOpening
from research.spec import DEV_START, VALIDATION_START, HOLDOUT_START, fingerprint
from research.strategy import EMACrossover, SignalDecision, _issued_window
from research.variants import RibbonStrategy, BreakoutStrategy, VariantDecision
import research.features as f
from test_research_features import candle, view


def changed(snapshot, **values):
    snapshot=replace(snapshot,**values)
    record=asdict(snapshot);record.pop('snapshot_fingerprint')
    return replace(snapshot,snapshot_fingerprint=fingerprint(record))


class VariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prices=[50]*501+[100]*97+[90,110]
        history=tuple(candle(i,prices[i+600]) for i in range(-600,0))
        meta=dict(normalized_4h_fingerprint=fingerprint([c.record() for c in history]))
        digest=fingerprint(meta);meta['history_view_fingerprint']=digest
        cls.hp=f.HistoryPackage('BTCUSDT',history,digest,json.dumps(meta),f._ISSUED)
        patcher=patch.dict(f.HISTORY_HASHES,{'BTCUSDT':digest});patcher.start();cls.addClassCleanup(patcher.stop)
        cls.dv=view(history[-99:]+tuple(candle(i) for i in range((VALIDATION_START-DEV_START)//f.STEP)))
        cls.fv=f.FeatureView(cls.hp,cls.dv)
        cls.snapshot=f.calculate_features(cls.fv.at_open(DEV_START))

    def controlled(self,strategy,snapshot):
        # Explicit test-only feature injection isolates branching; production
        # recomputation is separately exercised without mocks below.
        with patch('research.variants.calculate_features',return_value=snapshot):
            return strategy.evaluate(snapshot)

    def cross(self,direction,ribbon):
        result=[]
        for e in self.snapshot.emas:
            if e.span==9:e=replace(e,previous=D(100),latest=D(101 if direction=='LONG' else 99))
            if e.span==21:e=replace(e,previous=D(100),latest=D(100))
            result.append(e)
        return changed(self.snapshot,emas=tuple(result),ribbon=ribbon)

    def breakout(self,direction,trend,fresh=True):
        b=replace(self.snapshot.breakout,bullish=direction=='LONG',bearish=direction=='SHORT',
            fresh_bullish=direction=='LONG' and fresh,fresh_bearish=direction=='SHORT' and fresh,
            previous_bullish=direction=='LONG' and not fresh,previous_bearish=direction=='SHORT' and not fresh)
        return changed(self.snapshot,breakout=b,close_vs_ema200=trend)

    def test_a_long_short_with_matching_ribbon(self):
        a=RibbonStrategy(self.fv)
        for direction,regime in (('LONG','BULLISH'),('SHORT','BEARISH')):
            d=self.controlled(a,self.cross(direction,regime))
            self.assertEqual(d.signal,direction);self.assertEqual(d.reason,'CROSSOVER_WITH_RIBBON')

    def test_a_wrong_or_equal_ribbon_blocks(self):
        a=RibbonStrategy(self.fv)
        for direction,wrong in (('LONG','BEARISH'),('SHORT','BULLISH'),('LONG','NEUTRAL'),('SHORT','NEUTRAL')):
            d=self.controlled(a,self.cross(direction,wrong))
            self.assertEqual(d.signal,'NONE');self.assertEqual(d.reason,'FILTERED_REGIME')
            self.assertEqual(d.trigger,direction)

    def test_a_alignment_without_cross_or_repeated_cross(self):
        a=RibbonStrategy(self.fv)
        s=self.cross('LONG','BULLISH')
        self.assertEqual(self.controlled(a,s).signal,'LONG')
        emas=tuple(replace(e,previous=e.latest) for e in s.emas)
        later=changed(s,emas=emas,open_time_ms=DEV_START+f.STEP,completed_through_ms=DEV_START+f.STEP-1)
        self.assertEqual(self.controlled(a,later).signal,'NONE')

    def test_a_blocked_cross_not_delayed(self):
        a=RibbonStrategy(self.fv);s=self.cross('LONG','NEUTRAL')
        self.assertEqual(self.controlled(a,s).reason,'FILTERED_REGIME')
        later=changed(s,ribbon='BULLISH',emas=tuple(replace(e,previous=e.latest) for e in s.emas),
            open_time_ms=DEV_START+f.STEP,completed_through_ms=DEV_START+f.STEP-1)
        self.assertEqual(self.controlled(a,later).reason,'NO_CROSSOVER')

    def test_a_latest_ema_equality_is_no_cross(self):
        s=self.cross('LONG','BULLISH')
        s=changed(s,emas=tuple(replace(e,latest=D(100)) if e.span==9 else e for e in s.emas))
        self.assertEqual(self.controlled(RibbonStrategy(self.fv),s).signal,'NONE')

    def test_b_long_short_fresh_matching_trend(self):
        for direction,trend in (('LONG','BULLISH'),('SHORT','BEARISH')):
            d=self.controlled(BreakoutStrategy(self.fv),self.breakout(direction,trend))
            self.assertEqual(d.signal,direction);self.assertEqual(d.reason,'BREAKOUT_WITH_TREND')

    def test_b_wrong_or_equal_trend_blocks(self):
        for direction,trend in (('LONG','BEARISH'),('SHORT','BULLISH'),('LONG','NEUTRAL'),('SHORT','NEUTRAL')):
            d=self.controlled(BreakoutStrategy(self.fv),self.breakout(direction,trend))
            self.assertEqual(d.signal,'NONE');self.assertEqual(d.reason,'FILTERED_TREND')

    def test_b_trend_alone_or_stale_breakout(self):
        b=BreakoutStrategy(self.fv)
        for direction,trend in (('LONG','BULLISH'),('SHORT','BEARISH')):
            s=self.breakout(direction,trend,False)
            self.assertEqual(self.controlled(b,s).reason,'NO_FRESH_BREAKOUT')
            s=changed(s,breakout=replace(s.breakout,bullish=False,bearish=False))
            self.assertEqual(self.controlled(b,s).signal,'NONE')

    def test_b_blocked_breakout_not_delayed(self):
        b=BreakoutStrategy(self.fv)
        self.assertEqual(self.controlled(b,self.breakout('LONG','NEUTRAL')).reason,'FILTERED_TREND')
        later=changed(self.breakout('LONG','BULLISH',False),open_time_ms=DEV_START+f.STEP,
            completed_through_ms=DEV_START+f.STEP-1)
        self.assertEqual(self.controlled(b,later).reason,'NO_FRESH_BREAKOUT')

    def test_typed_output_identity_and_explicit_families(self):
        decisions=[c(self.fv).evaluate(self.snapshot) for c in (RibbonStrategy,BreakoutStrategy)]
        for strategy,d in zip((RibbonStrategy,BreakoutStrategy),decisions):
            self.assertIsInstance(d,SignalDecision);self.assertIsInstance(d,VariantDecision)
            self.assertEqual(d,strategy(self.fv).evaluate(self.snapshot))
            record=asdict(d);digest=record.pop('decision_id')
            self.assertEqual(fingerprint(record),digest)
            self.assertEqual(d.feature_fingerprint,self.snapshot.snapshot_fingerprint)
            with self.assertRaises(FrozenInstanceError):d.signal='SHORT'
        self.assertNotEqual(decisions[0].strategy_family,decisions[1].strategy_family)
        self.assertNotEqual(decisions[0].decision_id,decisions[1].decision_id)

    def test_no_implicit_strategy_or_parameter_override(self):
        for cls in (RibbonStrategy,BreakoutStrategy):
            with self.assertRaises(TypeError):cls()
            with self.assertRaises(PermissionError):cls({})
            with self.assertRaises(TypeError):cls(self.fv,parameters={})

    def test_unauthorized_input_timeframe_split_and_tampering(self):
        for cls in (RibbonStrategy,BreakoutStrategy):
            strategy=cls(self.fv)
            for bad in ({},self.fv,self.fv.at_open(DEV_START)):
                with self.assertRaises(PermissionError):strategy.evaluate(bad)
            with self.assertRaises(PermissionError):strategy.evaluate(changed(self.snapshot,timeframe='1h'))
            for t in (DEV_START-f.STEP,VALIDATION_START,HOLDOUT_START,DEV_START+900000):
                with self.assertRaises(PermissionError):strategy.evaluate(changed(self.snapshot,open_time_ms=t))
            with self.assertRaises(ValueError):strategy.evaluate(changed(self.snapshot,ribbon='NEUTRAL'))

    def test_future_forming_candles_leave_issued_decision_unchanged(self):
        cs=list(self.dv.candles)
        for i in range(99,len(cs)):cs[i]=replace(cs[i],high=D(999),low=D(1),close=D(800),volume=D(777))
        other=f.FeatureView(self.hp,view(cs))
        for cls in (RibbonStrategy,BreakoutStrategy):
            before=cls(self.fv).evaluate(self.snapshot)
            self.assertEqual(before.signal,'LONG')
            after=cls(other).evaluate(f.calculate_features(other.at_open(DEV_START)))
            self.assertEqual(before,after)

    def test_later_15m_component_cannot_affect_opening_signal(self):
        components=[]
        for j in range(16):
            t=DEV_START+j*900000
            components.append(Candle('BTCUSDT','15m',t,t+899999,D(100),D(101),D(99),D(100),D(1)))
        components[7]=replace(components[7],high=D(1000),low=D(1),close=D(999),volume=D(777))
        cs=list(self.dv.candles);cs[99]=aggregate_bucket(components,'4h')
        other=f.FeatureView(self.hp,view(cs))
        for cls in (RibbonStrategy,BreakoutStrategy):
            self.assertEqual(cls(self.fv).evaluate(self.snapshot),cls(other).evaluate(f.calculate_features(other.at_open(DEV_START))))

    def test_existing_adapter_crossover_equivalence(self):
        # Existing adapter is exercised unchanged on the same synthetic 99 rows.
        completed=self.fv.at_open(DEV_START).completed[-99:]
        forming=dict(open='100',high='100',low='100',close='100',volume='0')
        visible=dict(symbol='BTCUSDT',timeframe='4h',open_time_ms=DEV_START,
                     completed=[c.record() for c in completed],forming=forming)
        old_window=_issued_window(SignalOpening('BTCUSDT','4h',DEV_START,DEV_START-1,completed,D(100),fingerprint(visible)))
        before=EMACrossover().evaluate(old_window)
        RibbonStrategy(self.fv).evaluate(self.snapshot);BreakoutStrategy(self.fv).evaluate(self.snapshot)
        self.assertEqual(before,EMACrossover().evaluate(old_window))
        self.assertEqual(before.signal,RibbonStrategy(self.fv).evaluate(self.snapshot).trigger)
