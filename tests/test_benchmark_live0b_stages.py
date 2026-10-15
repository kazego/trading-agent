"""Safety/unit tests for offline instrumentation only; no operational database."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import benchmark_live0b_stages as h


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.root=h.allocate_root()
        self.addCleanup(h.remove_root,self.root)
        self.guard=h.Isolation(self.root)

    def test_reject_nonfixture_root(self):
        with self.assertRaises(ValueError): h.Isolation(h.REPO)

    def test_sqlite_only_inside_fixture(self):
        self.guard('sqlite3.connect',(str(self.root/'x.sqlite'),))
        self.guard('sqlite3.connect',((self.root/'x.sqlite').as_uri()+'?mode=ro',))
        for value in (':memory:',str(h.REPO/'artifacts/never-open.sqlite'),
                      (h.REPO/'artifacts/never-open.sqlite').as_uri()+'?mode=ro',
                      'file://server/share/x.sqlite'):
            with self.subTest(value=value),self.assertRaises(PermissionError):
                self.guard('sqlite3.connect',(value,))

    def test_production_artifacts_read_is_rejected(self):
        with self.assertRaises(PermissionError):
            self.guard('open',(str(h.REPO/'artifacts/never-read.json'),'r',os.O_RDONLY))

    def test_write_escape_and_traversal_rejected(self):
        for value in (self.root/'..'/'outside.txt',h.REPO/'paper/never-written.py'):
            with self.assertRaises(PermissionError):
                self.guard('open',(str(value),'w',os.O_WRONLY|os.O_CREAT))
        self.guard('open',(str(self.root/'okay.txt'),'w',os.O_WRONLY|os.O_CREAT))

    def test_filesystem_mutations_escape_rejected(self):
        for name in ('os.remove','os.rmdir','os.mkdir','os.chmod'):
            with self.assertRaises(PermissionError):self.guard(name,(str(h.REPO/'never-touch'),))
        for name in ('os.rename','os.link','os.symlink'):
            with self.assertRaises(PermissionError):
                self.guard(name,(str(self.root/'x'),str(h.REPO/'never-touch')))

    def test_network_and_shell_rejected(self):
        for name in ('socket.__new__','socket.connect','socket.getaddrinfo','urllib.Request',
                     'os.system','os.startfile','os.startfile/2','os.exec','os.posix_spawn'):
            with self.assertRaises(PermissionError):self.guard(name,())

    def test_only_git_read_batch_subprocess(self):
        self.guard('subprocess.Popen',('git',['git','cat-file','--batch'],h.REPO,None))
        for command in (['git','fetch'],['cmd','/c','echo'],['python','-m','paper','consume']):
            with self.assertRaises(PermissionError):
                self.guard('subprocess.Popen',(command[0],command,h.REPO,None))
        with self.assertRaises(PermissionError):
            self.guard('subprocess.Popen',('cmd',['git','cat-file','--batch'],h.REPO,None))

    def test_real_audit_hook_blocks_before_any_access(self):
        # Independent process: permanent audit hook cannot contaminate unittest.
        code="""
import sys,socket,sqlite3
from pathlib import Path
sys.path.insert(0,'tests')
from benchmark_live0b_stages import Isolation
root=Path(sys.argv[1]);sys.addaudithook(Isolation(root))
probe=root/'self-test';probe.mkdir()
file=probe/'synthetic.txt';file.write_text('synthetic-only');assert file.read_text()=='synthetic-only'
file.unlink();probe.rmdir()
checks=[lambda:socket.socket(),lambda:sqlite3.connect(str(root.parent/'not-a-fixture.sqlite')),
        lambda:(root.parent/'must-not-exist.txt').write_text('no')]
for action in checks:
 try: action()
 except PermissionError: pass
 else: raise AssertionError('Isolation failed')
print('PASS')
"""
        p=subprocess.run([sys.executable,'-B','-c',code,str(self.root)],cwd=h.REPO,
            capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(p.returncode,0,p.stderr.decode(errors='replace'))
        self.assertEqual(p.stdout.strip(),b'PASS')

    def test_meter_nested_exclusive_no_double_count(self):
        m=h.Meter()
        with patch.object(h.time,'perf_counter_ns',side_effect=[0,10,40,100]):
            with m.span('outer'):
                with m.span('inner'):pass
        self.assertAlmostEqual(m.rows['outer']['inclusive_s'],100/1e9)
        self.assertAlmostEqual(m.rows['outer']['exclusive_s'],70/1e9)
        self.assertAlmostEqual(sum(v['exclusive_s'] for v in m.rows.values()),100/1e9)
        self.assertEqual(m.edges['outer -> inner']['calls'],1)

    def test_meter_unwinds_on_error(self):
        m=h.Meter()
        with self.assertRaises(ValueError),m.span('error'):raise ValueError('synthetic')
        self.assertFalse(m.stack);self.assertEqual(m.rows['error']['calls'],1)

    def test_wrapper_restored_and_return_preserved(self):
        class X:
            @staticmethod
            def f(x):return x+1
        original=X.f;m=h.Meter()
        with ExitStack() as stack:
            m.wrap(X,'f',stack)
            self.assertEqual(X.f(3),4)
        self.assertIs(X.f,original)

    def test_sqlite_methods_and_semantics(self):
        m=h.Meter()
        with ExitStack() as stack:
            h.sqlite_instrumentation(m,stack)
            db=sqlite3.connect(self.root/'fixture.sqlite')
            db.executescript('CREATE TABLE t(x INTEGER);')
            db.executemany('INSERT INTO t VALUES(?)',[(1,),(2,),(3,)])
            db.commit()
            c=db.execute('SELECT x FROM t ORDER BY x')
            self.assertEqual(c.fetchone(),(1,))
            self.assertEqual(c.fetchmany(1),[(2,)])
            self.assertEqual(c.fetchall(),[(3,)])
            self.assertEqual(list(db.execute('SELECT x FROM t ORDER BY x')),[(1,),(2,),(3,)])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            db.execute('INSERT INTO t VALUES(4)');db.rollback()
            with db:db.execute('INSERT INTO t VALUES(5)')
            c.close();db.close()
        for name in ('connect','executescript','executemany','commit','rollback','context_exit','close',
                     'fetchone.ordinary','fetchmany.ordinary','fetchall.ordinary','next.ordinary',
                     'execute.integrity','fetchall.integrity','execute.foreign_keys'):
            self.assertGreater(m.rows['sqlite.'+name]['calls'],0,name)
        db=sqlite3.connect(self.root/'fixture.sqlite')
        self.assertEqual(db.execute('SELECT x FROM t ORDER BY x').fetchall(),[(1,),(2,),(3,),(5,)])
        db.close()

    def test_canonical_blocked_path(self):
        control=self.root/'artifacts/forward-paper-consumer'/h.RUN;control.mkdir(parents=True)
        h.require_unblocked(control)
        (control/'BLOCKED.json').write_text('{}')
        with self.assertRaises(AssertionError):h.require_unblocked(control)
        with self.assertRaises(ValueError):h.require_unblocked(self.root/'forward-paper-control'/h.RUN)

    def test_small_sample_summary_is_explicit(self):
        s=h.sample_summary([4,1,2])
        self.assertEqual((s['n'],s['p50'],s['p95_nearest_rank'],s['maximum']),(3,2,4,4))
        self.assertIn('NOT a robust',s['limitation'])

    def pair(self):
        finance=dict(account='1000',slot=['OPEN','fixture','ADAUSDT'],positions=1,fills=1,trades=0,equity_events=0)
        return dict(before=finance,after=copy.deepcopy(finance),result=dict(state='HEALTHY',transitions=4),
                    state_fingerprint='a'*64,seconds=2)

    def test_exact_equivalence_and_overhead(self):
        a=self.pair();b=copy.deepcopy(a);b['seconds']=2.2
        result=h.compare_pair(a,b)
        self.assertTrue(result['exact_state_equivalence'])
        self.assertAlmostEqual(result['overhead_ratio'],.1)

    def test_financial_drift_never_hidden_by_budget(self):
        a=self.pair();b=copy.deepcopy(a);b['result']['state']='BUDGET_REACHED';b['after']['account']='999'
        with self.assertRaises(AssertionError):h.compare_pair(a,b)

    def test_unexplained_instrumentation_divergence_rejected(self):
        a=self.pair();b=copy.deepcopy(a);b['state_fingerprint']='b'*64
        with self.assertRaises(AssertionError):h.compare_pair(a,b)

    def test_budget_prefix_not_claimed_equivalent(self):
        a=self.pair();b=copy.deepcopy(a);b['state_fingerprint']='b'*64;b['result']['state']='BUDGET_REACHED'
        result=h.compare_pair(a,b)
        self.assertFalse(result['exact_state_equivalence']);self.assertIsNone(result['overhead_ratio'])


if __name__=='__main__':unittest.main()
