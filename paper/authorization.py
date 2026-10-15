"""Read-only bound-store loaders. Proposals and hashes cannot issue capabilities."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

from paper.operations import Authorization, record, paths, source_from, rules_from
from paper.model import LABEL, parse
from paper import binding as b
from research.spec import encoded, fingerprint


def implementation():
    return b.source_identity(('paper/authorization.py','paper/binding.py'))


def now_ms(): return time.time_ns()//1000000


def chain(run_id, root):
    control,ledger=paths(run_id,root)
    e,a_raw=b.read(control/'activation.json'); t,t_raw=b.read(control/'transition-b6-v1.json')
    if set(e)!={'authorization','authorization_fingerprint'}:raise ValueError('Root envelope mismatch')
    v=e['authorization']
    current=record(run_id,v['t0_ms'],v['authorized_ms'],source_from(v['source']),rules_from(v['rules']))
    expected=dict(current,implementations=dict(current['implementations'],b3=b.OLD_B3,b4=b.ROOT_B4,b5=b.ROOT_B5))
    if v!=expected or fingerprint(v)!=e['authorization_fingerprint']:raise ValueError('Historical activation ancestry mismatch')
    from paper.transition import SCOPE
    body=dict(label=LABEL,version='paper-b6-transition-v1',scope=SCOPE,
        authorization=fingerprint(v),old_implementations=v['implementations'],
        new_implementations=dict(v['implementations'],b4=b.B6_B4,b5=b.B6_B5),t0_ms=v['t0_ms'],
        rules_fingerprint=v['rules_fingerprint'],source=v['source'],acceptance_policy=v['acceptance_policy'],
        policy_fingerprint=fingerprint(v['policy']),effective_ms=t['effective_ms'],checkpoint=t['checkpoint'])
    if t!=dict(body,transition_fingerprint=fingerprint(body)) or t['effective_ms']<v['t0_ms']:
        raise ValueError('Historical B6 ancestry mismatch')
    return v,t,current,a_raw,t_raw


def unchanged(v):
    return {k:v[k] for k in ('t0_ms','initial_equity_available_ms','rules_fingerprint','config','source','acceptance_policy','policy')}


def verify_successor(p,v,t,current):
    b.validate(p)
    from paper.clock_v3 import policy,implementation as clock_id
    binding=Authorization.binding.fget(_historical_auth(v))
    clock=dict(version='paper-clock-v3-authorization-v1',label=LABEL,binding=binding.id,
        effective_ms=p['effective_ms'],policy=fingerprint(policy()),implementation=clock_id())
    expected=dict(kind=b.SUCCESSOR,label=LABEL,schema=fingerprint(b.schema(b.SUCCESSOR)),run_id=v['run_id'],
        activation=fingerprint(v),b6=t['transition_fingerprint'],implementations=current['implementations'],
        previous_implementations=t['new_implementations'],effective_ms=p['effective_ms'],clock=clock,
        checkpoint=p['checkpoint'],unchanged=unchanged(v))
    if p!=expected or p['effective_ms']<=t['effective_ms']:raise ValueError('Successor ancestry/identity/configuration mismatch')


def _historical_auth(v):
    a=object.__new__(Authorization);object.__setattr__(a,'payload',encoded(v));return a


def state_at(run_id,root,v, *, count=None, checkpoints=None):
    """Full read-only B4 reconciliation; optional verified prefix for a checkpoint."""
    from paper.ledger import PaperLedger
    from paper.journal import Replay,HISTORICAL,implementation as ledger_id
    _,directory=paths(run_id,root)
    path=b.safe(directory/'paper.sqlite')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        row=db.execute('SELECT digest,payload FROM identity').fetchall()
        if len(row)!=1:raise ValueError('Unique historical genesis required')
        genesis,raw=row[0];value=parse(raw.encode());auth=_historical_auth(v)
        expected=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=v['implementations']['b4'],
            historical=HISTORICAL,binding=auth.binding.record,rules=auth.rules.record,activation='UNSET_FOUNDATION_ONLY',
            initial_equity='1000',initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=v['t0_ms']))
        if value!=expected or fingerprint(value)!=genesis:raise ValueError('Genesis ancestry mismatch')
        verifier=object.__new__(PaperLedger);verifier._db=db;verifier.identity=value;verifier.identity_id=genesis
        verifier.binding=auth.binding;verifier.rules=auth.rules;verifier._runtime_identity=ledger_id()
        if checkpoints is not None and count is not None:raise ValueError('Ambiguous checkpoint request')
        state=verifier._verify(checkpoints=checkpoints)
        rows=[list(r) for r in db.execute('SELECT * FROM journal ORDER BY sequence')]
        if count is not None:
            if type(count) is not int or count<0 or count>len(rows):raise ValueError('Invalid checkpoint length')
            rows=rows[:count];state=Replay(auth.binding,auth.rules,genesis)
            for _,digest,request,_,payload in rows:
                event=parse(payload.encode());state.apply(event['kind'],event['input'])
                state.journal_head=digest;state.event_ids[request]=digest
        cp=dict(genesis=genesis,identity=raw,journal_count=len(rows),journal_fingerprint=fingerprint(rows),
            account=state.rows()['account'],slot=state.rows()['global_slot'])
        return state,parse(encoded(cp))


def check_prefix(run_id,root,v,cp):
    _,actual=state_at(run_id,root,v,count=cp['journal_count'])
    if actual!=cp:raise ValueError('Bound historical checkpoint mismatch')


_SEAL=object()

@dataclass(frozen=True,init=False)
class ClockCapability(Authorization):
    root: Path
    envelope_bytes: bytes
    activation_bytes: bytes
    b6_bytes: bytes
    _seal: object

    def __init__(self,*args,**kwargs):raise TypeError('Use the canonical execution loader')

    @property
    def envelope(self):return parse(self.envelope_bytes[:-1])
    @property
    def proposal(self):return self.envelope['proposal']
    @property
    def bound_id(self):return fingerprint(self.envelope)
    @property
    def ledger_authorization(self):return self

    def guard(self):
        if self._seal is not _SEAL:raise ValueError('Unissued capability')
        run=self.record['run_id'];v,t,current,a_raw,t_raw=chain(run,self.root)
        _,path=b.locations(run,self.root,b.SUCCESSOR)
        value,raw=b.read(path)
        if raw!=self.envelope_bytes or a_raw!=self.activation_bytes or t_raw!=self.b6_bytes:
            raise ValueError('Bound authorization bytes changed')
        p=b.historical(value);verify_successor(p,v,t,current)
        if now_ms()<p['effective_ms']:raise ValueError('Successor not effective')

    def check(self,binding,accepted_ms,boundary_ms):
        self.guard()
        if binding!=self.binding or accepted_ms<self.proposal['effective_ms']:
            raise ValueError('Wrong run/binding or premature clock acceptance')
        # Earlier aligned boundaries can only be recorded as expired, never executable.
        if boundary_ms<self.proposal['effective_ms'] and accepted_ms<=boundary_ms+840000:
            raise ValueError('Pre-successor boundary cannot become executable')

    def accept(self,ledger,boundary):
        return ledger.accept_v3(boundary,authorization=self)


def _issue(cls,**fields):
    value=object.__new__(cls)
    for key,item in fields.items():object.__setattr__(value,key,item)
    object.__setattr__(value,'_seal',_SEAL)
    return value


def _read_successor(run_id, root):
    v,t,current,a_raw,t_raw=chain(run_id,root)
    _,path=b.locations(run_id,root,b.SUCCESSOR);e,raw=b.read(path)
    p=b.historical(e);verify_successor(p,v,t,current)
    if now_ms()<p['effective_ms']:raise ValueError('Successor not effective')
    return v,t,p,a_raw,t_raw,raw


@dataclass(frozen=True,init=False)
class ExecutionPlan:
    """Verified metadata only. No accept/check/guard execution capability.

    The capability is returned only after the ledger has verified both prefixes
    and all financial state in its invocation-local SQLite transaction.
    """
    root: Path
    run_id: str
    payload: bytes
    ancestry: tuple
    _seal: object
    _used: bool

    def __init__(self,*args,**kwargs):raise TypeError('Use prepare_execution with a run identity')
    @property
    def record(self):return parse(self.payload)
    @property
    def source(self):return source_from(self.record['source'])

    def open(self,verified_source):
        from paper.ledger import PaperLedger
        from paper.source import SourceAdapter
        from paper.market import CandleSource
        if self._seal is not _SEAL or self._used:raise ValueError('Invalid or consumed execution plan')
        object.__setattr__(self,'_used',True)
        v,t,p,a_raw,t_raw,raw=_read_successor(self.run_id,self.root)
        if encoded(v)!=self.payload or (a_raw,t_raw,raw)!=self.ancestry:
            raise ValueError('Authorization changed before ledger verification')
        _,directory=paths(self.run_id,self.root)
        if not (directory/'paper.sqlite').is_file():raise ValueError('Invocation requires existing authorized ledger')
        cap=_issue(ClockCapability,payload=encoded(v),root=self.root,envelope_bytes=raw,
                   activation_bytes=a_raw,b6_bytes=t_raw)
        source_path=self.root/'artifacts'/'forward-paper'/'live-0a-001'
        ledger=PaperLedger(directory,cap.binding,cap.rules,SourceAdapter(source_path,cap.source),
            CandleSource(source_path,cap.source),verified_source=verified_source,authorization=cap,
            invocation_reuse=True,checkpoints=(t['checkpoint'],p['checkpoint']))
        try:cap.guard()
        except BaseException:
            ledger.close();raise
        return cap,ledger


def prepare_execution(run_id,root=None):
    from paper.operations import ROOT
    root=Path(ROOT if root is None else root).resolve()
    v,t,p,a_raw,t_raw,raw=_read_successor(run_id,root)
    return _issue(ExecutionPlan,root=root,run_id=run_id,payload=encoded(v),
                  ancestry=(a_raw,t_raw,raw),_used=False)


def load_execution(run_id, root=None):
    from paper.operations import ROOT
    root=Path(ROOT if root is None else root).resolve()
    v,t,p,a_raw,t_raw,raw=_read_successor(run_id,root)
    state_at(run_id,root,v,checkpoints=(t['checkpoint'],p['checkpoint']))
    cap=_issue(ClockCapability,payload=encoded(v),root=root,envelope_bytes=raw,activation_bytes=a_raw,b6_bytes=t_raw)
    cap.guard();return cap


@dataclass(frozen=True,init=False)
class AbandonCapability:
    successor: ClockCapability
    envelope_bytes: bytes
    _seal: object
    def __init__(self,*args,**kwargs):raise TypeError('Use the canonical abandonment loader')
    @property
    def envelope(self):return parse(self.envelope_bytes[:-1])
    @property
    def record(self):return self.envelope['proposal']['authorization']
    @property
    def payload(self):return encoded(self.record)
    @property
    def approved_fingerprint(self):return fingerprint(self.record)
    def guard(self):
        if self._seal is not _SEAL:raise ValueError('Unissued abandonment capability')
        self.successor.guard();p=b.historical(self.envelope)
        owner=self.record['checkpoint']['owner'];run=self.successor.record['run_id']
        _,path=b.locations(run,self.successor.root,b.ABANDONMENT,owner=owner)
        _,raw=b.read(path)
        if raw!=self.envelope_bytes:raise ValueError('Abandonment binding bytes changed')
        verify_abandonment(p,self.successor)


def verify_abandonment(p,successor):
    from paper.operations import record as current_record
    from paper.recovery import mechanism
    b.validate(p);v=successor.record
    current=current_record(v['run_id'],v['t0_ms'],v['authorized_ms'],successor.source,successor.rules)
    expected=dict(kind=b.ABANDONMENT,label=LABEL,schema=fingerprint(b.schema(b.ABANDONMENT)),run_id=v['run_id'],
        activation=successor.id,b6=successor.proposal['b6'],implementations=current['implementations'],
        successor=successor.bound_id,authorization=p['authorization'],operation='BLOCKED_CLOCK->ABANDONED',
        constraints=['NO_ENTRY','NO_REPLAY','NO_FALLBACK','NO_FINANCIAL_EFFECT'])
    if p!=expected or p['authorization']['mechanism']!=fingerprint(mechanism()):raise ValueError('Abandonment identity/ancestry mismatch')


def load_abandonment(run_id, reservation_id, root=None):
    successor=load_execution(run_id,root)
    _,path=b.locations(run_id,successor.root,b.ABANDONMENT,owner=reservation_id);e,raw=b.read(path)
    p=b.historical(e);verify_abandonment(p,successor)
    if p['authorization']['checkpoint']['owner']!=reservation_id:raise ValueError('Wrong reservation')
    cap=_issue(AbandonCapability,successor=successor,envelope_bytes=raw);cap.guard()
    state,_=state_at(run_id,successor.root,successor.record)
    from paper.recovery import checkpoint
    existing=state.resolutions.get(reservation_id)
    if existing is None:
        if checkpoint(state,reservation_id)!=cap.record['checkpoint']:raise ValueError('Abandonment checkpoint drift')
    elif existing['authorization_fingerprint']!=cap.approved_fingerprint or existing.get('binding')!=e:
        raise ValueError('Conflicting completed abandonment')
    if now_ms()<cap.record['resolved_ms']:raise ValueError('Resolution timestamp is future')
    return cap
