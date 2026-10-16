"""Explicit administrative commands only. Never imported by the consumer."""
import argparse
from pathlib import Path
from contextlib import contextmanager,nullcontext
from paper import binding as b
from paper.authorization import (chain,unchanged,verify_successor,verify_abandonment,
    state_at,check_prefix,_historical_auth,load_execution,load_abandonment,now_ms)
from paper.operations import ROOT,paths,exclusive
from paper.model import LABEL
from research.spec import encoded,fingerprint
from runtime import DatabaseLock


def implementation():return b.source_identity(('paper_admin.py','paper/binding.py','paper/authorization.py'))


@contextmanager
def ownership(run_id,root,*,ledger=True):
    control,directory=paths(run_id,root)
    locks=[DatabaseLock(b.safe(control/'consumer'))]
    if ledger:locks.append(DatabaseLock(b.safe(directory/'paper.sqlite')))
    acquired=[]
    try:
        for lock in locks:
            b.safe(lock.path);lock.acquire();acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):lock.release()


def successor_proposal(run_id,root,effective_ms):
    from paper.clock_v3 import policy,implementation as clock_id
    v,t,current,_,_=chain(run_id,root)
    if effective_ms<now_ms()+300000:raise ValueError('Fresh future review boundary required')
    check_prefix(run_id,root,v,t['checkpoint'])
    _,cp=state_at(run_id,root,v)
    clock=dict(version='paper-clock-v3-authorization-v1',label=LABEL,binding=_historical_auth(v).binding.id,
        effective_ms=effective_ms,policy=fingerprint(policy()),implementation=clock_id())
    p=dict(kind=b.SUCCESSOR,label=LABEL,schema=fingerprint(b.schema(b.SUCCESSOR)),run_id=run_id,
        activation=fingerprint(v),b6=t['transition_fingerprint'],implementations=current['implementations'],
        previous_implementations=t['new_implementations'],effective_ms=effective_ms,clock=clock,
        checkpoint=cp,unchanged=unchanged(v))
    verify_successor(p,v,t,current);return p


def abandonment_proposal(run_id,root,owner,resolved_ms):
    from paper.recovery import checkpoint,mechanism
    successor=load_execution(run_id,root);state,_=state_at(run_id,root,successor.record)
    if state.reservations[owner]['state']!='BLOCKED_CLOCK' or owner in state.resolutions:
        raise ValueError('Only unresolved BLOCKED_CLOCK is reviewable')
    state._not_before('ABANDONMENT_PROPOSAL',resolved_ms)
    if resolved_ms>now_ms():raise ValueError('Future resolution claim')
    r=dict(version='paper-abandonment-authorization-v1',label=LABEL,mechanism=fingerprint(mechanism()),
        checkpoint=checkpoint(state,owner),resolved_ms=resolved_ms,reason='OPERATOR_AUTHORIZED_NONEXECUTABLE_CLOCK_INCIDENT')
    p=dict(kind=b.ABANDONMENT,label=LABEL,schema=fingerprint(b.schema(b.ABANDONMENT)),run_id=run_id,
        activation=successor.id,b6=successor.proposal['b6'],implementations=successor.proposal['implementations'],
        successor=successor.bound_id,authorization=r,operation='BLOCKED_CLOCK->ABANDONED',
        constraints=['NO_ENTRY','NO_REPLAY','NO_FALLBACK','NO_FINANCIAL_EFFECT'])
    verify_abandonment(p,successor);return p


def write_proposal(p,root):
    owner=p['authorization']['checkpoint']['owner'] if p['kind']==b.ABANDONMENT else None
    path,_=b.locations(p['run_id'],root,p['kind'],reviewed=fingerprint(p),owner=owner)
    b.validate(p);path.parent.mkdir(parents=True,exist_ok=True);exclusive(path,p);return path


def bind(run_id,kind,reviewed_fingerprint,*,root=ROOT,owner=None,synthetic_owner=None,administrative_owner=None):
    if kind==b.NEW_CYCLE:
        from research.new_paper_cycle import AdministrativeOwnership
        if synthetic_owner is None and administrative_owner is None:
            with AdministrativeOwnership(run_id,root,'bind') as lease:
                return bind(run_id,kind,reviewed_fingerprint,root=root,administrative_owner=lease)
        if administrative_owner is not None:
            if type(administrative_owner) is not AdministrativeOwnership or synthetic_owner is not None or administrative_owner.root!=Path(root):
                raise ValueError('Exact administrative binding owner required')
            administrative_owner.guard('bind',run_id)
        else:
            from research.incremental_full_consumer import validate_synthetic_owner
            validate_synthetic_owner(synthetic_owner,Path(root))
    elif administrative_owner is not None:raise ValueError('Administrative new-cycle lease only')
    proposal,target=b.locations(run_id,root,kind,reviewed=reviewed_fingerprint,owner=owner)
    with (nullcontext() if administrative_owner is not None else ownership(run_id,root)):
        if target.exists():raise FileExistsError('Authorization is already bound; never replace')
        started_ms=now_ms()
        p,_=b.read(proposal)
        if fingerprint(p)!=reviewed_fingerprint or p['run_id']!=run_id or p['kind']!=kind:
            raise ValueError('Exact reviewed proposal required')
        if kind==b.SUCCESSOR:expected=successor_proposal(run_id,root,p['effective_ms'])
        elif kind==b.NEW_CYCLE:
            from research.new_cycle_authorization import proposal as new_cycle_proposal
            expected=new_cycle_proposal(run_id,root,administrative_owner if administrative_owner is not None else synthetic_owner)
        else:expected=abandonment_proposal(run_id,root,owner,p['authorization']['resolved_ms'])
        if p!=expected:raise ValueError('Reviewed proposal checkpoint/identity changed')
        final_ms=now_ms()
        if final_ms<started_ms:raise ValueError('Clock regression during binding')
        if kind in (b.SUCCESSOR,b.NEW_CYCLE) and p['effective_ms']<final_ms+300000:
            raise ValueError('Future binding margin expired during validation')
        if administrative_owner is not None:administrative_owner.guard('bind',run_id)
        value=b.bound(p,final_ms)
        target.parent.mkdir(parents=True,exist_ok=True)
        exclusive(target,value)  # xb + flush + fsync; partial files are NOT repaired.
        persisted,raw=b.read(target)
        if raw!=encoded(value)+b'\n':raise ValueError('Persisted binding mismatch')
        b.historical(persisted)
        return fingerprint(persisted)


def resolve(run_id,owner,*,root=ROOT):
    """Separate explicit administration; bind itself never resolves anything."""
    from paper.ledger import PaperLedger
    from paper.source import SourceAdapter
    from paper.market import CandleSource
    with ownership(run_id,root,ledger=False):
        cap=load_abandonment(run_id,owner,root);auth=cap.successor
        _,directory=paths(run_id,root);source_path=Path(root)/'artifacts'/'forward-paper'/'live-0a-001'
        ledger=PaperLedger(directory,auth.binding,auth.rules,SourceAdapter(source_path,auth.source),
            CandleSource(source_path,auth.source),authorization=auth)
        try:return ledger.abandon_blocked_clock(cap)
        finally:ledger.close()



def initialize_new_cycle(auth,reviewed_fingerprint,*,root=ROOT,clock=now_ms):
    """Initialization approval is not execution approval. No proposal/binding here."""
    from paper.operations import Authorization
    from research.new_paper_cycle import AdministrativeOwnership,initialize
    if type(auth) is not Authorization or auth.id!=reviewed_fingerprint:
        raise ValueError('Exact reviewed initialization fingerprint required')
    auth=Authorization(auth.payload)
    with AdministrativeOwnership(auth.record['run_id'],root,'initialize') as lease:
        return initialize(auth,reviewed_fingerprint=reviewed_fingerprint,owner=lease,clock=clock)


def propose_new_cycle(run_id,*,root=ROOT):
    """Generate data for separate human review; never creates a binding."""
    from research.new_paper_cycle import AdministrativeOwnership
    from research.new_cycle_authorization import proposal
    with AdministrativeOwnership(run_id,root,'proposal') as lease:
        p=proposal(run_id,root,lease)
        path=write_proposal(p,root)
        persisted,raw=b.read(path)
        if persisted!=p or raw!=encoded(p)+b'\n':raise ValueError('Proposal readback mismatch')
        import hashlib
        return dict(path=str(path),fingerprint=fingerprint(p),sha256=hashlib.sha256(raw).hexdigest(),proposal=p)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicit human-authorized LIVE-0B administration')
    parser.add_argument('command',choices=('bind-clock-v3','bind-abandonment','resolve-abandonment',
        'initialize-new-cycle','propose-new-cycle','bind-new-cycle'))
    parser.add_argument('--run-id',required=True);parser.add_argument('--reviewed-fingerprint');parser.add_argument('--reservation-id')
    parser.add_argument('--reviewed-manifest')
    args=parser.parse_args(argv)
    if args.command=='initialize-new-cycle':
        if not args.reviewed_manifest or not args.reviewed_fingerprint:
            parser.error('Reviewed initialization manifest and exact fingerprint required')
        from paper.operations import Authorization
        value,_=b.read(args.reviewed_manifest);auth=Authorization(encoded(value))
        if auth.record['run_id']!=args.run_id:raise ValueError('Run mismatch')
        result=initialize_new_cycle(auth,args.reviewed_fingerprint)
    elif args.command=='propose-new-cycle':result=propose_new_cycle(args.run_id)
    elif args.command=='bind-new-cycle':
        if not args.reviewed_fingerprint:parser.error('Exact reviewed execution fingerprint required')
        result=bind(args.run_id,b.NEW_CYCLE,args.reviewed_fingerprint)
    elif args.command=='resolve-abandonment':
        if not args.reservation_id:parser.error('Exact reservation required')
        result=resolve(args.run_id,args.reservation_id)
    else:
        if not args.reviewed_fingerprint:parser.error('Independent exact fingerprint review required')
        kind=b.SUCCESSOR if args.command=='bind-clock-v3' else b.ABANDONMENT
        result=bind(args.run_id,kind,args.reviewed_fingerprint,owner=args.reservation_id)
    print(encoded(result).decode())


if __name__=='__main__':main()
