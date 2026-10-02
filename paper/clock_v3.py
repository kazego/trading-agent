"""Prospective versioned clock confirmation; no activation or persistent writes."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

from paper.acceptance import timestamp
from paper.model import LABEL, parse
from research.spec import encoded, fingerprint


def policy():
    return dict(version='clock-confirmation-v3', label=LABEL,
        utc_implementation='GetSystemTimeAsFileTime()',
        counter_implementation='QueryPerformanceCounter()',
        utc_resolution_ns=15625000, counter_resolution_ns=100,
        qU_ns=15625000, qP_ns=100, qT_ns=1000000,
        allowance_ns=32250000, max_bracket_ns=5000000,
        deadline='RECORDED_ACCEPTANCE_AND_POST_COMMIT_UTC_LE_INCLUSIVE_D',
        semantics='RESOLUTION_DERIVED_ENGINEERING_ALLOWANCE_NOT_UTC_ACCURACY')


def implementation():
    return fingerprint(dict(policy=policy(), source=hashlib.sha256(
        Path(__file__).read_text(encoding='utf-8').encode()).hexdigest()))


def verify_runtime():
    u, p = time.get_clock_info('time'), time.get_clock_info('perf_counter')
    if (u.implementation != policy()['utc_implementation'] or u.monotonic
            or not u.adjustable or u.resolution != 0.015625
            or p.implementation != policy()['counter_implementation']
            or not p.monotonic or p.adjustable or p.resolution != 0.0000001):
        raise ValueError('Unsupported clock-v3 runtime profile')


def sample():
    verify_runtime()
    before = time.perf_counter_ns()
    utc = time.time_ns() // 1000000
    after = time.perf_counter_ns()
    return dict(before_ns=before, utc_ms=utc, after_ns=after)


def valid_sample(value):
    return (type(value) is dict and set(value) == {'before_ns','utc_ms','after_ns'}
        and all(type(v) is int and v >= 0 for v in value.values()))


def confirmation(reservation, acceptance, durable):
    """Pure replay. No runtime clock reads and no relaxation of the deadline."""
    error = None
    lo = hi = delta = None
    if not valid_sample(acceptance) or not valid_sample(durable):
        error = 'INVALID_SAMPLE'
    else:
        a, c = acceptance, durable
        try:
            timestamp(a['utc_ms']); timestamp(c['utc_ms'])
        except (TypeError, ValueError):
            error = 'INVALID_UTC_SAMPLE'
        if a['utc_ms'] != reservation['acceptance_ms']:
            error = 'ACCEPTANCE_SAMPLE_MISMATCH'
        elif not a['before_ns'] <= a['after_ns'] <= c['before_ns'] <= c['after_ns']:
            error = 'MONOTONIC_SAMPLE_ORDER_REGRESSION'
        elif c['utc_ms'] < a['utc_ms']:
            error = 'UTC_REGRESSION'
        elif max(a['after_ns']-a['before_ns'],c['after_ns']-c['before_ns']) > 5000000:
            error = 'OVERSIZED_SAMPLE_BRACKET'
        lo = c['before_ns'] - a['after_ns'] - 200
        hi = c['after_ns'] - a['before_ns'] + 200
        delta = (c['utc_ms']-a['utc_ms'])*1000000
        if error is None and (delta+32250000 < lo or delta-32250000 > hi):
            error = 'UTC_COUNTER_DISAGREEMENT'
    status = ('BLOCKED_CLOCK' if error else 'MISSED_ACCEPTANCE_DEADLINE'
              if acceptance['utc_ms'] > reservation['deadline_ms'] or durable['utc_ms'] > reservation['deadline_ms']
              else 'PENDING_ENTRY')
    return dict(label=LABEL, version='live-0b-acceptance-confirmation-v3',
        reservation_id=reservation['reservation_id'], acceptance_sample=acceptance,
        durable_sample=durable, policy=policy(), policy_fingerprint=fingerprint(policy()),
        deadline_ms=reservation['deadline_ms'], durable_ms=durable.get('utc_ms') if valid_sample(durable) else None,
        elapsed_low_ns=lo, elapsed_high_ns=hi, utc_delta_ns=delta,
        allowance_ns=32250000, clock_error=error, status=status)


@dataclass(frozen=True)
class ClockAuthorization:
    """Explicit approval capability. No writer, CLI, consumer default or live binding."""
    payload: bytes
    approved_fingerprint: str

    def __post_init__(self):
        r = parse(self.payload)
        if (set(r) != {'version','label','binding','effective_ms','policy','implementation'}
                or r['version'] != 'paper-clock-v3-authorization-v1' or r['label'] != LABEL
                or r['policy'] != fingerprint(policy()) or r['implementation'] != implementation()
                or type(r['binding']) is not str or len(r['binding']) != 64
                or any(ch not in '0123456789abcdef' for ch in r['binding'])
                or encoded(r) != self.payload or fingerprint(r) != self.approved_fingerprint):
            raise ValueError('Explicit exact clock-v3 approval required')
        timestamp(r['effective_ms'])

    @property
    def record(self):
        return parse(self.payload)

    def check(self, binding, accepted_ms):
        self.__post_init__()
        if self.record['binding'] != binding.id or accepted_ms < self.record['effective_ms']:
            raise ValueError('Clock authorization binding/effective time mismatch')


def replay_confirmation(reservation, value):
    if value.get('version') == 'live-0b-acceptance-confirmation-v2':
        from paper.reservations import AcceptanceStore
        return AcceptanceStore._confirmation(reservation,value['durable_ms'],value['elapsed_ns'])
    if value.get('version') == 'live-0b-acceptance-confirmation-v3':
        return confirmation(reservation,value['acceptance_sample'],value['durable_sample'])
    raise ValueError('Unknown confirmation version')
