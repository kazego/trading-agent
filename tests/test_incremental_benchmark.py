"""Safety/measurement checks for the memory-only benchmark driver."""
import unittest
from benchmark_incremental_memory import isolation, stats, Meter, run_size


class BenchmarkTests(unittest.TestCase):
    def test_forbidden_access(self):
        for event,args in [('sqlite3.connect',('C:/forbidden.sqlite',)),
                           ('subprocess.Popen',('python',)),('socket.connect',()),
                           ('open',('C:/dev/trading-agent/artifacts/a','r',0)),
                           ('open',('C:/synthetic.txt','w',1)),('os.remove',('x',))]:
            with self.subTest(event=event),self.assertRaises(PermissionError):isolation(event,args)
        isolation('sqlite3.connect',(':memory:',))

    def test_percentile(self):
        r=stats(list(range(1,31)))
        self.assertEqual(r,dict(n=30,median=15.5,p95=29,maximum=30))

    def test_nested_attribution(self):
        m=Meter()
        with m.span('outer'):
            with m.span('inner'):pass
        self.assertAlmostEqual(m.rows['outer']['inclusive_s'],
            m.rows['outer']['exclusive_s']+m.rows['inner']['inclusive_s'],places=7)

    def test_real_small_fixture_no_skipped_verification(self):
        output=[];r=run_size(69,2,output.append)
        self.assertEqual(r['financial']['journal_count'],71)
        self.assertEqual(r['financial']['audit'],'PASS')
        self.assertEqual(r['total']['n'],2)
        samples=[x for x in output if x['type']=='sample']
        for s in samples:
            self.assertEqual(s['stages']['sqlite_commit']['calls'],2)
            self.assertEqual(s['stages']['continuity_memory_publication']['calls'],1)
            self.assertEqual(s['stages']['financial_reducer']['calls'],1)
            self.assertGreater(s['stages']['projection_validation']['calls'],0)


if __name__=='__main__':unittest.main()
