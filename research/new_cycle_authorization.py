"""New-cycle binding support. Canonical deployment or synthetic ownership; never imports old state."""
from contextlib import closing
from pathlib import Path
import sqlite3
from paper import binding as b
from paper.operations import Authorization, paths
from paper.model import LABEL, parse
from research.spec import encoded, fingerprint


def modules():
    return b.source_identity(('research/new_cycle_authorization.py','research/new_paper_cycle.py',
        'research/incremental_full_consumer.py','research/incremental_ledger.py',
        'research/incremental_durable.py','paper/authorization.py','paper/binding.py',
        'paper_admin.py','paper/consumer.py','runtime.py'))


def activation(run,root):
    control,_=paths(run,root)
    e,raw=b.read(control/'activation.json')
    if set(e)!={'authorization','authorization_fingerprint'}:raise ValueError('Activation envelope')
    auth=Authorization(encoded(e['authorization']))
    if auth.id!=e['authorization_fingerprint'] or auth.record['run_id']!=run:
        raise ValueError('New-cycle activation mismatch')
    marker,_=b.read(Path(root)/'cycle-initialization.json')
    expected=dict(operation='INITIALIZE_NEW_PAPER_ONLY',run_id=run,authorization=auth.id,
        initial_equity='1000',historical_import='FORBIDDEN',execution_authority='NONE')
    if marker!=expected:raise ValueError('New-cycle initialization identity mismatch')
    if (control/'transition-b6-v1.json').exists():raise ValueError('Historical cycle cannot become a new cycle')
    return auth,raw


def expected(auth,root,checkpoint,initial_state):
    from paper.authorization import unchanged
    from paper.operations import ROOT
    from paper.clock_v3 import policy,implementation
    run=auth.record['run_id'];_,directory=paths(run,root)
    return dict(kind=b.NEW_CYCLE,label=LABEL,schema=fingerprint(b.schema(b.NEW_CYCLE)),
        run_id=run,activation=auth.id,implementations=auth.record['implementations'],
        effective_ms=auth.record['t0_ms'],clock=dict(version='paper-clock-v3-authorization-v1',
        label=LABEL,binding=auth.binding.id,effective_ms=auth.record['t0_ms'],
        policy=fingerprint(policy()),implementation=implementation()),checkpoint=checkpoint,
        unchanged=unchanged(auth.record),backend='incremental',ledger=str(directory/'paper.sqlite'),
        modules=modules(),initial_state=initial_state,continuity=str(directory if Path(root)==ROOT else Path(root)))


def proposal(run,root,owner):
    from research.incremental_full_consumer import validate_synthetic_owner
    from research.incremental_durable import DurableExternal
    from paper.authorization import state_at,now_ms
    from research.incremental_ledger import compact
    from research.new_paper_cycle import AdministrativeOwnership
    if type(owner) is AdministrativeOwnership:
        if owner.root!=Path(root) or owner.purpose not in ('proposal','bind'):
            raise ValueError('Wrong administrative proposal owner')
        owner.guard(owner.purpose,run)
    else:validate_synthetic_owner(owner,Path(root))
    auth,_=activation(run,root)
    if auth.record['t0_ms']<now_ms()+300000:raise ValueError('Fresh future binding margin required')
    state,cp=state_at(run,root,auth.record)
    if cp['journal_count']!=0 or state.slot!=('EMPTY',None,None) or state.equity!=1000:
        raise ValueError('Only empty new genesis can be bound')
    _,directory=paths(run,root)
    with closing(sqlite3.connect(b.safe(directory/'paper.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('SELECT payload FROM incremental_state').fetchall()
    if len(rows)!=1:raise ValueError('Unique initial incremental state required')
    value=parse(rows[0][0].encode())
    ext=DurableExternal(owner)
    if (value!=ext.anchor or ext.blocked or value['sequence']!=0 or value['authorization']!=auth.id
            or value['state']!=compact(state)):
        raise ValueError('Initial continuity/financial state mismatch')
    p=expected(auth,root,cp,fingerprint(value));b.validate(p);return p


def read_execution(run,root):
    from paper.authorization import now_ms
    from research.incremental_durable import root_path,checked
    root=validated_root(root)
    auth,a_raw=activation(run,root)
    _,path=b.locations(run,root,b.NEW_CYCLE);e,raw=b.read(path)
    p=b.historical(e)
    if p!=expected(auth,root,p['checkpoint'],p['initial_state']):
        raise ValueError('New-cycle binding/identity/configuration mismatch')
    if p['checkpoint']['journal_count']!=0:raise ValueError('New-cycle genesis prefix required')
    if now_ms()<p['effective_ms']:raise ValueError('New cycle not effective')
    return auth.record,dict(checkpoint=p['checkpoint']),p,a_raw,b'',raw


def validated_root(root):
    """Only the installed deployment root or the existing synthetic namespace."""
    from paper.operations import ROOT
    from research.incremental_durable import root_path,checked
    root=Path(root).absolute()
    if root==ROOT:
        checked(root)
        for parent in root.parents:checked(parent)
    else:
        root_path(root.parent);checked(root)
    return root


def continuity_directory(run,root):
    from paper.operations import ROOT
    root=validated_root(root)
    return paths(run,root)[1] if root==ROOT else root


class ExecutionOwnership:
    """Existing OS locks, tied to a loaded binding and the consumer's global lease.

    Never creates a ledger, initializes continuity or grants approval itself.
    """
    def __init__(self,run_id,root,global_lock):
        from paper.operations import consumer_exclusion
        from runtime import DatabaseLock
        self.active=False
        self.root=validated_root(root);self.run_id=run_id
        _,_,proposal,_,_,self.binding_bytes=read_execution(run_id,self.root)
        self.run=continuity_directory(run_id,self.root)
        from research.incremental_durable import checked
        checked(self.run)
        if self.run!=Path(proposal['continuity']):
            raise ValueError('Bound continuity directory mismatch')
        self.global_lock=global_lock
        if type(global_lock) is not DatabaseLock or global_lock.path!=consumer_exclusion(self.root).path or global_lock._file is None or getattr(global_lock._file,'closed',True):
            raise ValueError('Existing global consumer ownership required')
        self.lock=DatabaseLock(b.safe(self.run/'ownership'))
        b.safe(self.lock.path)
        self.lock.acquire();self.active=True

    def guard(self,root,cap=None):
        if (not self.active or self.root!=root or self.lock._file is None
                or self.global_lock._file is None or getattr(self.lock._file,'closed',True)
                or getattr(self.global_lock._file,'closed',True)):raise ValueError('Incremental execution ownership lost')
        _,_,_,_,_,raw=read_execution(self.run_id,self.root)
        if raw!=self.binding_bytes or cap is not None and (cap.envelope_bytes!=raw or cap.record['run_id']!=self.run_id):
            raise ValueError('Ownership belongs to another binding/cycle')

    def close(self):
        self.active=False;self.lock.release()
