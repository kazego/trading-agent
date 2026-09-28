"""Explicit B5 authorization; no implicit run creation or market access."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import re
import time

from backtest.simulation import SymbolRules
from paper.acceptance import AcceptanceBinding, ProspectiveGate, policy as acceptance_policy, timestamp
from paper.journal import implementation as b4
from paper.market import RuleSnapshot
from paper.model import LABEL, SourceIdentity, DraftManifest, PaperConfig, parse
from research.spec import encoded, fingerprint

ROOT = Path(__file__).resolve().parents[1]


def policy():
    return dict(label=LABEL, version='paper-b5-policy-v1', prospective='S_STRICTLY_AFTER_T0',
        initial_equity='1000', initial_availability='T0', cadence_seconds=60,
        max_transitions=128, max_runtime_seconds=45, source_stale_ms=1200000,
        missing='WAIT_WITH_EXPOSURE_RETAINED', contradictions='BLOCKED_NO_AUTO_REPAIR',
        scheduler=dict(interval='PT1M', limit='PT2M', instances='IgnoreNew',
                       enabled=False, logon='InteractiveToken', privileges='LeastPrivilege'))


def implementation():
    paths=('paper/operations.py','paper/consumer.py','paper/readout.py','paper/__main__.py',
           'scripts/live0b-task.ps1')
    return fingerprint(dict(version='paper-b5-implementation-v1', b4=b4(), policy=policy(),
        sources={p:hashlib.sha256((ROOT/p).read_text(encoding='utf-8').encode()).hexdigest() for p in paths}))


def rules_from(value):
    rules=RuleSnapshot(value['version'], value['source_identity'],
                       tuple(SymbolRules(**r) for r in value['rules']))
    if rules.record != value:
        raise ValueError('Noncanonical reviewed symbol rules')
    return rules


def source_from(value):
    source=SourceIdentity(**{k:v for k,v in value.items() if k not in ('label','version')})
    if source.record != value:
        raise ValueError('Noncanonical source identity')
    return source


@dataclass(frozen=True)
class Authorization:
    payload: bytes

    def __post_init__(self):
        v=parse(self.payload)
        source=source_from(v['source']); rules=rules_from(v['rules'])
        expected=record(v['run_id'], v['t0_ms'], v['authorized_ms'], source, rules)
        if expected != v:
            raise ValueError('Activation identity/configuration mismatch')

    @property
    def record(self): return parse(self.payload)

    @property
    def id(self): return fingerprint(self.record)

    @property
    def rules(self): return rules_from(self.record['rules'])

    @property
    def source(self): return source_from(self.record['source'])

    @property
    def binding(self):
        return AcceptanceBinding(DraftManifest(self.source, rules_fingerprint=self.rules.id),
                                 ProspectiveGate(self.record['t0_ms'], self.id))


def record(run_id, t0_ms, authorized_ms, source, rules):
    if type(run_id) is not str or not re.fullmatch(r'live-0b-[a-z0-9-]{1,40}',run_id):
        raise ValueError('Explicit LIVE-0B run ID required')
    if timestamp(t0_ms)%14400000 or timestamp(authorized_ms)>=t0_ms:
        raise ValueError('Strictly future aligned T0 required')
    if type(source) is not SourceIdentity or type(rules) is not RuleSnapshot:
        raise ValueError('Explicit reviewed source and rules required')
    from paper.model import implementation as b1
    from paper.acceptance import implementation as b2
    from paper.settlement import implementation as b3
    return dict(label=LABEL, version='paper-b5-authorization-v1', run_id=run_id,
        t0_ms=t0_ms, authorized_ms=authorized_ms, initial_equity_available_ms=t0_ms,
        config=PaperConfig().record, source_run='live-0a-001', source=source.record, rules=rules.record,
        rules_fingerprint=rules.id, acceptance_policy=fingerprint(acceptance_policy()),
        implementations=dict(b1=b1(),b2=b2(),b3=b3(),b4=b4(),b5=implementation()), policy=policy())


def propose(run_id, t0_ms, source, rules, *, now_ms):
    return Authorization(encoded(record(run_id,t0_ms,now_ms,source,rules)))


def paths(run_id, root=ROOT):
    if not re.fullmatch(r'live-0b-[a-z0-9-]{1,40}',run_id):
        raise ValueError('Invalid run ID')
    root=Path(root).resolve()
    control=root/'artifacts'/'forward-paper-consumer'/run_id
    ledger=root/'artifacts'/'forward-paper-execution'/('b4-'+run_id)
    for path in (control, ledger):
        if path.resolve()!=path:
            raise ValueError('Redirected paper namespace')
    return control,ledger


def load(run_id, root=ROOT):
    control,_=paths(run_id,root)
    raw=(control/'activation.json').read_bytes()
    if not raw.endswith(b'\n'): raise ValueError('Canonical activation required')
    envelope=parse(raw[:-1])
    if set(envelope)!={'authorization','authorization_fingerprint'}:
        raise ValueError('Malformed activation envelope')
    if fingerprint(envelope['authorization'])!=envelope['authorization_fingerprint']:
        raise ValueError('Activation fingerprint mismatch')
    auth=Authorization(encoded(envelope['authorization']))
    if auth.record['run_id']!=run_id: raise ValueError('Run binding mismatch')
    return auth


def exclusive(path, value):
    with Path(path).open('xb') as handle:
        handle.write(encoded(value)+b'\n'); handle.flush(); os.fsync(handle.fileno())


def activate(auth, *, now_ms, root=ROOT, clock=None):
    """Explicit future-reviewed writer; never called by consume or import."""
    auth=Authorization(auth.payload)
    if timestamp(now_ms)>=auth.record['t0_ms'] or now_ms<auth.record['authorized_ms']:
        raise ValueError('Authorization expired or clock regressed')
    control,ledger=paths(auth.record['run_id'],root)
    if control.exists() or ledger.exists(): raise ValueError('Run already exists')
    from paper.source import SourceAdapter
    SourceAdapter(Path(root)/'artifacts'/'forward-paper'/auth.record['source_run'],auth.source).snapshot()
    final_ms=timestamp(time.time_ns()//1000000 if clock is None else clock())
    if final_ms<now_ms or final_ms>=auth.record['t0_ms']:
        raise ValueError('T0 expired during source verification')
    control.mkdir(parents=True)
    exclusive(control/'activation.json',dict(authorization=auth.record,authorization_fingerprint=auth.id))
