"""Pure/mocked isolation regressions. No workers or disk databases executed."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from durable_runner_security import (Registry, Policy, validate_grant, invoke,
                                     INDIVIDUAL_SECONDS, GLOBAL_SECONDS, strict_target)

ROOT=Path('C:/Users/diego/AppData/Local/TradingAgentSyntheticTests')
SUITE=ROOT/('incremental-'+'a'*32)
CASE=ROOT/('incremental-'+'b'*32)
OLD=ROOT/('incremental-'+'c'*32)
REPO=Path('C:/dev/trading-agent')
CMD=['C:/Python/python.exe','-I','-B',str(REPO/'tests/run_incremental_durable.py'),
     '--root',str(ROOT),'--worker']


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.registry=Registry(SUITE);self.binding=self.registry.register_new(CASE)
        self.grant=self.registry.grant(CASE,'corrupt','')

    def test_registered_case(self):self.assertEqual(validate_grant(self.grant,self.binding),self.grant)

    def test_foreign_previous_case_cannot_be_dispatched(self):
        with self.assertRaises(PermissionError):self.registry.grant(OLD,'corrupt','')

    def test_changed_directory_even_with_valid_token(self):
        with self.assertRaises(PermissionError):validate_grant(dict(self.grant,case=OLD.name),self.binding)

    def test_wrong_suite(self):
        with self.assertRaises(PermissionError):validate_grant(dict(self.grant,suite=OLD.name),self.binding)

    def test_marker_alone_or_forged_token_insufficient(self):
        with self.assertRaises(PermissionError):validate_grant(self.grant,{'kind':'synthetic-durable-v1','run':CASE.name})
        with self.assertRaises(PermissionError):validate_grant(dict(self.grant,token='0'*64),self.binding)

    def test_external_path_and_traversal(self):
        for name in ('../'+OLD.name,str(OLD),'incremental-../old'):
            with self.subTest(name=name),self.assertRaises(PermissionError):
                validate_grant(dict(self.grant,case=name),self.binding)

    def test_extra_approval_or_unknown_action(self):
        for value in (dict(self.grant,approved=True),dict(self.grant,action='consume'),dict(self.grant,fault='unknown')):
            with self.assertRaises(PermissionError):validate_grant(value,self.binding)

    def test_old_token_does_not_authorize_new_binding(self):
        another=Registry(SUITE);binding=another.register_new(CASE)
        with self.assertRaises(PermissionError):validate_grant(self.grant,binding)

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(PermissionError):self.registry.register_new(CASE)


class ConfinementTests(unittest.TestCase):
    def setUp(self):
        # Logical policy only; strict filesystem resolution is not bypassed in runtime.
        self.policy=Policy((CASE,),REPO,(Path('C:/Python/Lib'),),resolve=Path)

    def test_assigned_case_read_write(self):
        self.policy('open',(str(CASE/'synthetic.sqlite'),'rb',0))
        self.policy('open',(str(CASE/'continuity.json'),'xb',os.O_CREAT|os.O_WRONLY))

    def test_previous_case_read_write_and_sqlite_rejected(self):
        for path in (OLD/'synthetic.sqlite',ROOT/'unassigned.txt',REPO/'artifacts/live.sqlite'):
            for mode in ('rb','ab','wb'):
                with self.subTest(path=path,mode=mode),self.assertRaises(PermissionError):
                    self.policy('open',(str(path),mode,0))
            with self.assertRaises(PermissionError):self.policy('sqlite3.connect',(str(path),))

    def test_foreign_scans_denied(self):
        for path in (OLD,ROOT):
            with self.assertRaises(PermissionError):self.policy('os.scandir',(str(path),))

    def test_cleanup_parent_case_and_foreign_all_denied(self):
        for path in (CASE,ROOT,OLD):
            for event in ('os.remove','os.rmdir','os.rename','os.chmod'):
                with self.assertRaises(PermissionError):self.policy(event,(str(path),))

    def test_worker_cannot_spawn(self):
        with self.assertRaises(PermissionError):self.policy('subprocess.Popen',(CMD[0],CMD,str(REPO),None))

    def test_full_command_executable_cwd_environment_checked(self):
        self.policy.launch=(CMD[0],CMD.copy(),str(REPO))
        wire=lambda command:subprocess.list2cmdline(command) if os.name=='nt' else command
        self.policy('subprocess.Popen',(CMD[0],wire(CMD),str(REPO),None))
        altered=CMD.copy();altered[-1]='--action'
        external=CMD.copy();external[-2]=str(OLD)
        script=CMD.copy();script[3]=str(OLD/'worker.py')
        for args in ((CMD[0],wire(altered),str(REPO),None),(CMD[0],wire(CMD+['corrupt']),str(REPO),None),
                     ('evil.exe',wire(CMD),str(REPO),None),(None,wire(CMD),str(REPO),None),
                     (CMD[0],wire(CMD),str(OLD),None),(CMD[0],wire(CMD),str(REPO),{}),
                     (CMD[0],wire(external),str(REPO),None),(CMD[0],wire(script),str(REPO),None),
                     (CMD[0],wire([CMD[0]]+CMD[2:]),str(REPO),None)):
            with self.assertRaises(PermissionError):self.policy('subprocess.Popen',args)

    @unittest.skipUnless(os.name=='nt','Actual Windows CPython audit representation')
    def test_real_popen_audit_reaches_launch_boundary_without_starting_worker(self):
        # Exercise installed CPython conversion. Abort at the audit boundary:
        # no CreateProcess, worker, fixture or database is executed.
        class Observed(Exception):pass
        import sys
        command=[sys.executable,'-I','-B',str(REPO/'tests/run_incremental_durable.py'),
                 '--root',str(ROOT),'--worker']
        registry=Registry(SUITE);registry.register_new(CASE)
        grant=registry.grant(CASE,'init','');records=[];observed=[]
        def audit(event,*args):
            if event=='subprocess.Popen':
                self.policy(event,args)
                observed.append(args)
                raise Observed()
        with patch.object(subprocess.sys,'audit',side_effect=audit), \
             patch.object(subprocess._winapi,'CreateProcess') as create:
            with self.assertRaises(Observed):
                invoke(command,grant,self.policy,1000,records.append,clock=lambda:0,run=subprocess.run)
            create.assert_not_called()
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0],(sys.executable,subprocess.list2cmdline(command),str(REPO),None))
        self.assertIsNone(self.policy.launch)

    def test_source_read_allowed_not_source_write_or_data(self):
        self.policy('open',(str(REPO/'paper/journal.py'),'rb',0))
        for path,mode in ((REPO/'paper/journal.py','wb'),(REPO/'paper/secret.json','rb')):
            with self.assertRaises(PermissionError):self.policy('open',(str(path),mode,0))

    def test_unassigned_mkdir_rejected(self):
        with self.assertRaises(PermissionError):self.policy('os.mkdir',(str(OLD),))

    def test_alias_resolution_failure_propagates(self):
        self.policy.resolve=Mock(side_effect=PermissionError('strict alias rejected'))
        with self.assertRaises(PermissionError):self.policy('open',(str(CASE/'alias.sqlite'),'rb',0))


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.policy=Policy((CASE,),REPO,(),resolve=Path);self.records=[]
        registry=Registry(SUITE);registry.register_new(CASE);self.grant=registry.grant(CASE,'apply','')

    def test_timeout_logged_and_fails_no_secret(self):
        child=Mock(side_effect=subprocess.TimeoutExpired(CMD,180,output=b'partial',stderr=b'stuck'))
        with self.assertRaisesRegex(RuntimeError,'timeout'):
            invoke(CMD,self.grant,self.policy,1000,self.records.append,clock=iter([0,180]).__next__,run=child)
        r=self.records[0];self.assertEqual(r['status'],'FAIL');self.assertEqual(r['duration_s'],180)
        self.assertEqual(r['error'],'TimeoutExpired');self.assertEqual(r['stdout'],'partial')
        self.assertNotIn(self.grant['token'],json.dumps(r));self.assertIsNone(self.policy.launch)
        self.assertEqual(child.call_args.kwargs['timeout'],180)

    def test_global_expiry_never_launches(self):
        child=Mock()
        with self.assertRaises(TimeoutError):
            invoke(CMD,self.grant,self.policy,100,self.records.append,clock=lambda:101,run=child)
        child.assert_not_called();self.assertEqual(self.records[0]['status'],'FAIL')

    def test_remaining_global_budget_bounds_wait(self):
        child=Mock(return_value=SimpleNamespace(returncode=0,stdout='{}',stderr=''))
        invoke(CMD,self.grant,self.policy,110,self.records.append,clock=lambda:100,run=child)
        self.assertEqual(child.call_args.kwargs['timeout'],10)
        self.assertEqual(INDIVIDUAL_SECONDS,180);self.assertEqual(GLOBAL_SECONDS,1800)

    def test_success_and_unexpected_exit_logged(self):
        child=Mock(return_value=SimpleNamespace(returncode=0,stdout='{}',stderr=''))
        invoke(CMD,self.grant,self.policy,1000,self.records.append,clock=lambda:0,run=child)
        self.assertEqual(self.records[-1]['status'],'PASS')
        child.return_value.returncode=9
        with self.assertRaises(RuntimeError):
            invoke(CMD,self.grant,self.policy,1000,self.records.append,clock=lambda:0,run=child)
        self.assertEqual(self.records[-1]['status'],'FAIL')

    def test_exception_clears_launch_and_records_failure(self):
        with self.assertRaises(KeyboardInterrupt):
            invoke(CMD,self.grant,self.policy,1000,self.records.append,clock=lambda:0,
                   run=Mock(side_effect=KeyboardInterrupt))
        self.assertIsNone(self.policy.launch);self.assertEqual(self.records[-1]['status'],'FAIL')


if __name__=='__main__':unittest.main()
