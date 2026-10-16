"""Full consume() on newly generated synthetic authorization/source/SQLite.

Run directly on Windows. No operational root and no scheduler invocation.
"""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from benchmark_live0b_stages import build, RUN
from research.incremental_durable import new_run, AUTHORIZED, Ownership
from research.incremental_full_consumer import IncrementalConsumerLedger
from paper import consumer, authorization, ledger as ledger_module
from paper.ledger import PaperLedger
from paper.operations import paths
from forward import operations as forward_operations
from research.incremental_ledger import SyntheticLedger


def accepting_fixture(root):
    from b6_fixture import SourceFixture
    from artifact_fixture import fixture, bind_clock, T
    import test_paper_acceptance as b2
    from forward.store import Store
    from research.spec import encoded
    source=SourceFixture(root,rows_per_symbol=9617,observations=0)
    try:
        store=Store(source.directory,source.manifest)
        try:
            item=b2.item(source=source.source,boundary=T,signals=('LONG','NONE','NONE'))
            store.observation(item.record['observation'])
            store.db.execute('INSERT INTO events(payload) VALUES(?)',(encoded(item.record['receipt']).decode(),))
            store.db.commit()
        finally:store.close()
        f=fixture(root,RUN,source=source.source)
        bind_clock(f,effective=T)
        return dict(now=T+240000,patch_root=source.root['operations_fingerprint'])
    finally:source.close()


def financial_rows(directory):
    with closing(sqlite3.connect((directory/'paper.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        tables=('identity','journal','account','global_slot','reservations','positions',
                'fills','trades','equity','candles','processed')
        return {t:sorted(db.execute('SELECT * FROM '+t).fetchall()) for t in tables}


@unittest.skipUnless(sys.platform=='win32','Windows durable ownership')
class FullConsumerTests(unittest.TestCase):
    def test_complete_invocation_exact_and_restart(self):
        self.compare_invocations(False)

    def test_clock_v3_accept_confirm_exact_and_no_resampling(self):
        self.compare_invocations(True)

    def compare_invocations(self,acceptance):
        outputs=[]
        for incremental in (False,True):
            root=new_run(str(AUTHORIZED))
            print('Synthetic full invocation:',root,flush=True)
            meta=accepting_fixture(root) if acceptance else build(root,bars=2,backlog=1)
            _,directory=paths(RUN,root)
            owner=Ownership(root)
            instances=[];normal_replays=[]
            original_verify=PaperLedger._verify
            original_load=SyntheticLedger._load
            def diagnosed_load(core):
                # Read on the same connection/transaction, immediately before
                # validation. Diagnostics never replace or suppress that check.
                tail=core.db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
                rows=core.db.execute('SELECT payload FROM incremental_state').fetchall()
                try:
                    compact=json.loads(rows[0][0]) if len(rows)==1 else {}
                    evidence=dict(journal_tail=tail,compact_sequence=compact.get('sequence'),
                        compact_head=compact.get('state',{}).get('head'),
                        genesis=core.v.identity_id,in_transaction=core.db.in_transaction)
                except Exception:evidence=dict(journal_tail=tail,compact_rows=len(rows))
                try:return original_load(core)
                except Exception:
                    print('LOAD_FAILURE '+json.dumps(evidence),flush=True)
                    raise
            class Selected(IncrementalConsumerLedger):
                def __init__(self,*args,**kwargs):
                    self.ready=False
                    super().__init__(*args,synthetic_owner=owner,initialize=True,**kwargs)
                    self.ready=True;instances.append(self)
            def verify(obj,*args,**kwargs):
                if isinstance(obj,Selected) and obj.ready:
                    normal_replays.append(1)
                    raise AssertionError('Full replay during normal incremental processing')
                return original_verify(obj,*args,**kwargs)
            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.object(forward_operations,'PATCH_ROOT',meta['patch_root']))
                    stack.enter_context(patch.object(authorization,'now_ms',return_value=meta['now']))
                    if acceptance:
                        from paper import clock_v3
                        from test_paper_clock_v3 import sample
                        samples=stack.enter_context(patch.object(clock_v3,'sample',side_effect=[
                            sample(meta['now']),sample(meta['now']+1,1000000)]))
                    if incremental:
                        stack.enter_context(patch.object(ledger_module,'PaperLedger',Selected))
                        stack.enter_context(patch.object(PaperLedger,'_verify',verify))
                        stack.enter_context(patch.object(SyntheticLedger,'_load',diagnosed_load))
                    start=time.monotonic()
                    result=consumer.consume(RUN,root=root,clock=lambda:meta['now'])
                    elapsed=time.monotonic()-start
                    self.assertEqual(result['state'],'HEALTHY',result)
                    self.assertEqual(result['transitions'],1,result)
                    after=financial_rows(directory)
                    # Fresh invocation must neither repeat history nor replay per BAR.
                    again=consumer.consume(RUN,root=root,clock=lambda:meta['now'])
                    self.assertEqual(again['state'],'WAITING',again)
                    self.assertEqual(again['transitions'],0)
                    self.assertEqual(financial_rows(directory),after)
                    if acceptance:
                        self.assertEqual(samples.call_count,2)
                        self.assertEqual(len(after['journal']),2)
                        self.assertEqual(after['global_slot'][0][1],'PENDING')
                self.assertEqual(normal_replays,[])
                # Full independent historical oracle outside invocation timing.
                state,_=authorization.state_at(RUN,root,meta_record(root,meta))
                outputs.append((after,state.rows(),state.equity))
                print(json.dumps(dict(incremental=incremental,total_including_cleanup=elapsed,
                    consumer=result,initialization=[x.integration_times for x in instances],
                    normal_full_replays=len(normal_replays))),flush=True)
            finally:owner.close()
        self.assertEqual(outputs[0],outputs[1])


def meta_record(root,meta):
    with patch.object(forward_operations,'PATCH_ROOT',meta['patch_root']), \
         patch.object(authorization,'now_ms',return_value=meta['now']):
        return authorization.chain(RUN,root)[0]


if __name__=='__main__':unittest.main()
