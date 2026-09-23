"""B3 synthetic arithmetic/provenance checks; never operates a real paper run."""
import ast
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D, localcontext
from pathlib import Path
import unittest
from unittest.mock import patch

import test_paper_acceptance as b2
import test_paper_foundation as b1
from backtest.data import Candle, SYMBOLS
from backtest.simulation import SymbolRules
from paper.acceptance import AcceptanceBinding, BoundaryBatch, ProspectiveGate, decide
from paper.model import DraftManifest, encoded, fingerprint, parse
from paper.reservations import AcceptanceStore
from paper.market import RuleSnapshot, CandleEvidence, CandleSource
from paper.settlement import ConfirmedEntry, atr_levels, settle, protect, implementation

T, E = b1.T, b1.T + 900000


def rules(**changes):
    values = dict(version='SYNTHETIC_ONLY', tick_size=D('.01'), quantity_step=D('.001'),
                  min_quantity=D('.001'), max_quantity=D('100000'), min_notional=D('5'))
    values.update(changes)
    return RuleSnapshot('SYNTHETIC_ONLY', 'f'*64, tuple(SymbolRules(symbol=s, **values) for s in SYMBOLS))


def pending(snapshot=None, signals=('LONG', 'SHORT', 'LONG'), atr='1'):
    snapshot = snapshot or rules()
    item = b2.item(signals=signals, atr=(atr,)*3)
    binding = AcceptanceBinding(DraftManifest(item.source, rules_fingerprint=snapshot.id),
                                ProspectiveGate(T-14400000, 'b'*64))
    result = decide(binding, BoundaryBatch.from_evidence(item), accepted_ms=T+240000, slot='EMPTY')
    confirmation = AcceptanceStore._confirmation(result['reservation'], T+240001, 1000000)
    dispositions = parse(encoded(result['dispositions']))
    for d in dispositions:
        if d['symbol'] == result['reservation']['symbol']:
            d['reason'] = 'PENDING_ENTRY'
    result.update(confirmation=confirmation, reservation_state='PENDING_ENTRY', effective_dispositions=dispositions)
    return ConfirmedEntry(binding, item, encoded(result))


def candle(p, *, time=E, open='100', high='101', low='99', close='100', volume='1', symbol=None):
    c = Candle(symbol or p.symbol, '15m', time, time+899999, open, high, low, close, volume)
    return CandleEvidence(p.binding.draft.source, c, fingerprint(c.record()), time+900000)


class EntryTests(unittest.TestCase):
    def test_exact_candle_no_substitution(self):
        p = pending()
        for time in (E-900000, E+900000):
            result = settle(p, candle(p, time=time), rules())
            self.assertEqual((result.status, result.slot_state), ('UNRESOLVED', 'UNRESOLVED'))
        self.assertEqual(settle(p, candle(p), rules()).status, 'OPEN')

    def test_missing_entry_retains_exposure(self):
        result = settle(pending(), None, rules())
        self.assertEqual(result.code, 'MISSING_ENTRY_CANDLE')
        self.assertEqual(result.slot_state, 'UNRESOLVED')

    def test_wrong_symbol_source_and_rules(self):
        p = pending(); evidence = candle(p)
        for changed in (candle(p, symbol='ETHUSDT'),
                        replace(evidence, source=replace(evidence.source, manifest='a'*64))):
            self.assertEqual(settle(p, changed, rules()).status, 'UNRESOLVED')
        self.assertEqual(settle(p, evidence, rules(tick_size=D('.1'))).code, 'RULE_BINDING_MISMATCH')

    def test_long_entry_risk_and_fee(self):
        p = pending(); pos = settle(p, candle(p), rules()).position
        self.assertEqual((pos.entry.price, pos.entry.quantity), (D('100.02'), D('5')))
        self.assertEqual((pos.stop_loss, pos.take_profit), (D('98.02'), D('104.02')))
        self.assertEqual(pos.entry.fee, D('.25005'))
        self.assertEqual((pos.requested_risk, pos.planned_stop_risk, pos.utilization), (D(10), D(10), D(1)))

    def test_short_entry_risk_and_fee(self):
        p = pending(signals=('SHORT',)*3); pos = settle(p, candle(p), rules()).position
        self.assertEqual((pos.entry.price, pos.entry.quantity), (D('99.98'), D('5')))
        self.assertEqual((pos.stop_loss, pos.take_profit), (D('101.98'), D('95.98')))
        self.assertEqual(pos.entry.fee, D('.24995'))

    def test_future_hlcv_cannot_change_entry_financials(self):
        p = pending()
        a = settle(p, candle(p), rules()).position
        b = settle(p, candle(p, high='1000', low='1', close='500', volume='999'), rules()).position
        for field in ('entry', 'stop_loss', 'take_profit', 'requested_risk', 'planned_stop_risk', 'utilization'):
            self.assertEqual(getattr(a, field), getattr(b, field))
        self.assertNotEqual(a.entry_evidence.id, b.entry_evidence.id)
        self.assertEqual(p.id, b.pending.id)

    def test_historical_rounding_golden_vectors(self):
        rule = rules(tick_size=D('.3')).rules[0]
        self.assertEqual(atr_levels(D(100), 'LONG', D(1), rule), (D('97.8'), D('104.1')))
        self.assertEqual(atr_levels(D(100), 'SHORT', D(1), rule), (D('102'), D('96')))
        self.assertEqual(atr_levels(D(100), 'SHORT', D('1.01'), rules(tick_size=D('.1')).rules[0]),
                         (D('102.1'), D('95.9')))

    def test_rounded_distance_sizing_downward(self):
        snapshot = rules(tick_size=D('.3'), quantity_step=D('.01'))
        p = pending(snapshot)
        pos = settle(p, candle(p), snapshot).position
        self.assertEqual(pos.stop_loss, D('97.8'))
        self.assertEqual(pos.entry.price-pos.stop_loss, D('2.22'))
        self.assertEqual(pos.entry.quantity, D('4.50'))
        self.assertEqual(pos.planned_stop_risk, D('9.99'))
        self.assertEqual(pos.utilization, D('.999'))

    def test_independent_risk_examples(self):
        for atr, qty in [('1', '5'), ('2.5', '2')]:
            p = pending(atr=atr); pos = settle(p, candle(p), rules()).position
            self.assertEqual(pos.entry.quantity, D(qty))
            self.assertEqual(pos.planned_stop_risk, D(10))

    def test_margin_includes_entry_fee_and_caps_leverage(self):
        snapshot = rules(tick_size=D('.0001')); p = pending(snapshot, atr='0.001')
        pos = settle(p, candle(p), snapshot).position
        notional = pos.entry.quantity*pos.entry.price
        self.assertLessEqual(notional, D(3000))
        self.assertLessEqual(notional/D(3)+pos.entry.fee, D(1000))
        next_qty = pos.entry.quantity + D('.001')
        self.assertGreater(next_qty*pos.entry.price*(D(1)/D(3)+D('.0005')), D(1000))
        self.assertLess(pos.utilization, D(1))

    def test_max_quantity_reduces_utilization(self):
        snapshot = rules(max_quantity=D('2')); p = pending(snapshot)
        pos = settle(p, candle(p), snapshot).position
        self.assertEqual((pos.entry.quantity, pos.planned_stop_risk, pos.utilization), (D(2), D(4), D('.4')))

    def test_minimum_constraints_terminal_rejection(self):
        for change in ({'min_quantity': D('6')}, {'min_notional': D('10000')}, {'quantity_step': D('10')}):
            snapshot = rules(**change); p = pending(snapshot)
            result = settle(p, candle(p), snapshot)
            self.assertEqual((result.status, result.slot_state), ('REJECTED_SIZING', 'FREE'))

    def test_invalid_levels_terminal_rejection(self):
        for signals in (('LONG',)*3, ('SHORT',)*3):
            p = pending(signals=signals, atr='100')
            result = settle(p, candle(p), rules())
            self.assertEqual((result.status, result.slot_state), ('REJECTED_EXECUTION', 'FREE'))

    def test_rejection_never_exposes_lower_priority(self):
        for signals, selected, blocked in [(('LONG',)*3, 'BTCUSDT', 2), (('NONE', 'SHORT', 'LONG'), 'ETHUSDT', 1)]:
            snapshot = rules(min_notional=D('10000')); p = pending(snapshot, signals=signals)
            original = p.result
            result = settle(p, candle(p), snapshot)
            self.assertEqual(result.status, 'REJECTED_SIZING')
            self.assertEqual(p.symbol, selected)
            self.assertEqual(p.result, original)
            self.assertEqual(sum(d['reason'] == 'BLOCKED_PRIORITY' for d in parse(p.result)['effective_dispositions']), blocked)
            self.assertEqual(settle(p, candle(p), snapshot).id, result.id)

    def test_missing_late_or_blocked_confirmation_rejected(self):
        p = pending()
        for durable, elapsed in [(T+840001, 1000000), (T+239999, 1000000), (T+240001, 5000000)]:
            result = parse(p.result)
            result['confirmation'] = AcceptanceStore._confirmation(p.reservation, durable, elapsed)
            with self.assertRaises(ValueError): ConfirmedEntry(p.binding, p.evidence, encoded(result))
        result = parse(p.result); result['confirmation'] = None
        with self.assertRaises(ValueError): ConfirmedEntry(p.binding, p.evidence, encoded(result))

    def test_tampered_selected_instruction_rejected(self):
        p = pending()
        for key, value in [('symbol', 'ETHUSDT'), ('atr14', '2'), ('entry_boundary_ms', E+900000)]:
            result = parse(p.result); result['reservation'][key] = value
            with self.assertRaises(ValueError): ConfirmedEntry(p.binding, p.evidence, encoded(result))


class ProtectionTests(unittest.TestCase):
    def position(self, direction='LONG', **bar):
        p = pending(signals=(direction,)*3)
        e = candle(p, **bar)
        return settle(p, e, rules()).position, e

    def assert_accounting(self, trade):
        p = trade.position
        factor = D('.9998') if p.pending.signal == 'LONG' else D('1.0002')
        self.assertEqual(trade.exit.price, trade.exit.reference_price*factor)
        self.assertEqual(trade.exit.fee, trade.exit.price*trade.exit.quantity*D('.0005'))
        gross = (trade.exit.price-p.entry.price)*p.entry.quantity*(1 if p.pending.signal == 'LONG' else -1)
        self.assertEqual(trade.gross_pnl, gross)
        self.assertEqual(trade.net_pnl, gross-p.entry.fee-trade.exit.fee)
        self.assertEqual(trade.entry_slippage_cost, abs(p.entry.price-p.entry.reference_price)*p.entry.quantity)
        self.assertEqual(trade.exit_slippage_cost, abs(trade.exit.price-trade.exit.reference_price)*p.entry.quantity)

    def test_entry_bar_long_exact_stop_touch(self):
        pos, e = self.position(low='98.02')
        result = protect(pos, e)
        self.assertEqual((result.status, result.trade.reason, result.trade.exit.phase), ('CLOSED', 'STOP_LOSS', 'INTRABAR'))
        self.assertEqual(result.trade.exit.candle_open_time_ms, E)
        self.assert_accounting(result.trade)

    def test_entry_bar_long_exact_target_touch(self):
        pos, e = self.position(high='104.02')
        trade = protect(pos, e).trade
        self.assertEqual(trade.reason, 'TAKE_PROFIT')
        self.assertEqual(trade.exit.reference_price, D('104.02'))
        self.assert_accounting(trade)

    def test_entry_bar_short_exact_stop_and_target(self):
        for bar, reason in [({'high': '101.98'}, 'STOP_LOSS'), ({'low': '95.98'}, 'TAKE_PROFIT')]:
            pos, e = self.position('SHORT', **bar)
            trade = protect(pos, e).trade
            self.assertEqual(trade.reason, reason)
            self.assert_accounting(trade)

    def test_both_directions_dual_touch_stop_first(self):
        for direction in ('LONG', 'SHORT'):
            pos, e = self.position(direction, high='105', low='95')
            trade = protect(pos, e).trade
            self.assertEqual(trade.reason, 'STOP_LOSS')
            self.assertTrue(trade.dual_touch)
            self.assertEqual(trade.exit.reference_price, pos.stop_loss)
            self.assert_accounting(trade)

    def test_adverse_stop_gaps(self):
        for direction, price in [('LONG', '90'), ('SHORT', '110')]:
            pos, e = self.position(direction)
            pos = protect(pos, e).position
            gap = candle(pos.pending, time=E+900000, open=price, high=price, low=price, close=price)
            trade = protect(pos, gap).trade
            self.assertEqual((trade.reason, trade.exit.phase, trade.exit.reference_price), ('STOP_LOSS', 'OPEN', D(price)))
            self.assertFalse(trade.dual_touch)
            self.assert_accounting(trade)

    def test_target_gaps_no_favorable_improvement(self):
        for direction, price in [('LONG', '110'), ('SHORT', '90')]:
            pos, e = self.position(direction); pos = protect(pos, e).position
            gap = candle(pos.pending, time=E+900000, open=price, high='120', low='80', close=price)
            trade = protect(pos, gap).trade
            self.assertEqual((trade.reason, trade.exit.phase), ('TAKE_PROFIT', 'OPEN'))
            self.assertEqual(trade.exit.reference_price, pos.take_profit)
            self.assertFalse(trade.dual_touch)  # Opening chronology resolves the later ambiguous range.
            self.assert_accounting(trade)

    def test_no_end_of_data_or_worker_stop_close(self):
        pos, e = self.position(); result = protect(pos, e)
        self.assertEqual(result.position.last_processed_ms, E)
        missing = protect(result.position, None)
        self.assertEqual((missing.status, missing.slot_state), ('UNRESOLVED', 'UNRESOLVED'))
        self.assertIs(missing.position, result.position)
        self.assertIsNone(missing.trade)

    def test_duplicate_gap_and_out_of_order_fail_closed(self):
        pos, e = self.position()
        self.assertEqual(protect(pos, candle(pos.pending, time=E+900000)).status, 'UNRESOLVED')
        pos = protect(pos, e).position
        for time in (E-900000, E, E+1800000):
            result = protect(pos, candle(pos.pending, time=time))
            self.assertEqual(result.status, 'UNRESOLVED')
            self.assertIs(result.position, pos)

    def test_entry_candle_revision_fails_closed(self):
        pos, e = self.position()
        self.assertEqual(protect(pos, candle(pos.pending, high='102')).code, 'ENTRY_CANDLE_REVISION')

    def test_altered_financial_position_fails(self):
        pos, e = self.position()
        with self.assertRaises(ValueError): protect(replace(pos, stop_loss=D('97')), e)

    def test_deterministic_replay_no_mutation(self):
        pos, e = self.position(high='105', low='95')
        before = encoded(pos.record)
        self.assertEqual(protect(pos, e).id, protect(pos, e).id)
        self.assertEqual(encoded(pos.record), before)
        with self.assertRaises(FrozenInstanceError): pos.stop_loss = D(1)


class ModelAndSourceTests(unittest.TestCase):
    def test_decimal_context_independent(self):
        snapshot = rules(tick_size=D('.3')); p = pending(snapshot)
        expected = settle(p, candle(p), snapshot).id
        with localcontext() as ctx:
            ctx.prec = 6
            self.assertEqual(settle(p, candle(p), snapshot).id, expected)

    def test_floats_nonfinite_and_bad_atr_rejected(self):
        for value in (1.0, D('NaN'), D('Infinity'), True):
            with self.assertRaises(ValueError): atr_levels(value, 'LONG', D(1), rules().rules[0])
            with self.assertRaises(ValueError): atr_levels(D(100), 'LONG', value, rules().rules[0])
        for value in (D(0), D(-1)):
            with self.assertRaises(ValueError): atr_levels(D(100), 'LONG', value, rules().rules[0])
        p = pending(); pos = settle(p, candle(p), rules()).position
        with self.assertRaises(ValueError): replace(pos, requested_risk=10.0)

    def test_canonical_models_and_stable_identities(self):
        p = pending(); result = settle(p, candle(p), rules())
        self.assertEqual(parse(encoded(result.record)), result.record)
        self.assertEqual(rules().id, rules().id)
        self.assertEqual(implementation(), implementation())
        self.assertIn('FORWARD_PAPER / NON_EXECUTING', encoded(result.record).decode())

    def test_explicit_rules_no_missing_symbol_or_implicit_snapshot(self):
        snapshot = rules()
        with self.assertRaises(ValueError): replace(snapshot, rules=snapshot.rules[:2])
        with self.assertRaises(ValueError): replace(snapshot, source_identity='bad')
        with self.assertRaises(ValueError): replace(snapshot, rules=tuple(reversed(snapshot.rules)))
        p = pending()
        self.assertEqual(settle(p, candle(p), None).status, 'UNRESOLVED')

    def test_closed_candle_and_digest_proof_required(self):
        p = pending(); e = candle(p)
        with self.assertRaises(ValueError): replace(e, received_ms=E+899999)
        with self.assertRaises(ValueError): replace(e, row_digest='0'*64)

    def fixture(self):
        fixture = b1.AdapterTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        mocked = patch('paper.market.load', return_value=fixture.meta)
        mocked.start(); self.addCleanup(mocked.stop)
        return fixture

    def test_real_readonly_source_path_preserves_evidence(self):
        fixture = self.fixture()
        before = b1.op.checkpoint(fixture.source.db)
        source = CandleSource(fixture.directory, fixture.identity)
        item = source.at('BTCUSDT', T-900000)
        self.assertEqual(item.candle.open, D(100))
        self.assertIsNone(source.at('BTCUSDT', T))
        self.assertEqual(before, b1.op.checkpoint(fixture.source.db))
        self.assertFalse(any(hasattr(source, name) for name in ('db', 'execute', 'append', 'poll')))

    def test_source_tampering_and_blocked_rejected(self):
        fixture = self.fixture(); source = CandleSource(fixture.directory, fixture.identity)
        fixture.directory.joinpath('BLOCKED.json').write_text('{}')
        with self.assertRaises(ValueError): source.at('BTCUSDT', T-900000)
        fixture.directory.joinpath('BLOCKED.json').unlink()
        fixture.source.db.execute('DROP TRIGGER no_UPDATE_candles')
        fixture.source.db.execute("UPDATE candles SET digest=?", ('0'*64,)); fixture.source.db.commit()
        with self.assertRaises(ValueError): source.at('BTCUSDT', T-900000)

    def test_new_surface_imports_only_readonly_and_pure_utilities(self):
        root = Path(__file__).resolve().parents[1]
        allowed = {'dataclasses', 'decimal', 'hashlib', 'pathlib', 'backtest.data', 'backtest.simulation',
                   'forward.manifest', 'forward.operations', 'forward.readout', 'paper.model', 'paper.source',
                   'paper.acceptance', 'paper.reservations', 'paper.market', 'research.spec'}
        forbidden_calls = {'simulate', 'simulate_portfolio', 'open_position', 'ATRProtection', 'HistoricalClock',
                           'create_order', 'poll', 'accept', 'sqlite3.connect'}
        for name in ('paper/market.py', 'paper/settlement.py'):
            tree = ast.parse((root/name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names: self.assertIn(alias.name, allowed)
                if isinstance(node, ast.ImportFrom): self.assertIn(node.module, allowed)
                if isinstance(node, ast.Call):
                    called = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                    self.assertNotIn(called, forbidden_calls)


if __name__ == '__main__':
    unittest.main()
