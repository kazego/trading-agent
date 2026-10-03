"""B4 crash/replay tests on temporary synthetic sources and ledgers only."""
import ast
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_paper_acceptance as b2
import test_paper_foundation as b1
import test_paper_settlement as b3
from backtest.data import Candle
from paper.acceptance import AcceptanceBinding, ProspectiveGate
from paper.ledger import PaperLedger, LedgerBlocked, IMMUTABLE
from paper.market import CandleSource, CandleEvidence
from paper.model import DraftManifest, encoded, fingerprint, parse
from paper.source import SourceAdapter
from paper.journal import implementation, TemporalConsistencyError

T, E = b1.T, b1.T+900000


class Crash(BaseException):
    pass


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root/'artifacts'/'forward-paper-execution'/'b4-test'
        self.source_identity = b1.identity()
        self.rules = b3.rules()
        self.binding = AcceptanceBinding(DraftManifest(self.source_identity, rules_fingerprint=self.rules.id),
                                        ProspectiveGate(T-14400000, 'b'*64))
        self.source = SourceAdapter(root, self.source_identity)
        self.market = CandleSource(root, self.source_identity)
        self.items = {T: b2.item()}
        self.candles = {}
        self.addCleanup(patch.stopall)
        patch.object(self.source, 'snapshot', side_effect=lambda: tuple(self.items.values())).start()
        patch.object(self.market, 'at', side_effect=lambda s, t: self.candles.get((s, t))).start()
        self.ledger = self.open()
        self.addCleanup(lambda: self.ledger.close())

    def open(self, **kwargs):
        return PaperLedger(self.path, self.binding, self.rules, self.source, self.market, **kwargs)

    def reopen(self):
        self.ledger.close(); self.ledger = self.open()

    def bar(self, time=E, *, symbol='BTCUSDT', open='100', high='101', low='99', close='100'):
        c = Candle(symbol, '15m', time, time+899999, open, high, low, close, '1')
        evidence = CandleEvidence(self.source_identity, c, fingerprint(c.record()), time+900000)
        self.candles[(symbol, time)] = evidence
        return evidence

    def accept(self, boundary=T):
        with patch('paper.ledger.time.monotonic_ns', side_effect=[0, 1000000]):
            stamps = iter([boundary+240000, boundary+240001])
            return self.ledger.accept(boundary, clock=lambda: next(stamps))

    def owner(self):
        return self.accept()['reservation']['reservation_id']

    def opened(self, **kwargs):
        owner = self.owner(); self.bar(**kwargs)
        self.ledger.settle_entry(owner)
        return owner

    def rows(self):
        state = self.ledger.reconcile()
        return state.rows(), self.ledger._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall()

    def count(self, table):
        return self.ledger._db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_initial_state_namespace_and_no_real_columns(self):
        state = self.ledger.reconcile()
        self.assertEqual((state.equity, state.slot), (D(1000), ('EMPTY', None, None)))
        self.assertIsNone(self.binding.draft.prospective_start_ms)
        for table, in self.ledger._db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            self.assertFalse(any(r[2] == 'REAL' for r in self.ledger._db.execute(f'PRAGMA table_info({table})')))
        with self.assertRaises(ValueError):
            PaperLedger(self.path.parent/'live-0b-001', self.binding, self.rules, self.source, self.market)
        self.assertFalse((self.path.parent/'live-0b-001').exists())

    def test_pending_open_atomic_and_unique(self):
        owner = self.owner(); self.bar()
        self.assertEqual(self.ledger.reconcile().slot[0], 'PENDING')
        result = self.ledger.settle_entry(owner)
        self.assertEqual(result['status'], 'OPEN')
        before = self.rows()
        self.assertEqual(self.ledger.settle_entry(owner), result)
        self.assertEqual(self.rows(), before)
        self.assertEqual((self.count('positions'), self.count('fills'), self.count('equity')), (1, 1, 0))
        self.assertEqual(self.ledger.reconcile().equity, D(1000))

    def test_one_global_exposure_and_no_fallback(self):
        owner = self.opened()
        self.items[T+14400000] = b2.item(boundary=T+14400000)
        result = self.accept(T+14400000)
        self.assertIsNone(result['reservation'])
        self.assertEqual([d['reason'] for d in result['dispositions']], ['BLOCKED_GLOBAL_SLOT']*3)
        self.assertEqual(self.ledger.reconcile().slot[1], owner)

    def test_terminal_rejection_release_and_priority_final(self):
        self.items[T] = b2.item(atr=('100',)*3)
        owner = self.owner(); self.bar()
        result = self.ledger.settle_entry(owner)
        self.assertEqual(result['status'], 'REJECTED_EXECUTION')
        self.assertEqual(self.ledger.reconcile().slot, ('EMPTY', None, None))
        again = self.ledger.accept(T, clock=lambda: self.fail('Retry must not resample clock'))
        self.assertEqual(again['reservation']['reservation_id'], owner)
        self.assertEqual([d['reason'] for d in again['effective_dispositions']][1:], ['BLOCKED_PRIORITY']*2)
        self.assertEqual(self.count('positions'), 0)
        self.assertEqual(self.ledger.settle_entry(owner), result)

    def test_sizing_rejection(self):
        owner = self.owner(); self.bar(open='1', high='1', low='1', close='1')
        # Huge entry price reduces quantity below the frozen .001 minimum.
        self.bar(open='100000000', high='100000000', low='100000000', close='100000000')
        self.assertEqual(self.ledger.settle_entry(owner)['status'], 'REJECTED_SIZING')
        self.assertEqual(self.ledger.reconcile().slot[0], 'EMPTY')

    def test_missing_entry_keeps_pending_idempotently(self):
        owner = self.owner()
        result = self.ledger.settle_entry(owner)
        before = self.rows()
        self.assertEqual(result['status'], 'UNRESOLVED')
        self.assertEqual(self.ledger.settle_entry(owner), result)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.ledger.reconcile().slot[0], 'PENDING')
        self.items[T+14400000] = b2.item(boundary=T+14400000)
        self.assertIsNone(self.accept(T+14400000)['reservation'])
        self.bar(); self.assertEqual(self.ledger.settle_entry(owner)['status'], 'OPEN')

    def test_entry_candle_cursor_once_and_missing_future_not_eod(self):
        owner = self.opened()
        result = self.ledger.process_candle(owner, E)
        self.assertEqual(result['status'], 'OPEN')
        before = self.rows()
        self.assertEqual(self.ledger.process_candle(owner, E), result)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.ledger.process_candle(owner, E+900000)['status'], 'UNRESOLVED')
        state = self.ledger.reconcile()
        self.assertEqual(state.positions[owner].last_processed_ms, E)
        self.assertEqual(state.slot[0], 'OPEN')
        self.assertEqual(self.count('trades'), 0)
        self.reopen()
        self.assertEqual(self.ledger.reconcile().slot[0], 'OPEN')

    def test_entry_bar_close_and_exactly_once_equity(self):
        owner = self.opened(high='120', low='80')
        result = self.ledger.process_candle(owner, E)
        self.assertEqual((result['status'], result['trade']['reason']), ('CLOSED', 'STOP_LOSS'))
        state = self.ledger.reconcile(); trade = state.trades[owner]
        self.assertEqual(state.equity, D(1000)+trade.net_pnl)
        self.assertEqual(trade.net_pnl, trade.gross_pnl-trade.position.entry.fee-trade.exit.fee)
        self.assertEqual((self.count('fills'), self.count('trades'), self.count('equity')), (2, 1, 1))
        before = self.rows(); self.reopen()
        self.assertEqual(self.ledger.process_candle(owner, E), result)
        self.assertEqual(self.rows(), before)

    def test_equity_chain_and_next_reservation_uses_current_balance(self):
        owner = self.opened(high='120')
        self.ledger.process_candle(owner, E)
        first = self.ledger.reconcile().equity
        boundary = T+14400000
        self.items[boundary] = b2.item(boundary=boundary)
        accepted = self.accept(boundary)
        self.assertEqual(D(accepted['reservation']['equity_basis']['amount']), first)
        owner2 = accepted['reservation']['reservation_id']
        self.bar(time=boundary+900000, high='120')
        result = self.ledger.settle_entry(owner2)
        self.assertEqual(D(result['position']['requested_risk']), first*D('.01'))
        self.assertNotEqual(D(result['position']['requested_risk']), D(10))
        self.ledger.process_candle(owner2, boundary+900000)
        state = self.ledger.reconcile(); a, b = state.accounting
        self.assertEqual(b['previous'], a['event_id'])
        self.assertEqual(b['equity_before'], a['equity_after'])
        for event in state.accounting:
            self.assertEqual(D(event['equity_before'])+D(event['net_pnl']), D(event['equity_after']))

    def test_wrong_candle_and_gap_block_without_advancement(self):
        owner = self.opened()
        with self.assertRaises(ValueError): self.ledger.process_candle(owner, E+900000)
        self.assertTrue((self.path/'BLOCKED.json').exists())
        self.assertEqual(self.count('processed'), 0)
        with self.assertRaises(LedgerBlocked): self.ledger.accept(T, clock=lambda: 0)

    def test_conflicting_entry_revision_blocks_and_preserves_finances(self):
        owner = self.opened(); self.bar(high='102')
        with self.assertRaises(ValueError): self.ledger.process_candle(owner, E)
        self.assertEqual(self.count('fills'), 1)
        self.assertEqual(self.count('trades'), 0)
        self.assertTrue((self.path/'BLOCKED.json').exists())

    def test_source_authorization_failure_blocks_before_acceptance(self):
        self.source.snapshot.side_effect = ValueError('Synthetic authorization failure')
        with self.assertRaises(ValueError): self.accept()
        self.assertEqual(self.count('journal'), 0)
        with self.assertRaises(LedgerBlocked): self.accept()

    def test_changed_observation_rejected_before_next_reservation(self):
        self.opened()
        self.items[T] = b2.item(signals=('SHORT',)*3)
        with self.assertRaises(ValueError): self.ledger.accept(T, clock=lambda: 0)
        self.assertTrue((self.path/'BLOCKED.json').exists())

    def test_confirmed_late_clock_failure_never_executable(self):
        with patch('paper.ledger.time.monotonic_ns', side_effect=[0, 5000000]):
            stamps = iter([T+240000, T+240001])
            result = self.ledger.accept(T, clock=lambda: next(stamps))
        self.assertEqual(result['reservation_state'], 'BLOCKED_CLOCK')
        self.assertEqual(self.ledger.reconcile().slot[0], 'BLOCKED')

    def test_crash_after_accept_never_invents_confirmation(self):
        def fault(point):
            if point == 'ACCEPT.committed': raise Crash()
        self.ledger.fault = fault
        with self.assertRaises(Crash): self.accept()
        self.reopen()
        result = self.ledger.accept(T, clock=lambda: self.fail('No late confirmation'))
        self.assertEqual(result['reservation_state'], 'UNCONFIRMED_ACCEPTANCE')
        self.assertEqual(self.ledger.reconcile().slot[0], 'PENDING')
        self.assertEqual(self.count('fills'), 0)

    def test_no_historical_acceptance_inferred(self):
        self.bar(high='120')
        self.reopen()
        self.assertEqual(self.count('journal'), 0)
        self.assertEqual(self.count('reservations'), 0)
        self.assertEqual(self.count('positions'), 0)

    def test_short_trade_durable_accounting(self):
        self.items[T] = b2.item(signals=('SHORT',)*3)
        owner = self.opened(low='80')
        self.ledger.process_candle(owner, E)
        state = self.ledger.reconcile(); trade = state.trades[owner]
        self.assertEqual(trade.gross_pnl, (trade.position.entry.price-trade.exit.price)*trade.exit.quantity)
        self.assertEqual(state.equity, D(1000)+trade.net_pnl)

    def test_eth_rejection_never_exposes_ada(self):
        self.items[T] = b2.item(signals=('NONE','SHORT','LONG'), atr=('100',)*3)
        owner = self.owner(); self.bar(symbol='ETHUSDT')
        self.assertEqual(self.ledger.settle_entry(owner)['status'], 'REJECTED_EXECUTION')
        retry = self.ledger.accept(T, clock=lambda: self.fail('No reconsideration'))
        self.assertEqual(retry['reservation']['symbol'], 'ETHUSDT')
        self.assertEqual(retry['effective_dispositions'][2]['reason'], 'BLOCKED_PRIORITY')

    def test_blocked_boundary_not_reconsidered_after_position_closes(self):
        owner = self.opened(high='120')
        boundary = T+14400000; self.items[boundary] = b2.item(boundary=boundary)
        rejected = self.accept(boundary)
        self.ledger.process_candle(owner, E)
        self.assertEqual(self.ledger.accept(boundary, clock=lambda: self.fail('No retry decision')), rejected)

    def crash_convergence(self, kind, stage, *, closing=False, rejection=False):
        if rejection:
            self.items[T] = b2.item(atr=('100',)*3)
        owner = self.owner(); self.bar(high='120' if closing else '101')
        if kind == 'BAR': self.ledger.settle_entry(owner)
        operation = lambda: (self.ledger.settle_entry(owner) if kind == 'ENTRY'
                             else self.ledger.process_candle(owner, E))
        # Save the pre-operation database, then compare crash/retry to uninterrupted.
        backup = sqlite3.connect(':memory:'); self.ledger._db.backup(backup)
        expected = operation(); expected_rows = self.rows()
        self.ledger.close()
        restored = sqlite3.connect(self.path/'paper.sqlite'); backup.backup(restored); restored.close(); backup.close()
        self.ledger = self.open()
        def fault(point):
            if point == kind+'.'+stage: raise Crash()
        self.ledger.fault = fault
        with self.assertRaises(Crash): operation()
        self.reopen()
        self.assertEqual(operation(), expected)
        self.assertEqual(self.rows(), expected_rows)

    def test_crash_before_entry(self): self.crash_convergence('ENTRY', 'before')
    def test_crash_during_entry_journal(self): self.crash_convergence('ENTRY', 'journal')
    def test_crash_during_entry_materialization(self): self.crash_convergence('ENTRY', 'state')
    def test_crash_after_durable_open(self): self.crash_convergence('ENTRY', 'committed')
    def test_crash_rejection_release(self): self.crash_convergence('ENTRY', 'state', rejection=True)
    def test_crash_during_no_exit_cursor(self): self.crash_convergence('BAR', 'state')
    def test_crash_after_no_exit_cursor(self): self.crash_convergence('BAR', 'committed')
    def test_crash_before_exit(self): self.crash_convergence('BAR', 'before', closing=True)
    def test_crash_during_exit_journal(self): self.crash_convergence('BAR', 'journal', closing=True)
    def test_crash_during_exit_accounting(self): self.crash_convergence('BAR', 'state', closing=True)
    def test_crash_after_closed_accounting(self): self.crash_convergence('BAR', 'committed', closing=True)

    def test_corrupt_equity_chain_blocks_reopen(self):
        owner = self.opened(high='120'); self.ledger.process_candle(owner, E)
        self.ledger._db.execute("UPDATE account SET amount='999999'"); self.ledger._db.commit()
        with self.assertRaises(ValueError): self.ledger.reconcile()
        self.ledger.close()
        with self.assertRaises(LedgerBlocked): self.open()

    def test_malformed_equity_event_detected_even_with_trigger_restored(self):
        owner = self.opened(high='120'); self.ledger.process_candle(owner, E)
        sql = self.ledger._db.execute("SELECT sql FROM sqlite_master WHERE name='no_UPDATE_equity'").fetchone()[0]
        self.ledger._db.execute('DROP TRIGGER no_UPDATE_equity')
        self.ledger._db.execute("UPDATE equity SET delta='NaN'")
        self.ledger._db.execute(sql); self.ledger._db.commit()
        with self.assertRaises(ValueError): self.ledger.reconcile()
        self.assertTrue((self.path/'BLOCKED.json').exists())

    def test_global_active_unique_index(self):
        self.opened()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._db.execute("INSERT INTO reservations VALUES(?,?,?,?,?)",
                                   ('different', T+14400000, 'ETHUSDT', 'PENDING_ENTRY', '{}'))
        self.ledger._db.rollback()

    def test_no_float_equity_override(self):
        from paper.acceptance import decide, BoundaryBatch
        state = self.ledger.reconcile(); basis = state.basis
        for invalid in (1000.0, 'NaN', '1000.0', '-1'):
            bad = dict(basis, amount=invalid)
            with self.assertRaises(ValueError):
                decide(self.binding, BoundaryBatch.from_evidence(self.items[T]), accepted_ms=T+240000,
                       slot='EMPTY', equity_basis=encoded(bad))

    def test_nonactivated_gate_cannot_accept_historical_source(self):
        from paper.acceptance import decide, BoundaryBatch
        inactive = replace(self.binding, prospective=ProspectiveGate())
        result = decide(inactive, BoundaryBatch.from_evidence(self.items[T]), accepted_ms=T+240000, slot='EMPTY')
        self.assertIsNone(result['reservation'])
        self.assertEqual([d['reason'] for d in result['dispositions']], ['BLOCKED_NOT_ACTIVATED']*3)

    def test_unconfirmed_blocks_future_reservation_after_restart(self):
        def fault(point):
            if point == 'ACCEPT.committed': raise Crash()
        self.ledger.fault = fault
        with self.assertRaises(Crash): self.accept()
        self.reopen()
        boundary = T+14400000; self.items[boundary] = b2.item(boundary=boundary)
        self.assertIsNone(self.accept(boundary)['reservation'])
        self.assertEqual(self.ledger.reconcile().slot[0], 'PENDING')

    def test_append_only_and_duplicate_constraints(self):
        owner = self.opened(high='120'); self.ledger.process_candle(owner, E)
        for table in IMMUTABLE:
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger._db.execute(f'DELETE FROM {table}')
            self.ledger._db.rollback()
        for table in ('fills', 'trades', 'equity', 'processed', 'global_slot'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger._db.execute(f'INSERT INTO {table} SELECT * FROM {table}')
            self.ledger._db.rollback()

    def test_corrupt_journal_payload_blocks(self):
        self.owner()
        self.ledger._db.execute('DROP TRIGGER no_UPDATE_journal')
        self.ledger._db.execute("UPDATE journal SET payload='{}' WHERE sequence=1")
        self.ledger._db.commit()
        with self.assertRaises(ValueError): self.ledger.reconcile()
        self.assertTrue((self.path/'BLOCKED.json').exists())

    def test_orphan_or_changed_projection_blocks(self):
        self.opened()
        self.ledger._db.execute("UPDATE positions SET payload='{}'"); self.ledger._db.commit()
        with self.assertRaises(ValueError): self.ledger.reconcile()

    def test_deterministic_identity_and_serialization(self):
        self.assertEqual(implementation(), implementation())
        self.assertEqual(parse(encoded(self.ledger.identity)), self.ledger.identity)
        self.assertEqual(self.ledger.identity['activation'], 'UNSET_FOUNDATION_ONLY')

    def close_through(self, exit_open, *, received_ms=None):
        owner = self.opened()
        for stamp in range(E, exit_open+1, 900000):
            item = self.bar(time=stamp, high='120' if stamp == exit_open else '101')
            if stamp == exit_open and received_ms is not None:
                self.candles[('BTCUSDT', stamp)] = replace(item, received_ms=received_ms)
            self.ledger.process_candle(owner, stamp)
        return owner

    def accept_at(self, boundary, claimed_ms):
        with patch('paper.ledger.time.monotonic_ns', side_effect=[0, 1000000]):
            stamps = iter([claimed_ms, claimed_ms+1])
            return self.ledger.accept(boundary, clock=lambda: next(stamps))

    def test_temporal_regression_original_0404_vs_0415(self):
        boundary = T+14400000
        self.close_through(boundary)
        state = self.ledger.reconcile()
        self.assertEqual(state.equity, D('1019.685967005'))
        self.assertEqual(state.financial_available_ms, boundary+900000)
        before = self.count('journal')
        self.items[boundary] = b2.item(boundary=boundary)
        with self.assertRaises(TemporalConsistencyError): self.accept_at(boundary, boundary+240000)
        self.assertEqual(self.count('journal'), before)
        self.assertEqual(self.count('reservations'), 1)
        blocked = parse((self.path/'BLOCKED.json').read_bytes().rstrip(b'\n'))
        self.assertEqual(blocked['code'], 'TEMPORAL_EVIDENCE_REGRESSION')
        self.assertEqual(blocked['temporal_evidence']['claimed_ms'], boundary+240000)
        self.assertEqual(blocked['temporal_evidence']['financial_available_at_ms'], boundary+900000)
        self.assertEqual(self.ledger._db.execute('SELECT phase FROM global_slot').fetchone(), ('EMPTY',))
        with self.assertRaises(LedgerBlocked): self.accept_at(boundary, boundary+900001)

    def test_temporal_regression_after_restart(self):
        boundary = T+14400000
        self.close_through(boundary)
        before = self.ledger.reconcile().rows()
        self.reopen()
        self.assertEqual(self.ledger.reconcile().rows(), before)
        self.items[boundary] = b2.item(boundary=boundary)
        with self.assertRaises(TemporalConsistencyError): self.accept_at(boundary, boundary+240000)
        self.assertEqual(self.count('equity'), 1)

    def test_exact_availability_equality_is_allowed(self):
        boundary = T+14400000; available = boundary+240000
        self.close_through(boundary-900000, received_ms=available)
        self.items[boundary] = b2.item(boundary=boundary)
        result = self.accept_at(boundary, available)
        self.assertEqual(result['reservation_state'], 'PENDING_ENTRY')
        self.assertEqual(result['acceptance_ms'], available)

    def test_after_availability_is_allowed(self):
        boundary = T+14400000; available = boundary+240000
        self.close_through(boundary-900000, received_ms=available)
        self.items[boundary] = b2.item(boundary=boundary)
        result = self.accept_at(boundary, available+1000)
        self.assertEqual(result['reservation_state'], 'PENDING_ENTRY')

    def test_watermark_equity_chain_advances_across_closures(self):
        owner = self.opened(high='120'); self.ledger.process_candle(owner, E)
        first = self.ledger.reconcile().financial_available_ms
        self.assertEqual(first, E+900000)
        boundary = T+14400000; self.items[boundary] = b2.item(boundary=boundary)
        owner2 = self.accept(boundary)['reservation']['reservation_id']
        self.bar(time=boundary+900000, high='120')
        self.ledger.settle_entry(owner2); self.ledger.process_candle(owner2, boundary+900000)
        state = self.ledger.reconcile(); second = state.financial_available_ms
        self.assertEqual(second, boundary+1800000)
        self.assertGreater(second, first)
        self.assertEqual(state.accounting[1]['financial_available_before_ms'], first)
        self.assertEqual(state.accounting[1]['financial_available_at_ms'], second)
        self.reopen()
        self.assertEqual(self.ledger.reconcile().financial_available_ms, second)

    def test_watermark_includes_earlier_consumed_evidence_not_just_exit(self):
        owner = self.owner()
        item = self.bar()
        late = T+14400000+300000
        self.candles[('BTCUSDT', E)] = replace(item, received_ms=late)
        self.ledger.settle_entry(owner); self.ledger.process_candle(owner, E)
        self.bar(time=E+900000, high='120')
        self.ledger.process_candle(owner, E+900000)
        state = self.ledger.reconcile()
        self.assertLess(state.accounting[0]['exit_evidence_received_ms'], late)
        self.assertEqual(state.accounting[0]['source_evidence_available_at_ms'], late)
        self.assertEqual(state.financial_available_ms, late)

    def test_consumed_open_evidence_prevents_cross_operation_clock_regression(self):
        owner = self.opened()
        boundary = T+14400000
        for stamp in range(E, boundary+1, 900000):
            self.bar(time=stamp)
            self.ledger.process_candle(owner, stamp)
        self.assertEqual(self.ledger.reconcile().equity, D(1000))
        self.reopen()
        self.items[boundary] = b2.item(boundary=boundary)
        with self.assertRaises(TemporalConsistencyError): self.accept_at(boundary, boundary+240000)
        self.assertEqual(self.ledger._db.execute('SELECT phase FROM global_slot').fetchone(), ('OPEN',))

    def test_replay_rejects_locally_valid_impossible_acceptance(self):
        from paper.acceptance import decide, BoundaryBatch
        from paper.reservations import AcceptanceStore
        boundary = T+14400000
        self.close_through(boundary)
        state = self.ledger.reconcile(); item = b2.item(boundary=boundary)
        data = dict(source=item.record, accepted_ms=boundary+240000, equity=state.basis)
        decision = decide(self.binding, BoundaryBatch.from_evidence(item),
                          accepted_ms=data['accepted_ms'], slot='EMPTY', equity_basis=encoded(state.basis))
        confirmation = AcceptanceStore._confirmation(decision['reservation'], boundary+240001, 1000000)
        self.assertEqual(confirmation['status'], 'PENDING_ENTRY')
        # A canonical, correctly hash-linked event cannot bypass temporal replay.
        sequence, parent = self.ledger._db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
        result = dict(decision, confirmation=None, reservation_state='UNCONFIRMED_ACCEPTANCE',
                      effective_dispositions=parse(encoded(decision['dispositions'])))
        result['effective_dispositions'][0]['reason'] = 'UNCONFIRMED_ACCEPTANCE'
        request = 'ACCEPT:'+str(boundary)
        event = dict(label=b1.LABEL, parent=parent, request=request, kind='ACCEPT', input=data, result=result)
        self.ledger._db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',
                               (sequence+1, fingerprint(event), request, parent, encoded(event).decode()))
        self.ledger._db.commit()
        with self.assertRaises(TemporalConsistencyError): self.ledger.reconcile()
        self.assertEqual(parse((self.path/'BLOCKED.json').read_bytes().rstrip(b'\n'))['code'], 'TEMPORAL_EVIDENCE_REGRESSION')
        self.assertEqual(self.count('reservations'), 1)

    def test_old_acceptance_retry_keeps_original_equity(self):
        original = self.accept(); owner = original['reservation']['reservation_id']
        self.bar(high='120'); self.ledger.settle_entry(owner); self.ledger.process_candle(owner, E)
        self.reopen()
        replay = self.ledger.accept(T, clock=lambda: self.fail('Historical retry cannot claim a new time'))
        self.assertEqual(replay, original)
        self.assertEqual(replay['reservation']['equity_basis']['amount'], '1000')

    def test_initial_equity_availability_unset_without_activation(self):
        inactive = replace(self.binding, prospective=ProspectiveGate())
        other = PaperLedger(self.path.parent/'b4-inactive', inactive, self.rules, self.source, self.market)
        try:
            state = other.reconcile()
            self.assertIsNone(state.financial_available_ms)
            self.assertIsNone(state.state_available_ms)
            self.assertIsNone(other.identity['initial_equity_availability']['available_at_ms'])
            with patch('paper.ledger.time.monotonic_ns', return_value=0):
                result = other.accept(T, clock=lambda: T+240000)
            self.assertIsNone(result['reservation'])
            self.assertIsNone(other.reconcile().financial_available_ms)
        finally:
            other.close()

    def test_initial_equity_uses_only_explicit_synthetic_gate(self):
        state = self.ledger.reconcile()
        self.assertEqual(state.financial_available_ms, self.binding.prospective.start_ms)
        self.assertEqual(self.ledger.identity['initial_equity_availability']['available_at_ms'], self.binding.prospective.start_ms)

    def test_watermark_projection_tampering_blocks(self):
        owner = self.opened(high='120'); self.ledger.process_candle(owner, E)
        self.ledger._db.execute('UPDATE account SET financial_available_ms=financial_available_ms-1')
        self.ledger._db.commit()
        with self.assertRaises(ValueError): self.ledger.reconcile()
        self.assertTrue((self.path/'BLOCKED.json').exists())

    def confirm_crash(self, stage):
        def fault(point):
            if point == 'CONFIRM.'+stage: raise Crash()
        self.ledger.fault = fault
        with self.assertRaises(Crash): self.accept()
        self.reopen()
        result = self.ledger.accept(T, clock=lambda: self.fail('No recreated confirmation'))
        expected = 'PENDING_ENTRY' if stage == 'committed' else 'UNCONFIRMED_ACCEPTANCE'
        self.assertEqual(result['reservation_state'], expected)
        self.assertEqual(result['effective_dispositions'][1]['reason'], 'BLOCKED_PRIORITY')
        self.assertEqual(result['effective_dispositions'][2]['reason'], 'BLOCKED_PRIORITY')
        self.assertEqual(self.count('reservations'), 1)
        self.assertEqual(len(self.ledger.reconcile().confirmations), int(stage == 'committed'))
        before = self.rows(); self.reopen()
        self.assertEqual(self.ledger.accept(T, clock=lambda: self.fail('No clock on retry')), result)
        self.assertEqual(self.rows(), before)

    def test_crash_during_confirm_journal(self): self.confirm_crash('journal')
    def test_crash_during_confirm_projection(self): self.confirm_crash('state')
    def test_crash_immediately_after_confirm_commit(self): self.confirm_crash('committed')

    def test_network_order_import_isolation(self):
        allowed = {'contextlib','pathlib','os','sqlite3','time','dataclasses','decimal','hashlib',
                   'backtest.data','backtest.simulation','paper.acceptance','paper.market','paper.model',
                   'paper.source','paper.journal','paper.reservations','paper.settlement','research.spec','runtime',
                   'paper.verified_source','paper.transition','paper.clock_v3','paper.recovery','paper.binding','paper.authorization','paper.operations'}
        root = Path(__file__).resolve().parents[1]
        for name in ('paper/journal.py', 'paper/ledger.py'):
            tree = ast.parse((root/name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names: self.assertIn(alias.name, allowed)
                if isinstance(node, ast.ImportFrom): self.assertIn(node.module, allowed)


if __name__ == '__main__': unittest.main()
