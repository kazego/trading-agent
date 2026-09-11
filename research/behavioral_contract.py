"""Forward behavioral contract; golden checks never authorize changed source."""
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from importlib.metadata import version
import platform

from research import identity as history
from research.spec import encoded, fingerprint, canonical, DEV_START

VERSION = 'semantic-contract-v2'
IDENTITY2 = 'a3a8321028270b005f2fd1fa094bba8f6ad4973d56bb7f42eaf9c307d1c55328'
FIXTURE_FILE = 'research/semantic_contract_v2.json'
# Full implementation evidence is separate from the stable behavioral contract.
IMPLEMENTATION_FILES = tuple(dict.fromkeys(history.SEMANTIC_FILES + history.INFRASTRUCTURE_FILES + (
    'research/semantic_identity.py', 'research/validation.py', 'research/behavioral_contract.py',
    'research/variants.py', 'research/regime_diagnostics.py',
    'backtest/acquire.py', 'backtest/baseline.py', 'backtest/diagnostics.py',
)))
SPEC = dict(
    strategy=dict(fast=9, slow=21, adjust=False, rows=100, completed_rows=99,
        previous_index=-3, latest_index=-2, long='previous_fast<=slow AND latest_fast>slow',
        short='previous_fast>=slow AND latest_fast<slow', otherwise='NONE', stateless=True),
    temporal=dict(signal='4h', execution='15m', history=600, forming='open-only OHLC; volume zero',
        completed='close_time < decision_open', evaluation='[start,end)', alignment='UTC epoch'),
    atr=dict(period=14, input_rows=100, true_range='max(high-low,abs(high-prev_close),abs(low-prev_close))',
        seed='mean(first 14 TR of 99 transitions)', recurrence='(13*previous+TR)/14', frozen_at_entry=True),
    protection=dict(stop_multiple='2', target_multiple='4', trailing=False,
        basis='actual slipped entry', rounding='away from entry', dual_touch='STOP_LOSS',
        stop_gap='worse open', target_gap='no improvement', end_of_data='final completed close'),
    sizing=dict(equity='1000', risk='0.01', max_leverage='3', basis='actual rounded stop distance',
        quantity_rounding='DOWN', filters='per-symbol', entry_fee_margin_reserve=True),
    portfolio=dict(priority=['BTCUSDT','ETHUSDT','ADAUSDT'], max_positions=1,
        equity='realized', no_same_boundary_reentry=True),
    costs=dict(mode='FEES_AND_SLIPPAGE', entry_fee='0.0005', exit_fee='0.0005',
        adverse_slippage_bps='2', funding=False, liquidation=False),
    data=dict(prices='finite positive Decimal', volume='finite nonnegative Decimal',
        ordering='strict contiguous 15m', aggregation='first open/max high/min low/last close/exact volume sum',
        components_4h=16, duplicates='reject', gaps='reject', incomplete='reject'),
)
PROBES = frozenset(('features','signals','forming','history_rejection','schedule','aggregation',
                    'order_rejection','atr_levels','sizing','slippage','configuration'))


def contract(root):
    """Semantic ID depends on explicit specification and frozen observations only."""
    raw = (Path(root)/FIXTURE_FILE).read_bytes()
    record = history._json(raw)
    if raw.rstrip(b'\n') != encoded(record) or set(record) != {'schema','spec','golden'}:
        raise ValueError('Canonical complete contract required')
    if record['schema'] != VERSION or record['spec'] != SPEC or set(record['golden']) != PROBES:
        raise ValueError('Missing or mutated contract specification/fixture component')
    if any(v is None or v == [] or v == {} for v in record['golden'].values()):
        raise ValueError('Empty golden component')
    return record


def observe():
    """Small synthetic component calls only: never a simulation or portfolio run.

    Test-only window issuance isolates kernels from historical data loaders.
    These finite probes do not claim universal behavioral equivalence.
    """
    from research import features as f, strategy as s, data as d, schedule as sch
    from research.protection import ATRProtection
    from research.runner import approved_configuration
    from backtest import simulation as sim
    from backtest.data import Candle
    step = d.STEPS['4h']
    candles = tuple(d.AggregateCandle('BTCUSDT','4h',DEV_START-(600-j)*step,
        DEV_START-(599-j)*step-1,D(100),D(102+j%7),D(98-j%3),D(100+j%2),D(1)) for j in range(600))
    snapshot = f.calculate_features(f._window('BTCUSDT',DEV_START,candles))
    features = dict(emas=[asdict(e) for e in snapshot.emas], atr=snapshot.atr14,
                    ribbon=snapshot.ribbon, breakout=asdict(snapshot.breakout))
    def window(values, price=D(100)):
        cs = tuple(d.AggregateCandle('BTCUSDT','4h',DEV_START-(99-j)*step,
            DEV_START-(98-j)*step-1,D(100),max(D(100),D(v)),min(D(100),D(v)),D(v),D(1))
            for j,v in enumerate(values))
        visible = dict(symbol='BTCUSDT',timeframe='4h',open_time_ms=DEV_START,
            completed=[c.record() for c in cs],forming=dict(open=str(price),high=str(price),low=str(price),close=str(price),volume='0'))
        return s._issued_window(d.SignalOpening('BTCUSDT','4h',DEV_START,DEV_START-1,cs,price,fingerprint(visible)))
    cases = ([100]*97+[90,110], [100]*97+[110,90], [100]*99, [100]*98+[101], [100]*98+[99])
    strategy = s.EMACrossover()
    signals = [strategy.evaluate(window(c)).signal for c in cases]
    frame = s._frame(window(cases[0],D(10000)))
    forming = dict(rows=len(frame),previous=str(frame.iloc[-3]['close']),
                   latest=str(frame.iloc[-2]['close']),last=[str(x) for x in frame.iloc[-1][['open','high','low','close','volume']]])
    def rejected(fn):
        try: fn()
        except (ValueError, PermissionError): return True
        return False
    # Missing required constructor remains an error; an unused optional argument
    # leaves this normal entry-point behavior intact.
    constructor_rejects = rejected(lambda: sch.ResearchSchedule({}, strategy))
    class View:
        provenance = dict(start_ms=DEV_START,end_ms=DEV_START+step,signal_timeframe='4h')
        def at_open(self,t):
            w=window(cases[0])
            return d.SignalOpening(w.symbol,w.timeframe,t,t-1,w.completed,w.opening_price,w.visible_fingerprint)
    holder = SimpleNamespace(_views={'BTCUSDT':View()})
    schedule = dict(constructor_rejects=constructor_rejects,
        at_boundary=sch.ResearchSchedule.window_at(holder,'BTCUSDT',DEV_START).open_time_ms,
        between=sch.ResearchSchedule.window_at(holder,'BTCUSDT',DEV_START+900000) is None,
        end_rejects=rejected(lambda: sch.ResearchSchedule.window_at(holder,'BTCUSDT',DEV_START+step)))
    components = tuple(Candle('BTCUSDT','15m',DEV_START+j*900000,DEV_START+(j+1)*900000-1,
        D(100+j),D(120+j),D(80-j),D(101+j),D('0.1')) for j in range(16))
    config = approved_configuration()
    rules = sim.SymbolRules('BTCUSDT','synthetic',D('0.1'),D('0.001'),D('0.001'),D(1000),D(1))
    atr = SimpleNamespace(atr=D('1.03'))
    return canonical(dict(features=features, signals=signals, forming=forming,
        history_rejection=rejected(lambda: strategy.evaluate(window([100]*98))), schedule=schedule,
        aggregation=d.aggregate_bucket(components,'4h').record(),
        order_rejection=[rejected(lambda cs=cs: d.aggregate_bucket(cs,'4h')) for cs in
                         (components[:-1],components[::-1],components[:-1]+(components[-2],))],
        atr_levels=[ATRProtection.levels(atr,D(100),side,rules) for side in ('LONG','SHORT')],
        sizing=[sim.size_quantity(D(1000),D(100),D(stop),'LONG',rules,config,price_distance_risk=True)
                for stop in ('98','95','97.9','99.99')],
        slippage=[sim.slipped_price(D(100),side,config) for side in ('BUY','SELL')], configuration=asdict(config)))


def implementation_identity(root):
    return fingerprint(dict(schema=VERSION, sources=history.source_hashes(root,IMPLEMENTATION_FILES),
        software=dict(python=platform.python_version(),pandas=version('pandas'),numpy=version('numpy'))))


def verify(root, *, expected_contract, approved_implementation):
    """Both approvals mandatory. Passing finite golden vectors is not approval."""
    record = contract(root)
    if fingerprint(record) != expected_contract:
        raise ValueError('Behavioral contract changed')
    if implementation_identity(root) != approved_implementation:
        raise ValueError('Implementation changed; separate review required')
    if observe() != record['golden']:
        raise ValueError('Behavioral fixture mismatch')
    return expected_contract


def migration_binding(root, *, expected_contract, approved_implementation):
    selection = history.verify_history(root)
    # Identity2 is verified against its original recorded implementation. No
    # redefinition of that fingerprint to make current structural edits pass.
    from research.semantic_identity import snapshot_sources, component_identity
    old = snapshot_sources(root)
    for p,text in old.items():
        if history.sha(text.encode()) != selection['frozen_trial_spec']['source_identity'][p]:
            raise ValueError('Historical source mismatch')
    if component_identity(old,old)['semantic_fingerprint'] != IDENTITY2:
        raise ValueError('Identity2 historical verification failed')
    verified = verify(root,expected_contract=expected_contract,approved_implementation=approved_implementation)
    body = dict(schema=VERSION,identity2=IDENTITY2,selection=history.SELECTION,
        historical_result=selection['selected_result_fingerprint'],contract=verified,
        implementation=approved_implementation, authorization='IDENTITY_ONLY')
    return dict(record=body,fingerprint=fingerprint(body))
