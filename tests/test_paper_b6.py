"""B6 offline tests: synthetic temporary stores only."""
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_paper_foundation as b1
import test_paper_acceptance as b2
import test_paper_operations as b5
from paper.git_batch import blobs
from paper import verified_source as vs
from forward.store import Store
from forward import operations as fo
from backtest.data import Candle, SYMBOLS
from research.spec import encoded, fingerprint

REQUEST = '0'*40+':file.py'


def framed(value):
    oid=hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest()
    return oid.encode()+b' blob '+str(len(value)).encode()+b'\n'+value+b'\n'


class BatchTests(unittest.TestCase):
    def run_bytes(self, value):
        with patch('paper.git_batch.subprocess.run',return_value=SimpleNamespace(stdout=value)) as call:
            result=blobs('.', [REQUEST]);self.assertEqual(call.call_count,1)
            return result,call.call_args.kwargs

    def test_exact_binary_framing_and_immutable_result(self):
        value=b'hi\n\0\r\n'
        result,_=self.run_bytes(framed(value))
        self.assertEqual(result[REQUEST],value)
        with self.assertRaises(TypeError):result[REQUEST]=b'bad'

    def test_malformed_truncated_wrong_type_wrong_length_errors(self):
        valid=framed(b'abc')
        cases=[b'',valid[:-1],valid[:-3],valid+b'extra',valid.replace(b'blob',b'tree'),
               valid.replace(b' 3\n',b' 4\n'),valid.replace(b'abc',b'abd'),
               b'x missing\n',b'0'*40+b' blob -1\n',b'0'*40+b' blob 9000000\n']
        for raw in cases:
            with self.subTest(raw=raw),self.assertRaises(ValueError):self.run_bytes(raw)

    def test_process_failure_timeout_no_retry(self):
        for error in (subprocess.CalledProcessError(1,'git'),subprocess.TimeoutExpired('git',1)):
            with patch('paper.git_batch.subprocess.run',side_effect=error) as call:
                with self.assertRaises(type(error)):blobs(Path.cwd(),[REQUEST])
                self.assertEqual(call.call_count,1)

    def test_windows_and_non_windows_flags(self):
        for name,expected in [('nt',0x08000000),('posix',0)]:
            with patch('paper.git_batch.os.name',name):
                _,kwargs=self.run_bytes(framed(b'abc'))
                self.assertEqual(kwargs['creationflags'],expected)
                self.assertEqual(kwargs['timeout'],15)

    def test_invalid_requests_fail_before_launch(self):
        for values in ([],[REQUEST,REQUEST],['HEAD:file.py'],['0'*40+':../foo']):
            with patch('paper.git_batch.subprocess.run') as call,self.assertRaises(ValueError):
                blobs(Path.cwd(),values)
            call.assert_not_called()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.source=b1.identity();self.t=b1.T
        self.meta=dict(manifest_fingerprint=self.source.manifest,history_start_ms=self.t-900000,first_observation_ms=self.t)
        self.store=Store(self.path,self.meta);self.addCleanup(self.store.close)
        self.append(self.t-900000)
        self.cp=fo.checkpoint(self.store.db)
        self.root=dict(operations_fingerprint=self.source.operations,checkpoint=self.cp,
                      effective_boundary_ms=self.t,policy=fo.policy())
        self.patch=dict(checkpoint=self.cp)
        self.addCleanup(patch.stopall)
        patch.object(vs,'_authorization',return_value=(self.meta,self.root,self.patch,())).start()
        patch.object(vs.fm,'implementation',return_value=self.source.implementation).start()
        patch.object(vs.fm,'verify_references').start()

    def append(self,t):
        for s in SYMBOLS:
            self.store.append(s,[Candle(s,'15m',t,t+899999,'100','101','99','100','1')],received_ms=t+900000)

    def capture(self):return vs.capture(self.path,self.source)

    def test_one_full_pass_detached_immutable_no_cross_invocation_cache(self):
        original=Store.verify
        with patch.object(Store,'verify',autospec=True,side_effect=original) as verify:
            a=self.capture();self.assertEqual(verify.call_count,1)
        self.append(self.t)
        self.assertIsNone(a.at('BTCUSDT',self.t))
        b=self.capture();self.assertIsNotNone(b.at('BTCUSDT',self.t));self.assertNotEqual(a.id,b.id)
        with self.assertRaises(TypeError):a.candles['BTCUSDT',self.t]=None
        with self.assertRaises(Exception):a.observations=()

    def test_concurrent_append_during_transaction_is_next_invocation_only(self):
        original=fo.verify_checkpoint;called=[]
        def append_after_read(db,cp):
            original(db,cp)
            if not called:called.append(1);self.append(self.t)
        with patch.object(fo,'verify_checkpoint',side_effect=append_after_read):a=self.capture()
        self.assertIsNone(a.at('BTCUSDT',self.t))
        self.assertIsNotNone(self.capture().at('BTCUSDT',self.t))

    def test_receipt_after_capture_not_incorporated(self):
        o=b1.observation();self.store.observation(o)
        a=self.capture();self.assertIsNone(a.observations[0].receipt)
        r=b1.receipt(o)
        self.store.db.execute('INSERT INTO events(payload) VALUES(?)',(encoded(r).decode(),));self.store.db.commit()
        self.assertIsNone(a.observations[0].receipt)
        self.assertIsNotNone(self.capture().observations[0].receipt)

    def test_historical_checkpoint_tamper(self):
        self.root['checkpoint']=dict(self.cp,candles=dict(count=3,fingerprint='0'*64))
        with self.assertRaises(ValueError):self.capture()

    def test_data_tamper_rejected(self):
        self.store.db.execute('DROP TRIGGER no_UPDATE_candles')
        self.store.db.execute("UPDATE candles SET digest=?",('0'*64,));self.store.db.commit()
        with self.assertRaises(ValueError):self.capture()

    def test_blocked_and_current_code_mutation(self):
        a=self.capture()
        with patch.object(vs.fm,'implementation',return_value='wrong'),self.assertRaises(ValueError):a.guard()
        (self.path/'BLOCKED.json').write_text('{}')
        with self.assertRaises(ValueError):a.guard()
        with self.assertRaises(ValueError):self.capture()

    def test_missing_candle_is_unresolved_not_invented(self):
        self.assertIsNone(self.capture().at('BTCUSDT',self.t))


class InvocationTests(b5.OperationsTests):
    # Inherited tests also exercise all existing B5 deterministic clock/financial
    # fixtures via the detached interface. They are deliberately not live runs.
    def test_actual_clock_after_verification_crosses_deadline(self):
        self.arm()
        from paper import consumer
        original=consumer.capture.side_effect
        def delayed(*args,**kwargs):
            result=original(*args,**kwargs);self.now=b1.T+840001;return result
        consumer.capture.side_effect=delayed
        self.run_once()
        result=self.out('latest')['decision']
        self.assertIsNone(result['reservation'])
        self.assertTrue(all(d['reason'] != 'PENDING_ENTRY' for d in result['effective_dispositions']))

    def test_no_legacy_source_reads_during_consume(self):
        self.arm()
        with patch.object(vs,'capture'):
            from paper.consumer import capture
            with patch.object(__import__('paper.source',fromlist=['SourceAdapter']).SourceAdapter,'snapshot',side_effect=AssertionError('extra source verification')):
                self.run_once()
            self.assertEqual(capture.call_count,1)


class RealVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        from b6_fixture import SourceFixture
        self.fixture=SourceFixture(self.root);self.addCleanup(self.fixture.close)

    def test_full_invocation_exactly_one_git_one_source_scan(self):
        from paper.operations import propose,paths,exclusive
        from paper.consumer import consume
        import test_paper_settlement as b3
        run='live-0b-b6-real-fixture'
        auth=propose(run,b1.T,self.fixture.source,b3.rules(),now_ms=b1.T-14400000)
        control,_=paths(run,self.root);control.mkdir(parents=True)
        exclusive(control/'activation.json',dict(authorization=auth.record,authorization_fingerprint=auth.id))
        original=Store.verify;process=subprocess.run
        with patch.object(Store,'verify',autospec=True,side_effect=original) as verify,patch('paper.git_batch.subprocess.run',wraps=process) as calls:
            result=consume(run,root=self.root,clock=lambda:b1.T+400000)
        self.assertEqual(result['state'],'WAITING');self.assertEqual(calls.call_count,1);self.assertEqual(verify.call_count,1)
        self.assertEqual(calls.call_args.args[0],['git','cat-file','--batch'])
        if os.name=='nt':self.assertEqual(calls.call_args.kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)

    def test_actual_authorization_tampering_rejected(self):
        path=self.fixture.directory/'operations-patch-v1.json'
        value=b1.parse(path.read_bytes()[:-1]);value['new_implementation']='0'*64
        path.write_bytes(encoded(value)+b'\n')
        with self.assertRaises(ValueError):vs.capture(self.fixture.directory,self.fixture.source)

    def test_real_legacy_and_new_views_identical(self):
        from paper.source import SourceAdapter
        old=SourceAdapter(self.fixture.directory,self.fixture.source).snapshot()
        new=vs.capture(self.fixture.directory,self.fixture.source)
        self.assertEqual(old,new.observations)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        from paper import transition as tr,operations as op
        from paper.ledger import SCHEMA,IMMUTABLE
        from paper.journal import Replay,HISTORICAL
        from paper.acceptance import AcceptanceBinding,ProspectiveGate
        from paper.model import DraftManifest,LABEL
        import test_paper_settlement as b3
        self.tr,self.op=tr,op
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.run='live-0b-legacy-fixture'
        self.v=op.record(self.run,b1.T-14400000,b1.T-28800000,b1.identity(),b3.rules())
        self.v['implementations'].update(b4=tr.OLD_B4,b5=tr.OLD_B5)
        self.control,self.ledger=op.paths(self.run,self.root)
        self.control.mkdir(parents=True);self.ledger.mkdir(parents=True)
        op.exclusive(self.control/'activation.json',dict(authorization=self.v,authorization_fingerprint=fingerprint(self.v)))
        binding=AcceptanceBinding(DraftManifest(b1.identity(),rules_fingerprint=b3.rules().id),ProspectiveGate(b1.T-14400000,fingerprint(self.v)))
        identity=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=tr.OLD_B4,historical=HISTORICAL,
            binding=binding.record,rules=b3.rules().record,activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=b1.T-14400000))
        db=sqlite3.connect(self.ledger/'paper.sqlite');db.executescript(SCHEMA)
        db.execute('INSERT INTO identity VALUES(1,?,?)',(fingerprint(identity),encoded(identity).decode()))
        for table in IMMUTABLE:
            for operation in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER no_{operation}_{table} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END")
        state=Replay(binding,b3.rules(),fingerprint(identity))
        # Preserved accepted NONE fixture: historical journal, no fabricated trade.
        item=b1.evidence();data=dict(source=item.record,accepted_ms=b1.T+240000,equity=state.basis)
        result=state.apply('ACCEPT',data)
        event=dict(label=LABEL,parent=fingerprint(identity),request='ACCEPT:'+str(b1.T),kind='ACCEPT',input=data,result=result)
        db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',(1,fingerprint(event),event['request'],event['parent'],encoded(event).decode()))
        for table,rows in state.rows().items():
            for row in rows:db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')',row)
        db.commit();db.close()
        self.before=(self.ledger/'paper.sqlite').read_bytes()
        self.now=b1.T+300000;self.effective=self.now+600000

    def install(self):
        value=self.tr.plan(self.run,root=self.root,effective_ms=self.effective,now_ms=self.now)
        self.op.exclusive(self.control/'transition-b6-v1.json',value)
        return value

    def test_historical_genesis_replay_readonly_unchanged(self):
        value=self.install()
        with patch('paper.transition.time.time_ns',return_value=(self.effective+1)*1000000):
            auth=self.op.load(self.run,self.root)
            from paper.readout import report
            out=report(self.run,'summary',root=self.root,now_ms=self.effective+1)
        self.assertEqual(out['realized_equity'],'1000');self.assertEqual(out['closed_trades'],0)
        self.assertEqual(auth.id,fingerprint(self.v));self.assertEqual(value['checkpoint']['journal_count'],1)
        self.assertEqual((self.ledger/'paper.sqlite').read_bytes(),self.before)

    def test_transition_continues_and_replays_closed_trade_without_rewriting_history(self):
        from types import MappingProxyType
        from paper.source import SourceAdapter
        from paper.market import CandleSource,CandleEvidence
        from paper.ledger import PaperLedger
        from paper.readout import report
        self.install()
        t=b1.T+14400000
        c=Candle('BTCUSDT','15m',t+900000,t+1799999,'100','101','90','100','1')
        item=CandleEvidence(b1.identity(),c,fingerprint(c.record()),t+1800000)
        source=SourceAdapter(self.root,b1.identity());market=CandleSource(self.root,b1.identity())
        snapshot=vs.VerifiedSourceSnapshot(b1.identity(),(b1.evidence(),b2.item(boundary=t)),
            MappingProxyType({('BTCUSDT',c.open_time_ms):item}),b'{}',b'{}',self.root,(),b1.identity().implementation,vs._SEAL)
        with patch('paper.transition.time.time_ns',return_value=(t+2000000)*1000000),patch.object(vs.VerifiedSourceSnapshot,'guard'):
            auth=self.op.load(self.run,self.root)
            ledger=PaperLedger(self.ledger,auth.binding,auth.rules,source,market,verified_source=snapshot,authorization=auth)
            original=ledger._db.execute('SELECT * FROM journal WHERE sequence=1').fetchone()
            try:
                with patch('paper.ledger.time.monotonic_ns',side_effect=[0,1000000]):
                    stamps=iter([t+240000,t+240001]);result=ledger.accept(t,clock=lambda:next(stamps))
                owner=result['reservation']['reservation_id']
                ledger.settle_entry(owner);ledger.process_candle(owner,c.open_time_ms)
                expected=ledger.reconcile().rows()
                self.assertEqual(original,ledger._db.execute('SELECT * FROM journal WHERE sequence=1').fetchone())
            finally:ledger.close()
            reopened=PaperLedger(self.ledger,auth.binding,auth.rules,source,market,verified_source=snapshot,authorization=auth)
            try:self.assertEqual(reopened.reconcile().rows(),expected)
            finally:reopened.close()
            out=report(self.run,'summary',root=self.root,now_ms=t+2000000)
            self.assertEqual(out['closed_trades'],1)

    def test_explicit_writer_exact_approval_exclusive_and_no_ledger_changes(self):
        value=self.tr.plan(self.run,root=self.root,effective_ms=self.effective,now_ms=self.now)
        with patch('paper.transition.time.time_ns',return_value=self.now*1000000):
            with self.assertRaises(ValueError):
                self.tr.bind(self.run,root=self.root,effective_ms=self.effective,now_ms=self.now,approved_fingerprint='0'*64)
            self.assertFalse((self.control/'transition-b6-v1.json').exists())
            self.tr.bind(self.run,root=self.root,effective_ms=self.effective,now_ms=self.now,approved_fingerprint=value['transition_fingerprint'])
            with self.assertRaises(ValueError):
                self.tr.bind(self.run,root=self.root,effective_ms=self.effective,now_ms=self.now,approved_fingerprint=value['transition_fingerprint'])
        self.assertEqual((self.ledger/'paper.sqlite').read_bytes(),self.before)

    def test_missing_transition_fails_closed(self):
        with self.assertRaises(ValueError):self.op.load(self.run,self.root)

    def test_mutated_transition_fields_fail_closed(self):
        value=self.install();path=self.control/'transition-b6-v1.json'
        for key,replacement in [('authorization','0'*64),('new_implementations',{}),('old_implementations',{}),('t0_ms',0),('rules_fingerprint','0'*64)]:
            bad=dict(value);bad[key]=replacement;path.write_bytes(encoded(bad)+b'\n')
            with self.subTest(key=key),self.assertRaises((ValueError,KeyError)):
                self.op.load(self.run,self.root)

    def test_checkpoint_corruption_and_future_effective_fail_closed(self):
        value=self.install()
        with patch('paper.transition.time.time_ns',return_value=(self.effective-1)*1000000),self.assertRaises(ValueError):self.op.load(self.run,self.root)
        db=sqlite3.connect(self.ledger/'paper.sqlite');db.execute('DROP TRIGGER no_UPDATE_journal')
        db.execute("UPDATE journal SET digest=?",('0'*64,));db.commit();db.close()
        with patch('paper.transition.time.time_ns',return_value=(self.effective+1)*1000000),self.assertRaises(ValueError):self.op.load(self.run,self.root)


class HistoricalAndFinancialTests(unittest.TestCase):
    def test_exact_live_run_authorization_and_genesis_reconstructed_without_live_access(self):
        from paper.operations import record,rules_from
        from paper.model import SourceIdentity,DraftManifest,LABEL
        from paper.acceptance import AcceptanceBinding,ProspectiveGate
        from paper.transition import OLD_B4,OLD_B5
        from paper.journal import HISTORICAL,Replay
        rules=rules_from(b1.parse((Path(__file__).parent/'fixtures/b6-reviewed-rules.json').read_bytes().rstrip(b'\n')))
        source=SourceIdentity('a36d985436303dea4d477e76718cefa9ec6d3155df74d98e2850efcd0139bf6b',
            '070ed9e8c4a016bf8d3048f576b60846052af6ef2f102ca8d9e8db47ae3a0b5b',
            'a84880499dafc8f32809641b870827948ebe41845fc6195df59d2758ec006bcd',
            'd7e7a2f461f5060654f13daebb36415d3bd873f1aa9018ae28988f262fdb3b7a',1789171200000,1789977600000)
        v=record('live-0b-001',1790697600000,1790643280990,source,rules)
        v['implementations'].update(b4=OLD_B4,b5=OLD_B5)
        auth=fingerprint(v)
        self.assertEqual(auth,'b513a8397ae2210ec4d83cab6f21004586893b7dec358d4181c2c3d383d2a902')
        binding=AcceptanceBinding(DraftManifest(source,rules_fingerprint=rules.id),ProspectiveGate(v['t0_ms'],auth))
        genesis=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=OLD_B4,historical=HISTORICAL,
            binding=binding.record,rules=rules.record,activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=v['t0_ms']))
        self.assertEqual(fingerprint(genesis),'4c312960fc132b0b2c0d8fe874efb16aa29c65d3393d69af2cc9ea2656210769')
        replay=Replay(binding,rules,fingerprint(genesis))
        self.assertEqual(str(replay.equity),'1000');self.assertEqual(replay.financial_available_ms,v['t0_ms'])

    def test_identical_financial_and_disposition_records_with_detached_source(self):
        from types import MappingProxyType
        from paper.source import SourceAdapter
        from paper.market import CandleSource,CandleEvidence
        from paper.ledger import PaperLedger
        from paper.acceptance import AcceptanceBinding,ProspectiveGate
        from paper.model import DraftManifest
        import test_paper_settlement as b3
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source_id=b1.identity();rules=b3.rules()
            binding=AcceptanceBinding(DraftManifest(source_id,rules_fingerprint=rules.id),ProspectiveGate(b1.T-14400000,'b'*64))
            source=SourceAdapter(root,source_id);market=CandleSource(root,source_id)
            c=Candle('BTCUSDT','15m',b1.T+900000,b1.T+1799999,'100','101','90','100','1')
            item=CandleEvidence(source_id,c,fingerprint(c.record()),b1.T+1800000)
            candles={('BTCUSDT',c.open_time_ms):item};observations=(b2.item(),)
            detached=vs.VerifiedSourceSnapshot(source_id,observations,MappingProxyType(candles),b'{}',b'{}',root,(),source_id.implementation,vs._SEAL)
            with patch.object(source,'snapshot',return_value=observations),patch.object(market,'at',side_effect=lambda s,t:candles.get((s,t))),patch.object(vs.VerifiedSourceSnapshot,'guard'):
                a=PaperLedger(root/'artifacts/forward-paper-execution/b4-a',binding,rules,source,market)
                b=PaperLedger(root/'artifacts/forward-paper-execution/b4-b',binding,rules,source,market,verified_source=detached)
                try:
                    results=[]
                    for ledger in (a,b):
                        with patch('paper.ledger.time.monotonic_ns',side_effect=[0,1000000]):
                            clocks=iter([b1.T+240000,b1.T+240001]);r=ledger.accept(b1.T,clock=lambda:next(clocks))
                        owner=r['reservation']['reservation_id']
                        entry=ledger.settle_entry(owner);closed=ledger.process_candle(owner,c.open_time_ms)
                        results.append((r,entry,closed,ledger.reconcile().rows()))
                    self.assertEqual(results[0],results[1]);self.assertEqual(results[0][2]['status'],'CLOSED')
                    self.assertEqual(len(results[0][3]['equity']),1)
                finally:a.close();b.close()
