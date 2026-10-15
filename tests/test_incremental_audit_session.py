"""Session protocol unit tests and direct manual Windows integration."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parents[1]))
sys.path.insert(0,str(Path(__file__).resolve(strict=True).parent))
from research.incremental_audit_session import Sessions, validate_response, _worker, _completed_response
from research.incremental_ledger import Blocked
from research.spec import encoded, fingerprint
from test_incremental_certificate import Integration, SealedIntegration


class ProtocolTests(Integration):
    def test_response_fields_and_fabricated_receipt(self):
        r=dict(self.result,version='synthetic-snapshot-audit-result-v1',replay_ns=1,total_ns=2)
        manifest=dict(snapshot_id=r['snapshot_id'],file_sha256=r['file_sha256'],captured_at_ns=100)
        r['manifest_hash']=fingerprint(manifest)
        value=dict(session='expected',result=r,replay_count=1)
        self.assertEqual(encoded(validate_response(encoded(value),'expected',manifest,self.anchor)),encoded(r))
        for modified in (dict(value,session='foreign'),dict(value,replay_count=2),
                         dict(value,result=dict(r,head='bad')),dict(value,extra=True)):
            with self.assertRaises((Blocked,ValueError)):
                validate_response(encoded(modified),'expected',manifest,self.anchor)
        with self.assertRaises((Blocked,ValueError)):
            validate_response(encoded(value)[:-5],'expected',manifest,self.anchor)
        for forged in (value,object(),None):
            with self.assertRaises(Blocked):self.core.install_receipt(forged)

    def test_worker_exactly_one_audit(self):
        class Output:
            def __init__(self):self.rows=[];self.closed=False
            def send_bytes(self,raw):self.rows.append(raw)
            def close(self):self.closed=True
        output=Output()
        def fake(*args,**kwargs):kwargs['on_replay']();return self.result
        with patch('research.incremental_audit_session.audit_sealed',side_effect=fake) as auditor:
            _worker('.',self.anchor,self.v.binding,self.v.rules,'session',output)
        self.assertEqual(auditor.call_count,1);self.assertEqual(len(output.rows),1);self.assertTrue(output.closed)


class WindowsSessionTests(SealedIntegration):
    def test_real_sealed_audit_and_install(self):
        from research.incremental_certificate import CertifiedSyntheticLedger
        def supervised(core,directory,anchor,proposal):
            receipt=core.supervise_audit(directory,anchor,timeout_s=60)
            # The parent must never run financial replay during installation.
            with (patch('research.incremental_snapshot_audit.replay_snapshot',side_effect=AssertionError('Duplicate replay')),
                  patch('research.incremental_certificate.audit_sealed',side_effect=AssertionError('Duplicate audit'))):
                result=core.install_receipt(receipt)
                with self.assertRaises(Blocked):core.install_receipt(receipt)
            return result
        with patch.object(CertifiedSyntheticLedger,'certify',supervised):
            super().test_real_sealed_audit_and_install()


class PipeLifecycleTests(unittest.TestCase):
    def broken(self,code=109):
        error=BrokenPipeError('synthetic Windows pipe closure');error.winerror=code
        return error

    def finish(self,polls,reads,exitcode=0):
        receive=Mock();receive.poll.side_effect=polls;receive.recv_bytes.side_effect=reads
        child=Mock();child.exitcode=exitcode
        with patch('research.incremental_audit_session.validate_response',return_value={'validated':True}) as validate:
            result=_completed_response(receive,child,float('inf'),'session',{}, {})
            validate.assert_called_once_with(b'complete','session',{}, {})
        child.join.assert_called_once()
        return result

    def test_normal_eof(self):
        self.assertEqual(self.finish([True,True],[b'complete',EOFError()]),{'validated':True})

    def test_windows_109_after_complete_response(self):
        self.assertEqual(self.finish([True,self.broken()],[b'complete',EOFError()]),{'validated':True})

    def test_premature_disconnect(self):
        for polls in ([True],[self.broken()]):
            with self.subTest(polls=polls),self.assertRaisesRegex(Blocked,'before complete'):
                self.finish(polls,[EOFError()])

    def test_nonzero_or_unfinished_worker_after_valid_response(self):
        for code in (1,23,None):
            with self.subTest(code=code),self.assertRaisesRegex(Blocked,'Abnormal'):
                self.finish([True,self.broken()],[b'complete',EOFError()],code)

    def test_duplicate_and_timeout(self):
        for polls,reads in (([True,True],[b'complete',b'extra']),([True,False],[b'complete'])):
            with self.assertRaises(Blocked):self.finish(polls,reads)

    def test_unrelated_error_and_incomplete_frame(self):
        for polls,reads in (([True,self.broken(5)],[b'complete']),([True],[OSError('partial frame')]),
                            ([True,self.broken()],[b'complete',self.broken()])):
            with self.assertRaises(OSError):self.finish(polls,reads)

    def test_invalid_response_never_joins_as_success(self):
        receive=Mock();receive.poll.return_value=True;receive.recv_bytes.return_value=b'partial'
        child=Mock()
        with self.assertRaises((ValueError,Blocked)):
            _completed_response(receive,child,float('inf'),'session',{}, {})
        child.join.assert_not_called()


if __name__=='__main__':unittest.main()
