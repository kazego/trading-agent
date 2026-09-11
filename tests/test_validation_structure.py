"""Metadata-only Phase 6F0 checks; no candles or simulation calls."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import validation as v
from research import identity as legacy
from research import semantic_identity as m
from research.data import authorize
from research.spec import SplitRequest, Role, DEV_START, VALIDATION_START, HOLDOUT_START

ROOT = Path(__file__).resolve().parents[1]


class ValidationStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = legacy._json((ROOT/'docs/phase6e-selection.json').read_bytes())
        cls.spec = s['frozen_trial_spec']
        cls.software = cls.spec['software_identity']
        cls.binding = m.migration_binding(ROOT, cls.spec, cls.software)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifacts = Path(self.temp.name)/'research'
        self.mock = patch.object(m, 'migration_binding', side_effect=self.checked_binding)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.plan = v.plan_validation(ROOT, frozen_spec=self.spec, software=self.software)

    def checked_binding(self, root, spec, software):
        if spec != self.spec or software != self.software: raise ValueError('Frozen input changed')
        return copy.deepcopy(self.binding)

    def check(self, plan=None):
        return v.preflight(ROOT, self.plan if plan is None else plan, artifact_root=self.artifacts)

    def mutate(self, key, value):
        r = self.plan.record
        r[key] = value
        return v.ValidationPlan(v.encoded(r))

    def test_exact_structure_ready_no_files(self):
        result = self.check()
        self.assertEqual(result['status'], 'READY')
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['context'].validation_attempts, 0)
        self.assertFalse(self.artifacts.exists())

    def test_wrong_candidates(self):
        for name in ('phase6d-a-ribbon-v1','phase6d-b-breakout-v1','reference','other'):
            with self.subTest(name=name), self.assertRaises((PermissionError, ValueError)):
                self.check(self.mutate('candidate', name))

    def test_frozen_provenance_mutations(self):
        for key in ('historical_selection','historical_result','candidate_semantic_identity','historical_source_identity'):
            r = self.plan.record
            r['historical_binding']['record'][key] = '0'*64
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(v.ValidationPlan(v.encoded(r)))
        r = self.plan.record
        r['historical_binding']['fingerprint'] = '0'*64
        with self.assertRaises(ValueError): self.check(v.ValidationPlan(v.encoded(r)))

    def test_infrastructure_mutation(self):
        with self.assertRaises(ValueError): self.check(self.mutate('infrastructure_fingerprint','0'*64))

    def test_strategy_protection_and_parameters(self):
        for key in ('strategy_parameters','protection_parameters','configuration'):
            r = self.plan.record
            r['frozen_specification'][key] = {}
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(v.ValidationPlan(v.encoded(r)))

    def test_intervals(self):
        for key in ('start_ms','end_ms','warmup_start_ms','warmup_end_ms'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(self.mutate(key, self.plan.record[key]+v.STEP_MS))

    def test_history_semantics(self):
        for key,value in (('warmup_role','DEVELOPMENT'),('completed_history',599),('legacy_ema_history',100)):
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(self.mutate(key,value))

    def test_calendar_boundaries(self):
        c = self.check()['context']
        c.check_boundary(VALIDATION_START, signal=True)
        c.check_boundary(HOLDOUT_START-v.STEP_MS)
        for timestamp in (DEV_START,VALIDATION_START-v.STEP_MS,HOLDOUT_START,HOLDOUT_START+v.STEP_MS):
            with self.subTest(timestamp=timestamp), self.assertRaises(PermissionError): c.check_boundary(timestamp)

    def test_no_2026_dependency(self):
        c = self.check()['context']
        c.check_history(v.WARMUP_START,VALIDATION_START,decision_time=VALIDATION_START)
        with self.assertRaises(PermissionError): c.check_history(v.WARMUP_START,HOLDOUT_START,decision_time=VALIDATION_START)
        with self.assertRaises(PermissionError): c.check_history(HOLDOUT_START-600*v.SIGNAL_STEP,HOLDOUT_START,decision_time=HOLDOUT_START)

    def test_paths_and_collision(self):
        p = v.artifact_destination(self.artifacts,self.plan)
        self.assertEqual(p.name,'validation')
        self.assertNotEqual(p,p.parent/'development')
        p.mkdir(parents=True)
        with self.assertRaises(FileExistsError): self.check()

    def test_attempt_rejection(self):
        with patch.object(v.Registry,'events',return_value=[dict(kind='ATTEMPT')]):
            with self.assertRaises(PermissionError): self.check()

    def test_classification_and_no_holdout(self):
        self.assertEqual(self.check()['context'].reporting['split'],'LOCKED_PREVIOUSLY_OBSERVED_VALIDATION')
        for key,value in (('split',Role.HOLDOUT.value),('holdout_authorized',True),('development_closed',False),
                          ('execution_authorized',True),('market_data_access_authorized',True)):
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(self.mutate(key,value))

    def test_development_unchanged(self):
        authorize(SplitRequest(Role.DEVELOPMENT,DEV_START,VALIDATION_START,DEV_START-99*v.SIGNAL_STEP),'4h')
        with self.assertRaises(PermissionError):
            authorize(SplitRequest(Role.VALIDATION,VALIDATION_START,HOLDOUT_START,VALIDATION_START-99*v.SIGNAL_STEP),'4h')

    def test_determinism(self):
        self.assertEqual(self.plan,v.plan_validation(ROOT,frozen_spec=self.spec,software=self.software))


if __name__ == '__main__': unittest.main()
