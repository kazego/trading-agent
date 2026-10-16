"""New executable binding. Disk tests use only newly allocated synthetic roots."""
from pathlib import Path
import sys
import unittest
from copy import deepcopy
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from paper import authorization as a, binding as b
from paper.operations import propose,paths,consumer_exclusion
from paper_admin import bind,write_proposal
from research.new_cycle_authorization import expected,read_execution,proposal
from research.new_paper_cycle import initialize
from research.incremental_durable import AUTHORIZED,Ownership,new_run,Blocked
from research.spec import encoded,fingerprint
import test_paper_foundation as b1
import test_paper_settlement as b3


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.root=AUTHORIZED/'incremental-unit-only'
        self.auth=propose('live-0b-new-auth-test',b1.T+14400000,b1.identity(),b3.rules(),now_ms=b1.T)
        self.p=expected(self.auth,self.root,{'journal_count':0},'a'*64)
        self.e=b.bound(self.p,b1.T+1)

    def read(self,e=None):
        with patch('research.new_cycle_authorization.activation',return_value=(self.auth,b'a')), \
             patch('research.incremental_durable.root_path'),patch('research.incremental_durable.checked'), \
             patch.object(b,'read',return_value=(e or self.e,encoded(e or self.e)+b'\n')), \
             patch.object(a,'now_ms',return_value=self.auth.record['t0_ms']):
            return read_execution(self.auth.record['run_id'],self.root)

    def test_new_schema_no_historical_ancestry(self):
        self.assertEqual(b.validate(self.p),self.p)
        self.assertNotIn('b6',self.p)
        self.assertEqual(self.read()[2],self.p)
        with self.assertRaises(TypeError):a.ClockCapability(self.e)

    def test_changed_identity_path_cycle_policy_and_genesis_prefix_rejected(self):
        for field,value in [('run_id','live-0b-other'),('activation','b'*64),('modules','b'*64),
                ('ledger','C:/other.sqlite'),('backend','traditional'),('checkpoint',{'journal_count':1}),
                ('implementations',{}),('unchanged',{})]:
            with self.subTest(field=field):
                p=deepcopy(self.p);p[field]=value
                with self.assertRaises(ValueError):self.read(b.bound(p,b1.T+1))

    def test_missing_binding_never_uses_proposal(self):
        with patch('research.incremental_durable.root_path'),patch('research.incremental_durable.checked'), \
             patch('research.new_cycle_authorization.activation',return_value=(self.auth,b'a')), \
             patch.object(b,'read',side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):read_execution(self.auth.record['run_id'],self.root)

    def test_premature_effectivity_and_late_binding(self):
        with self.assertRaises(ValueError):b.bound(self.p,self.auth.record['t0_ms'])
        with patch('research.incremental_durable.root_path'),patch('research.incremental_durable.checked'), \
             patch('research.new_cycle_authorization.activation',return_value=(self.auth,b'a')), \
             patch.object(b,'read',return_value=(self.e,encoded(self.e)+b'\n')), \
             patch.object(a,'now_ms',return_value=b1.T):
            with self.assertRaisesRegex(ValueError,'not effective'):read_execution(self.auth.record['run_id'],self.root)

    def test_no_owner_no_binding(self):
        with self.assertRaises(Blocked):bind(self.auth.record['run_id'],b.NEW_CYCLE,fingerprint(self.p),root=self.root)

    def test_pre_t0_and_cross_binding_rejected(self):
        cap=a._issue(a.ClockCapability,payload=self.auth.payload,root=self.root,
            envelope_bytes=encoded(self.e)+b'\n',activation_bytes=b'a',b6_bytes=b'')
        with patch.object(a.ClockCapability,'guard'):
            for boundary in (b1.T,self.auth.record['t0_ms']):
                with self.assertRaisesRegex(ValueError,'T0'):cap.check(cap.binding,self.auth.record['t0_ms']+1,boundary)
                with self.assertRaisesRegex(ValueError,'T0'):cap.accept(None,boundary)
            with self.assertRaises(ValueError):cap.check(object(),b1.T+30000000,b1.T+28800000)
            cap.check(cap.binding,b1.T+28800000,b1.T+28800000)

    def test_partial_or_forged_envelope(self):
        for e in ({},dict(self.e,reviewed_fingerprint='0'*64),dict(self.e,approval=True)):
            with self.assertRaises((ValueError,KeyError)):b.historical(e)


    def test_capability_rejects_binding_byte_change(self):
        cap=a._issue(a.ClockCapability,payload=self.auth.payload,root=self.root,
            envelope_bytes=encoded(self.e)+b'\n',activation_bytes=b'a',b6_bytes=b'')
        loaded=(self.auth.record,{},self.p,b'a',b'',b'changed')
        with patch('research.new_cycle_authorization.read_execution',return_value=loaded):
            with self.assertRaisesRegex(ValueError,'bytes changed'):cap.guard()

    def test_global_lock_domain_and_alias_check(self):
        from runtime import DatabaseLock
        with patch.object(b,'safe',side_effect=lambda p:p) as safe:
            one=consumer_exclusion(AUTHORIZED/'incremental-one')
            two=consumer_exclusion(AUTHORIZED/'incremental-two')
            self.assertIsInstance(one,DatabaseLock)
            self.assertEqual(one.path,two.path)
            self.assertEqual(one.path,AUTHORIZED/'paper-consumer-exclusive.lock')
            self.assertEqual(safe.call_args.args[0],two.path)

    def test_consumer_global_contention_precedes_authorization(self):
        from paper.consumer import consume
        from errors import RuntimeOwnershipError
        lease=Mock();lease.acquire.side_effect=RuntimeOwnershipError('Busy')
        with patch('paper.consumer.paths',return_value=(self.root,self.root/'ledger')), \
             patch.object(Path,'is_file',return_value=True), \
             patch.object(Path,'is_symlink',return_value=False), \
             patch.object(Path,'exists',return_value=False), \
             patch('paper.operations.consumer_exclusion',return_value=lease), \
             patch('paper.consumer.prepare_execution') as loader:
            with self.assertRaises(RuntimeOwnershipError):consume('live-0b-other',root=self.root)
            loader.assert_not_called()

    def test_global_lock_released_if_cycle_lock_fails(self):
        from paper.consumer import consume
        from errors import RuntimeOwnershipError
        lease=Mock();cycle=Mock();cycle.acquire.side_effect=RuntimeOwnershipError('Busy')
        with patch('paper.consumer.paths',return_value=(self.root,self.root/'ledger')), \
             patch.object(Path,'is_file',return_value=True), \
             patch.object(Path,'is_symlink',return_value=False), \
             patch.object(Path,'exists',return_value=False), \
             patch('paper.operations.consumer_exclusion',return_value=lease), \
             patch('paper.consumer.DatabaseLock',return_value=cycle), \
             patch('paper.consumer.prepare_execution') as loader:
            with self.assertRaises(RuntimeOwnershipError):consume('live-0b-other',root=self.root)
            loader.assert_not_called();lease.release.assert_called_once()


@unittest.skipUnless(sys.platform=='win32','Windows synthetic ownership')
class DurableTests(unittest.TestCase):
    def test_review_bind_open_accept_and_reopen(self):
        self.exercise(False)

    def test_prospective_clock_v3_confirm_and_reopen(self):
        self.exercise(True)

    def test_global_exclusion_between_processes(self):
        import subprocess
        first=new_run(str(AUTHORIZED));second=new_run(str(AUTHORIZED))
        lease=consumer_exclusion(first)
        code="""import sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from paper.operations import consumer_exclusion
from errors import RuntimeOwnershipError
lock=consumer_exclusion(Path(sys.argv[2]))
try:lock.acquire()
except RuntimeOwnershipError:print('BLOCKED')
else:
    lock.release()
    print('ACQUIRED')
"""
        def child():
            result=subprocess.run([sys.executable,'-I','-B','-c',code,
                str(Path(__file__).resolve().parents[1]),str(second)],
                capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout.strip()
        lease.acquire()
        try:self.assertEqual(child(),'BLOCKED')
        finally:lease.release()
        self.assertEqual(child(),'ACQUIRED')

    def exercise(self,actionable):
        from b6_fixture import SourceFixture
        from paper.verified_source import capture
        from paper.ledger import PaperLedger
        from test_paper_clock_v3 import sample
        from errors import RuntimeOwnershipError
        root=new_run(str(AUTHORIZED));source=SourceFixture(root,rows_per_symbol=9650,observations=0)
        from forward.store import Store
        import test_paper_acceptance as b2
        store=Store(source.directory,source.manifest)
        try:
            for i in range(3):
                signals=('LONG','NONE','NONE') if actionable and i==2 else ('NONE','NONE','NONE')
                item=b2.item(source=source.source,boundary=b1.T+i*14400000,signals=signals)
                store.observation(item.record['observation'])
                store.db.execute('INSERT INTO events(payload) VALUES(?)',(encoded(item.record['receipt']).decode(),))
                store.db.commit()
        finally:store.close()
        old,_=paths('live-0b-001',root);old.mkdir(parents=True)
        sentinel=old/'historical-preservation.test';sentinel.write_bytes(b'OLD_CYCLE_UNTOUCHED')
        owner=Ownership(root);ledger=None
        try:
            now=b1.T+600000;t0=b1.T+14400000;boundary=t0+14400000
            auth=propose('live-0b-new-auth-test',t0,source.source,b3.rules(),now_ms=now)
            initialize(auth,reviewed_fingerprint=auth.id,owner=owner,clock=lambda:now)
            _,directory=paths(auth.record['run_id'],root)
            before=(directory/'paper.sqlite').read_bytes()
            with patch.object(a,'now_ms',return_value=now), patch('paper_admin.now_ms',return_value=now):
                p=proposal(auth.record['run_id'],root,owner);write_proposal(p,root)
                with self.assertRaises((ValueError,FileNotFoundError)):a.prepare_execution(auth.record['run_id'],root)
                with self.assertRaises(FileNotFoundError):bind(auth.record['run_id'],b.NEW_CYCLE,'0'*64,root=root,synthetic_owner=owner)
                bind(auth.record['run_id'],b.NEW_CYCLE,fingerprint(p),root=root,synthetic_owner=owner)
                with self.assertRaises(FileExistsError):bind(auth.record['run_id'],b.NEW_CYCLE,fingerprint(p),root=root,synthetic_owner=owner)
                with self.assertRaisesRegex(ValueError,'not effective'):a.prepare_execution(auth.record['run_id'],root)
            self.assertEqual((directory/'paper.sqlite').read_bytes(),before)
            with patch.object(a,'now_ms',return_value=boundary+240000):
                verified=capture(source.directory,source.source)
                with self.assertRaisesRegex(ValueError,'explicit incremental'):
                    a.prepare_execution(auth.record['run_id'],root).open(verified)
                cap,ledger=a.prepare_execution(auth.record['run_id'],root).open(verified,backend='incremental',incremental_owner=owner)
                ledger.core.clock=lambda:(boundary+240000)/1000
                initial=ledger.reconcile()
                self.assertEqual(initial.equity,1000);self.assertEqual(initial.slot,('EMPTY',None,None))
                self.assertFalse(initial.positions);self.assertFalse(initial.reservations)
                self.assertEqual(ledger.core._load()['sequence'],0)
                other=consumer_exclusion(new_run(str(AUTHORIZED)))
                with self.assertRaises(RuntimeOwnershipError):other.acquire()
                with self.assertRaisesRegex(ValueError,'T0'):cap.accept(ledger,t0)
                with patch('paper.clock_v3.sample',side_effect=[sample(boundary+240000),sample(boundary+240001,1000000)]), \
                     patch.object(PaperLedger,'_verify',side_effect=AssertionError('Replay during transition')):
                    cap.accept(ledger,boundary)
                state=ledger.reconcile();self.assertEqual(state.equity,1000)
                self.assertEqual(state.slot[0],'PENDING' if actionable else 'EMPTY')
                ledger.close();ledger=None;owner.close();owner=Ownership(root)
                cap,ledger=a.prepare_execution(auth.record['run_id'],root).open(verified,backend='incremental',incremental_owner=owner)
                ledger.core.clock=lambda:(boundary+240000)/1000
                with patch('paper.clock_v3.sample',side_effect=AssertionError('Resampled duplicate')):cap.accept(ledger,boundary)
                self.assertEqual(ledger.reconcile().rows(),PaperLedger._verify(ledger).rows())
                self.assertEqual(ledger._db.execute('SELECT count(*) FROM journal').fetchone()[0],2 if actionable else 1)
            self.assertEqual(sentinel.read_bytes(),b'OLD_CYCLE_UNTOUCHED')
            self.assertFalse(paths('live-0b-001',root)[1].exists())
            print('NEW_CYCLE_EXECUTION_SYNTHETIC',root,flush=True)
        finally:
            if ledger is not None:ledger.close()
            owner.close();source.close()

if __name__=='__main__':unittest.main()
