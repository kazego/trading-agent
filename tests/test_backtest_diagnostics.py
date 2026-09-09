import unittest
from decimal import Decimal as D
from types import SimpleNamespace as N
from backtest.diagnostics import *
from backtest.simulation import ExitReason
from backtest.portfolio import Disposition

T=1704067200000

def trade(direction='LONG',net='2',start=T,end=None,symbol='BTCUSDT',reason=ExitReason.TAKE_PROFIT,identity='a'):
    end=start+STEP_MS if end is None else end
    return N(position=N(decision=N(signal=direction,symbol=symbol,decision_id=identity),
        entry=N(price=D(100),reference_price=D(99) if direction=='LONG' else D(101),quantity=D(1),fee=D(1),side='BUY' if direction=='LONG' else 'SELL',candle_open_time_ms=start),
        stop_loss=D(98) if direction=='LONG' else D(102),take_profit=D(104) if direction=='LONG' else D(96),planned_stop_risk=D(4)),
        exit=N(price=D(104) if direction=='LONG' else D(96),reference_price=D(105) if direction=='LONG' else D(95),quantity=D(1),fee=D(1),side='SELL' if direction=='LONG' else 'BUY',candle_open_time_ms=end,phase='INTRABAR'),
        net_pnl=D(net),gross_pnl=D(net)+2,reason=reason)

class DiagnosticsTests(unittest.TestCase):
    def test_direction_and_calendar(self):
        ts=[trade(),trade('SHORT',start=1735689600000)]
        self.assertEqual(set(grouped(ts,lambda t:t.position.decision.signal)),{'LONG','SHORT'})
        self.assertEqual(set(grouped(ts,lambda t:date(t.exit.candle_open_time_ms,'%Y'))),{'2024','2025'})
    def test_costs_exact_and_reconcile(self):
        c=cost_decomposition([trade(),trade('SHORT')],D(1000))
        self.assertEqual((c['fees'],c['recorded_fill_slippage'],c['fixed_fill_reference_pnl']),(D(4),D(4),D(12)))
        bad=trade();bad.net_pnl=D(10)
        with self.assertRaises(ValueError):cost_decomposition([bad],D(1000))
    def test_realized_break_even(self):
        p=payoff([trade(net='4'),trade(net='-2',reason=ExitReason.STOP_LOSS)])
        self.assertEqual(p['realized_win_loss_ratio'],D(2))
        self.assertAlmostEqual(p['realized_break_even_pct'],D(100)/3)
    def test_duration_bounds(self):
        t=trade();self.assertEqual(duration(t),(D(15),D(30)))
        t.exit.phase='OPEN';self.assertEqual(duration(t),(D(15),D(15)))
        t.exit.phase='CLOSE';self.assertEqual(duration(t)[0],D(1799999)/60000)
    def test_percentiles(self):
        d=distribution([0,10,20,30]);self.assertEqual(d['median'],D(15));self.assertEqual(d['p90'],D(27))
        self.assertIsNone(distribution([])['mean'])
    def test_long_excursion_excludes_exit_extremes_and_future(self):
        t=trade();bars={T:N(high=D(103),low=D(99)),T+STEP_MS:N(open=D(101),high=D(999),low=D(1)),T+2*STEP_MS:N(high=D(9999),low=D(0))}
        e=excursion(t,bars);self.assertEqual((e['mfe_price'],e['mae_price']),(D(5),D(1)))
        bars[T+STEP_MS].high=D(99999);self.assertEqual(e,excursion(t,bars))
    def test_short_excursion(self):
        bars={T:N(high=D(102),low=D(97)),T+STEP_MS:N(open=D(99),high=D(999),low=D(1))}
        e=excursion(trade('SHORT'),bars);self.assertEqual((e['mfe_price'],e['mae_price']),(D(5),D(2)))
    def test_same_boundary_exit_no_full_ohlc(self):
        t=trade(end=T);t.exit.phase='OPEN';t.exit.reference_price=D(100)
        e=excursion(t,{T:N(open=D(100),high=D(999),low=D(1))});self.assertEqual(e['mfe_price'],0)
    def test_whipsaw_definitions(self):
        ts=[trade('LONG' if i%2==0 else 'SHORT',net='-2',start=T+i*2*STEP_MS,reason=ExitReason.STOP_LOSS,identity=str(i)) for i in range(3)]
        w=whipsaw(ts);self.assertEqual(w['maximum_stop_run'],3)
        self.assertEqual(w['rapid_opposites']['trade_count'],2);self.assertEqual(w['flip_sequences']['pattern_count'],1)
        self.assertEqual(w['loss_days']['net_pnl'],D(-6))
    def test_blocking(self):
        d=N(signal='LONG',symbol='BTCUSDT')
        bs=[N(open_time_ms=T,position_symbol_before='BTCUSDT',decisions=[N(decision=d,disposition=Disposition.BLOCKED,reason='BLOCKED_OPEN_POSITION')])]
        b=blocking(bs);self.assertEqual(b['occupied_at_open_pct'],100);self.assertEqual(b['actionable'],1)
    def test_concentration(self):
        ts=[trade(net=str(i-10),identity=str(i)) for i in range(20)]
        c=concentration(ts);self.assertEqual(c['worst_5']['net_pnl'],D(-40));self.assertEqual(c['best_5pct']['trades'],1)
    def test_deterministic_serialization(self):
        from backtest.data import _encoded,_hash
        from backtest.portfolio import _canonical
        x=_canonical(stats([trade(),trade('SHORT')]))
        self.assertEqual(_hash(x),_hash(json.loads(_encoded(x))))
