"""Full new-cycle consume connection; only disposable synthetic fixtures."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch,Mock
from types import SimpleNamespace
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from research.spec import encoded
from paper import consumer,authorization as a,binding as b,operations
from research.new_cycle_authorization import ExecutionOwnership,validated_root
from research.incremental_durable import AUTHORIZED,new_run,Ownership


class ConnectionTests(unittest.TestCase):
    def test_bad_backend_before_filesystem(self):
        with patch.object(consumer,'paths') as paths:
            for value in ('auto','',None,True,{}):
                with self.assertRaises(ValueError):consumer.consume('live-0b-invalid',backend=value)
            paths.assert_not_called()

    def test_cli_default_and_explicit(self):
        from paper.__main__ import main
        with patch('paper.__main__.consume',return_value={}) as run,patch('builtins.print'):
            main(['consume','--run-id','live-0b-new'])
            run.assert_called_with('live-0b-new',backend='traditional')
            main(['consume','--run-id','live-0b-new','--backend','incremental'])
            run.assert_called_with('live-0b-new',backend='incremental')

    def test_arbitrary_root_rejected(self):
        with self.assertRaises(ValueError):validated_root(Path('C:/unapproved-cycle'))

    def test_old_cycle_cannot_select_incremental(self):
        root=AUTHORIZED/'incremental-unit';lease=Mock();cycle=Mock()
        plan=SimpleNamespace(ancestry=(b'',b'',encoded({})+b'\n'))
        with patch.object(consumer,'paths',return_value=(root,root/'ledger')), \
             patch.object(Path,'is_file',return_value=True),patch.object(Path,'exists',return_value=False), \
             patch.object(Path,'is_symlink',return_value=False), \
             patch('paper.operations.consumer_exclusion',return_value=lease), \
             patch.object(consumer,'DatabaseLock',return_value=cycle), \
             patch.object(consumer,'prepare_execution',return_value=plan), \
             patch.object(b,'historical',return_value={'kind':b.SUCCESSOR}), \
             patch.object(consumer.sqlite3,'connect') as connect, \
             patch('research.new_cycle_authorization.ExecutionOwnership') as owner:
            with self.assertRaisesRegex(ValueError,'new-cycle binding'):
                consumer.consume('live-0b-001',root=root,backend='incremental')
            connect.assert_not_called();owner.assert_not_called()
            lease.release.assert_called_once();cycle.release.assert_called_once()

    def test_owner_requires_existing_global_lock(self):
        root=AUTHORIZED/'incremental-unit'
        with patch('research.new_cycle_authorization.validated_root',return_value=root), \
             patch('research.new_cycle_authorization.read_execution',return_value=({}, {},{'continuity':str(root)},b'',b'',b'bound')), \
             patch('research.new_cycle_authorization.continuity_directory',return_value=root), \
             patch('research.incremental_durable.checked'), \
             patch('paper.operations.consumer_exclusion'):
            with self.assertRaisesRegex(ValueError,'global consumer'):ExecutionOwnership('live-0b-new',root,object())

    def test_owner_lost_or_cross_capability(self):
        owner=object.__new__(ExecutionOwnership);owner.active=True;owner.root=Path('C:/synthetic-unit')
        owner.run_id='live-0b-new';owner.lock=Mock(_file=Mock(closed=False));owner.global_lock=Mock(_file=Mock(closed=False))
        owner.binding_bytes=b'bound'
        with patch('research.new_cycle_authorization.read_execution',return_value=({}, {},{},b'',b'',b'bound')):
            with self.assertRaises(ValueError):owner.guard(Path('C:/other'))
            with self.assertRaises(ValueError):owner.guard(owner.root,Mock(envelope_bytes=b'foreign'))
            owner.global_lock._file=None
            with self.assertRaisesRegex(ValueError,'lost'):owner.guard(owner.root)


class FingerprintPathTests(unittest.TestCase):
    def auth(self):
        import test_paper_foundation as b1
        import test_paper_settlement as b3
        return operations.propose('live-0b-path-test',b1.T+14400000,
            b1.identity(),b3.rules(),now_ms=b1.T)

    def test_deployment_root_does_not_change_code_identity(self):
        auth=self.auth();identity=operations.implementation()
        # Reproduces the deployment-layout ROOT patch without creating a fixture.
        with patch.object(operations,'ROOT',AUTHORIZED/'incremental-no-source-files'):
            self.assertEqual(operations.implementation(),identity)
            self.assertEqual(operations.Authorization(auth.payload),auth)

    def test_missing_installed_source_still_fails_closed(self):
        target=Path(operations.__file__).resolve()
        original=Path.read_text
        def read(path,*args,**kwargs):
            if path==target:raise FileNotFoundError('Missing installed source')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(FileNotFoundError,'Missing installed source'):
                operations.implementation()

    def test_changed_installed_source_still_invalidates_authorization(self):
        auth=self.auth();target=Path(operations.__file__).resolve()
        original=Path.read_text
        def read(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            return value+'\n# simulated source mutation\n' if path==target else value
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(ValueError,'identity/configuration mismatch'):
                operations.Authorization(auth.payload)


@unittest.skipUnless(sys.platform=='win32','Windows durable fixtures')
class IntegralTests(unittest.TestCase):
    def test_full_consume_and_restart_synthetic_layout(self):self.exercise(False)
    def test_full_consume_and_restart_deployment_layout(self):self.exercise(True)

    def exercise(self,deployment_layout):
        from b6_fixture import SourceFixture
        from forward.store import Store
        from research.spec import encoded,fingerprint
        from research.new_paper_cycle import initialize
        from research.new_cycle_authorization import proposal
        from paper_admin import bind,write_proposal
        from research.incremental_full_consumer import _ConsumerCore
        from paper.ledger import PaperLedger
        from test_paper_clock_v3 import sample
        import test_paper_foundation as b1
        import test_paper_settlement as b3
        import test_paper_acceptance as b2
        import time
        root=new_run(str(AUTHORIZED));source=SourceFixture(root,rows_per_symbol=9650,observations=0)
        run='live-0b-full-new';t0=b1.T+14400000;boundary=t0+14400000;now=boundary+240000;created=b1.T+600000
        store=Store(source.directory,source.manifest)
        try:
            for i in range(3):
                item=b2.item(source=source.source,boundary=b1.T+i*14400000,
                    signals=('LONG','NONE','NONE') if i==2 else ('NONE','NONE','NONE'))
                store.observation(item.record['observation'])
                store.db.execute('INSERT INTO events(payload) VALUES(?)',(encoded(item.record['receipt']).decode(),));store.db.commit()
        finally:store.close()
        owner=Ownership(root)
        try:
            auth=operations.propose(run,t0,source.source,b3.rules(),now_ms=created)
            initialize(auth,reviewed_fingerprint=auth.id,owner=owner,clock=lambda:created)
            _,directory=operations.paths(run,root)
            with ExitStack() as setup:
                if deployment_layout:
                    # Only copies test-generated continuity into the canonical
                    # per-ledger layout; never reads any operational artifact.
                    setup.enter_context(patch.object(operations,'ROOT',root))
                    for prefix in ('continuity','audit'):
                        for path in root.glob(prefix+'-*'):
                            with (directory/path.name).open('xb') as target:target.write(path.read_bytes())
                setup.enter_context(patch.object(a,'now_ms',return_value=created))
                setup.enter_context(patch('paper_admin.now_ms',return_value=created))
                p=proposal(run,root,owner);write_proposal(p,root)
                bind(run,b.NEW_CYCLE,fingerprint(p),root=root,synthetic_owner=owner)
        finally:owner.close()
        old,_=operations.paths('live-0b-001',root);old.mkdir(parents=True)
        sentinel=old/'old-history.test';sentinel.write_bytes(b'UNTOUCHED')
        try:
            with ExitStack() as stack:
                if deployment_layout:stack.enter_context(patch.object(operations,'ROOT',root))
                stack.enter_context(patch.object(a,'now_ms',return_value=now))
                def core(*args,**kwargs):
                    kwargs['clock']=lambda:now/1000
                    return _ConsumerCore(*args,**kwargs)
                stack.enter_context(patch('research.incremental_full_consumer._ConsumerCore',side_effect=core))
                original_verify=PaperLedger._verify
                def verify(obj,*args,**kwargs):
                    if getattr(obj,'core',None) is not None:
                        raise AssertionError('Full replay during incremental transition')
                    return original_verify(obj,*args,**kwargs)
                stack.enter_context(patch.object(PaperLedger,'_verify',verify))
                samples=stack.enter_context(patch('paper.clock_v3.sample',side_effect=[sample(now),sample(now+1,1000000)]))
                started=time.monotonic();result=consumer.consume(run,root=root,clock=lambda:now,backend='incremental')
                print('NEW_CYCLE_CONSUME',deployment_layout,time.monotonic()-started,result,root,flush=True)
                self.assertEqual(result['state'],'HEALTHY',result);self.assertEqual(result['transitions'],1)
                state,cp=a.state_at(run,root,auth.record)
                self.assertEqual(cp['journal_count'],2);self.assertEqual(state.equity,1000)
                self.assertEqual(state.slot[0],'PENDING');self.assertFalse(state.positions)
                self.assertEqual(list(state.batches),[boundary])
                again=consumer.consume(run,root=root,clock=lambda:now,backend='incremental')
                self.assertEqual(again['state'],'WAITING',again);self.assertEqual(again['transitions'],0)
                self.assertEqual(a.state_at(run,root,auth.record)[1],cp)
                self.assertEqual(samples.call_count,2)
                # Binding mutation must refuse a fresh invocation, never fallback.
                _,target=b.locations(run,root,b.NEW_CYCLE)
                value,_=b.read(target);value['proposal']['activation']='0'*64
                value['reviewed_fingerprint']=fingerprint(value['proposal'])
                target.write_bytes(encoded(value)+b'\n')
                with self.assertRaises(ValueError):consumer.consume(run,root=root,clock=lambda:now,backend='incremental')
                self.assertEqual(a.state_at(run,root,auth.record)[1],cp)
            self.assertEqual(sentinel.read_bytes(),b'UNTOUCHED')
        finally:source.close()

if __name__=='__main__':unittest.main()
