"""Read-only historical verification and explicit, versioned current source scopes.

No runner, data loader, authorization writer or simulation is imported here.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from research.spec import encoded, fingerprint, digest, policy
from research.registry import Registry

SELECTION = '92ee399eccbb27b0b6cfa32efbcfea1e9e837569bf61befd6d777eff10014d84'
PLAN = '4172bd2610e0c51d8ef4df73aad21f3afe06656ca5f71e1ae5441ad650ca785b'
SNAPSHOT_C = 'c70d7e618e34832e5076c8ffcd6724353514fd39'
SNAPSHOT_D = '313f8bd9de8210fb113850813b4f2551789c686c'
SCHEMA = 'scoped-validation-identity-v1'
# Conservative whole-file locks: mixed data/feature files require a reviewed
# scope migration before calendar support can change. Never silently exempt them.
SEMANTIC_FILES = (
    'strategy.py', 'requirements.txt', 'backtest/__init__.py',
    'backtest/data.py', 'backtest/clock.py', 'backtest/strategy_adapter.py',
    'backtest/simulation.py', 'backtest/portfolio.py', 'research/__init__.py',
    'research/data.py', 'research/features.py', 'research/strategy.py',
    'research/schedule.py', 'research/protection.py',
)
INFRASTRUCTURE_FILES = (
    'research/identity.py', 'research/spec.py', 'research/registry.py',
    'research/runner.py', 'research/phase6d5.py', 'research/scorecard.py',
    'backtest/metrics.py', 'backtest/report.py',
)
PRIOR_MANIFESTS = {
    '94effde761681b2133e4164e25886b6463af735ca51407044c3c88e93f071ee7':
        '16f158c13c456d6e0fc05b452fa6690881928053b27e58c366fc4712afa31074',
    '41dd3eef1b985af4cca0f885a68997b721d60357a63b9f0b01f2f160d96f8288':
        '21f1792f8c803c04fb82427e76709a6b1668b7b705a7d9edf600c260a17c2741',
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Noncanonical number: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_float=invalid,
                      parse_constant=invalid)


def signed_record(path, field, expected):
    """Check an independently anchored legacy metadata fingerprint, read only."""
    record = _json(Path(path).read_bytes())
    _require(record.get(field) == digest(expected), 'Recorded identity differs')
    body = {k: v for k, v in record.items() if k != field}
    _require(fingerprint(body) == expected, 'Metadata integrity failure')
    return record


def _relative(name):
    p = PurePosixPath(name)
    _require(not p.is_absolute() and '..' not in p.parts and '\\' not in name
             and ':' not in name and str(p) == name, 'Unsafe source path')
    return name


def source_hashes(root, paths):
    """Explicit scope only; same universal-newline convention as legacy hashes."""
    return {name: sha((Path(root) / _relative(name)).read_text(encoding='utf8').encode('utf8'))
            for name in paths}


def verify_snapshot(root, source, software, revision):
    """Match every recorded hash to a pinned Git snapshot, never today's tree."""
    _require(bool(source) and bool(software), 'Missing historical identity')
    _require(re.fullmatch('[0-9a-f]{40}', revision) is not None, 'Full Git revision required')
    for name, expected in source.items():
        digest(expected)
        raw = subprocess.check_output(['git', 'show', revision + ':' + _relative(name)],
                                      cwd=root)
        text = raw.decode('utf8').replace('\r\n', '\n').replace('\r', '\n')
        _require(sha(text.encode('utf8')) == expected, 'Historical snapshot mismatch: ' + name)
    _require(all(isinstance(k, str) and isinstance(v, str) and v for k, v in software.items()),
             'Invalid historical software identity')
    return fingerprint(dict(source=source, software=software))


def verify_legacy_run(directory, expected_manifest, spec, revision, root):
    """Verify anchored file bytes, embedded result ID and source consistency.

    Financial result fingerprints are NOT regenerated; their immutable file
    checksum and stored ID are checked through the trusted manifest instead.
    """
    directory = Path(directory)
    manifest = signed_record(directory / 'run-manifest.json', 'manifest_fingerprint', expected_manifest)
    files = manifest['file_sha256']
    _require(set(files) == {'approved-inputs.json', 'first-result.json', 'scorecard.json', 'report.txt'},
             'Unexpected manifest file set')
    for name, expected in files.items():
        _require(sha((directory / name).read_bytes()) == digest(expected), 'Artifact tampering: ' + name)
    inputs = _json((directory / 'approved-inputs.json').read_bytes())
    identity = manifest['identity']
    _require(inputs['identity'] == identity and inputs['spec'] == spec, 'Input identity mismatch')
    _require(identity['experiment_fingerprint'] == fingerprint(spec), 'Experiment identity mismatch')
    source = dict(spec['source_identity'] if 'trial_id' in spec else spec['source_hashes'])
    software = dict(spec['software_identity'] if 'trial_id' in spec else spec['software_versions'])
    _require(identity['source_hashes'] == source
             and identity['software_versions'] == software, 'Source substitution')
    _require(_json((directory / 'first-result.json').read_bytes())['result_fingerprint']
             == manifest['result_fingerprint'], 'Result reference mismatch')
    signed_record(directory / 'scorecard.json', 'scorecard_fingerprint', manifest['scorecard_fingerprint'])
    verify_snapshot(root, source, software, revision)
    return manifest


def verify_history(root):
    """Explicit legacy Phase 6C/D/E path. Returns detached metadata; no writes."""
    root = Path(root)
    selection = signed_record(root / 'docs/phase6e-selection.json', 'selection_fingerprint', SELECTION)
    registry = Registry(root / 'artifacts/research/registry')
    _require(sha(registry.path.read_bytes()) == selection['evidence']['ledger_snapshot_sha256'],
             'Ledger snapshot changed; explicit new checkpoint required')
    events = registry.events()
    plans = [e['payload'] for e in events if e['kind'] == 'PHASE6D_PLAN']
    _require(len(plans) == 1 and plans[0]['plan_fingerprint'] == PLAN
             and fingerprint(plans[0]['plan']) == PLAN == selection['trial_plan_fingerprint'],
             'Plan integrity failure')
    specs = {fingerprint(s): s for s in plans[0]['plan']['trials']}
    specs.update({e['payload']['experiment_id']: e['payload']['spec']
                  for e in events if e['kind'] == 'EXPERIMENT'})
    _require(len(specs) == 6, 'Expected six historical trials')
    _require(specs[selection['selected_trial_fingerprint']] == selection['frozen_trial_spec'],
             'Selected specification mismatch')
    historical_policy = next(s['policy'] for s in specs.values() if 'policy' in s)
    _require(fingerprint(historical_policy) == selection['policy_fingerprint']
             and fingerprint(historical_policy['eligibility']) == selection['eligibility_fingerprint'],
             'Policy mismatch')
    anchors = dict(PRIOR_MANIFESTS)
    anchors.update({v['experiment']: v['manifest_fingerprint']
                    for v in selection['evidence']['identities'].values()})
    manifests = {}
    for identity, anchor in anchors.items():
        spec = specs[identity]
        revision = SNAPSHOT_D if 'trial_id' in spec else SNAPSHOT_C
        m = verify_legacy_run(root / 'artifacts/research' / identity / 'development',
                              anchor, spec, revision, root)
        attempts = [e for e in events if e['kind'] == 'ATTEMPT' and e['payload']['experiment_id'] == identity]
        _require(len(attempts) == 1, 'Unexpected historical attempt count')
        outcomes = [e['payload'] for e in events if e['kind'] == 'OUTCOME'
                    and e['payload']['attempt_id'] == attempts[0]['event_id']]
        _require(len(outcomes) == 1 and outcomes[0]['status'] == 'COMPLETED'
                 and outcomes[0]['result_fingerprint'] == m['scorecard_fingerprint'],
                 'Outcome mismatch')
        manifests[identity] = m
    for evidence in selection['evidence']['identities'].values():
        m = manifests[evidence['experiment']]
        _require(all(m[k] == evidence[k] for k in ('result_fingerprint', 'scorecard_fingerprint')),
                 'Selection evidence mismatch')
    signed_record(root / 'artifacts/research/phase6d6-comparison-v1/comparison.json',
                  'comparison_fingerprint', selection['comparison_fingerprint'])
    return selection


def infrastructure_identity(root, software):
    """Versioned scope. Future new infrastructure must be explicitly added here."""
    record = dict(schema=SCHEMA, source=source_hashes(root, INFRASTRUCTURE_FILES), software=software)
    return dict(record=record, fingerprint=fingerprint(record))


def candidate_binding(root, proposed_spec, software):
    """Read-only future-artifact binding; this grants NO validation permission.

    Historical data/split provenance remains in the selected spec. A future
    validation artifact must additionally bind its separately authorized split.
    """
    selection = verify_history(root)
    frozen = selection['frozen_trial_spec']
    _require(fingerprint(policy()) == selection['policy_fingerprint'], 'Frozen policy changed')
    _require(proposed_spec == frozen, 'Frozen candidate specification changed')
    _require(software == frozen['software_identity'], 'Unreviewed runtime change')
    expected = {p: frozen['source_identity'][p] for p in SEMANTIC_FILES}
    _require(source_hashes(root, SEMANTIC_FILES) == expected, 'Candidate/execution source changed')
    semantic_keys = ('strategy_family', 'strategy_version', 'strategy_parameters', 'signal_timeframe',
        'execution_timeframe', 'feature_version', 'feature_parameters', 'protection_family',
        'protection_version', 'protection_parameters', 'risk_basis', 'configuration',
        'symbols', 'priority', 'max_positions', 'funding', 'margin_liquidation_model', 'rules_fingerprint')
    semantics = {k: frozen[k] for k in semantic_keys}
    semantics['protection_behavior'] = selection['protection_behavior']
    record = dict(schema=SCHEMA, selection_fingerprint=SELECTION,
        historical_trial_fingerprint=selection['selected_trial_fingerprint'],
        historical_result_fingerprint=selection['selected_result_fingerprint'],
        historical_source_identity=dict(source=frozen['source_identity'], software=frozen['software_identity']),
        candidate_semantics=dict(parameters=semantics, source=expected, software=software),
        current_infrastructure_identity=infrastructure_identity(root, software))
    return dict(record=record, fingerprint=fingerprint(record))
