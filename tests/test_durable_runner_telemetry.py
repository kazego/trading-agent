"""Regression of the real invoke -> report encoder boundary, no subprocess/IO."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from durable_runner_security import encode_telemetry, invoke, Policy, report_preserving
from research.spec import encoded, fingerprint


class TelemetryTests(unittest.TestCase):
    def test_original_worker_error_survives_broken_encoder(self):
        child=Mock(return_value=SimpleNamespace(returncode=23,stdout='',stderr='original worker error'))
        with self.assertRaisesRegex(RuntimeError,'Unexpected worker exit') as caught:
            self.execute(child,encoded)
        notes='\n'.join(caught.exception.__notes__)
        self.assertIn('original worker error',notes)
        self.assertIn('floats are forbidden',notes)

    def test_timeout_survives_unwritable_report(self):
        child=Mock(side_effect=subprocess.TimeoutExpired(['python'],.125,stderr=b'original timeout'))
        with self.assertRaisesRegex(RuntimeError,'Worker timeout') as caught:
            self.execute(child,Mock(side_effect=PermissionError('report denied')))
        self.assertIn('original timeout','\n'.join(caught.exception.__notes__))
        self.assertIn('report denied','\n'.join(caught.exception.__notes__))

    def test_fail_and_completion_reports_preserve_original(self):
        original=RuntimeError('original failure')
        try:
            raise original
        except RuntimeError:
            report_preserving(original,Mock(side_effect=OSError('disk full')),'FAIL.json')
            report_preserving(original,Mock(side_effect=PermissionError('denied')),'Completed-cases report')
        self.assertEqual(str(original),'original failure');self.assertEqual(len(original.__notes__),2)

    def test_report_failure_without_primary_is_not_silenced(self):
        with self.assertRaisesRegex(OSError,'disk full'):
            report_preserving(None,Mock(side_effect=OSError('disk full')),'report')

    def execute(self,run,encoder):
        records=[]
        self.records=records
        policy=Policy((),Path('C:/synthetic'),(),resolve=Path)
        grant={'case':'synthetic','action':'init','fault':''}
        times=iter([10.0,10.125,10.25])
        return invoke(['python','worker'],grant,policy,100,
            lambda row:records.append(encoder(row)),clock=times.__next__,run=run)

    def test_exact_previous_finally_failure(self):
        child=Mock(return_value=SimpleNamespace(returncode=0,stdout='{}',stderr=''))
        with self.assertRaisesRegex(ValueError,'Unsupported value; floats are forbidden'):
            self.execute(child,encoded)

    def test_duration_report_round_trip(self):
        child=Mock(return_value=SimpleNamespace(returncode=0,stdout='{}',stderr=''))
        self.assertEqual(self.execute(child,encode_telemetry),{})
        result=json.loads(self.records[0]);self.assertEqual(result['duration_s'],.25)
        self.assertEqual(result['status'],'PASS')

    def test_worker_failure_not_masked_by_logging(self):
        child=Mock(return_value=SimpleNamespace(returncode=23,stdout='',stderr='original worker error'))
        with self.assertRaisesRegex(RuntimeError,'Unexpected worker exit'):
            self.execute(child,encode_telemetry)
        result=json.loads(self.records[0]);self.assertEqual(result['exit'],23)
        self.assertEqual(result['stderr'],'original worker error');self.assertEqual(result['status'],'FAIL')

    def test_fractional_timeout_report(self):
        child=Mock(side_effect=subprocess.TimeoutExpired(['python'],.125,output=b'partial'))
        with self.assertRaisesRegex(RuntimeError,'Worker timeout'):
            self.execute(child,encode_telemetry)
        result=json.loads(self.records[0]);self.assertEqual(result['timeout_s'],.125)
        self.assertEqual(result['error'],'TimeoutExpired')

    def test_nonfinite_and_negative_timings_rejected(self):
        for value in (float('nan'),float('inf'),float('-inf'),-.01):
            with self.subTest(value=value),self.assertRaises(ValueError):
                encode_telemetry({'duration_s':value})

    def test_telemetry_cannot_encode_financial_fields_or_nested_payloads(self):
        for record in ({'equity':1.5},{'state':{'equity':'1000'}},{'stdout':{'amount':1.5}},
                       {'exit':1.5},{'command':[1.5]}):
            with self.assertRaises(ValueError):encode_telemetry(record)

    def test_financial_serializers_remain_strict(self):
        for record in ({'equity':1000.0},{'journal':{'input':{'price':.25}}},
                       {'state':{'equity':1000.0}},{'at':10.125}):
            for serializer in (encoded,fingerprint):
                with self.assertRaisesRegex(ValueError,'floats are forbidden'):serializer(record)
        self.assertEqual(encoded({'equity':'1000','sequence':1}),b'{"equity":"1000","sequence":1}')


if __name__=='__main__':unittest.main()
