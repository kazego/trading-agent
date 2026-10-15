"""Experimental in-process prefix certification. No production authority.

Certificates are data. Only a successful independent snapshot replay plus
continuity checks installs a certificate in this invocation-local controller.
Restart defaults to blocked: re-audit/revalidate, never trust a saved hash alone.
Durable certificate publication is deliberately not implemented.
"""
from copy import deepcopy
import time

from research.incremental_ledger import SyntheticLedger, Blocked
from research.incremental_snapshot_audit import audit_sealed
from research.spec import encoded, fingerprint
from paper.model import parse


def candidate(result, anchor):
    if (result['status']!='PASS' or result['authority']!='NONE'
            or result['sequence']!=anchor['sequence'] or result['head']!=anchor['state']['head']
            or result['state_hash']!=fingerprint(anchor['state'])
            or result['reducer']!=anchor['reducer']):
        raise Blocked('Audit result mismatch')
    value=dict(version='synthetic-prefix-candidate-v1',authority='NONE',
               snapshot=result['snapshot_id'],manifest=result['manifest_hash'],file_sha256=result['file_sha256'],
               captured_at_ns=result['captured_at_ns'],anchor=deepcopy(anchor))
    encoded(value)
    return value


def freshness(cert, sequence, now_ns):
    captured=cert['captured_at_ns'];n=cert['anchor']['sequence']
    if any(type(x) is not int for x in (captured,n,sequence,now_ns)):
        raise Blocked('Exact integer time/sequence required')
    if captured<0 or n<0 or now_ns<captured or now_ns-captured>=86400*1_000_000_000 or not 0<=sequence-n<1000:
        raise Blocked('Prefix certificate expired or future')


def continuity(core, cert, *, cursor=None):
    value=core._load();anchor=cert['anchor'];n=anchor['sequence']
    if value!=core.external.anchor:raise Blocked('Unpublished/divergent continuation')
    for key in ('version','reducer','authorization','schema','policy'):
        if anchor[key]!=value[key]:raise Blocked('Identity drift')
    if anchor['state']['identity']!=value['state']['identity']:raise Blocked('Genesis drift')
    if cursor is None:
        records=[parse(raw) for raw in core.external.records]
        matches=[i for i,r in enumerate(records) if r==anchor]
        if len(matches)!=1:raise Blocked('Missing/ambiguous external prefix')
        suffix=records[matches[0]:]
    else:
        token,session,snapshot=cursor
        suffix=core.external.cursor_suffix(token,anchor,core,session,snapshot)
    if suffix[-1]!=value:raise Blocked('External tail mismatch')
    for item in suffix:
        if any(item[key]!=anchor[key] for key in ('version','reducer','authorization','schema','policy')):
            raise Blocked('External identity drift')
    head=anchor['state']['head']
    if core.db.execute('SELECT digest FROM journal WHERE sequence=?',(n,)).fetchone()!=(head,):
        raise Blocked('Certified prefix missing/forked')
    rows=core.db.execute('SELECT sequence,digest,request,parent,payload FROM journal WHERE sequence>? ORDER BY sequence',(n,)).fetchall()
    if len(rows)!=value['sequence']-n or len(suffix)!=len(rows)+1:raise Blocked('Truncated continuation')
    for offset,row in enumerate(rows,1):
        seq,digest,request,parent,raw=row;event=parse(raw.encode())
        if (seq!=n+offset or parent!=head or event['parent']!=parent or event['request']!=request
                or fingerprint(event)!=digest or suffix[offset]['sequence']!=seq
                or suffix[offset]['state']['head']!=digest):raise Blocked('Forked continuation')
        head=digest
    return value


class CertifiedSyntheticLedger(SyntheticLedger):
    """Memory-only prototype, explicit independent audit installation required."""
    def __init__(self,*args,clock_ns=time.time_ns,**kwargs):
        super().__init__(*args,**kwargs)
        self._certificate=None;self._certificate_ids=set();self.clock_ns=clock_ns
        from research.incremental_audit_session import Sessions
        self._sessions=Sessions()
        if hasattr(self.external,'invalidate_cursors'):self.external.invalidate_cursors()

    def apply(self,*args,**kwargs):
        try:return super().apply(*args,**kwargs)
        except BaseException:
            self.external.invalidate_cursors();raise

    def supervise_audit(self,directory,anchor,*,timeout_s=7200):
        from research.incremental_ledger import ExternalMemory
        if type(self.external) is not ExternalMemory:raise Blocked('Memory cursor backend required')
        anchor=parse(encoded(anchor))
        def prepare(session,manifest):
            snapshot=fingerprint(manifest)
            cert={'anchor':anchor,'captured_at_ns':manifest['captured_at_ns']}
            with self.external.lock:
                self._guard()
                if self.db.in_transaction:raise Blocked('Caller transaction')
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    freshness(cert,self._load()['sequence'],self.clock_ns())
                    continuity(self,cert)  # Full initial verification, BEFORE auditor starts.
                    token=self.external.issue_cursor(anchor,self,session,snapshot)
                    self.db.commit()
                    return token,session,snapshot
                except BaseException:
                    self.db.rollback();self.external.invalidate_cursors();raise
        try:
            return self._sessions.audit(directory,anchor,self.v.binding,self.v.rules,
                                        timeout_s=timeout_s,prepare=prepare)
        except BaseException:
            self.external.invalidate_cursors();raise

    def install_receipt(self,receipt,*,fault=lambda stage:None):
        def precheck(cert):
            with self.external.lock:
                freshness(cert,self._load()['sequence'],self.clock_ns())
        try:
            verified,cursor=self._sessions.consume(receipt,precheck=precheck,with_context=True)
            if cursor is None:raise Blocked('Missing session continuity cursor')
            return self._install(verified,fault=fault,cursor=cursor)
        except BaseException:
            self.external.invalidate_cursors();raise

    def certify(self, directory, anchor, proposed, *, fault=lambda stage:None):
        # Never elevate a caller's matching hash/result to authority.
        result=audit_sealed(directory,anchor,self.v.binding,self.v.rules)
        verified=candidate(result,anchor)
        if verified!=proposed:raise Blocked('Candidate differs from verified snapshot')
        return self._install(verified,fault=fault)

    def _install(self,verified,*,fault=lambda stage:None,cursor=None):
        with self.external.lock:
            self._guard()
            if self.db.in_transaction:raise Blocked('Caller transaction')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                freshness(verified,self._load()['sequence'],self.clock_ns())
                value=continuity(self,verified,cursor=cursor)
                freshness(verified,value['sequence'],self.clock_ns())
                identity=fingerprint(verified)
                if identity in self._certificate_ids:raise Blocked('Duplicate certificate')
                if self._certificate is not None and (
                        verified['anchor']['sequence']<=self._certificate['anchor']['sequence']
                        or verified['captured_at_ns']<self._certificate['captured_at_ns']):
                    raise Blocked('Out of order certificate')
                fault('before_install')
                # Read transaction commits before installing volatile authority.
                self.db.commit()
                fault('after_commit')
                freshness(verified,value['sequence'],self.clock_ns())
                self._certificate=deepcopy(verified);self._certificate_ids.add(identity)
            except BaseException:
                self.db.rollback();raise
        return identity

    def _audit_guard(self,value):
        if self._certificate is None:raise Blocked('No installed prefix certificate')
        freshness(self._certificate,value['sequence'],self.clock_ns())
        # Full suffix verification belongs to installation, not each candle.
        n=self._certificate['anchor']['sequence']
        if self.db.execute('SELECT digest FROM journal WHERE sequence=?',(n,)).fetchone()!= (
                self._certificate['anchor']['state']['head'],):raise Blocked('Certified prefix changed')
