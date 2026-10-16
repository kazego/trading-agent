"""Explicit incremental constructor for an authorized new cycle or synthetic tests.

No CLI registration or automatic backend switch. ExecutionPlan.open can select
this class explicitly for owned, already initialized ledgers only. No bootstrap
is performed by the consumer. Deployment ownership is binding-scoped.
"""
from copy import deepcopy
import time
import sqlite3
from contextlib import closing

from paper.ledger import PaperLedger
from paper.authorization import ClockCapability
from paper.operations import paths
from paper.acceptance import timestamp
from paper.model import parse
from research.incremental_durable import DurableLedger, DurableExternal, Ownership, Blocked, root_path, checked


def validate_synthetic_owner(owner,root):
    if type(owner) is not Ownership or not owner.active or owner.run!=root:
        raise Blocked('Incremental backend requires exclusive synthetic ownership; operational backend unavailable')
    root_path(owner.run.parent)
    checked(owner.run)


def validate_incremental_owner(owner,root,cap=None):
    from research.new_cycle_authorization import ExecutionOwnership
    if type(owner) is ExecutionOwnership:
        owner.guard(root,cap)
    else:validate_synthetic_owner(owner,root)


class _ConsumerCore(DurableLedger):
    def database_path(self):
        return self.v.directory/'paper.sqlite'


class IncrementalConsumerLedger(PaperLedger):
    def __init__(self,*args,synthetic_owner,initialize=False,**kwargs):
        self.core=None;self.integration_times={};self._cycle_lease=None;self._execution_owner=synthetic_owner
        cap=kwargs.get('authorization')
        if type(cap) is not ClockCapability:
            raise Blocked('Exact owned synthetic authorization root required')
        validate_incremental_owner(synthetic_owner,cap.root,cap)
        if type(initialize) is not bool:raise Blocked('Explicit synthetic initialization flag required')
        # Ownership was obtained through the existing authorized synthetic root.
        if paths(cap.record['run_id'],cap.root)[1]!=args[0]:
            raise Blocked('Synthetic ledger path mismatch')
        if not initialize:
            # Before PaperLedger can create, latch or materialize anything.
            path=checked(args[0]/'paper.sqlite')
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as probe:
                if not probe.execute("SELECT 1 FROM sqlite_master WHERE name='incremental_state' AND type='table'").fetchone():
                    raise Blocked('Incremental ledger not initialized; explicit migration required')
        from paper.binding import NEW_CYCLE
        from research.new_cycle_authorization import ExecutionOwnership
        if cap.proposal['kind']==NEW_CYCLE and type(synthetic_owner) is not ExecutionOwnership:
            from paper.operations import consumer_exclusion
            self._cycle_lease=consumer_exclusion(cap.root)
            self._cycle_lease.acquire()
        start=time.monotonic()
        try:
            super().__init__(*args,**kwargs)
        except BaseException:
            if self._cycle_lease is not None:self._cycle_lease.release()
            raise
        try:
            self.integration_times['authorized_open_seconds']=time.monotonic()-start
            # One complete startup verification also supplies the historical batch
            # index. Compact financial state alone intentionally omits old batches.
            start=time.monotonic()
            initial=PaperLedger._verify(self)
            self._sources(initial)
            self._consumer_batches=deepcopy(initial.batches)
            self.integration_times['startup_verify_seconds']=time.monotonic()-start
            external=DurableExternal(synthetic_owner)
            if cap.proposal['kind']==NEW_CYCLE:
                from research.spec import fingerprint
                initial_records,_=external._chain('continuity')
                if not initial_records or fingerprint(initial_records[0])!=cap.proposal['initial_state']:
                    raise Blocked('Bound initial continuity mismatch')
            core=_ConsumerCore(self,external,authorization_id=self._authorization_identity(),
                authorize=self._authorization_identity,evidence=self._incremental_evidence)
            self.core=core
            start=time.monotonic()
            exists=self._db.execute("SELECT 1 FROM sqlite_master WHERE name='incremental_state'").fetchone()
            if exists:
                value=core.recover()
                if cap.proposal['kind']==NEW_CYCLE and value['sequence']==0:
                    from research.spec import fingerprint
                    if fingerprint(value)!=cap.proposal['initial_state']:
                        raise Blocked('Bound initial continuity mismatch')
            elif initialize:core.bootstrap()  # Explicit synthetic fixture setup only.
            else:raise Blocked('Incremental state disappeared; never recreate')
            self.integration_times['bootstrap_or_recovery_seconds']=time.monotonic()-start
        except BaseException:
            self.close();raise

    def close(self):
        try:super().close()
        finally:
            if self._cycle_lease is not None:self._cycle_lease.release()

    def _authorization_identity(self):
        from paper.binding import NEW_CYCLE
        from research.new_cycle_authorization import ExecutionOwnership
        if type(self._execution_owner) is ExecutionOwnership:
            self._execution_owner.guard(self.authorization.root,self.authorization)
        elif self.authorization.proposal['kind']==NEW_CYCLE and (self._cycle_lease is None or self._cycle_lease._file is None):
            raise Blocked('New-cycle global ownership lost')
        self.authorization.guard()
        # Genesis continuity already binds the fresh activation. Binding grants
        # execution without rewriting that immutable initial continuity record.
        return self.authorization.id if self.authorization.proposal['kind']==NEW_CYCLE else self.authorization.bound_id

    def _incremental_evidence(self,kind,data):
        self.authorization.guard()
        if kind=='ACCEPT':
            items=self._sources(self.core._state(self.core._load()))
            boundary=data['source']['observation']['boundary_ms']
            if boundary not in items or items[boundary].record!=data['source']:
                raise Blocked('Acceptance evidence changed')
            self.authorization.check(self.binding,data['accepted_ms'],boundary)
        elif kind=='CONFIRM_V3':
            if data['binding_evidence']!=self.authorization.envelope:
                raise Blocked('Confirmation authorization changed')
        elif kind in ('ENTRY','BAR'):
            value=data['candle']
            if value is not None:
                c=value['candle'];item=self._candle(c['symbol'],c['open_time_ms'])
                if item is None or item.record!=value:raise Blocked('Execution evidence changed')
        else:raise Blocked('Unsupported incremental consumer event')

    def reconcile(self):
        if self.core is None:return super().reconcile()
        with self._guard():
            self.core._validate_database()
            state=self.core._state(self.core.recover())
            self._consumer_batches.update(deepcopy(state.batches))
            state.batches=deepcopy(self._consumer_batches)
            return state

    def _append(self,kind,data,request,*,bar_open_ms=None):
        if self.core is None:return super()._append(kind,data,request,bar_open_ms=bar_open_ms)
        with self.core.external.lock, self._guard():
            state=self.reconcile();self._sources(state)
            if bar_open_ms is not None:
                if kind!='BAR' or request is not None or set(data)!={'owner'}:
                    raise ValueError('Invalid transactional BAR request')
                timestamp(bar_open_ms)
                owner=data['owner'];request='BAR:'+owner+':'+str(bar_open_ms)
                old=self._db.execute('SELECT payload FROM journal WHERE request=?',(request,)).fetchone()
                if old:
                    data=parse(old[0].encode())['input']
                else:
                    r=state.reservations[owner]['reservation'];p=state.positions[owner]
                    expected=p.last_processed_ms+900000 if p.last_processed_ms is not None else r['entry_boundary_ms']
                    if bar_open_ms!=expected:raise ValueError('Must process exact next candle')
                    item=self._candle(r['symbol'],bar_open_ms)
                    data=dict(owner=owner,candle=item.record if item else None)
                    if item is None:request='WAIT:'+request
            if request!=self.core._retry_request(state,kind,data):
                raise ValueError('Incorrect deterministic request identity')
            return self.core.apply(kind,data)
