"""Experimental constructor for synthetic consume() integration tests only.

No CLI registration or production backend switch. The test selects this class
at ExecutionPlan.open; the real loader still verifies and issues its capability.
"""
from copy import deepcopy
import time

from paper.ledger import PaperLedger
from paper.authorization import ClockCapability
from paper.operations import paths
from paper.acceptance import timestamp
from paper.model import parse
from research.incremental_durable import DurableLedger, DurableExternal, Ownership, Blocked


class _ConsumerCore(DurableLedger):
    def database_path(self):
        return self.v.directory/'paper.sqlite'


class IncrementalConsumerLedger(PaperLedger):
    def __init__(self,*args,synthetic_owner,**kwargs):
        self.core=None;self.integration_times={}
        cap=kwargs.get('authorization')
        if (type(synthetic_owner) is not Ownership or type(cap) is not ClockCapability
                or cap.root!=synthetic_owner.run or not synthetic_owner.active):
            raise Blocked('Exact owned synthetic authorization root required')
        # Ownership was obtained through the existing authorized synthetic root.
        if paths(cap.record['run_id'],cap.root)[1]!=args[0]:
            raise Blocked('Synthetic ledger path mismatch')
        start=time.monotonic()
        super().__init__(*args,**kwargs)
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
            core=_ConsumerCore(self,external,authorization_id=cap.bound_id,
                authorize=self._authorization_identity,evidence=self._incremental_evidence)
            self.core=core
            start=time.monotonic()
            exists=self._db.execute("SELECT 1 FROM sqlite_master WHERE name='incremental_state'").fetchone()
            if exists:core.recover()
            else:core.bootstrap()  # Synthetic fixture only; no operational migration.
            self.integration_times['bootstrap_or_recovery_seconds']=time.monotonic()-start
        except BaseException:
            self.close();raise

    def _authorization_identity(self):
        self.authorization.guard()
        return self.authorization.bound_id

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
