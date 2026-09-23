"""Explicit frozen rules and read-only closed-candle provenance for paper B3."""
from dataclasses import dataclass, asdict
from pathlib import Path

from backtest.data import Candle, SYMBOLS
from backtest.simulation import SymbolRules
from forward.manifest import load
from forward.operations import load_operations, verify_checkpoint
from forward.readout import connect
from paper.model import LABEL, SourceIdentity, parse
from paper.source import SourceAdapter
from paper.acceptance import timestamp
from research.spec import canonical, encoded, fingerprint, digest


@dataclass(frozen=True)
class RuleSnapshot:
    version: str
    source_identity: str
    rules: tuple[SymbolRules, ...]

    def __post_init__(self):
        if type(self.version) is not str or not self.version.strip():
            raise ValueError('Explicit frozen rule version required')
        digest(self.source_identity)
        if (type(self.rules) is not tuple or any(type(r) is not SymbolRules for r in self.rules)
                or tuple(r.symbol for r in self.rules) != SYMBOLS):
            raise ValueError('Exact three-symbol normalized rule snapshot required')

    @property
    def record(self):
        return canonical(dict(label=LABEL, schema='paper-symbol-rules-v1', **asdict(self)))

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
