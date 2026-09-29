"""Explicit frozen rules and read-only closed-candle provenance for paper B3."""
from dataclasses import dataclass, asdict
from pathlib import Path
from decimal import Decimal, InvalidOperation, localcontext

from backtest.data import Candle, SYMBOLS
from backtest.simulation import SymbolRules
from forward.manifest import load
from forward.operations import load_operations, verify_checkpoint
from forward.readout import connect
from paper.model import LABEL, SourceIdentity, parse
from paper.source import SourceAdapter
from paper.acceptance import timestamp
from research.spec import canonical, encoded, fingerprint, digest


FILTER_SCOPE = {
    'PRICE_FILTER': 'REQUIRED_STATIC_RULE',
    'LOT_SIZE': 'REQUIRED_STATIC_RULE',
    'MARKET_LOT_SIZE': 'REQUIRED_STATIC_RULE',
    'MIN_NOTIONAL': 'REQUIRED_STATIC_RULE',
    'MAX_NUM_ORDERS': 'NOT_APPLICABLE_NON_EXECUTING',
    'PERCENT_PRICE': 'DYNAMIC_EXCHANGE_RULE_OUTSIDE_FROZEN_MODEL',
    'POSITION_RISK_CONTROL': 'OUTSIDE_PAPER_SCOPE_UNDOCUMENTED',
}


def _number(value):
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError('Exact decimal string required')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Invalid decimal string') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('Finite nonnegative decimal required')
    return result


def _validate_static(payload, rules):
    """Validate immutable raw provenance without assigning unknown Binance semantics."""
    if type(payload) is not bytes:
        raise ValueError('Immutable canonical provenance required')
    data = parse(payload)
    if encoded(data) != payload or set(data) != {'endpoint', 'acquired_utc', 'response_sha256', 'symbols'}:
        raise ValueError('Malformed static provenance')
    if data['endpoint'] != 'https://fapi.binance.com/fapi/v1/exchangeInfo':
        raise ValueError('Official exchangeInfo provenance required')
    from datetime import datetime
    acquired = datetime.fromisoformat(data['acquired_utc'].replace('Z', '+00:00'))
    if acquired.utcoffset() is None or acquired.utcoffset().total_seconds() != 0:
        raise ValueError('UTC acquisition provenance required')
    digest(data['response_sha256'])
    if type(data['symbols']) is not list or len(data['symbols']) != 3:
        raise ValueError('Exact symbol provenance required')
    fields = {
        'PRICE_FILTER': {'minPrice', 'maxPrice', 'tickSize'},
        'LOT_SIZE': {'minQty', 'maxQty', 'stepSize'},
        'MARKET_LOT_SIZE': {'minQty', 'maxQty', 'stepSize'},
        'MIN_NOTIONAL': {'notional'}, 'MAX_NUM_ORDERS': {'limit'},
        'PERCENT_PRICE': {'multiplierUp', 'multiplierDown', 'multiplierDecimal'},
        'POSITION_RISK_CONTROL': {'positionControlSide'},
    }
    for item, rule in zip(data['symbols'], rules):
        if set(item) != {'symbol', 'status', 'contractType', 'quoteAsset', 'marginAsset', 'filters', 'classifications'}:
            raise ValueError('Malformed symbol provenance')
        if (item['symbol'] != rule.symbol or item['status'] != 'TRADING'
                or item['contractType'] != 'PERPETUAL' or item['quoteAsset'] != 'USDT'
                or item['marginAsset'] != 'USDT' or item['classifications'] != FILTER_SCOPE):
            raise ValueError('Symbol or filter classification mismatch')
        filters = item['filters']
        if type(filters) is not list or len(filters) != len(fields):
            raise ValueError('Missing or duplicate filters')
        indexed = {}
        for f in filters:
            kind = f.get('filterType')
            if kind not in fields or kind in indexed or set(f) != fields[kind] | {'filterType'}:
                raise ValueError('Unknown, duplicate or malformed filter')
            indexed[kind] = f
        p, lot, market = (indexed[k] for k in ('PRICE_FILTER', 'LOT_SIZE', 'MARKET_LOT_SIZE'))
        low, high, tick = (_number(p[k]) for k in ('minPrice', 'maxPrice', 'tickSize'))
        if tick <= 0 or (low and high and low > high):
            raise ValueError('Invalid static price bounds')
        with localcontext() as ctx:
            ctx.prec = 100
            if low % tick:
                raise ValueError('Unsupported price grid origin')
            for f in (lot, market):
                minimum, maximum, step = (_number(f[k]) for k in ('minQty', 'maxQty', 'stepSize'))
                if step <= 0 or minimum <= 0 or maximum < minimum or minimum % step:
                    raise ValueError('Invalid quantity grid')
        if _number(lot['stepSize']) != _number(market['stepSize']):
            raise ValueError('Incompatible quantity steps')
        expected = (tick, _number(lot['stepSize']), max(_number(lot['minQty']), _number(market['minQty'])),
                    min(_number(lot['maxQty']), _number(market['maxQty'])), _number(indexed['MIN_NOTIONAL']['notional']))
        if expected != (rule.tick_size, rule.quantity_step, rule.min_quantity, rule.max_quantity, rule.min_notional):
            raise ValueError('Raw/normalized static rule mismatch')
        if type(indexed['MAX_NUM_ORDERS']['limit']) is not int or indexed['MAX_NUM_ORDERS']['limit'] < 0:
            raise ValueError('Malformed order count')
        for k in ('multiplierUp', 'multiplierDown', 'multiplierDecimal'):
            _number(indexed['PERCENT_PRICE'][k])
        if type(indexed['POSITION_RISK_CONTROL']['positionControlSide']) is not str or not indexed['POSITION_RISK_CONTROL']['positionControlSide']:
            raise ValueError('Opaque position control string required')
    return data


@dataclass(frozen=True)
class RuleSnapshot:
    version: str
    source_identity: str
    rules: tuple[SymbolRules, ...]
    static_provenance: bytes | None = None

    def __post_init__(self):
        if type(self.version) is not str or not self.version.strip():
            raise ValueError('Explicit frozen rule version required')
        digest(self.source_identity)
        if (type(self.rules) is not tuple or any(type(r) is not SymbolRules for r in self.rules)
                or tuple(r.symbol for r in self.rules) != SYMBOLS):
            raise ValueError('Exact three-symbol normalized rule snapshot required')
        if self.static_provenance is not None:
            _validate_static(self.static_provenance, self.rules)

    @property
    def record(self):
        value = dict(label=LABEL, schema='paper-symbol-rules-v1', version=self.version,
                     source_identity=self.source_identity, rules=[asdict(r) for r in self.rules])
        if self.static_provenance is not None:
            value.update(schema='paper-symbol-rules-v2', static_provenance=parse(self.static_provenance),
                         price_bounds=[self.price_bounds(s) for s in SYMBOLS])
        return canonical(value)

    def price_bounds(self, symbol):
        if self.static_provenance is None:
            return None
        item = parse(self.static_provenance)['symbols'][SYMBOLS.index(symbol)]
        p = next(f for f in item['filters'] if f['filterType'] == 'PRICE_FILTER')
        return dict(symbol=symbol, min_price=p['minPrice'], max_price=p['maxPrice'], tick_size=p['tickSize'])

    def prices_allowed(self, symbol, *prices):
        if self.static_provenance is None:
            return True
        p = self.price_bounds(symbol)
        low, high = _number(p['min_price']), _number(p['max_price'])
        return all(price.is_finite() and price > 0 and (not low or price >= low)
                   and (not high or price <= high) for price in prices)

    @property
    def id(self):
        return fingerprint(self.record)

    def for_symbol(self, symbol):
        return self.rules[SYMBOLS.index(symbol)]


@dataclass(frozen=True)
class CandleEvidence:
    source: SourceIdentity
    candle: Candle
    row_digest: str
    received_ms: int

    def __post_init__(self):
        if type(self.source) is not SourceIdentity or type(self.candle) is not Candle:
            raise ValueError('Typed source and normalized closed candle required')
        digest(self.row_digest)
        if (self.row_digest != fingerprint(self.candle.record())
                or timestamp(self.received_ms) <= self.candle.close_time_ms):
            raise ValueError('Candle provenance/completion mismatch')

    @property
    def record(self):
        return dict(label=LABEL, version='paper-candle-evidence-v1', source=self.source.id,
                    candle=self.candle.record(), row_digest=self.row_digest, received_ms=self.received_ms)

    @property
    def id(self):
        return fingerprint(self.record)


class CandleSource:
    """No mutable connection exposed. Fetches no network data and fills no gaps."""
    def __init__(self, directory, expected):
        self._directory = Path(directory).resolve(strict=True)
        self._adapter = SourceAdapter(directory, expected)
        self._expected = expected

    def at(self, symbol, open_ms):
        if symbol not in SYMBOLS or timestamp(open_ms) % 900000:
            raise ValueError('Configured symbol and exact 15m opening required')
        self._adapter.snapshot()  # Unchanged complete source authorization chain.
        manifest = load(self._directory)
        operations = load_operations(self._directory, manifest)
        patch = parse((self._directory/'operations-patch-v1.json').read_bytes()[:-1])
        if (manifest['manifest_fingerprint'] != self._expected.manifest
                or operations['operations_fingerprint'] != self._expected.operations
                or patch['patch_fingerprint'] != self._expected.patch):
            raise ValueError('Source authorization changed during read')
        with connect(self._directory, manifest) as db:
            verify_checkpoint(db, operations['checkpoint'])
            verify_checkpoint(db, patch['checkpoint'])
            if ((self._directory/'BLOCKED.json').exists() or any(
                    parse(row[0].encode()).get('event') == 'BLOCKED'
                    for row in db.execute('SELECT payload FROM events'))):
                raise ValueError('Source is BLOCKED')
            row = db.execute('SELECT payload,digest,received_ms FROM candles WHERE symbol=? AND open_ms=?',
                             (symbol, open_ms)).fetchone()
            if row is None:
                return None
            candle = Candle(**parse(row[0].encode()))
            if candle.symbol != symbol or candle.open_time_ms != open_ms or encoded(candle.record()).decode() != row[0]:
                raise ValueError('Wrong source candle')
            return CandleEvidence(self._expected, candle, row[1], row[2])
