"""New-cycle initialization only; all disk fixtures are new synthetic directories."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from paper.operations import propose, paths
from paper.authorization import prepare_execution
from paper.ledger import PaperLedger
from paper.source import SourceAdapter
from paper.market import CandleSource
from research.new_paper_cycle import initialize
from research.incremental_durable import AUTHORIZED, new_run, Ownership, DurableExternal, Blocked
from research.incremental_full_consumer import _ConsumerCore
import test_paper_foundation as b1
import test_paper_settlement as b3


class ApprovalTests(unittest.TestCase):
    def auth(self):
        return propose('live-0b-new-test',b1.T+14400000,b1.identity(),b3.rules(),now_ms=b1.T)

    def test_unreviewed_fingerprint_rejected_before_filesystem(self):
        with patch('research.new_paper_cycle.validate_synthetic_owner') as ownership:
            with self.assertRaisesRegex(Blocked,'reviewed'):
                initialize(self.auth(),reviewed_fingerprint='0'*64,owner=None,clock=lambda:b1.T)
            ownership.assert_not_called()

    def test_missing_owner_rejected(self):
        auth=self.auth()
        with self.assertRaises(Blocked):initialize(auth,reviewed_fingerprint=auth.id,owner=None,clock=lambda:b1.T)

    def test_bad_run_or_nonfuture_start_rejected(self):
        for run,t0 in (('../old',b1.T+14400000),('live-0b-new-test',b1.T),('live-0b-new-test',b1.T+1)):
            with self.subTest(run=run,t0=t0),self.assertRaises(ValueError):
                propose(run,t0,b1.identity(),b3.rules(),now_ms=b1.T)


@unittest.skipUnless(sys.platform=='win32','Windows ownership required')
class DurableNewCycleTests(unittest.TestCase):
    def test_empty_genesis_restart_and_no_execution_authority(self):
        from b6_fixture import SourceFixture
        root=new_run(str(AUTHORIZED));source=SourceFixture(root)
        owner=Ownership(root)
        try:
            now=b1.T+600000;t0=b1.T+14400000
            auth=propose('live-0b-new-test',t0,source.source,b3.rules(),now_ms=now)
            result=initialize(auth,reviewed_fingerprint=auth.id,owner=owner,clock=lambda:now)
            self.assertEqual(result['equity'],'1000');self.assertEqual(result['sequence'],0)
            self.assertEqual(result['first_eligible_boundary_ms'],t0+14400000)
            control,directory=paths(auth.record['run_id'],root)
            before=(directory/'paper.sqlite').read_bytes()
            with self.assertRaises(Blocked):initialize(auth,reviewed_fingerprint=auth.id,owner=owner,clock=lambda:now)
            self.assertEqual((directory/'paper.sqlite').read_bytes(),before)
            owner.close();owner=Ownership(root)  # Release/reacquire persistent ownership.
            ledger=PaperLedger(directory,auth.binding,auth.rules,
                SourceAdapter(source.directory,source.source),CandleSource(source.directory,source.source))
            try:
                core=_ConsumerCore(ledger,DurableExternal(owner),authorization_id=auth.id,
                    authorize=lambda:auth.id,evidence=lambda k,d:(_ for _ in ()).throw(Blocked('No execution')),
                    clock=lambda:now/1000)
                state=core._state(core.recover())
                self.assertEqual(state.rows(),ledger._verify().rows())
                self.assertEqual(state.slot,('EMPTY',None,None))
                self.assertEqual(state.equity,1000)
                for table in ('journal','reservations','positions','fills','trades','equity','processed'):
                    self.assertEqual(ledger._db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
                core.audit()
            finally:ledger.close()
            with self.assertRaises((ValueError,FileNotFoundError)):
                prepare_execution(auth.record['run_id'],root)
            self.assertFalse(paths('live-0b-001',root)[0].exists())
            self.assertFalse(paths('live-0b-001',root)[1].exists())
            print('NEW_CYCLE_SYNTHETIC',root,result,flush=True)
        finally:owner.close();source.close()


if __name__=='__main__':unittest.main()
