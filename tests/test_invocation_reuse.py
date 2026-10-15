"""Invocation reuse safety on synthetic SQLite only; no operational paths.

Memory fixtures substitute filesystem latches and source acquisition, not ledger
verification. They do not certify production binding or physical durability.
"""
import sqlite3
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_transactional_bar import fixture, previous_path
from paper.authorization import ExecutionPlan


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.f,self.v,self.owner,self.stamp,self.candles=fixture(backlog=4)
        self.addCleanup(self.f.doCleanups)
        self.v._enable_invocation_reuse()

    def rows(self,v=None):
        v=v or self.v
        return {t:v._db.execute('select * from '+t).fetchall() for t in
                ('journal','positions','fills','account','global_slot','processed','trades','equity')}

    def test_one_initial_plus_one_post_per_transition(self):
        self.v.reconcile();self.v.reconcile()
        self.assertEqual(self.v._verification_stats['full_passes'],1)
        for i in range(3):
            self.v.process_candle(self.owner,self.stamp+i*900000)
            self.v.reconcile()
        self.assertEqual(self.v._verification_stats['full_passes'],4)
        self.assertEqual(self.v.reconcile().equity,1000)

    def test_returned_state_is_not_cache(self):
        state=self.v.reconcile();state.equity=7;state.positions.clear();state.event_ids.clear()
        again=self.v.reconcile()
        self.assertEqual(again.equity,1000);self.assertIn(self.owner,again.positions)
        self.assertTrue(again.event_ids)

    def test_financial_and_journal_equivalence(self):
        f,v,owner,stamp,_=fixture(backlog=4);self.addCleanup(f.doCleanups)
        self.v.reconcile()
        for i in range(3):
            self.assertEqual(previous_path(v,owner,stamp+i*900000),self.v.process_candle(self.owner,self.stamp+i*900000))
            self.assertEqual(self.rows(v),self.rows())

    def test_own_write_invalidates_and_corruption_fails(self):
        self.v.reconcile()
        self.v._db.execute("update account set amount='7'");self.v._db.commit()
        with self.assertRaises(ValueError):self.v.reconcile()

    def test_unexpected_rollback_and_commit_force_recheck(self):
        for ending in ('rollback','commit'):
            self.v.reconcile();count=self.v._verification_stats['full_passes']
            self.v._db.execute('BEGIN IMMEDIATE');getattr(self.v._db,ending)()
            self.v.reconcile()
            self.assertEqual(self.v._verification_stats['full_passes'],count+1)

    def test_external_write_detected_even_without_journal_change(self):
        # Separate connection to a named, entirely in-memory database.
        from uuid import uuid4
        uri='file:reuse-'+uuid4().hex+'?mode=memory&cache=shared'
        first=sqlite3.connect(uri,uri=True);second=sqlite3.connect(uri,uri=True)
        self.addCleanup(first.close);self.addCleanup(second.close)
        self.v._db.backup(first);self.v._db=first;self.v._enable_invocation_reuse()
        self.v.reconcile()
        second.execute("update account set amount='7'");second.commit()
        with self.assertRaises(ValueError):self.v.reconcile()

    def test_reader_transaction_excludes_external_writer(self):
        from uuid import uuid4
        uri='file:reuse-'+uuid4().hex+'?mode=memory&cache=shared'
        first=sqlite3.connect(uri,uri=True);second=sqlite3.connect(uri,uri=True)
        self.addCleanup(first.close);self.addCleanup(second.close)
        self.v._db.backup(first);self.v._db=first;self.v._enable_invocation_reuse()
        self.v.reconcile();first.execute('BEGIN IMMEDIATE');self.v._verified_state()
        with self.assertRaises(sqlite3.OperationalError):second.execute("update account set amount='7'")
        first.rollback()

    def test_exception_rollback_retry_no_duplicate(self):
        self.v.reconcile();before=self.rows()
        def fault(point):
            if point=='BAR.state':raise RuntimeError('crash')
        self.v.fault=fault
        with self.assertRaises(RuntimeError):self.v.process_candle(self.owner,self.stamp)
        self.assertEqual(before,self.rows());self.v.fault=None
        self.v.process_candle(self.owner,self.stamp);after=self.rows()
        self.v.process_candle(self.owner,self.stamp)
        self.assertEqual(after,self.rows())

    def test_changed_checkpoint_is_not_cached(self):
        self.v.reconcile()
        self.v._required_checkpoints=({'journal_count':0},)
        with self.assertRaises(ValueError):self.v.reconcile()

    def test_changed_authorization_or_source_fails(self):
        self.v.reconcile()
        for field in ('authorization','verified_source'):
            def bad():raise ValueError('changed evidence')
            setattr(self.v,field,SimpleNamespace(guard=bad))
            with self.assertRaises(ValueError):self.v.reconcile()
            setattr(self.v,field,None)

    def test_wrong_genesis_fails(self):
        self.v.reconcile();self.v.identity_id='0'*64
        with self.assertRaises(ValueError):self.v.reconcile()

    def test_new_connection_and_restart_cannot_reuse(self):
        self.v.reconcile();self.v.process_candle(self.owner,self.stamp)
        new=sqlite3.connect(':memory:');self.addCleanup(new.close)
        self.v._db.backup(new);self.v._db=new
        self.v._enable_invocation_reuse();self.v.reconcile()
        self.assertEqual(self.v._verification_stats['full_passes'],1)
        self.v.process_candle(self.owner,self.stamp+900000)
        self.assertEqual(self.v._verification_stats['full_passes'],2)

    def test_plan_metadata_cannot_construct_execution_authority(self):
        with self.assertRaises(TypeError):ExecutionPlan(approval=True)
        self.assertFalse(hasattr(ExecutionPlan,'accept'))
        self.assertFalse(hasattr(ExecutionPlan,'check'))


if __name__=='__main__':unittest.main()
