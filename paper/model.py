"""Immutable B1 draft identities. No activation boundary or execution objects."""
from dataclasses import dataclass, asdict, field
from decimal import Decimal
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform

from backtest.data import canonical_decimal, SYMBOLS, _timestamp
from research.spec import canonical, encoded, fingerprint, digest

LABEL = 'FORWARD_PAPER / NON_EXECUTING'
CONTRACT = 'b9db00e5e2ec6fcf6cc34cde7c2144a3bdba73556c8d2bb05c7c7450cf68c27d'


def parse(raw):
    if type(raw) is not bytes:
        raise ValueError('Immutable canonical bytes required')
    value=json.loads(raw)
    if encoded(value)!=raw:
        raise ValueError('Noncanonical payload')
    return value


def implementation():
    root=Path(__file__).resolve().parents[1]
    paths=('paper/__init__.py','paper/model.py','paper/source.py','paper/store.py',
           'research/spec.py','backtest/data.py','runtime.py','errors.py')
    return fingerprint(dict(version='live-0b-b1-implementation-v1',
        sources={p:hashlib.sha256((root/p).read_text(encoding='utf-8').encode()).hexdigest() for p in paths},
        software=dict(python=platform.python_version(),pandas=version('pandas'),numpy=version('numpy'))))


@dataclass(frozen=True)
class PaperConfig:
    starting_equity: Decimal = Decimal('1000')
    risk_fraction: Decimal = Decimal('0.01')
    leverage: Decimal = Decimal('3')
    fee_rate: Decimal = Decimal('0.0005')
    slippage_fraction: Decimal = Decimal('0.0002')

    def __post_init__(self):
        for name,expected in [('starting_equity','1000'),('risk_fraction','0.01'),
                              ('leverage','3'),('fee_rate','0.0005'),('slippage_fraction','0.0002')]:
            value=Decimal(canonical_decimal(getattr(self,name)))
            if value!=Decimal(expected): raise ValueError('Frozen paper configuration changed')
            object.__setattr__(self,name,value)

    @property
    def record(self):
        return canonical(dict(label=LABEL,version='live-0b-design-v1',**asdict(self),
            candidate='phase6d-c-atr-v1',semantic_contract=CONTRACT,symbols=SYMBOLS,priority=SYMBOLS,
            directions=('LONG','SHORT'),global_slots=1,signal_timeframe='4h',execution_timeframe='15m',
            ema=(9,21),ema_completed_rows=99,ewm_adjust=False,atr_period=14,stop_atr=2,target_atr=4,
            atr_frozen_at_entry=True,trailing=False,context='DESCRIPTIVE_ONLY',
            entry_offset_ms=900000,acceptance_margin_ms=60000,
            arbitration='ONE_SYMBOL_AT_ACCEPTANCE_NO_FUTURE_FALLBACK',
            delayed_entry='NEVER',late_pending_settlement='PRECOMMITTED_ONLY',
            dual_touch='STOP_LOSS',stop_gap='ADVERSE_OPEN',target_gap='NO_FAVORABLE_IMPROVEMENT',
            rule_policy='IMMUTABLE_SNAPSHOT',equity_basis='REALIZED_CLOSED_TRADE_BALANCE',
            mtm='CANDLE_CLOSE_PRICE_ONLY_ENTRY_FEES_DEFERRED',funding=False,liquidation=False))

    @property
    def id(self): return fingerprint(self.record)


@dataclass(frozen=True)
class SourceIdentity:
    manifest: str
    operations: str
    patch: str
    implementation: str
    first_observation_ms: int
    operational_effective_ms: int
    semantic_contract: str = CONTRACT

    def __post_init__(self):
        for name in ('manifest','operations','patch','implementation','semantic_contract'):
            digest(getattr(self,name))
        if self.semantic_contract!=CONTRACT: raise ValueError('Unknown semantic contract')
        for name in ('first_observation_ms','operational_effective_ms'):
            value=getattr(self,name)
            if type(value) is not int or _timestamp(value)%14400000: raise ValueError('4h UTC boundary required')
        if self.operational_effective_ms<self.first_observation_ms: raise ValueError('Invalid source boundaries')

    @property
    def record(self): return dict(label=LABEL,version='live-0a-source-v1',**asdict(self))

    @property
    def id(self): return fingerprint(self.record)


@dataclass(frozen=True)
class DraftManifest:
    source: SourceIdentity
    config: PaperConfig = field(default_factory=PaperConfig)
    rules_fingerprint: str | None = None
    prospective_start_ms: None = None
    implementation_fingerprint: str = field(default_factory=implementation)

    def __post_init__(self):
        if type(self.source) is not SourceIdentity or type(self.config) is not PaperConfig:
            raise ValueError('Typed immutable draft required')
        if self.prospective_start_ms is not None: raise ValueError('B1 cannot select/activate a start boundary')
        if self.rules_fingerprint is not None: digest(self.rules_fingerprint)
        digest(self.implementation_fingerprint)
        if self.implementation_fingerprint!=implementation(): raise ValueError('Unapproved B1 implementation')

    @property
    def record(self):
        return dict(label=LABEL,version='live-0b-b1-draft-v1',state='DRAFT_NOT_ACTIVATED',
            source=self.source.record,config=self.config.record,rules_fingerprint=self.rules_fingerprint,
            prospective_start_ms=None,implementation=self.implementation_fingerprint)

    @property
    def id(self): return fingerprint(self.record)


@dataclass(frozen=True)
class SourceObservation:
    source: SourceIdentity
    observation: bytes
    receipt: bytes | None

    def __post_init__(self):
        if type(self.source) is not SourceIdentity: raise ValueError('Source identity required')
        o=parse(self.observation)
        boundary=o['boundary_ms']
        if (type(boundary) is not int or boundary%14400000 or boundary<self.source.first_observation_ms
                or o['label']!=LABEL or o['manifest_fingerprint']!=self.source.manifest
                or type(o['observed_ms']) is not int or o['observed_ms']<boundary):
            raise ValueError('Malformed observation identity/time')
        decisions=o['decisions']
        if type(decisions) is not list or [d['symbol'] for d in decisions]!=list(SYMBOLS):
            raise ValueError('Exact symbol decision batch required')
        for d in decisions:
            if (d['label']!=LABEL or d['candidate']!='phase6d-c-atr-v1' or d['contract']!=CONTRACT
                    or d['signal_timeframe']!='4h' or d['boundary_ms']!=boundary
                    or d['completed_through_ms']!=boundary-1 or d['signal'] not in ('LONG','SHORT','NONE')
                    or type(d['atr14']) is not str or canonical_decimal(d['atr14'])!=d['atr14']
                    or Decimal(d['atr14'])<0
                    or fingerprint({k:v for k,v in d.items() if k!='signal_fingerprint'})!=d['signal_fingerprint']):
                raise ValueError('Decision provenance mismatch')
        contexts=o['descriptive_context']
        if set(contexts)!=set(SYMBOLS): raise ValueError('Context symbol mismatch')
        for context in contexts.values():
            if (set(context['values'])!={'1D','1H','15M'}
                    or fingerprint(context['values'])!=context['context_fingerprint']):
                raise ValueError('Context provenance mismatch')
        if 'operations_fingerprint' in o:
            if o['operations_fingerprint']!=self.source.operations or o['timing']!='UNCONFIRMED':
                raise ValueError('Operational receipt binding required')
        elif o['timing'] not in ('ONLINE','DELAYED_RECONSTRUCTED') or self.receipt is not None:
            raise ValueError('Invalid legacy timing')
        if self.receipt is not None:
            r=parse(self.receipt)
            if (r['label']!=LABEL or r['event'] not in ('RECEIPT','RECOVERED_RECEIPT')
                    or 'operations_fingerprint' not in o or r['operations_fingerprint']!=self.source.operations
                    or r['observation_fingerprint']!=fingerprint(o) or r['boundary_ms']!=boundary
                    or type(r['observed_ms']) is not int or r['observed_ms']<o['observed_ms']):
                raise ValueError('Receipt provenance mismatch')
            tolerance=300000 if boundary>=self.source.operational_effective_ms else 120000
            timing='DELAYED_RECONSTRUCTED'
            if r['event']=='RECEIPT':
                if r['tolerance_ms']!=tolerance: raise ValueError('Receipt policy changed')
                timing='ONLINE' if r['observed_ms']<=boundary+tolerance else timing
            elif r.get('code')!='COMMIT_TIME_UNKNOWN': raise ValueError('Unknown recovery receipt')
            if r['timing']!=timing: raise ValueError('Receipt classification mismatch')

    @property
    def boundary_ms(self): return parse(self.observation)['boundary_ms']

    @property
    def effective_timing(self):
        return parse(self.receipt)['timing'] if self.receipt else parse(self.observation)['timing']

    @property
    def record(self):
        return dict(label=LABEL,version='live-0b-source-evidence-v1',source_identity=self.source.id,
            observation=parse(self.observation),receipt=parse(self.receipt) if self.receipt else None)

    @property
    def id(self): return fingerprint(self.record)
