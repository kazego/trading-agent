"""Bound-store integration using temporary synthetic ledgers only."""
import ast
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from paper import binding as b,authorization as a,clock_v3 as v3
from paper.operations import paths,exclusive,load
from paper.ledger import PaperLedger
from paper.source import SourceAdapter
from paper.market import CandleSource
from research.spec import encoded,fingerprint
from runtime import DatabaseLock
import paper_admin as admin
from artifact_fixture import fixture,bind_clock,T
import test_paper_acceptance as b2
from test_paper_clock_v3 import sample


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.f=fixture(Path(self.tmp.name));self.now=T+240000
        self.addCleanup(patch.stopall)
        patch.object(a,'now_ms',side_effect=lambda:self.now).start()
        patch.object(admin,'now_ms',side_effect=lambda:self.now).start()
        self.items={T:b2.item()}
        patch.object(SourceAdapter,'snapshot',side_effect=lambda s:tuple(self.items.values()),autospec=True).start()
        patch.object(CandleSource,'at',return_value=None).start()

    def bind(self):return bind_clock(self.f)
    def loader(self):return a.load_execution(self.f.run,self.f.root)
    def open(self,cap=None):
        cap=cap or self.loader();source=SourceAdapter(self.f.root,self.f.source);market=CandleSource(self.f.root,self.f.source)
        ledger=PaperLedger(self.f.directory,self.f.binding,self.f.rules,source,market,authorization=cap)
        self.addCleanup(ledger.close);return ledger
    def accept(self,ledger,first=None,last=None):
        with patch.object(v3,'sample',side_effect=[first or sample(T+240000),last or sample(T+240384,390000000)]):
            return ledger.accept_v3(T,authorization=ledger.authorization)
    def proposal(self):
        with patch.object(admin,'now_ms',return_value=T-600000):return admin.successor_proposal(self.f.run,self.f.root,T)
    def target(self):return b.locations(self.f.run,self.f.root,b.SUCCESSOR)[1]
    def alter(self,mutate):
        value,_=b.read(self.target());mutate(value['proposal']);value['reviewed_fingerprint']=fingerprint(value['proposal'])
        self.target().write_bytes(encoded(value)+b'\n')

    def test_unbound_matching_proposal_is_not_authority(self):
        p=self.proposal();admin.write_proposal(p,self.f.root)
        with self.assertRaises(FileNotFoundError):self.loader()
        with self.assertRaises(TypeError):a.ClockCapability(encoded(p),fingerprint(p),approval=True)
        with self.assertRaises(TypeError):a.AbandonCapability(payload=encoded(p),approved=True)

    def test_historical_clock_record_cannot_execute(self):
        self.bind();l=self.open();r=self.proposal()['clock'];old=v3.ClockAuthorization(encoded(r),fingerprint(r))
        with self.assertRaises(ValueError):l.accept_v3(T,authorization=old)
        self.assertEqual(len(l.reconcile().batches),0)

    def test_exact_binding_restart_readonly(self):
        p=self.bind();before=(self.f.directory/'paper.sqlite').read_bytes()
        x=self.loader();y=self.loader();self.assertEqual(x.bound_id,y.bound_id)
        self.assertEqual(x.proposal,p);self.assertEqual(before,(self.f.directory/'paper.sqlite').read_bytes())
        self.assertEqual(load(self.f.run,self.f.root).bound_id,x.bound_id)

    def test_wrong_ancestry_identity_policy_checkpoint(self):
        for field in ('activation','b6','implementations','clock','unchanged','checkpoint'):
            with self.subTest(field=field):
                p=self.proposal();p[field]={} if isinstance(p[field],dict) else '0'*64
                path=admin.write_proposal(p,self.f.root) if field not in ('clock',) else None
                if path is None:continue  # malformed clock is separately tested below
                with patch.object(admin,'now_ms',return_value=T-600000),self.assertRaises((ValueError,KeyError)):
                    admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
                self.assertFalse(self.target().exists())

    def test_wrong_clock_policy(self):
        p=self.proposal();p['clock']['policy']='0'*64
        with self.assertRaises(ValueError):b.validate(p)

    def test_premature_no_v2_fallback(self):
        self.bind();self.now=T-1
        with patch.object(PaperLedger,'accept') as old,self.assertRaises(ValueError):self.loader()
        old.assert_not_called()

    def test_duplicate_and_partial_no_repair(self):
        p=self.bind();original=self.target().read_bytes()
        with self.assertRaises(FileExistsError):admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
        self.target().write_bytes(b'{');before=self.target().read_bytes()
        with self.assertRaises(Exception):self.loader()
        with self.assertRaises(FileExistsError):admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
        self.assertEqual(before,self.target().read_bytes());self.assertNotEqual(original,before)

    def test_binding_mutation_invalidates_existing_capability(self):
        self.bind();cap=self.loader();self.alter(lambda p:p.update(effective_ms=T+14400000))
        with self.assertRaises(ValueError):cap.guard()

    def test_wrong_checkpoint_loader(self):
        self.bind();self.alter(lambda p:p['checkpoint'].update(journal_fingerprint='0'*64))
        with self.assertRaises(ValueError):self.loader()

    def test_cross_run_capability(self):
        self.bind();cap=self.loader();other=fixture(Path(self.tmp.name)/'other',run='live-0b-other');bind_clock(other)
        oc=a.load_execution(other.run,other.root)
        with self.assertRaises(ValueError):cap.check(oc.binding,T+240000,T)

    def test_consumer_and_ledger_do_not_import_binder(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('consumer','ledger','authorization','binding','clock_v3','recovery'):
            tree=ast.parse((root/'paper'/f'{name}.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):self.assertNotIn('paper_admin',[x.name for x in node.names])
                if isinstance(node,ast.ImportFrom):self.assertNotEqual(node.module,'paper_admin')
        self.assertNotIn('bind-clock-v3',(root/'paper/__main__.py').read_text())

    def test_binding_lock_contention(self):
        p=self.proposal();admin.write_proposal(p,self.f.root)
        for path in (self.f.control/'consumer',self.f.directory/'paper.sqlite'):
            lock=DatabaseLock(path);lock.acquire()
            try:
                with self.assertRaises(Exception):admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
                self.assertFalse(self.target().exists())
            finally:lock.release()

    def test_new_bound_acceptance_and_replay(self):
        self.bind();l=self.open();r=self.accept(l)
        self.assertEqual(r['reservation_state'],'PENDING_ENTRY')
        l.close();l2=self.open();self.assertEqual(l2.reconcile().result(T),r)
        with patch.object(v3,'sample',side_effect=AssertionError('No resampling')):
            self.assertEqual(l2.accept_v3(T,authorization=l2.authorization),r)

    def test_no_v2_for_new_successor_acceptance(self):
        self.bind();l=self.open()
        with self.assertRaises(ValueError):l.accept(T,clock=lambda:T+240000)

    def test_delayed_confirmation_really_crosses_deadline(self):
        self.bind();l=self.open();clock=[T+839999];calls=[]
        def sample_clock():
            calls.append(clock[0]);return sample(clock[0],len(calls)*1000000)
        def fault(point):
            if point=='ACCEPT.committed':clock[0]=T+840000
            if point=='CONFIRM_V3.before_commit':clock[0]=T+840100
        l.fault=fault
        with patch.object(v3,'sample',side_effect=sample_clock):r=l.accept_v3(T,authorization=l.authorization)
        self.assertEqual(clock[0],T+840100);self.assertEqual(calls,[T+839999,T+840000])
        self.assertEqual(r['reservation_state'],'PENDING_ENTRY')

    def test_historical_replay_without_binding_grants_no_capability(self):
        self.bind();l=self.open();r=self.accept(l);e=l.authorization.envelope
        self.target().unlink()  # temporary synthetic artifact only
        self.assertEqual(l.reconcile().result(T),r)
        self.assertEqual(b.historical(e)['kind'],b.SUCCESSOR)
        with self.assertRaises(FileNotFoundError):self.loader()
        with self.assertRaises(TypeError):a.ClockCapability(b.historical(e))


    def incident(self):
        self.bind();l=self.open();cap=l.authorization;l.authorization=None
        with patch('paper.ledger.time.monotonic_ns',side_effect=[0,390000000]):
            stamps=iter([T+240261,T+240645]);r=l.accept(T,clock=lambda:next(stamps))
        l.authorization=cap
        self.assertEqual(r['reservation_state'],'BLOCKED_CLOCK')
        return l,r['reservation']['reservation_id'],r

    def abandon_proposal(self,l,owner):
        l.close();self.now=T+1000000
        return admin.abandonment_proposal(self.f.run,self.f.root,owner,self.now)

    def bind_abandonment(self,p,owner):
        admin.write_proposal(p,self.f.root)
        return admin.bind(self.f.run,b.ABANDONMENT,fingerprint(p),root=self.f.root,owner=owner)

    def test_v2_blocked_clock_cannot_upgrade(self):
        l,owner,r=self.incident();before=l._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall()
        with patch.object(v3,'sample',side_effect=AssertionError('No resampling')):
            self.assertEqual(l.accept_v3(T,authorization=l.authorization),r)
        self.assertEqual(before,l._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall())
        self.assertEqual(l.reconcile().confirmations[owner]['status'],'BLOCKED_CLOCK')

    def test_unbound_abandonment_cannot_authorize(self):
        l,owner,_=self.incident();p=self.abandon_proposal(l,owner);admin.write_proposal(p,self.f.root)
        with self.assertRaises(FileNotFoundError):a.load_abandonment(self.f.run,owner,self.f.root)
        from paper.recovery import RecoveryAuthorization
        r=p['authorization'];old=RecoveryAuthorization(encoded(r),fingerprint(r))
        l=self.open()
        with self.assertRaises(ValueError):l.abandon_blocked_clock(old)
        self.assertEqual(l.reconcile().slot[0],'BLOCKED')

    def test_wrong_abandonment_owner_evidence_checkpoint(self):
        l,owner,_=self.incident();p=self.abandon_proposal(l,owner)
        for field in ('owner','journal_head','account','slot','confirmation','accept_event','confirm_event'):
            wrong=copy.deepcopy(p);cp=wrong['authorization']['checkpoint'];cp[field]=[] if isinstance(cp[field],list) else '0'*64
            admin.write_proposal(wrong,self.f.root)
            with self.subTest(field=field),self.assertRaises((ValueError,KeyError)):
                admin.bind(self.f.run,b.ABANDONMENT,fingerprint(wrong),root=self.f.root,owner=owner)
        with self.assertRaises(FileNotFoundError):a.load_abandonment(self.f.run,'0'*64,self.f.root)

    def test_abandonment_resolve_restart_and_retry_after_journal_growth(self):
        l,owner,original=self.incident();prefix=l._db.execute('SELECT * FROM journal ORDER BY sequence').fetchall()
        p=self.abandon_proposal(l,owner);self.bind_abandonment(p,owner)
        first=admin.resolve(self.f.run,owner,root=self.f.root)
        l=self.open();state=l.reconcile()
        self.assertEqual(state.slot,('EMPTY',None,None));self.assertEqual(state.equity,1000)
        self.assertEqual(state.confirmations[owner],original['confirmation'])
        self.assertEqual(prefix,l._db.execute('SELECT * FROM journal WHERE sequence<=2 ORDER BY sequence').fetchall())
        for table in ('fills','positions','trades','equity'):self.assertEqual(l._db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        later=T+14400000;self.items[later]=b2.item(boundary=later)
        self.now=later+900001
        with patch.object(v3,'sample',return_value=sample(self.now)):
            l.accept_v3(later,authorization=l.authorization)
        l.close()
        self.assertEqual(admin.resolve(self.f.run,owner,root=self.f.root),first)
        state,_=a.state_at(self.f.run,self.f.root,self.f.v)
        self.assertEqual(len(state.resolutions),1);self.assertEqual(len(state.accounting),0)
        # Remove current authority only in this synthetic fixture: replay still works.
        b.locations(self.f.run,self.f.root,b.ABANDONMENT,owner=owner)[1].unlink();self.target().unlink()
        state,_=a.state_at(self.f.run,self.f.root,self.f.v)
        self.assertEqual(state.result(T)['reservation_state'],'ABANDONED')
        self.assertEqual(state.confirmations[owner]['status'],'BLOCKED_CLOCK')
        with self.assertRaises(FileNotFoundError):admin.resolve(self.f.run,owner,root=self.f.root)

    def test_resolution_binding_changed_before_execution(self):
        l,owner,_=self.incident();p=self.abandon_proposal(l,owner);self.bind_abandonment(p,owner)
        cap=a.load_abandonment(self.f.run,owner,self.f.root)
        path=b.locations(self.f.run,self.f.root,b.ABANDONMENT,owner=owner)[1]
        value,_=b.read(path);value['proposal']['authorization']['resolved_ms']+=1
        value['reviewed_fingerprint']=fingerprint(value['proposal']);path.write_bytes(encoded(value)+b'\n')
        with self.assertRaises(ValueError):cap.guard()

    def test_consumer_uses_capability_v3_and_one_snapshot(self):
        from paper.consumer import consume
        from paper.verified_source import VerifiedSourceSnapshot,_SEAL
        from types import MappingProxyType
        self.bind()
        source_path=self.f.root/'artifacts/forward-paper/live-0a-001'
        progress={s:dict(first_open_ms=None,last_open_ms=None,count=0) for s in ('BTCUSDT','ETHUSDT','ADAUSDT')}
        view=VerifiedSourceSnapshot(self.f.source,tuple(self.items.values()),MappingProxyType({}),encoded(progress),b'{}',source_path,(),self.f.source.implementation,_SEAL)
        with patch('paper.consumer.capture',return_value=view) as captures,patch.object(VerifiedSourceSnapshot,'guard'),patch.object(v3,'sample',side_effect=[sample(T+240000),sample(T+240001,1000000)]),patch.object(PaperLedger,'accept',side_effect=AssertionError('No v2 fallback')):
            result=consume(self.f.run,root=self.f.root,clock=lambda:self.now)
        self.assertEqual(captures.call_count,1);self.assertEqual(result['state'],'HEALTHY')
        l=self.open();self.assertEqual(l.reconcile().confirmations[next(iter(l.reconcile().reservations))]['version'],'live-0b-acceptance-confirmation-v3')

    def test_missing_binding_consumer_never_uses_v2(self):
        from paper.consumer import consume
        with patch.object(PaperLedger,'accept') as legacy,patch('paper.consumer.capture') as source:
            with self.assertRaises(FileNotFoundError):consume(self.f.run,root=self.f.root,clock=lambda:self.now)
        legacy.assert_not_called();source.assert_not_called()


    def test_changed_binding_during_consume_fails_before_acceptance(self):
        from paper.consumer import consume
        self.bind();before=(self.f.directory/'paper.sqlite').read_bytes()
        def changed(*args,**kwargs):
            self.alter(lambda p:p['implementations'].update(b4='0'*64))
            from paper.verified_source import VerifiedSourceSnapshot,_SEAL
            from types import MappingProxyType
            return VerifiedSourceSnapshot(self.f.source,tuple(self.items.values()),MappingProxyType({}),b'{}',b'{}',args[0],(),self.f.source.implementation,_SEAL)
        from paper.verified_source import VerifiedSourceSnapshot
        with patch('paper.consumer.capture',side_effect=changed),patch.object(VerifiedSourceSnapshot,'guard'),patch.object(PaperLedger,'accept_v3') as accept:
            with self.assertRaises(ValueError):consume(self.f.run,root=self.f.root,clock=lambda:self.now)
        accept.assert_not_called();self.assertEqual(before,(self.f.directory/'paper.sqlite').read_bytes())

    def test_checkpoint_drift_between_review_and_bind(self):
        p=self.proposal();admin.write_proposal(p,self.f.root)
        # Hash-valid but wrong reviewed account cannot be accepted by bind.
        wrong=copy.deepcopy(p);wrong['checkpoint']['account'][0][1]='999'
        admin.write_proposal(wrong,self.f.root)
        with patch.object(admin,'now_ms',return_value=T-600000),self.assertRaises(ValueError):
            admin.bind(self.f.run,b.SUCCESSOR,fingerprint(wrong),root=self.f.root)
        self.assertFalse(self.target().exists())


    def test_bind_time_margin_expiry_rejects_before_creation(self):
        p=self.proposal();admin.write_proposal(p,self.f.root)
        original=admin.successor_proposal
        for final in (T-299999,T,T+1,T-600001):
            with self.subTest(final=final):
                self.now=T-600000
                def advance(*args):
                    result=original(*args)
                    self.now=final
                    return result
                with patch.object(admin,'successor_proposal',side_effect=advance),self.assertRaises(ValueError):
                    admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
                self.assertFalse(self.target().exists())
                self.assertFalse(self.target().parent.exists())

    def test_bind_time_exact_margin_is_inclusive(self):
        p=self.proposal();admin.write_proposal(p,self.f.root)
        original=admin.successor_proposal;self.now=T-600000
        def advance(*args):
            result=original(*args);self.now=T-300000;return result
        with patch.object(admin,'successor_proposal',side_effect=advance):
            admin.bind(self.f.run,b.SUCCESSOR,fingerprint(p),root=self.f.root)
        self.assertEqual(b.read(self.target())[0]['bound_ms'],T-300000)


    def test_same_run_capability_cannot_cross_deployment_roots(self):
        self.bind();cap=self.loader()
        other=fixture(Path(self.tmp.name)/'other',run=self.f.run)
        before=(other.directory/'paper.sqlite').read_bytes()
        source=SourceAdapter(self.f.root,self.f.source);market=CandleSource(self.f.root,self.f.source)
        with self.assertRaisesRegex(ValueError,'another ledger directory'):
            PaperLedger(other.directory,self.f.binding,self.f.rules,source,market,authorization=cap)
        self.assertEqual(before,(other.directory/'paper.sqlite').read_bytes())
