"""One explicit immutable B6 transition; no automatic upgrades or live writes."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

from paper.operations import Authorization, record, rules_from, source_from, paths, exclusive
from paper.model import LABEL, parse
from research.spec import encoded, fingerprint

OLD_B4 = '217d636cc701b0b88ce6f06dac4b27a4ff7380d37827a0efdc33132206a3da32'
OLD_B5 = '3e16cfc0fb14a9a2710b80d9ba00e3669875943cb206d0d112e12301280c3a31'
SCOPE = 'Invocation-only detached verification; batched Git/no-console; unchanged financial semantics'


def _root(payload):
    v = parse(payload)
    expected = record(v['run_id'],v['t0_ms'],v['authorized_ms'],source_from(v['source']),rules_from(v['rules']))
    expected['implementations'].update(b4=OLD_B4,b5=OLD_B5)
    if v != expected:
        raise ValueError('Unapproved historical activation or changed B1/B2/B3 semantics')
    return v


def _read(path):
    raw = path.read_bytes()
    value = parse(raw[:-1])
    if raw != encoded(value)+b'\n': raise ValueError('Noncanonical authorization')
    return value,raw


def _prefix(db, count):
    rows = [list(r) for r in db.execute('SELECT * FROM journal ORDER BY sequence LIMIT ?', (count,))]
    if len(rows) != count: raise ValueError('Truncated transition journal checkpoint')
    return fingerprint(rows)


def verify_checkpoint(path, cp):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        identity = db.execute('SELECT digest,payload FROM identity').fetchall()
        if identity != [(cp['genesis'],cp['identity'])] or _prefix(db,cp['journal_count']) != cp['journal_fingerprint']:
            raise ValueError('B6 historical ledger checkpoint changed')


def _body(v, cp, effective_ms):
    current = record(v['run_id'],v['t0_ms'],v['authorized_ms'],source_from(v['source']),rules_from(v['rules']))
    if type(effective_ms) is not int or effective_ms < v['t0_ms']:
        raise ValueError('Invalid transition effective timestamp')
    return dict(label=LABEL,version='paper-b6-transition-v1',scope=SCOPE,
        authorization=fingerprint(v),old_implementations=v['implementations'],
        new_implementations=current['implementations'],t0_ms=v['t0_ms'],
        rules_fingerprint=v['rules_fingerprint'],source=v['source'],
        acceptance_policy=v['acceptance_policy'],policy_fingerprint=fingerprint(v['policy']),
        effective_ms=effective_ms,checkpoint=cp)


@dataclass(frozen=True)
class TransitionAuthorization(Authorization):
    transition_payload: bytes
    control: Path
    ledger: Path
    activation_bytes: bytes

    def __post_init__(self):
        v = _root(self.payload)
        t = parse(self.transition_payload)
        body = _body(v,t['checkpoint'],t['effective_ms'])
        if t != dict(body,transition_fingerprint=fingerprint(body)):
            raise ValueError('B6 transition identity/ancestry mismatch')

    def guard(self):
        self.__post_init__()  # Rehash current implementation, never mtime/size caching.
        if (self.control/'activation.json').read_bytes() != self.activation_bytes:
            raise ValueError('Root activation changed')
        if (self.control/'transition-b6-v1.json').read_bytes() != self.transition_payload+b'\n':
            raise ValueError('B6 transition changed')
        if time.time_ns()//1000000 < parse(self.transition_payload)['effective_ms']:
            raise ValueError('B6 transition not effective')


def load_transition(run_id, root, activation_bytes):
    control,ledger = paths(run_id,root)
    e = parse(activation_bytes[:-1])
    if activation_bytes != encoded(e)+b'\n' or fingerprint(e['authorization']) != e['authorization_fingerprint']:
        raise ValueError('Root activation identity mismatch')
    t,raw = _read(control/'transition-b6-v1.json')
    result = TransitionAuthorization(encoded(e['authorization']),raw[:-1],control,ledger,activation_bytes)
    if result.record['run_id'] != run_id: raise ValueError('Wrong transition run')
    result.guard()
    verify_checkpoint(ledger/'paper.sqlite',t['checkpoint'])
    return result


def plan(run_id, *, root, effective_ms, now_ms):
    """Read-only proposal after draining the consumer; no snapshot/evidence rewriting."""
    if effective_ms < now_ms+300000: raise ValueError('Future effective time with five-minute margin required')
    control,ledger = paths(run_id,root)
    if (control/'transition-b6-v1.json').exists(): raise ValueError('Transition already exists')
    e,raw = _read(control/'activation.json'); v = _root(encoded(e['authorization']))
    if e != dict(authorization=v,authorization_fingerprint=fingerprint(v)): raise ValueError('Root hash mismatch')
    for path in (control,ledger):
        if (path/'BLOCKED.json').exists(): raise ValueError('Blocked run cannot transition')
    # Pure B4 financial replay, no source consumption or mutable ledger constructor.
    from paper.ledger import PaperLedger
    from paper.journal import implementation,HISTORICAL
    from paper.acceptance import AcceptanceBinding,ProspectiveGate
    from paper.model import DraftManifest
    with closing(sqlite3.connect((ledger/'paper.sqlite').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        rows = db.execute('SELECT digest,payload FROM identity').fetchall()
        if len(rows) != 1: raise ValueError('Unique historical genesis required')
        genesis,identity = rows[0]; value = parse(identity.encode())
        verifier = object.__new__(PaperLedger)
        verifier._db=db; verifier.identity=value;verifier.identity_id=genesis
        verifier._runtime_identity=implementation()
        verifier.binding=AcceptanceBinding(DraftManifest(source_from(v['source']),rules_fingerprint=v['rules_fingerprint']),
                                            ProspectiveGate(v['t0_ms'],fingerprint(v)))
        verifier.rules=rules_from(v['rules'])
        expected=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=OLD_B4,
            historical=HISTORICAL,binding=verifier.binding.record,rules=verifier.rules.record,
            activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=v['t0_ms']))
        if value != expected or fingerprint(value) != genesis:
            raise ValueError('Historical genesis differs from activation')
        state=verifier._verify()
        count=db.execute('SELECT count(*) FROM journal').fetchone()[0]
        cp=dict(genesis=genesis,identity=identity,journal_count=count,journal_fingerprint=_prefix(db,count),
                account=state.rows()['account'],slot=state.rows()['global_slot'])
    body=_body(v,cp,effective_ms)
    return dict(body,transition_fingerprint=fingerprint(body))


def bind(run_id, *, root, effective_ms, now_ms, approved_fingerprint):
    """Operator-only exclusive binding, never called by consume or import."""
    from runtime import DatabaseLock
    control,_=paths(run_id,root)
    lock=DatabaseLock(control/'consumer');lock.acquire()
    try:
        value=plan(run_id,root=root,effective_ms=effective_ms,now_ms=now_ms)
        if value['transition_fingerprint'] != approved_fingerprint or time.time_ns()//1000000 >= effective_ms:
            raise ValueError('Exact unexpired transition approval required')
        exclusive(control/'transition-b6-v1.json',value)
        return value
    finally: lock.release()
