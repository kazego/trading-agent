"""Supervised synthetic audit sessions. Private pipe, no imported result authority.

Not a defense against code executing inside the owner process or an administrator.
No writer lock is held during audit. Receipt registry is volatile and owner-local.
"""
import multiprocessing
import secrets
import time
from pathlib import Path

from research.spec import encoded, fingerprint
from paper.model import parse
from research.incremental_ledger import Blocked
from research.incremental_snapshot import verify_sealed
from research.incremental_snapshot_audit import audit_sealed


def _worker(directory,anchor,binding,rules,session,output):
    try:
        passes=[]
        result=audit_sealed(Path(directory),anchor,binding,rules,on_replay=lambda:passes.append(1))
        output.send_bytes(encoded(dict(session=session,result=result,replay_count=len(passes))))
    finally:output.close()


def validate_response(raw,session,manifest,anchor):
    value=parse(raw)
    if (encoded(value)!=raw or set(value)!={'session','result','replay_count'}
            or value['session']!=session or type(value['replay_count']) is not int or value['replay_count']!=1):
        raise Blocked('Wrong/partial audit session response')
    r=value['result']
    expected={'version','authority','status','snapshot_id','manifest_hash','file_sha256',
              'captured_at_ns','reducer','replay_ns','total_ns','sequence','head','genesis',
              'state_hash','equity','financial_rows','projections_hash'}
    if not isinstance(r,dict) or set(r)!=expected:raise Blocked('Audit response schema')
    checks=dict(version='synthetic-snapshot-audit-result-v1',authority='NONE',status='PASS',
        snapshot_id=manifest['snapshot_id'],manifest_hash=fingerprint(manifest),
        file_sha256=manifest['file_sha256'],captured_at_ns=manifest['captured_at_ns'],
        reducer=anchor['reducer'],sequence=anchor['sequence'],head=anchor['state']['head'],
        genesis=anchor['state']['identity'],state_hash=fingerprint(anchor['state']),equity=anchor['state']['equity'])
    if any(r[k]!=v for k,v in checks.items()):raise Blocked('Audit session identity mismatch')
    if any(type(r[k]) is not int or r[k]<0 for k in ('replay_ns','total_ns')):
        raise Blocked('Invalid audit timings')
    return r


def _receive(receive,deadline):
    """Windows PeekNamedPipe may raise 109 where ReadFile reports EOF.

    Only normalize that poll condition; recv_bytes must still prove a complete
    message or EOF. Other errors and partial frames are never success.
    """
    try:
        ready=receive.poll(max(0,deadline-time.monotonic()))
    except OSError as exc:
        if getattr(exc,'winerror',None)!=109:raise
        ready=True
    if not ready:raise Blocked('Auditor channel timeout')
    return receive.recv_bytes(64*1024*1024)


def _completed_response(receive,child,deadline,session,manifest,anchor):
    try:raw=_receive(receive,deadline)
    except EOFError as exc:raise Blocked('Auditor disconnected before complete response') from exc
    result=validate_response(raw,session,manifest,anchor)
    try:_receive(receive,deadline)
    except EOFError:pass
    else:raise Blocked('Duplicate auditor response')
    child.join(max(0,deadline-time.monotonic()))
    if child.exitcode!=0:raise Blocked('Abnormal auditor termination')
    return result


class Sessions:
    def __init__(self):self.__receipts={}

    def audit(self,directory,anchor,binding,rules,*,timeout_s=7200,prepare=None):
        # Detached immutable wire inputs; callers cannot mutate an in-flight anchor.
        anchor=parse(encoded(anchor));directory=Path(directory)
        manifest=verify_sealed(directory,anchor)
        session=secrets.token_hex(32)
        context=prepare(session,manifest) if prepare is not None else None
        ctx=multiprocessing.get_context('spawn');receive,send=ctx.Pipe(duplex=False)
        child=ctx.Process(target=_worker,args=(str(directory),anchor,binding,rules,session,send))
        deadline=time.monotonic()+timeout_s
        try:
            child.start();send.close()
            result=_completed_response(receive,child,deadline,session,manifest,anchor)
            if verify_sealed(directory,anchor)!=manifest:raise Blocked('Snapshot changed')
            from research.incremental_certificate import candidate
            receipt=object()
            self.__receipts[receipt]=(directory,anchor,manifest,candidate(result,anchor),context)
            return receipt
        finally:
            if child.pid is not None and child.is_alive():child.terminate();child.join(5)
            receive.close();send.close()

    def consume(self,receipt,*,precheck=None,with_context=False):
        if type(receipt) is not object or receipt not in self.__receipts:
            raise Blocked('Unknown, foreign or consumed audit receipt')
        directory,anchor,manifest,proposed,context=self.__receipts.pop(receipt)
        if precheck is not None:precheck(proposed)
        # No replay. Hash/integrity recheck retained until OS file protection exists.
        if verify_sealed(directory,anchor)!=manifest:raise Blocked('Snapshot changed before installation')
        return (proposed,context) if with_context else proposed
