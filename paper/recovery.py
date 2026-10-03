"""Exact append-only abandonment authorization. No activation or automatic recovery."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from paper.acceptance import timestamp
from paper.model import LABEL, parse
from research.spec import encoded, fingerprint, canonical


def mechanism():
    return dict(version='paper-abandon-blocked-clock-v1', label=LABEL,
        from_state='BLOCKED_CLOCK', to_state='ABANDONED',
        financial_effect='NONE', fallback=False, original_confirmation='PRESERVED',
        source=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode()).hexdigest())


def checkpoint(state, owner):
    if owner not in state.reservations:
        raise ValueError('Unknown abandonment reservation')
    r = state.reservations[owner]
    return canonical(dict(genesis=state.identity, journal_head=state.journal_head,
        binding=state.binding.id, rules=state.rules.id, owner=owner,
        reservation=fingerprint(r['reservation']), confirmation=fingerprint(state.confirmations.get(owner)),
        accept_event=state.event_ids.get('ACCEPT:'+str(r['reservation']['signal_boundary_ms'])),
        confirm_event=state.event_ids.get('CONFIRM:'+owner),
        account=state.rows()['account'], slot=list(state.slot)))


@dataclass(frozen=True)
class RecoveryAuthorization:
    payload: bytes
    approved_fingerprint: str

    def __post_init__(self):
        r = parse(self.payload)
        if (set(r) != {'version','label','mechanism','checkpoint','resolved_ms','reason'}
                or r['version'] != 'paper-abandonment-authorization-v1' or r['label'] != LABEL
                or type(r['mechanism']) is not str or len(r['mechanism']) != 64
                or r['reason'] != 'OPERATOR_AUTHORIZED_NONEXECUTABLE_CLOCK_INCIDENT'
                or self.payload != encoded(r) or fingerprint(r) != self.approved_fingerprint):
            raise ValueError('Exact explicit abandonment approval required')
        timestamp(r['resolved_ms'])

    @property
    def record(self):
        return parse(self.payload)


def apply(state, owner, authorization, approved):
    auth = RecoveryAuthorization(encoded(authorization), approved)
    r = state.reservations.get(owner)
    if (r is None or r['state'] != 'BLOCKED_CLOCK'
            or state.confirmations[owner]['status'] != 'BLOCKED_CLOCK'
            or state.slot != ('BLOCKED',owner,r['reservation']['symbol'])
            or owner in state.positions or owner in state.trades
            or any(e['owner'] == owner for e in state.accounting)
            or any(o == owner for o,t in state.processed)
            or auth.record['checkpoint'] != checkpoint(state,owner)):
        raise ValueError('Abandonment ownership/history/checkpoint mismatch')
    state._not_before('ABANDON_BLOCKED_CLOCK',auth.record['resolved_ms'])
    if not auth.record['checkpoint']['accept_event'] or not auth.record['checkpoint']['confirm_event']:
        raise ValueError('Original ACCEPT/CONFIRM identities required')
    resolution = dict(label=LABEL,version='paper-abandonment-v1',owner=owner,
        state='ABANDONED',authorization=auth.record,authorization_fingerprint=approved)
    state.resolutions[owner] = resolution
    r['state'] = 'ABANDONED'
    state.slot = ('EMPTY',None,None)
    state.state_available_ms = auth.record['resolved_ms']
    return resolution
