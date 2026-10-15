"""In-memory tests of new orchestration; NOT a durable/loader certification."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_incremental_consumer as helpers
from paper.ledger import PaperLedger
from research.incremental_full_consumer import IncrementalConsumerLedger
from research.incremental_ledger import SyntheticLedger, ExternalMemory


class FullAdapterTests(unittest.TestCase):
    def prepare(self,closing=False):
        h=helpers.ConsumerIntegration();self.addCleanup(h.doCleanups)
        v,owner,stamp,candles=h.prepare(closing)
        a=object.__new__(IncrementalConsumerLedger)
        a.__dict__.update(v.__dict__);a.core=None
        a.authorization=SimpleNamespace(guard=lambda:None)
        initial=a._verify();a._consumer_batches=deepcopy(initial.batches)
        a.core=SyntheticLedger(a,ExternalMemory(),authorization_id='unit',authorize=lambda:'unit',
                              evidence=a._incremental_evidence,clock=lambda:100)
        a.core.bootstrap()
        return h,a,owner,stamp,candles

    def test_financial_equivalence_and_no_transition_replay(self):
        for closing in (False,True):
            with self.subTest(closing=closing):
                h,a,owner,stamp,candles=self.prepare(closing)
                v,_,_,original=h.prepare(closing)
                h.run_loop(v,stamp,original)
                with patch.object(PaperLedger,'_verify',side_effect=AssertionError('Replay')):
                    h.run_loop(a,stamp,candles)
                    a.process_candle(owner,stamp)
                self.assertEqual(h.rows(a),h.rows(v))
                self.assertEqual(a._verify().rows(),v._verify().rows())

    def test_retains_consumed_boundary_index(self):
        h,a,owner,stamp,candles=self.prepare()
        boundaries=set(a._verify().batches)
        self.assertGreater(len(boundaries),len(a.core._state(a.core.recover()).batches))
        h.run_loop(a,stamp,candles)
        self.assertEqual(set(a.reconcile().batches),boundaries)

    def test_gap_and_authorization_failure_do_not_write(self):
        h,a,owner,stamp,candles=self.prepare();before=h.rows(a)
        with self.assertRaisesRegex(ValueError,'exact next'):a.process_candle(owner,stamp+900000)
        self.assertEqual(h.rows(a),before)
        a.authorization=SimpleNamespace(guard=lambda:(_ for _ in ()).throw(ValueError('auth changed')))
        with self.assertRaisesRegex(ValueError,'auth changed'):a.process_candle(owner,stamp)
        self.assertEqual(h.rows(a),before)


if __name__=='__main__':unittest.main()
