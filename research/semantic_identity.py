"""Identity-only mixed-module migration. Never imports the runtime modules."""
import ast
import copy
from pathlib import Path
import subprocess

from research import identity as legacy
from research.spec import fingerprint

SCHEMA = 'mixed-module-semantic-identity-v1'
COMPONENTS = {
    'research/features.py': ('_chronology', 'HistoryPackage', 'load_history', 'FeatureWindow',
        '_window', 'FeatureView', 'FeatureParameters', 'EMAValue', 'BreakoutState',
        '_breakout', '_atr', '_ribbon', 'FeatureSnapshot', 'calculate_features'),
    'research/strategy.py': ('EMAConfig', 'OpeningWindow', '_issued_window', 'SignalDecision',
        'Strategy', '_frame', 'EMACrossover', 'ExecutionDecision', '_execution_decision'),
    'research/schedule.py': ('ResearchSchedule',),
    'research/data.py': ('authorize', 'exact_sum', 'AggregateCandle', 'aggregate_bucket',
        'SignalOpening', 'SignalDataView', 'build_view', 'load_local_development'),
}
# Only existing calendar reference positions can vary. No arbitrary expression,
# function, comparison, history size, or new statement is excluded.
CALENDAR_NAMES = frozenset(('DEV_START', 'VALIDATION_START', 'HOLDOUT_START', 'START', 'DECEMBER_START'))
CALENDAR_ROLES = frozenset(('DEVELOPMENT', 'VALIDATION', 'HOLDOUT'))
LABELS = frozenset((
    'Only aligned 2024 DEVELOPMENT boundaries',
    'Warmup, non-boundary and validation/holdout evaluation forbidden',
    'Only 2024 DEVELOPMENT signal openings are authorized',
    'Outside authorized DEVELOPMENT execution boundaries',
    'Only DEVELOPMENT data access is implemented in Phase 6B2',
    'Exact 2024 DEVELOPMENT interval required',
))


def snapshot_sources(root):
    """Derive expected components from the same hash-verified historical snapshot."""
    result = {}
    for path in COMPONENTS:
        raw = subprocess.check_output(['git', 'show', legacy.SNAPSHOT_D + ':' + path], cwd=root)
        result[path] = raw.decode('utf8').replace('\r\n', '\n').replace('\r', '\n')
    return result


def _walk(node, path=()):
    yield path, node
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield from _walk(value, path + (field,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield from _walk(item, path + (field, index))


def _at(tree, path):
    for key in path:
        tree = tree[key] if isinstance(key, int) else getattr(tree, key)
    return tree


def _kind(node):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in CALENDAR_NAMES:
        return 'calendar_reference'
    if isinstance(node, ast.alias) and node.name in CALENDAR_NAMES and node.asname is None:
        return 'calendar_import'
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 'Role' and node.attr in CALENDAR_ROLES):
        return 'split_role'
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in LABELS:
        return 'authorization_label'
    return None


def _parse(path, text):
    tree = ast.parse(text, filename=path)
    named = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))]
    if tuple(named) != COMPONENTS[path]:
        raise ValueError('Missing, duplicate, renamed or unexpected component: ' + path)
    return tree


def extract(path, text, historical_text):
    """Only historically identified AST positions are calendar slots.

    The complete residual module AST is retained, including imports, globals,
    decorators, signatures, all component bodies and module-level statements.
    Thus dependencies or added hidden behavior cannot escape the frozen scope.
    """
    old = _parse(path, historical_text)
    current = _parse(path, text)
    slots = [(p, _kind(n)) for p, n in _walk(old) if _kind(n)]
    infrastructure = []
    for position, kind in slots:
        try:
            node = _at(current, position)
        except (AttributeError, IndexError, TypeError) as exc:
            raise ValueError('Calendar slot structure changed') from exc
        if _kind(node) != kind:
            raise ValueError('Unexpected calendar slot structure')
        infrastructure.append(dict(position=list(position), kind=kind, value=ast.dump(node)))
        if kind == 'calendar_reference': node.id = '__calendar_reference__'
        elif kind == 'calendar_import': node.name = '__calendar_import__'
        elif kind == 'split_role': node.attr = '__calendar_role__'
        else: node.value = '__authorization_label__'
    return dict(semantics=ast.dump(current, include_attributes=False), calendar=infrastructure)


def component_identity(current, historical):
    if set(current) != set(COMPONENTS) or set(historical) != set(COMPONENTS):
        raise ValueError('Exact mixed-module source set required')
    records = {p: extract(p, current[p], historical[p]) for p in COMPONENTS}
    expected = {p: extract(p, historical[p], historical[p])['semantics'] for p in COMPONENTS}
    actual = {p: r['semantics'] for p, r in records.items()}
    if actual != expected:
        raise ValueError('Frozen mixed-module semantics changed')
    return dict(semantic_fingerprint=fingerprint(dict(schema=SCHEMA, components=actual)),
                calendar_components={p: r['calendar'] for p, r in records.items()})


def migration_binding(root, proposed_spec, software):
    """New explicit identity path; legacy execution gates are NOT loosened."""
    selection = legacy.verify_history(root)
    frozen = selection['frozen_trial_spec']
    if proposed_spec != frozen or software != frozen['software_identity']:
        raise ValueError('Frozen specification/runtime changed')
    if fingerprint(legacy.policy()) != selection['policy_fingerprint']:
        raise ValueError('Frozen policy changed')
    historical = snapshot_sources(root)
    for path, text in historical.items():
        if legacy.sha(text.encode('utf8')) != frozen['source_identity'][path]:
            raise ValueError('Historical source substitution')
    current = {p: (Path(root) / p).read_text(encoding='utf8') for p in COMPONENTS}
    mixed = component_identity(current, historical)
    fixed_paths = tuple(p for p in legacy.SEMANTIC_FILES if p not in COMPONENTS)
    fixed = {p: frozen['source_identity'][p] for p in fixed_paths}
    if legacy.source_hashes(root, fixed_paths) != fixed:
        raise ValueError('Frozen protection/execution semantics changed')
    infrastructure = dict(schema=SCHEMA,
        base=legacy.infrastructure_identity(root, software),
        migration_source=legacy.source_hashes(root, ('research/semantic_identity.py',)),
        calendar_components=mixed['calendar_components'])
    record = dict(schema=SCHEMA, historical_selection=legacy.SELECTION,
        historical_trial=selection['selected_trial_fingerprint'],
        historical_result=selection['selected_result_fingerprint'],
        historical_source_identity=fingerprint(dict(source=frozen['source_identity'], software=software)),
        frozen_specification=fingerprint(frozen),
        candidate_semantic_identity=mixed['semantic_fingerprint'],
        execution_semantic_identity=fingerprint(fixed),
        infrastructure_identity=fingerprint(infrastructure), infrastructure=infrastructure)
    return dict(record=copy.deepcopy(record), fingerprint=fingerprint(record))
