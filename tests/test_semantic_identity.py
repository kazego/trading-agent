"""Synthetic source mutations only; no market loading or strategy execution."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from research import identity as legacy
from research import semantic_identity as m

ROOT = Path(__file__).resolve().parents[1]


class SemanticIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = m.snapshot_sources(ROOT)

    def changed(self, path, old, new):
        source = dict(self.old)
        self.assertIn(old, source[path])
        source[path] = source[path].replace(old, new, 1)
        return source

    def reject(self, path, old, new):
        with self.assertRaises(ValueError):
            m.component_identity(self.changed(path, old, new), self.old)

    def test_deterministic(self):
        self.assertEqual(m.component_identity(self.old, self.old), m.component_identity(dict(reversed(list(self.old.items()))), self.old))

    def test_ema(self):
        self.reject('research/features.py', 'adjust=False', 'adjust=True')

    def test_atr(self):
        self.reject('research/features.py', '(13*value+item)/14', '(12*value+item)/14')

    def test_crossover(self):
        self.reject('research/strategy.py', 'fast.iloc[-3] <= slow.iloc[-3]', 'fast.iloc[-3] < slow.iloc[-3]')

    def test_schedule_boundary(self):
        self.reject('research/schedule.py', 'timestamp % STEPS[meta["signal_timeframe"]]', 'timestamp % STEP_MS')

    def test_aggregation(self):
        self.reject('research/data.py', 'max(c.high for c in components)', 'min(c.high for c in components)')

    def test_ordering(self):
        self.reject('research/data.py', 'first.open_time_ms+i*STEP_MS', 'first.open_time_ms+(i+1)*STEP_MS')

    def test_completed_window(self):
        self.reject('research/features.py', 'self._candles[index-600:index]', 'self._candles[index-599:index+1]')

    def test_decimal(self):
        self.reject('research/data.py', 'return sum(values, Decimal(0))', 'return sum(values, 0.0)')

    def test_calendar_only_independence(self):
        changed = self.changed('research/strategy.py',
            'DEV_START <= window.open_time_ms < VALIDATION_START',
            'VALIDATION_START <= window.open_time_ms < HOLDOUT_START')
        before = m.component_identity(self.old, self.old)
        after = m.component_identity(changed, self.old)
        self.assertEqual(before['semantic_fingerprint'], after['semantic_fingerprint'])
        self.assertNotEqual(legacy.fingerprint(before['calendar_components']), legacy.fingerprint(after['calendar_components']))
        self.assertEqual(self.old, m.snapshot_sources(ROOT))

    def test_role_only_independence(self):
        changed = self.changed('research/data.py', 'request.role != Role.DEVELOPMENT', 'request.role != Role.VALIDATION')
        self.assertEqual(m.component_identity(self.old, self.old)['semantic_fingerprint'],
                         m.component_identity(changed, self.old)['semantic_fingerprint'])

    def test_missing_duplicate_and_renamed(self):
        for replacement in ('def renamed(cs):', 'def _atr(cs):\n    pass\n\ndef _atr(cs):'):
            self.reject('research/features.py', 'def _atr(cs):', replacement)

    def test_structure_and_hidden_dependency(self):
        self.reject('research/features.py', 'import math', 'import math\nimport unknown')
        self.reject('research/strategy.py', 'DEV_START <= window.open_time_ms', 'DEV_START + 1 <= window.open_time_ms')

    def test_reporting_not_candidate_scope(self):
        selection = legacy._json((ROOT / 'docs/phase6e-selection.json').read_bytes())
        args = (ROOT, selection['frozen_trial_spec'], selection['frozen_trial_spec']['software_identity'])
        with patch.object(legacy, 'verify_history', return_value=selection):
            before = m.migration_binding(*args)
            with patch.object(legacy, 'infrastructure_identity', return_value={'changed_reporting': True}):
                after = m.migration_binding(*args)
        for key in ('candidate_semantic_identity', 'execution_semantic_identity', 'historical_result', 'historical_source_identity'):
            self.assertEqual(before['record'][key], after['record'][key])
        self.assertNotEqual(before['record']['infrastructure_identity'], after['record']['infrastructure_identity'])

    def test_execution_and_protection_still_locked(self):
        selection = legacy._json((ROOT / 'docs/phase6e-selection.json').read_bytes())
        original = legacy.source_hashes
        for name in ('research/protection.py', 'backtest/simulation.py'):
            def changed(root, paths):
                result = original(root, paths)
                if name in result: result[name] = '0' * 64
                return result
            with self.subTest(name=name), patch.object(legacy, 'verify_history', return_value=selection), \
                 patch.object(legacy, 'source_hashes', side_effect=changed):
                with self.assertRaises(ValueError):
                    m.migration_binding(ROOT, selection['frozen_trial_spec'], selection['frozen_trial_spec']['software_identity'])

    @unittest.skipUnless((ROOT / 'artifacts/research/registry/events.jsonl').exists(), 'Local evidence unavailable')
    def test_migration_preserves_evidence_and_runtime(self):
        paths = [ROOT / p for p in m.COMPONENTS]
        paths += [ROOT / 'artifacts/research/registry/events.jsonl', ROOT / 'docs/phase6e-selection.json']
        before = {str(p): legacy.sha(p.read_bytes()) for p in paths}
        selection = legacy._json((ROOT / 'docs/phase6e-selection.json').read_bytes())
        with patch.object(legacy.Registry, '_append', side_effect=AssertionError('No writes')):
            binding = m.migration_binding(ROOT, selection['frozen_trial_spec'], selection['frozen_trial_spec']['software_identity'])
        self.assertEqual(binding['record']['historical_selection'], legacy.SELECTION)
        self.assertEqual(before, {str(p): legacy.sha(p.read_bytes()) for p in paths})


if __name__ == '__main__': unittest.main()
