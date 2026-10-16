"""Administrative new-cycle flow; every durable test allocates a new synthetic root."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from paper import operations,authorization as a,binding as b
import paper_admin as admin
from research.new_paper_cycle import AdministrativeOwnership
from research.incremental_full_consumer import validate_incremental_owner
from research.incremental_durable import AUTHORIZED,new_run,Blocked
from research.spec import fingerprint
import test_paper_foundation as b1
import test_paper_settlement as b3


class AdministrativeTests(unittest.TestCase):
    def auth(self):
        return operations.propose('live-0b-002',b1.T+14400000,b1.identity(),b3.rules(),now_ms=b1.T)

    def test_wrong_approval_precedes_ownership(self):
        with patch('research.new_paper_cycle.AdministrativeOwnership') as owner:
            with self.assertRaises(ValueError):admin.initialize_new_cycle(self.auth(),'0'*64)
            owner.assert_not_called()

    def test_arbitrary_root_rejected_without_lock(self):
        with patch('paper.operations.consumer_exclusion') as lock:
            with self.assertRaises(Blocked):AdministrativeOwnership('live-0b-002',Path('C:/arbitrary'),'initialize')
            lock.assert_not_called()

    def test_invalid_purpose_rejected_without_lock(self):
        with patch('paper.operations.consumer_exclusion') as lock:
            with self.assertRaises(Blocked):AdministrativeOwnership('live-0b-002',operations.ROOT,'consume')
            lock.assert_not_called()

    def test_administrative_owner_cannot_execute(self):
        owner=object.__new__(AdministrativeOwnership)
        owner.active=True;owner.run=AUTHORIZED/'incremental-unit'
        with self.assertRaises(Blocked):validate_incremental_owner(owner,owner.run)

    def test_wrong_purpose_or_closed_owner_rejected(self):
        owner=object.__new__(AdministrativeOwnership)
        owner.active=False;owner.purpose='initialize';owner.run_id='live-0b-002';owner._locks=[]
        for purpose,run in [('initialize','live-0b-002'),('bind','live-0b-002'),('initialize','live-0b-other')]:
            with self.assertRaises(Blocked):owner.guard(purpose,run)

    def test_cli_requires_separate_exact_initialization_review(self):
        with patch.object(admin,'initialize_new_cycle') as initialize,patch('sys.stderr'):
            with self.assertRaises(SystemExit):admin.main(['initialize-new-cycle','--run-id','live-0b-002'])
            initialize.assert_not_called()


@unittest.skipUnless(sys.platform=='win32','Windows durable fixtures')
class DurableAdministrativeTests(unittest.TestCase):
    def test_initialize_propose_review_bind_and_reopen(self):
        from b6_fixture import SourceFixture
        root=new_run(str(AUTHORIZED));source=SourceFixture(root)
        run='live-0b-002';created=b1.T+600000;t0=b1.T+14400000
        old,old_ledger=operations.paths('live-0b-001',root)
        old.mkdir(parents=True);old_ledger.mkdir(parents=True)
        for path in (old/'activation.json',old_ledger/'paper.sqlite'):
            path.write_bytes(b'OLD_FIXTURE_NEVER_OPENED')
        try:
            with patch.object(operations,'ROOT',root),patch.object(a,'now_ms',return_value=created),patch.object(admin,'now_ms',return_value=created):
                auth=operations.propose(run,t0,source.source,b3.rules(),now_ms=created)
                from errors import RuntimeOwnershipError
                global_lease=operations.consumer_exclusion(root);global_lease.acquire()
                try:
                    with self.assertRaises(RuntimeOwnershipError):
                        admin.initialize_new_cycle(auth,auth.id,root=root,clock=lambda:created)
                finally:global_lease.release()
                result=admin.initialize_new_cycle(auth,auth.id,root=root,clock=lambda:created)
                self.assertEqual(result['sequence'],0);self.assertEqual(result['equity'],'1000')
                self.assertEqual(result['slot'],'EMPTY');self.assertEqual(result['first_eligible_boundary_ms'],t0+14400000)
                control,directory=operations.paths(run,root)
                database_before=(directory/'paper.sqlite').read_bytes()
                state,cp=a.state_at(run,root,auth.record)
                self.assertFalse(state.positions);self.assertFalse(state.reservations);self.assertEqual(cp['journal_count'],0)
                self.assertTrue(list(directory.glob('continuity-*.json')))
                self.assertFalse(list(root.glob('continuity-*.json')))
                with self.assertRaises(Blocked):admin.initialize_new_cycle(auth,auth.id,root=root,clock=lambda:created)
                with self.assertRaises((FileNotFoundError,ValueError)):a.prepare_execution(run,root)
                report=admin.propose_new_cycle(run,root=root)
                proposal=report['proposal'];self.assertEqual(report['fingerprint'],fingerprint(proposal))
                self.assertEqual(proposal['backend'],'incremental');self.assertEqual(proposal['ledger'],str(directory/'paper.sqlite'))
                self.assertEqual(proposal['checkpoint'],cp)
                self.assertEqual(proposal['activation'],auth.id)
                self.assertEqual(proposal['continuity'],str(directory))
                target=b.locations(run,root,b.NEW_CYCLE)[1];self.assertFalse(target.exists())
                with self.assertRaises((FileNotFoundError,ValueError)):a.prepare_execution(run,root)
                with self.assertRaises(FileNotFoundError):admin.bind(run,b.NEW_CYCLE,'0'*64,root=root)
                self.assertFalse(target.exists())
                binding_id=admin.bind(run,b.NEW_CYCLE,report['fingerprint'],root=root)
                bound_bytes=target.read_bytes()
                self.assertEqual(binding_id,fingerprint(b.read(target)[0]))
                with self.assertRaises(FileExistsError):admin.bind(run,b.NEW_CYCLE,report['fingerprint'],root=root)
                self.assertEqual(target.read_bytes(),bound_bytes)
                with self.assertRaisesRegex(ValueError,'not effective'):a.prepare_execution(run,root)
                self.assertEqual((directory/'paper.sqlite').read_bytes(),database_before)
                with patch.object(a,'now_ms',return_value=t0):
                    first=a.load_execution(run,root);second=a.load_execution(run,root)
                    self.assertEqual(first.bound_id,second.bound_id)
                    self.assertEqual(first.proposal['checkpoint'],cp)
                    with self.assertRaisesRegex(ValueError,'T0'):first.check(first.binding,t0,t0)
                self.assertEqual(a.state_at(run,root,auth.record)[1],cp)
            for path in (old/'activation.json',old_ledger/'paper.sqlite'):
                self.assertEqual(path.read_bytes(),b'OLD_FIXTURE_NEVER_OPENED')
            print('ADMINISTRATIVE_NEW_CYCLE_PASS',root,result,flush=True)
        finally:source.close()

    def test_interrupted_initialization_never_repaired_or_overwritten(self):
        from b6_fixture import SourceFixture
        root=new_run(str(AUTHORIZED));source=SourceFixture(root)
        created=b1.T+600000;run='live-0b-002'
        try:
            with patch.object(operations,'ROOT',root):
                auth=operations.propose(run,b1.T+14400000,source.source,b3.rules(),now_ms=created)
                # Activation is persisted by its own writer; fail at the next
                # create-once marker. Preserve the partial cycle for review.
                with patch('research.new_paper_cycle.exclusive',side_effect=OSError('injected interruption')):
                    with self.assertRaises(OSError):admin.initialize_new_cycle(auth,auth.id,root=root,clock=lambda:created)
                control,directory=operations.paths(run,root)
                saved=(control/'activation.json').read_bytes()
                with self.assertRaises(Blocked):admin.initialize_new_cycle(auth,auth.id,root=root,clock=lambda:created)
                self.assertEqual((control/'activation.json').read_bytes(),saved)
                self.assertFalse(directory.exists())
                self.assertFalse(b.locations(run,root,b.NEW_CYCLE)[1].exists())
        finally:source.close()

if __name__=='__main__':unittest.main()
