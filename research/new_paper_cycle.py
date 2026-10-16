"""Explicit new-cycle genesis initialization. Never runs a consumer.

Uses the existing activation writer, ledger genesis, bootstrap and ownership.
The resulting activation is NOT a clock-v3 execution capability. Execution remains blocked until a separately reviewed execution binding exists.
"""
from paper.operations import Authorization, activate, paths, exclusive
from paper.ledger import PaperLedger
from paper.source import SourceAdapter
from paper.market import CandleSource
from research.incremental_durable import DurableExternal, Blocked
from research.incremental_full_consumer import validate_synthetic_owner, _ConsumerCore
from runtime import DatabaseLock


def initialize(auth, *, reviewed_fingerprint, owner, clock):
    """Administrative one-shot initialization, not a runtime approval API.

    Caller must own a NEW synthetic run or an administrative initialization lease. No import of an old ledger,
    current positions or old authorizations is accepted by this interface.
    Partial initialization is retained and blocks retries; never auto-repaired.
    """
    if type(auth) is not Authorization:raise Blocked('Explicit new-cycle authorization required')
    auth=Authorization(auth.payload)
    if auth.id!=reviewed_fingerprint:raise Blocked('Exact reviewed initialization fingerprint required')
    administrative=type(owner) is AdministrativeOwnership
    if administrative:
        owner.guard('initialize',auth.record['run_id'])
        root=owner.root
    else:
        validate_synthetic_owner(owner,getattr(owner,'run',None))
        root=owner.run
    control,directory=paths(auth.record['run_id'],root)
    if control.exists() or directory.exists() or (root/'cycle-initialization.json').exists():
        raise Blocked('Cycle already exists or initialization interrupted')
    external=DurableExternal(owner)
    if external.anchor is not None or external.audit is not None or external.blocked:
        raise Blocked('New cycle requires unused continuity store')
    # Shared administrative exclusion across synthetic cycles; no process killed.
    lock=None if administrative else DatabaseLock(root.parent/'paper-cycle-initialization')
    if lock is not None:lock.acquire()
    ledger=None
    try:
        if administrative:owner.guard('initialize',auth.record['run_id'])
        now=clock()
        # Existing source validation and final future-T0 check occur in activate.
        activate(auth,now_ms=now,root=root,clock=clock)
        exclusive(root/'cycle-initialization.json',dict(
            operation='INITIALIZE_NEW_PAPER_ONLY',run_id=auth.record['run_id'],
            authorization=auth.id,initial_equity='1000',historical_import='FORBIDDEN',
            execution_authority='NONE'))
        source_path=root/'artifacts'/'forward-paper'/auth.record['source_run']
        ledger=PaperLedger(directory,auth.binding,auth.rules,
            SourceAdapter(source_path,auth.source),CandleSource(source_path,auth.source))
        def guard():
            from paper.operations import load
            if administrative:owner.guard('initialize',auth.record['run_id'])
            loaded=load(auth.record['run_id'],root)
            if type(loaded) is not Authorization or loaded.payload!=auth.payload:
                raise Blocked('New-cycle activation changed')
            return auth.id
        def no_execution(kind,data):
            raise Blocked('Initialization does not authorize financial execution')
        core=_ConsumerCore(ledger,external,authorization_id=auth.id,authorize=guard,
                           evidence=no_execution,clock=lambda:clock()/1000)
        state=ledger._verify()
        if state.batches or state.reservations or state.positions or state.equity!=1000 or state.slot!=('EMPTY',None,None):
            raise Blocked('New cycle is not empty genesis')
        core.bootstrap()
        core.recover()
        core.audit()
        return dict(run_id=auth.record['run_id'],authorization=auth.id,
                    genesis=ledger.identity_id,sequence=0,equity='1000',slot='EMPTY',
                    t0_ms=auth.record['t0_ms'],first_eligible_boundary_ms=auth.record['t0_ms']+14400000,
                    execution='BLOCKED_NO_NEW_CYCLE_EXECUTION_AUTHORIZATION')
    finally:
        try:
            if ledger is not None:ledger.close()
        finally:
            if lock is not None:lock.release()


class AdministrativeOwnership:
    """Administrative-only lease. Cannot be used by the execution loader.

    Only the canonical deployment root; tests substitute ROOT within synthetic
    fixtures. Existing OS locks are reused, never unlinked or forcibly released.
    """
    def __init__(self,run_id,root,purpose):
        from pathlib import Path
        from paper import operations,binding
        from research.new_cycle_authorization import validated_root
        self.active=False;self._locks=[]
        if purpose not in ('initialize','proposal','bind'):raise Blocked('Unknown administrative purpose')
        if Path(root)!=operations.ROOT:raise Blocked('Canonical deployment root required')
        self.root=validated_root(root);self.run_id=run_id;self.purpose=purpose
        control,directory=paths(run_id,self.root)
        self.run=directory  # DurableExternal remains scoped to this ledger only.
        global_lock=operations.consumer_exclusion(self.root)
        try:
            global_lock.acquire();self._locks.append(global_lock)
            if purpose=='initialize':
                review=self.root/'artifacts'/'forward-paper-review'/run_id
                for path in (control,directory,self.root/'cycle-initialization.json',review):
                    binding.safe(path)
                    if path.exists():raise Blocked('Cycle identity exists or initialization interrupted')
            else:
                from research.new_cycle_authorization import activation
                activation(run_id,self.root)  # Fresh activation and exact init marker only.
                if not (directory/'paper.sqlite').is_file():raise Blocked('Missing initialized ledger')
                for path in (control/'consumer',directory/'paper.sqlite',directory/'ownership'):
                    lock=DatabaseLock(binding.safe(path));binding.safe(lock.path)
                    lock.acquire();self._locks.append(lock)
            self.active=True
        except BaseException:
            self.close();raise

    def guard(self,purpose,run_id):
        if (not self.active or self.purpose!=purpose or self.run_id!=run_id
                or not self._locks or any(lock._file is None or lock._file.closed for lock in self._locks)):
            raise Blocked('Administrative ownership/purpose mismatch')

    def close(self):
        self.active=False
        for lock in reversed(self._locks):lock.release()
        self._locks=[]

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
