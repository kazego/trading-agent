"""Explicit registered DEVELOPMENT execution. Importing this module does nothing.

No CLI, downloader, implicit registration, promotion, or default run inputs.
"""
from dataclasses import asdict
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import platform

import numpy
import pandas

from backtest.data import Dataset, _hash
from backtest.portfolio import simulate_portfolio
from backtest.simulation import SimulationConfig, CostMode, SymbolRules
from backtest.report import ASSUMPTIONS
from research.data import authorize, build_view
from research.registry import Registry
from research.schedule import ResearchSchedule
from research.scorecard import build_scorecard
from research.spec import (ExperimentSpec, Role, SYMBOLS, RULES_HASH, DEV_START,
    VALIDATION_START, canonical, encoded, fingerprint, policy, digest)
from research.strategy import EMACrossover, EMAConfig, VERSION


def approved_configuration():
    """Planning helper only. Execution still requires the explicit exact value."""
    return SimulationConfig(D('1000'),D('.01'),D('3'),D('.015'),D('.03'),
                            CostMode.FEES_AND_SLIPPAGE,D('.0005'),D('.0005'),D('2'))


def source_identity():
    root = Path(__file__).resolve().parent.parent
    paths = sorted([*root.joinpath('research').glob('*.py'),
                    *root.joinpath('backtest').glob('*.py'), root/'strategy.py', root/'requirements.txt'])
    return tuple((p.relative_to(root).as_posix(),hashlib.sha256(
        p.read_text(encoding='utf-8').encode('utf-8')).hexdigest()) for p in paths)


def software_identity():
    return tuple(sorted(dict(python=platform.python_version(),pandas=pandas.__version__,numpy=numpy.__version__).items()))


def planned_batch(view_fingerprints, *, source_hashes, software_versions):
    """Return three immutable plans; no registry records or execution are created."""
    if set(view_fingerprints) != {'15m','1h','4h'}: raise ValueError('Exactly three timeframe plans required')
    return tuple(ExperimentSpec(tf,VERSION,source_hashes,tuple(view_fingerprints[tf].items()),
        software_versions) for tf in ('15m','1h','4h'))


def ledger_summary(registry):
    """Count independent plans separately from attempts; preserve full event history."""
    events=registry.events()
    attempts=[e for e in events if e['kind']=='ATTEMPT']
    return dict(planned_configurations=sum(e['kind']=='EXPERIMENT' for e in events),
        attempts=len(attempts), retries=sum(e['payload']['attempt_number']>1 for e in attempts),
        failures=sum(e['kind']=='OUTCOME' and e['payload']['status']=='FAILED' for e in events),
        completed=sum(e['kind']=='OUTCOME' and e['payload']['status']=='COMPLETED' for e in events),
        duplicate_registrations=sum(e['kind']=='DUPLICATE' for e in events),
        history=events)


def validate_inputs(*, registry, spec, request, attempt_id, datasets, views, strategy, config, rules_snapshot):
    # Authorization precedes inspecting any supplied candle contents.
    if not isinstance(registry,Registry) or not isinstance(spec,ExperimentSpec):
        raise PermissionError('Registered typed experiment required')
    authorize(request,spec.signal_timeframe)
    events=registry.events()
    if registry._experiment(events,spec.id) != canonical(spec.record):
        raise PermissionError('Registered specification mismatch')
    registry._check_access(events,spec.id,request)
    attempts=[e for e in events if e['kind']=='ATTEMPT' and e['event_id']==attempt_id]
    if len(attempts)!=1: raise PermissionError('Explicit attempt required')
    attempt=attempts[0]['payload']
    if (attempt['experiment_id']!=spec.id or attempt['split']!=request.record
            or attempt['datasets']!=dict(spec.development_dataset_fingerprints)
            or registry._find(events,'OUTCOME','attempt_id',attempt_id)):
        raise PermissionError('Attempt is mismatched or already terminal')
    if (spec.source_hashes != tuple(sorted(source_identity())) or spec.software_versions!=software_identity()
            or spec.strategy_version!=VERSION or type(strategy) is not EMACrossover
            or strategy.config!=EMAConfig(9,21) or type(config) is not SimulationConfig
            or config!=approved_configuration()):
        raise PermissionError('Exact strategy/source/software/configuration required')
    if strategy.source_hash!=dict(spec.source_hashes)['research/strategy.py']:
        raise PermissionError('Strategy instance source differs from registered source')
    if set(datasets)!=set(SYMBOLS) or set(views)!=set(SYMBOLS): raise ValueError('Exact watchlist required')
    snapshot={k:v for k,v in rules_snapshot.items() if k not in ('retrieved_at_utc','rules_fingerprint')}
    if _hash(snapshot)!=RULES_HASH or rules_snapshot['rules_fingerprint']!=RULES_HASH:
        raise ValueError('Frozen research-rules-v1 required')
    rules={s:SymbolRules(**snapshot['normalized_rules'][s]) for s in SYMBOLS}
    for s in SYMBOLS:
        data=datasets[s]
        if not isinstance(data,Dataset): raise ValueError('Validated Dataset required')
        meta=data.provenance
        if (meta['start_ms'],meta['end_ms'],meta['timeframe'],meta['symbols'])!=(DEV_START,VALIDATION_START,'15m',[s]):
            raise PermissionError('Exact 2024 execution data required')
        if any(c.open_time_ms>=VALIDATION_START for c in data.candles):
            raise PermissionError('No validation/holdout candles allowed')
        if (_hash({k:v for k,v in meta.items() if k!='dataset_fingerprint'})!=data.fingerprint
                or _hash([c.record() for c in data.candles])!=meta['normalized_candle_fingerprint']):
            raise ValueError('Execution data integrity failure')
        rebuilt=build_view(data,s,spec.signal_timeframe,request)
        if rebuilt!=views[s] or rebuilt.fingerprint!=dict(spec.development_dataset_fingerprints)[s]:
            raise ValueError('Exact frozen signal view and components required')
    schedule=ResearchSchedule(views,strategy)
    identity=dict(experiment_fingerprint=spec.id,run_fingerprint=attempt['run_id'],
        dataset_fingerprints={s:datasets[s].fingerprint for s in SYMBOLS},
        view_fingerprints=dict(spec.development_dataset_fingerprints),
        strategy_fingerprint=fingerprint(dict(family='EMA_CROSSOVER',version=VERSION,
            parameters=asdict(strategy.config),source_sha256=strategy.source_hash)),rules_fingerprint=RULES_HASH,
        source_hashes=dict(spec.source_hashes),software_versions=dict(spec.software_versions),split=request.record)
    return schedule,rules,identity


def _write(path, content):
    with Path(path).open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def human_report(card):
    p=card['performance']
    lines=['DEVELOPMENT ONLY — not validation or holdout',
        'Research assumptions: modeled fee 0.0005 per fill; adverse slippage 0.0002 per fill.',
        'These are not actual account fees or exact historical transaction costs.',
        'research-rules-v1: static current exchange rules applied retrospectively.',
        'No claim of future profitability.',*ASSUMPTIONS,
        f"Evidence: {card['eligibility']['status']} ({', '.join(card['eligibility']['reasons'])})",
        f"Initial/final USDT: {p['starting_equity']} / {p['final_realized_equity']}",
        f"Gross/net P&L: {p['gross_pnl']} / {p['net_pnl']}; return %: {p['return_pct']}",
        f"Trades: {p['total_trades']}; gross PF: {p['profit_factor']}; net PF: {p['net_profit_factor']}",
        f"Net expectancy: {p['expectancy_per_trade']}; MTM DD %: {card['risk']['mtm']['max_drawdown_pct']}",
        f"Realized DD %: {card['risk']['realized']['max_drawdown_pct']}; modeled fees: {p['total_fees']}",
        'Fingerprint: '+card['scorecard_fingerprint']]
    return '\n'.join(lines)+'\n'


def execute(*, registry, spec, request, attempt_id, datasets, views, strategy, config, rules_snapshot, artifact_root):
    """Explicit opt-in API; caller registers/accesses/starts an attempt beforehand.

    Existing directories block execution, including interrupted attempts. Recovery
    is manual; never rerun a preserved result to repair reporting.
    """
    try:
        schedule,rules,identity=validate_inputs(registry=registry,spec=spec,request=request,attempt_id=attempt_id,
            datasets=datasets,views=views,strategy=strategy,config=config,rules_snapshot=rules_snapshot)
    except Exception as exc:
        if isinstance(registry,Registry) and isinstance(spec,ExperimentSpec):
            events=registry.events()
            if any(e['kind']=='ATTEMPT' and e['event_id']==attempt_id and e['payload']['experiment_id']==spec.id
                   for e in events) and not registry._find(events,'OUTCOME','attempt_id',attempt_id):
                registry.finish_attempt(attempt_id,'FAILED',error=type(exc).__name__)
        raise
    return execute_validated(registry=registry,spec=spec,attempt_id=attempt_id,datasets=datasets,
        config=config,schedule=schedule,rules=rules,identity=identity,artifact_root=artifact_root)


def artifact_destination(artifact_root, experiment_id):
    """One locator shared by preflight and execution."""
    digest(experiment_id)
    root = Path(artifact_root)
    if root.name != 'research': raise ValueError('Dedicated research artifact namespace required')
    return root / experiment_id / 'development'


def execute_validated(*, registry, spec, attempt_id, datasets, config, schedule, rules, identity,
                      artifact_root, protection_provider=None, scorecard_builder=None):
    """Shared artifact lifecycle, called only after phase-specific validation.

    This internal integration primitive is not an independently authorized runner.
    """
    output=artifact_destination(artifact_root,spec.id)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.mkdir()  # Exclusive reservation; do not catch and mark another attempt failed.
    try:
        approved=dict(identity=identity,spec=spec.record,configuration=asdict(config),rules=asdict_rules(rules))
        _write(output/'approved-inputs.json',encoded(approved))
        if protection_provider is None:
            result=simulate_portfolio(datasets,rules,config,decision_provider=schedule)
        else:
            result=simulate_portfolio(datasets,rules,config,decision_provider=schedule,
                                      protection_provider=protection_provider)
        # Preserve all financial and decision records BEFORE any analytics.
        _write(output/'first-result.json',encoded(dict(result=asdict(result),result_fingerprint=result.fingerprint)))
        builder=build_scorecard if scorecard_builder is None else scorecard_builder
        card=builder(result,datasets,dict(identity,portfolio_result_fingerprint=result.fingerprint))
        _write(output/'scorecard.json',encoded(card))
        _write(output/'report.txt',human_report(card).encode('utf-8'))
        manifest=dict(identity=identity,result_fingerprint=result.fingerprint,scorecard_fingerprint=card['scorecard_fingerprint'],
            file_sha256={name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in
                ('approved-inputs.json','first-result.json','scorecard.json','report.txt')})
        manifest['manifest_fingerprint']=fingerprint(manifest)
        _write(output/'run-manifest.json',encoded(manifest))
        _write(output/'fingerprint.txt',(card['scorecard_fingerprint']+'\n').encode('ascii'))
    except Exception as exc:
        registry.finish_attempt(attempt_id,'FAILED',error=type(exc).__name__)
        raise
    registry.finish_attempt(attempt_id,'COMPLETED',result_fingerprint=card['scorecard_fingerprint'])
    return card


def asdict_rules(rules):
    return {s:asdict(rules[s]) for s in SYMBOLS}


def write_comparison(comparison, directory):
    """Explicit immutable batch artifact, no selection or registry promotion."""
    if fingerprint({k:v for k,v in comparison.items() if k!='comparison_fingerprint'})!=comparison['comparison_fingerprint']:
        raise ValueError('Invalid comparison fingerprint')
    path=Path(directory)
    path.mkdir()  # Never overwrite a prior complete or partial comparison.
    _write(path/'comparison.json',encoded(comparison))
    lines=[comparison['notice'],'Pareto axes: '+', '.join(comparison['pareto_axes']),
        'No automatic validation nomination or promotion.',
        'TF | Return % | Gross PF | Net PF | Net expectancy | MTM DD % | Realized DD % | Trades | Fees | Friction % | Positive quarters | Positive month fraction | Evidence']
    def ratio(value):
        return str(value['value']) if value['value'] is not None else value['status']
    for row in comparison['rows']:
        p,r,c,t=row['performance'],row['risk'],row['costs'],row['temporal']
        lines.append(' | '.join(map(str,(row['timeframe'],p['return_pct'],ratio(p['profit_factor']),
            ratio(p['net_profit_factor']),p['expectancy_per_trade'],r['mtm']['max_drawdown_pct'],
            r['realized']['max_drawdown_pct'],p['total_trades'],c['modeled_fees'],
            c['friction_pct_starting_equity'],t['profitable_quarters'],t['profitable_month_fraction'],row['eligibility']['status']))))
    for row in comparison['rows']:
        lines.append(row['timeframe']+' actual-trade symbol/direction net USDT: '+', '.join(
            key+'='+str(value['net_pnl']) for key,value in row['attribution']['symbol_direction'].items()))
        op=row['operations']
        lines.append(row['timeframe']+' operations: '+', '.join(k+'='+str(op[k]) for k in
            ('accepted_entries','BLOCKED_OPEN_POSITION','BLOCKED_PRIORITY','eligibility_rejections','STOP_LOSS','TAKE_PROFIT','END_OF_DATA')))
    _write(path/'report.txt',('\n'.join(lines)+'\n').encode('utf-8'))
