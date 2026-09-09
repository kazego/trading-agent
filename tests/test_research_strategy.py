"""Synthetic-only decisions and short synthetic scheduler regression runs."""
from dataclasses import replace, FrozenInstanceError
from decimal import Decimal as D
import unittest
from unittest.mock import patch

from backtest.clock import OpeningEvent
from backtest.data import Candle, DataValidationError, _hash, _encoded
from backtest.strategy_adapter import FrozenStrategyAdapter
from backtest.portfolio import simulate_portfolio
from research.data import AggregateCandle, SignalOpening, SignalDataView, STEPS, build_view
from research.spec import DEV_START, VALIDATION_START, HOLDOUT_START, STEP_MS
from research.strategy import EMAConfig, EMACrossover, OpeningWindow, _issued_window, _frame
from research.schedule import ResearchSchedule
import test_research_data as data_helpers
import test_backtest_portfolio as portfolio_helpers


def opening(closes=None, tf="15m", t=DEV_START, price=D(100)):
    """Explicit test-only issuance; never used by the normal research path."""
    closes = [D(100)]*99 if closes is None else list(map(D,closes))
    step = STEPS[tf]
    completed = tuple(AggregateCandle("BTCUSDT",tf,t-(len(closes)-i)*step,t-(len(closes)-i-1)*step-1,
        D(100),max(D(100),c),min(D(100),c),c,D(1)) for i,c in enumerate(closes))
    record=dict(symbol="BTCUSDT",timeframe=tf,open_time_ms=t,completed=[c.record() for c in completed],
                forming=dict.fromkeys(("open","high","low","close"),str(price)) | {"volume":"0"})
    return _issued_window(SignalOpening("BTCUSDT",tf,t,t-1,completed,price,_hash(record)))


def baseline_event(window):
    cs=tuple(Candle(**c.record()) for c in window.completed)
    return OpeningEvent(window.symbol,"15m",window.open_time_ms,window.completed_through_ms,window.opening_price,cs,"a"*64)


class ResearchStrategyTests(unittest.TestCase):
    def test_typed_config_validation_and_immutability(self):
        for fast,slow in ((9,9),(21,9),(0,21),(True,21),(9.0,21)):
            with self.assertRaises(ValueError):EMAConfig(fast,slow)
        with self.assertRaises(ValueError):EMACrossover({"fast":9,"slow":21})
        with self.assertRaises(FrozenInstanceError):EMACrossover().config=EMAConfig()

    def test_exact_baseline_long_short_none_equality(self):
        cases=(([100]*97+[90,110],"LONG"),([100]*97+[110,90],"SHORT"),
               ([100]*99,"NONE"),([100]*98+[101],"LONG"),([100]*98+[99],"SHORT"))
        for closes,expected in cases:
            with self.subTest(expected=expected):
                w=opening(closes)
                actual=EMACrossover().evaluate(w)
                self.assertEqual(actual.signal,expected)
                self.assertEqual(actual.signal,FrozenStrategyAdapter().evaluate(baseline_event(w)).signal)

    def test_repeated_rolling_evaluations(self):
        history=[D(100)]*97+[D(90),D(110),D(80),D(120),D(100)]
        for i in range(4):
            w=opening(history[i:i+99],t=DEV_START+i*STEP_MS)
            self.assertEqual(EMACrossover().evaluate(w).signal,FrozenStrategyAdapter().evaluate(baseline_event(w)).signal)
            self.assertEqual(EMACrossover().evaluate(w),EMACrossover().evaluate(w))

    def test_insufficient_visible_history_matches_adapter_rejection(self):
        w=opening([100]*98)
        with self.assertRaises(DataValidationError):EMACrossover().evaluate(w)
        with self.assertRaises(DataValidationError):FrozenStrategyAdapter().evaluate(baseline_event(w))

    def test_exact_frame_and_forming_isolation(self):
        closes=[100]*97+[90,110]
        first=opening(closes,price=D(1))
        second=opening(closes,price=D(10000))
        self.assertEqual(EMACrossover().evaluate(first).signal,EMACrossover().evaluate(second).signal)
        frame=_frame(second)
        self.assertEqual(len(frame),100)
        self.assertEqual(list(frame.iloc[-1][['open','high','low','close','volume']]),[10000,10000,10000,10000,0])
        self.assertEqual(frame.iloc[-3]['close'],90)
        self.assertEqual(frame.iloc[-2]['close'],110)

    def test_decision_identity_timeframe_symbol_time(self):
        a=EMACrossover()
        self.assertNotEqual(a.evaluate(opening()).decision_id,a.evaluate(opening(tf='1h')).decision_id)
        self.assertNotEqual(a.evaluate(opening()).decision_id,a.evaluate(opening(t=DEV_START+STEP_MS)).decision_id)
        d=a.evaluate(opening())
        self.assertEqual(d.strategy_family,'EMA_CROSSOVER')
        self.assertEqual(d.parameters,EMAConfig())
        self.assertEqual(d.reason,'NO_CROSS')

    def test_raw_and_unauthorized_windows_rejected(self):
        w=opening()
        with self.assertRaises(PermissionError):EMACrossover().evaluate(replace(w,_issuer=None))
        with self.assertRaises(PermissionError):EMACrossover().evaluate([])
        for t in (VALIDATION_START,HOLDOUT_START):
            with self.assertRaises(PermissionError):EMACrossover().evaluate(opening(t=t))

    def test_future_history_and_fingerprint_rejected(self):
        w=opening()
        with self.assertRaises(DataValidationError):EMACrossover().evaluate(replace(w,completed=w.completed[1:]+(w.completed[-1],)))
        with self.assertRaises(DataValidationError):EMACrossover().evaluate(replace(w,visible_fingerprint='0'*64))

    def test_input_and_dataframe_not_mutated(self):
        w=opening();before=w.completed
        f=_frame(w);f.loc[0,'close']=99999
        self.assertEqual(_frame(w).loc[0,'close'],100)
        EMACrossover().evaluate(w)
        self.assertEqual(before,w.completed)


class ResearchScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=data_helpers.dataset([data_helpers.candle(t) for t in range(DEV_START-1584*STEP_MS,VALIDATION_START,STEP_MS)])
        cls.views={tf:build_view(cls.source,'BTCUSDT',tf,data_helpers.request(tf)) for tf in STEPS}

    def test_only_signal_boundaries(self):
        for tf,step in STEPS.items():
            schedule=ResearchSchedule({'BTCUSDT':self.views[tf]},EMACrossover())
            times=[DEV_START+i*STEP_MS for i in range(32)]
            with patch.object(EMACrossover,'evaluate',autospec=True,return_value='synthetic') as evaluate:
                results=[schedule.evaluate('BTCUSDT',t) for t in times]
                self.assertEqual(evaluate.call_count,sum(t%step==0 for t in times))
                self.assertEqual([r is not None for r in results],[t%step==0 for t in times])

    def test_four_hour_08_12_16_windows(self):
        schedule=ResearchSchedule({'BTCUSDT':self.views['4h']},EMACrossover())
        for hour in (8,12,16):
            t=DEV_START+hour*3600000
            w=schedule.window_at('BTCUSDT',t)
            self.assertEqual(w.completed[-1].open_time_ms,t-4*3600000)
            self.assertTrue(all(c.close_time_ms<t for c in w.completed))
            self.assertIsNone(schedule.window_at('BTCUSDT',t+STEP_MS))

    def test_future_components_do_not_change_decision(self):
        source=self.source
        cs=list(source.candles)
        noon=DEV_START+12*3600000
        for i,c in enumerate(cs):
            if noon<=c.open_time_ms<noon+4*3600000:
                cs[i]=replace(c,high=D(999),low=D(1),close=D(500),volume=D(900))
            if c.open_time_ms==noon+4*3600000:
                cs[i]=replace(c,open=D(101))
        changed=data_helpers.dataset(cs)
        view=build_view(changed,'BTCUSDT','4h',data_helpers.request('4h'))
        before=ResearchSchedule({'BTCUSDT':self.views['4h']},EMACrossover()).evaluate('BTCUSDT',noon)
        after=ResearchSchedule({'BTCUSDT':view},EMACrossover()).evaluate('BTCUSDT',noon)
        self.assertEqual(before,after)
        self.assertNotEqual(view.fingerprint,self.views['4h'].fingerprint)

    def test_policy_and_raw_view_denial(self):
        with self.assertRaises(ValueError):ResearchSchedule({'BTCUSDT':[]},EMACrossover())
        schedule=ResearchSchedule({'BTCUSDT':self.views['1h']},EMACrossover())
        for t in (DEV_START-3600000,VALIDATION_START,HOLDOUT_START):
            with self.assertRaises(PermissionError):schedule.evaluate('BTCUSDT',t)
        meta=self.views['1h'].provenance;meta['role']='LOCKED_PREVIOUSLY_OBSERVED_VALIDATION'
        with self.assertRaises(ValueError):ResearchSchedule({'BTCUSDT':SignalDataView(self.views['1h'].candles,_encoded(meta).decode())},EMACrossover())

    def test_execution_bridge_metadata(self):
        schedule=ResearchSchedule({'BTCUSDT':self.views['1h']},EMACrossover())
        event=OpeningEvent('BTCUSDT','15m',DEV_START,DEV_START-1,D(100),(),self.source.fingerprint)
        d=schedule(event)
        self.assertEqual(d.timeframe,'15m');self.assertEqual(d.signal_timeframe,'1h')
        self.assertEqual(d.signal_decision_id,schedule.evaluate('BTCUSDT',DEV_START).decision_id)
        self.assertIsNone(schedule(replace(event,open_time_ms=DEV_START+STEP_MS,completed_through_ms=DEV_START+STEP_MS-1)))
        with self.assertRaises(ValueError):schedule(replace(event,dataset_fingerprint='0'*64))


class SchedulerSeamTests(unittest.TestCase):
    def test_explicit_baseline_provider_matches_default_exactly(self):
        data=portfolio_helpers.datasets();rules=portfolio_helpers.rules()
        default=simulate_portfolio(data,rules)
        supplied=simulate_portfolio(data,rules,decision_provider=FrozenStrategyAdapter().evaluate)
        self.assertEqual(default,supplied);self.assertEqual(default.fingerprint,supplied.fingerprint)

    def test_no_decision_does_not_instantiate_baseline_adapter(self):
        with patch('backtest.portfolio.FrozenStrategyAdapter',side_effect=AssertionError('Implicit selection')):
            result=simulate_portfolio(portfolio_helpers.datasets(),portfolio_helpers.rules(),decision_provider=lambda e:None)
        self.assertEqual(result.trades,())
        self.assertTrue(all(b.decisions==() for b in result.boundaries))

    def test_management_continues_without_signals(self):
        data=portfolio_helpers.datasets(bars={'BTCUSDT':[(100,101,99,100),(100,101,98,100),(100,101,99,100)]})
        adapter=FrozenStrategyAdapter()
        def provider(event):
            return adapter.evaluate(event) if event.open_time_ms==portfolio_helpers.START else None
        result=simulate_portfolio(data,portfolio_helpers.rules(),decision_provider=provider)
        self.assertEqual(len(result.trades),1)
        self.assertEqual(result.boundaries[1].closed.reason.value,'STOP_LOSS')
        self.assertEqual(result.boundaries[1].decisions,())

    def test_bad_supplied_decision_fails_before_execution(self):
        adapter=FrozenStrategyAdapter()
        def provider(event):return replace(adapter.evaluate(event),open_time_ms=event.open_time_ms+STEP_MS)
        with self.assertRaises(DataValidationError):simulate_portfolio(portfolio_helpers.datasets(),portfolio_helpers.rules(),decision_provider=provider)
