"""Direct unittest on NEW synthetic directories; fixtures retained for review.

No production consumer/loader. Child exits deliberately at existing fault hooks.
"""
import multiprocessing
import os
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
import test_incremental_consumer as memory_tests
from paper.ledger import PaperLedger
from research.incremental_consumer import DurablePositionLedger
from research.incremental_durable import AUTHORIZED, new_run, Ownership, connect, DurableExternal


def worker(run, action, closing, point, output):
    helper=memory_tests.ConsumerIntegration()
    owner=None;db=None
    try:
        v,reservation,stamp,candles=helper.prepare(closing)
        owner=Ownership(Path(run))
        db=connect(owner,initialize=action=='init')
        if action=='init':v._db.backup(db)
        v._db=db
        def crash(stage):
            if point==stage:os._exit(77)
        external=DurableExternal(owner,fault=crash)
        adapter=DurablePositionLedger(v,external,authorization_id='synthetic-durable-consumer',
            authorize=lambda:'synthetic-durable-consumer',clock=lambda:100,initialize=action=='init')
        apply=adapter.core.apply
        with patch.object(PaperLedger,'_verify',side_effect=AssertionError('Unexpected transition replay')) as replay:
            if action=='apply':
                with patch.object(adapter.core,'apply',side_effect=lambda k,d:apply(k,d,fault=crash)):
                    helper.run_loop(adapter,stamp,candles)
            if action=='retry':adapter.process_candle(reservation,stamp)
            adapter.reconcile()
            replay.assert_not_called()
        # Independent oracle AFTER the measured transition, never used by it.
        state=v._verify()
        output.send(('PASS',helper.rows(v),state.rows(),str(state.equity),
                     db.execute('PRAGMA integrity_check').fetchall()))
    except BaseException as exc:
        output.send(('FAIL',type(exc).__name__,str(exc),traceback.format_exc()))
    finally:
        if db is not None:db.close()
        if owner is not None:owner.close()
        helper.doCleanups();output.close()


@unittest.skipUnless(sys.platform=='win32','Windows ownership backend')
class DurableConsumerTests(unittest.TestCase):
    def call(self,run,action,closing=False,point=None,exitcode=0):
        ctx=multiprocessing.get_context('spawn')
        receive,send=ctx.Pipe(duplex=False)
        p=ctx.Process(target=worker,args=(str(run),action,closing,point,send))
        p.start();send.close()
        try:
            # Drain the pipe before join: financial rows can exceed pipe capacity.
            result=None
            try:
                if receive.poll(60):result=receive.recv()
            except (EOFError,BrokenPipeError):pass
            p.join(10)
            if p.is_alive():
                p.terminate();p.join(10)
                self.fail('Synthetic child timeout')
            self.assertEqual(p.exitcode,exitcode,result)
            return result
        finally:
            if p.is_alive():p.terminate();p.join(10)
            receive.close();p.close()

    def fresh(self,closing=False):
        run=new_run(str(AUTHORIZED))
        result=self.call(run,'init',closing)
        self.assertIsNotNone(result,'No child response: '+str(run))
        self.assertEqual(result[0],'PASS',f'{run}\n{result}')
        print('Synthetic fixture:',run,flush=True)
        return run

    def expected(self,closing):
        h=memory_tests.ConsumerIntegration()
        try:
            v,_,stamp,candles=h.prepare(closing)
            h.run_loop(v,stamp,candles)
            state=v._verify()
            return ('PASS',h.rows(v),state.rows(),str(state.equity),[('ok',)])
        finally:h.doCleanups()

    def test_reopen_financial_equivalence_and_idempotency(self):
        for closing in (False,True):
            with self.subTest(closing=closing):
                run=self.fresh(closing);expected=self.expected(closing)
                self.assertEqual(self.call(run,'apply',closing),expected)
                self.assertEqual(self.call(run,'recover',closing),expected)
                self.assertEqual(self.call(run,'retry',closing),expected)

    def test_process_crashes_rollback_or_recover_committed_event(self):
        for point in ('before_event','after_event','after_financial','after_state',
                      'after_commit','after_publish','receipt_synced'):
            with self.subTest(point=point):
                run=self.fresh(True)
                before=self.call(run,'recover',True)
                self.call(run,'apply',True,point,77)
                recovered=self.call(run,'recover',True)
                expected=self.expected(True)
                committed=point in ('after_commit','after_publish','receipt_synced')
                self.assertEqual(recovered,expected if committed else before)
                self.assertEqual(self.call(run,'retry',True),expected)

    def test_partial_external_publication_blocks_without_repair(self):
        for point in ('partial_record','record_synced'):
            with self.subTest(point=point):
                run=self.fresh()
                self.call(run,'apply',False,point,77)
                first=self.call(run,'recover')
                self.assertEqual(first[0],'FAIL')
                self.assertEqual(first[1],'Blocked')
                retry=self.call(run,'retry')
                self.assertEqual(retry[:2],('FAIL','Blocked'))


if __name__=='__main__':unittest.main()
