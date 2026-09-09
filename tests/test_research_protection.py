"""Offline numerical ATR contracts; synthetic views only, no experiment execution."""
from dataclasses import asdict, replace, FrozenInstanceError
from decimal import Decimal as D, localcontext
import json
import unittest
from unittest.mock import patch

from backtest.clock import OpeningEvent
from backtest.data import Candle, DataValidationError
from backtest.simulation import (SimulationConfig, SymbolRules, CostMode, ExitReason,
    Position, SizingError, open_position, process_bar, close_position, size_quantity)
from research.protection import ATRProtection, ATRPosition
from research.schedule import ResearchSchedule
from research.strategy import EMACrossover
from research.spec import DEV_START, VALIDATION_START, HOLDOUT_START, fingerprint
import research.features as f
from test_research_features import candle, view


def fixture(direction):
    prices=[100]*598+([90,110] if direction=='LONG' else [110,90])
    history=tuple(candle(i,prices[i+600]) for i in range(-600,0))
    meta=dict(normalized_4h_fingerprint=fingerprint([c.record() for c in history]))
    digest=fingerprint(meta);meta['history_view_fingerprint']=digest
    hp=f.HistoryPackage('BTCUSDT',history,digest,json.dumps(meta),f._ISSUED)
    dv=view(history[-99:]+tuple(candle(i) for i in range((VALIDATION_START-DEV_START)//f.STEP)))
    source=dv.provenance
    source.pop('aggregate_fingerprint')
    source['source_15m_dataset_fingerprint']='1'*64
    source['aggregate_fingerprint']=fingerprint(source)
    dv=replace(dv,provenance_json=json.dumps(source))
    with patch.dict(f.HISTORY_HASHES,{'BTCUSDT':digest}):fv=f.FeatureView(hp,dv)
    schedule=ResearchSchedule({'BTCUSDT':dv},EMACrossover())
    event=OpeningEvent('BTCUSDT','15m',DEV_START,DEV_START-1,D(100),(),
                       dv.provenance['source_15m_dataset_fingerprint'])
    return fv,schedule,event,f.calculate_features(fv.at_open(DEV_START))


class ATRProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures={s:fixture(s) for s in ('LONG','SHORT')}
        cls.rules=SymbolRules('BTCUSDT','synthetic-v1',D('.01'),D('.001'),D('.001'),D(10000),D(5))
        cls.config=SimulationConfig()

    def policy(self, direction='LONG', atr=None, event=None):
        fv,schedule,opening,snapshot=self.fixtures[direction]
        event=opening if event is None else event
        if atr is None:return ATRProtection(fv,snapshot,schedule,event)
        # Only numerical branch tests inject ATR. Unmocked binding is tested too.
        snapshot=replace(snapshot,atr14=D(atr))
        record=asdict(snapshot);record.pop('snapshot_fingerprint')
        snapshot=replace(snapshot,snapshot_fingerprint=fingerprint(record))
        with patch('research.protection.calculate_features',return_value=snapshot):
            return ATRProtection(fv,snapshot,schedule,event)

    def position(self, direction='LONG', atr='1', rules=None, config=None, equity=D(1000)):
        fv,schedule,event,snapshot=self.fixtures[direction]
        return open_position(schedule(event),event,equity,rules or self.rules,config or self.config,
                             protection_policy=self.policy(direction,atr))

    def test_independent_ten_usdt_risk_examples(self):
        for atr,stop,qty in [('1','98','5'),('2.5','95','2')]:
            p=self.position(atr=atr)
            self.assertEqual((p.stop_loss,p.entry.quantity),(D(stop),D(qty)))
            self.assertEqual(p.protection.requested_risk,D(10))
            self.assertEqual(p.protection.actual_planned_risk,D(10))
            self.assertEqual(p.protection.risk_utilization_ratio,D(1))

    def test_both_directions_exact_levels(self):
        for side,levels in [('LONG',(98,104)),('SHORT',(102,96))]:
            p=self.position(side)
            self.assertEqual((p.stop_loss,p.take_profit),tuple(map(D,levels)))
            self.assertEqual(p.entry.quantity,D(5))

    def test_non_tick_rounding_away_from_entry(self):
        rules=replace(self.rules,tick_size=D('.3'))
        for side,expected in [('LONG',('97.8','104.1')),('SHORT',('102','96'))]:
            p=self.position(side,rules=rules)
            self.assertEqual((p.stop_loss,p.take_profit),tuple(map(D,expected)))
            self.assertLessEqual(p.planned_stop_risk,D(10))
        self.assertEqual(self.position(rules=rules).entry.quantity,D('4.545'))

    def test_short_non_tick_target_and_stop(self):
        p=self.position('SHORT',atr='1.01',rules=replace(self.rules,tick_size=D('.1')))
        self.assertEqual((p.stop_loss,p.take_profit),(D('102.1'),D('95.9')))
        self.assertEqual(p.entry.quantity,D('4.761'))

    def test_wider_atr_decreases_quantity(self):
        quantities=[self.position(atr=a).entry.quantity for a in ('.1','1','2.5','5')]
        self.assertEqual(quantities,sorted(quantities,reverse=True))

    def test_leverage_cap_lowers_planned_risk(self):
        p=self.position(atr='.01')
        self.assertEqual(p.entry.quantity,D(30))
        self.assertEqual(p.planned_stop_risk,D('.6'))
        self.assertEqual(p.protection.risk_utilization_ratio,D('.06'))

    def test_quantity_step_never_increases_risk(self):
        for step in ('.001','.3','1','3'):
            for side in ('LONG','SHORT'):
                p=self.position(side,atr='1.01',rules=replace(self.rules,quantity_step=D(step)))
                self.assertLessEqual(p.planned_stop_risk,D(10))

    def test_current_equity_not_starting_equity(self):
        p=self.position(equity=D(500))
        self.assertEqual(p.entry.quantity,D('2.5'))
        self.assertEqual(p.protection.requested_risk,D(5))

    def test_fees_margin_and_slippage(self):
        config=SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE,entry_fee_rate=D('.0005'),
                                exit_fee_rate=D('.0005'),slippage_bps=D(2))
        for side,entry,stop,target in [('LONG','100.02','98.02','104.02'),('SHORT','99.98','101.98','95.98')]:
            p=self.position(side,config=config)
            self.assertEqual((p.entry.price,p.stop_loss,p.take_profit),tuple(map(D,(entry,stop,target))))
            self.assertEqual(p.entry.fee,p.entry.price*p.entry.quantity*D('.0005'))
            self.assertEqual(p.entry.quantity,D(5))
        p=self.position(atr='.01',config=config)
        self.assertLessEqual(p.entry.quantity*p.entry.price/3+p.entry.fee,D(1000))
        self.assertLess(p.planned_stop_risk,D(10))

    def test_filters_fail_closed(self):
        for rules in (replace(self.rules,min_quantity=D(6)),replace(self.rules,min_notional=D(501)),
                      replace(self.rules,quantity_step=D(10))):
            with self.assertRaises(SizingError):self.position(rules=rules)
        self.assertEqual(self.position(rules=replace(self.rules,max_quantity=D(2))).entry.quantity,D(2))
        with self.assertRaises(DataValidationError):self.policy().levels(D(100),'LONG',None)

    def test_invalid_protection_prices(self):
        for side in ('LONG','SHORT'):
            with self.assertRaises(SizingError):self.position(side,atr='100')
        with self.assertRaises(SizingError):self.position(rules=replace(self.rules,tick_size=D(200)))

    def test_missing_nonpositive_float_nonfinite_atr(self):
        fv,schedule,event,snapshot=self.fixtures['LONG']
        for atr in (None,D(0),D(-1),D('NaN'),D('Infinity'),1.0):
            with self.subTest(atr=atr),self.assertRaises((ValueError,TypeError)):
                ATRProtection(fv,replace(snapshot,atr14=atr),schedule,event)

    def test_authorization_and_mismatch_denial(self):
        fv,schedule,event,snapshot=self.fixtures['LONG']
        for bad in ({},fv,fv.at_open(DEV_START)):
            with self.assertRaises(PermissionError):ATRProtection(fv,bad,schedule,event)
        with self.assertRaises(PermissionError):ATRProtection({},snapshot,schedule,event)
        for change in (dict(symbol='ETHUSDT'),dict(open_time_ms=DEV_START+f.STEP),
                       dict(timeframe='1h')):
            with self.assertRaises(PermissionError):ATRProtection(fv,replace(snapshot,**change),schedule,event)
        for change in (dict(atr14=snapshot.atr14+D(1)),dict(snapshot_fingerprint='0'*64)):
            with self.assertRaises(DataValidationError):ATRProtection(fv,replace(snapshot,**change),schedule,event)
        for t in (DEV_START-f.STEP,VALIDATION_START,HOLDOUT_START):
            with self.assertRaises(PermissionError):
                ATRProtection(fv,replace(snapshot,open_time_ms=t),schedule,replace(event,open_time_ms=t))

    def test_binding_rejects_changed_entry_or_signal(self):
        fv,schedule,event,snapshot=self.fixtures['LONG'];policy=self.policy()
        for decision,opening in [(replace(schedule(event),signal='SHORT'),event),
                                 (schedule(event),replace(event,opening_price=D(101)))]:
            with self.assertRaises(DataValidationError):
                open_position(decision,opening,D(1000),self.rules,self.config,protection_policy=policy)
        with self.assertRaises(DataValidationError):self.position(config=replace(self.config,risk_fraction=D('.02')))

    def test_unmocked_binding_deterministic_and_immutable(self):
        fv,schedule,event,snapshot=self.fixtures['LONG'];policy=self.policy()
        p=open_position(schedule(event),event,D(1000),self.rules,self.config,protection_policy=policy)
        self.assertIsInstance(p,ATRPosition)
        record=asdict(p.protection);identity=record.pop('identity')
        self.assertEqual(fingerprint(record),identity)
        self.assertEqual(p,self.position(atr=None))
        with self.assertRaises(FrozenInstanceError):policy.atr=D(9)
        with self.assertRaises(FrozenInstanceError):p.stop_loss=D(1)

    def test_execution_never_recomputes_atr_and_levels_stay_frozen(self):
        fv,schedule,event,snapshot=self.fixtures['LONG'];policy=self.policy()
        with patch('research.protection.calculate_features',side_effect=AssertionError('ATR recomputed')):
            p=open_position(schedule(event),event,D(1000),self.rules,self.config,protection_policy=policy)
            before=asdict(p)
            quiet=Candle('BTCUSDT','15m',DEV_START,DEV_START+899999,'100','100','100','100','0')
            self.assertIsNone(process_bar(p,quiet,self.config))
            self.assertEqual(asdict(p),before)

    def test_exit_engine_dual_touch_gaps_tp_eod(self):
        for side in ('LONG','SHORT'):
            p=self.position(side)
            bar=Candle('BTCUSDT','15m',DEV_START,DEV_START+899999,'100','105','95','100','1')
            trade=process_bar(p,bar,self.config)
            self.assertEqual(trade.reason,ExitReason.STOP_LOSS);self.assertTrue(trade.dual_touch)
            self.assertEqual(trade.net_pnl,D(-10))
            adverse='90' if side=='LONG' else '110'
            gap=replace(bar,open=D(adverse),high=D(110),low=D(90))
            self.assertEqual(process_bar(p,gap,self.config).exit.price,D(adverse))
            favorable='110' if side=='LONG' else '90'
            self.assertEqual(process_bar(p,replace(gap,open=D(favorable)),self.config).exit.price,p.take_profit)
            self.assertEqual(close_position(p,D(100),DEV_START,'CLOSE',ExitReason.END_OF_DATA,False,self.config).net_pnl,D(0))

    def test_future_and_forming_changes_leave_protection_unchanged(self):
        fv,schedule,event,snapshot=self.fixtures['LONG']
        # Rebuild only synthetic input; completed history and known open are identical.
        original=schedule._views['BTCUSDT']
        cs=list(original.candles)
        for i in range(99,len(cs)):
            cs[i]=replace(cs[i],high=D(999),low=D(1),close=D(800),volume=D(777))
        altered=replace(original,candles=tuple(cs))
        meta=altered.provenance;meta.pop('aggregate_fingerprint')
        meta['normalized_aggregate_fingerprint']=fingerprint([c.record() for c in cs])
        meta['aggregate_fingerprint']=fingerprint(meta)
        altered=replace(altered,provenance_json=json.dumps(meta))
        # Existing issued view remains immutable; new schedule sees only the same opening.
        other=ResearchSchedule({'BTCUSDT':altered},EMACrossover())
        self.assertEqual(ATRProtection(fv,snapshot,other,event),self.policy())
        p=self.position()
        later=replace(snapshot,atr14=snapshot.atr14*10)
        self.assertNotEqual(later.atr14,snapshot.atr14)
        self.assertEqual((p.stop_loss,p.take_profit),(D(98),D(104)))

    def test_source_mismatch_and_no_signal_rejected(self):
        fv,schedule,event,snapshot=self.fixtures['LONG']
        with self.assertRaises(DataValidationError):
            ATRProtection(fv,snapshot,schedule,replace(event,dataset_fingerprint='2'*64))
        # Later synthetic opening has no fresh crossover.
        later=replace(event,open_time_ms=DEV_START+f.STEP,completed_through_ms=DEV_START+f.STEP-1)
        snap=f.calculate_features(fv.at_open(later.open_time_ms))
        self.assertEqual(schedule(later).signal,'NONE')
        with self.assertRaises(DataValidationError):ATRProtection(fv,snap,schedule,later)

    def test_default_fixed_path_unchanged(self):
        fv,schedule,event,snapshot=self.fixtures['LONG']
        p=open_position(schedule(event),event,D(1000),self.rules,self.config)
        self.assertIs(type(p),Position)
        self.assertEqual((p.stop_loss,p.take_profit,p.entry.quantity),(D('98.5'),D(103),D('6.666')))
        self.assertEqual(p,open_position(schedule(event),event,D(1000),self.rules,self.config,protection_policy=None))
        self.assertNotIn('protection',asdict(p))

    def test_decimal_context_determinism(self):
        before=self.position(atr='1.2345')
        with localcontext() as ctx:
            ctx.prec=6
            after=self.position(atr='1.2345')
        self.assertEqual(before,after)
