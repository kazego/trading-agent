"""Explicit local activation; exclusive immutable manifest, never implicit T0."""
import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path

from research.spec import encoded, fingerprint
from forward.model import CONTRACT, SELECTION, FOUR_HOURS, HISTORY, LABEL
from backtest.data import SYMBOLS, _timestamp

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('forward/__init__.py', 'forward/__main__.py', 'forward/model.py',
           'forward/public_feed.py', 'forward/manifest.py', 'forward/store.py',
           'forward/observer.py', 'backtest/data.py', 'research/data.py',
           'research/features.py', 'research/spec.py', 'runtime.py', 'errors.py')


def implementation():
    return fingerprint(dict(sources={p: hashlib.sha256((ROOT/p).read_text(encoding='utf-8').encode()).hexdigest()
                                    for p in SOURCES},
        software=dict(python=platform.python_version(), pandas=version('pandas'), numpy=version('numpy'))))


def specification():
    return dict(label=LABEL, schema='live-0a-v1', candidate='phase6d-c-atr-v1',
        selection=SELECTION, semantic_contract=CONTRACT, implementation=implementation(),
        symbols=list(SYMBOLS), signal_timeframe='4h', observation_timeframe='15m',
        history_ms=HISTORY, ema=[9,21], ema_completed_rows=99,
        atr_period=14, atr_input_rows=100, stop_atr='2', target_atr='4',
        atr_frozen_at_entry=True, trailing=False, planned_risk='0.01', max_leverage='3',
        priority=list(SYMBOLS), max_simulated_positions=1, execution_implemented=False,
        contextual_timeframes=['1D','1H','15M'], context_role='DESCRIPTIVE_ONLY',
        entry_selection_implemented=False, costs_implemented=False,
        reference_fee_per_fill='0.0005', reference_adverse_slippage='0.0002',
        funding=False, liquidation=False, reference_equity='1000',
        timing='strictly after T0; completed 4h only', max_online_lateness_ms=120000,
        delayed_policy='persist as DELAYED_RECONSTRUCTED; never label online',
        public_host='fapi.binance.com', public_paths=['/fapi/v1/time','/fapi/v1/klines'],
        evidence='observed history/live interval cannot remain untouched holdout')


def verify_references():
    contract = json.loads((ROOT/'research/semantic_contract_v2.json').read_bytes())
    selection = json.loads((ROOT/'docs/phase6e-selection.json').read_bytes())
    if (fingerprint(contract) != CONTRACT or selection.get('selection_fingerprint') != SELECTION
            or fingerprint({k:v for k,v in selection.items() if k != 'selection_fingerprint'}) != SELECTION):
        raise ValueError('Frozen candidate reference mismatch')


def freeze(directory, *, t0, created_ms):
    """Explicit operator-only call. Refuses existing directory, even if incomplete."""
    _timestamp(t0); _timestamp(created_ms)
    if type(t0) is not int or type(created_ms) is not int or t0 % FOUR_HOURS or t0 < created_ms+300000:
        raise ValueError('T0 must be an explicit future UTC 4h boundary, at least five minutes ahead')
    verify_references()
    record = dict(spec=specification(), t0_ms=t0, created_ms=created_ms,
                  history_start_ms=t0-HISTORY, first_observation_ms=t0+FOUR_HOURS)
    record['manifest_fingerprint'] = fingerprint(record)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    with (directory/'manifest.json').open('xb') as stream:
        stream.write(encoded(record)+b'\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())
    return record


def load(directory):
    verify_references()
    raw = (Path(directory)/'manifest.json').read_bytes()
    record = json.loads(raw)
    if raw != encoded(record)+b'\n' or set(record) != {'spec','t0_ms','created_ms','history_start_ms','first_observation_ms','manifest_fingerprint'}:
        raise ValueError('Invalid canonical manifest')
    body = {k:v for k,v in record.items() if k != 'manifest_fingerprint'}
    if (fingerprint(body) != record['manifest_fingerprint'] or record['spec'] != specification()
            or type(record['t0_ms']) is not int or record['t0_ms'] % FOUR_HOURS
            or record['t0_ms'] < record['created_ms']+300000
            or record['history_start_ms'] != record['t0_ms']-HISTORY
            or record['first_observation_ms'] != record['t0_ms']+FOUR_HOURS):
        raise ValueError('Manifest identity, implementation or T0 changed')
    return record
