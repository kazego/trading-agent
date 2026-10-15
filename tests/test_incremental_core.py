"""Synthetic memory-only validation; no production path or execution loader."""
import unittest
from copy import deepcopy

from research.incremental_ledger import SyntheticLedger, ExternalMemory, Blocked
from research.spec import encoded
from research.spec import fingerprint
from backtest.data import Candle
from paper.market import CandleEvidence
import sqlite3
from test_transactional_bar import fixture, previous_path


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.f,self.v,self.owner,self.stamp,self.candles=fixture(bars=2,backlog=8)
        self.addCleanup(self.f.doCleanups)
        self.external=ExternalMemory();self.now=100;self.auth='synthetic-approved'
        def evidence(kind,data):
            if kind=='BAR' and data['candle'] is not None:
                c=data['candle']['candle']
                if self.candles.get((c['symbol'],c['open_time_ms'])).record!=data['candle']:
                    raise Blocked('Source changed')
        self.evidence=evidence
        self.core=self.new_core();self.core.bootstrap()

    def new_core(self):
        return SyntheticLedger(self.v,self.external,authorization_id='synthetic-approved',
                               authorize=lambda:self.auth,evidence=self.evidence,clock=lambda:self.now)

    def data(self,i=0):
        return dict(owner=self.owner,candle=self.candles['BTCUSDT',self.stamp+i*900000].record)

    def rows(self,db):
        return {t:db.execute('SELECT * FROM '+t).fetchall() for t in
                ('journal','reservations','positions','fills','candles','processed','trades','equity','account','global_slot')}

    def test_three_bars_equal_full_replay_and_all_projections(self):
        f,v,owner,stamp,_=fixture(bars=2,backlog=8);self.addCleanup(f.doCleanups)
        for i in range(3):
            self.assertEqual(self.core.apply('BAR',self.data(i)),previous_path(v,owner,stamp+i*900000))
            self.assertEqual(self.rows(v._db),self.rows(self.v._db))
            self.core.audit()

    def test_duplicate_and_conflicting_retry(self):
        data=self.data();result=self.core.apply('BAR',data);rows=self.rows(self.v._db)
        self.assertEqual(self.core.apply('BAR',data),result)
        self.assertEqual(rows,self.rows(self.v._db))
        bad=deepcopy(data);bad['candle']['received_ms']+=1
        with self.assertRaises(ValueError):self.core.apply('BAR',bad)

    def test_gap_no_progress(self):
        rows=self.rows(self.v._db)
        with self.assertRaises(ValueError):self.core.apply('BAR',self.data(1))
        self.assertEqual(rows,self.rows(self.v._db))

    def test_transaction_faults_rollback(self):
        for stage in ('before_event','after_event','after_financial','after_state'):
            rows=self.rows(self.v._db);anchor=deepcopy(self.external.anchor)
            def fault(point):
                if point==stage:raise RuntimeError('synthetic interruption')
            with self.assertRaises(RuntimeError):self.core.apply('BAR',self.data(),fault=fault)
            self.assertEqual(rows,self.rows(self.v._db));self.assertEqual(anchor,self.external.anchor)
            self.core.audit()

    def test_commit_before_publish_recovered_after_restart(self):
        anchor=deepcopy(self.external.anchor)
        def fault(point):
            if point=='after_commit':raise RuntimeError('lost process')
        with self.assertRaises(RuntimeError):self.core.apply('BAR',self.data(),fault=fault)
        self.assertEqual(anchor,self.external.anchor)
        self.core=self.new_core();self.core.recover();rows=self.rows(self.v._db)
        self.core.apply('BAR',self.data());self.assertEqual(rows,self.rows(self.v._db));self.core.audit()

    def test_after_publish_restart(self):
        def fault(point):
            if point=='after_publish':raise RuntimeError('lost process')
        with self.assertRaises(RuntimeError):self.core.apply('BAR',self.data(),fault=fault)
        rows=self.rows(self.v._db);self.new_core().recover()
        self.assertEqual(rows,self.rows(self.v._db))

    def test_corrupt_projection_blocks_recovery(self):
        self.v._db.execute("UPDATE account SET amount='7'");self.v._db.commit()
        with self.assertRaises(Blocked):self.core.recover()
        self.assertTrue(self.external.blocked)

    def test_self_consistent_state_hash_cannot_replace_anchor(self):
        value=deepcopy(self.external.anchor);value['state']['equity']='7'
        self.v._db.execute('UPDATE incremental_state SET payload=?',(encoded(value).decode(),));self.v._db.commit()
        with self.assertRaises(Blocked):self.core.recover()

    def test_wrong_authorization(self):
        self.auth='changed'
        with self.assertRaises(Blocked):self.core.apply('BAR',self.data())

    def test_audit_age_and_count_gate(self):
        self.now+=86400
        with self.assertRaises(Blocked):self.core.apply('BAR',self.data())
        self.core.audit();self.core.apply('BAR',self.data())
        self.external.audit['sequence']-=1000
        with self.assertRaises(Blocked):self.core.apply('BAR',self.data(1))

    def test_old_corruption_detected_by_independent_audit(self):
        self.v._db.execute('DROP TRIGGER no_UPDATE_journal')
        self.v._db.execute("UPDATE journal SET payload='{}' WHERE sequence=1");self.v._db.commit()
        with self.assertRaises(ValueError):self.core.audit()
        self.assertTrue(self.external.blocked)
        with self.assertRaises(Blocked):self.core.apply('BAR',self.data())

    def test_returned_recovery_state_is_detached(self):
        state=self.core.recover();state['state']['equity']='9'
        self.assertEqual(self.core.recover()['state']['equity'],'1000')

    def test_restart_from_older_database_blocked(self):
        image=self.v._db.serialize();self.core.apply('BAR',self.data())
        self.v._db.deserialize(image)
        with self.assertRaises(Blocked):self.new_core().recover()

    def test_closed_trade_and_retry_match_full_replay(self):
        f,v,owner,stamp,cs=fixture(bars=2,backlog=8);self.addCleanup(f.doCleanups)
        p=v.reconcile().positions[owner];price=p.take_profit
        c=Candle('BTCUSDT','15m',stamp,stamp+899999,price,price,price,price,'1')
        e=CandleEvidence(f.binding.draft.source,c,fingerprint(c.record()),stamp+900000)
        cs['BTCUSDT',stamp]=e;self.candles['BTCUSDT',stamp]=e
        self.assertEqual(self.core.apply('BAR',self.data()),previous_path(v,owner,stamp))
        self.assertEqual(self.rows(v._db),self.rows(self.v._db));self.core.audit()
        rows=self.rows(self.v._db);self.core.apply('BAR',self.data())
        self.assertEqual(rows,self.rows(self.v._db))
        self.assertEqual(self.external.anchor['state']['slot'],['EMPTY',None,None])
        self.assertEqual(self.external.anchor['financial_count'],1)

    def test_restart_on_new_connection(self):
        self.core.apply('BAR',self.data())
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.deserialize(self.v._db.serialize());self.v._db=db
        self.core=self.new_core();self.core.recover();self.core.apply('BAR',self.data(1));self.core.audit()

    def test_corrupt_unpublished_commit_blocks(self):
        def fault(point):
            if point=='after_commit':raise RuntimeError('interruption')
        with self.assertRaises(RuntimeError):self.core.apply('BAR',self.data(),fault=fault)
        self.v._db.execute("UPDATE account SET amount='12'");self.v._db.commit()
        with self.assertRaises(Blocked):self.new_core().recover()

    def test_active_state_size_bounded_across_bars(self):
        initial=len(encoded(self.external.anchor))
        for i in range(6):self.core.apply('BAR',self.data(i))
        self.assertLessEqual(len(encoded(self.external.anchor)),initial+100)
        self.core.audit()

    def test_no_replay_during_normal_transition(self):
        from unittest.mock import patch
        with patch.object(self.v,'_verify',side_effect=AssertionError('full replay forbidden')):
            self.core.apply('BAR',self.data())
            self.core.recover()

    def test_policy_change_cannot_refresh_audit_silently(self):
        self.core.policy['max_events']=2000
        with self.assertRaises(Blocked):self.core.recover()

    def test_schema_trigger_change_blocks_before_transition(self):
        self.v._db.execute('DROP TRIGGER no_DELETE_journal');self.v._db.commit()
        with self.assertRaises(Blocked):self.core.apply('BAR',self.data())

    def test_next_accept_none_matches_shared_reducer(self):
        from test_consumer_budget import b2
        f,v,owner,stamp,cs=fixture(bars=2,backlog=8);self.addCleanup(f.doCleanups)
        state=v.reconcile();boundary=max(state.batches)+14400000
        data=dict(source=b2.item(boundary=boundary,signals=('NONE',)*3).record,
                  accepted_ms=boundary+240000,equity=state.basis)
        self.assertEqual(self.core.apply('ACCEPT',data),v._append('ACCEPT',data,'ACCEPT:'+str(boundary)))
        self.assertEqual(self.rows(v._db),self.rows(self.v._db));self.core.audit()

    def test_disk_connection_rejected_without_opening_disk(self):
        from unittest.mock import Mock
        # No real disk connection is made even by this negative test.
        verifier=Mock();verifier._db.execute.return_value.fetchall.return_value=[(0,'main','C:/forbidden.sqlite')]
        with self.assertRaises(Blocked):SyntheticLedger(verifier,ExternalMemory(),authorization_id='x',
                                                       authorize=lambda:'x',evidence=lambda *_:None)
        self.assertEqual(verifier._db.execute.call_count,1)

    def test_unknown_operation_rolls_back(self):
        rows=self.rows(self.v._db)
        with self.assertRaises((ValueError,KeyError)):self.core.apply('REVERSE',dict(owner=self.owner,candle=None))
        self.assertEqual(rows,self.rows(self.v._db))


if __name__=='__main__':unittest.main()
