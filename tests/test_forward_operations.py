"""Operational tests: temporary synthetic stores, mocked clocks/HTTP, no live polling."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from forward import manifest, operations as op, observer, public_feed as feed, readout
from forward.store import Store
from forward.timing import Clock, classify
from forward.model import LABEL, FOUR_HOURS
from research.spec import fingerprint, encoded
from backtest.data import Candle


class TimingTests(unittest.TestCase):
    def test_five_minute_inclusive(self):
        self.assertEqual(classify(1000000,1180000),'ONLINE')
        self.assertEqual(classify(1000000,1300000),'ONLINE')
        self.assertEqual(classify(1000000,1300001),'DELAYED_RECONSTRUCTED')
        with self.assertRaises(ValueError): classify(1000000,999999)

    def test_monotonic_exchange_and_wall_checks(self):
        mono=[1000000000]; wall=[1000000000000]
        c=Clock(1000000,mono[0],mono=lambda:mono[0],wall=lambda:wall[0])
        mono[0]+=1000000000; wall[0]+=1000000000
        self.assertEqual(c.now(),1001000)
        with self.assertRaises(ValueError): c.reconcile(2000000)
        wall[0]+=31000000000
        with self.assertRaises(ValueError): c.now()

    def test_monotonic_backwards_and_slow_time_request(self):
        with self.assertRaises(ValueError): Clock(0,0,mono=lambda:10000000001,wall=lambda:0)
        samples=iter([100,99])
        c=Clock(1000,100,mono=lambda:next(samples),wall=lambda:1000000000)
        with self.assertRaises(ValueError): c.now()


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'run'; self.path.mkdir()
        # Reconstruct frozen metadata without any dependency on ignored artifacts.
        spec=manifest.specification()
        spec['implementation']='482fec92afbbb96d2f95baf418a22dac442798b805d1e7a44ae4805400746511'
        meta=dict(spec=spec,t0_ms=1789156800000,created_ms=1789151370290,
                  history_start_ms=1780516800000,first_observation_ms=1789171200000)
        meta['manifest_fingerprint']=fingerprint(meta)
        assert meta['manifest_fingerprint']==op.LEGACY_MANIFEST
        raw=encoded(meta)+b'\n'
        self.path.joinpath('manifest.json').write_bytes(raw)
        self.meta=json.loads(raw); self.store=Store(self.path,self.meta)
        self.effective=self.meta['first_observation_ms']+2*FOUR_HOURS
        self.now=self.effective-600000

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def plan(self): return op.plan(self.path,effective_ms=self.effective,now_ms=self.now)

    def bind(self):
        p=self.plan(); self.store.close()
        op.bind(self.path,effective_ms=self.effective,now_ms=self.now,
                approved_implementation=p['implementation'],approved_plan=p['operations_fingerprint'])
        return p

    def test_plan_deterministic_and_no_side_effect(self):
        before=self.path.joinpath('manifest.json').read_bytes()
        self.assertEqual(self.plan(),self.plan())
        self.assertFalse(self.path.joinpath('operations-v1.json').exists())
        self.assertEqual(before,self.path.joinpath('manifest.json').read_bytes())

    def test_explicit_binding_and_existing_manifest_preserved(self):
        before=self.path.joinpath('manifest.json').read_bytes()
        p=self.bind()
        self.assertEqual(manifest.load(self.path),self.meta)
        self.assertEqual(op.load_operations(self.path,self.meta),p)
        self.assertEqual(before,self.path.joinpath('manifest.json').read_bytes())
        with self.assertRaises(FileExistsError):
            op.bind(self.path,effective_ms=self.effective,now_ms=self.now,
                    approved_implementation=p['implementation'],approved_plan=p['operations_fingerprint'])

    def test_unbound_run_cannot_execute(self):
        self.assertEqual(manifest.load(self.path,read_only=True),self.meta)
        with self.assertRaises(FileNotFoundError): manifest.load(self.path)

    def test_identity_and_missing_policy_fail(self):
        p=self.bind()
        with patch.object(manifest,'implementation',return_value='mutated'):
            with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)
        p.pop('policy')
        self.path.joinpath('operations-v1.json').write_bytes(encoded(p)+b'\n')
        with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_future_alignment_and_approval_required(self):
        for t in (self.now,self.effective+1):
            with self.assertRaises(ValueError): op.plan(self.path,effective_ms=t,now_ms=self.now)
        self.store.close()
        with self.assertRaises(ValueError): op.bind(self.path,effective_ms=self.effective,now_ms=self.now,
            approved_implementation='wrong',approved_plan='wrong')
        self.assertFalse(self.path.joinpath('operations-v1.json').exists())

    def test_checkpoint_detects_candle_and_event_rewrite(self):
        t=self.meta['history_start_ms']
        self.store.append('BTCUSDT',[Candle('BTCUSDT','15m',t,t+899999,'100','101','99','100','1')],received_ms=self.now)
        self.store.event('START'); p=self.plan()
        self.store.event('STOP')
        op.verify_checkpoint(self.store.db,p['checkpoint'])
        self.store.db.execute('DROP TRIGGER no_UPDATE_events')
        self.store.db.execute("UPDATE events SET payload='{}' WHERE sequence=1"); self.store.db.commit()
        with self.assertRaises(ValueError): op.verify_checkpoint(self.store.db,p['checkpoint'])

    def test_checkpoint_detects_changed_observation(self):
        self.store.observation(dict(label=LABEL,boundary_ms=self.meta['first_observation_ms'],
            manifest_fingerprint=self.meta['manifest_fingerprint'],timing='DELAYED_RECONSTRUCTED'))
        cp=op.checkpoint(self.store.db)
        self.store.db.execute('DROP TRIGGER no_UPDATE_observations')
        self.store.db.execute("UPDATE observations SET digest='changed'"); self.store.db.commit()
        with self.assertRaises(ValueError): op.verify_checkpoint(self.store.db,cp)

    def test_blocked_cannot_bind(self):
        self.store.event('BLOCKED')
        with self.assertRaises(ValueError): self.plan()

    def test_readonly_commands_no_signals_no_network_no_mutation(self):
        before=self.path.joinpath('observer.sqlite').read_bytes()
        with patch.object(Store,'__init__',side_effect=AssertionError('writer')), \
             patch.object(observer,'signal_snapshot',side_effect=AssertionError('signal')), \
             patch.object(feed,'server_time',side_effect=AssertionError('network')):
            for cmd in ('status','latest','summary'):
                r=readout.report(self.path,cmd,now_ms=self.now)
                self.assertEqual(r['health'],'MIGRATION_REQUIRED')
        self.assertEqual(before,self.path.joinpath('observer.sqlite').read_bytes())
        with readout.connect(self.path,self.meta) as db:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM events')

    def test_historical_success_time_not_invented(self):
        self.store.event('STOP')
        r=readout.report(self.path,'status',now_ms=self.now)
        self.assertIsNone(r['last_successful_poll_ms'])
        self.store.event('STOP',observed_ms=self.now)
        self.assertEqual(readout.report(self.path,'status',now_ms=self.now)['last_successful_poll_ms'],self.now)

    def test_temporary_failure_durable_no_latch_or_retry(self):
        self.bind()
        with patch.object(feed,'server_time',side_effect=feed.TemporaryPublicError(retry_after_s=900)) as call:
            r=observer.poll(self.path)
            self.assertEqual(r['health'],'RECOVERABLE'); self.assertEqual(call.call_count,1)
        self.assertFalse(self.path.joinpath('BLOCKED.json').exists())
        with patch.object(feed,'server_time') as call:
            self.assertEqual(observer.poll(self.path)['health'],'BACKOFF'); call.assert_not_called()

    def test_critical_durable_latch_prevents_next_network(self):
        self.bind()
        with patch.object(feed,'server_time',side_effect=ValueError('malformed')) as call:
            with self.assertRaises(ValueError): observer.poll(self.path)
            self.assertEqual(call.call_count,1)
        self.assertTrue(self.path.joinpath('BLOCKED.json').exists())
        with patch.object(feed,'server_time') as call:
            with self.assertRaises(ValueError): observer.poll(self.path)
            call.assert_not_called()

    def test_manifest_corruption_latches_without_network(self):
        self.store.close(); self.path.joinpath('manifest.json').write_text('{}')
        with patch.object(feed,'server_time') as call:
            with self.assertRaises(ValueError): observer.poll(self.path)
            call.assert_not_called()
        self.assertTrue(self.path.joinpath('BLOCKED.json').exists())

    def test_durable_receipt_time_not_decision_ready_time(self):
        p=self.plan(); t=self.effective
        # Seed next cursor without a costly history calculation; storage unit only.
        self.store.manifest=dict(self.meta,first_observation_ms=t)
        clock=MagicMock(); clock.now.side_effect=[t+180000,t+300001]
        self.store.observation(dict(label=LABEL,boundary_ms=t,manifest_fingerprint=self.meta['manifest_fingerprint'],
                                   timing='ONLINE'),clock=clock,operations=p)
        raw=self.store.db.execute('SELECT payload FROM observations').fetchone()[0]
        self.assertEqual(json.loads(raw)['timing'],'UNCONFIRMED')
        self.assertEqual(list(readout.records(self.store.db))[0][0]['effective_timing'],'DELAYED_RECONSTRUCTED')

    def test_commit_receipt_inclusive_and_legacy_tolerance(self):
        p=self.plan(); t=self.meta['first_observation_ms']
        clock=MagicMock(); clock.now.side_effect=[t+180000,t+180001]
        self.store.observation(dict(label=LABEL,boundary_ms=t,manifest_fingerprint=self.meta['manifest_fingerprint'],
                                   timing='ONLINE'),clock=clock,operations=p)
        self.assertEqual(list(readout.records(self.store.db))[0][0]['effective_timing'],'DELAYED_RECONSTRUCTED')
        t+=FOUR_HOURS; p=dict(p,effective_boundary_ms=t)
        clock.now.side_effect=[t+180000,t+300000]
        self.store.observation(dict(label=LABEL,boundary_ms=t,manifest_fingerprint=self.meta['manifest_fingerprint'],
                                   timing='NONE'),clock=clock,operations=p)
        self.assertEqual(list(readout.records(self.store.db))[-1][0]['effective_timing'],'ONLINE')

    def test_crash_after_commit_stays_unconfirmed(self):
        p=self.plan(); t=self.meta['first_observation_ms']
        clock=MagicMock(); clock.now.side_effect=[t+180000,ValueError('clock failed after commit')]
        with self.assertRaises(ValueError):
            self.store.observation(dict(label=LABEL,boundary_ms=t,manifest_fingerprint=self.meta['manifest_fingerprint']),clock=clock,operations=p)
        self.assertEqual(self.store.next_boundary(),t+FOUR_HOURS)
        self.assertEqual(list(readout.records(self.store.db))[0][0]['effective_timing'],'UNCONFIRMED')

    def populate_for_boundary(self):
        self.effective=self.meta['first_observation_ms']; self.now=self.effective-600000
        for symbol in ('BTCUSDT','ETHUSDT','ADAUSDT'):
            start=self.meta['history_start_ms']
            cs=tuple(Candle(symbol,'15m',start+i*900000,start+(i+1)*900000-1,
                           '100','101','99','100','1') for i in range(9616))
            self.store.append(symbol,cs,received_ms=self.effective+180000)
        self.bind()

    def test_mocked_success_receipts_readout_and_duplicate_safe_restart(self):
        self.populate_for_boundary(); stamp=self.effective+180000
        clock=MagicMock(); clock.now.return_value=stamp
        with patch.object(feed,'server_time',return_value=stamp), \
             patch.object(feed,'closed_klines',side_effect=AssertionError('already complete')), \
             patch.object(observer.time,'time_ns',return_value=stamp*1000000), \
             patch('forward.timing.Clock',return_value=clock):
            self.assertEqual(observer.poll(self.path)['observations_written'],1)
            self.assertEqual(observer.poll(self.path)['observations_written'],0)
        latest=readout.report(self.path,'latest',now_ms=stamp)['observation']
        self.assertEqual(latest['effective_timing'],'ONLINE')
        self.assertEqual(latest['timing'],'UNCONFIRMED')  # Original row is never rewritten.
        self.assertEqual([d['signal'] for d in latest['decisions']],['NONE']*3)
        summary=readout.report(self.path,'summary',now_ms=stamp)
        self.assertEqual(summary['scored_observations'],1)
        self.assertEqual(summary['symbol_decisions'],3)
        self.assertEqual(sum(g['ONLINE'] for g in summary['timing_by_policy'].values()),1)

    def test_crash_receipt_recovery_does_not_regenerate_signal(self):
        self.populate_for_boundary(); stamp=self.effective+180000
        clock=MagicMock(); clock.now.return_value=stamp
        original=Store.event
        def interrupted(store,name,**kwargs):
            if name=='RECEIPT': raise KeyboardInterrupt('synthetic crash')
            return original(store,name,**kwargs)
        with patch.object(feed,'server_time',return_value=stamp), \
             patch.object(observer.time,'time_ns',return_value=stamp*1000000), \
             patch('forward.timing.Clock',return_value=clock):
            with patch.object(Store,'event',interrupted):
                with self.assertRaises(KeyboardInterrupt): observer.poll(self.path)
            with patch.object(observer,'signal_snapshot',side_effect=AssertionError('do not regenerate')):
                self.assertEqual(observer.poll(self.path)['observations_written'],0)
        latest=readout.report(self.path,'latest',now_ms=stamp)['observation']
        self.assertEqual(latest['effective_timing'],'DELAYED_RECONSTRUCTED')
        self.assertEqual(latest['persistence_receipt']['code'],'COMMIT_TIME_UNKNOWN')

    def test_database_corruption_has_independent_block_latch(self):
        self.bind()
        self.path.joinpath('observer.sqlite').write_bytes(b'not sqlite')
        with patch.object(feed,'server_time') as call:
            with self.assertRaises(ValueError): observer.poll(self.path)
            call.assert_not_called()
        self.assertTrue(self.path.joinpath('BLOCKED.json').exists())

    def test_safe_read_error_never_creates_latch(self):
        self.store.close(); self.path.joinpath('observer.sqlite').write_bytes(b'bad')
        self.assertEqual(readout.safe_report(self.path,'status',now_ms=self.now)['health'],
                         'INTEGRITY_OR_AUTHORIZATION_FAILURE')
        self.assertFalse(self.path.joinpath('BLOCKED.json').exists())


class TransportTests(unittest.TestCase):
    def test_known_http_recovery_and_backoff(self):
        connection=MagicMock(); response=connection.getresponse.return_value
        response.getheader.return_value='1200'
        for status in (408,429,500,502,503,504):
            response.status=status
            with patch.object(feed.http.client,'HTTPSConnection',return_value=connection):
                with self.assertRaises(feed.TemporaryPublicError) as c: feed.server_time()
                self.assertEqual(c.exception.retry_after_s,1200)

    def test_transport_recoverable_malformed_critical(self):
        with patch.object(feed.http.client,'HTTPSConnection') as c:
            c.return_value.request.side_effect=TimeoutError()
            with self.assertRaises(feed.TemporaryPublicError): feed.server_time()
        c=MagicMock(); c.getresponse.return_value.status=200; c.getresponse.return_value.read.return_value=b'INVALID'
        with patch.object(feed.http.client,'HTTPSConnection',return_value=c):
            with self.assertRaises(ValueError) as error: feed.server_time()
            self.assertNotIsInstance(error.exception,feed.TemporaryPublicError)


class TaskTests(unittest.TestCase):
    def test_generate_only_disabled_task_with_project_runtime(self):
        from datetime import datetime,timezone,timedelta
        future=datetime.now(timezone.utc)+timedelta(days=1)
        stamp=future.replace(minute=3,second=0,microsecond=0).isoformat().replace('+00:00','Z')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'task.xml'
            r=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','RemoteSigned','-File',str(manifest.ROOT/'scripts/live0a-task.ps1'),
                '-StartBoundary',stamp,'-OutputPath',str(path),
                '-PythonwPath',str(manifest.ROOT/'.venv/Scripts/pythonw.exe')],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            raw=path.read_bytes()
            self.assertTrue(raw.startswith(b'\xff\xfe'))
            text=raw.decode('utf-16')
            self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
            self.assertEqual(raw,b'\xff\xfe'+text.encode('utf-16-le'))
            import xml.etree.ElementTree as ET
            self.assertEqual(ET.tostring(ET.fromstring(raw)),ET.tostring(ET.fromstring(text)))
            root=ET.fromstring(raw)
            ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            expected={'Actions/Exec/Command':str(manifest.ROOT/'.venv/Scripts/pythonw.exe'),
                      'Actions/Exec/Arguments':'-m forward poll --run-id live-0a-001 --public-data-only',
                      'Actions/Exec/WorkingDirectory':str(manifest.ROOT),
                      'Triggers/TimeTrigger/StartBoundary':stamp,
                      'Triggers/TimeTrigger/Repetition/Interval':'PT15M',
                      'Principals/Principal/LogonType':'InteractiveToken',
                      'Principals/Principal/RunLevel':'LeastPrivilege',
                      'Settings/Enabled':'false','Settings/MultipleInstancesPolicy':'IgnoreNew',
                      'Settings/ExecutionTimeLimit':'PT10M','Settings/RunOnlyIfIdle':'false'}
            for key,value in expected.items():
                self.assertEqual(root.find('/'.join('t:'+part for part in key.split('/')),ns).text,value)
            self.assertIsNone(root.find('t:Settings/t:RestartOnFailure',ns))
            for value in ('PT15M','PT10M','IgnoreNew','InteractiveToken','<Enabled>false</Enabled>',
                          '<StartWhenAvailable>true</StartWhenAvailable>','.venv\\Scripts\\pythonw.exe'):
                self.assertIn(value,text)
            self.assertNotIn('codex-runtimes',text)
            self.assertIn('NOTHING REGISTERED',r.stdout)
            # Local TaskDefinition parsing only: no registration, activation or task run.
            # Reproduce the old UTF-8 declaration mismatch on the same Unicode string.
            probe=r'''
$ErrorActionPreference='Stop'
$xml=[IO.File]::ReadAllText($args[0])
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$definition=$service.NewTask(0)
$definition.XmlText=$xml
if ($definition.Settings.Enabled) { throw 'Task must remain disabled' }
$old=$xml.Replace('encoding="UTF-16"','encoding="UTF-8"')
$rejected=$false
try { $definition.XmlText=$old } catch { $rejected=$true; Write-Output $_.Exception.Message }
if (-not $rejected) { throw 'Old encoding mismatch did not reproduce' }
Write-Output 'UTF16_ACCEPTED_UTF8_REJECTED_NO_REGISTRATION'
'''
            # Pass the path as a literal through an environment variable to avoid shell quoting.
            import os
            env=dict(os.environ,FORWARD_XML_TEST_PATH=str(path))
            probe=probe.replace('$args[0]','$env:FORWARD_XML_TEST_PATH')
            result=subprocess.run(['powershell','-NoProfile','-Command',probe],
                                  capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('UTF16_ACCEPTED_UTF8_REJECTED_NO_REGISTRATION',result.stdout)
