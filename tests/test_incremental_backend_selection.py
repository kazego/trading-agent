"""Loader dispatch unit tests; no filesystem, operational DB or capability bypass in runtime."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import Mock, patch

from paper import authorization as a
from research.spec import encoded
from research.incremental_full_consumer import IncrementalConsumerLedger
from research.incremental_durable import Blocked


class SelectionTests(unittest.TestCase):
    def plan(self):
        return a._issue(a.ExecutionPlan,root=Path('C:/synthetic-unit'),run_id='synthetic',
                        payload=encoded({'run_id':'synthetic'}),ancestry=(b'a',b'b',b'c'),_used=False)

    def dispatch(self,**kwargs):
        plan=self.plan();source=object();directory=plan.root/'ledger'
        cap=SimpleNamespace(binding=object(),rules=object(),source=object(),guard=Mock())
        with ExitStack() as s:
            s.enter_context(patch.object(a,'_read_successor',return_value=(
                {'run_id':'synthetic'},{'checkpoint':'old'},{'checkpoint':'new'},b'a',b'b',b'c')))
            s.enter_context(patch.object(a,'paths',return_value=(plan.root,directory)))
            s.enter_context(patch.object(a,'_issue',return_value=cap))
            s.enter_context(patch.object(Path,'is_file',return_value=True))
            s.enter_context(patch('paper.source.SourceAdapter'))
            s.enter_context(patch('paper.market.CandleSource'))
            traditional=s.enter_context(patch('paper.ledger.PaperLedger'))
            incremental=s.enter_context(patch('research.incremental_full_consumer.IncrementalConsumerLedger'))
            ownership=s.enter_context(patch('research.incremental_full_consumer.validate_synthetic_owner'))
            result=plan.open(source,**kwargs)
            self.assertIs(result[0],cap);cap.guard.assert_called_once()
            selected=incremental if kwargs.get('backend')=='incremental' else traditional
            self.assertIs(result[1],selected.return_value)
            self.assertEqual(selected.call_args.kwargs['checkpoints'],('old','new'))
            self.assertIs(selected.call_args.kwargs['verified_source'],source)
            with self.assertRaisesRegex(ValueError,'consumed'):plan.open(source,**kwargs)
            return traditional,incremental,ownership,plan

    def test_default_and_explicit_traditional_unchanged(self):
        for config in ({},{'backend':'traditional'}):
            with self.subTest(config=config):
                old,new,owner,_=self.dispatch(**config)
                old.assert_called_once();new.assert_not_called();owner.assert_not_called()

    def test_incremental_explicit_existing_only(self):
        owner=object()
        old,new,check,plan=self.dispatch(backend='incremental',incremental_owner=owner)
        old.assert_not_called();new.assert_called_once()
        check.assert_called_once_with(owner,plan.root)
        self.assertIs(new.call_args.kwargs['initialize'],False)
        self.assertIs(new.call_args.kwargs['synthetic_owner'],owner)

    def test_bad_configuration_rejected_before_authorization_reads(self):
        for value in ('auto','',None,True,{},'INCREMENTAL',object()):
            with self.subTest(value=value),patch.object(a,'_read_successor') as read:
                with self.assertRaisesRegex(ValueError,'Unknown ledger backend'):
                    self.plan().open(None,backend=value)
                read.assert_not_called()
        with self.assertRaises(ValueError):self.plan().open(None,incremental_owner=object())

    def test_missing_or_forged_owner_blocks_before_any_ledger_read(self):
        for owner in (None,SimpleNamespace(active=True,run=Path('C:/synthetic-unit'))):
            with patch.object(a,'_read_successor') as read:
                with self.assertRaises(Blocked):self.plan().open(None,backend='incremental',incremental_owner=owner)
                read.assert_not_called()

    def test_changed_ancestry_still_blocks(self):
        with patch.object(a,'_read_successor',return_value=(
                {'run_id':'synthetic'},{},{},b'changed',b'b',b'c')):
            with self.assertRaisesRegex(ValueError,'Authorization changed'):self.plan().open(None)

    def test_missing_incremental_state_cannot_initialize(self):
        # Real empty SQLite, simulated path/ownership boundary only. Constructor
        # must refuse before the traditional writer constructor is called.
        root=Path('C:/synthetic-unit');directory=root/'ledger'
        cap=a._issue(a.ClockCapability,root=root,payload=encoded({'run_id':'synthetic'}))
        db=sqlite3.connect(':memory:')
        with patch('research.incremental_full_consumer.validate_synthetic_owner'), \
             patch('research.incremental_full_consumer.paths',return_value=(root,directory)), \
             patch('research.incremental_full_consumer.checked',side_effect=lambda p:p), \
             patch('research.incremental_full_consumer.sqlite3.connect',return_value=db) as connect, \
             patch('paper.ledger.PaperLedger.__init__') as writer:
            with self.assertRaisesRegex(Blocked,'explicit migration required'):
                IncrementalConsumerLedger(directory,synthetic_owner=object(),authorization=cap)
            writer.assert_not_called()
            self.assertIn('?mode=ro',connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])


if __name__=='__main__':unittest.main()
