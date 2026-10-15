"""B5 tests use temporary synthetic authorization/evidence only."""
import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from dataclasses import FrozenInstanceError

import test_paper_foundation as b1
import test_paper_acceptance as b2
import test_paper_settlement as b3
from paper.operations import propose,activate,load,paths,Authorization,implementation,policy,rules_from
from paper.consumer import consume
from paper.readout import report,connect
from paper.source import SourceAdapter
from paper.market import CandleSource,CandleEvidence
from paper.model import parse
from backtest.data import Candle
from research.spec import encoded,fingerprint
from runtime import DatabaseLock
from errors import RuntimeOwnershipError

T=b1.T


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.run='live-0b-synthetic'
        self.source_dir=self.root/'artifacts'/'forward-paper'/'live-0a-001'
        self.source_dir.mkdir(parents=True)
        self.auth=propose(self.run,T-14400000,b1.identity(),b3.rules(),now_ms=T-28800000)
        self.now=T+240000; self.items=[b2.item()]; self.candles={}
        self.addCleanup(patch.stopall)
        # These retained B5 financial fixtures deliberately exercise historical v2.
        # The production entry point has no fallback; new bound-loader integration
        # is covered separately by test_artifact_binding.
        class HistoricalFixtureCapability:
            def __init__(inner,auth):inner.auth=auth
            def __getattr__(inner,name):return getattr(inner.auth,name)
            @property
            def ledger_authorization(inner):
                from paper.transition import TransitionAuthorization
                return inner.auth if type(inner.auth) is TransitionAuthorization else None
            def guard(inner):
                if load(self.run,self.root).payload!=inner.auth.payload:raise ValueError('Changed fixture activation')
            def accept(inner,ledger,boundary):return ledger.accept(boundary,clock=lambda:self.now)
            def open(inner,verified):
                from paper.ledger import PaperLedger
                _,directory=paths(self.run,self.root)
                ledger=PaperLedger(directory,inner.binding,inner.rules,
                    SourceAdapter(self.source_dir,inner.source),CandleSource(self.source_dir,inner.source),
                    verified_source=verified,authorization=inner.ledger_authorization)
                return inner,ledger
        patch('paper.consumer.prepare_execution',side_effect=lambda run,root:HistoricalFixtureCapability(load(run,root))).start()
        patch.object(SourceAdapter,'snapshot',side_effect=lambda:tuple(self.items)).start()
        patch.object(CandleSource,'at',side_effect=lambda s,t:self.candles.get((s,t))).start()
        from paper.consumer import source_progress
        self.progress_function=source_progress
        patch('paper.consumer.source_progress',side_effect=lambda source:{s:dict(first_open_ms=None,
            last_open_ms=max((t for symbol,t in self.candles if symbol==s),default=None),count=0)
            for s in ('BTCUSDT','ETHUSDT','ADAUSDT')}).start()
        patch('paper.ledger.time.monotonic_ns',return_value=0).start()
        from paper.verified_source import VerifiedSourceSnapshot, _SEAL
        from types import MappingProxyType
        def captured(directory, expected, **kwargs):
            return VerifiedSourceSnapshot(expected,tuple(self.items),MappingProxyType(dict(self.candles)),
                encoded(__import__('paper.consumer',fromlist=['source_progress']).source_progress(None)),
                b'{}',self.source_dir,(),expected.implementation,_SEAL)
        patch('paper.consumer.capture',side_effect=captured).start()
        patch.object(VerifiedSourceSnapshot,'guard').start()


    def arm(self):
        activate(self.auth,now_ms=self.auth.record['authorized_ms'],root=self.root,
                 clock=lambda:self.auth.record['authorized_ms'])
    def run_once(self): return consume(self.run,root=self.root,clock=lambda:self.now)
    def out(self,command='status'): return report(self.run,command,root=self.root,now_ms=self.now)
    def bar(self,stamp=T+900000,high='101',low='99'):
        c=Candle('BTCUSDT','15m',stamp,stamp+899999,'100',high,low,'100','1')
        self.candles[('BTCUSDT',stamp)]=CandleEvidence(b1.identity(),c,fingerprint(c.record()),stamp+900000)
    def snapshot(self): return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_unactivated_refuses_without_files(self):
        before=self.snapshot()
        with self.assertRaises(ValueError): self.run_once()
        self.assertEqual(before,self.snapshot())

    def test_future_and_aligned_t0_required(self):
        for stamp in (T-28800000,T-28800001,T+1):
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):
                propose(self.run,stamp,b1.identity(),b3.rules(),now_ms=T-28800000)

    def test_immutable_deterministic_authorization(self):
        self.assertEqual(self.auth.id,Authorization(self.auth.payload).id)
        with self.assertRaises(FrozenInstanceError): self.auth.payload=b'{}'
        self.assertEqual(self.auth.record['initial_equity_available_ms'],T-14400000)

    def test_proposal_has_no_side_effects(self):
        self.assertFalse(paths(self.run,self.root)[0].exists())

    def test_activation_expired_rejected(self):
        with self.assertRaises(ValueError): activate(self.auth,now_ms=T,root=self.root)
        self.assertFalse(paths(self.run,self.root)[0].exists())

    def test_expiration_during_authorization_no_files(self):
        with self.assertRaises(ValueError):
            activate(self.auth,now_ms=self.auth.record['authorized_ms'],root=self.root,clock=lambda:T)
        self.assertFalse(paths(self.run,self.root)[0].exists())

    def test_activation_default_clock_is_fresh(self):
        with patch('paper.operations.time.time_ns',return_value=T*1000000):
            with self.assertRaises(ValueError):
                activate(self.auth,now_ms=self.auth.record['authorized_ms'],root=self.root)
        self.assertFalse(paths(self.run,self.root)[0].exists())

    def test_persisted_t0_mutation_rejected_before_ledger(self):
        self.arm();control,ledger=paths(self.run,self.root)
        value=parse((control/'activation.json').read_bytes()[:-1])
        value['authorization']['t0_ms']=T+14400000
        value['authorization']['initial_equity_available_ms']=T+14400000
        (control/'activation.json').write_bytes(encoded(value)+b'\n')
        with self.assertRaisesRegex(ValueError,'fingerprint mismatch'):load(self.run,self.root)
        self.assertFalse(ledger.exists())

    def test_activation_exclusive(self):
        self.arm()
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(load(self.run,self.root).id,self.auth.id)

    def test_source_rules_config_bound(self):
        for field in ('source','rules','config','implementations','acceptance_policy'):
            v=self.auth.record; v[field]={}
            with self.subTest(field=field),self.assertRaises((ValueError,KeyError,TypeError)):
                Authorization(encoded(v))

    def test_rule_roundtrip_and_missing_rules(self):
        self.assertEqual(rules_from(b3.rules().record).id,b3.rules().id)
        v=b3.rules().record;v['rules'].pop()
        with self.assertRaises(ValueError): rules_from(v)

    def test_before_activation_no_ledger(self):
        self.arm();self.now=T-14400001
        self.assertEqual(self.run_once()['state'],'WAITING_FOR_T0')
        self.assertFalse(paths(self.run,self.root)[1].exists())

    def test_exact_t0_and_pre_t0_never_accepted(self):
        self.auth=propose(self.run,T,b1.identity(),b3.rules(),now_ms=T-14400000)
        self.arm(); self.items=[b2.item()]
        self.run_once()
        self.assertIsNone(self.out('latest')['decision'])
        self.assertEqual(self.out('summary')['accepted'],0)

    def test_first_post_t0_accepted_once_restart(self):
        self.arm();self.run_once(); first=self.out('latest')['decision']
        self.run_once()
        self.assertEqual(self.out('latest')['decision'],first)
        self.assertEqual(self.out('summary')['accepted'],1)

    def test_old_online_not_retroactive(self):
        self.arm();self.now=T+900000;self.run_once()
        self.assertEqual(self.out('summary')['accepted'],0)
        self.assertIn('MISSED_ACCEPTANCE_DEADLINE',self.out('summary')['dispositions'])

    def test_delayed_missed(self):
        self.arm();self.items=[b2.item(timing='DELAYED_RECONSTRUCTED')];self.now=T+700000
        self.run_once();self.assertEqual(self.out('summary')['dispositions']['MISSED_DELAYED_SIGNAL'],3)

    def test_chronological_multiple_missed(self):
        self.arm();self.items=[b2.item(),b2.item(boundary=T+14400000)]
        self.now=T+14400000+900000;self.run_once()
        self.assertEqual(self.out('latest')['decision']['boundary_ms'],T+14400000)
        self.assertEqual(self.out('summary')['accepted'],0)

    def test_unsorted_and_duplicate_source_block(self):
        self.arm();self.items=[b2.item(),b2.item()]
        with self.assertRaises(ValueError): self.run_once()
        self.assertTrue((paths(self.run,self.root)[0]/'BLOCKED.json').exists())

    def test_source_gap_blocks(self):
        self.arm();self.items=[b2.item(boundary=T+14400000)]
        with self.assertRaises(ValueError):self.run_once()

    def test_empty_source_waits(self):
        self.arm();self.items=[]
        self.assertEqual(self.run_once()['state'],'WAITING')
        self.assertEqual(self.out('summary')['accepted'],0)

    def test_overlap_refused_no_block(self):
        self.arm();control,_=paths(self.run,self.root); lock=DatabaseLock(control/'consumer');lock.acquire()
        try:
            with self.assertRaises(RuntimeOwnershipError):self.run_once()
        finally:lock.release()
        self.assertFalse((control/'BLOCKED.json').exists())

    def test_operational_hardlink_refused_without_write(self):
        import os
        self.arm();control,_=paths(self.run,self.root)
        other=self.root/'unrelated';other.write_bytes(b'unchanged')
        os.link(other,control/'operations.sqlite')
        with self.assertRaises(ValueError):self.run_once()
        self.assertEqual(other.read_bytes(),b'unchanged')

    def test_tampered_activation_blocks_before_source(self):
        self.arm();control,_=paths(self.run,self.root)
        (control/'activation.json').write_bytes(b'{}\n')
        with patch.object(SourceAdapter,'snapshot',side_effect=AssertionError('Must not read source')):
            with self.assertRaises(ValueError):self.run_once()
        self.assertTrue((control/'BLOCKED.json').exists())

    def test_blocked_prevents_reads(self):
        self.arm();control,_=paths(self.run,self.root);(control/'BLOCKED.json').write_bytes(b'{}\n')
        with patch.object(SourceAdapter,'snapshot',side_effect=AssertionError('Must not read source')):
            with self.assertRaises(ValueError):self.run_once()

    def test_reconciliation_blocks_corruption(self):
        self.arm();self.run_once();_,path=paths(self.run,self.root)
        with closing(sqlite3.connect(path/'paper.sqlite')) as db:
            db.execute("UPDATE account SET amount='999'");db.commit()
        with self.assertRaises(ValueError): self.run_once()
        self.assertTrue((path/'BLOCKED.json').exists())

    def test_missing_candle_retains_slot(self):
        self.arm();self.run_once();self.now=T+1800000;self.run_once()
        self.assertEqual(self.out()['slot'][0],'PENDING')

    def test_execution_gap_blocks(self):
        self.arm();self.run_once();self.bar(T+1800000);self.now=T+2700000
        with self.assertRaises(ValueError):self.run_once()

    def test_financial_roundtrip_and_reports_read_only(self):
        self.arm();self.run_once();self.bar(high='120');self.now=T+1800000
        self.run_once(); before=self.snapshot()
        for command in ('status','latest','trades','missed','summary'): self.out(command)
        self.assertEqual(before,self.snapshot())
        summary=self.out('summary');self.assertEqual(summary['closed_trades'],1)
        self.assertEqual(summary['realized_equity'],str(1000+__import__('decimal').Decimal(summary['net_pnl'])))
        self.assertIsNone(summary['mtm_drawdown'])
        self.run_once();again=self.out('summary')
        for key in ('last_attempt','last_success'):summary.pop(key);again.pop(key)
        self.assertEqual(again,summary)

    def test_open_mtm_uses_persisted_only(self):
        self.arm();self.run_once();self.bar();self.now=T+1800000;self.run_once()
        result=self.out();self.assertEqual(result['slot'][0],'OPEN');self.assertIsNotNone(result['mtm'])
        self.assertEqual(result['mtm']['open_ms'],T+900000)

    def test_reports_cannot_write(self):
        self.arm();self.run_once();_,path=paths(self.run,self.root)
        with connect(path/'paper.sqlite') as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute("UPDATE account SET amount='1'")

    def test_reporting_does_not_read_source(self):
        self.arm();self.run_once()
        with patch.object(SourceAdapter,'snapshot',side_effect=AssertionError('Source read')):
            for command in ('status','latest','trades','missed','summary'):self.out(command)

    def test_policy_bounded_and_no_network_imports(self):
        self.assertEqual(policy()['scheduler']['interval'],'PT1M')
        for name in ('operations','consumer','readout','__main__'):
            tree=ast.parse((Path(__file__).parents[1]/'paper'/f'{name}.py').read_text())
            imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            imports += [a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
            self.assertFalse(any(x.split('.')[0] in ('requests','urllib','http','socket','binance') for x in imports))

    def test_scheduler_static_configuration(self):
        text=(Path(__file__).parents[1]/'scripts/live0b-task.ps1').read_text()
        for required in ('UTF-16','[Text.Encoding]::Unicode','PT1M','PT2M','IgnoreNew','InteractiveToken','LeastPrivilege',
                         '<Enabled>false</Enabled>','-m paper consume','RegisterDisabled','CreateNew',"'.venv\\Scripts\\pythonw.exe'"):
            self.assertIn(required,text)
        self.assertNotIn('Enable-ScheduledTask',text)
        self.assertNotIn('Start-ScheduledTask',text)

    def test_xml_bytes_and_task_configuration(self):
        import subprocess
        import xml.etree.ElementTree as ET
        path=self.root/'task.xml'
        script=Path(__file__).parents[1]/'scripts/live0b-task.ps1'
        result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
            '-RunId',self.run,'-StartBoundary','2099-01-01T00:00:00Z','-OutputPath',str(path),
            '-PythonwPath',str(script.parents[1]/'.venv/Scripts/pythonw.exe')],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        raw=path.read_bytes();self.assertTrue(raw.startswith(b'\xff\xfe'))
        tree=ET.fromstring(raw);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for query,expected in [('t:Settings/t:Enabled','false'),('t:Settings/t:MultipleInstancesPolicy','IgnoreNew'),
                ('t:Settings/t:ExecutionTimeLimit','PT2M'),('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval','PT1M'),
                ('t:Principals/t:Principal/t:LogonType','InteractiveToken'),
                ('t:Actions/t:Exec/t:Arguments','-m paper consume --run-id '+self.run)]:
            self.assertEqual(tree.find(query,ns).text,expected)
        self.assertEqual(tree.find('t:Actions/t:Exec/t:WorkingDirectory',ns).text,str(script.parents[1]))

    def test_scheduler_rejects_cache_runtime(self):
        import subprocess
        script=Path(__file__).parents[1]/'scripts/live0b-task.ps1'
        result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
            '-RunId',self.run,'-StartBoundary','2099-01-01T00:00:00Z','-OutputPath',str(self.root/'bad.xml'),
            '-PythonwPath',r'C:\codex-runtimes\pythonw.exe'],capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertFalse((self.root/'bad.xml').exists())

    def test_bounded_invocation_resumes(self):
        self.arm();self.items=[b2.item(),b2.item(boundary=T+14400000)]
        self.now=T+14400000+900000
        p=policy();p['max_transitions']=1
        with patch('paper.consumer.policy',return_value=p):
            self.assertEqual(self.run_once()['state'],'BUDGET_REACHED')
            self.assertEqual(self.out('latest')['decision']['boundary_ms'],T)
            self.run_once()
        self.assertEqual(self.out('latest')['decision']['boundary_ms'],T+14400000)

    def test_current_boundary_not_settled_before_arbitration(self):
        self.arm();self.run_once();self.bar();self.now=T+1800000;self.run_once()
        # No later execution candle exists: unresolved exposure must block priority.
        self.items.append(b2.item(boundary=T+14400000));self.now=T+14400000+240000
        self.run_once()
        self.assertEqual([d['reason'] for d in self.out('latest')['decision']['dispositions']],['BLOCKED_GLOBAL_SLOT']*3)

    def test_interrupted_confirmation_not_counted_as_accepted(self):
        from paper.ledger import PaperLedger
        class Crash(BaseException): pass
        def fault(self,point):
            if point=='CONFIRM.before': raise Crash()
        self.arm()
        with patch.object(PaperLedger,'_hit',fault):
            with self.assertRaises(Crash):self.run_once()
        summary=self.out('summary')
        self.assertEqual(summary['selected_reservations'],1)
        self.assertEqual(summary['accepted'],0)
        self.assertEqual(self.out()['health'],'UNRESOLVED_ACCEPTANCE')
        self.run_once()
        self.assertEqual(self.out('summary')['accepted'],0)
        self.assertEqual(self.out()['slot'][0],'PENDING')

    def test_blocked_warning_dominates_unconfirmed_health(self):
        from paper.ledger import PaperLedger
        class Crash(BaseException): pass
        def fault(self,point):
            if point=='CONFIRM.before': raise Crash()
        self.arm()
        with patch.object(PaperLedger,'_hit',fault):
            with self.assertRaises(Crash):self.run_once()
        control,_=paths(self.run,self.root)
        (control/'BLOCKED.json').write_bytes(b'{}\n')
        self.assertEqual(self.out()['health'],'BLOCKED')

    def test_missing_existing_ledger_never_recreated(self):
        self.arm();self.run_once();control,path=paths(self.run,self.root)
        (path/'paper.sqlite').unlink()  # Synthetic temporary ledger only.
        view=self.out()
        self.assertEqual(view['health'],'INCOMPLETE_OR_MISSING_LEDGER')
        self.assertIsNone(view['realized_equity'])
        with self.assertRaisesRegex(ValueError,'ledger is missing'):self.run_once()
        self.assertFalse((path/'paper.sqlite').exists())
        self.assertTrue((control/'BLOCKED.json').exists())

    def test_truncated_existing_ledger_never_reinitialized(self):
        self.arm();self.run_once();control,path=paths(self.run,self.root)
        (path/'paper.sqlite').write_bytes(b'')
        with self.assertRaises(sqlite3.DatabaseError):self.run_once()
        self.assertEqual((path/'paper.sqlite').read_bytes(),b'')
        self.assertTrue((control/'BLOCKED.json').exists())

    def test_no_future_receipt_consumption(self):
        self.arm();self.run_once();self.bar();self.now=T+1799999
        self.assertEqual(self.run_once()['transitions'],0)

    def test_stale_source_visible(self):
        self.arm();self.run_once();self.assertTrue(self.out()['source_stale'])

    def test_read_only_blocked_does_not_repair(self):
        self.arm();control,_=paths(self.run,self.root)
        (control/'BLOCKED.json').write_bytes(b'{}\n');before=self.snapshot()
        self.assertEqual(self.out()['health'],'BLOCKED');self.assertEqual(before,self.snapshot())

    def test_malformed_rules_no_float(self):
        v=b3.rules().record;v['rules'][0]['tick_size']=0.1
        with self.assertRaises(ValueError):rules_from(v)

    def test_source_progress_real_readonly_rows_and_gap(self):
        # Exercise the original helper against temporary SQLite, not LIVE-0A.
        from forward import readout as source_readout
        path=self.root/'source.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE candles(symbol TEXT,open_ms INTEGER)')
            db.executemany('INSERT INTO candles VALUES(?,?)',[('BTCUSDT',T),('BTCUSDT',T+900000)])
            db.commit()
        adapter=SourceAdapter(self.source_dir,b1.identity())
        original=self.progress_function
        before=path.read_bytes()
        with patch('forward.manifest.load',return_value={}),patch.object(source_readout,'connect',side_effect=lambda *args:connect(path)):
            self.assertEqual(original(adapter)['BTCUSDT']['count'],2)
            self.assertEqual(before,path.read_bytes())
            with closing(sqlite3.connect(path)) as db:
                db.execute('UPDATE candles SET open_ms=? WHERE open_ms=?',(T+1800000,T+900000));db.commit()
            with self.assertRaises(ValueError):original(adapter)


if __name__=='__main__': unittest.main()
