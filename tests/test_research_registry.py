"""Offline metadata tests. All ledgers live in temporary directories."""
from dataclasses import FrozenInstanceError
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from research.spec import (ExperimentSpec, Hypothesis, Role, SplitRequest, policy, encoded,
                           fingerprint, SYMBOLS, DEV_START, VALIDATION_START, HOLDOUT_START, STEP_MS)
from research.registry import Registry

HASH = "a"*64
DATASETS = tuple((s, HASH) for s in SYMBOLS)


def spec(timeframe="15m"):
    return ExperimentSpec(timeframe, "research-ema-v1", (("strategy", HASH),), DATASETS, (("python", "test"),))


def request(role=Role.DEVELOPMENT, end=None):
    start, stop = {Role.DEVELOPMENT: (DEV_START, VALIDATION_START),
                   Role.VALIDATION: (VALIDATION_START, HOLDOUT_START),
                   Role.HOLDOUT: (HOLDOUT_START, HOLDOUT_START+96*STEP_MS)}[role]
    return SplitRequest(role, start, stop if end is None else end, start-99*STEP_MS)


class SpecificationTests(unittest.TestCase):
    def test_identity_order_and_immutability(self):
        original = spec()
        other = ExperimentSpec("15m", "research-ema-v1", (("strategy", HASH),), tuple(reversed(DATASETS)), (("python", "test"),))
        self.assertEqual(original.id, other.id)
        self.assertNotEqual(original.id, spec("1h").id)
        with self.assertRaises(FrozenInstanceError): original.signal_timeframe = "4h"
        copy = original.record
        copy["policy"]["starting_equity"] = "99"
        self.assertEqual(original.record["policy"]["starting_equity"], "1000")

    def test_canonical_decimal(self):
        self.assertEqual(encoded({"x": Decimal("1.000"), "y": Decimal("-0")}), b'{"x":"1","y":"0"}')
        self.assertEqual(fingerprint({"a": 1,"b": 2}),fingerprint({"b": 2,"a": 1}))
        for x in (0.1, Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(ValueError): encoded(x)

    def test_observed_validation_and_soft_target(self):
        p=policy()
        self.assertIn("baseline-001 and Phase 6A",p["validation_observation"])
        self.assertIsNone(p["roles"][Role.HOLDOUT.value][1])
        self.assertFalse(p["eligibility"]["trade_target_is_hard_rejection"])
        self.assertEqual(p["eligibility"]["completed_trade_evidence_target"],100)
        self.assertEqual(p["eligibility"]["max_mtm_drawdown_pct_lt"],"25")

    def test_wrong_intervals_roles_units(self):
        for role,start,end in ((Role.DEVELOPMENT,DEV_START, HOLDOUT_START),
                               (Role.VALIDATION,DEV_START,HOLDOUT_START),
                               ("VALIDATION",VALIDATION_START,HOLDOUT_START),
                               (Role.DEVELOPMENT,DEV_START//1000,VALIDATION_START//1000)):
            with self.assertRaises(ValueError): SplitRequest(role,start,end,start)

    def test_half_open_and_warmup_not_evaluation(self):
        r=request()
        self.assertFalse(r.evaluation_allowed(r.warmup_start_ms))
        self.assertTrue(r.evaluation_allowed(r.start_ms))
        self.assertFalse(r.evaluation_allowed(r.end_ms))
        self.assertFalse(r.evaluation_allowed(r.start_ms+1))

    def test_unapproved_timeframe_and_hash(self):
        with self.assertRaises(ValueError): spec("1d")
        with self.assertRaises(ValueError): ExperimentSpec("15m","v",(("s","bad"),),DATASETS,(("p","v"),))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry=Registry(Path(self.temp.name)/"ledger")
        self.h=self.registry.register_hypothesis(Hypothesis("Question","Mechanism","Comparison"))
        self.id,_=self.registry.register_experiment(spec(),self.h)

    def complete(self,role):
        r=request(role)
        self.registry.record_access(self.id,r,"READ","test operator")
        attempt=self.registry.begin_attempt(self.id,r,DATASETS)
        self.registry.finish_attempt(attempt,"COMPLETED",HASH)
        return attempt

    def test_duplicate_same_identity_records_not_trials(self):
        identity,new=self.registry.register_experiment(spec(),self.h)
        self.assertEqual(identity,self.id);self.assertFalse(new)
        self.assertEqual(sum(e['kind']=='EXPERIMENT' for e in self.registry.events()),1)
        self.assertEqual(self.registry.events()[-1]['kind'],'DUPLICATE')

    def test_three_slots_only(self):
        for tf in ('1h','4h'): self.registry.register_experiment(spec(tf),self.h)
        changed=ExperimentSpec('15m','changed',(("s",HASH),),DATASETS,(("p","test"),))
        with self.assertRaises(ValueError): self.registry.register_experiment(changed,self.h)
        self.assertEqual(sum(e['kind']=='EXPERIMENT' for e in self.registry.events()),3)

    def test_ancestry_prior_registered_only(self):
        child=Hypothesis('Child','Mechanism','Comparison',(self.id,),True)
        child_id=self.registry.register_hypothesis(child)
        self.registry.register_experiment(spec('1h'),child_id)
        with self.assertRaises(ValueError): self.registry.register_hypothesis(Hypothesis('Bad','M','C',('b'*64,)))
        self.assertEqual(child.record['parents'],[self.id])

    def test_failed_retry_same_run_not_independent(self):
        r=request();self.registry.record_access(self.id,r,'READ','operator')
        first=self.registry.begin_attempt(self.id,r,DATASETS)
        with self.assertRaises(ValueError):self.registry.begin_attempt(self.id,r,DATASETS)
        self.registry.finish_attempt(first,'FAILED',error='synthetic failure')
        second=self.registry.begin_attempt(self.id,r,DATASETS)
        attempts=[e for e in self.registry.events() if e['kind']=='ATTEMPT']
        self.assertEqual(attempts[0]['payload']['run_id'],attempts[1]['payload']['run_id'])
        self.assertEqual(attempts[1]['payload']['attempt_number'],2)
        self.registry.finish_attempt(second,'COMPLETED',HASH)
        with self.assertRaises(ValueError):self.registry.begin_attempt(self.id,r,DATASETS)
        with self.assertRaises(ValueError):self.registry.finish_attempt(second,'FAILED',error='replacement')

    def test_validation_promotion_requires_completed_development(self):
        with self.assertRaises(PermissionError):self.registry.record_access(self.id,request(Role.VALIDATION),'READ','operator')
        with self.assertRaises(ValueError):self.registry.promote_validation(self.id,'operator','manual decision')
        self.complete(Role.DEVELOPMENT)
        self.registry.promote_validation(self.id,'operator','Eligibility reviewed manually; no scorecard calculated')
        self.registry.record_access(self.id,request(Role.VALIDATION),'READ','operator')
        with self.assertRaises(ValueError):self.registry.promote_validation(self.id,'operator','replace')

    def test_holdout_denied_without_separate_authorization(self):
        for purpose in ('READ','ACQUISITION'):
            with self.assertRaises(PermissionError):self.registry.record_access(self.id,request(Role.HOLDOUT),purpose,'operator')
        with self.assertRaises(ValueError):self.registry.authorize_holdout(self.id,request(Role.HOLDOUT).end_ms,'operator','reason')

    def test_future_endpoint_frozen_before_metadata_access(self):
        self.complete(Role.DEVELOPMENT)
        self.registry.promote_validation(self.id,'operator','reason')
        self.complete(Role.VALIDATION)
        r=request(Role.HOLDOUT)
        self.registry.authorize_holdout(self.id,r.end_ms,'operator','future authorization model test only')
        self.registry.record_access(self.id,r,'ACQUISITION','operator')
        with self.assertRaises(PermissionError):self.registry.record_access(self.id,request(Role.HOLDOUT,r.end_ms+STEP_MS),'READ','operator')
        with self.assertRaises(ValueError):self.registry.authorize_holdout(self.id,r.end_ms+STEP_MS,'operator','replace')
        kinds=[e['kind'] for e in self.registry.events()]
        self.assertLess(kinds.index('HOLDOUT_AUTHORIZATION'),len(kinds)-1)

    def test_access_required_and_frozen_datasets(self):
        with self.assertRaises(PermissionError): self.registry.begin_attempt(self.id,request(),DATASETS)
        self.registry.record_access(self.id,request(),'READ','operator')
        with self.assertRaises(ValueError): self.registry.begin_attempt(self.id,request(),tuple((s,'b'*64) for s in SYMBOLS))
        bad=SplitRequest(Role.DEVELOPMENT,DEV_START,VALIDATION_START,DEV_START)
        with self.assertRaises(ValueError): self.registry.record_access(self.id,bad,'READ','operator')

    def test_append_only_reopen_and_corruption(self):
        before=self.registry.path.read_bytes()
        self.registry.record_rejection(spec('1h'),self.h,'not run')
        self.assertTrue(self.registry.path.read_bytes().startswith(before))
        self.assertEqual(Registry(self.registry.directory).events(),self.registry.events())
        with self.registry.path.open('ab') as stream:stream.write(b'{')
        with self.assertRaises(ValueError):self.registry.events()
        with self.assertRaises(ValueError):self.registry.register_experiment(spec('4h'),self.h)

    def test_writer_lock_fail_closed(self):
        (self.registry.directory/'writer.lock').write_text('stale')
        with self.assertRaises(FileExistsError):self.registry.register_experiment(spec('1h'),self.h)
        self.assertTrue((self.registry.directory/'writer.lock').exists())

    def test_disclosures_and_prior_exposure(self):
        with self.assertRaises(ValueError):self.registry.record_disclosure(HASH,'operator','VIEWED','reason')
        self.registry.record_disclosure(HASH,'operator','PRIOR_EXPOSURE','baseline-001 and Phase 6A observed 2025')
        self.complete(Role.DEVELOPMENT)
        self.registry.record_disclosure(HASH,'operator','VIEWED','read result')
        self.registry.record_disclosure(HASH,'operator','DISCLOSED','shared result')
        self.assertEqual(self.registry.events()[-1]['kind'],'DISCLOSED')

    def test_same_actions_produce_same_ledger(self):
        other=Registry(Path(self.temp.name)/'other')
        h=other.register_hypothesis(Hypothesis('Question','Mechanism','Comparison'))
        other.register_experiment(spec(),h)
        self.assertEqual(other.path.read_bytes(),self.registry.path.read_bytes())
