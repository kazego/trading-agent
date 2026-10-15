"""Real SQLite verification with simulated owned-path metadata; no disk access."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from test_transactional_bar import fixture
from research.incremental_durable import DurableLedger, Blocked
import test_incremental_consumer_durable as durable_tests


class InitializationTests(unittest.TestCase):
    def setUp(self):
        f,self.v,*_=fixture();self.addCleanup(f.doCleanups)
        self.run=Path('C:/synthetic-unit-only')
        self.extra=[]
        def execute(sql):
            if sql=='PRAGMA database_list':
                rows=[(seq,name,str(self.run/'synthetic.sqlite') if name=='main' else filename)
                      for seq,name,filename in self.v._db.execute(sql).fetchall()]
                return SimpleNamespace(fetchall=lambda:rows+self.extra)
            return self.v._db.execute(sql)
        self.core=SimpleNamespace(db=SimpleNamespace(execute=execute),
            database_path=lambda:self.run/'synthetic.sqlite',
            external=SimpleNamespace(owner=SimpleNamespace(active=True,run=self.run)))

    def validate(self):
        # Only the path read is simulated. All SQLite checks use the real memory DB.
        with patch('research.incremental_durable.checked',side_effect=lambda p:p):
            DurableLedger._validate_database(self.core)

    def test_full_verification_initializes_empty_temp_and_remains_valid(self):
        self.validate()
        self.v._verify()
        rows=self.v._db.execute('PRAGMA database_list').fetchall()
        self.assertIn((1,'temp',''),rows)
        self.assertEqual(self.v._db.execute('SELECT name FROM temp.sqlite_master').fetchall(),[])
        # Exact previous guard rejected this legitimate state.
        self.assertNotEqual(len(rows),1)
        self.validate();self.validate()

    def test_temporary_objects_rejected(self):
        self.v._db.execute('CREATE TEMP TABLE unwanted(value)')
        with self.assertRaisesRegex(Blocked,'Temporary database objects'):self.validate()

    def test_other_database_or_file_backed_temp_rejected(self):
        for row in ((2,'foreign','C:/foreign.sqlite'),(1,'temp','C:/temp.sqlite')):
            with self.subTest(row=row):
                self.extra=[row]
                with self.assertRaisesRegex(Blocked,'Only owned'):self.validate()

    def test_worker_error_preserves_reason_and_traceback(self):
        output=Mock()
        with patch.object(durable_tests.memory_tests.ConsumerIntegration,'prepare',
                          side_effect=Blocked('Only owned synthetic database permitted')):
            durable_tests.worker('not-opened','init',False,None,output)
        result=output.send.call_args.args[0]
        self.assertEqual(result[:3],('FAIL','Blocked','Only owned synthetic database permitted'))
        self.assertIn('Traceback (most recent call last)',result[3])
        self.assertIn('prepare',result[3])
        output.close.assert_called_once()


if __name__=='__main__':unittest.main()
