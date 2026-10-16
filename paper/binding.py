"""Canonical approval records and pure historical checks. Never grants authority."""
import hashlib
from pathlib import Path
from paper.model import LABEL, parse
from paper.acceptance import timestamp
from research.spec import encoded, fingerprint

SUCCESSOR = 'clock-v3-successor-v1'
ABANDONMENT = 'clock-abandonment-v1'
NEW_CYCLE = 'new-paper-cycle-execution-v1'
OLD_B3 = 'ae8493a638ee9017df9641a12bbe910fd1d0cde0572ff907df99e1c2cb056d94'
ROOT_B4 = '217d636cc701b0b88ce6f06dac4b27a4ff7380d37827a0efdc33132206a3da32'
ROOT_B5 = '3e16cfc0fb14a9a2710b80d9ba00e3669875943cb206d0d112e12301280c3a31'
B6_B4 = '0c3543daf8499baaaf969a76f077f7dd6a34accdbe9cf31b17c3b4fb6e4cde78'
B6_B5 = 'c27c64c7ff65b70e7493009f902fa1d62e02f4183af4e292e1b817804c48efc5'


def schema(kind):
    if kind==NEW_CYCLE:
        return dict(version=kind,fields=sorted(['kind','label','schema','run_id','activation',
            'implementations','effective_ms','clock','checkpoint','unchanged','backend',
            'ledger','modules','initial_state','continuity']),authority='CANONICAL_BOUND_STORE_ONLY',
            replay='RECORDED_VERSION_NO_CURRENT_CAPABILITY',operation='NEW_PAPER_INCREMENTAL_ONLY')
    common = ['kind','label','schema','run_id','activation','b6','implementations']
    fields = common + (['effective_ms','clock','checkpoint','unchanged','previous_implementations']
        if kind == SUCCESSOR else ['successor','authorization','operation','constraints'])
    if kind not in (SUCCESSOR,ABANDONMENT): raise ValueError('Unknown approval kind')
    return dict(version=kind,fields=sorted(fields),authority='CANONICAL_BOUND_STORE_ONLY',
        replay='RECORDED_VERSION_NO_CURRENT_CAPABILITY',
        operation='PROSPECTIVE_CLOCK_V3' if kind==SUCCESSOR else 'BLOCKED_CLOCK->ABANDONED')


def digest(value):
    if type(value) is not str or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Canonical SHA-256 required')
    return value


def validate(proposal):
    kind=proposal['kind']; spec=schema(kind)
    if set(proposal)!=set(spec['fields']) or proposal['label']!=LABEL or proposal['schema']!=fingerprint(spec):
        raise ValueError('Malformed proposal schema')
    for field in (('activation',) if kind==NEW_CYCLE else ('activation','b6')):digest(proposal[field])
    if kind==NEW_CYCLE:
        if proposal['backend']!='incremental':raise ValueError('Incremental backend required')
        for field in ('modules','initial_state'):digest(proposal[field])
    if kind in (SUCCESSOR,NEW_CYCLE):
        if timestamp(proposal['effective_ms'])%14400000: raise ValueError('Aligned successor required')
        from paper.clock_v3 import ClockAuthorization
        c=proposal['clock'];ClockAuthorization(encoded(c),fingerprint(c))
        if c['effective_ms']!=proposal['effective_ms']:raise ValueError('Clock effective mismatch')
    else:
        digest(proposal['successor'])
        if (proposal['operation']!='BLOCKED_CLOCK->ABANDONED' or proposal['constraints']!=
            ['NO_ENTRY','NO_REPLAY','NO_FALLBACK','NO_FINANCIAL_EFFECT']):raise ValueError('Wrong resolution semantics')
        from paper.recovery import RecoveryAuthorization
        r=proposal['authorization'];RecoveryAuthorization(encoded(r),fingerprint(r))
    return proposal


def bound(proposal, bound_ms):
    validate(proposal)
    value=dict(version='paper-bound-authorization-v1',label=LABEL,proposal=proposal,
                reviewed_fingerprint=fingerprint(proposal),bound_ms=timestamp(bound_ms))
    historical(value)
    return value


def historical(envelope):
    if (type(envelope) is not dict or set(envelope)!=
            {'version','label','proposal','reviewed_fingerprint','bound_ms'}
            or envelope['version']!='paper-bound-authorization-v1' or envelope['label']!=LABEL
            or fingerprint(validate(envelope['proposal']))!=envelope['reviewed_fingerprint']):
        raise ValueError('Malformed historical approval record')
    stamp=timestamp(envelope['bound_ms']); p=envelope['proposal']
    if p['kind'] in (SUCCESSOR,NEW_CYCLE) and stamp>=p['effective_ms']: raise ValueError('Late historical binding')
    if p['kind']==ABANDONMENT and stamp<p['authorization']['resolved_ms']:
        raise ValueError('Binding precedes resolution timestamp')
    return p  # Plain data, never a runtime capability.


def safe(path):
    path=Path(path).absolute()
    if path.resolve()!=path or path.exists() and (path.is_symlink() or path.stat().st_nlink!=1):
        raise ValueError('Aliased authorization path')
    return path


def read(path):
    raw=safe(path).read_bytes(); v=parse(raw[:-1])
    if raw!=encoded(v)+b'\n':raise ValueError('Canonical artifact bytes required')
    return v,raw


def locations(run_id, root, kind, *, reviewed=None, owner=None):
    from paper.operations import paths
    control,_=paths(run_id,root); schema(kind)
    proposal=None
    if reviewed is not None:
        digest(reviewed)
        proposal=safe(Path(root).resolve()/'artifacts'/'forward-paper-review'/run_id/kind/(reviewed+'.proposal.json'))
    if kind in (SUCCESSOR,NEW_CYCLE): target=control/'authorizations'/(kind+'.json')
    else: target=control/'authorizations'/'abandonments'/(digest(owner)+'.json')
    return proposal,safe(target)


def source_identity(paths):
    root=Path(__file__).resolve().parents[1]
    return fingerprint({p:hashlib.sha256((root/p).read_text(encoding='utf-8').encode()).hexdigest() for p in paths})
