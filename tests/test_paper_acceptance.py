"""Offline B2: synthetic source evidence and clocks only; no activated run."""
import ast
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_paper_foundation as b1
from paper.model import DraftManifest, SourceObservation, encoded, fingerprint, parse, implementation as b1_identity
from paper.source import SourceAdapter
from paper.acceptance import (AcceptanceBinding, ProspectiveGate, BoundaryBatch, Candidate,
                              decide, implementation, policy)
from paper.reservations import AcceptanceStore

T = b1.T


def item(*, source=None, boundary=T, signals=('LONG', 'SHORT', 'LONG'), atr=('2', '2', '2'),
         timing='ONLINE', available=None, with_receipt=True):
    source = source or b1.identity()
    o = b1.observation(source)
    o.update(boundary_ms=boundary, observed_ms=boundary+180000)
    for i, d in enumerate(o['decisions']):
        d.update(boundary_ms=boundary, completed_through_ms=boundary-1, signal=signals[i], atr14=atr[i])
        d['signal_fingerprint'] = fingerprint({k: v for k, v in d.items() if k != 'signal_fingerprint'})
    r = b1.receipt(o)
    r.update(boundary_ms=boundary, timing=timing,
             observed_ms=available if available is not None else boundary+(600000 if timing == 'DELAYED_RECONSTRUCTED' else 180001))
    return SourceObservation(source, encoded(o), encoded(r) if with_receipt else None)


def binding(*, source=None, active=True, rules=True):
    return AcceptanceBinding(DraftManifest(source or b1.identity(), rules_fingerprint='a'*64 if rules else None),
                             ProspectiveGate(T-14400000, 'b'*64) if active else ProspectiveGate())


class DecisionTests(unittest.TestCase):
    def evaluate(self, evidence=None, *, when=T+240000, slot='EMPTY', bound=None):
        return decide(bound or binding(), BoundaryBatch.from_evidence(evidence or item()), accepted_ms=when, slot=slot)

    def reasons(self, result):
        return [d['reason'] for d in result['dispositions']]

    def test_priority_and_both_directions(self):
        for signals, expected in [(('LONG', 'SHORT', 'LONG'), 'BTCUSDT'),
                                  (('SHORT', 'LONG', 'LONG'), 'BTCUSDT'),
                                  (('NONE', 'SHORT', 'LONG'), 'ETHUSDT'),
                                  (('NONE', 'NONE', 'SHORT'), 'ADAUSDT')]:
            with self.subTest(signals=signals):
                result = self.evaluate(item(signals=signals))
                self.assertEqual(result['reservation']['symbol'], expected)
                self.assertEqual(self.reasons(result).count('SELECTED_PENDING_CONFIRMATION'), 1)

    def test_none_audit(self):
        result = self.evaluate(item(signals=('NONE',)*3))
        self.assertIsNone(result['reservation'])
        self.assertEqual(self.reasons(result), ['NONE']*3)

    def test_delayed_permanently_missed_even_after_deadline(self):
        result = self.evaluate(item(timing='DELAYED_RECONSTRUCTED'), when=T+900000)
        self.assertEqual(self.reasons(result), ['MISSED_DELAYED_SIGNAL']*3)
        self.assertIsNone(result['reservation'])

    def test_deadline_inclusive_fixed_entry(self):
        result = self.evaluate(when=T+840000)
        self.assertEqual(result['reservation']['entry_boundary_ms'], T+900000)
        self.assertEqual(result['reservation']['deadline_ms'], T+840000)
        late = self.evaluate(when=T+840001)
        self.assertEqual(self.reasons(late), ['MISSED_ACCEPTANCE_DEADLINE']*3)
        self.assertEqual(late['entry_boundary_ms'], T+900000)

    def test_unconfirmed_and_legacy_online_cannot_reserve(self):
        self.assertEqual(self.reasons(self.evaluate(item(with_receipt=False))), ['BLOCKED_UNCONFIRMED_SOURCE']*3)
        o = parse(item().observation); del o['operations_fingerprint']; o['timing'] = 'ONLINE'
        legacy = SourceObservation(b1.identity(), encoded(o), None)
        self.assertIsNone(self.evaluate(legacy)['reservation'])

    def test_information_must_already_be_durable(self):
        for stamp in (T+180000, T+180001):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError): self.evaluate(when=stamp)

    def test_prospective_gate_without_actual_activation(self):
        self.assertEqual(self.reasons(self.evaluate(bound=binding(active=False))), ['BLOCKED_NOT_ACTIVATED']*3)
        at_start = replace(binding(), prospective=ProspectiveGate(T, 'b'*64))
        self.assertEqual(self.reasons(self.evaluate(bound=at_start)), ['OUTSIDE_PROSPECTIVE_INTERVAL']*3)
        self.assertIsNotNone(self.evaluate()['reservation'])

    def test_missing_rules_blocks_and_invalid_atr_not_selected(self):
        self.assertEqual(self.reasons(self.evaluate(bound=binding(rules=False))), ['BLOCKED_CONFIGURATION']*3)
        result = self.evaluate(item(atr=('0', '2', '2')))
        self.assertEqual(result['reservation']['symbol'], 'ETHUSDT')
        self.assertEqual(self.reasons(result), ['REJECTED_INVALID_ATR', 'SELECTED_PENDING_CONFIRMATION', 'BLOCKED_PRIORITY'])

    def test_all_occupied_states_block(self):
        for state in ('PENDING', 'OPEN', 'BLOCKED'):
            with self.subTest(state=state):
                self.assertEqual(self.reasons(self.evaluate(slot=state)), ['BLOCKED_GLOBAL_SLOT']*3)
        with self.assertRaises(ValueError): self.evaluate(slot='UNKNOWN')

    def test_complete_three_symbol_batch_and_consistency(self):
        batch = BoundaryBatch.from_evidence(item())
        for candidates in (batch.candidates[:2], batch.candidates[::-1], (batch.candidates[0],)*3):
            with self.assertRaises(ValueError): replace(batch, candidates=candidates)
        o = parse(item().observation); o['decisions'].pop()
        with self.assertRaises(ValueError): SourceObservation(b1.identity(), encoded(o), None)

    def test_tampering_fails_before_arbitration(self):
        e = item(); o = parse(e.observation); o['decisions'][0]['atr14'] = '100'
        with self.assertRaises(ValueError): SourceObservation(e.source, encoded(o), e.receipt)

    def test_exact_frozen_reservation_values_and_determinism(self):
        result = self.evaluate(); r = result['reservation']
        self.assertEqual(result, self.evaluate())
        self.assertEqual(r['atr14'], '2')
        self.assertEqual(r['equity_basis']['amount'], '1000')
        self.assertEqual(r['equity_reference'], fingerprint(binding().equity))
        self.assertEqual(r['observation'], fingerprint(parse(item().observation)))
        self.assertEqual(r['receipt'], fingerprint(parse(item().receipt)))
        self.assertEqual(r['evidence'], item().id)
        self.assertEqual(r['reservation_id'], fingerprint({k: v for k, v in r.items() if k != 'reservation_id'}))

    def test_no_future_inputs_or_context_in_projection(self):
        batch = BoundaryBatch.from_evidence(item())
        names = {f.name for f in fields(batch)} | {f.name for f in fields(Candidate)}
        self.assertFalse(names & {'open', 'high', 'low', 'close', 'price', 'context', 'sizing', 'performance'})
        for key in ('entry_price', 'future_high', 'future_low', 'future_close', 'sizing_outcome', 'performance'):
            with self.subTest(key=key), self.assertRaises(TypeError):
                decide(binding(), batch, accepted_ms=T+240000, slot='EMPTY', **{key: Decimal('99')})
        for future_value in ('1', '1000000'):
            o = parse(item().observation)
            o['descriptive_context']['BTCUSDT']['values']['1D']['hypothetical_price'] = future_value
            values = o['descriptive_context']['BTCUSDT']['values']
            o['descriptive_context']['BTCUSDT']['context_fingerprint'] = fingerprint(values)
            e = SourceObservation(b1.identity(), encoded(o), encoded(dict(parse(item().receipt), observation_fingerprint=fingerprint(o))))
            self.assertEqual(self.evaluate(e)['reservation']['symbol'], 'BTCUSDT')


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'artifacts'/'forward-paper-execution'/'b2-test'
        self.binding = binding()
        self.store = AcceptanceStore(self.path, self.binding); self.addCleanup(self.store.close)
        self.adapter = SourceAdapter(Path(self.temp.name), self.binding.draft.source)
        self.snapshot = patch.object(SourceAdapter, 'snapshot', return_value=(item(),)).start()
        self.monotonic = patch('paper.reservations.time.monotonic_ns', return_value=0).start()
        self.addCleanup(patch.stopall)

    def accept(self, evidence=None, times=None):
        if evidence is not None: self.snapshot.return_value = (evidence,)
        stamps = iter(times or (self.snapshot.return_value[0].boundary_ms+240000,
                                self.snapshot.return_value[0].boundary_ms+240001))
        return self.store.accept(self.adapter, self.snapshot.return_value[0].boundary_ms, clock=lambda: next(stamps))

    def test_one_atomic_reservation_and_three_dispositions(self):
        result = self.accept()
        self.assertEqual(result['reservation_state'], 'PENDING_ENTRY')
        self.assertEqual([d['reason'] for d in result['effective_dispositions']],
                         ['PENDING_ENTRY', 'BLOCKED_PRIORITY', 'BLOCKED_PRIORITY'])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM reservations').fetchone(), (1,))
        self.store.verify()

    def test_replay_does_not_sample_clock_or_duplicate(self):
        first = self.accept()
        def forbidden(): raise AssertionError('Replay cannot reaccept')
        second = self.store.accept(self.adapter, T, clock=forbidden)
        self.assertEqual(first, second)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM batches').fetchone(), (1,))

    def test_restart_preserves_original_decision(self):
        first = self.accept(); self.store.close()
        self.store = AcceptanceStore(self.path, self.binding); self.addCleanup(self.store.close)
        self.assertEqual(first, self.accept(times=(T+99999999,)))

    def test_conflicting_replay_rejected(self):
        self.accept()
        with self.assertRaises(ValueError): self.accept(item(signals=('SHORT', 'LONG', 'LONG')))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM reservations').fetchone(), (1,))

    def test_occupied_slot_audits_next_boundary(self):
        self.accept()
        result = self.accept(item(boundary=T+14400000))
        self.assertIsNone(result['reservation'])
        self.assertEqual([d['reason'] for d in result['dispositions']], ['BLOCKED_GLOBAL_SLOT']*3)
        self.store.verify()

    def test_no_fallback_after_hypothetical_btc_or_eth_rejection(self):
        for signals, selected in ((('LONG', 'LONG', 'SHORT'), 'BTCUSDT'), (('NONE', 'SHORT', 'LONG'), 'ETHUSDT')):
            path = self.path.parent/('b2-'+selected)
            store = AcceptanceStore(path, self.binding)
            try:
                self.snapshot.return_value = (item(signals=signals),)
                stamps = iter((T+240000, T+240001))
                first = store.accept(self.adapter, T, clock=lambda: next(stamps))
                # Stand-in for unresolved future rejection; no B3 processing implemented.
                with store._db: store._db.execute("UPDATE global_slot SET phase='BLOCKED'")
                for later_outcome in ('REJECTED_SIZING', 'REJECTED_EXECUTION'):
                    with self.subTest(symbol=selected, outcome=later_outcome):
                        again = store.accept(self.adapter, T, clock=lambda: T+99999999)
                        self.assertEqual(again['reservation']['reservation_id'], first['reservation']['reservation_id'])
                        self.assertEqual(again['reservation']['symbol'], selected)
                        self.assertEqual(again['dispositions'], first['dispositions'])
                        self.assertEqual(store._db.execute('SELECT count(*) FROM reservations').fetchone(), (1,))
            finally: store.close()

    def test_rollback_reservation_and_audit_together(self):
        self.store._db.execute("CREATE TRIGGER fail_reservation BEFORE INSERT ON reservations "
                               "BEGIN SELECT RAISE(ABORT,'Injected crash'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.accept()
        for table in ('batches', 'reservations', 'evidence', 'source_observations', 'source_receipts'):
            self.assertEqual(self.store._db.execute(f'SELECT count(*) FROM {table}').fetchone(), (0,))
        self.assertEqual(self.store._db.execute('SELECT phase FROM global_slot').fetchone(), ('EMPTY',))

    def test_crash_after_commit_never_infers_confirmation(self):
        with self.assertRaises(StopIteration): self.accept(times=(T+240000,))
        self.store.close(); self.store = AcceptanceStore(self.path, self.binding); self.addCleanup(self.store.close)
        result = self.accept(times=(T+250000,))
        self.assertEqual(result['reservation_state'], 'UNCONFIRMED_ACCEPTANCE')
        self.assertEqual(result['reservation']['symbol'], 'BTCUSDT')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM confirmations').fetchone(), (0,))
        self.assertIsNone(self.accept(item(boundary=T+14400000))['reservation'])

    def test_confirmation_failure_leaves_no_executable_reservation(self):
        self.store._db.execute("CREATE TRIGGER fail_confirmation BEFORE INSERT ON confirmations "
                               "BEGIN SELECT RAISE(ABORT,'Injected confirmation failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.accept()
        result = self.accept(times=(T+250000,))
        self.assertEqual(result['reservation_state'], 'UNCONFIRMED_ACCEPTANCE')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM reservations').fetchone(), (1,))

    def test_same_database_cannot_have_two_writers(self):
        from errors import RuntimeOwnershipError
        with self.assertRaises(RuntimeOwnershipError): AcceptanceStore(self.path, self.binding)

    def test_terminal_miss_not_reopened_and_receipt_revision_conflicts(self):
        first = self.accept(times=(T+900000,))
        self.assertEqual(first, self.accept(times=(T+240000,)))
        e = item(); r = parse(e.receipt); r['observed_ms'] += 1
        changed = SourceObservation(e.source, e.observation, encoded(r))
        with self.assertRaises(ValueError): self.accept(changed)

    def test_changed_binding_rejected_on_reopen(self):
        self.store.close()
        with self.assertRaises(ValueError): AcceptanceStore(self.path, binding(active=False))

    def test_runtime_identity_change_fails_closed(self):
        with patch('paper.reservations.implementation', return_value='0'*64):
            with self.assertRaises(ValueError): self.accept()
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM batches').fetchone(), (0,))

    def test_durable_deadline_edge_and_slow_commit(self):
        self.assertEqual(self.accept(times=(T+840000, T+840000))['reservation_state'], 'PENDING_ENTRY')
        for offset, state in ((840001, 'MISSED_ACCEPTANCE_DEADLINE'), (239999, 'BLOCKED_CLOCK')):
            store = AcceptanceStore(self.path.parent/('b2-clock-'+str(offset)), self.binding)
            try:
                stamps = iter((T+240000, T+offset))
                result = store.accept(self.adapter, T, clock=lambda: next(stamps))
                self.assertEqual(result['reservation_state'], state)
                self.assertEqual(self.store._db.execute('SELECT count(*) FROM reservations').fetchone(), (1,))
                self.assertEqual(store._db.execute('SELECT phase FROM global_slot').fetchone(), ('BLOCKED',))
                store.verify()
            finally: store.close()

    def test_hidden_utc_rollback_cannot_make_late_commit_timely(self):
        # UTC advanced 500ms, but the commit interval took at least two seconds.
        self.monotonic.side_effect = (10000000000, 12000000000)
        result = self.accept(times=(T+839000, T+839500))
        self.assertEqual(result['reservation_state'], 'BLOCKED_CLOCK')
        self.assertEqual(result['confirmation']['clock_error'], 'UTC_MONOTONIC_DISAGREEMENT')
        self.assertEqual(result['confirmation']['minimum_durable_ms'], T+841000)
        self.assertEqual(self.store._db.execute('SELECT phase FROM global_slot').fetchone(), ('BLOCKED',))
        self.store.close(); self.store = AcceptanceStore(self.path, self.binding); self.addCleanup(self.store.close)
        replay = self.accept(times=(T+839900,))
        self.assertEqual(result, replay)
        self.assertEqual(replay['dispositions'][1]['reason'], 'BLOCKED_PRIORITY')

    def test_monotonic_anchor_precedes_acceptance_clock(self):
        calls = []
        def monotonic():
            calls.append('monotonic')
            return 0
        def utc():
            calls.append('utc')
            return T+240000
        self.monotonic.side_effect = monotonic
        self.store.accept(self.adapter, T, clock=utc)
        self.assertEqual(calls, ['monotonic', 'utc', 'monotonic', 'utc'])

    def test_monotonic_elapsed_exact_deadline_is_inclusive(self):
        self.monotonic.side_effect = (10000000000, 11000000000)
        result = self.accept(times=(T+839000, T+840000))
        self.assertEqual(result['reservation_state'], 'PENDING_ENTRY')
        self.assertEqual(result['confirmation']['minimum_durable_ms'], T+840000)

    def test_monotonic_regression_is_durably_blocked(self):
        self.monotonic.side_effect = (10000000000, 9999999999)
        result = self.accept()
        self.assertEqual(result['reservation_state'], 'BLOCKED_CLOCK')
        self.assertEqual(result['confirmation']['clock_error'], 'MONOTONIC_REGRESSION')
        self.store.verify()

    def test_invalid_post_commit_clock_sample_is_auditable(self):
        result = self.accept(times=(T+240000, None))
        self.assertEqual(result['reservation_state'], 'BLOCKED_CLOCK')
        self.assertEqual(result['confirmation']['clock_error'], 'INVALID_UTC_SAMPLE')
        self.store.verify()

    def test_invalid_post_commit_monotonic_sample_is_auditable(self):
        self.monotonic.side_effect = (0, None)
        result = self.accept()
        self.assertEqual(result['reservation_state'], 'BLOCKED_CLOCK')
        self.assertEqual(result['confirmation']['clock_error'], 'INVALID_MONOTONIC_SAMPLE')
        self.store.verify()

    def test_after_deadline_never_reserves(self):
        result = self.accept(times=(T+840001,))
        self.assertIsNone(result['reservation'])
        self.assertEqual([d['reason'] for d in result['dispositions']], ['MISSED_ACCEPTANCE_DEADLINE']*3)

    def test_dormant_binding_and_missing_configuration(self):
        for bound, reason in ((binding(active=False), 'BLOCKED_NOT_ACTIVATED'),
                              (binding(rules=False), 'BLOCKED_CONFIGURATION')):
            store = AcceptanceStore(self.path.parent/('b2-'+reason), bound)
            try:
                result = store.accept(self.adapter, T, clock=lambda: T+240000)
                self.assertIsNone(result['reservation'])
                self.assertEqual([d['reason'] for d in result['dispositions']], [reason]*3)
            finally: store.close()

    def test_partial_missing_and_failed_authorization_no_rows(self):
        for items in ((), (item(), item())):
            self.snapshot.return_value = items
            with self.assertRaises(ValueError): self.store.accept(self.adapter, T, clock=lambda: T+240000)
        self.snapshot.side_effect = ValueError('Source authorization failure')
        with self.assertRaises(ValueError): self.store.accept(self.adapter, T, clock=lambda: T+240000)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM batches').fetchone(), (0,))

    def test_no_raw_input_bypass_and_wrong_binding(self):
        with self.assertRaises(ValueError): self.store.accept(item(), T, clock=lambda: T+240000)
        self.snapshot.return_value = (item(source=replace(b1.identity(), manifest='f'*64)),)
        with self.assertRaises(ValueError): self.store.accept(self.adapter, T, clock=lambda: T+240000)

    def test_append_only_no_financial_real_and_restart_tampering(self):
        self.accept()
        for table in ('batches', 'reservations', 'confirmations'):
            with self.assertRaises(sqlite3.IntegrityError): self.store._db.execute(f'DELETE FROM {table}')
            self.assertFalse(any(c[2] == 'REAL' for c in self.store._db.execute(f'PRAGMA table_info({table})')))
        self.store._db.rollback()
        self.store._db.execute('DROP TRIGGER no_UPDATE_batches')
        self.store._db.execute("UPDATE batches SET payload='{}'"); self.store._db.commit(); self.store.close()
        with self.assertRaises((ValueError, KeyError)): AcceptanceStore(self.path, self.binding)


class IntegrationTests(unittest.TestCase):
    def test_actual_readonly_adapter_operational_chain_and_no_source_write(self):
        fixture = b1.AdapterTests()
        fixture.setUp()
        try:
            from forward.operations import checkpoint
            before = checkpoint(fixture.source.db)
            bound = binding(source=fixture.identity)
            path = fixture.directory/'artifacts'/'forward-paper-execution'/'b2-integration'
            store = AcceptanceStore(path, bound)
            try:
                result = store.accept(fixture.adapter, T, clock=lambda: T+240000)
                self.assertIsNone(result['reservation'])  # Synthetic source's three NONE signals.
                self.assertEqual(before, checkpoint(fixture.source.db))
                fixture.loader.assert_called_with(fixture.directory)
            finally: store.close()
        finally: fixture.doCleanups()

    def test_b1_preserved_and_b2_identity_deterministic(self):
        self.assertEqual(b1_identity(), 'f3f5e761bcbda57ff75dfb2d514bfc17cd8a54ff57dc79e7e36e5bd4e8aab353')
        self.assertEqual(implementation(), implementation())
        self.assertEqual(binding().id, binding().id)
        self.assertNotEqual(binding().id, binding(active=False).id)
        self.assertEqual(policy()['priority'], ['BTCUSDT', 'ETHUSDT', 'ADAUSDT'])

    def test_no_network_order_or_execution_dependencies(self):
        root = Path(__file__).resolve().parents[1]/'paper'
        allowed = {'dataclasses', 'decimal', 'hashlib', 'pathlib', 'sqlite3', 'time', 'backtest.data',
                   'paper.model', 'paper.source', 'paper.store', 'paper.acceptance', 'research.spec', 'runtime'}
        for name in ('acceptance.py', 'reservations.py'):
            for node in ast.walk(ast.parse((root/name).read_text())):
                if isinstance(node, ast.Import):
                    for n in node.names: self.assertIn(n.name, allowed)
                if isinstance(node, ast.ImportFrom): self.assertIn(node.module, allowed)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('eval', 'exec', '__import__'))
        # B5 owns the explicit CLI. B2's audited imports remain isolated above.
        self.assertNotIn('__main__', (root/'__init__.py').read_text())


if __name__ == '__main__':
    unittest.main()
