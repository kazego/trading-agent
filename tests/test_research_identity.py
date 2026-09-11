"""Offline provenance tests. Never load candles or execute a research run."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import identity as i

ROOT = Path(__file__).resolve().parents[1]


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.selection = json.loads((ROOT / 'docs/phase6e-selection.json').read_text())
        self.spec = self.selection['frozen_trial_spec']
        self.software = self.spec['software_identity']

    def bind(self, spec=None):
        with patch.object(i, 'verify_history', return_value=copy.deepcopy(self.selection)):
            return i.candidate_binding(ROOT, self.spec if spec is None else spec, self.software)

    def test_binding_deterministic(self):
        self.assertEqual(self.bind(), self.bind())

    def test_parameters_fail_closed(self):
        for key, value in [('strategy_parameters', {'fast': 10, 'slow': 21}),
                           ('protection_parameters', {}), ('configuration', {}),
                           ('priority', []), ('max_positions', 2)]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.spec)
                changed[key] = value
                with self.assertRaises(ValueError): self.bind(changed)

    def test_strategy_protection_execution_mutations(self):
        original = i.source_hashes
        for name in ('research/strategy.py', 'research/protection.py', 'backtest/simulation.py',
                     'backtest/portfolio.py', 'research/features.py'):
            def changed(root, paths):
                result = original(root, paths)
                if name in result: result[name] = '0' * 64
                return result
            with self.subTest(name=name), patch.object(i, 'source_hashes', side_effect=changed):
                with self.assertRaises(ValueError): self.bind()

    def test_infrastructure_mutation_changes_only_infrastructure(self):
        before = self.bind()
        original = i.source_hashes
        def changed(root, paths):
            result = original(root, paths)
            if 'research/scorecard.py' in result: result['research/scorecard.py'] = '0' * 64
            return result
        with patch.object(i, 'source_hashes', side_effect=changed): after = self.bind()
        for key in ('candidate_semantics', 'historical_source_identity', 'historical_result_fingerprint'):
            self.assertEqual(before['record'][key], after['record'][key])
        self.assertNotEqual(before['record']['current_infrastructure_identity'],
                            after['record']['current_infrastructure_identity'])

    def test_unlisted_files_do_not_enter_explicit_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in i.INFRASTRUCTURE_FILES:
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('fixture')
            before = i.infrastructure_identity(root, self.software)
            (root / 'research/unrelated_validation.py').write_text('not an active component')
            self.assertEqual(before, i.infrastructure_identity(root, self.software))

    def test_current_source_cannot_replace_historical_source(self):
        changed = copy.deepcopy(self.spec)
        changed['source_identity']['research/runner.py'] = '0' * 64
        with self.assertRaises(ValueError): self.bind(changed)

    def test_snapshot_tampering_rejected(self):
        with patch.object(i.subprocess, 'check_output', return_value=b'changed'):
            with self.assertRaises(ValueError):
                i.verify_snapshot(ROOT, {'strategy.py': '0' * 64}, self.software, i.SNAPSHOT_D)

    def test_snapshot_not_current_working_tree(self):
        with patch.object(i.subprocess, 'check_output', return_value=b'old\r\n'), \
             patch.object(i, 'source_hashes', side_effect=AssertionError('Current tree accessed')):
            i.verify_snapshot(ROOT, {'strategy.py': i.sha(b'old\n')}, self.software, i.SNAPSHOT_D)

    def test_metadata_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'record.json'
            body = {'value': 'original'}
            anchor = i.fingerprint(body)
            p.write_bytes(i.encoded(dict(body, identity=anchor)))
            i.signed_record(p, 'identity', anchor)
            p.write_bytes(i.encoded(dict(value='changed', identity=anchor)))
            with self.assertRaises(ValueError): i.signed_record(p, 'identity', anchor)

    def test_noncanonical_numbers_and_duplicate_fields(self):
        for raw in ('{"a":1.5}', '{"a":NaN}', '{"a":1,"a":2}'):
            with self.assertRaises(ValueError): i._json(raw)

    def test_source_path_escape_rejected(self):
        for name in ('../x', '/x', 'C:/x', 'a\\b'):
            with self.assertRaises(ValueError): i._relative(name)

    @unittest.skipUnless((ROOT / 'artifacts/research/registry/events.jsonl').exists(),
                         'Preserved local evidence unavailable')
    def test_all_preserved_evidence_read_only(self):
        # Explicit result/governance files only: no market dataset discovery.
        paths = [ROOT / 'artifacts/research/registry/events.jsonl',
                 ROOT / 'docs/phase6e-selection.json',
                 ROOT / 'artifacts/research/phase6d6-comparison-v1/comparison.json']
        ids = set(i.PRIOR_MANIFESTS) | {v['experiment'] for v in self.selection['evidence']['identities'].values()}
        for identity in ids:
            paths.extend(ROOT / 'artifacts/research' / identity / 'development' / name
                         for name in ('approved-inputs.json', 'first-result.json', 'scorecard.json',
                                      'report.txt', 'run-manifest.json', 'fingerprint.txt'))
        before = {str(p): i.sha(p.read_bytes()) for p in paths}
        with patch.object(i.Registry, '_append', side_effect=AssertionError('Write attempted')), \
             patch.object(i, 'source_hashes', side_effect=AssertionError('Current tree consulted')):
            result = i.verify_history(ROOT)
        self.assertEqual(result['selection_fingerprint'], i.SELECTION)
        self.assertEqual(before, {str(p): i.sha(p.read_bytes()) for p in paths})


if __name__ == '__main__': unittest.main()
