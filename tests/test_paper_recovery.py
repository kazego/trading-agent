"""Atomic synthetic ledger recovery; never touches production evidence."""
import copy
import unittest
from unittest.mock import patch
import test_paper_ledger as b4
import artifact_fixture as af
from paper import clock_v3 as v3, recovery
from paper.model import LABEL
from research.spec import encoded,fingerprint

T=b4.T

def clock_auth(binding):
    r=dict(version='paper-clock-v3-authorization-v1',label=LABEL,binding=binding.id,
        effective_ms=T,policy=fingerprint(v3.policy()),implementation=v3.implementation())
    return v3.ClockAuthorization(encoded(r),fingerprint(r))

def make_auth(ledger,owner):
    return af.recovery_cap(ledger,owner)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=af.BoundLedgerHarness();self.f.setUp();self.addCleanup(self.f.doCleanups)
        with patch('paper.ledger.time.monotonic_ns',side_effect=[0,390000000]):
            stamps=iter([T+240261,T+240645]);cap=self.f.ledger.authorization;self.f.ledger.authorization=None
            try:result=self.f.ledger.accept(T,clock=lambda:next(stamps))
            finally:self.f.ledger.authorization=cap
        self.owner=result['reservation']['reservation_id']
        self.assertEqual(result['reservation_state'],'BLOCKED_CLOCK')
        self.auth=make_auth(self.f.ledger,self.owner)
        self.original=self.f.ledger._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall()

    def reload_auth(self):
        from paper.authorization import load_abandonment
        cap=self.f.ledger.authorization
        value=load_abandonment(cap.record['run_id'],self.owner,cap.root)
        self.f.ledger.authorization=value.successor
        return value

    def test_abandonment_atomic_idempotent_restart_no_financial_effect(self):
        value=self.f.ledger.abandon_blocked_clock(self.auth)
        self.assertEqual(value['state'],'ABANDONED')
        state=self.f.ledger.reconcile()
        self.assertEqual(state.slot,('EMPTY',None,None));self.assertEqual(state.equity,1000)
        self.assertEqual(state.confirmations[self.owner]['status'],'BLOCKED_CLOCK')
        self.assertEqual(state.result(T)['reservation_state'],'ABANDONED')
        self.assertEqual(state.result(T)['dispositions'][1]['reason'],'BLOCKED_PRIORITY')
        self.assertEqual(self.f.ledger._db.execute('SELECT * FROM journal WHERE sequence<=2 ORDER BY sequence').fetchall(),self.original)
        for table in ('positions','fills','trades','equity'):self.assertEqual(self.f.count(table),0)
        self.assertEqual(self.f.count('journal'),3);self.assertEqual(self.f.count('resolutions'),1)
        self.f.reopen();self.assertEqual(self.f.ledger.abandon_blocked_clock(self.reload_auth()),value)
        self.assertEqual(self.f.count('journal'),3)
        self.assertEqual(self.f.ledger.accept(T,clock=lambda:self.fail('No retrospective acceptance'))['reservation_state'],'ABANDONED')
        self.assertEqual(self.f.ledger._db.execute('SELECT state FROM reservations').fetchone()[0],'ABANDONED')

    def test_wrong_authorization_hash(self):
        with self.assertRaises(ValueError):recovery.RecoveryAuthorization(self.auth.payload,'0'*64)

    def test_wrong_reservation_or_checkpoint(self):
        for field in ('owner','journal_head','genesis','reservation','confirmation','accept_event','confirm_event','binding','rules'):
            with self.subTest(field=field):
                state=self.f.ledger.reconcile();r=copy.deepcopy(self.auth.record);r['checkpoint'][field]='0'*64
                with self.assertRaises(ValueError):recovery.apply(state,self.owner,r,fingerprint(r))

    def test_wrong_slot_owner_and_financial_children(self):
        for kind in ('slot','position','trade','accounting','processed'):
            state=self.f.ledger.reconcile()
            if kind=='slot':state.slot=('BLOCKED','0'*64,'BTCUSDT')
            elif kind=='position':state.positions[self.owner]=object()
            elif kind=='trade':state.trades[self.owner]=object()
            elif kind=='accounting':state.accounting.append(dict(owner=self.owner))
            else:state.processed[self.owner,T]='x'
            with self.subTest(kind=kind),self.assertRaises(ValueError):recovery.apply(state,self.owner,self.auth.record,self.auth.approved_fingerprint)

    def test_existing_fill_contradiction_fail_closed(self):
        self.f.ledger._db.execute('INSERT INTO fills VALUES(?,?,?,?)',(self.owner,'ENTRY','0'*64,'{}'));self.f.ledger._db.commit()
        with self.assertRaises(ValueError):self.f.ledger.abandon_blocked_clock(self.auth)

    def test_conflicting_retry_fail_closed(self):
        self.f.ledger.abandon_blocked_clock(self.auth)
        r=self.auth.record;r['resolved_ms']+=1
        bad=recovery.RecoveryAuthorization(encoded(r),fingerprint(r))
        with self.assertRaises(ValueError):self.f.ledger.abandon_blocked_clock(bad)
        self.assertEqual(self.f.count('journal'),3)

    def test_earlier_resolution_fail_closed(self):
        r=self.auth.record;r['resolved_ms']=T
        with self.assertRaises(ValueError):recovery.apply(self.f.ledger.reconcile(),self.owner,r,fingerprint(r))

    def test_append_only_resolution(self):
        self.f.ledger.abandon_blocked_clock(self.auth)
        for sql in ('DELETE FROM resolutions','UPDATE resolutions SET digest="x"'):
            with self.assertRaises(Exception):self.f.ledger._db.execute(sql)
            self.f.ledger._db.rollback()

    def test_crash_at_every_stage(self):
        for point in ('ABANDON_BLOCKED_CLOCK.before','ABANDON_BLOCKED_CLOCK.journal','ABANDON_BLOCKED_CLOCK.schema',
                      'MATERIALIZE.resolutions','MATERIALIZE.reservations','MATERIALIZE.global_slot',
                      'MATERIALIZE.account','ABANDON_BLOCKED_CLOCK.state','ABANDON_BLOCKED_CLOCK.before_commit'):
            with self.subTest(point=point):
                self.f.ledger.fault=lambda p: (_ for _ in ()).throw(b4.Crash()) if p==point else None
                with self.assertRaises(b4.Crash):self.f.ledger.abandon_blocked_clock(self.auth)
                self.f.reopen();self.auth=self.reload_auth();self.assertEqual(self.f.ledger.reconcile().slot[0],'BLOCKED')
                self.assertEqual(self.f.count('journal'),2)
                self.assertEqual(self.f.ledger._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall(),self.original)

    def test_crash_after_commit_restart(self):
        self.f.ledger.fault=lambda p: (_ for _ in ()).throw(b4.Crash()) if p=='ABANDON_BLOCKED_CLOCK.committed' else None
        with self.assertRaises(b4.Crash):self.f.ledger.abandon_blocked_clock(self.auth)
        self.f.reopen();self.assertEqual(self.f.ledger.reconcile().slot[0],'EMPTY')
        self.f.ledger.abandon_blocked_clock(self.reload_auth());self.assertEqual(self.f.count('journal'),3)

class V3LedgerTests(unittest.TestCase):
    def setUp(self):
        self.f=af.BoundLedgerHarness();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.auth=self.f.ledger.authorization

    def accept(self,a=None,c=None):
        from test_paper_clock_v3 import sample
        a=a or sample(T+240000);c=c or sample(T+240384,390000000)
        with patch.object(v3,'sample',side_effect=[a,c]):return self.f.ledger.accept_v3(T,authorization=self.f.ledger.authorization)

    def test_v3_entry_and_financial_path_reuses_b3(self):
        result=self.accept();self.assertEqual(result['reservation_state'],'PENDING_ENTRY')
        owner=result['reservation']['reservation_id'];self.f.bar();self.assertEqual(self.f.ledger.settle_entry(owner)['status'],'OPEN')
        self.f.reopen();self.assertEqual(self.f.ledger.reconcile().slot[0],'OPEN')

    def test_confirm_persistence_later_does_not_resample(self):
        result=self.accept();self.f.reopen()
        with patch.object(v3,'sample',side_effect=AssertionError('Must not resample')):
            self.assertEqual(self.f.ledger.accept_v3(T,authorization=self.f.ledger.authorization),result)

    def test_timely_sample_survives_confirmation_persistence_after_deadline(self):
        from test_paper_clock_v3 import sample
        later = []
        def fault(point):
            if point == 'CONFIRM_V3.before_commit':
                later.append(T+840100)
        self.f.ledger.fault = fault
        with patch.object(v3.time,'time_ns',side_effect=AssertionError('No resampling during persistence')):
            r=self.accept(sample(T+839999),sample(T+840000,1000000))
        self.assertEqual(later,[T+840100])
        self.assertEqual(r['reservation_state'],'PENDING_ENTRY')
        self.f.reopen()
        self.assertEqual(self.f.ledger.reconcile().result(T),r)

    def test_post_commit_sample_after_deadline_is_not_executable(self):
        from test_paper_clock_v3 import sample
        r=self.accept(sample(T+839999),sample(T+840001,2000000))
        self.assertEqual(r['reservation_state'],'MISSED_ACCEPTANCE_DEADLINE')
        self.assertEqual(self.f.ledger.reconcile().slot[0],'BLOCKED')

    def test_unsupported_profile_before_accept_has_no_journal_effect(self):
        with patch.object(v3,'verify_runtime',side_effect=ValueError('Unsupported profile')):
            with self.assertRaises(ValueError):self.f.ledger.accept_v3(T,authorization=self.f.ledger.authorization)
        self.assertEqual(self.f.count('journal'),0)
        self.assertEqual(self.f.count('reservations'),0)

    def test_authorization_binding_effective_and_tampering(self):
        for field,value in (('binding','0'*64),('effective_ms',T+500000)):
            r=self.auth.proposal['clock'];r[field]=value
            a=v3.ClockAuthorization(encoded(r),fingerprint(r))
            with self.assertRaises(ValueError):a.check(self.f.binding,T+240000)
        with self.assertRaises(ValueError):v3.ClockAuthorization(encoded(self.auth.proposal['clock']),'0'*64)

    def test_crash_between_accept_and_confirm_does_not_upgrade(self):
        self.f.ledger.fault=lambda p: (_ for _ in ()).throw(b4.Crash()) if p=='ACCEPT.committed' else None
        with self.assertRaises(b4.Crash):self.accept()
        self.f.reopen()
        with patch.object(v3,'sample',side_effect=AssertionError('No later confirmation')):
            r=self.f.ledger.accept_v3(T,authorization=self.f.ledger.authorization)
        self.assertEqual(r['reservation_state'],'UNCONFIRMED_ACCEPTANCE')

    def test_late_acceptance_no_reservation(self):
        from test_paper_clock_v3 import sample
        r=self.accept(sample(T+840001),sample(T+840002,1000000))
        self.assertIsNone(r['reservation']);self.assertEqual(self.f.count('reservations'),0)


class HistoricalSchemaTests(unittest.TestCase):
    def fixture(self):
        import paper.ledger as module
        f=af.BoundLedgerHarness()
        # Build an actual v1 CHECK constraint using the unchanged historical SQL.
        with patch.object(module,'SCHEMA',module.SCHEMA.replace(",'ABANDONED'",'')):
            f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def block(self,f,boundary):
        with patch('paper.ledger.time.monotonic_ns',side_effect=[0,390000000]):
            stamps=iter([boundary+240261,boundary+240645])
            cap=f.ledger.authorization;f.ledger.authorization=None
            try:return f.ledger.accept(boundary,clock=lambda:next(stamps))['reservation']['reservation_id']
            finally:f.ledger.authorization=cap

    def test_old_schema_migration_and_following_boundary(self):
        f=self.fixture();owner=self.block(f,T);auth=make_auth(f.ledger,owner)
        original=f.ledger._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall()
        f.ledger.abandon_blocked_clock(auth);f.reopen()
        self.assertEqual(f.ledger._db.execute('PRAGMA foreign_keys').fetchone(),(1,))
        self.assertEqual(f.ledger._db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(f.ledger._db.execute('SELECT * FROM journal WHERE sequence<=2 ORDER BY sequence').fetchall(),original)
        f.items[T+14400000]=b4.b2.item(boundary=T+14400000)
        r=f.accept(T+14400000)
        self.assertEqual(r['reservation_state'],'PENDING_ENTRY')
        self.assertNotEqual(r['reservation']['reservation_id'],owner)
        self.assertEqual(f.ledger.reconcile().result(T)['reservation_state'],'ABANDONED')

    def test_schema_rebuild_crash_rolls_back_schema_and_history(self):
        f=self.fixture();owner=self.block(f,T);auth=make_auth(f.ledger,owner)
        schema=f.ledger._db.execute("SELECT sql FROM sqlite_master WHERE name='reservations'").fetchone()
        f.ledger.fault=lambda p: (_ for _ in ()).throw(b4.Crash()) if p=='ABANDON_BLOCKED_CLOCK.reservation_schema' else None
        with self.assertRaises(b4.Crash):f.ledger.abandon_blocked_clock(auth)
        f.reopen()
        self.assertEqual(f.ledger._db.execute("SELECT sql FROM sqlite_master WHERE name='reservations'").fetchone(),schema)
        self.assertEqual(f.ledger.reconcile().slot[0],'BLOCKED')

    def test_closed_history_survives_schema_extension(self):
        f=self.fixture();owner=f.owner();f.bar(high='104',low='90')
        f.ledger.settle_entry(owner);f.ledger.process_candle(owner,b4.E)
        prior=f.ledger.reconcile();self.assertEqual(len(prior.trades),1)
        immutable={t:f.ledger._db.execute('SELECT * FROM '+t+' ORDER BY 1').fetchall()
                   for t in ('fills','trades','equity','positions','identity')}
        boundary=T+14400000;f.items[boundary]=b4.b2.item(boundary=boundary)
        blocked=self.block(f,boundary);a=make_auth(f.ledger,blocked)
        f.ledger.abandon_blocked_clock(a);f.reopen()
        for table,rows in immutable.items():self.assertEqual(f.ledger._db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall(),rows)
        self.assertEqual(f.ledger.reconcile().equity,prior.equity)
        self.assertEqual(f.ledger._db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_baseline_source_identities_reproduce(self):
        from clock_historical import historical_identities
        self.assertEqual(historical_identities(),dict(
            b3='ae8493a638ee9017df9641a12bbe910fd1d0cde0572ff907df99e1c2cb056d94',
            b4='0c3543daf8499baaaf969a76f077f7dd6a34accdbe9cf31b17c3b4fb6e4cde78',
            b5='c27c64c7ff65b70e7493009f902fa1d62e02f4183af4e292e1b817804c48efc5'))
