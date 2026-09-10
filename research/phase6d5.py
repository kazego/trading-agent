"""Explicit three-trial integration: dry construction, never implicit execution."""
from dataclasses import dataclass, asdict, field
from functools import partial
from pathlib import Path
from types import MappingProxyType
import json

from backtest.clock import OpeningEvent
from backtest.simulation import SymbolRules
from research.spec import (SYMBOLS, DEV_START, VALIDATION_START, Role, SplitRequest,
    fingerprint, policy, encoded, canonical, RULES_HASH)
from research.registry import Registry
from research.runner import (source_identity, software_identity, approved_configuration,
    artifact_destination, execute_validated, build_scorecard)
from research.data import load_local_development, build_view, authorize, ACQUISITION_HASH
from research.features import (FeatureView, FeatureParameters, load_history, calculate_features,
    MANIFEST_HASH, HISTORY_HASHES, VERSION as FEATURE_VERSION)
from research.variants import RibbonStrategy, BreakoutStrategy
from research.strategy import EMACrossover, EMAConfig, ExecutionDecision
from research.schedule import ResearchSchedule
from research.protection import ATRProtection

TRIAL_IDS = ('phase6d-a-ribbon-v1', 'phase6d-b-breakout-v1', 'phase6d-c-atr-v1')
POLICY_HASH = '2642052de313ece07b300791ab1663ff60fc8368d9d7d0059934cde75d4dac99'
ELIGIBILITY_HASH = 'e8f4c0d736c8a8c29e3dc26e61aa3bf194e519eeb9db16e614190f19054cb348'
PRIOR_IDS = (
    '94effde761681b2133e4164e25886b6463af735ca51407044c3c88e93f071ee7',
    '41dd3eef1b985af4cca0f885a68997b721d60357a63b9f0b01f2f160d96f8288',
    '0c8d02a7bb3a2e9bb02c9e8b9c36e6c248581d7c95b2b3e3f91572faa7dd5851')
DATA_HASHES = dict(zip(SYMBOLS, (
    '566c94bf3dd152fc4636392edfdbf322277d0c34fefd3d6ca6b7171c8b0f1980',
    'a3a854c9d19bb4970faf235f9dff76d455e7af1376ff79cf58357e0c8097a756',
    'a7d2d582fa6171e5ee6ca5cc285c1f08dabf5c9b2543c49e1b49fa4f59cfea24')))
VIEW_HASHES = dict(zip(SYMBOLS, (
    '1002b59b7699ce1a5b6c29beaaf3f15412f51742ebceee25ce77da5b8fe60630',
    '95b9852180e7b913bd20d27b80d0de9598b7bb0653340faa9c3f8d82d15a8b3e',
    '9ef5a0dd24016d1c418a4357be7384c68e96a4c5a872926dcda6f1abb8ec840a')))
REQUEST = SplitRequest(Role.DEVELOPMENT, DEV_START, VALIDATION_START, DEV_START-99*14400000)
ROOT = Path(__file__).resolve().parent.parent
_ISSUED = object()


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    def invalid(value): raise ValueError('Noncanonical number: '+value)
    return json.loads(text, object_pairs_hook=pairs, parse_float=invalid, parse_constant=invalid)


@dataclass(frozen=True)
class CanonicalRecord:
    """Immutable canonical bytes; record access returns a detached copy."""
    payload: bytes

    def __post_init__(self):
        if type(self.payload) is not bytes or encoded(_json(self.payload)) != self.payload:
            raise ValueError('Canonical JSON bytes required')

    @property
    def record(self): return _json(self.payload)

    @property
    def fingerprint(self): return fingerprint(self.record)


@dataclass(frozen=True)
class TrialSpec(CanonicalRecord):
    @property
    def id(self): return self.fingerprint

    @property
    def trial_id(self): return self.record['trial_id']


@dataclass(frozen=True)
class TrialPlan(CanonicalRecord):
    @property
    def trials(self): return tuple(TrialSpec(encoded(r)) for r in self.record['trials'])


@dataclass(frozen=True)
class Registration:
    strategy_type: type
    protection_type: type | None  # None explicitly selects existing fixed protection.
    key: tuple[str, str, str, str]


REGISTRATIONS = (
    Registration(RibbonStrategy, None, (RibbonStrategy.family, RibbonStrategy.version, 'FIXED_PERCENT', 'fixed-1.5-3-v1')),
    Registration(BreakoutStrategy, None, (BreakoutStrategy.family, BreakoutStrategy.version, 'FIXED_PERCENT', 'fixed-1.5-3-v1')),
    Registration(EMACrossover, ATRProtection, ('EMA_CROSSOVER', 'research-ema-opening-v1', 'WILDER_ATR_PROTECTION', 'atr14-2x-4x-v1')))


def trial_mapping(spec):
    if type(spec) is not TrialSpec: raise ValueError('Typed trial required')
    r = spec.record
    key = tuple(r[k] for k in ('strategy_family','strategy_version','protection_family','protection_version'))
    found = [item for item in REGISTRATIONS if item.key == key]
    if len(found) != 1: raise PermissionError('Unregistered strategy/protection family or version')
    return found[0]


def trial_plan():
    """The sole plan representation. No registration or execution side effects."""
    if fingerprint(policy()) != POLICY_HASH or fingerprint(policy()['eligibility']) != ELIGIBILITY_HASH:
        raise ValueError('Frozen policy changed')
    source = dict(source_identity())
    common = dict(signal_timeframe='4h', execution_timeframe='15m', symbols=SYMBOLS,
        split=REQUEST.record, dataset_fingerprints=DATA_HASHES, view_fingerprints=VIEW_HASHES,
        development_dataset_fingerprints=tuple(VIEW_HASHES.items()),
        history_fingerprints=HISTORY_HASHES, history_manifest_fingerprint=MANIFEST_HASH,
        acquisition_manifest_fingerprint=ACQUISITION_HASH,
        feature_version=FEATURE_VERSION, feature_parameters=asdict(FeatureParameters()),
        feature_implementation_fingerprint=source['research/features.py'],
        source_identity=source, software_identity=dict(software_identity()),
        rules_fingerprint=RULES_HASH, configuration=asdict(approved_configuration()),
        config_fingerprint=fingerprint(asdict(approved_configuration())),
        policy_fingerprint=POLICY_HASH, eligibility_fingerprint=ELIGIBILITY_HASH,
        priority=SYMBOLS, max_positions=1, funding=False, margin_liquidation_model=False)
    parameters = (asdict(RibbonStrategy.parameters), asdict(BreakoutStrategy.parameters), asdict(EMAConfig()))
    trials = []
    for trial_id, registration, params in zip(TRIAL_IDS, REGISTRATIONS, parameters):
        family,version,protection,pversion = registration.key
        fixed = registration.protection_type is None
        trials.append(dict(common, trial_id=trial_id, strategy_family=family, strategy_version=version,
            strategy_parameters=params, protection_family=protection, protection_version=pversion,
            protection_parameters=(dict(stop_fraction='0.015', target_fraction='0.03') if fixed else
                dict(atr_period=14, stop_multiple='2', target_multiple='4', frozen_at_entry=True)),
            risk_basis='fixed_fraction_distance' if fixed else 'actual_rounded_stop_distance'))
    return TrialPlan(encoded(dict(schema='phase6d-trial-plan-v2', trials=trials,
        prior_experiments=PRIOR_IDS, diagnostic_d='DIAGNOSTIC_ONLY_ZERO_TRIALS',
        full_prior_policy=policy(), policy_fingerprint=POLICY_HASH,
        eligibility_fingerprint=ELIGIBILITY_HASH, validation='SEPARATE_FUTURE_AUTHORIZATION_MAX_ONE',
        holdout='UNAUTHORIZED_ENDPOINT_UNSET')))


def validate_plan(plan):
    if type(plan) is not TrialPlan or plan != trial_plan():
        raise ValueError('Exact canonical frozen data/source/software/configuration plan required')
    return plan


def accounting(registry, plan):
    """Reconcile historical identities; attempts/retries/diagnostics are not trials."""
    validate_plan(plan)
    events = registry.events()
    prior = [e['payload'] for e in events if e['kind']=='EXPERIMENT']
    if (tuple(p['experiment_id'] for p in prior) != PRIOR_IDS or
            tuple(p['spec']['signal_timeframe'] for p in prior) != ('15m','1h','4h')):
        raise ValueError('Historical independent configuration population differs')
    for entry in prior:
        if fingerprint(entry['spec']) != entry['experiment_id'] or entry['spec']['policy'] != canonical(policy()):
            raise ValueError('Historical specification/policy integrity failure')
        if len(registry._completed(events, entry['experiment_id'], Role.DEVELOPMENT)) != 1:
            raise ValueError('One preserved completed result per historical configuration required')
    planned = plan.trials
    if tuple(t.trial_id for t in planned) != TRIAL_IDS or len({t.id for t in planned}) != len(planned):
        raise ValueError('Exact three distinct new configurations required')
    attempts = [e for e in events if e['kind']=='ATTEMPT' and e['payload']['experiment_id'] in {t.id for t in planned}]
    return dict(prior_phase6c=len(prior), phase6d_predeclared=len(planned), diagnostic_d_counted=0,
        future_total_strategy_configurations=len(prior)+len(planned), phase6d_attempts=len(attempts))


class Phase6DRegistry(Registry):
    """Atomic plan registration on the existing append-only, hash-chained ledger."""
    def events(self):
        events = super().events()
        plans = [e['payload'] for e in events if e['kind']=='PHASE6D_PLAN']
        if len(plans)>1: raise ValueError('Duplicate registered plans')
        if plans and (set(plans[0])!={'plan','plan_fingerprint'} or
                fingerprint(plans[0]['plan'])!=plans[0]['plan_fingerprint']):
            raise ValueError('Registered plan payload corruption')
        return events

    def registered_plan(self):
        records = [e for e in self.events() if e['kind']=='PHASE6D_PLAN']
        if len(records) != 1: raise ValueError('One explicitly registered Phase 6D plan required')
        payload = records[0]['payload']
        plan = TrialPlan(encoded(payload['plan']))
        if plan.fingerprint != payload['plan_fingerprint']: raise ValueError('Registered plan corrupt')
        return validate_plan(plan)

    def register_plan(self, plan):
        validate_plan(plan)
        accounting(self, plan)
        with self._writer() as events:
            found = [e for e in events if e['kind']=='PHASE6D_PLAN']
            payload = dict(plan=plan.record, plan_fingerprint=plan.fingerprint)
            if found:
                if len(found)!=1 or found[0]['payload']!=payload: raise ValueError('Plan already frozen; no replacement')
                return found[0]['event_id']
            return self._append(events, 'PHASE6D_PLAN', payload)

    @classmethod
    def _experiment(cls, events, experiment_id):
        for event in events:
            if event['kind']=='PHASE6D_PLAN':
                plan = TrialPlan(encoded(event['payload']['plan']))
                if plan.fingerprint != event['payload']['plan_fingerprint']: raise ValueError('Invalid registered plan')
                for trial in plan.trials:
                    if trial.id == experiment_id:
                        validate_plan(plan)
                        return trial.record
        return super()._experiment(events, experiment_id)

    @classmethod
    def _check_access(cls, events, experiment_id, request):
        spec = cls._experiment(events, experiment_id)
        if 'trial_id' in spec:
            if request != REQUEST: raise PermissionError('Phase 6D DEVELOPMENT only; no validation or holdout')
            authorize(request,'4h')
        else:
            super()._check_access(events,experiment_id,request)

    def promote_validation(self, experiment_id, operator, rationale):
        if 'trial_id' in self._experiment(self.events(),experiment_id):
            raise PermissionError('Separate future phase authorization required; at most one candidate')
        return super().promote_validation(experiment_id,operator,rationale)

    def authorize_holdout(self, experiment_id, endpoint_ms, operator, rationale):
        if 'trial_id' in self._experiment(self.events(),experiment_id):
            raise PermissionError('Holdout endpoint remains unset')
        return super().authorize_holdout(experiment_id,endpoint_ms,operator,rationale)


def _manifest(path, expected, hash_field=None):
    document = _json(Path(path).read_bytes())
    if type(document) is not dict or not document: raise ValueError('Nonempty manifest object required')
    body = {k:v for k,v in document.items() if k != hash_field}
    if fingerprint(body)!=expected or (hash_field and document.get(hash_field)!=expected):
        raise ValueError('Frozen manifest fingerprint mismatch')
    return document


def _inputs(historical_root, phase6d0_root, rules_path):
    """Reads selected 2023/2024 inputs; never evaluates a candle boundary."""
    _manifest(Path(historical_root)/'acquisition-manifest.json', ACQUISITION_HASH, 'fingerprint')
    _manifest(Path(phase6d0_root)/'manifest.json', MANIFEST_HASH, 'fingerprint')
    snapshot = _json(Path(rules_path).read_bytes())
    body = {k:v for k,v in snapshot.items() if k not in ('retrieved_at_utc','rules_fingerprint')}
    if fingerprint(body)!=RULES_HASH or snapshot.get('rules_fingerprint')!=RULES_HASH:
        raise ValueError('Frozen rules mismatch')
    rules = {s:SymbolRules(**body['normalized_rules'][s]) for s in SYMBOLS}
    datasets,views,features = {},{},{}
    for symbol in SYMBOLS:
        data, _december_count = load_local_development(historical_root,symbol)
        if data.fingerprint != DATA_HASHES[symbol]: raise ValueError('Frozen execution dataset mismatch')
        view = build_view(data,symbol,'4h',REQUEST)
        if view.fingerprint != VIEW_HASHES[symbol]: raise ValueError('Frozen aggregate view mismatch')
        features[symbol] = FeatureView(load_history(phase6d0_root,symbol),view)
        datasets[symbol],views[symbol] = data,view
    return datasets,views,features,rules


@dataclass(frozen=True)
class VariantExecutionDecision(ExecutionDecision):
    parameters_json: str
    feature_fingerprint: str
    trigger: str


def _variant_execution(signal, event):
    metadata = dict(symbol=signal.symbol,timeframe=event.timeframe,open_time_ms=signal.open_time_ms,
        completed_through_ms=signal.open_time_ms-1,signal=signal.signal,
        completed_open_times_ms=signal.completed_open_times_ms,dataset_fingerprint=event.dataset_fingerprint,
        window_fingerprint=signal.visible_fingerprint,strategy_version=signal.strategy_version,
        strategy_source_sha256=signal.source_sha256,adapter_version='phase6d-variant-bridge-v1',
        ema_spans=signal.parameters.trigger_ema_spans,pandas_version=signal.pandas_version,
        numpy_version=signal.numpy_version,python_version=signal.python_version,
        signal_timeframe=signal.signal_timeframe,strategy_family=signal.strategy_family,
        reason=signal.reason,signal_decision_id=signal.decision_id,
        parameters_json=encoded(asdict(signal.parameters)).decode('ascii'),
        feature_fingerprint=signal.feature_fingerprint,trigger=signal.trigger)
    return VariantExecutionDecision(**metadata,decision_id=fingerprint(metadata))


@dataclass(frozen=True, init=False, eq=False)
class TrialSchedule:
    """Dry construction; calling requires separately authorized future execution."""
    def __init__(self, spec, views, features):
        if spec not in trial_plan().trials: raise ValueError('Frozen trial required')
        object.__setattr__(self,'registration',trial_mapping(spec))
        object.__setattr__(self,'views',MappingProxyType(dict(views)))
        object.__setattr__(self,'features',MappingProxyType(dict(features)))
        if set(features)!=set(SYMBOLS) or any(type(v) is not FeatureView for v in features.values()):
            raise ValueError('All authorized FeatureViews required')
        for symbol in SYMBOLS:
            if (dict(features[symbol].provenance) != dict(history=HISTORY_HASHES[symbol],development=VIEW_HASHES[symbol])
                    or views[symbol].fingerprint != VIEW_HASHES[symbol]):
                raise ValueError('Feature/signal view identity mismatch')
        object.__setattr__(self,'ema_schedule',ResearchSchedule(views,EMACrossover(EMAConfig())))
        kind = self.registration.strategy_type
        object.__setattr__(self,'strategies',MappingProxyType({s:(EMACrossover(EMAConfig()) if kind is EMACrossover else kind(features[s])) for s in SYMBOLS}))
        object.__setattr__(self,'_entries',{})

    def __call__(self, event):
        if type(event) is not OpeningEvent or event.timeframe!='15m' or event.symbol not in self.views:
            raise ValueError('Authorized 15m event required')
        self._entries.pop(event.symbol,None)
        if event.dataset_fingerprint != self.views[event.symbol].provenance['source_15m_dataset_fingerprint']:
            raise ValueError('Execution source mismatch')
        window = self.ema_schedule.window_at(event.symbol,event.open_time_ms)
        if window is None: return None
        if event.opening_price!=window.opening_price or event.completed_through_ms!=window.completed_through_ms:
            raise ValueError('Signal/execution boundary mismatch')
        snapshot = calculate_features(self.features[event.symbol].at_open(event.open_time_ms))
        if self.registration.strategy_type is EMACrossover:
            decision = self.ema_schedule(event)
        else:
            decision = _variant_execution(self.strategies[event.symbol].evaluate(snapshot),event)
        self._entries[event.symbol] = (decision,event,snapshot)
        return decision

    def protection_for_entry(self, decision, event):
        if self.registration.protection_type is not ATRProtection:
            raise PermissionError('A/B explicitly use fixed protection')
        saved = self._entries.get(event.symbol)
        if saved is None or saved[:2] != (decision,event) or decision.signal not in ('LONG','SHORT'):
            raise ValueError('Exact actionable entry-boundary snapshot required')
        return self.registration.protection_type(self.features[event.symbol],saved[2],self.ema_schedule,event)


def _scorecard(plan, spec, result, datasets, identity):
    card = build_scorecard(result,datasets,identity)
    card['policy'] = dict(phase6d_plan_fingerprint=plan.fingerprint, trial=spec.record,
        prior_policy_fingerprint=POLICY_HASH, eligibility=policy()['eligibility'])
    card['scorecard_fingerprint'] = fingerprint({k:v for k,v in card.items() if k!='scorecard_fingerprint'})
    return card


@dataclass(frozen=True)
class TrialContext:
    plan: TrialPlan
    spec: TrialSpec
    schedule: TrialSchedule
    datasets: object
    rules: object
    _issuer: object = field(default=None,repr=False,compare=False)
    _rules_fingerprint: str = field(default='',repr=False)

    @property
    def protection_provider(self):
        return None if self.schedule.registration.protection_type is None else self.schedule.protection_for_entry

    def destination(self, root): return artifact_destination(root,self.spec.id)

    def bind_runner(self, registry, attempt_id, artifact_root):
        """Validate an explicit future attempt and return a callable; never invokes it."""
        validate_plan(self.plan)
        if self._issuer is not _ISSUED or self.spec not in self.plan.trials or self.schedule.registration != trial_mapping(self.spec):
            raise PermissionError('Use a preflight-issued context')
        if set(self.datasets)!=set(SYMBOLS) or set(self.rules)!=set(SYMBOLS):
            raise ValueError('Exact context inputs required')
        if fingerprint({s:asdict(self.rules[s]) for s in SYMBOLS})!=self._rules_fingerprint:
            raise ValueError('Context rules changed')
        for symbol in SYMBOLS:
            if (self.datasets[symbol].fingerprint!=DATA_HASHES[symbol] or
                    build_view(self.datasets[symbol],symbol,'4h',REQUEST)!=self.schedule.views[symbol]):
                raise ValueError('Context data changed')
        if type(registry) is not Phase6DRegistry or registry.registered_plan()!=self.plan:
            raise PermissionError('Exact registered plan required')
        events = registry.events()
        registry._check_access(events,self.spec.id,REQUEST)
        attempts = [e for e in events if e['kind']=='ATTEMPT' and e['event_id']==attempt_id]
        if len(attempts)!=1: raise PermissionError('Explicit attempt required')
        attempt = attempts[0]['payload']
        if (attempt['experiment_id']!=self.spec.id or attempt['split']!=REQUEST.record or
                attempt['datasets']!=VIEW_HASHES or registry._find(events,'OUTCOME','attempt_id',attempt_id)):
            raise PermissionError('Mismatched/terminal attempt')
        if self.destination(artifact_root).exists(): raise FileExistsError('Partial/completed output collision')
        identity = dict(experiment_fingerprint=self.spec.id,run_fingerprint=attempt['run_id'],
            dataset_fingerprints=DATA_HASHES,view_fingerprints=VIEW_HASHES,split=REQUEST.record,
            strategy_fingerprint=fingerprint(self.spec.record),rules_fingerprint=RULES_HASH,
            source_hashes=self.spec.record['source_identity'],software_versions=self.spec.record['software_identity'])
        return partial(execute_validated,registry=registry,spec=self.spec,attempt_id=attempt_id,
            datasets=self.datasets,config=approved_configuration(),schedule=self.schedule,rules=self.rules,
            identity=identity,artifact_root=artifact_root,protection_provider=self.protection_provider,
            scorecard_builder=partial(_scorecard,self.plan,self.spec))


@dataclass(frozen=True)
class PreflightResult:
    metadata: CanonicalRecord
    contexts: tuple[TrialContext,...]

    @property
    def record(self): return self.metadata.record


def preflight(plan, *, registry=None, artifact_root=ROOT/'artifacts/research',
              historical_root=ROOT/'artifacts/baseline-001/historical',
              phase6d0_root=ROOT/'artifacts/research-data/phase6d0-warmup-v1',
              rules_path=ROOT/'artifacts/baseline-001/research-rules-v1.json'):
    """READY proves dry wiring; no features, signals, attempts or results generated."""
    validate_plan(plan)
    registry = Phase6DRegistry(Path(artifact_root)/'registry') if registry is None else registry
    if type(registry) is not Phase6DRegistry or registry.registered_plan()!=plan:
        raise PermissionError('Explicit registration of the exact plan required')
    counts = accounting(registry,plan)
    if counts['phase6d_attempts']: raise PermissionError('Execution history exists; reconcile before any later run')
    authorize(REQUEST,'4h')
    for spec in plan.trials:
        registry._check_access(registry.events(),spec.id,REQUEST)
        trial_mapping(spec)
        path = artifact_destination(artifact_root,spec.id)
        if path.exists() or path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
            raise FileExistsError('Partial/completed/misdirected trial output')
        if (Path(artifact_root)/spec.trial_id).exists(): raise ValueError('Mismatched readable artifact identity')
    datasets,views,features,rules = _inputs(historical_root,phase6d0_root,rules_path)
    contexts = tuple(TrialContext(plan,spec,TrialSchedule(spec,views,features),
        MappingProxyType(datasets),MappingProxyType(rules),_ISSUED,
        fingerprint({s:asdict(rules[s]) for s in SYMBOLS})) for spec in plan.trials)
    metadata = dict(status='READY',plan_fingerprint=plan.fingerprint,accounting=counts,
        trial_fingerprints={s.trial_id:s.id for s in plan.trials},policy_fingerprint=POLICY_HASH,
        eligibility_fingerprint=ELIGIBILITY_HASH,authorized_history_and_2024_data_read=True,
        features_calculated=False,signals_generated=False,simulation_run=False,
        performance_calculated=False,attempts_created=False,result_artifacts_created=False)
    return PreflightResult(CanonicalRecord(encoded(metadata)),contexts)
