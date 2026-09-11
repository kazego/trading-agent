"""Phase 6F0 structural context only. No data loading, signals or execution."""
from dataclasses import dataclass
from pathlib import Path

from research import identity as legacy
from research import semantic_identity as migration
from research.registry import Registry
from research.spec import encoded, fingerprint, Role, VALIDATION_START, HOLDOUT_START, STEP_MS

SCHEMA = 'locked-validation-structure-v1'
CANDIDATE = 'phase6d-c-atr-v1'
SEMANTICS = 'a3a8321028270b005f2fd1fa094bba8f6ad4973d56bb7f42eaf9c307d1c55328'
MIGRATION = 'e0068346177042e0f2629f40050e6a5f5cae2d8040883770a54cd74c235fb005'
RESULT = '46e37dd09f8fad5ed8cb67e7732e5ec3286757255d169296128aa8246f454f1f'
SIGNAL_STEP = 16 * STEP_MS
WARMUP_START = VALIDATION_START - 600 * SIGNAL_STEP
LABEL = Role.VALIDATION.value


@dataclass(frozen=True)
class ValidationPlan:
    payload: bytes

    def __post_init__(self):
        if type(self.payload) is not bytes or encoded(legacy._json(self.payload)) != self.payload:
            raise ValueError('Canonical immutable plan required')

    @property
    def record(self):
        return legacy._json(self.payload)

    @property
    def fingerprint(self):
        return fingerprint(self.record)


def plan_validation(root, *, frozen_spec, software):
    """Explicit frozen candidate input, not an implicit strategy/default selection."""
    if frozen_spec.get('trial_id') != CANDIDATE:
        raise PermissionError('Only the Phase 6E selected C candidate')
    binding = migration.migration_binding(root, frozen_spec, software)
    b = binding['record']
    if (binding['fingerprint'] != MIGRATION or b['candidate_semantic_identity'] != SEMANTICS
            or b['historical_selection'] != legacy.SELECTION or b['historical_result'] != RESULT):
        raise ValueError('Frozen migration/candidate evidence mismatch')
    infrastructure = dict(schema=SCHEMA, migration_infrastructure=b['infrastructure_identity'],
        sources=legacy.source_hashes(root, ('research/validation.py',)), software=software)
    return ValidationPlan(encoded(dict(schema=SCHEMA, candidate=CANDIDATE,
        frozen_specification=frozen_spec, historical_binding=binding,
        current_infrastructure=infrastructure, infrastructure_fingerprint=fingerprint(infrastructure),
        split=LABEL, start_ms=VALIDATION_START, end_ms=HOLDOUT_START,
        warmup_start_ms=WARMUP_START, warmup_end_ms=VALIDATION_START,
        warmup_role='HISTORY_ONLY', completed_history=600, legacy_ema_history=99,
        signal_timeframe='4h', execution_timeframe='15m',
        development_closed=True, holdout_authorized=False,
        market_data_access_authorized=False, execution_authorized=False,
        validation_eligibility=None)))


def artifact_destination(artifact_root, plan):
    if type(plan) is not ValidationPlan or Path(artifact_root).name != 'research':
        raise ValueError('Typed validation plan and research artifact root required')
    return Path(artifact_root) / plan.fingerprint / 'validation'


@dataclass(frozen=True)
class ValidationContext:
    """Dry structural context, not a capability to load data or execute a run."""
    plan: ValidationPlan
    artifact_path: Path
    validation_attempts: int

    def check_boundary(self, timestamp, *, signal=False):
        step = SIGNAL_STEP if signal else STEP_MS
        if (type(timestamp) is not int or not VALIDATION_START <= timestamp < HOLDOUT_START
                or timestamp % step):
            raise PermissionError('Outside aligned locked validation boundary')

    def check_history(self, start, end, *, decision_time):
        self.check_boundary(decision_time, signal=True)
        if (type(start) is not int or type(end) is not int
                or (start, end) != (decision_time - 600 * SIGNAL_STEP, decision_time)
                or start < WARMUP_START or end >= HOLDOUT_START):
            raise PermissionError('Exactly 600 prior completed 4h candles required')

    @property
    def reporting(self):
        return dict(split=LABEL, previously_observed=True, development_eligibility_applied=False,
                    validation_eligibility=None)


def preflight(root, plan, *, artifact_root):
    """READY means structural checks only, never permission to run validation."""
    if type(plan) is not ValidationPlan:
        raise ValueError('Typed validation plan required')
    r = plan.record
    try:
        expected = plan_validation(root, frozen_spec=r['frozen_specification'],
                                   software=r['current_infrastructure']['software'])
    except KeyError as exc:
        raise ValueError('Incomplete validation identity') from exc
    if plan != expected:
        raise ValueError('Validation identity/configuration/calendar mismatch')
    path = artifact_destination(artifact_root, plan)
    if path.exists() or path.is_symlink():
        raise FileExistsError('Existing complete/partial validation artifact')
    # An independent ledger namespace leaves the historical DEVELOPMENT
    # checkpoint intact. Inspect only; Registry construction creates no files.
    registry = Registry(Path(artifact_root) / 'validation-registry')
    events = registry.events()
    attempts = sum(e['kind'] == 'ATTEMPT' for e in events)
    if attempts or events:
        raise PermissionError('Validation ledger must be pristine before first authorization')
    context = ValidationContext(plan, path, attempts)
    context.check_boundary(VALIDATION_START, signal=True)
    context.check_history(WARMUP_START, VALIDATION_START, decision_time=VALIDATION_START)
    return dict(status='READY', scope='STRUCTURAL_ONLY', execution_authorized=False,
                context=context, plan_fingerprint=plan.fingerprint,
                infrastructure_fingerprint=r['infrastructure_fingerprint'])
