"""B1 offline foundations; synthetic databases only, no activation or execution."""
import ast
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backtest.data import Candle, SYMBOLS
from forward import operations as op
from forward.manifest import implementation as source_implementation
from forward.readout import connect
from forward.store import Store
from paper.model import (CONTRACT, LABEL, DraftManifest, PaperConfig, SourceIdentity,
                         SourceObservation, encoded, fingerprint, implementation, parse)
from paper.source import SourceAdapter
from paper.store import PaperStore


T = 1789171200000


def identity():
    return SourceIdentity('1'*64, '2'*64, '3'*64, '4'*64, T, T)


def observation(source=None):
    source = source or identity()
    decisions = []
    for symbol in SYMBOLS:
        d = dict(label=LABEL, candidate='phase6d-c-atr-v1', contract=CONTRACT,
                 symbol=symbol, boundary_ms=T, signal_timeframe='4h', signal='NONE',
                 completed_through_ms=T-1, atr14='2')
        decisions.append(dict(d, signal_fingerprint=fingerprint(d)))
    values = {tf: {'role': 'DESCRIPTIVE_ONLY'} for tf in ('1D', '1H', '15M')}
    return dict(label=LABEL, manifest_fingerprint=source.manifest, boundary_ms=T,
                observed_ms=T+180000, timing='UNCONFIRMED',
                operations_fingerprint=source.operations, evidence_role='PROSPECTIVE_INTERVAL',
                decisions=decisions, descriptive_context={s: dict(values=values,
                    context_fingerprint=fingerprint(values)) for s in SYMBOLS})


def receipt(o):
    return dict(label=LABEL, event='RECEIPT', boundary_ms=T, observed_ms=T+180001,
                observation_fingerprint=fingerprint(o), operations_fingerprint=o['operations_fingerprint'],
                tolerance_ms=300000, timing='ONLINE')


def evidence(source=None):
    source = source or identity()
    o = observation(source)
    return SourceObservation(source, encoded(o), encoded(receipt(o)))


class ModelTests(unittest.TestCase):
    def test_immutable_deterministic_draft_and_fresh_records(self):
        a = DraftManifest(identity())
        self.assertEqual(a.id, DraftManifest(identity()).id)
        self.assertEqual(a.implementation_fingerprint, implementation())
        self.assertEqual(a.record['state'], 'DRAFT_NOT_ACTIVATED')
        self.assertIsNone(a.prospective_start_ms)
        self.assertIsNone(a.rules_fingerprint)
        with self.assertRaises(FrozenInstanceError):
            a.prospective_start_ms = T
        record = a.record
        record['config']['priority'].reverse()
        self.assertEqual(a.config.record['priority'], list(SYMBOLS))
        self.assertEqual(parse(encoded(a.record)), a.record)

    def test_no_activation_or_hidden_runtime(self):
        with self.assertRaises(ValueError): DraftManifest(identity(), prospective_start_ms=T)
        with self.assertRaises(ValueError): DraftManifest(identity(), implementation_fingerprint='0'*64)
        with self.assertRaises(ValueError): DraftManifest(identity(), rules_fingerprint='bad')

    def test_financial_decimal_and_frozen_parameters(self):
        self.assertEqual(PaperConfig().record['risk_fraction'], '0.01')
        for value in (0.01, Decimal('NaN'), Decimal('Infinity'), Decimal('0.02'), True):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                PaperConfig(risk_fraction=value)
        self.assertEqual(PaperConfig(risk_fraction='0.0100').id, PaperConfig().id)

    def test_source_identity_rejects_bad_fields(self):
        for change in ({'manifest': '{}'}, {'semantic_contract': '0'*64},
                       {'first_observation_ms': T+1}, {'operational_effective_ms': T-14400000}):
            with self.subTest(change=change), self.assertRaises(ValueError): replace(identity(), **change)

    def test_canonical_rejects_float_and_noncanonical_json(self):
        for raw in (b'{"a":1.0}', b'{"a":NaN}', b'{ "a":1}', b'{"a":1,"a":1}'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)): parse(raw)

    def test_exact_observation_and_receipt_provenance(self):
        item = evidence()
        self.assertEqual(item.effective_timing, 'ONLINE')
        self.assertEqual(item.id, evidence().id)
        self.assertEqual(item.record['observation'], observation())
        with self.assertRaises(FrozenInstanceError): item.observation = b'{}'

    def test_unconfirmed_and_legacy_are_preserved_not_upgraded(self):
        o = observation()
        self.assertEqual(SourceObservation(identity(), encoded(o), None).effective_timing, 'UNCONFIRMED')
        del o['operations_fingerprint']
        o['timing'] = 'DELAYED_RECONSTRUCTED'
        self.assertEqual(SourceObservation(identity(), encoded(o), None).effective_timing, 'DELAYED_RECONSTRUCTED')

    def test_mutated_decision_context_receipt_rejected(self):
        for field in ('decision', 'context', 'manifest', 'receipt'):
            o = observation(); r = receipt(o)
            if field == 'decision': o['decisions'][0]['signal'] = 'LONG'
            if field == 'context': o['descriptive_context']['BTCUSDT']['values']['1D']['role'] = 'WRONG'
            if field == 'manifest': o['manifest_fingerprint'] = '0'*64
            if field == 'receipt': r['observation_fingerprint'] = '0'*64
            with self.subTest(field=field), self.assertRaises(ValueError):
                SourceObservation(identity(), encoded(o), encoded(r))

    def test_receipt_timing_inclusive_and_recovery(self):
        o = observation(); r = receipt(o)
        r['observed_ms'] = T+300000
        self.assertEqual(SourceObservation(identity(), encoded(o), encoded(r)).effective_timing, 'ONLINE')
        r['observed_ms'] += 1
        with self.assertRaises(ValueError): SourceObservation(identity(), encoded(o), encoded(r))
        r['timing'] = 'DELAYED_RECONSTRUCTED'
        SourceObservation(identity(), encoded(o), encoded(r))
        r.update(event='RECOVERED_RECEIPT', code='COMMIT_TIME_UNKNOWN')
        SourceObservation(identity(), encoded(o), encoded(r))

    def test_zero_atr_is_evidence_not_an_entry_eligibility_decision(self):
        o = observation()
        d = o['decisions'][0]
        d['atr14'] = '0'
        d['signal_fingerprint'] = fingerprint({k: v for k, v in d.items() if k != 'signal_fingerprint'})
        item = SourceObservation(identity(), encoded(o), encoded(receipt(o)))
        self.assertEqual(parse(item.observation)['decisions'][0]['atr14'], '0')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/'artifacts'/'forward-paper-execution'/'b1-test'
        self.manifest = DraftManifest(identity())
        self.store = PaperStore(self.directory, self.manifest)
        self.addCleanup(self.store.close)

    def test_separate_namespace_rejects_source_and_activated_run(self):
        for directory in (Path(self.temp.name)/'artifacts'/'forward-paper'/'live-0a-001',
                          self.directory.parent/'live-0b-001'):
            with self.assertRaises(ValueError): PaperStore(directory, self.manifest)
            self.assertFalse(directory.exists())

    def test_idempotent_and_reopen(self):
        item = evidence()
        self.assertEqual(self.store.record_source(item), item.id)
        self.store.record_source(item)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM evidence').fetchone(), (1,))
        self.store.close()
        with self.assertRaises(ValueError): PaperStore(self.directory, replace(self.manifest, rules_fingerprint='f'*64))
        reopened = PaperStore(self.directory, self.manifest)
        reopened.verify(); reopened.close()

    def test_late_receipt_append_preserves_original(self):
        full = evidence()
        early = SourceObservation(full.source, full.observation, None)
        self.store.record_source(early)
        self.store.record_source(full)
        self.store.record_source(early)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM evidence').fetchone(), (2,))
        self.assertEqual(self.store._db.execute('SELECT payload FROM source_observations').fetchone(),
                         (full.observation.decode(),))
        self.store.verify()

    def test_conflict_fails_without_replacing_evidence(self):
        item = evidence(); self.store.record_source(item)
        o = parse(item.observation); o['observed_ms'] += 1
        other = SourceObservation(item.source, encoded(o), None)
        with self.assertRaises(ValueError): self.store.record_source(other)
        r = parse(item.receipt); r['observed_ms'] += 1
        with self.assertRaises(ValueError):
            self.store.record_source(SourceObservation(item.source, item.observation, encoded(r)))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM evidence').fetchone(), (1,))
        self.store.verify()

    def test_transaction_rolls_back_all_parts(self):
        self.store._db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON source_receipts "
                               "BEGIN SELECT RAISE(ABORT,'Injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.record_source(evidence())
        for table in ('source_observations', 'source_receipts', 'evidence'):
            self.assertEqual(self.store._db.execute(f'SELECT count(*) FROM {table}').fetchone(), (0,))

    def test_append_only(self):
        self.store.record_source(evidence())
        for table in ('identity', 'source_observations', 'source_receipts', 'evidence'):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.store._db.execute(f'DELETE FROM {table}')

    def test_one_global_pending_or_open_slot_across_all_symbols(self):
        # Schema constraint test only: no public reservation/position operation exists.
        with self.store._db:
            self.store._db.execute("UPDATE global_slot SET phase='PENDING',owner_id='synthetic',symbol='BTCUSDT'")
        for symbol in SYMBOLS:
            for phase in ('PENDING', 'OPEN'):
                for slot in (1, 2):
                    with self.subTest(symbol=symbol, phase=phase, slot=slot), self.assertRaises(sqlite3.IntegrityError):
                        self.store._db.execute('INSERT INTO global_slot VALUES(?,?,?,?)', (slot, phase, 'other', symbol))
        with self.assertRaises(ValueError): self.store.verify()
        self.store._db.rollback()
        with self.store._db:
            self.store._db.execute("UPDATE global_slot SET phase='EMPTY',owner_id=NULL,symbol=NULL")
        self.store.verify()
        self.assertFalse(hasattr(self.store, 'reserve_entry'))

    def test_no_real_financial_columns(self):
        for (table,) in self.store._db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            self.assertFalse(any(row[2] == 'REAL' for row in self.store._db.execute(f'PRAGMA table_info({table})')))
        payload = self.store._db.execute('SELECT payload FROM identity').fetchone()[0]
        self.assertEqual(parse(payload.encode())['config']['starting_equity'], '1000')

    def test_reopen_detects_corrupt_evidence(self):
        self.store.record_source(evidence())
        self.store._db.execute('DROP TRIGGER no_UPDATE_evidence')
        self.store._db.execute("UPDATE evidence SET payload='{}'")
        self.store._db.commit(); self.store.close()
        with self.assertRaises((KeyError, ValueError)):
            PaperStore(self.directory, self.manifest)

    def test_wrong_source_rejected(self):
        with self.assertRaises(ValueError):
            self.store.record_source(evidence(replace(identity(), manifest='a'*64)))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM evidence').fetchone(), (0,))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.meta = dict(manifest_fingerprint='1'*64, history_start_ms=T-900000, first_observation_ms=T)
        self.source = Store(self.directory, self.meta); self.addCleanup(self.source.close)
        for symbol in SYMBOLS:
            self.source.append(symbol, [Candle(symbol, '15m', T-900000, T-1, '100', '101', '99', '100', '1')],
                               received_ms=T+1)
        self.root = dict(label=LABEL, version='operations-v1', manifest_fingerprint='1'*64,
                         implementation=op.PATCH_OLD, effective_boundary_ms=T,
                         policy=op.policy(), policy_fingerprint=fingerprint(op.policy()),
                         checkpoint=op.checkpoint(self.source.db))
        self.root['operations_fingerprint'] = fingerprint(self.root)
        self.directory.joinpath('operations-v1.json').write_bytes(encoded(self.root)+b'\n')
        self.patch_root = patch.object(op, 'PATCH_ROOT', self.root['operations_fingerprint'])
        self.patch_root.start(); self.addCleanup(self.patch_root.stop)
        body = op._patch_body(self.root, op.checkpoint(self.source.db), T)
        self.authorization = dict(body, patch_fingerprint=fingerprint(body))
        self.directory.joinpath('operations-patch-v1.json').write_bytes(encoded(self.authorization)+b'\n')
        self.identity = SourceIdentity('1'*64, self.root['operations_fingerprint'],
                                       self.authorization['patch_fingerprint'], source_implementation(), T, T)
        self.item = evidence(self.identity)
        self.source.db.execute('INSERT INTO observations VALUES(?,?,?)',
                               (T, self.item.observation.decode(), fingerprint(parse(self.item.observation))))
        self.source.db.execute('INSERT INTO events(payload) VALUES(?)', (self.item.receipt.decode(),))
        self.source.db.commit()
        # Only the manifest fixture is substituted; the real operational chain,
        # SQLite readonly verifier, checkpoints and receipt validation are exercised.
        self.loader = patch('paper.source.load', return_value=self.meta).start()
        self.addCleanup(patch.stopall)
        self.adapter = SourceAdapter(self.directory, self.identity)

    def test_provenance_readonly_and_unchanged_source(self):
        before = op.checkpoint(self.source.db)
        with patch('paper.source.connect', wraps=connect) as opened:
            self.assertEqual(self.adapter.snapshot(), (self.item,))
            opened.assert_called_once()
        self.loader.assert_called_once_with(self.directory)
        self.assertEqual(before, op.checkpoint(self.source.db))
        for name in ('execute', 'db', 'write', 'append', 'poll'):
            self.assertFalse(hasattr(self.adapter, name))
        with connect(self.directory, self.meta) as db:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM observations')
            db.execute('PRAGMA query_only=OFF')
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM observations')

    def test_production_loader_failure_is_not_bypassed(self):
        self.loader.side_effect = ValueError('Authorization failed')
        with self.assertRaises(ValueError): self.adapter.snapshot()

    def test_missing_and_tampered_patch(self):
        path = self.directory/'operations-patch-v1.json'
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError): self.adapter.snapshot()
        path.write_bytes(original+b' ')
        with self.assertRaises(ValueError): self.adapter.snapshot()

    def test_unknown_expected_source_rejected(self):
        for field in ('manifest', 'operations', 'patch', 'implementation'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                SourceAdapter(self.directory, replace(self.identity, **{field: '0'*64})).snapshot()

    def test_tampered_observation_rejected(self):
        self.source.db.execute('DROP TRIGGER no_UPDATE_observations')
        self.source.db.execute("UPDATE observations SET payload='{}'"); self.source.db.commit()
        with self.assertRaises((ValueError, KeyError)): self.adapter.snapshot()

    def test_tampered_receipt_rejected(self):
        self.source.db.execute('DROP TRIGGER no_UPDATE_events')
        r = parse(self.item.receipt); r['observation_fingerprint'] = '0'*64
        self.source.db.execute('UPDATE events SET payload=?', (encoded(r).decode(),)); self.source.db.commit()
        with self.assertRaises(ValueError): self.adapter.snapshot()

    def test_checkpoint_and_blocked_fail_closed(self):
        self.source.db.execute('DROP TRIGGER no_UPDATE_candles')
        self.source.db.execute('UPDATE candles SET received_ms=received_ms+1'); self.source.db.commit()
        with self.assertRaises(ValueError): self.adapter.snapshot()

    def test_blocked_source_no_copy(self):
        self.directory.joinpath('BLOCKED.json').write_text('{}')
        with self.assertRaises(ValueError): self.adapter.snapshot()


class IsolationTests(unittest.TestCase):
    def test_no_network_sdk_or_execution_imports_or_cli(self):
        root = Path(__file__).resolve().parents[1]/'paper'
        allowed = {'dataclasses', 'decimal', 'hashlib', 'importlib.metadata', 'json', 'pathlib',
                   'platform', 'sqlite3', 'backtest.data', 'research.spec', 'runtime',
                   'forward.manifest', 'forward.operations', 'forward.readout', 'paper.model'}
        for path in root.glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for name in node.names: self.assertIn(name.name, allowed)
                if isinstance(node, ast.ImportFrom): self.assertIn(node.module, allowed)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('eval', 'exec', '__import__'))
        self.assertFalse((root/'__main__.py').exists())


if __name__ == '__main__':
    unittest.main()
